from asyncio import sleep, wait_for

from PIL import Image
from pyrogram import StopTransmission
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, PhotoInvalidDimensions
from pyrogram.types import ReplyParameters

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from inspect import signature
from os import path as ospath
from time import monotonic
from aiofiles.os import path as aiopath, remove

try:
    from pyrogram.raw.types import DocumentAttributeFilename
except ImportError:
    DocumentAttributeFilename = None

from ... import LOGGER
from ...core.config_manager import Config
from ...core.tg_client import BOT_MAX_BYTES, TgClient
from ..telegram_helper.tg_transfer import HypertgTransfer
from ..ext_utils.media_utils import (
    get_audio_thumbnail,
    get_document_type,
    get_media_info,
    get_multiple_frames_thumbnail,
    get_video_thumbnail,
)
from ..ext_utils.bot_utils import parse_dest
from ..ext_utils.tmdb_utils import get_auto_thumbnail


class HypertgUpload(HypertgTransfer):
    def __init__(self, obj):
        super().__init__(obj)
        self._up_file = ""
        self._file_progress = {}
        self._done_flag = {"ok": False}
        self._meter = {"t": monotonic(), "b": 0, "speed": 0.0}
        self._upload_begin_ts = 0.0
        self._last_edit = 0.0

    @staticmethod
    def _cap_send_name(name: str, max_len: int = 64) -> str:
        if len(name) <= max_len:
            return name
        stem, ext = ospath.splitext(name)
        if len(ext) >= max_len:
            return name[:max_len]
        return f"{stem[: max_len - len(ext)]}{ext}"

    async def _get_upload_client_for_part(self, file_path: str):
        """Choose the upload identity per part. >2 GB requires the configured user/premium session."""
        part_size = ospath.getsize(file_path)
        if part_size > BOT_MAX_BYTES:
            if TgClient.user is None or not getattr(TgClient, "IS_PREMIUM_USER", False):
                raise RuntimeError(
                    "Part exceeds 2 GiB (BOT_MAX_BYTES); a Telegram Premium USER_SESSION_STRING is required."
                )
            return TgClient.user, True
        return TgClient.bot, False

    async def _progress(self, current, total_bytes, file_path):
        if self._done_flag.get("ok"):
            return
        if self._listener.is_cancelled and StopTransmission is not None:
            raise StopTransmission()  # only reliable way to abort cbgram transfer

        from time import time as _wall_now

        # Start the speed clock on the first real byte so status does not
        # average in session warm-up / caption / thumb prep as "slow upload".
        if not getattr(self._obj, "_speed_clock_started", False) and current > 0:
            self._obj._start_time = _wall_now()
            self._obj._speed_clock_started = True
        if not self._file_progress and current > 0:
            self._meter = {"t": monotonic(), "b": 0, "speed": 0.0}
        self._upload_begin_ts = 0.0

        # Immediate speed tracking from the first tick — no slow-start ramp
        now = monotonic()
        delta_t = now - self._meter["t"]
        delta_b = current - self._meter["b"]
        if delta_b > 0 and (delta_t >= 0.05 or not self._meter["speed"]):
            dt = max(delta_t, 0.01)
            instant = delta_b / dt
            self._meter["speed"] = (
                instant
                if not self._meter["speed"]
                else (self._meter["speed"] * 0.35 + instant * 0.65)
            )
            self._meter["t"], self._meter["b"] = now, current
            speed_map = getattr(self._obj, "_speed_map", None)
            if isinstance(speed_map, dict):
                speed_map[file_path] = self._meter["speed"]
                self._obj._instant_speed = sum(speed_map.values())
            else:
                self._obj._instant_speed = self._meter["speed"]

        # Update processed bytes immediately on first tick or every >= 0.1s
        if self._last_edit != 0.0 and (now - self._last_edit < 0.1) and total_bytes and current < total_bytes:
            return
        self._last_edit = now

        self._file_progress[file_path] = current
        prog_map = getattr(self._obj, "_file_progress_map", None)
        if isinstance(prog_map, dict):
            prog_map[file_path] = current
            self._obj._processed_bytes = sum(prog_map.values())
        else:
            self._obj._processed_bytes = sum(self._file_progress.values())

    async def upload(
        self,
        file_path,
        cap_mono,
        reply_target,
        reply_msg_id,
        force_document=False,
        user_thumb=None,
        user_session=False,
    ):
        self._cancel.clear()
        self._up_file = ospath.basename(file_path)

        is_video, is_audio, is_image = await get_document_type(file_path)

        thumb = user_thumb if user_thumb and user_thumb != "none" else None
        hd_cover = getattr(self._obj, "_hd_thumb", None)
        if hd_cover and not await aiopath.isfile(hd_cover):
            hd_cover = None

        if not is_image and thumb is None:
            file_name = ospath.splitext(self._up_file)[0]
            base_path = getattr(self._obj, "_path", "")
            thumb_path = f"{base_path}/yt-dlp-thumb/{file_name}.jpg"
            if await aiopath.isfile(thumb_path):
                thumb = thumb_path
            elif await aiopath.isfile(thumb_path.replace("/yt-dlp-thumb", "")):
                thumb = thumb_path.replace("/yt-dlp-thumb", "")
            elif is_audio and not is_video:
                thumb = await get_audio_thumbnail(file_path)

        if not is_image and thumb is None and user_thumb not in ("none",):
            # user_thumb is None when mode=auto or no custom file — run auto path.
            user_dict = self._listener.user_dict or {}
            mode = str(user_dict.get("THUMBNAIL_MODE") or "").strip().lower()
            auto_on = user_dict.get("AUTO_THUMBNAIL")
            if auto_on is None:
                auto_on = getattr(Config, "AUTO_THUMBNAIL", True)
            if mode in ("auto", "random", "") and auto_on:
                try:
                    from ..ext_utils.media_utils import format_tg_thumbnail
                    from ..ext_utils.bot_utils import sync_to_async

                    thumb = await wait_for(
                        get_auto_thumbnail(
                            self._up_file or self._listener.name or file_path,
                            force_document or self._listener.as_doc,
                        ),
                        timeout=1.5,
                    )
                    if thumb and await aiopath.isfile(thumb):
                        formatted = await sync_to_async(
                            format_tg_thumbnail, thumb, None, True
                        )
                        if formatted and await aiopath.isfile(formatted):
                            thumb = formatted
                except Exception as e:
                    LOGGER.warning(f"Auto thumbnail failed: {e}")
                    thumb = None

        duration = 0
        width = 480
        height = 320
        artist = ""
        title = ""

        if (
            force_document
            or self._listener.as_doc
            or (not is_video and not is_audio and not is_image)
        ):
            key = "documents"
            if is_video and thumb is None:
                thumb = await get_video_thumbnail(file_path, None)
        elif is_video:
            key = "videos"
            duration = (await get_media_info(file_path))[0]
            if thumb is None and self._listener.thumbnail_layout:
                thumb = await get_multiple_frames_thumbnail(
                    file_path,
                    self._listener.thumbnail_layout,
                    self._listener.screen_shots,
                )
            if thumb is None:
                thumb = await get_video_thumbnail(file_path, duration)
            if thumb is not None and thumb != "none":
                with Image.open(thumb) as img:
                    width, height = img.size
        elif is_audio:
            key = "audios"
            duration, artist, title = await get_media_info(file_path)
        else:
            key = "photos"

        if thumb == "none":
            thumb = None

        up_size = ospath.getsize(file_path)
        hyper_user_only = False

        if self._listener.up_dest:
            # Explicit command destination always has highest priority.
            upload_chat_id = self._listener.up_dest
            thread_id = self._listener.chat_thread_id
        elif Config.LEECH_LOG_CHAT:
            # The primary Leech Dump Chat is the first staging destination.
            upload_chat_id, thread_id = parse_dest(Config.LEECH_LOG_CHAT)
        elif getattr(self._listener, "leech_dests", None):
            # If the primary field is empty, use the first configured dump
            # from Bot Settings / User Settings as the initial upload target.
            # Remaining destinations are copied after the Bot PM delivery.
            first_dump = self._listener.leech_dests[0]
            if isinstance(first_dump, (tuple, list)):
                upload_chat_id, thread_id = first_dump[0], (first_dump[1] if len(first_dump) > 1 else None)
            else:
                upload_chat_id, thread_id = parse_dest(first_dump)
        else:
            # No dump configured: deliver directly to the user's Bot PM.
            upload_chat_id, thread_id = self._listener.user_id, None

        is_private_dm_target = (
            upload_chat_id == self._listener.user_id
            or (isinstance(upload_chat_id, int) and upload_chat_id > 0)
        )

        _, part_needs_user = await self._get_upload_client_for_part(file_path)
        # Primary upload to leech_chat / leech_dump_chats must use USER_SESSION_STRING
        # whenever configured; Private PM delivery always uses bot session only.
        if is_private_dm_target:
            user_session = False
            use_hyper = False
            hyper_user_only = False
        elif TgClient.user is not None and (
            part_needs_user
            or self._listener.transmission_mode in ("user", "both")
            or bool(Config.LEECH_LOG_CHAT)
            or bool(getattr(self._listener, "leech_dests", None))
        ):
            user_session = True
            has_user_helpers = bool(TgClient.helper_users)
            use_hyper = bool(getattr(Config, "USE_HYPER", True) and has_user_helpers)
            hyper_user_only = True
        else:
            user_session = False
            use_hyper = bool(
                getattr(Config, "USE_HYPER", True)
                and self.num_clients > 0
                and not part_needs_user
            )

        self._done_flag = {"ok": False}
        self._meter = {"t": monotonic(), "b": 0, "speed": 0.0}
        self._upload_begin_ts = 0.0
        self._last_edit = 0.0
        try:
            if use_hyper:
                hyper_rply = (
                    reply_msg_id if upload_chat_id == reply_target.chat.id else None
                )
                try:
                    sent = await self._hyper_send(
                        file_path,
                        key,
                        thumb,
                        cap_mono,
                        upload_chat_id,
                        hyper_rply,
                        thread_id,
                        duration=duration,
                        width=width,
                        height=height,
                        artist=artist,
                        title=title,
                        hd_cover=hd_cover,
                        user_only=hyper_user_only,
                    )
                except Exception as hyper_err:
                    LOGGER.warning(
                        f"HypertgUL helper upload to {upload_chat_id} failed ({hyper_err}); falling back to direct send"
                    )
                    sent = await self._direct_send(
                        file_path,
                        key,
                        thumb,
                        cap_mono,
                        upload_chat_id,
                        hyper_rply,
                        thread_id,
                        duration=duration,
                        width=width,
                        height=height,
                        artist=artist,
                        title=title,
                        hd_cover=hd_cover,
                        user_session=user_session or (up_size > BOT_MAX_BYTES),
                    )
            else:
                direct_rply = (
                    reply_msg_id if upload_chat_id == reply_target.chat.id else None
                )
                sent = await self._direct_send(
                    file_path,
                    key,
                    thumb,
                    cap_mono,
                    upload_chat_id,
                    direct_rply,
                    thread_id,
                    duration=duration,
                    width=width,
                    height=height,
                    artist=artist,
                    title=title,
                    hd_cover=hd_cover,
                    user_session=user_session,
                )

            LOGGER.info(f"HypertgUL uploaded {self._up_file}")
            return sent

        except StopTransmission:
            LOGGER.warning(f"HypertgUL cancelled {self._up_file}")
            raise
        except Exception as e:
            LOGGER.error(f"HypertgUL fail {self._up_file}: {type(e).__name__}: {e}")
            raise
        finally:
            self._done_flag["ok"] = True
            if user_thumb is None and thumb is not None and await aiopath.exists(thumb):
                try:
                    await remove(thumb)
                except Exception:
                    pass

    async def _send_with_retry(self, send_func, **kwargs):
        while True:
            try:
                return await send_func(**kwargs)
            except (FloodWait, FloodPremiumWait) as f:
                LOGGER.warning(f"HypertgUL flood {f.value}s on {self._up_file}")
                await sleep(f.value + 1)

    async def _try_send(self, key, client, kwargs):
        try:
            if key == "videos":
                return await self._send_with_retry(client.send_video, **kwargs)
            elif key == "audios":
                return await self._send_with_retry(client.send_audio, **kwargs)
            elif key == "photos":
                return await self._send_with_retry(client.send_photo, **kwargs)
            else:
                return await self._send_with_retry(client.send_document, **kwargs)
        except PhotoInvalidDimensions:
            kwargs.pop("thumb", None)
            kwargs.pop("video_cover", None)
            if key == "videos":
                return await self._send_with_retry(client.send_video, **kwargs)
            elif key == "audios":
                return await self._send_with_retry(client.send_audio, **kwargs)
            elif key == "photos":
                return await self._send_with_retry(client.send_photo, **kwargs)
            else:
                return await self._send_with_retry(client.send_document, **kwargs)

    async def _upload_via_bot_bridge(
        self,
        key,
        user_client,
        kwargs,
        target_chat_id,
        reply_msg_id=None,
        thread_id=None,
    ):
        """Stage >2 GiB upload from user_client to TgClient.bot, then copy to target_chat_id via TgClient.bot."""
        bot_uname = TgClient.BNAME or getattr(TgClient.bot.me, "username", "")
        bot_peer = f"@{str(bot_uname).lstrip('@')}"
        user_me = getattr(user_client, "me", None) or await user_client.get_me()
        user_uid = user_me.id

        anchor = None
        try:
            anchor = await TgClient.bot.send_message(
                chat_id=user_uid, text="⏳", disable_notification=True
            )
        except Exception:
            try:
                s_msg = await user_client.send_message(bot_peer, "/start")
                await sleep(0.5)
                await s_msg.delete()
                anchor = await TgClient.bot.send_message(
                    chat_id=user_uid, text="⏳", disable_notification=True
                )
            except Exception:
                anchor = None

        tag = f"__wzbridge_{id(self)}_{int(ospath.getsize(kwargs.get('video') or kwargs.get('document') or kwargs.get('audio') or kwargs.get('photo')))}__"
        stage_kwargs = dict(kwargs)
        stage_kwargs["chat_id"] = bot_peer
        stage_kwargs["caption"] = tag
        stage_kwargs.pop("reply_parameters", None)
        stage_kwargs.pop("message_thread_id", None)

        staged_user_msg = None
        bot_view_msg = None
        try:
            staged_user_msg = await self._try_send(key, user_client, stage_kwargs)
            if anchor is not None:
                probe_ids = list(range(anchor.id + 1, anchor.id + 30))
                for _ in range(4):
                    try:
                        cands = await TgClient.bot.get_messages(
                            chat_id=user_uid, message_ids=probe_ids
                        )
                        if not isinstance(cands, list):
                            cands = [cands]
                        for c in cands:
                            if c and not getattr(c, "empty", False) and tag in str(getattr(c, "caption", "") or ""):
                                bot_view_msg = c
                                break
                    except Exception:
                        pass
                    if bot_view_msg is not None:
                        break
                    await sleep(0.5)

            if bot_view_msg is None:
                raise RuntimeError("Bot bridge could not locate staged upload message")

            copy_kwargs = {
                "chat_id": target_chat_id,
                "from_chat_id": user_uid,
                "message_id": bot_view_msg.id,
                "caption": kwargs.get("caption") or "",
                "disable_notification": True,
            }
            if reply_msg_id:
                copy_kwargs["reply_parameters"] = ReplyParameters(message_id=reply_msg_id)
            elif thread_id:
                copy_kwargs["message_thread_id"] = thread_id

            try:
                return await self._send_with_retry(TgClient.bot.copy_message, **copy_kwargs)
            except Exception:
                copy_kwargs.pop("reply_parameters", None)
                copy_kwargs.pop("message_thread_id", None)
                return await self._send_with_retry(TgClient.bot.copy_message, **copy_kwargs)
        finally:
            for m in (anchor, bot_view_msg, staged_user_msg):
                if m is not None:
                    try:
                        await m.delete()
                    except Exception:
                        pass

    async def _hyper_send(
        self,
        file_path,
        key,
        thumb,
        cap_mono,
        chat_id,
        reply_msg_id,
        thread_id=None,
        duration=0,
        width=0,
        height=0,
        artist="",
        title="",
        hd_cover=None,
        user_only=False,
    ):
        if user_only:
            candidates = {
                k: self.work_loads.get(k, 0) for k in self.clients if k < 0
            }
            if not candidates:
                idx = self._pick_client()
            else:
                idx = min(candidates, key=candidates.get)
        else:
            idx = self._pick_client()
        client = self.clients[idx]
        self.work_loads[idx] += 1

        try:
            kwargs = {
                "chat_id": chat_id,
                "disable_notification": True,
                "progress": self._progress,
                "progress_args": (file_path,),
                "parse_mode": ParseMode.HTML,
            }
            if cap_mono:
                kwargs["caption"] = cap_mono
            if reply_msg_id:
                kwargs["reply_parameters"] = ReplyParameters(message_id=reply_msg_id)
            elif thread_id:
                kwargs["message_thread_id"] = thread_id

            if key == "videos":
                if duration:
                    kwargs["duration"] = duration
                if width:
                    kwargs["width"] = width
                if height:
                    kwargs["height"] = height
                if thumb:
                    kwargs["thumb"] = thumb
                if hd_cover:
                    # Telegram's custom video cover is separate from the small
                    # upload thumbnail; keep the higher-resolution saved artwork.
                    kwargs["video_cover"] = hd_cover
                elif thumb:
                    kwargs["video_cover"] = thumb
            elif key == "audios":
                if duration:
                    kwargs["duration"] = duration
                if artist:
                    kwargs["performer"] = artist
                if title:
                    kwargs["title"] = title
                if thumb:
                    kwargs["thumb"] = thumb
            elif key == "documents":
                # Telegram supports a custom thumbnail on sendDocument too.
                # Previously the document branch omitted this argument, so
                # custom thumbnails appeared on videos/audio but not documents.
                if thumb and await aiopath.isfile(thumb):
                    kwargs["thumb"] = thumb

            if key == "videos":
                kwargs["video"] = file_path
            elif key == "audios":
                kwargs["audio"] = file_path
            elif key == "photos":
                kwargs["photo"] = file_path
            else:
                kwargs["document"] = file_path

            sent = await self._try_send(key, client, kwargs)
            return sent
        finally:
            self.work_loads[idx] -= 1

    async def _direct_send(
        self,
        file_path,
        key,
        thumb,
        cap_mono,
        chat_id,
        reply_msg_id,
        thread_id=None,
        duration=0,
        width=0,
        height=0,
        artist="",
        title="",
        hd_cover=None,
        user_session=False,
    ):
        up_size = ospath.getsize(file_path)
        is_private_dm_target = (
            chat_id == self._listener.user_id
            or (isinstance(chat_id, int) and chat_id > 0)
        )
        client = (
            TgClient.user
            if (user_session and TgClient.user and not is_private_dm_target)
            else TgClient.bot
        )
        kwargs = {
            "chat_id": chat_id,
            "disable_notification": True,
            "progress": self._progress,
            "progress_args": (file_path,),
            "parse_mode": ParseMode.HTML,
        }
        if cap_mono:
            kwargs["caption"] = cap_mono
        if reply_msg_id:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_msg_id)
        elif thread_id:
            kwargs["message_thread_id"] = thread_id

        if key == "videos":
            if thumb:
                kwargs["thumb"] = thumb
            if hd_cover:
                kwargs["video_cover"] = hd_cover
            elif thumb:
                kwargs["video_cover"] = thumb
            if duration:
                kwargs["duration"] = duration
            if width:
                kwargs["width"] = width
            if height:
                kwargs["height"] = height
        elif key == "audios":
            if thumb:
                kwargs["thumb"] = thumb
            if duration:
                kwargs["duration"] = duration
            if artist:
                kwargs["performer"] = artist
            if title:
                kwargs["title"] = title
        elif key == "documents":
            # sendDocument accepts `thumb`; sendPhoto does not.
            if thumb and await aiopath.isfile(thumb):
                kwargs["thumb"] = thumb

        if key == "videos":
            kwargs["video"] = file_path
        elif key == "audios":
            kwargs["audio"] = file_path
        elif key == "photos":
            kwargs["photo"] = file_path
        else:
            send_name = self._cap_send_name(ospath.basename(file_path), 64)
            kwargs["document"] = file_path
            kwargs["file_name"] = send_name
            if (
                DocumentAttributeFilename is not None
                and "attributes" in signature(client.send_document).parameters
            ):
                kwargs["attributes"] = [DocumentAttributeFilename(file_name=send_name)]

        # If target is User PM and file > 2 GiB, stage via TgClient.user -> TgClient.bot bridge
        if is_private_dm_target and up_size > BOT_MAX_BYTES and TgClient.user is not None:
            return await self._upload_via_bot_bridge(
                key,
                TgClient.user,
                kwargs,
                chat_id,
                reply_msg_id=reply_msg_id,
                thread_id=thread_id,
            )

        try:
            return await self._try_send(key, client, kwargs)
        except Exception as err:
            # If user_session failed on a dump chat, fallback to TgClient.bot (or bot bridge for >2 GiB)
            if client is TgClient.user:
                LOGGER.warning(
                    f"User session direct upload to {chat_id} failed ({err}); falling back to bot delivery"
                )
                if up_size <= BOT_MAX_BYTES:
                    try:
                        return await self._try_send(key, TgClient.bot, kwargs)
                    except Exception:
                        kwargs["chat_id"] = self._listener.user_id
                        kwargs.pop("reply_parameters", None)
                        kwargs.pop("message_thread_id", None)
                        return await self._try_send(key, TgClient.bot, kwargs)
                return await self._upload_via_bot_bridge(
                    key,
                    TgClient.user,
                    kwargs,
                    chat_id,
                    reply_msg_id=reply_msg_id,
                    thread_id=thread_id,
                )
            elif chat_id != self._listener.user_id:
                LOGGER.warning(
                    f"Bot upload to dump {chat_id} failed ({err}); delivering directly to User PM {self._listener.user_id}"
                )
                kwargs["chat_id"] = self._listener.user_id
                kwargs.pop("reply_parameters", None)
                kwargs.pop("message_thread_id", None)
                return await self._try_send(key, TgClient.bot, kwargs)
            raise

    async def cancel(self):
        await super().cancel()
