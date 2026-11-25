from asyncio import (
    Event,
    TimeoutError as AsyncTimeout,
    sleep,
    to_thread,
    wait_for,
)
from contextlib import suppress
from html import escape
from os import makedirs, path as ospath
import re
from secrets import token_hex
from time import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import cloudscraper
import requests

from pyrogram.enums import ButtonStyle, ChatType
from pyrogram.filters import create, private, text, user
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import Message

from .. import LOGGER, bot_loop, user_data
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.status_utils import get_readable_time
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import delete_message, edit_message, send_message

GATEWAY_DOMAINS = [
    "https://www.1tamilmv.fi",
    "https://1tamilmv.fi",
    "https://www.1tamilmv.ceo",
]
DEFAULT_BASE_URL = "https://www.1tamilmv.ceo"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
BAD_IMG_WORDS = (
    "smilies",
    "border",
    "utorrent",
    "torrborder",
    "default_large",
    "theme",
    "icon",
    "blank.gif",
)

_MONITOR_TASKS: dict[int, object] = {}
_DEAD_TOPICS: set[str] = set()
_TOPIC_PICKERS: dict[str, dict] = {}
_FAIL_COUNTS: dict[str, int] = {}
_INPUT_WAIT_TIMEOUT = 120


def _default_tmv_settings() -> dict:
    return {
        "enabled": False,
        "paused": False,
        "baseline_initialized": False,
        "dumps": [],  # list of {"name": "." or keyword, "chat_id": str}
        "domain": "",
        "active_domain": DEFAULT_BASE_URL,
        "settings_mode": "user",  # "user" | "independent"
        "interval": 360,
        "download_mode": "all",  # "all" | "magnet" | "direct" | "torrent"
        "independent": {
            "UPLOAD_DEST": "telegram",  # "telegram" | "mirror" | "uphoster" | custom -up
            "LEECH_DUMP_CHAT": "",
            "AS_DOCUMENT": False,
            "AUTO_THUMBNAIL": True,
            "THUMBNAIL_MODE": "auto",
            "LEECH_PREFIX": "",
            "LEECH_SUFFIX": "",
            "LEECH_CAPTION": "",
            "LEECH_FONT": "none",
            # Size limits (bytes). -1 = unlimited. Applies to monitor auto-leech releases.
            "DOWNLOAD_SIZE_LIMIT": 4 * 1024 * 1024 * 1024,  # 4 GiB default
            "ZIP_SIZE_LIMIT": -1,  # unlimited by default
        },
        "seen_links": [],
        "seen_topics": [],
    }


def _get_tmv_cfg(uid: int) -> dict:
    ud = user_data.setdefault(uid, {})
    cfg = ud.get("TMV_SETTINGS")
    if not isinstance(cfg, dict):
        cfg = _default_tmv_settings()
        ud["TMV_SETTINGS"] = cfg
    defaults = _default_tmv_settings()
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v if not isinstance(v, (dict, list)) else (dict(v) if isinstance(v, dict) else list(v))
    if not isinstance(cfg.get("independent"), dict):
        cfg["independent"] = dict(defaults["independent"])
    else:
        for ik, iv in defaults["independent"].items():
            cfg["independent"].setdefault(ik, iv)
    if not isinstance(cfg.get("seen_links"), list):
        cfg["seen_links"] = []
    if not isinstance(cfg.get("seen_topics"), list):
        cfg["seen_topics"] = []
    if not isinstance(cfg.get("dumps"), list):
        cfg["dumps"] = []
    return cfg


async def _save_tmv_cfg(uid: int):
    if Config.DATABASE_URL:
        with suppress(Exception):
            await database.update_user_data(uid)


def _create_scraper():
    try:
        return cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    except Exception:
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        return s


def _norm(url: str) -> str:
    if not url:
        return url
    p = urlparse(url)
    path = p.path.rstrip("/")
    query = p.query.rstrip("/")
    return (path or "/") + (f"?{query}" if query else "")


def _topic_id(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"/topic/(\d+)", url)
    return f"topic_id:{m.group(1)}" if m else ""


def _extract_size(text_val: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?\s*(?:GB|MB|KB))", text_val, re.IGNORECASE)
    return m.group(1) if m else "Unknown"


def _clean_release_title(raw_title: str) -> str:
    title = re.sub(r"^www\.\S+\s*-\s*", "", raw_title.strip(), flags=re.IGNORECASE).strip()
    if title.lower().endswith(".torrent"):
        title = title[:-8].strip()
    if title.lower().endswith(".mkv"):
        title = title[:-4].strip()
    return re.sub(r"\s+", " ", title).strip() or "1TamilMV Release"


def _parse_domains_list(raw_domains: str) -> list[str]:
    if not raw_domains:
        return []
    out = []
    for part in str(raw_domains).split(","):
        dom = part.strip().rstrip("/")
        if not dom:
            continue
        if not dom.startswith(("http://", "https://")):
            dom = f"https://{dom}"
        if dom not in out:
            out.append(dom)
    return out


def discover_active_domain_sync(custom_domain: str = "") -> str:
    domains = _parse_domains_list(custom_domain)
    if domains:
        scraper = _create_scraper()
        for dom in domains:
            try:
                r = scraper.get(f"{dom}/index.php?/forums/topic/", timeout=10, allow_redirects=True)
                if r.status_code == 200:
                    return dom
            except Exception:
                pass
        return domains[0]

    scraper = _create_scraper()
    for gateway in ("https://www.1tamilmv.fi", "https://1tamilmv.fi"):
        try:
            r = scraper.get(gateway, timeout=8, allow_redirects=False)
            if r.status_code in (301, 302, 307, 308) and r.headers.get("Location"):
                p = urlparse(r.headers["Location"])
                final = f"{p.scheme}://{p.netloc}".rstrip("/")
                if "tamilmv" in final.lower():
                    return final
        except Exception:
            pass

    for domain in GATEWAY_DOMAINS:
        try:
            r = scraper.get(domain, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                m = re.search(
                    r"official\s+website\s+(WWW\.[A-Z0-9.-]+)", r.text, re.IGNORECASE
                )
                if m:
                    return f"https://{m.group(1).lower()}".rstrip("/")
                p = urlparse(r.url)
                return f"{p.scheme}://{p.netloc}".rstrip("/")
        except Exception:
            pass

    return DEFAULT_BASE_URL


def _parse_topic_sync(scraper, topic_url: str, forum_url: str = ""):
    if _norm(topic_url) in _DEAD_TOPICS:
        return None
    if not forum_url:
        p = urlparse(topic_url)
        forum_url = f"{p.scheme}://{p.netloc}/index.php?/forums/topic/"
    r = scraper.get(topic_url, timeout=20, headers={"Referer": forum_url})
    if r.status_code in (404, 410):
        _DEAD_TOPICS.add(_norm(topic_url))
        return None
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    post = (
        soup.find("div", attrs={"data-role": "commentContent"})
        or soup.find("div", class_="cPost_contentWrap")
    )
    if not post:
        return None

    poster_url = None
    for img in post.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src or any(bad in src.lower() for bad in BAD_IMG_WORDS):
            continue
        poster_url = urljoin(topic_url, src)
        break

    releases = []
    current = None
    for a in post.find_all("a"):
        href = (a.get("href") or "").strip()
        href_lower = href.lower()
        fileext = a.get("data-fileext")
        raw_text = a.get_text(" ", strip=True)

        is_torrent = (
            (fileext and fileext.lower() == "torrent")
            or ("attachment.php" in href_lower and "key=" in href_lower)
            or href_lower.endswith(".torrent")
        )
        if is_torrent:
            current = {
                "title": _clean_release_title(raw_text),
                "torrent_link": urljoin(topic_url, href),
                "size": _extract_size(raw_text),
                "direct_link": None,
                "magnet": None,
            }
            releases.append(current)
        elif current:
            classes = [c.lower() for c in a.get("class", [])]
            is_direct = (
                "DIRECT" in raw_text.upper()
                or "cyberloom" in href_lower
                or any("download" in c for c in classes)
            )
            if href_lower.startswith("magnet:"):
                if not current["magnet"]:
                    current["magnet"] = href
            elif is_direct and href.startswith("http") and not current["direct_link"]:
                current["direct_link"] = href

    if not releases:
        return None

    title_tag = soup.find("h1") or soup.find("title")
    topic_title = (
        _clean_release_title(title_tag.get_text()) if title_tag else releases[0]["title"]
    )
    return {
        "topic_url": topic_url,
        "title": topic_title,
        "poster_url": poster_url,
        "releases": releases,
    }


def crawl_tbl_sync(base_domains: str, max_topics: int = 30):
    scraper = _create_scraper()
    candidates = _parse_domains_list(base_domains)
    resp = None
    active_base = ""

    def _has_forum_topics(html_text: str) -> bool:
        return bool(html_text and re.search(r"index\.php\?/forums/topic/", html_text, re.IGNORECASE))

    for dom in candidates:
        try:
            forum_url = f"{dom}/index.php?/forums/topic/"
            r = scraper.get(forum_url, timeout=20, headers={"Referer": dom})
            r.raise_for_status()
            if _has_forum_topics(r.text):
                resp = r
                active_base = dom
                break
            LOGGER.warning("TMV domain %s returned 200 without forum topic links (possible challenge)", dom)
        except Exception as exc:
            LOGGER.warning("TMV domain %s fetch failed: %s", dom, exc)
            continue

    if resp is None:
        active_base = discover_active_domain_sync()
        forum_url = f"{active_base}/index.php?/forums/topic/"
        try:
            r = scraper.get(forum_url, timeout=20, headers={"Referer": active_base})
            r.raise_for_status()
            if _has_forum_topics(r.text):
                resp = r
            else:
                LOGGER.warning("TMV discovered domain %s returned 200 without topic links", active_base)
        except Exception as exc:
            LOGGER.warning("TMV crawl failed on discovered domain %s: %s", active_base, exc)
            return [], active_base, False

    if resp is None:
        return [], active_base, False

    soup = BeautifulSoup(resp.text, "html.parser")
    recently_added_links, other_banger_links, general_links = [], [], []

    for b in soup.find_all("div", class_="banger-container"):
        header = b.find("div", class_="banger-header")
        header_text = header.get_text() if header else b.get_text()
        is_recent = "RECENTLY ADDED" in header_text.upper()
        for a in b.find_all("a", href=True):
            href = a.get("href", "").strip()
            if re.search(r"index\.php\?/forums/topic/", href, re.IGNORECASE) and not href.endswith("-0/"):
                full_url = urljoin(active_base, href)
                (recently_added_links if is_recent else other_banger_links).append(full_url)

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if re.search(r"index\.php\?/forums/topic/", href, re.IGNORECASE) and not href.endswith("-0/"):
            general_links.append(urljoin(active_base, href))

    def _dedupe(urls):
        seen, out = set(), []
        for u in urls:
            k = _topic_id(u) or _norm(u)
            if k not in seen:
                seen.add(k)
                out.append(u)
        return out

    top_links = _dedupe(other_banger_links)
    rest_links = _dedupe(recently_added_links + general_links)
    top_keys = {_topic_id(u) or _norm(u) for u in top_links}
    rest_links = [u for u in rest_links if (_topic_id(u) or _norm(u)) not in top_keys]
    topic_links = top_links + rest_links
    scan_limit = len(top_links) + max_topics

    target_urls = [u for u in topic_links[:scan_limit] if _norm(u) not in _DEAD_TOPICS]
    forum_ref = f"{active_base}/index.php?/forums/topic/"
    LOGGER.info(
        "TMV crawl: active_base=%s, found=%d topics (top=%d, recent=%d), scanning=%d",
        active_base,
        len(topic_links),
        len(top_links),
        len(_dedupe(recently_added_links)),
        len(target_urls),
    )

    topics = []
    for t_url in target_urls:
        try:
            # Reuse the same scraper session so Cloudflare cookies from the forum page persist.
            res = _parse_topic_sync(scraper, t_url, forum_ref)
            if res:
                topics.append(res)
        except Exception as exc:
            LOGGER.warning("TMV topic parse failed %s: %s", t_url, exc)

    crawl_ok = bool(topics) or not topic_links
    LOGGER.info("TMV crawl completed on %s: parsed %d topic(s) with releases", active_base, len(topics))
    return topics, active_base, crawl_ok


def _resolve_cyberloom_sync(url: str) -> str:
    if not url or "cyberloom" not in url.lower():
        return url
    try:
        scraper = _create_scraper()
        r = scraper.get(url, timeout=15, allow_redirects=True)
        if r.url and "cyberloom" not in r.url.lower() and "google.com/search" not in r.url.lower():
            return r.url
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("http://", "https://")) and any(
                tok in (a.get_text(" ", strip=True).lower() + " " + href.lower())
                for tok in ("download", "direct", "dl", "file", "cdn", "storage")
            ):
                if "cyberloom" not in href.lower():
                    return href
    except Exception:
        pass
    return url


def _download_torrent_file_sync(torrent_url: str, base_url: str, uid: int, title: str) -> str | None:
    try:
        scraper = _create_scraper()
        r = scraper.get(torrent_url, timeout=25, headers={"Referer": base_url})
        r.raise_for_status()
        if not r.content:
            return None
        folder = f"/tmp/tmv_torrents_{uid}"
        makedirs(folder, exist_ok=True)
        safe_name = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:100] or f"tmv_{int(time())}"
        fp = ospath.join(folder, f"{safe_name}.torrent")
        with open(fp, "wb") as f:
            f.write(r.content)
        return fp
    except Exception as exc:
        LOGGER.warning("TMV .torrent download failed (%s): %s", torrent_url, exc)
        return None


def _rel_links(rel: dict) -> list[str]:
    return [l for l in (rel.get("torrent_link"), rel.get("direct_link"), rel.get("magnet")) if l]


async def _pick_single_release_source(rel: dict, dl_mode: str, active_base: str, uid: int):
    """Pick ONE link per release: prefer magnet or .torrent (via qBittorrent), else cyberloom/direct."""
    if dl_mode == "direct" and rel.get("direct_link"):
        link = await to_thread(_resolve_cyberloom_sync, rel["direct_link"])
        return link, None, False
    if dl_mode == "torrent" and rel.get("torrent_link"):
        tor = await to_thread(_download_torrent_file_sync, rel["torrent_link"], active_base, uid, rel["title"])
        return (tor or rel["torrent_link"]), tor, True
    if dl_mode == "magnet" and rel.get("magnet"):
        return rel["magnet"], None, True

    # "all" or fallback: ALWAYS prefer magnet or .torrent (qBittorrent) over cyberloom
    if rel.get("magnet"):
        return rel["magnet"], None, True
    if rel.get("torrent_link"):
        tor = await to_thread(_download_torrent_file_sync, rel["torrent_link"], active_base, uid, rel["title"])
        return (tor or rel["torrent_link"]), tor, True
    if rel.get("direct_link"):
        link = await to_thread(_resolve_cyberloom_sync, rel["direct_link"])
        return link, None, False
    return None, None, False


async def _dispatch_release_to_lmb(
    uid: int,
    rel: dict,
    poster_url: str | None,
    active_base: str,
    chat_id: int | None = None,
    mode_override: dict | None = None,
):
    from .mirror_leech import Mirror

    from ..core.torrent_manager import TorrentManager

    cfg = _get_tmv_cfg(uid)
    dl_mode = cfg.get("download_mode", "all")
    # Enforce per-user download / zip size limits for monitor auto-leech.
    ind = cfg.get("independent") or {}
    size_str = str(rel.get("size") or "").strip()
    size_bytes = 0
    if size_str:
        try:
            from ..helper.ext_utils.bot_utils import get_size_bytes
            size_bytes = get_size_bytes(size_str) or 0
        except Exception:
            size_bytes = 0
    is_zip_like = any(
        x in (rel.get("title") or "").lower()
        for x in (".zip", ".rar", ".7z", ".tar", ".gz")
    ) or (size_str and any(x in size_str.lower() for x in ()))
    limit_key = "ZIP_SIZE_LIMIT" if is_zip_like else "DOWNLOAD_SIZE_LIMIT"
    limit_val = ind.get(limit_key, -1)
    try:
        limit_val = int(limit_val)
    except (TypeError, ValueError):
        limit_val = -1
    if limit_val >= 0 and size_bytes > 0 and size_bytes > limit_val:
        from ..helper.ext_utils.status_utils import get_readable_file_size
        LOGGER.info(
            "TMV skip oversized release [%s]: %s (%s > limit %s)",
            uid,
            rel.get("title"),
            get_readable_file_size(size_bytes),
            get_readable_file_size(limit_val),
        )
        return False
    target_link, torrent_file_path, use_qbit = await _pick_single_release_source(
        rel, dl_mode, active_base, uid
    )
    if not target_link:
        return False
    if use_qbit and TorrentManager.qbittorrent is None:
        LOGGER.warning("TMV: qBittorrent client unavailable; falling back to Aria2 for %s", rel.get("title"))
        use_qbit = False

    target_chat = chat_id or uid
    is_leech = True
    is_uphoster = False
    up_dest_flag = ""

    if mode_override is not None:
        is_leech = bool(mode_override.get("is_leech", True))
        is_uphoster = bool(mode_override.get("is_uphoster", False))
        up_dest_flag = mode_override.get("up_dest", "")
    elif cfg.get("settings_mode") == "independent":
        ind = cfg.get("independent", {})
        up_dest = str(ind.get("UPLOAD_DEST", "telegram") or "telegram").strip()
        low_dest = up_dest.lower()
        if low_dest in ("telegram", "tg", "leech"):
            is_leech = True
            is_uphoster = False
        elif low_dest in ("uphoster", "ddl", "gofile", "buzzheavier", "pixeldrain"):
            is_leech = False
            is_uphoster = True
            if low_dest not in ("uphoster", "ddl"):
                up_dest_flag = low_dest
        elif low_dest in ("mirror", "gdrive", "gd", "rclone", "rc"):
            is_leech = False
            is_uphoster = False
            if low_dest in ("gd", "rc"):
                up_dest_flag = low_dest
        else:
            is_leech = False
            up_dest_flag = up_dest

    if is_uphoster:
        cmd_name = BotCommands.UpHosterCommand[0] if isinstance(BotCommands.UpHosterCommand, list) else BotCommands.UpHosterCommand
    elif is_leech:
        cmd_list = BotCommands.QbLeechCommand if use_qbit else BotCommands.LeechCommand
        cmd_name = cmd_list[0] if isinstance(cmd_list, list) else cmd_list
    else:
        cmd_list = BotCommands.QbMirrorCommand if use_qbit else BotCommands.MirrorCommand
        cmd_name = cmd_list[0] if isinstance(cmd_list, list) else cmd_list

    flags = []
    if mode_override is not None:
        if mode_override.get("extra_flags"):
            flags.append(mode_override["extra_flags"])
    elif cfg.get("settings_mode") == "independent":
        ind = cfg.get("independent", {})
        flags.append("-doc" if ind.get("AS_DOCUMENT") else "-med")
        if up_dest_flag:
            flags.append(f"-up {up_dest_flag}")
        elif is_leech and ind.get("LEECH_DUMP_CHAT"):
            flags.append(f"-up {ind['LEECH_DUMP_CHAT']}")
    else:
        if up_dest_flag:
            flags.append(f"-up {up_dest_flag}")

    flag_str = " ".join(flags).strip()
    if torrent_file_path and ospath.isfile(torrent_file_path):
        cmd_text = f"/{cmd_name} {flag_str}".strip()
        try:
            tor_msg = await TgClient.bot.send_document(
                chat_id=target_chat,
                document=torrent_file_path,
                caption=f"🎬 <b>{escape(rel['title'])}</b> ({escape(str(rel.get('size', 'Unknown')))})",
                disable_notification=True,
            )
            synth = await send_message(tor_msg, cmd_text)
        except Exception as exc:
            LOGGER.warning("TMV send_document failed (%s); sending direct command", exc)
            synth = await send_message(target_chat, cmd_text)
    else:
        cmd_text = f"/{cmd_name} {target_link} {flag_str}".strip()
        synth = await send_message(target_chat, cmd_text)

    if not isinstance(synth, Message):
        LOGGER.warning("TMV dispatch failed to create synthetic message for %s: %r", rel.get("title"), synth)
        return False

    with suppress(Exception):
        fetched = await TgClient.bot.get_messages(chat_id=target_chat, message_ids=synth.id)
        if isinstance(fetched, Message):
            synth = fetched

    if getattr(synth, "from_user", None) is None or synth.from_user.id != uid:
        try:
            synth.from_user = await TgClient.bot.get_users(uid)
        except Exception as exc:
            LOGGER.warning("TMV get_users(%s) failed (%s); using synthetic user", uid, exc)
            synth.from_user = SimpleNamespace(
                id=uid,
                first_name=f"User {uid}",
                username=None,
                is_bot=False,
                mention=lambda style="html": f'<a href="tg://user?id={uid}">User {uid}</a>',
            )

    if torrent_file_path and ospath.isfile(torrent_file_path):
        setattr(synth, "_tmv_torrent_path", torrent_file_path)

    if mode_override is None:
        setattr(synth, "_tmv_monitor", True)
        setattr(synth, "_tmv_auto_extract", True)
        if cfg.get("settings_mode") == "independent":
            setattr(synth, "_tmv_overrides", dict(cfg.get("independent", {})))
    if cfg.get("dumps"):
        setattr(synth, "_tmv_dump_rules", list(cfg.get("dumps", [])))

    LOGGER.info(
        "TMV dispatching [%s]: '%s' (qbit=%s, leech=%s, uphoster=%s)",
        uid,
        rel.get("title"),
        use_qbit,
        is_leech,
        is_uphoster,
    )
    try:
        await Mirror(
            TgClient.bot,
            synth,
            is_qbit=use_qbit,
            is_leech=is_leech,
            is_uphoster=is_uphoster,
        ).new_event()
        return True
    except Exception as exc:
        LOGGER.exception("TMV Mirror.new_event failed for %s: %s", rel.get("title"), exc)
        return False


async def _run_tmv_cycle(uid: int, force_notify: bool = False) -> tuple[int, str]:
    cfg = _get_tmv_cfg(uid)
    if cfg.get("paused"):
        return 0, "Monitor is paused. Click Resume to continue."
    base_url = cfg.get("domain") or cfg.get("active_domain") or ""
    topics, active_base, crawl_ok = await to_thread(crawl_tbl_sync, base_url, 30)
    if active_base:
        cfg["active_domain"] = active_base

    if not crawl_ok or not topics:
        LOGGER.warning("TMV monitor [%s]: crawl returned no topics on %s (crawl_ok=%s); skipping state update.", uid, active_base, crawl_ok)
        return 0, f"Could not fetch releases from <code>{escape(active_base or DEFAULT_BASE_URL)}</code>. Will retry next cycle."

    seen_links = list(cfg.get("seen_links", []))
    seen_links_set = set(seen_links)
    seen_topics = list(cfg.get("seen_topics", []))
    seen_topics_set = set(seen_topics)
    is_first_run = (not cfg.get("baseline_initialized")) or (not seen_links_set and not seen_topics_set)

    def _remember_link(link_val):
        nk = _norm(link_val)
        if nk and nk not in seen_links_set:
            seen_links_set.add(nk)
            seen_links.append(nk)

    def _remember_topic(topic_val):
        nt = _norm(topic_val)
        if nt and nt not in seen_topics_set:
            seen_topics_set.add(nt)
            seen_topics.append(nt)
        tid = _topic_id(topic_val)
        if tid and tid not in seen_topics_set:
            seen_topics_set.add(tid)
            seen_topics.append(tid)

    if is_first_run:
        for topic in topics:
            t_url = topic.get("topic_url", "")
            if t_url:
                _remember_topic(t_url)
            for rel in topic.get("releases", []):
                for l in _rel_links(rel):
                    _remember_link(l)
        cfg["baseline_initialized"] = True
        cfg["seen_links"] = seen_links[-5000:]
        cfg["seen_topics"] = seen_topics[-2000:]
        await _save_tmv_cfg(uid)
        LOGGER.info(
            "TMV monitor [%s]: baseline initialized on %s (%d topics, %d links). Waiting for new incoming releases.",
            uid,
            active_base,
            len(topics),
            len(seen_links),
        )
        return 0, f"Baseline cached on <code>{escape(active_base)}</code> ({len(topics)} topics). Only new incoming releases will download."

    pending_topics = sum(
        1 for t in topics
        if any(_norm(l) not in seen_links_set for r in t.get("releases", []) for l in _rel_links(r))
    )
    LOGGER.info("TMV monitor [%s]: %d/%d topics have new incoming releases.", uid, pending_topics, len(topics))

    dispatched = 0
    for topic in topics:
        t_url = topic.get("topic_url", "")
        releases = topic.get("releases", [])
        poster_url = topic.get("poster_url")

        new_releases = [
            rel for rel in releases
            if _rel_links(rel) and any(_norm(l) not in seen_links_set for l in _rel_links(rel))
        ]
        if not new_releases:
            if t_url:
                _remember_topic(t_url)
            continue

        all_handled = True
        for rel in new_releases[:5]:
            ok = await _dispatch_release_to_lmb(uid, rel, poster_url, active_base)
            rel_keys = [_norm(l) for l in _rel_links(rel) if _norm(l)]
            if ok:
                dispatched += 1
                for l in _rel_links(rel):
                    _remember_link(l)
                for rk in rel_keys:
                    _FAIL_COUNTS.pop(f"{uid}:{rk}", None)
            else:
                all_handled = False
                for rk in rel_keys:
                    fkey = f"{uid}:{rk}"
                    _FAIL_COUNTS[fkey] = _FAIL_COUNTS.get(fkey, 0) + 1
                    if _FAIL_COUNTS[fkey] >= 3:
                        _remember_link(rk)
                        _FAIL_COUNTS.pop(fkey, None)
            await sleep(1.0)

        if t_url and all_handled:
            _remember_topic(t_url)

    cfg["baseline_initialized"] = True
    cfg["seen_links"] = seen_links[-5000:]
    cfg["seen_topics"] = seen_topics[-2000:]
    await _save_tmv_cfg(uid)

    return dispatched, f"Checked <code>{escape(active_base)}</code> — queued <b>{dispatched}</b> new release(s)."


async def _tmv_monitor_loop(uid: int):
    LOGGER.info("TMV monitor started for user %s", uid)
    while True:
        cfg = _get_tmv_cfg(uid)
        if not cfg.get("enabled"):
            break
        if not cfg.get("paused"):
            try:
                dispatched, note = await _run_tmv_cycle(uid)
                LOGGER.info("TMV monitor [%s]: cycle finished (dispatched=%d)", uid, dispatched)
            except Exception as exc:
                LOGGER.warning("TMV monitor cycle error for %s: %s", uid, exc, exc_info=True)
        interval = max(60, int(cfg.get("interval", 360) or 360))
        await sleep(interval)
    _MONITOR_TASKS.pop(uid, None)
    LOGGER.info("TMV monitor stopped for user %s", uid)


def _ensure_monitor(uid: int):
    task = _MONITOR_TASKS.get(uid)
    if task is None or task.done():
        _MONITOR_TASKS[uid] = bot_loop.create_task(_tmv_monitor_loop(uid))


def _stop_monitor(uid: int):
    task = _MONITOR_TASKS.pop(uid, None)
    if task and not task.done():
        task.cancel()


async def start_tmv_monitors():
    for uid, ud in list(user_data.items()):
        if isinstance(ud, dict) and isinstance(ud.get("TMV_SETTINGS"), dict):
            if ud["TMV_SETTINGS"].get("enabled"):
                _ensure_monitor(int(uid))


# ---------- -tmv Interactive Topic Quality Picker ----------

_PICKER_PER_PAGE = 8


def _picker_filtered_indices(releases: list[dict], tab: str) -> list[int]:
    if tab == "magnet":
        return [i for i, r in enumerate(releases) if r.get("magnet")]
    if tab == "torrent":
        return [i for i, r in enumerate(releases) if r.get("torrent_link")]
    if tab == "direct":
        return [i for i, r in enumerate(releases) if r.get("direct_link")]
    return list(range(len(releases)))


def _render_picker_ui(token: str):
    state = _TOPIC_PICKERS[token]
    topic = state["topic"]
    selected = state["selected"]  # set of (idx, kind)
    releases = topic["releases"]
    tab = state.get("tab", "all")

    mag_cnt = sum(1 for r in releases if r.get("magnet"))
    tor_cnt = sum(1 for r in releases if r.get("torrent_link"))
    cyb_cnt = sum(1 for r in releases if r.get("direct_link"))

    indices = _picker_filtered_indices(releases, tab)
    total = len(indices)
    total_pages = max(1, (total + _PICKER_PER_PAGE - 1) // _PICKER_PER_PAGE)
    page = max(0, min(state.get("page", 0), total_pages - 1))
    state["page"] = page
    start = page * _PICKER_PER_PAGE
    end = min(total, start + _PICKER_PER_PAGE)

    tab_names = {"all": "All", "magnet": "Magnet", "torrent": "Torrent", "direct": "Cyberloom"}
    lines = [
        f"<b>🎬 {escape(topic['title'])}</b>",
        f"<blockquote>Tab: <b>{tab_names.get(tab, 'All')}</b> ({start + 1 if total else 0}–{end} of {total} • Page {page + 1}/{total_pages}) | Selected: <b>{len(selected)}</b></blockquote>",
    ]
    btns = ButtonMaker()

    # Source-type filter tabs (only show types available on this page)
    if (mag_cnt > 0) + (tor_cnt > 0) + (cyb_cnt > 0) > 1:
        btns.data_button(f"{'• ' if tab == 'all' else ''}📋 All ({len(releases)})", f"tmvp {token} tab all", "header")
    if mag_cnt:
        btns.data_button(f"{'• ' if tab == 'magnet' else ''}🧲 Magnet ({mag_cnt})", f"tmvp {token} tab magnet", "header")
    if tor_cnt:
        btns.data_button(f"{'• ' if tab == 'torrent' else ''}📄 Torrent ({tor_cnt})", f"tmvp {token} tab torrent", "header")
    if cyb_cnt:
        btns.data_button(f"{'• ' if tab == 'direct' else ''}🔗 Cyberloom ({cyb_cnt})", f"tmvp {token} tab direct", "header")

    sel_kind = tab if tab in ("magnet", "torrent", "direct") else "all"
    for idx in indices[start:end]:
        rel = releases[idx]
        mark = "✅" if (idx, sel_kind) in selected else "⬜"
        if sel_kind == "magnet":
            src_tag = "🧲"
        elif sel_kind == "torrent":
            src_tag = "📄"
        elif sel_kind == "direct":
            src_tag = "🔗"
        else:
            src_tag = "🧲" if rel.get("magnet") else ("📄" if rel.get("torrent_link") else "🔗")
        label = f"{mark} {src_tag} {rel['title'][:32]} ({rel.get('size', '?')})"
        btns.data_button(label, f"tmvp {token} tog {idx} {sel_kind}")

    if total_pages > 1:
        if page > 0:
            btns.data_button("⬅️ Prev", f"tmvp {token} page {page - 1}", "l_body")
        btns.data_button(f"📄 {page + 1}/{total_pages}", f"tmvp {token} noop", "l_body")
        if page < total_pages - 1:
            btns.data_button("➡️ Next", f"tmvp {token} page {page + 1}", "l_body")

    btns.data_button("☑️ Select All", f"tmvp {token} all", "footer")
    btns.data_button("🧹 Clear", f"tmvp {token} none", "footer")
    btns.data_button(f"✅ Done ({len(selected)})", f"tmvp {token} done", "footer", style=ButtonStyle.SUCCESS)
    btns.data_button("❌ Cancel", f"tmvp {token} cancel", "footer", style=ButtonStyle.DANGER)
    return "\n".join(lines), btns.build_menu(1, h_cols=2, f_cols=2)


async def start_tmv_topic_picker(listener):
    """Handle `<tamilmv_topic_url> -tmv`: scrape topic and let user select Magnet/Torrent/Cyberloom qualities."""
    topic_url = listener.link
    if not topic_url or "http" not in topic_url:
        await send_message(listener.message, "⚠️ Please provide a valid 1TamilMV topic URL with <code>-tmv</code>.")
        return

    wait_msg = await send_message(listener.message, "🔍 <b>Fetching 1TamilMV topic qualities...</b>")
    try:
        scraper = _create_scraper()
        topic = await to_thread(_parse_topic_sync, scraper, topic_url)
    except Exception as exc:
        await edit_message(wait_msg, f"❌ Failed to fetch topic: <code>{escape(str(exc))}</code>")
        return

    if not topic or not topic.get("releases"):
        await edit_message(wait_msg, "⚠️ No downloadable releases (magnet/torrent/cyberloom) found on this page.")
        return

    token = token_hex(4)
    p = urlparse(topic_url)
    active_base = f"{p.scheme}://{p.netloc}".rstrip("/")

    raw_text = (getattr(listener.message, "text", None) or getattr(listener.message, "caption", None) or "").strip()
    raw_tokens = raw_text.split()[1:] if raw_text else []
    preserved = []
    for tok in raw_tokens:
        if tok == "-tmv" or tok == topic_url:
            continue
        preserved.append(tok)
    if getattr(listener, "extract", False) and "-e" not in preserved:
        if isinstance(listener.extract, str):
            preserved.extend(["-e", listener.extract])
        else:
            preserved.append("-e")
    if getattr(listener, "compress", False) and "-z" not in preserved:
        preserved.append("-z")
    if getattr(listener, "seed", False) and "-d" not in preserved:
        preserved.append("-d")
    if getattr(listener, "join", False) and "-j" not in preserved:
        preserved.append("-j")
    extra_flags = preserved

    _TOPIC_PICKERS[token] = {
        "uid": listener.user_id,
        "chat_id": listener.message.chat.id,
        "topic": topic,
        "active_base": active_base,
        "selected": set(),
        "tab": "all",
        "page": 0,
        "mode_override": {
            "is_leech": bool(getattr(listener, "is_leech", False)),
            "is_uphoster": bool(getattr(listener, "is_uphoster", False)),
            "up_dest": getattr(listener, "up_dest", "") or "",
            "extra_flags": " ".join(extra_flags),
        },
    }
    text_msg, markup = _render_picker_ui(token)
    await edit_message(wait_msg, text_msg, markup)


@new_task
async def tmv_picker_callback(_, query):
    parts = query.data.split()
    if len(parts) < 3:
        await query.answer()
        return
    token, action = parts[1], parts[2]
    state = _TOPIC_PICKERS.get(token)
    if not state:
        await query.answer("Selection expired.", show_alert=True)
        return
    if query.from_user.id != state["uid"]:
        await query.answer("This selection menu is not for you!", show_alert=True)
        return

    releases = state["topic"]["releases"]
    if action == "cancel":
        _TOPIC_PICKERS.pop(token, None)
        await query.answer("Cancelled")
        await delete_message(query.message, query.message.reply_to_message)
        return

    if action == "noop":
        await query.answer()
        return

    if action == "tab" and len(parts) > 3:
        state["tab"] = parts[3]
        state["page"] = 0
        await query.answer()
        text_msg, markup = _render_picker_ui(token)
        await edit_message(query.message, text_msg, markup)
        return

    if action == "page" and len(parts) > 3:
        state["page"] = int(parts[3])
        await query.answer()
        text_msg, markup = _render_picker_ui(token)
        await edit_message(query.message, text_msg, markup)
        return

    if action == "tog" and len(parts) > 4:
        key = (int(parts[3]), parts[4])
        if key in state["selected"]:
            state["selected"].remove(key)
        else:
            state["selected"].add(key)
        await query.answer()
        text_msg, markup = _render_picker_ui(token)
        await edit_message(query.message, text_msg, markup)
        return

    if action == "all":
        tab = state.get("tab", "all")
        sel_kind = tab if tab in ("magnet", "torrent", "direct") else "all"
        for idx in _picker_filtered_indices(releases, tab):
            state["selected"].add((idx, sel_kind))
        await query.answer(f"Selected all in {tab} ({len(state['selected'])} total)")
        text_msg, markup = _render_picker_ui(token)
        await edit_message(query.message, text_msg, markup)
        return

    if action == "none":
        state["selected"].clear()
        await query.answer("Selection cleared")
        text_msg, markup = _render_picker_ui(token)
        await edit_message(query.message, text_msg, markup)
        return

    if action == "done":
        if not state["selected"]:
            await query.answer("Select at least one item first!", show_alert=True)
            return
        _TOPIC_PICKERS.pop(token, None)
        chosen = sorted(state["selected"])
        await query.answer(f"Starting {len(chosen)} download(s)...")
        await edit_message(
            query.message,
            f"✅ <b>Queued {len(chosen)} item(s)</b> from <b>{escape(state['topic']['title'])}</b> — starting download &amp; upload...",
        )
        for idx, kind in chosen:
            if not (0 <= idx < len(releases)):
                continue
            rel = dict(releases[idx])
            if kind == "magnet":
                rel["torrent_link"] = None
                rel["direct_link"] = None
            elif kind == "torrent":
                rel["magnet"] = None
                rel["direct_link"] = None
            elif kind == "direct":
                rel["magnet"] = None
                rel["torrent_link"] = None
            await _dispatch_release_to_lmb(
                state["uid"],
                rel,
                state["topic"].get("poster_url"),
                state["active_base"],
                chat_id=state["chat_id"],
                mode_override=state["mode_override"],
            )
            await sleep(1.0)


# ---------- /tmv DM Settings UI ----------

def _build_home_ui(uid: int):
    cfg = _get_tmv_cfg(uid)
    enabled = bool(cfg.get("enabled"))
    paused = bool(cfg.get("paused"))
    status_label = "🟡 Paused" if (enabled and paused) else ("🟢 Running" if enabled else "🔴 Stopped")
    domain_disp = cfg.get("domain") or cfg.get("active_domain") or DEFAULT_BASE_URL
    mode_label = "Independent" if cfg.get("settings_mode") == "independent" else "User Settings"
    dl_mode = str(cfg.get("download_mode", "all")).upper()
    interval = int(cfg.get("interval", 360) or 360)

    text_msg = (
        f"<b>🎬 1TamilMV Auto-Leech (/tmv)</b>\n\n"
        f"<blockquote>"
        f"• <b>Status:</b> {status_label}\n"
        f"• <b>Domains:</b> <code>{escape(domain_disp)}</code>\n"
        f"• <b>Active Domain:</b> <code>{escape(cfg.get('active_domain') or DEFAULT_BASE_URL)}</code>\n"
        f"• <b>Settings Mode:</b> <b>{mode_label}</b>\n"
        f"• <b>Link Filter:</b> <code>{dl_mode}</code>\n"
        f"• <b>Check Interval:</b> <code>{get_readable_time(interval)}</code>\n"
        f"• <b>Dump Rules:</b> <code>{len(cfg.get('dumps', []))}</code>"
        f"</blockquote>"
    )
    btns = ButtonMaker()
    btns.data_button("🌐 Set Domain", f"tmv {uid} domain")
    btns.data_button("🔍 Auto-Detect Domain", f"tmv {uid} autodomain")
    btns.data_button(
        "⏹️ Stop Monitor" if enabled else "▶️ Start Monitor",
        f"tmv {uid} toggle",
        style=ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS,
    )
    if paused:
        btns.data_button("▶️ Resume", f"tmv {uid} resume", style=ButtonStyle.SUCCESS)
    else:
        btns.data_button("⏸️ Pause", f"tmv {uid} pause", style=ButtonStyle.PRIMARY)
    btns.data_button("🔄 Check Now", f"tmv {uid} checknow")
    btns.data_button("📥 Dumps", f"tmv {uid} dumps")
    btns.data_button("⚙️ Settings", f"tmv {uid} settings", style=ButtonStyle.PRIMARY)
    btns.data_button("❌ Close", f"tmv {uid} close", "footer", style=ButtonStyle.DANGER)
    return text_msg, btns.build_menu(2)



def _build_dumps_ui(uid: int):
    cfg = _get_tmv_cfg(uid)
    dumps = cfg.get("dumps", [])
    if dumps:
        rules_lines = "\n".join(
            f"• <b>{i + 1}.</b> <code>{escape(str(d.get('name', '.')))}</code> → <code>{escape(str(d.get('chat_id', '')))}</code>"
            for i, d in enumerate(dumps)
        )
    else:
        rules_lines = "<i>No custom dump rules added yet.</i>"

    text_msg = (
        f"<b>📥 /tmv Dump Rules ({len(dumps)})</b>\n\n"
        f"<blockquote>"
        f"{rules_lines}\n\n"
        f"• Use <code>. | -100xxxx</code> to send <b>all</b> files to that dump.\n"
        f"• Use <code>dvd | -100xxxx</code> to send files matching <code>dvd</code> in filename or caption."
        f"</blockquote>"
    )
    btns = ButtonMaker()
    btns.data_button("➕ Add Dump", f"tmv {uid} add_dump", style=ButtonStyle.SUCCESS)
    if dumps:
        btns.data_button("🧹 Clear All", f"tmv {uid} clear_dumps", style=ButtonStyle.DANGER)
        for i, d in enumerate(dumps[:20]):
            btns.data_button(f"🗑️ {i + 1}. {str(d.get('name', '.'))[:14]}", f"tmv {uid} del_dump {i}")
    btns.data_button("🔙 Back", f"tmv {uid} home", "footer")
    return text_msg, btns.build_menu(2)

def _build_settings_mode_ui(uid: int):
    cfg = _get_tmv_cfg(uid)
    mode = cfg.get("settings_mode", "user")
    text_msg = (
        f"<b>⚙️ /tmv Settings Mode</b>\n\n"
        f"<blockquote>"
        f"• <b>Active Mode:</b> <b>{'Independent' if mode == 'independent' else 'User Settings'}</b>\n\n"
        f"• <b>Independent:</b> Configure a complete, isolated set of settings (Leech, Mirror, Uphoster, Video Tools, FFmpeg, Metadata, and more) dedicated to <code>/tmv</code>.\n"
        f"• <b>User Settings:</b> Automatically use your main <code>/{BotCommands.UserSetCommand[0]}</code> configuration for all <code>/tmv</code> tasks."
        f"</blockquote>"
    )
    btns = ButtonMaker()
    btns.data_button(
        f"{'✅ ' if mode == 'independent' else ''}🎛️ Independent",
        f"tmv {uid} mode independent",
    )
    btns.data_button(
        f"{'✅ ' if mode == 'user' else ''}👤 User Settings",
        f"tmv {uid} mode user",
    )
    btns.data_button("🔙 Back", f"tmv {uid} home", "footer")
    return text_msg, btns.build_menu(2)


def _build_independent_ui(uid: int):
    cfg = _get_tmv_cfg(uid)
    ind = cfg.get("independent", {})
    dl_mode = cfg.get("download_mode", "all").upper()
    interval = int(cfg.get("interval", 360) or 360)
    up_dest = ind.get("UPLOAD_DEST", "telegram")
    dump_val = ind.get("LEECH_DUMP_CHAT") or "Default (Bot Settings)"
    as_doc = "Document" if ind.get("AS_DOCUMENT") else "Media"
    auto_thumb = "ON" if ind.get("AUTO_THUMBNAIL", True) else "OFF"
    prefix = ind.get("LEECH_PREFIX") or "None"
    suffix = ind.get("LEECH_SUFFIX") or "None"
    caption = ind.get("LEECH_CAPTION") or "Default"
    from ..helper.ext_utils.status_utils import get_readable_file_size as _grfs
    def _fmt_limit(val):
        try:
            v = int(val)
        except (TypeError, ValueError):
            return "Unlimited"
        return "Unlimited" if v < 0 else _grfs(v)
    dl_limit = _fmt_limit(ind.get("DOWNLOAD_SIZE_LIMIT", 4 * 1024**3))
    zip_limit = _fmt_limit(ind.get("ZIP_SIZE_LIMIT", -1))

    text_msg = (
        f"<b>🎛️ /tmv Independent Settings</b>\n\n"
        f"<blockquote>"
        f"• <b>Upload Destination:</b> <code>{escape(str(up_dest))}</code>\n"
        f"• <b>Dump Chat:</b> <code>{escape(str(dump_val))}</code>\n"
        f"• <b>Upload As:</b> <code>{as_doc}</code>\n"
        f"• <b>Auto Poster Thumb:</b> <code>{auto_thumb}</code>\n"
        f"• <b>Link Filter:</b> <code>{dl_mode}</code>\n"
        f"• <b>Interval:</b> <code>{get_readable_time(interval)}</code>\n"
        f"• <b>Prefix:</b> <code>{escape(str(prefix))}</code> | <b>Suffix:</b> <code>{escape(str(suffix))}</code>\n"
        f"• <b>Caption:</b> <code>{escape(str(caption)[:40])}</code>\n"
        f"• <b>Download Size Limit:</b> <code>{dl_limit}</code>\n"
        f"• <b>ZIP Size Limit:</b> <code>{zip_limit}</code>"
        f"</blockquote>"
    )
    btns = ButtonMaker()
    btns.data_button("📤 Upload Destination", f"tmv {uid} ind_updest")
    btns.data_button("📥 Set Dump Chat", f"tmv {uid} ind_dump")
    btns.data_button(f"📁 Mode: {as_doc}", f"tmv {uid} ind_doc")
    btns.data_button(f"🖼️ Auto Thumb: {auto_thumb}", f"tmv {uid} ind_thumb")
    btns.data_button(f"🔗 Filter: {dl_mode}", f"tmv {uid} ind_filter")
    btns.data_button("⏱️ Set Interval", f"tmv {uid} ind_interval")
    btns.data_button("✏️ Prefix", f"tmv {uid} ind_prefix")
    btns.data_button("✏️ Suffix", f"tmv {uid} ind_suffix")
    btns.data_button("📝 Caption", f"tmv {uid} ind_caption")
    btns.data_button(f"📏 DL Limit: {dl_limit}", f"tmv {uid} ind_dllimit")
    btns.data_button(f"📦 ZIP Limit: {zip_limit}", f"tmv {uid} ind_ziplimit")
    btns.data_button("🔄 Reset Defaults", f"tmv {uid} ind_reset")
    btns.data_button("🔙 Back", f"tmv {uid} settings", "footer")
    return text_msg, btns.build_menu(2)


def _build_updest_ui(uid: int):
    cfg = _get_tmv_cfg(uid)
    cur = cfg["independent"].get("UPLOAD_DEST", "telegram")
    text_msg = (
        f"<b>📤 Select /tmv Upload Destination</b>\n\n"
        f"<blockquote><b>Current:</b> <code>{escape(str(cur))}</code></blockquote>"
    )
    btns = ButtonMaker()
    btns.data_button("💎 Telegram (Leech)", f"tmv {uid} set_updest telegram")
    btns.data_button("☁️ Mirror (GDrive/Rclone)", f"tmv {uid} set_updest mirror")
    btns.data_button("🌐 Uphoster (DDL)", f"tmv {uid} set_updest uphoster")
    btns.data_button("✏️ Custom (-up Target)", f"tmv {uid} ind_updest_custom")
    btns.data_button("🔙 Back", f"tmvset {uid} back main", "footer")
    return text_msg, btns.build_menu(2)


async def _wait_dm_input(client, uid: int, prompt_msg, prompt_text: str):
    btns = ButtonMaker()
    btns.data_button("❌ Cancel", f"tmv {uid} cancel_input")
    await edit_message(prompt_msg, prompt_text, btns.build_menu(1))
    evt = Event()
    got = {"text": None, "cancelled": False}

    async def _on_msg(_, msg):
        if msg.from_user and msg.from_user.id == uid:
            got["text"] = (msg.text or "").strip()
            evt.set()

    async def _on_cancel(_, query):
        if query.data == f"tmv {uid} cancel_input" and query.from_user.id == uid:
            await query.answer("Cancelled")
            got["cancelled"] = True
            evt.set()

    mh = client.add_handler(MessageHandler(_on_msg, filters=text & user(uid) & private), group=-1)
    ch = client.add_handler(
        CallbackQueryHandler(
            _on_cancel,
            filters=create(lambda _, __, q: q.data == f"tmv {uid} cancel_input" and q.from_user.id == uid),
        ),
        group=-1,
    )
    try:
        await wait_for(evt.wait(), timeout=_INPUT_WAIT_TIMEOUT)
    except AsyncTimeout:
        return None
    finally:
        with suppress(Exception):
            client.remove_handler(*mh)
            client.remove_handler(*ch)

    if got["cancelled"]:
        return None
    return got["text"]


@new_task
async def tmv_command(_, message):
    if message.chat.type != ChatType.PRIVATE:
        btns = ButtonMaker()
        tmv_cmd = BotCommands.TmvCommand[0] if isinstance(BotCommands.TmvCommand, list) else BotCommands.TmvCommand
        btns.url_button("🎬 Open /tmv in Bot DM", f"https://t.me/{TgClient.BNAME}?start=tmv")
        await send_message(
            message,
            f"⚠️ Please use <code>/{tmv_cmd}</code> in my private DM.",
            btns.build_menu(1),
        )
        return

    uid = message.from_user.id
    text_msg, markup = _build_home_ui(uid)
    await send_message(message, text_msg, markup)


@new_task
async def tmv_callback(client, query):
    data = query.data.split()
    if len(data) < 3:
        await query.answer()
        return
    uid = int(data[1])
    action = data[2]
    if query.from_user.id != uid:
        await query.answer("This menu is not for you!", show_alert=True)
        return

    msg = query.message
    cfg = _get_tmv_cfg(uid)

    if action == "close":
        await query.answer()
        await delete_message(msg, msg.reply_to_message)
        return

    if action == "home":
        await query.answer()
        text_msg, markup = _build_home_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "dumps":
        await query.answer()
        text_msg, markup = _build_dumps_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "add_dump":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "📥 <b>Add Dump Rule(s)</b>\n\n"
            "Send one or more lines in the format: <code>name | chat_id</code>\n"
            "• <code>. | -1001234567890</code> → sends <b>all</b> files\n"
            "• <code>dvd | -1001234567890</code> → sends files with <code>dvd</code> in filename or caption",
        )
        if val is not None:
            dumps = cfg.setdefault("dumps", [])
            added = 0
            for line in val.splitlines():
                if "|" not in line:
                    continue
                name_part, chat_part = line.split("|", 1)
                name_clean = name_part.strip() or "."
                chat_clean = chat_part.strip()
                if chat_clean:
                    dumps.append({"name": name_clean, "chat_id": chat_clean})
                    added += 1
            if added:
                await _save_tmv_cfg(uid)
        text_msg, markup = _build_dumps_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "del_dump" and len(data) > 3:
        idx = int(data[3])
        dumps = cfg.get("dumps", [])
        if 0 <= idx < len(dumps):
            dumps.pop(idx)
            await _save_tmv_cfg(uid)
        await query.answer("Dump rule removed")
        text_msg, markup = _build_dumps_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "clear_dumps":
        cfg["dumps"] = []
        await _save_tmv_cfg(uid)
        await query.answer("All dump rules cleared")
        text_msg, markup = _build_dumps_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "settings":
        await query.answer()
        text_msg, markup = _build_settings_mode_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "mode":
        target_mode = data[3] if len(data) > 3 else "user"
        cfg["settings_mode"] = target_mode
        await _save_tmv_cfg(uid)
        if target_mode == "independent":
            from .users_settings import open_tmv_independent_settings
            await query.answer("Independent mode enabled!")
            await open_tmv_independent_settings(query)
        else:
            await query.answer("User Settings mode enabled for /tmv!", show_alert=True)
            text_msg, markup = _build_settings_mode_ui(uid)
            await edit_message(msg, text_msg, markup)
        return

    if action == "autodomain":
        await query.answer("Discovering active 1TamilMV domain...")
        discovered = await to_thread(discover_active_domain_sync)
        cfg["domain"] = ""
        cfg["active_domain"] = discovered
        await _save_tmv_cfg(uid)
        text_msg, markup = _build_home_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "domain":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "🌐 <b>Set 1TamilMV Domain(s)</b>\n\n"
            "Send one or multiple domains separated by commas (e.g. "
            "<code>https://www.1tamilmv.ceo, https://www.1tamilmv.fi</code>) "
            "or send <code>auto</code> for automatic discovery.",
        )
        if val is not None:
            if val.lower() == "auto":
                cfg["domain"] = ""
                cfg["active_domain"] = await to_thread(discover_active_domain_sync)
            else:
                doms = _parse_domains_list(val)
                cfg["domain"] = ", ".join(doms)
                cfg["active_domain"] = await to_thread(discover_active_domain_sync, cfg["domain"])
            await _save_tmv_cfg(uid)
        text_msg, markup = _build_home_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "toggle":
        cfg["enabled"] = not bool(cfg.get("enabled"))
        if cfg["enabled"]:
            cfg["paused"] = False
            _ensure_monitor(uid)
            await query.answer("1TamilMV monitor started!")
        else:
            _stop_monitor(uid)
            await query.answer("1TamilMV monitor stopped!")
        await _save_tmv_cfg(uid)
        text_msg, markup = _build_home_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "pause":
        cfg["paused"] = True
        await _save_tmv_cfg(uid)
        await query.answer("1TamilMV monitor paused! No monitoring or processing will run.")
        text_msg, markup = _build_home_ui(uid)
        await edit_message(msg, text_msg, markup)
        return

    if action == "resume":
        cfg["paused"] = False
        cfg["enabled"] = True
        await _save_tmv_cfg(uid)
        _ensure_monitor(uid)
        await query.answer("1TamilMV monitor resumed! Checking for releases...")
        _, note = await _run_tmv_cycle(uid, force_notify=True)
        text_msg, markup = _build_home_ui(uid)
        await edit_message(msg, f"{text_msg}\n\n📡 {note}", markup)
        return

    if action == "checknow":
        await query.answer("Checking 1TamilMV for new releases...")
        _, note = await _run_tmv_cycle(uid, force_notify=True)
        text_msg, markup = _build_home_ui(uid)
        await edit_message(msg, f"{text_msg}\n\n📡 {note}", markup)
        return

    ind = cfg["independent"]
    if action == "ind_updest":
        await query.answer()
        text_msg, markup = _build_updest_ui(uid)
        await edit_message(msg, text_msg, markup)
        return
    if action == "set_updest" and len(data) > 3:
        ind["UPLOAD_DEST"] = data[3]
        await _save_tmv_cfg(uid)
        await query.answer(f"Upload Destination set to {data[3]}")
    elif action == "ind_updest_custom":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "📤 <b>Custom Upload Destination</b>\n\nSend destination (e.g., <code>gd</code>, <code>rc</code>, <code>gofile</code>, <code>buzzheavier</code>, or chat/folder ID):",
        )
        if val is not None:
            ind["UPLOAD_DEST"] = val.strip()
            await _save_tmv_cfg(uid)
    elif action == "ind_doc":
        ind["AS_DOCUMENT"] = not bool(ind.get("AS_DOCUMENT"))
        await _save_tmv_cfg(uid)
        await query.answer(f"Upload mode: {'Document' if ind['AS_DOCUMENT'] else 'Media'}")
    elif action == "ind_thumb":
        ind["AUTO_THUMBNAIL"] = not bool(ind.get("AUTO_THUMBNAIL", True))
        await _save_tmv_cfg(uid)
        await query.answer(f"Auto Poster Thumb: {'ON' if ind['AUTO_THUMBNAIL'] else 'OFF'}")
    elif action == "ind_filter":
        order = ["all", "magnet", "direct", "torrent"]
        cur = cfg.get("download_mode", "all")
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "all"
        cfg["download_mode"] = nxt
        await _save_tmv_cfg(uid)
        await query.answer(f"Link Filter: {nxt.upper()}")
    elif action == "ind_dump":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "📥 <b>Set Independent Dump Chat</b>\n\nSend chat ID (e.g. <code>-1001234567890</code>) or <code>none</code> to use default.",
        )
        if val is not None:
            ind["LEECH_DUMP_CHAT"] = "" if val.lower() == "none" else val.strip()
            await _save_tmv_cfg(uid)
    elif action == "ind_interval":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "⏱️ <b>Set Check Interval (seconds)</b>\n\nSend interval in seconds (minimum <code>60</code>, default <code>360</code>).",
        )
        if val is not None and val.isdigit():
            cfg["interval"] = max(60, int(val))
            await _save_tmv_cfg(uid)
    elif action == "ind_prefix":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "✏️ <b>Set Leech Prefix</b>\n\nSend prefix text or <code>none</code> to clear.",
        )
        if val is not None:
            ind["LEECH_PREFIX"] = "" if val.lower() == "none" else val
            await _save_tmv_cfg(uid)
    elif action == "ind_suffix":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "✏️ <b>Set Leech Suffix</b>\n\nSend suffix text or <code>none</code> to clear.",
        )
        if val is not None:
            ind["LEECH_SUFFIX"] = "" if val.lower() == "none" else val
            await _save_tmv_cfg(uid)
    elif action == "ind_caption":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "📝 <b>Set Custom Leech Caption</b>\n\nSend caption template or <code>none</code> to reset.",
        )
        if val is not None:
            ind["LEECH_CAPTION"] = "" if val.lower() == "none" else val
            await _save_tmv_cfg(uid)
    elif action == "ind_dllimit":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "📏 <b>Set Download Size Limit</b>\n\n"
            "Send size like <code>4GB</code>, <code>2048MB</code>, <code>5000000000</code> (bytes), "
            "or <code>-1</code> / <code>unlimited</code> for no limit.\n"
            "Applies to all non-archive monitor releases.",
        )
        if val is not None:
            low = val.strip().lower()
            if low in ("-1", "unlimited", "none", "0"):
                ind["DOWNLOAD_SIZE_LIMIT"] = -1
            else:
                try:
                    from ..helper.ext_utils.bot_utils import get_size_bytes
                    parsed = get_size_bytes(val.strip())
                    if parsed is None or parsed < 0:
                        raise ValueError("bad size")
                    ind["DOWNLOAD_SIZE_LIMIT"] = int(parsed)
                except Exception:
                    await send_message(msg, "❌ Invalid size. Examples: 4GB, 2GiB, 500MB, -1")
                    return
            await _save_tmv_cfg(uid)
    elif action == "ind_ziplimit":
        await query.answer()
        val = await _wait_dm_input(
            client,
            uid,
            msg,
            "📦 <b>Set ZIP / Archive Size Limit</b>\n\n"
            "Send size like <code>4GB</code>, <code>2048MB</code>, or <code>-1</code> / <code>unlimited</code> for no limit.\n"
            "Applies when the release title looks like a zip/rar/7z/tar archive.",
        )
        if val is not None:
            low = val.strip().lower()
            if low in ("-1", "unlimited", "none", "0"):
                ind["ZIP_SIZE_LIMIT"] = -1
            else:
                try:
                    from ..helper.ext_utils.bot_utils import get_size_bytes
                    parsed = get_size_bytes(val.strip())
                    if parsed is None or parsed < 0:
                        raise ValueError("bad size")
                    ind["ZIP_SIZE_LIMIT"] = int(parsed)
                except Exception:
                    await send_message(msg, "❌ Invalid size. Examples: 4GB, 2GiB, 500MB, -1")
                    return
            await _save_tmv_cfg(uid)
    elif action == "ind_reset":
        cfg["independent"] = dict(_default_tmv_settings()["independent"])
        cfg["download_mode"] = "all"
        cfg["interval"] = 360
        await _save_tmv_cfg(uid)
        await query.answer("Independent settings reset!")

    from .users_settings import open_tmv_independent_settings
    await open_tmv_independent_settings(query)
