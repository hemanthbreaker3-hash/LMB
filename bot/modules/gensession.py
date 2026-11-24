from asyncio import Event, TimeoutError as AsyncTimeout, wait_for
from contextlib import suppress

from cbgram import Client, __version__ as cbgram_version
from pyrogram.enums import ChatType
from pyrogram.errors import (
    ApiIdInvalid,
    FloodWait,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)
from pyrogram.filters import create, private, text, user
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from ...core.config_manager import Config
from ...core.tg_client import TgClient
from ..ext_utils.bot_utils import new_task
from ..ext_utils.status_utils import get_readable_time
from ..telegram_helper.button_build import ButtonMaker
from ..telegram_helper.message_utils import delete_message, edit_message, send_message

_STOP = "gensess_cmd_stop"
_TIMEOUT = 180


def _stop_btns():
    btns = ButtonMaker()
    btns.data_button("❌ Cancel", data=_STOP)
    return btns.build_menu(1)


def _stop_filter(uid):
    async def _check(_, __, update):
        return update.data == _STOP and update.from_user.id == uid

    return create(_check)


async def _safe_disconnect(client):
    with suppress(Exception):
        await client.disconnect()


async def _ask_input(client, message, prompt_msg, uid, prompt_text, cancel_event):
    await edit_message(prompt_msg, prompt_text, _stop_btns())
    got = {"msg": None}
    evt = Event()

    async def _on_msg(_, msg):
        if msg.chat.id == message.chat.id and msg.from_user and msg.from_user.id == uid:
            got["msg"] = msg
            evt.set()

    async def _on_stop(_, query):
        await query.answer("Session generation cancelled.", show_alert=False)
        cancel_event.set()
        evt.set()

    msg_h = client.add_handler(
        MessageHandler(_on_msg, filters=text & user(uid) & private), group=-1
    )
    cb_h = client.add_handler(
        CallbackQueryHandler(_on_stop, filters=_stop_filter(uid)), group=-1
    )
    try:
        await wait_for(evt.wait(), timeout=_TIMEOUT)
    except AsyncTimeout:
        return None, "timeout"
    finally:
        with suppress(Exception):
            client.remove_handler(*msg_h)
            client.remove_handler(*cb_h)

    if cancel_event.is_set() or got["msg"] is None:
        return None, "cancelled"

    val = (got["msg"].text or "").strip()
    with suppress(Exception):
        await delete_message(got["msg"])
    return val, None


@new_task
async def gen_session_cmd(client, message):
    if message.chat.type != ChatType.PRIVATE:
        btns = ButtonMaker()
        btns.url_button("🔐 Open Bot DM", f"https://t.me/{TgClient.BNAME}?start=gensession")
        await send_message(
            message,
            "⚠️ <b>Security Notice:</b> Please run <code>/gensession</code> in my private chat so your API credentials, OTP, and session string stay private.",
            btns.build_menu(1),
        )
        return

    uid = message.from_user.id
    uname = message.from_user.first_name or "Admin"
    cancel_event = Event()

    status_msg = await send_message(
        message,
        f"🔐 <b>cbgram Session Generator</b>\n\n"
        f"👤 <b>Admin:</b> {uname}\n"
        f"⚙️ <b>Engine:</b> <code>cbgram v{cbgram_version}</code>\n\n"
        f"Initializing...",
        _stop_btns(),
    )

    # Step 1: API_ID
    while True:
        raw_id, err = await _ask_input(
            client,
            message,
            status_msg,
            uid,
            "🔐 <b>cbgram Session Generator (Step 1/4)</b>\n\n"
            "📥 Please send your <b>API ID</b> (numeric value from my.telegram.org).\n"
            f"⏱️ <i>Timeout: {get_readable_time(_TIMEOUT)}</i>",
            cancel_event,
        )
        if err:
            await edit_message(
                status_msg,
                "⏱️ Session generation timed out." if err == "timeout" else "❌ Session generation cancelled.",
            )
            return
        if raw_id.isdigit() and int(raw_id) > 0:
            api_id = int(raw_id)
            break
        await edit_message(
            status_msg,
            "⚠️ Invalid <b>API ID</b>. Please send a valid positive integer.",
            _stop_btns(),
        )

    # Step 2: API_HASH
    api_hash, err = await _ask_input(
        client,
        message,
        status_msg,
        uid,
        "🔐 <b>cbgram Session Generator (Step 2/4)</b>\n\n"
        f"✅ <b>API ID:</b> <code>{api_id}</code>\n\n"
        "📥 Please send your <b>API Hash</b>.\n"
        f"⏱️ <i>Timeout: {get_readable_time(_TIMEOUT)}</i>",
        cancel_event,
    )
    if err or not api_hash:
        await edit_message(
            status_msg,
            "⏱️ Session generation timed out." if err == "timeout" else "❌ Session generation cancelled.",
        )
        return

    # Step 3: Phone Number with country code
    while True:
        phone, err = await _ask_input(
            client,
            message,
            status_msg,
            uid,
            "🔐 <b>cbgram Session Generator (Step 3/4)</b>\n\n"
            f"✅ <b>API ID:</b> <code>{api_id}</code>\n"
            f"✅ <b>API Hash:</b> <code>{api_hash[:4]}****</code>\n\n"
            "📱 Please send your <b>mobile number with country code</b> (for example: <code>+91xxxxxxxxxx</code>).\n"
            f"⏱️ <i>Timeout: {get_readable_time(_TIMEOUT)}</i>",
            cancel_event,
        )
        if err:
            await edit_message(
                status_msg,
                "⏱️ Session generation timed out." if err == "timeout" else "❌ Session generation cancelled.",
            )
            return
        cleaned_phone = phone.replace(" ", "")
        if cleaned_phone.startswith("+") and cleaned_phone[1:].isdigit() and len(cleaned_phone) >= 8:
            phone = cleaned_phone
            break
        await edit_message(
            status_msg,
            "⚠️ Invalid phone format. Include the country code starting with <code>+</code> (for example: <code>+91xxxxxxxxxx</code>).",
            _stop_btns(),
        )

    temp_client = Client(
        name=f"cbgram-gen-{uid}",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    )

    try:
        await edit_message(status_msg, "⏳ Connecting to Telegram and requesting OTP...", _stop_btns())
        await temp_client.connect()
        sent_code = await temp_client.send_code(phone)
    except ApiIdInvalid:
        await _safe_disconnect(temp_client)
        await edit_message(status_msg, "❌ <b>Error:</b> Invalid API ID or API Hash.")
        return
    except PhoneNumberInvalid:
        await _safe_disconnect(temp_client)
        await edit_message(status_msg, "❌ <b>Error:</b> Invalid phone number.")
        return
    except FloodWait as fw:
        await _safe_disconnect(temp_client)
        await edit_message(
            status_msg,
            f"⚠️ <b>FloodWait:</b> Telegram requested a {get_readable_time(fw.value)} cooldown. Please try again later.",
        )
        return
    except Exception as exc:
        await _safe_disconnect(temp_client)
        await edit_message(status_msg, f"❌ <b>Connection Failed:</b> <code>{exc}</code>")
        return

    # Step 4: OTP
    otp_code, err = await _ask_input(
        client,
        message,
        status_msg,
        uid,
        "🔐 <b>cbgram Session Generator (Step 4/4)</b>\n\n"
        f"📱 <b>Phone:</b> <code>{phone}</code>\n\n"
        "📩 Please enter the <b>OTP</b> sent to your Telegram app (spaces optional, e.g., <code>12345</code> or <code>1 2 3 4 5</code>).\n"
        f"⏱️ <i>Timeout: {get_readable_time(_TIMEOUT)}</i>",
        cancel_event,
    )
    if err or not otp_code:
        await _safe_disconnect(temp_client)
        await edit_message(
            status_msg,
            "⏱️ OTP entry timed out." if err == "timeout" else "❌ Session generation cancelled.",
        )
        return
    otp_code = otp_code.replace(" ", "")

    # Sign in (and handle optional 2FA)
    try:
        await temp_client.sign_in(phone, sent_code.phone_code_hash, otp_code)
    except SessionPasswordNeeded:
        password, err = await _ask_input(
            client,
            message,
            status_msg,
            uid,
            "🔒 <b>Two-Step Verification Required</b>\n\n"
            "🔑 Please enter your <b>2FA password</b>.\n"
            f"⏱️ <i>Timeout: {get_readable_time(_TIMEOUT)}</i>",
            cancel_event,
        )
        if err or not password:
            await _safe_disconnect(temp_client)
            await edit_message(
                status_msg,
                "⏱️ 2FA entry timed out." if err == "timeout" else "❌ Session generation cancelled.",
            )
            return
        try:
            await temp_client.check_password(password)
        except Exception as exc:
            await _safe_disconnect(temp_client)
            await edit_message(status_msg, f"❌ <b>Invalid 2FA Password:</b> <code>{exc}</code>")
            return
    except PhoneCodeInvalid:
        await _safe_disconnect(temp_client)
        await edit_message(status_msg, "❌ <b>Error:</b> The OTP you entered is invalid.")
        return
    except PhoneCodeExpired:
        await _safe_disconnect(temp_client)
        await edit_message(status_msg, "❌ <b>Error:</b> The OTP has expired. Please run the command again.")
        return
    except Exception as exc:
        await _safe_disconnect(temp_client)
        await edit_message(status_msg, f"❌ <b>Sign-In Failed:</b> <code>{exc}</code>")
        return

    try:
        session_string = await temp_client.export_session_string()
        me = await temp_client.get_me()
    except Exception as exc:
        await _safe_disconnect(temp_client)
        await edit_message(status_msg, f"❌ <b>Failed to export session:</b> <code>{exc}</code>")
        return
    finally:
        await _safe_disconnect(temp_client)

    acc_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "User"
    acc_user = f"@{me.username}" if getattr(me, "username", None) else "None"
    is_prem = "Yes" if getattr(me, "is_premium", False) else "No"

    await edit_message(
        status_msg,
        "✅ <b>cbgram Session String Generated!</b>\n\n"
        f"👤 <b>Account:</b> {acc_name} ({acc_user})\n"
        f"💎 <b>Telegram Premium:</b> {is_prem}\n"
        f"⚙️ <b>Engine:</b> <code>cbgram v{cbgram_version}</code>\n\n"
        f"<code>{session_string}</code>\n\n"
        "⚠️ <i>Keep this session string private. Do not share it with anyone.</i>",
    )
