import os
import re
from collections import deque
from asyncio import Lock, sleep
from os import path as ospath
from time import time
from secrets import token_hex
from aiofiles.os import makedirs
from pyrogram.errors import FloodWait, PeerIdInvalid, ChannelInvalid

from bot.helper.ext_utils.hyperdl_utils import HypertgDownload

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from .... import (
    LOGGER,
    task_dict,
    task_dict_lock,
)
from ....core.tg_client import TgClient
from ....core.config_manager import Config
from ...ext_utils.task_manager import check_running_tasks, stop_duplicate_check
from ...mirror_leech_utils.status_utils.queue_status import QueueStatus
from ...mirror_leech_utils.status_utils.telegram_status import TelegramStatus
from ...telegram_helper.message_utils import send_status_message

global_lock = Lock()
GLOBAL_GID = dict()


def clean_caption_filename(caption: str, fallback_filename: str) -> str:
    if not caption or not caption.strip():
        return fallback_filename

    from ...ext_utils.files_utils import is_archive, is_archive_split, ARCH_EXT
    if fallback_filename and is_archive_split(fallback_filename):
        # Preserve exact part names (.part1.rar, .part2.rar, .zip.001, .001, etc.) so multi-part extraction works
        return fallback_filename

    ext = ""
    if fallback_filename:
        low_fb = fallback_filename.lower()
        for a_ext in sorted(ARCH_EXT, key=len, reverse=True):
            if low_fb.endswith(a_ext):
                ext = fallback_filename[-len(a_ext):]
                break
        if not ext and "." in fallback_filename:
            ext = ospath.splitext(fallback_filename)[1]

    lines = [line.strip() for line in caption.split("\n") if line.strip()]
    cleaned_title = ""

    for line in lines:
        line_clean = re.sub(r"[^\w\s\.\-\_\(\)\[\]]", "", line).strip()
        if not line_clean:
            continue

        if re.search(r"\b(size|duration|languages|language|audio|subtitles|subtitle|quality|codec|fps)\b\s*:", line_clean, re.IGNORECASE):
            continue

        cleaned_title = line_clean
        break

    if not cleaned_title:
        return fallback_filename

    if ext and not cleaned_title.lower().endswith(ext.lower()):
        # If the real file is an archive (e.g. .zip, .rar, .7z) and caption ends with a media extension (.mkv, .mp4),
        # strip the media extension from the caption before appending the real archive extension.
        if fallback_filename and is_archive(fallback_filename):
            cleaned_title = re.sub(r"\.(mkv|mp4|avi|mov|flv|wmv|webm|m4v|ts|mp3|flac|aac|m4a|wav)$", "", cleaned_title, flags=re.IGNORECASE)
        cleaned_title += ext

    return cleaned_title


class TelegramDownloadHelper:
    def __init__(self, listener):
        self._processed_bytes = 0
        self._start_time = 0.0
        self._transfer_start = 0.0
        self._speed_samples = deque(maxlen=24)
        self._listener = listener
        self._id = ""
        self.session = ""
        self._user_client = TgClient.user
        tm = self._listener.transmission_mode
        self._dump_chat = (
            self._listener.up_dest if self._listener.is_leech else None
        ) or Config.LEECH_LOG_CHAT
        self._access_client = None
        # Use HypertgDownload (parallel cbgram MtprotoPool DC lanes + cbcrypto AES-CTR)
        # as primary for max multi-connection speed, with native cbgram download_media fallback.
        self._cb_native = False
        self._hyper_dl = (
            Config.USE_HYPER
            and (
                TgClient.user is not None
                or TgClient.bot is not None
                or len(TgClient.helper_bots) != 0
                or len(TgClient.helper_users) != 0
            )
        )
        self._hyper_dl_instance = None
        self._active_helper_client = None

    def _record_bytes(self, delta: int):
        now = time()
        if self._transfer_start <= 0 and delta > 0:
            self._transfer_start = now
            self._speed_samples.clear()
            self._speed_samples.append((now, 0))
        self._processed_bytes += delta
        if delta > 0:
            self._speed_samples.append((now, self._processed_bytes))

    @property
    def speed(self):
        if self._processed_bytes <= 0 or self._transfer_start <= 0:
            return 0
        now = time()
        # Keep a ~3s sliding window so the very first tick shows real burst speed
        # without being dragged down by pre-transfer auth/session setup time.
        while len(self._speed_samples) > 2 and (now - self._speed_samples[0][0]) > 3.0:
            self._speed_samples.popleft()
        if len(self._speed_samples) >= 2:
            t0, b0 = self._speed_samples[0]
            dt = now - t0
            if dt >= 0.15 and self._processed_bytes > b0:
                return (self._processed_bytes - b0) / dt
        return self._processed_bytes / max(0.05, now - self._transfer_start)

    @property
    def processed_bytes(self):
        return self._processed_bytes

    async def _on_download_start(self, file_id, gid, from_queue):
        async with global_lock:
            GLOBAL_GID[file_id] = gid
        self._id = file_id
        async with task_dict_lock:
            task_dict[self._listener.mid] = TelegramStatus(
                self._listener,
                self,
                gid,
                "dl",
                "cb" if self._cb_native else ("hdl" if self._hyper_dl else ""),
            )
        if not from_queue:
            await self._listener.on_download_start()
            if self._listener.multi <= 1:
                await send_status_message(self._listener.message)
            LOGGER.info(f"Download from Telegram: {self._listener.name}")
        else:
            LOGGER.info(f"Start Queued Download from Telegram: {self._listener.name}")

    async def _on_download_progress(self, current, _):
        if self._listener.is_cancelled:
            # stop_transmission raises Pyrogram's StopTransmission by design.
            # Let it propagate so the active transfer stops; restart shutdown
            # separately suppresses retries and directory cleanup.
            if self._active_helper_client is not None:
                self._active_helper_client.stop_transmission()
            elif self.session == "user" and self._user_client is not None:
                self._user_client.stop_transmission()
            elif self.session == "hbots":
                for hbot in TgClient.helper_bots.values():
                    hbot.stop_transmission()
            elif TgClient.bot is not None:
                TgClient.bot.stop_transmission()
            return
        now = time()
        if self._transfer_start <= 0 and current > 0:
            self._transfer_start = now
            self._speed_samples.clear()
            self._speed_samples.append((now, 0))
        self._processed_bytes = current
        if current > 0:
            self._speed_samples.append((now, current))

    async def _on_download_error(self, error):
        async with global_lock:
            if self._id in GLOBAL_GID:
                GLOBAL_GID.pop(self._id)
        await self._listener.on_download_error(error)

    async def _on_download_complete(self):
        await self._listener.on_download_complete()
        async with global_lock:
            GLOBAL_GID.pop(self._id)
        return

    async def _download_file(self, message, path):
        try:
            if dir_path := ospath.dirname(path):
                await makedirs(dir_path, exist_ok=True)
            download = None
            if self._hyper_dl:
                try:
                    self._hyper_dl_instance = HypertgDownload(self)
                    download = await self._hyper_dl_instance.download_media(
                        message,
                        file_name=path,
                        dump_chat=self._dump_chat,
                    )
                    if (
                        self._hyper_dl_instance is not None
                        and self._hyper_dl_instance.dump_chat
                        and self._hyper_dl_instance.message
                        and hasattr(self._hyper_dl_instance.message, "id")
                    ):
                        self._listener.dump_chat = self._hyper_dl_instance.dump_chat
                        self._listener.dump_msg_id = self._hyper_dl_instance.message.id
                    self._hyper_dl_instance = None
                except Exception as e:
                    self._hyper_dl_instance = None
                    # Cancellation must not fall through into a second download.
                    # The original transfer may still be unwinding its temp file.
                    if self._listener.is_cancelled:
                        LOGGER.info(
                            f"Telegram download cancelled for {self._listener.name}"
                        )
                        return False
                    LOGGER.warning(f"Hyper download failed, using normal: {e}")
                    download = None

            if download is None and not self._listener.is_cancelled:
                # Native cbgram + cbcrypto Rust file-path download (https://cbgram.pages.dev/):
                # Always pass a real file_name path (never in_memory=True) so cbcrypto
                # handles MTProto sockets, pacing, AES-CTR decryption, and disk writes in Rust.
                use_user = (
                    self._user_client is not None
                    and not getattr(self._listener, "bot_trans", False)
                )
                download = None
                if use_user:
                    self.session = "user"
                    try:
                        user_msg = None
                        try:
                            user_msg = await self._user_client.get_messages(
                                chat_id=message.chat.id, message_ids=message.id
                            )
                        except Exception:
                            user_msg = None

                        if user_msg is None or getattr(user_msg, "empty", False):
                            try:
                                await TgClient.ensure_peer(
                                    self._user_client, message.chat.id, fallback_client=TgClient.bot
                                )
                                user_msg = await self._user_client.get_messages(
                                    chat_id=message.chat.id, message_ids=message.id
                                )
                            except Exception:
                                user_msg = None

                        if (user_msg is None or getattr(user_msg, "empty", False)) and self._dump_chat:
                            from ...ext_utils.bot_utils import parse_dest
                            dc_id, _ = parse_dest(self._dump_chat)
                            if isinstance(dc_id, int) and dc_id != message.chat.id:
                                copied = None
                                for copier in (TgClient.bot, self._user_client, self._access_client):
                                    if copier is None:
                                        continue
                                    try:
                                        await TgClient.ensure_peer(copier, dc_id, fallback_client=TgClient.bot)
                                        await TgClient.ensure_peer(copier, message.chat.id, fallback_client=TgClient.bot)
                                        copied = await copier.copy_message(
                                            chat_id=dc_id,
                                            from_chat_id=message.chat.id,
                                            message_id=message.id,
                                            disable_notification=True,
                                        )
                                        if copied:
                                            break
                                    except Exception:
                                        copied = None
                                if copied:
                                    self._listener.dump_chat = dc_id
                                    self._listener.dump_msg_id = copied.id
                                    await TgClient.ensure_peer(
                                        self._user_client, dc_id, fallback_client=TgClient.bot
                                    )
                                    user_msg = await self._user_client.get_messages(
                                        chat_id=dc_id, message_ids=copied.id
                                    )

                        if user_msg is not None and not getattr(user_msg, "empty", False):
                            download = await self._user_client.download_media(
                                user_msg,
                                file_name=path,
                                progress=self._on_download_progress,
                            )
                    except Exception as u_err:
                        if self._listener.is_cancelled:
                            return False
                        LOGGER.warning(f"User session cbgram download_media failed ({u_err}); falling back to helper/bot")
                        download = None

                if (
                    download is None
                    and not self._listener.is_cancelled
                    and (TgClient.helper_users or TgClient.helper_bots)
                ):
                    media_obj = getattr(message, message.media.value, None) if getattr(message, "media", None) else None
                    f_size = int(getattr(media_obj, "file_size", 0) or 0)
                    h_client, h_kind = TgClient.pick_least_loaded(
                        prefer_premium=f_size > TgClient.BOT_MAX_BYTES
                    )
                    if h_client is not None and h_client is not TgClient.bot and h_client is not self._user_client:
                        self._active_helper_client = h_client
                        self.session = h_kind
                        try:
                            h_msg = None
                            try:
                                h_msg = await h_client.get_messages(
                                    chat_id=message.chat.id, message_ids=message.id
                                )
                            except Exception:
                                h_msg = None
                            if (h_msg is None or getattr(h_msg, "empty", False)) and self._dump_chat:
                                from ...ext_utils.bot_utils import parse_dest
                                dc_id, _ = parse_dest(self._dump_chat)
                                if isinstance(dc_id, int) and dc_id != message.chat.id:
                                    copied = await TgClient.bot.copy_message(
                                        chat_id=dc_id,
                                        from_chat_id=message.chat.id,
                                        message_id=message.id,
                                        disable_notification=True,
                                    )
                                    if copied:
                                        self._listener.dump_chat = dc_id
                                        self._listener.dump_msg_id = copied.id
                                        h_msg = await h_client.get_messages(
                                            chat_id=dc_id, message_ids=copied.id
                                        )
                            if h_msg is not None and not getattr(h_msg, "empty", False):
                                download = await h_client.download_media(
                                    h_msg,
                                    file_name=path,
                                    progress=self._on_download_progress,
                                )
                        except Exception as h_err:
                            if self._listener.is_cancelled:
                                return False
                            LOGGER.warning(f"Helper cbgram download_media failed ({h_err}); falling back to bot")
                            download = None
                        finally:
                            self._active_helper_client = None
                            TgClient.release_load(h_client)

                if download is None and not self._listener.is_cancelled and TgClient.bot is not None:
                    self.session = "bot"
                    bot_msg = None
                    try:
                        bot_msg = await TgClient.bot.get_messages(
                            chat_id=message.chat.id, message_ids=message.id
                        )
                    except Exception:
                        bot_msg = None
                    if (bot_msg is None or getattr(bot_msg, "empty", False)) and getattr(self._listener, "dump_chat", None) and getattr(self._listener, "dump_msg_id", None):
                        try:
                            bot_msg = await TgClient.bot.get_messages(
                                chat_id=self._listener.dump_chat, message_ids=self._listener.dump_msg_id
                            )
                        except Exception:
                            bot_msg = None
                    if bot_msg is not None and not getattr(bot_msg, "empty", False):
                        download = await TgClient.bot.download_media(
                            bot_msg,
                            file_name=path,
                            progress=self._on_download_progress,
                        )
                    else:
                        LOGGER.error(
                            "Bot and USER_SESSION_STRING cannot access source chat %s or dump_chat; personal user session is restricted to channel access only.",
                            getattr(message.chat, "id", "?"),
                        )
            if self._listener.is_cancelled:
                return False
            return download is not None
        except (FloodWait, FloodPremiumWait) as f:
            LOGGER.warning(str(f))
            await sleep(f.value)
            return await self._download_file(message, path)
        except (FileNotFoundError, ConnectionError) as e:
            # Pyrogram may try to remove its .temp file after StopTransmission.
            # Cancellation/cleanup can remove that file first; this is not a
            # second failure and must not trigger retries or noisy tracebacks.
            if self._listener.is_cancelled:
                LOGGER.info(
                    f"Telegram download stopped for {self._listener.name}: {e}"
                )
            else:
                LOGGER.error(
                    f"Telegram download failed for {self._listener.name}: {e}",
                    exc_info=True,
                )
            return False
        except Exception as e:
            if self._listener.is_cancelled:
                LOGGER.info(
                    f"Telegram download cancelled for {self._listener.name}"
                )
                return False
            LOGGER.error(str(e), exc_info=True)
            return False

    async def _download(self, message, path):
        success = await self._download_file(message, path)
        if success:
            await self._on_download_complete()
        elif not self._listener.is_cancelled:
            await self._on_download_error("Internal error occurred")
        return

    async def _download_direct_url(self, url, path):
        try:
            import aiohttp
            from aiofiles import open as aiopen
            async with aiohttp.ClientSession() as session:
                async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=1800)) as resp:
                    if resp.status == 200:
                        filename = None
                        if "Content-Disposition" in resp.headers:
                            cd = resp.headers["Content-Disposition"]
                            fname_match = re.findall(r'filename="?([^";]+)"?', cd)
                            if fname_match:
                                filename = fname_match[0]
                        if not filename:
                            filename = url.rsplit("/", 1)[-1].split("?")[0] or f"file_{self._listener.mid}"
                        if ospath.isdir(path) or path.endswith("/"):
                            await makedirs(path, exist_ok=True)
                            dest_file = ospath.join(path, filename)
                        else:
                            dest_file = path
                            await makedirs(ospath.dirname(dest_file), exist_ok=True)
                        async with aiopen(dest_file, "wb") as f:
                            async for chunk in resp.content.iter_chunked(1024 * 1024):
                                await f.write(chunk)
                        return True
        except Exception as e:
            LOGGER.warning(f"Error downloading direct URL {url}: {e}")
        return False

    async def _process_text_links(self, msg_text, path):
        if not msg_text:
            return False
        urls = re.findall(r"(?:https?:\/\/|magnet:\?|tg:\/\/)\S+", msg_text)
        if not urls:
            return False
        found_any = False
        for url in urls:
            if self._listener.is_cancelled:
                break
            url = url.rstrip(".,)]}>\"'")
            if not url:
                continue
            from ...ext_utils.links_utils import (
                is_telegram_link,
                is_url,
                is_magnet,
                is_gdrive_link,
                is_gdrive_id,
                is_mega_link,
            )
            if is_telegram_link(url):
                try:
                    from ...telegram_helper.message_utils import get_tg_link_message
                    user_range_mode = self._listener.user_dict.get("RANGE_LINK_MODE") or getattr(Config, "RANGE_LINK_MODE", "normal")
                    tg_msg, s_sess = await get_tg_link_message(url, range_mode=user_range_mode, user_id=self._listener.user_id, user_dict=self._listener.user_dict)
                    if isinstance(tg_msg, list):
                        for tm in tg_msg:
                            if isinstance(tm, str):
                                sub_msg, _ = await get_tg_link_message(tm, range_mode="normal", user_id=self._listener.user_id, user_dict=self._listener.user_dict)
                                tm = sub_msg[0] if isinstance(sub_msg, list) and sub_msg else sub_msg
                            if getattr(tm, "media", None):
                                m_obj = getattr(tm, tm.media.value)
                                f_name = (
                                    m_obj.file_name.rsplit("/", 1)[-1]
                                    if hasattr(m_obj, "file_name") and m_obj.file_name
                                    else f"file_{getattr(tm, 'id', self._listener.mid)}"
                                )
                                await self._download_file(tm, ospath.join(path, f_name))
                                found_any = True
                            elif getattr(tm, "text", None) or getattr(tm, "caption", None):
                                sub_res = await self._process_text_links(tm.text or tm.caption, path)
                                if sub_res:
                                    found_any = True
                    elif tg_msg and getattr(tg_msg, "media", None):
                        m_obj = getattr(tg_msg, tg_msg.media.value)
                        f_name = (
                            m_obj.file_name.rsplit("/", 1)[-1]
                            if hasattr(m_obj, "file_name") and m_obj.file_name
                            else f"file_{getattr(tg_msg, 'id', self._listener.mid)}"
                        )
                        await self._download_file(tg_msg, ospath.join(path, f_name))
                        found_any = True
                    elif tg_msg and (getattr(tg_msg, "text", None) or getattr(tg_msg, "caption", None)):
                        sub_res = await self._process_text_links(tg_msg.text or tg_msg.caption, path)
                        if sub_res:
                            found_any = True
                except Exception as e:
                    LOGGER.warning(f"Error fetching nested TG link {url}: {e}")
            elif is_gdrive_link(url) or is_gdrive_id(url):
                try:
                    from ..download_utils.gd_download import add_gd_download
                    orig_link = self._listener.link
                    self._listener.link = url
                    await add_gd_download(self._listener, path)
                    self._listener.link = orig_link
                    found_any = True
                except Exception as e:
                    LOGGER.warning(f"Error downloading GD link {url}: {e}")
            elif is_mega_link(url):
                try:
                    from ..download_utils.mega_download import add_mega_download
                    orig_link = self._listener.link
                    self._listener.link = url
                    await add_mega_download(self._listener, path)
                    self._listener.link = orig_link
                    found_any = True
                except Exception as e:
                    LOGGER.warning(f"Error downloading Mega link {url}: {e}")
            elif is_magnet(url) or (is_url(url) and (url.endswith(".torrent") or "magnet:" in url)):
                try:
                    from ..download_utils.aria2_download import add_aria2_download
                    orig_link = self._listener.link
                    self._listener.link = url
                    await add_aria2_download(self._listener, path, "", None, None)
                    self._listener.link = orig_link
                    found_any = True
                except Exception as e:
                    LOGGER.warning(f"Error downloading torrent/magnet link {url}: {e}")
            elif is_url(url):
                res = await self._download_direct_url(url, path)
                if res:
                    found_any = True
        return found_any

    async def add_download(self, message, path, session):
        self.session = session
        await TgClient.ensure_user_client()
        self._access_client = getattr(message, "_client", None)
        # Always use Bot Settings USER_SESSION_STRING (TgClient.user) for downloads;
        # Misc Settings personal session is strictly for channel access / reference resolution.
        self._user_client = TgClient.user
        if not self.session:
            if self._hyper_dl:
                self.session = "hbots"
            elif (
                self._listener.transmission_mode in ("user", "both")
                and self._listener.is_super_chat
            ):
                if not self._user_client:
                    LOGGER.warning(
                        "User session not available, downloading with bot session"
                    )
                    self.session = "bot"
                else:
                    self.session = "user"
                    try:
                        await TgClient.ensure_peer(
                            self._user_client, message.chat.id, fallback_client=TgClient.bot
                        )
                        message = await self._user_client.get_messages(
                            chat_id=message.chat.id, message_ids=message.id
                        )
                    except (PeerIdInvalid, ChannelInvalid):
                        LOGGER.warning(
                            "User session is not in this chat, downloading with bot session"
                        )
                        self.session = "bot"
            else:
                self.session = "bot"
        media = getattr(message, message.media.value) if message.media else None

        if media is not None:
            async with global_lock:
                download = media.file_unique_id not in GLOBAL_GID

            if download:
                fallback_name = (
                    media.file_name.rsplit("/", 1)[-1]
                    if hasattr(media, "file_name") and media.file_name
                    else f"file_{self._listener.mid}"
                )
                name_source = self._listener.user_dict.get("NAME_SOURCE", "caption")
                if name_source == "caption" and message.caption:
                    self._listener.name = clean_caption_filename(message.caption, fallback_name)
                elif not self._listener.name:
                    self._listener.name = fallback_name

                if path.endswith("/"):
                    path = path + self._listener.name
                self._listener.size = media.file_size
                gid = token_hex(5)

                add_to_queue, event = await check_running_tasks(self._listener)
                if add_to_queue:
                    LOGGER.info(f"Added to Queue/Download: {self._listener.name}")
                    async with task_dict_lock:
                        task_dict[self._listener.mid] = QueueStatus(
                            self._listener, gid, "dl"
                        )
                    await self._listener.on_download_start()
                    if self._listener.multi <= 1:
                        await send_status_message(self._listener.message)
                    await event.wait()
                    if self.session == "bot":
                        message = await self._listener.client.get_messages(
                            chat_id=message.chat.id, message_ids=message.id
                        )
                    elif self._user_client:
                        try:
                            message = await self._user_client.get_messages(
                                chat_id=message.chat.id, message_ids=message.id
                            )
                        except (PeerIdInvalid, ChannelInvalid):
                            message = await self._listener.client.get_messages(
                                chat_id=message.chat.id, message_ids=message.id
                            )
                    else:
                        message = await self._listener.client.get_messages(
                            chat_id=message.chat.id, message_ids=message.id
                        )
                    if self._listener.is_cancelled:
                        async with global_lock:
                            if self._id in GLOBAL_GID:
                                GLOBAL_GID.pop(self._id)
                        return
                self._start_time = time()
                await self._on_download_start(media.file_unique_id, gid, add_to_queue)
                await self._download(message, path)
                msg_text = message.text or message.caption or ""
                if msg_text:
                    await self._process_text_links(msg_text, ospath.dirname(path) if ospath.isfile(path) else path)
            else:
                await self._on_download_error("File already being downloaded!")
        else:
            msg_text = message.text or message.caption or ""
            if msg_text:
                gid = token_hex(5)
                self._start_time = time()
                await self._on_download_start(f"txt_{self._listener.mid}", gid, False)
                res = await self._process_text_links(msg_text, path)
                if res and not self._listener.is_cancelled:
                    await self._on_download_complete()
                elif not self._listener.is_cancelled:
                    await self._on_download_error("No valid downloadable link found in message text!")
            else:
                await self._on_download_error(
                    "No document or link found in the replied message!"
                )

    async def add_range_download(self, messages, path, session):
        self.session = session or "bot"
        if not self._listener.name:
            self._listener.name = f"Telegram_Range_{self._listener.mid}"

        total_size = 0
        for msg in messages:
            if msg.media:
                media = getattr(msg, msg.media.value, None)
                if media and hasattr(media, "file_size") and media.file_size:
                    total_size += media.file_size
        self._listener.size = total_size

        gid = token_hex(5)
        add_to_queue, event = await check_running_tasks(self._listener)
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Download: {self._listener.name}")
            async with task_dict_lock:
                task_dict[self._listener.mid] = QueueStatus(
                    self._listener, gid, "dl"
                )
            await self._listener.on_download_start()
            if self._listener.multi <= 1:
                await send_status_message(self._listener.message)
            await event.wait()
            if self._listener.is_cancelled:
                async with global_lock:
                    if self._id in GLOBAL_GID:
                        GLOBAL_GID.pop(self._id)
                return

        self._start_time = time()
        await self._on_download_start(f"range_{self._listener.mid}", gid, add_to_queue)

        for message in messages:
            if self._listener.is_cancelled:
                break

            if isinstance(message, str):
                try:
                    from ...telegram_helper.message_utils import get_tg_link_message
                    sub_msg, _ = await get_tg_link_message(message, range_mode="normal", user_id=self._listener.user_id, user_dict=self._listener.user_dict)
                    if isinstance(sub_msg, list):
                        message = sub_msg[0]
                    else:
                        message = sub_msg
                except Exception as e:
                    LOGGER.warning(f"Error resolving TG link string {message}: {e}")
                    continue

            media = getattr(message, message.media.value) if hasattr(message, "media") and message.media else None
            if media is not None:
                fallback_name = (
                    media.file_name.rsplit("/", 1)[-1]
                    if hasattr(media, "file_name") and media.file_name
                    else f"file_{getattr(message, 'id', self._listener.mid)}"
                )
                name_source = self._listener.user_dict.get("NAME_SOURCE", "caption")
                if name_source == "caption" and getattr(message, "caption", None):
                    file_name = clean_caption_filename(message.caption, fallback_name)
                else:
                    file_name = fallback_name

                file_path = ospath.join(path, file_name)
                await self._download_file(message, file_path)

            msg_text = (getattr(message, "text", "") or getattr(message, "caption", "")) if hasattr(message, "text") or hasattr(message, "caption") else ""
            if msg_text:
                await self._process_text_links(msg_text, path)

        if not self._listener.is_cancelled:
            await self._on_download_complete()

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(
            f"Cancelling download on user request: name: {self._listener.name} id: {self._id}"
        )
        if self._hyper_dl_instance:
            try:
                await self._hyper_dl_instance.cancel()
            except Exception:
                pass
            self._hyper_dl_instance = None
        await self._on_download_error("Stopped by user!")
