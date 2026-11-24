from asyncio import Lock, Semaphore, ensure_future, gather, sleep, wait_for
from logging import getLogger
from os import path as ospath, walk
from re import match as re_match, sub as re_sub, I as re_I
from time import time

from aioshutil import rmtree
from natsort import natsorted
from pyrogram import StopTransmission
from pyrogram.enums import ChatType, ParseMode
from pyrogram.errors import FloodWait, RPCError

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from aiofiles.os import (
    path as aiopath,
    remove,
    rename,
)
from pyrogram.types import (
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    ReplyParameters,
)

from ....core.config_manager import Config
from ....core.tg_client import BOT_MAX_BYTES, TgClient
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.files_utils import get_base_name, is_archive
from ...ext_utils.status_utils import get_readable_file_size, get_readable_time

from ...ext_utils.media_utils import get_md5_hash, get_media_info
from ...telegram_helper.message_utils import delete_message
from ...telegram_helper.tg_copy import call_with_flood_retry as _call_with_flood_retry
from ...ext_utils.hyperul_utils import HypertgUpload
from ...ext_utils.media_utils import format_tg_thumbnail

LOGGER = getLogger(__name__)


class TelegramUploader:
    def __init__(self, listener, path):
        self._processed_bytes = 0
        self._listener = listener
        self._path = path
        self._start_time = time()
        self._total_files = 0
        self._thumb = self._listener.thumb
        self._hd_thumb = None
        self._msgs_dict = {}
        self._corrupted = 0
        self._is_corrupted = False
        self._media_dict = {"videos": {}, "documents": {}}
        self._last_msg_in_group = False
        self._lprefix = ""
        self._lsuffix = ""
        self._lcaption = ""
        self._lfont = ""
        self._bot_pm = False
        self._media_group = False
        self._is_private = False
        self._sent_msg = None
        self._user_session = self._listener.transmission_mode in ("user", "both")
        self._hu: HypertgUpload | None = None
        self._file_progress_map = {}
        self._speed_map = {}
        self._error = ""
        self._upload_seq = []
        self._msg_to_seq = {}
        # Cache media-info (duration/quality/languages/subtitles) so split parts
        # of a merged file still get a full caption instead of empty languages.
        self._media_info_cache = {}
        self._media_lock = Lock()

    async def _user_settings(self):
        settings_map = {
            "MEDIA_GROUP": ("_media_group", False),
            "BOT_PM": ("_bot_pm", False),
            "LEECH_PREFIX": ("_lprefix", ""),
            "LEECH_SUFFIX": ("_lsuffix", ""),
            "LEECH_CAPTION": ("_lcaption", ""),
            "LEECH_FONT": ("_lfont", ""),
        }

        user_dict = self._listener.user_dict or {}
        for key, (attr, default) in settings_map.items():
            value = user_dict.get(key)
            if value is None:
                value = getattr(Config, key, default)
            setattr(self, attr, value)

        # Always deliver through the bot account. This avoids depending on the
        # user session for private-message delivery.
        self._bot_pm = True

        # Thumbnail resolution order:
        # 1) explicit -t / "none"
        # 2) THUMBNAIL_MODE: custom | auto | random | none
        # 3) saved user thumbnail file
        # 4) AUTO_THUMBNAIL → leave None so HypertgUpload fetches artwork
        explicit_thumb = self._listener.thumb
        mode = str(user_dict.get("THUMBNAIL_MODE") or "").strip().lower()
        saved_thumb = user_dict.get("THUMBNAIL") or f"thumbnails/{self._listener.user_id}.jpg"
        auto_thumb_on = user_dict.get("AUTO_THUMBNAIL")
        if auto_thumb_on is None:
            auto_thumb_on = getattr(Config, "AUTO_THUMBNAIL", True)

        if explicit_thumb == "none" or mode == "none":
            self._thumb = "none"
        elif explicit_thumb and explicit_thumb != "none":
            self._thumb = explicit_thumb
        elif mode == "custom":
            self._thumb = saved_thumb if await aiopath.isfile(saved_thumb) else None
        elif mode in ("auto", "random"):
            self._thumb = None  # resolved later by auto-thumbnail path
        elif user_dict.get("THUMBNAIL") and await aiopath.isfile(str(user_dict.get("THUMBNAIL"))):
            self._thumb = user_dict.get("THUMBNAIL")
        elif await aiopath.isfile(saved_thumb):
            self._thumb = saved_thumb
        elif auto_thumb_on:
            self._thumb = None  # HypertgUpload will call get_auto_thumbnail
        else:
            self._thumb = None

        if self._thumb not in (None, "none"):
            if not await aiopath.isfile(self._thumb):
                LOGGER.warning(f"Configured thumbnail does not exist: {self._thumb}")
                self._thumb = None
            else:
                # Telegram upload thumbnails must be JPEG, <=320x320 and <200 KB.
                formatted = await sync_to_async(format_tg_thumbnail, self._thumb, None, True)
                if formatted and await aiopath.isfile(formatted):
                    self._thumb = formatted
                else:
                    LOGGER.warning(f"Could not format thumbnail: {self._thumb}")
                    self._thumb = None

        self._hd_thumb = None
        if self._thumb not in (None, "none") and user_dict.get("HD_THUMBNAIL"):
            source = (
                explicit_thumb
                if explicit_thumb and explicit_thumb != "none" and await aiopath.isfile(explicit_thumb)
                else (saved_thumb if await aiopath.isfile(saved_thumb) else self._thumb)
            )
            hd = await sync_to_async(format_tg_thumbnail, source, None, True)
            self._hd_thumb = hd if hd and await aiopath.isfile(hd) else self._thumb

    async def _msg_to_reply(self):
        if self._user_session and TgClient.user is None:
            self._user_session = False

        # Prefer sudo-configured leech chat as the PRIMARY upload target.
        # User session (if present) uploads there first at full MTProto speed;
        # the bot then copies to user DM + dumps (no user session on those hops).
        leech_dests = getattr(self._listener, "leech_dests", None) or []
        first_dump = None
        if leech_dests:
            fd = leech_dests[0]
            first_dump = fd[0] if isinstance(fd, (tuple, list)) else fd

        leech_chat = (
            getattr(self._listener, "up_dest", None)
            or Config.LEECH_LOG_CHAT
            or first_dump
        )
        if leech_chat and not getattr(self._listener, "up_dest", None):
            self._listener.up_dest = leech_chat
            if leech_dests and isinstance(leech_dests[0], (tuple, list)) and len(leech_dests[0]) > 1:
                self._listener.chat_thread_id = leech_dests[0][1]

        if TgClient.user is not None and (Config.LEECH_LOG_CHAT or self._listener.up_dest or first_dump):
            # Always prefer user session for the primary high-speed upload hop to leech/dump chat.
            self._user_session = True

        if not self._listener.up_dest and not Config.LEECH_LOG_CHAT and not first_dump:
            # No leech chat configured — upload directly to User PM via bot only.
            if self._listener.pm_msg is not None:
                self._sent_msg = self._listener.pm_msg
            elif self._listener.message.chat.type == ChatType.PRIVATE:
                self._sent_msg = self._listener.message
            else:
                try:
                    self._sent_msg = await _call_with_flood_retry(
                        TgClient.bot.send_message,
                        chat_id=self._listener.user_id,
                        text=(
                            f"<b>💎 LMB · Task Started</b>\n"
                            f"<a href='{self._listener.source_url}'>Source</a>"
                        ),
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        disable_notification=True,
                    )
                    self._listener.pm_msg = self._sent_msg
                except Exception as e:
                    LOGGER.error(f"Cannot send to User PM {self._listener.user_id}: {e}")
                    await self._listener.on_upload_error(
                        "Start the bot in PM (Private) first so it can send your files!"
                    )
                    return False
            self._is_private = True
            return True

        # Anchor reply in the command chat; actual bytes go to leech chat via user session.
        self._sent_msg = self._listener.message
        self._is_private = self._sent_msg.chat.type == ChatType.PRIVATE
        return True

    async def _prepare_file(self, pre_file_, dirpath):
        cap_file_ = file_ = pre_file_
        lprefix = self._lprefix
        lsuffix = self._lsuffix
        lcaption = self._lcaption

        if lprefix:
            cap_file_ = lprefix.replace(r"\s", " ") + file_
            lprefix = re_sub(r"<.*?>", "", lprefix).replace(r"\s", " ")
            if not file_.startswith(lprefix):
                file_ = f"{lprefix}{file_}"

        if lsuffix:
            name, ext = ospath.splitext(cap_file_)
            cap_file_ = name + lsuffix.replace(r"\s", " ") + ext
            lsuffix = re_sub(r"<.*?>", "", lsuffix).replace(r"\s", " ")

        # Build the caption first, then apply the selected font to the final
        # caption. Previously only the bare filename was styled; a custom
        # LEECH_CAPTION replaced that value and silently discarded the font.
        lfont = self._lfont if self._lfont is not None else Config.LEECH_FONT
        cap_mono = cap_file_
        if lcaption:
            lcaption = re_sub(
                r"(\\\||\\\{|\\\}|\\s)",
                lambda m: {r"\|": "%%", r"\{": "&%&", r"\}": "$%$", r"\s": " "}[
                    m.group(0)
                ],
                lcaption,
            )

            parts = lcaption.split("|")
            parts[0] = re_sub(
                r"\{([^}]+)\}", lambda m: f"{{{m.group(1).lower()}}}", parts[0]
            )
            up_path = ospath.join(dirpath, pre_file_)
            probe_path = up_path
            # Split parts (.mkv.001 / .part2.rar style) are not full media files —
            # probe the original unsplit path when present, otherwise reuse cache.
            split_match = re_match(
                r"^(.+?)(?:\.part\d+(?:\.\w+)?|\.\d{3}|\.r\d+)$",
                pre_file_,
                flags=0,
            )
            if split_match:
                candidate = ospath.join(dirpath, split_match.group(1))
                if await aiopath.isfile(candidate):
                    probe_path = candidate
                else:
                    # Common pattern: name.mkv → name.mkv.001 (suffix after full name)
                    for ext in (
                        ".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v",
                        ".ts", ".mp3", ".m4a", ".flac", ".zip", ".rar", ".7z",
                    ):
                        base = pre_file_
                        for suffix in (r"\.\d{3}$", r"\.part\d+$", r"\.r\d+$"):
                            base2 = re_sub(suffix, "", base)
                            if base2 != base:
                                base = base2
                        cand2 = ospath.join(dirpath, base if base.endswith(ext) else f"{base}{ext}")
                        if await aiopath.isfile(cand2):
                            probe_path = cand2
                            break

            cache_key = ospath.abspath(probe_path)
            # Prefer any previously cached info for this logical file / basename family
            family_key = re_sub(
                r"(?:\.part\d+(?:\.\w+)?|\.\d{3}|\.r\d+)$",
                "",
                ospath.basename(pre_file_),
            )
            media_info_res = self._media_info_cache.get(cache_key) or self._media_info_cache.get(
                family_key
            )
            if not media_info_res:
                media_info_res = await get_media_info(probe_path, True)
                # If probe of split part failed (empty langs), try parent without numeric suffix
                if (
                    (not media_info_res or not any(media_info_res[2:5] if len(media_info_res) >= 5 else media_info_res[2:4]))
                    and probe_path == up_path
                    and split_match
                ):
                    # last resort: reuse any cache entry for this family
                    for k, v in self._media_info_cache.items():
                        if family_key and family_key in str(k):
                            media_info_res = v
                            break
                if media_info_res:
                    self._media_info_cache[cache_key] = media_info_res
                    if family_key:
                        self._media_info_cache[family_key] = media_info_res
            if len(media_info_res) >= 5:
                dur, qual, lang, subs, langr = media_info_res[:5]
            else:
                dur, qual, lang, subs = media_info_res[:4] if media_info_res else (0, "", "", "")
                langr = lang
            lang = ", ".join(
                dict.fromkeys(
                    item.strip()
                    for item in str(lang or "").split(",")
                    if item.strip()
                )
            )
            langr = ", ".join(
                dict.fromkeys(
                    item.strip()
                    for item in str(langr or "").split(",")
                    if item.strip()
                )
            )
            if not lang and langr:
                lang = langr
            if not langr and lang:
                langr = lang
            subs = ", ".join(
                dict.fromkeys(
                    item.strip()
                    for item in str(subs or "").split(",")
                    if item.strip()
                )
            )
            md5_val = (
                await sync_to_async(get_md5_hash, up_path)
                if "{md5_hash}" in parts[0]
                else ""
            )
            cap_mono = parts[0].format(
                filename=cap_file_,
                size=get_readable_file_size(await aiopath.getsize(up_path)),
                duration=get_readable_time(dur),
                quality=qual,
                languages=lang,
                languagesr=langr,
                subtitles=subs,
                md5_hash=md5_val,
                mime_type=self._listener.file_details.get("mime_type", "text/plain"),
                prefilename=self._listener.file_details.get("filename", ""),
                precaption=self._listener.file_details.get("caption", ""),
            )

            for part in parts[1:]:
                args = part.split(":")
                cap_mono = cap_mono.replace(
                    args[0],
                    args[1] if len(args) > 1 else "",
                    int(args[2]) if len(args) == 3 else -1,
                )
            cap_mono = re_sub(
                r"%%|&%&|\$%\$",
                lambda m: {"%%": "|", "&%&": "{", "$%$": "}"}[m.group()],
                cap_mono,
            )

        # Telegram font styles are HTML entities. Apply after custom-caption
        # formatting so both default filename captions and custom templates
        # receive the selected style. Restrict tags to the settings UI values.
        font_tags = {
            "b": "b",
            "i": "i",
            "code": "code",
            "tg-spoiler": "tg-spoiler",
            "spoiler": "tg-spoiler",
            "s": "s",
            "u": "u",
        }
        selected_tag = font_tags.get(str(lfont or "none").strip().lower())
        if cap_mono and selected_tag:
            cap_mono = f"<{selected_tag}>{cap_mono}</{selected_tag}>"

        if len(file_) > 60:
            if is_archive(file_):
                name = get_base_name(file_)
                ext = file_.split(name, 1)[1]
            elif match := re_match(r".+(?=\..+\.0*\d+$)|.+(?=\.part\d+\..+$)", file_):
                name = match.group(0)
                ext = file_.split(name, 1)[1]
            elif len(fsplit := ospath.splitext(file_)) > 1:
                name = fsplit[0]
                ext = fsplit[1]
            else:
                name = file_
                ext = ""
            if lsuffix:
                ext = f"{lsuffix}{ext}"
            name = name[: 64 - len(ext)]
            file_ = f"{name}{ext}"
        elif lsuffix:
            name, ext = ospath.splitext(file_)
            file_ = f"{name}{lsuffix}{ext}"

        old_path = ospath.join(dirpath, pre_file_)
        new_path = ospath.join(dirpath, file_)
        if old_path != new_path:
            await rename(old_path, new_path)

        return new_path, cap_mono

    def _get_input_media(self, subkey, key):
        rlist = []
        for msg in self._media_dict[key][subkey]:
            caption = msg.caption.html if msg.caption else None
            if key == "videos":
                input_media = InputMediaVideo(
                    media=msg.video.file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            else:
                input_media = InputMediaDocument(
                    media=msg.document.file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            rlist.append(input_media)
        return rlist

    async def _send_screenshots(self, dirpath, outputs):
        inputs = [
            InputMediaPhoto(ospath.join(dirpath, p), p.rsplit("/", 1)[-1])
            for p in outputs
        ]
        for i in range(0, len(inputs), 10):
            batch = inputs[i : i + 10]
            await _call_with_flood_retry(
                TgClient.bot.send_media_group,
                chat_id=self._listener.user_id,
                media=batch,
                disable_notification=True,
            )
            if self._listener.up_dest and self._listener.up_dest != self._listener.user_id:
                with_thread = (
                    {"message_thread_id": self._listener.chat_thread_id}
                    if self._listener.chat_thread_id
                    else {}
                )
                try:
                    self._sent_msg = (
                        await _call_with_flood_retry(
                            TgClient.bot.send_media_group,
                            chat_id=self._listener.up_dest,
                            media=batch,
                            disable_notification=True,
                            **with_thread,
                        )
                    )[-1]
                except Exception as e:
                    LOGGER.warning(f"Failed to send screenshots to dump {self._listener.up_dest}: {e}")

    async def _send_media_group(self, subkey, key, msgs):
        old_ids = [(msg[0], msg[1]) for msg in msgs]
        for index, msg in enumerate(msgs):
            if self._listener.transmission_mode == "both" or not self._user_session:
                msgs[index] = await _call_with_flood_retry(
                    self._listener.client.get_messages,
                    chat_id=msg[0],
                    message_ids=msg[1],
                )
            else:
                msgs[index] = await _call_with_flood_retry(
                    TgClient.user.get_messages, chat_id=msg[0], message_ids=msg[1]
                )
        media = self._get_input_media(subkey, key)
        del self._media_dict[key][subkey]
        if reply_target := msgs[0].reply_to_message:
            msgs_list = await _call_with_flood_retry(
                reply_target.reply_media_group,
                media=media,
                reply_parameters=ReplyParameters(message_id=reply_target.id),
                disable_notification=True,
            )
        else:
            msgs_list = await _call_with_flood_retry(
                (
                    self._listener.client
                    if self._listener.transmission_mode == "both"
                    or not self._user_session
                    else TgClient.user
                ).send_media_group,
                chat_id=msgs[0].chat.id,
                media=media,
                disable_notification=True,
                message_thread_id=(
                    self._listener.chat_thread_id
                    if msgs[0].chat.id == self._listener.up_dest
                    else None
                ),
            )
        for msg in msgs:
            if msg.link in self._msgs_dict:
                del self._msgs_dict[msg.link]
            await delete_message(msg)
        if self._listener.is_super_chat or self._listener.up_dest:
            for m in msgs_list:
                self._msgs_dict[m.link] = m.caption
        for i, (old_cid, old_mid) in enumerate(old_ids):
            old_key = (old_cid, old_mid)
            if old_key in self._msg_to_seq:
                seq_idx = self._msg_to_seq.pop(old_key)
                new_msg = msgs_list[i]
                self._upload_seq[seq_idx] = {
                    "chat_id": new_msg.chat.id,
                    "msg_id": new_msg.id,
                    "link": new_msg.link,
                    "file_": self._upload_seq[seq_idx]["file_"],
                    "user_session": self._upload_seq[seq_idx].get("user_session", False),
                }
                self._msg_to_seq[(new_msg.chat.id, new_msg.id)] = seq_idx
        self._sent_msg = msgs_list[-1]

    async def _copy_media(self):
        try:
            if self._bot_pm:
                await _call_with_flood_retry(
                    TgClient.bot.copy_message,
                    chat_id=self._listener.user_id,
                    from_chat_id=self._sent_msg.chat.id,
                    message_id=self._sent_msg.id,
                    reply_parameters=(
                        ReplyParameters(message_id=self._listener.pm_msg.id)
                        if self._listener.pm_msg
                        else None
                    ),
                )
        except Exception as err:
            if not self._listener.is_cancelled:
                err_msg = str(err)
                if "Can't copy" in err_msg:
                    LOGGER.warning(
                        f"BotPM copy skipped (restricted content): {err_msg}"
                    )
                else:
                    LOGGER.error(f"Failed To Send in BotPM:\n{err_msg}")

    async def _sequence_copies(self, src_chat):
        """Deliver every uploaded item to Bot PM and configured dump targets.

        For user-session uploads, first make a bot-authored copy in the upload
        chat (where possible). Keep the original until the DM and all requested
        extra destinations have been copied successfully, then remove it.
        """
        for entry in self._upload_seq:
            if not entry:
                continue

            original_chat = entry["chat_id"]
            original_msg = entry["msg_id"]
            # Track the uploader per message. A task may contain files on both
            # sides of the session threshold, so the instance-wide flag is not
            # reliable once concurrent upload tasks have completed.
            entry_uses_user = bool(entry.get("user_session", self._user_session))
            copy_from_chat = original_chat
            copy_from_msg = original_msg
            ghost_copy = None

            # Keep original user-session upload in the primary leech/dump chat
            # and copy directly via TgClient.bot (no duplicate message in primary chat).

            # If upload landed in a separate dump (e.g. dump-only mode), make
            # a bot-owned staging copy in the command/source chat as WZML does.
            if (
                original_chat != src_chat.id
                and not self._listener.up_dest
                and not ghost_copy
                and not entry_uses_user
            ):
                try:
                    staged = await _call_with_flood_retry(
                        TgClient.bot.copy_message,
                        chat_id=src_chat.id,
                        from_chat_id=original_chat,
                        message_id=original_msg,
                    )
                    copy_from_chat, copy_from_msg = src_chat.id, staged.id
                    entry["chat_id"], entry["msg_id"] = copy_from_chat, copy_from_msg
                    if getattr(staged, "link", None):
                        entry["link"] = staged.link
                except Exception as err:
                    LOGGER.error(f"Failed to stage upload from dump chat: {err}")
                    continue

            delivery_ok = True

            # 1) Always copy to the requesting user's private chat using bot.
            if copy_from_chat != self._listener.user_id:
                try:
                    dm_kwargs = {
                        "chat_id": self._listener.user_id,
                        "from_chat_id": copy_from_chat,
                        "message_id": copy_from_msg,
                    }
                    if self._listener.pm_msg:
                        dm_kwargs["reply_parameters"] = ReplyParameters(
                            message_id=self._listener.pm_msg.id
                        )
                    await _call_with_flood_retry(TgClient.bot.copy_message, **dm_kwargs)
                except Exception as err:
                    err_text = str(err)
                    # A stale/deleted reply target should not prevent delivery.
                    if "REPLY" in err_text.upper() or "MESSAGE_ID_INVALID" in err_text.upper():
                        try:
                            await _call_with_flood_retry(
                                TgClient.bot.copy_message,
                                chat_id=self._listener.user_id,
                                from_chat_id=copy_from_chat,
                                message_id=copy_from_msg,
                            )
                        except Exception as retry_err:
                            delivery_ok = False
                            LOGGER.error(f"Failed to deliver upload to Bot PM: {retry_err}")
                    else:
                        delivery_ok = False
                        if "Can't copy" in err_text:
                            LOGGER.warning(f"Bot PM copy restricted: {err_text}")
                        else:
                            LOGGER.error(f"Failed to deliver upload to Bot PM: {err_text}")

            # 2) Copy to primary, command-specific, and per-user extra dumps.
            extras = [
                (self._listener.up_dest, self._listener.chat_thread_id),
                (self._listener.cmd_up_dest, self._listener.cmd_thread_id),
                *getattr(self._listener, "cmd_up_dests", ()),
                *getattr(self._listener, "leech_dests", ()),
            ]
            primary_thread = (
                self._listener.chat_thread_id
                if copy_from_chat == self._listener.up_dest
                else None
            )
            done = {
                (copy_from_chat, None),
                (copy_from_chat, primary_thread),
                (self._listener.user_id, None),
            }
            async def _copy_to_dump(dest, thread_id):
                nonlocal delivery_ok
                try:
                    kwargs = {
                        "chat_id": dest,
                        "from_chat_id": copy_from_chat,
                        "message_id": copy_from_msg,
                    }
                    if thread_id:
                        kwargs["message_thread_id"] = thread_id
                    copied = await _call_with_flood_retry(
                        TgClient.bot.copy_message, **kwargs
                    )
                    if copied and getattr(copied, "link", None) and not entry.get("link"):
                        entry["link"] = copied.link
                except Exception as err:
                    if thread_id and any(
                        token in str(err).upper()
                        for token in ("THREAD", "TOPIC", "MESSAGE_ID_INVALID")
                    ):
                        try:
                            copied = await _call_with_flood_retry(
                                TgClient.bot.copy_message,
                                chat_id=dest,
                                from_chat_id=copy_from_chat,
                                message_id=copy_from_msg,
                            )
                            if copied and getattr(copied, "link", None) and not entry.get("link"):
                                entry["link"] = copied.link
                            return
                        except Exception as retry_err:
                            err = retry_err
                    delivery_ok = False
                    if not self._listener.is_cancelled:
                        LOGGER.error(f"Failed to copy upload to dump {dest}: {err}")

            dump_coros = []
            for dest, thread_id in extras:
                if not dest or dest == self._listener.user_id:
                    continue
                target = (dest, thread_id)
                if target in done:
                    continue
                done.add(target)
                dump_coros.append(_copy_to_dump(dest, thread_id))
            if dump_coros:
                await gather(*dump_coros, return_exceptions=True)

            # Keep every Telegram upload in its configured Leech Dump Chat.
            # Dump messages are permanent archive copies: never delete the
            # original upload after DM delivery or additional-dump fan-out.
            # This intentionally applies to bot- and user-session uploads.

    async def _upload_file_task(self, file_, f_path, dirpath, user_session, seq_idx, reply_anchor=None):
        up_path = None
        try:
            up_path, cap_mono = await self._prepare_file(file_, dirpath)
            sent = await self._upload_file(
                cap_mono, up_path, file_, seq_idx, user_session=user_session, reply_anchor=reply_anchor
            )
            if sent and not self._is_corrupted:
                if self._listener.is_super_chat or self._listener.up_dest:
                    if not self._is_private:
                        entry = self._upload_seq[seq_idx]
                        if entry["msg_id"] == sent.id:
                            self._msgs_dict[sent.link] = file_
            return sent
        except StopTransmission:
            return None
        except Exception as err:
            LOGGER.error(f"{err}. Path: {f_path}", exc_info=True)
            self._error = str(err)
            self._corrupted += 1
            return None
        finally:
            path_to_clean = up_path or f_path
            if not self._listener.is_cancelled and await aiopath.exists(path_to_clean):
                await remove(path_to_clean)

    async def upload(self):
        await self._user_settings()
        res = await self._msg_to_reply()
        if not res:
            return
        # Pre-cache media info for the primary download path so that after
        # proceed_split removes the original and only .001/.002 parts remain,
        # captions still show languages/quality from the merged source.
        try:
            if await aiopath.isfile(self._path):
                info = await get_media_info(self._path, True)
                if info:
                    self._media_info_cache[ospath.abspath(self._path)] = info
                    self._media_info_cache[ospath.basename(self._path)] = info
                    family = re_sub(
                        r"(?:\.part\d+(?:\.\w+)?|\.\d{3}|\.r\d+)$",
                        "",
                        ospath.basename(self._path),
                    )
                    if family:
                        self._media_info_cache[family] = info
            elif await aiopath.isdir(self._path):
                async def _cache_one(fp, f):
                    info = await wait_for(get_media_info(fp, True), timeout=2.5)
                    if info and (info[2] or info[3] if len(info) > 3 else False):
                        self._media_info_cache[ospath.abspath(fp)] = info
                        self._media_info_cache[f] = info
                        family = re_sub(
                            r"(?:\.part\d+(?:\.\w+)?|\.\d{3}|\.r\d+)$",
                            "",
                            f,
                        )
                        if family:
                            self._media_info_cache[family] = info
                cache_coros = []
                for dirpath, _, files in await sync_to_async(walk, self._path):
                    for f in files:
                        if re_match(r".+\.(mkv|mp4|webm|avi|mov|m4v|ts)$", f, re_I):
                            cache_coros.append(_cache_one(ospath.join(dirpath, f), f))
                if cache_coros:
                    await gather(*cache_coros[:8], return_exceptions=True)
        except Exception as e:
            LOGGER.debug(f"Media-info pre-cache skipped: {e}")
        upload_tasks = []
        seq_idx = 0
        client_pool_size = (
            len(TgClient.helper_bots)
            + len(TgClient.helper_users)
            + (1 if TgClient.user else 0)
            + 1
        )
        max_parallel = max(12, min(48, client_pool_size * 6))
        up_sem = Semaphore(max_parallel)
        anchor_msg = self._sent_msg
        for dirpath, _, files in natsorted(await sync_to_async(walk, self._path)):
            if dirpath.strip().endswith("/yt-dlp-thumb"):
                continue
            if dirpath.strip().endswith("_mltbss"):
                await self._send_screenshots(dirpath, files)
                await rmtree(dirpath, ignore_errors=True)
                continue
            for file_ in natsorted(files):
                self._error = ""
                f_path = ospath.join(dirpath, file_)
                if not await aiopath.exists(f_path):
                    LOGGER.error(f"{f_path} not exists! Continue uploading!")
                    continue
                try:
                    f_size = await aiopath.getsize(f_path)
                    self._total_files += 1
                    if f_size == 0:
                        LOGGER.warning(f"{f_path} size is zero, skipping")
                        self._corrupted += 1
                        if not self._listener.is_cancelled:
                            await remove(f_path)
                        continue
                    if self._listener.is_cancelled:
                        return
                    if self._last_msg_in_group:
                        group_lists = [
                            x for v in self._media_dict.values() for x in v.keys()
                        ]
                        match = re_match(r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)", f_path)
                        if not match or match and match.group(0) not in group_lists:
                            for key, value in list(self._media_dict.items()):
                                for subkey, msgs in list(value.items()):
                                    if len(msgs) > 1:
                                        await self._send_media_group(subkey, key, msgs)
                    # Client selection per part size: >2 GB requires user/premium session;
                    # <=2 GB prefers bot client when possible.
                    if f_size > BOT_MAX_BYTES:
                        if TgClient.user is None or not getattr(TgClient, "IS_PREMIUM_USER", False):
                            raise RuntimeError(
                                "Part exceeds 2 GiB (BOT_MAX_BYTES); a Telegram Premium USER_SESSION_STRING is required."
                            )
                        part_user_session = True
                    else:
                        part_user_session = (
                            TgClient.user is not None
                            and (self._listener.transmission_mode in ("user", "both") or bool(self._listener.up_dest or Config.LEECH_LOG_CHAT))
                        )
                    self._user_session = part_user_session
                    self._last_msg_in_group = False
                    self._upload_seq.append(None)
                    async def _run_bounded(
                        _file=file_,
                        _fpath=f_path,
                        _dir=dirpath,
                        _usess=part_user_session,
                        _idx=seq_idx,
                    ):
                        async with up_sem:
                            if self._listener.is_cancelled:
                                return None
                            return await self._upload_file_task(
                                _file, _fpath, _dir, _usess, _idx, reply_anchor=anchor_msg
                            )

                    upload_tasks.append(_run_bounded())
                    seq_idx += 1
                    if self._listener.is_cancelled:
                        return
                except Exception as err:
                    LOGGER.error(f"{err}. Path: {f_path}", exc_info=True)
                    self._error = str(err)
                    self._corrupted += 1
                    if self._listener.is_cancelled:
                        return
        if upload_tasks:
            results = await gather(*upload_tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    LOGGER.error(f"Upload task error: {r}")
        for key, value in list(self._media_dict.items()):
            for subkey, msgs in list(value.items()):
                if len(msgs) > 1:
                    try:
                        await self._send_media_group(subkey, key, msgs)
                    except Exception as e:
                        LOGGER.info(
                            f"While sending media group at the end of task. Error: {e}"
                        )
        if self._upload_seq:
            src_chat = self._listener.message.chat
            await self._sequence_copies(src_chat)
            self._msgs_dict = {
                e["link"]: e["file_"]
                for e in self._upload_seq
                if e is not None and e.get("link")
            }
        if self._listener.is_cancelled:
            return
        if self._total_files == 0:
            await self._listener.on_upload_error(
                "No files to upload. In case you have filled EXCLUDED_EXTENSIONS, then check if all files have those extensions or not."
            )
            return
        if self._total_files <= self._corrupted:
            await self._listener.on_upload_error(
                f"Files Corrupted or unable to upload. {self._error or 'Check logs!'}"
            )
            return
        LOGGER.info(f"Leech Completed: {self._listener.name}")
        await self._listener.on_upload_complete(
            None, self._msgs_dict, self._total_files, self._corrupted
        )
        return

    async def _upload_file(
        self, cap_mono, o_path, file_, seq_idx, force_document=False, user_session=False, reply_anchor=None
    ):
        if self._sent_msg is None:
            LOGGER.error("Cannot upload: _sent_msg is None")
            await self._listener.on_upload_error(
                "Upload failed: Message not initialized"
            )
            return

        if not hasattr(self._sent_msg, "chat") or self._sent_msg.chat is None:
            LOGGER.error("Cannot upload: _sent_msg.chat is None")
            await self._listener.on_upload_error(
                "Upload failed: Invalid message object"
            )
            return

        if (
            self._thumb is not None
            and not await aiopath.exists(self._thumb)
            and self._thumb != "none"
        ):
            self._thumb = None
        self._is_corrupted = False
        try:
            hu = HypertgUpload(self)
            self._hu = hu
            target_msg = reply_anchor or self._sent_msg

            sent_msg = await hu.upload(
                file_path=o_path,
                cap_mono=cap_mono,
                reply_target=target_msg,
                reply_msg_id=target_msg.id,
                force_document=force_document,
                user_session=user_session,
                user_thumb=self._thumb,
            )

            if self._listener.is_cancelled:
                return

            self._sent_msg = sent_msg

            self._upload_seq[seq_idx] = {
                "chat_id": sent_msg.chat.id,
                "msg_id": sent_msg.id,
                "link": sent_msg.link,
                "file_": file_,
                "user_session": bool(user_session),
            }
            self._msg_to_seq[(sent_msg.chat.id, sent_msg.id)] = seq_idx

            if (
                not self._listener.is_cancelled
                and self._media_group
                and (sent_msg.video or sent_msg.document)
            ):
                key = "documents" if sent_msg.document else "videos"
                if match := re_match(r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)", o_path):
                    pname = match.group(0)
                    flush_msgs = None
                    async with self._media_lock:
                        if pname in self._media_dict[key]:
                            self._media_dict[key][pname].append(
                                [sent_msg.chat.id, sent_msg.id]
                            )
                        else:
                            self._media_dict[key][pname] = [[sent_msg.chat.id, sent_msg.id]]
                        msgs = self._media_dict[key][pname]
                        if len(msgs) == 10:
                            flush_msgs = list(msgs)
                        else:
                            self._last_msg_in_group = True
                    if flush_msgs is not None:
                        await self._send_media_group(pname, key, flush_msgs)

            return sent_msg
        except StopTransmission:
            raise
        except Exception as err:
            err_type = "RPCError: " if isinstance(err, RPCError) else ""
            LOGGER.error(f"{err_type}{err}. Path: {o_path}", exc_info=True)
            raise err

    @property
    def speed(self):
        """Prefer recent-window throughput so status reflects live MTProto speed."""
        try:
            instant = float(getattr(self, "_instant_speed", 0) or 0)
            if instant > 0:
                return instant
            elapsed = time() - self._start_time
            if elapsed <= 0.05:
                elapsed = 0.05
            return self._processed_bytes / elapsed
        except ZeroDivisionError:
            return 0

    @property
    def processed_bytes(self):
        return self._processed_bytes

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self._listener.name}")
        await self._listener.on_upload_error("your upload has been stopped!")
