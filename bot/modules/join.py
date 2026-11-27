import re
from html import escape

from pyrogram.errors import (
    ChannelPrivate,
    FloodWait,
    InviteHashExpired,
    InviteHashInvalid,
    InviteRequestSent,
    UserAlreadyParticipant,
    UsernameInvalid,
    UsernameNotOccupied,
)

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from .. import LOGGER
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import edit_message, send_message

_INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog|telegram\.space)/(\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_PUBLIC_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog|telegram\.space)/(?!c/)([A-Za-z][A-Za-z0-9_]{3,})(?:/\d+)?",
    re.IGNORECASE,
)
_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,})")
_PRIVATE_MSG_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog|telegram\.space)/c/\d+",
    re.IGNORECASE,
)


def _normalize_target(raw: str):
    if not raw:
        return None
    text = raw.strip()
    if m := _INVITE_RE.search(text):
        return f"https://t.me/{m.group(1)}"
    if _PRIVATE_MSG_LINK_RE.search(text):
        return "PRIVATE_MSG_LINK"
    if m := _PUBLIC_LINK_RE.search(text):
        return f"@{m.group(1)}"
    if m := _USERNAME_RE.search(text):
        return f"@{m.group(1)}"
    token = text.split()[0].strip(".,)]}>\"'")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", token):
        return f"@{token}"
    return None


def _extract_join_target(message):
    text = (message.text or message.caption or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        if target := _normalize_target(parts[1]):
            return target

    reply = getattr(message, "reply_to_message", None)
    if reply:
        for attr_ent, attr_txt in (("entities", "text"), ("caption_entities", "caption")):
            r_text = getattr(reply, attr_txt, None) or ""
            for ent in getattr(reply, attr_ent, None) or []:
                url = getattr(ent, "url", None)
                if url:
                    if target := _normalize_target(url):
                        return target
                etype = str(getattr(ent, "type", "")).lower()
                if "url" in etype or "mention" in etype:
                    seg = r_text[ent.offset : ent.offset + ent.length]
                    if target := _normalize_target(seg):
                        return target
            if r_text:
                if target := _normalize_target(r_text):
                    return target
    return None


@new_task
async def join_chat(_, message):
    cmd = BotCommands.JoinCommand
    if isinstance(cmd, list):
        cmd = cmd[0]

    target = _extract_join_target(message)
    if not target:
        await send_message(
            message,
            f"<b>Usage:</b> <code>/{escape(str(cmd))} &lt;invite_link | @username&gt;</code>\n"
            "<blockquote>Send or reply to a Telegram invite link (<code>t.me/+...</code>, <code>t.me/joinchat/...</code>), "
            "request link, or public channel/group link to join with the Bot Settings user session.</blockquote>",
        )
        return

    if target == "PRIVATE_MSG_LINK":
        await send_message(
            message,
            "<b>❌ Private message links (<code>t.me/c/...</code>) cannot be joined directly.</b>\n"
            "<blockquote>Provide an invite link (<code>t.me/+...</code>) or join-request link instead.</blockquote>",
        )
        return

    user_client = await TgClient.ensure_user_client()
    if user_client is None:
        await send_message(
            message,
            "<b>❌ Bot Settings <code>USER_SESSION_STRING</code> is not configured or unavailable.</b>\n"
            "<blockquote>Configure a valid <code>USER_SESSION_STRING</code> in Bot Settings first.</blockquote>",
        )
        return

    status_msg = await send_message(
        message,
        f"<b>⏳ Joining</b> <code>{escape(target)}</code> <b>with Bot Settings user session...</b>",
    )

    me = getattr(user_client, "me", None)
    acc_label = (
        f"@{me.username}"
        if me and getattr(me, "username", None)
        else (getattr(me, "first_name", None) or "Bot User Session")
    )

    try:
        chat = await user_client.join_chat(target)
        title = escape(getattr(chat, "title", None) or getattr(chat, "first_name", None) or target)
        chat_id = getattr(chat, "id", "Unknown")
        uname = getattr(chat, "username", None)
        extra = f"\n• <b>Username:</b> @{escape(uname)}" if uname else ""
        await edit_message(
            status_msg,
            f"<b>✅ Joined Successfully</b>\n\n"
            f"<blockquote>• <b>Chat:</b> {title}\n"
            f"• <b>Chat ID:</b> <code>{chat_id}</code>{extra}\n"
            f"• <b>Account:</b> {escape(acc_label)}</blockquote>",
        )
    except UserAlreadyParticipant:
        chat_info = ""
        try:
            chat = await user_client.get_chat(target)
            title = escape(getattr(chat, "title", None) or target)
            chat_id = getattr(chat, "id", "")
            chat_info = f"\n• <b>Chat:</b> {title}\n• <b>Chat ID:</b> <code>{chat_id}</code>"
        except Exception:
            pass
        await edit_message(
            status_msg,
            f"<b>ℹ️ Already a Member</b>\n\n"
            f"<blockquote>• <b>Target:</b> <code>{escape(target)}</code>{chat_info}\n"
            f"• <b>Account:</b> {escape(acc_label)}</blockquote>",
        )
    except InviteRequestSent:
        await edit_message(
            status_msg,
            f"<b>📩 Join Request Sent</b>\n\n"
            f"<blockquote>• <b>Target:</b> <code>{escape(target)}</code>\n"
            f"• <b>Account:</b> {escape(acc_label)}\n"
            f"• <b>Status:</b> Waiting for channel/group admin approval.</blockquote>",
        )
    except (InviteHashExpired, InviteHashInvalid):
        await edit_message(
            status_msg,
            f"<b>❌ Invalid or Expired Invite Link</b>\n\n"
            f"<blockquote><code>{escape(target)}</code></blockquote>",
        )
    except (ChannelPrivate, UsernameInvalid, UsernameNotOccupied) as e:
        await edit_message(
            status_msg,
            f"<b>❌ Cannot Join Target</b>\n\n"
            f"<blockquote><code>{escape(str(e))}</code></blockquote>",
        )
    except (FloodWait, FloodPremiumWait) as e:
        wait_s = getattr(e, "value", 0)
        await edit_message(
            status_msg,
            f"<b>⏳ FloodWait:</b> Please retry after <code>{wait_s}s</code>.",
        )
    except Exception as e:
        LOGGER.error(f"Join failed for {target}: {e}")
        await edit_message(
            status_msg,
            f"<b>❌ Join Failed</b>\n\n<blockquote><code>{escape(str(e))}</code></blockquote>",
        )
