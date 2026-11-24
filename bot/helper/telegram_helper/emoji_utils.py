import re
from contextlib import suppress

from pyrogram import raw

from ... import LOGGER
from ...core.config_manager import Config

EMOJI_MAP: dict[str, str] = {}
PREMIUM_READY: bool = False

_TG_EMOJI_BLOCK_RE = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", re.DOTALL | re.IGNORECASE)
_TG_EMOJI_STRIP_RE = re.compile(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>", re.DOTALL | re.IGNORECASE)


def strip_premium_emojis(text: str) -> str:
    """Strip <tg-emoji> wrappers back to plain Unicode emojis."""
    if not text or "<tg-emoji" not in text:
        return text
    return _TG_EMOJI_STRIP_RE.sub(r"\1", text)


def _extract_pack_short_names(raw_value) -> list[str]:
    if not raw_value:
        return []
    packs = []
    for item in re.split(r"[,\s]+", str(raw_value).strip()):
        item = item.strip()
        if not item:
            continue
        if "addemoji/" in item:
            item = item.split("addemoji/", 1)[1].split("?", 1)[0].strip("/")
        elif "addstickers/" in item:
            item = item.split("addstickers/", 1)[1].split("?", 1)[0].strip("/")
        if item and item not in packs:
            packs.append(item)
    return packs


async def sync_bot_premium_emojis(client) -> dict[str, str]:
    """Automatically sync custom/premium emojis owned or configured for the bot."""
    global EMOJI_MAP, PREMIUM_READY
    if client is None:
        return EMOJI_MAP

    me = getattr(client, "me", None)
    is_prem = bool(getattr(me, "is_premium", False))
    new_map: dict[str, str] = {}

    # 1. Load explicit config mapping if provided
    cfg_map = getattr(Config, "PREMIUM_EMOJIS", None)
    if isinstance(cfg_map, dict):
        for k, v in cfg_map.items():
            if k and v:
                new_map[str(k)] = str(v)

    # 2. Sync custom emoji sticker sets from Config.PREMIUM_EMOJI_PACK and common bot-owned names
    pack_names = _extract_pack_short_names(getattr(Config, "PREMIUM_EMOJI_PACK", ""))
    uname = getattr(me, "username", "") or ""
    if uname and is_prem:
        for candidate in (
            f"{uname}_emojis",
            f"{uname}_emoji",
            f"{uname}_by_{uname}",
        ):
            if candidate not in pack_names:
                pack_names.append(candidate)

    for short_name in pack_names:
        try:
            res = await client.invoke(
                raw.functions.messages.GetStickerSet(
                    stickerset=raw.types.InputStickerSetShortName(short_name=short_name),
                    hash=0,
                )
            )
            docs_by_id = {d.id: d for d in getattr(res, "documents", [])}
            for pack in getattr(res, "packs", []):
                emoticon = getattr(pack, "emoticon", "")
                doc_ids = getattr(pack, "documents", [])
                if emoticon and doc_ids:
                    for did in doc_ids:
                        if did in docs_by_id:
                            new_map.setdefault(emoticon, str(did))
                            break
            if new_map:
                LOGGER.info(
                    "Synced %d Telegram premium emojis from pack '%s'",
                    len(new_map),
                    short_name,
                )
        except Exception:
            pass

    # Only enable <tg-emoji> rendering if the bot is genuinely premium-enabled
    # or has explicit synced custom emoji IDs configured.
    if is_prem or (bool(new_map) and getattr(Config, "USE_PREMIUM_EMOJIS", True)):
        EMOJI_MAP = new_map
        PREMIUM_READY = bool(EMOJI_MAP) and bool(getattr(Config, "USE_PREMIUM_EMOJIS", True))
    else:
        EMOJI_MAP = {}
        PREMIUM_READY = False

    return EMOJI_MAP


def apply_premium_emojis(text: str) -> str:
    """Replace matching Unicode emojis with <tg-emoji> tags when bot owns premium emojis."""
    if not text or not isinstance(text, str):
        return text
    if not PREMIUM_READY or not EMOJI_MAP or not getattr(Config, "USE_PREMIUM_EMOJIS", True):
        return text

    # Split on existing <tg-emoji>...</tg-emoji> and HTML tags so attributes are never corrupted
    parts = re.split(r"(<tg-emoji\b[^>]*>.*?</tg-emoji>|<[^>]+>)", text, flags=re.DOTALL | re.IGNORECASE)
    keys = sorted(EMOJI_MAP.keys(), key=len, reverse=True)
    if not keys:
        return text
    pattern = re.compile("|".join(re.escape(k) for k in keys))

    for i in range(len(parts)):
        seg = parts[i]
        if not seg or seg.startswith("<"):
            continue
        parts[i] = pattern.sub(
            lambda m: f'<tg-emoji emoji-id="{EMOJI_MAP[m.group(0)]}">{m.group(0)}</tg-emoji>',
            seg,
        )
    return "".join(parts)
