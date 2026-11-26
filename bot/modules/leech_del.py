from asyncio import sleep

from ..core.tg_client import TgClient
from ..helper.telegram_helper.message_utils import edit_message, send_message

delete = set()


async def delete_leech(client, message):
    args = message.text.split()
    if len(args) > 1:
        link = args[1]
    elif reply_to := message.reply_to_message:
        link = (reply_to.text or "").strip()
    else:
        link = ""
    if not link.startswith("https://t.me/"):
        return await send_message(
            message,
            "Send a Telegram message link along with the command or reply to a link.",
        )
    parts = link.split("/")
    try:
        message_id = int(parts[-1])
    except (ValueError, IndexError):
        return await send_message(message, "Invalid Telegram message link.")
    if message_id in delete:
        return await send_message(message, "Deletion already in progress for this message.")
    chat_id = parts[-2]
    if chat_id.isdigit():
        chat_id = int(f"-100{chat_id}")
    reply_message = await send_message(message, f"Deleting all replies for {link}...")
    await deleting(client, chat_id, message_id, reply_message)


async def deleting(client, chat_id, message_id, message):
    delete.add(message_id)
    try:
        msg = await client.get_messages(chat_id, message_id, replies=-1)
        replies_ids = []
        while msg:
            await sleep(0)
            replies_ids.append(msg.id)
            if msg.media_group_id:
                media_group = await msg.get_media_group()
                media_ids = []
                for media in media_group:
                    media_ids.append(media.id)
                    msg = media.reply_to_message
                    if not msg and getattr(media, "reply_to_message_id", None):
                        msg = await client.get_messages(
                            chat_id, media.reply_to_message_id, replies=-1
                        )
                replies_ids.extend(media_ids)
            else:
                msg = msg.reply_to_message
        replies_ids = list(set(replies_ids))
        total_ids = len(replies_ids)
        chunks = [replies_ids[i : i + 100] for i in range(0, total_ids, 100)]
        deleted = 0
        for each100 in chunks:
            deleted += await client.delete_messages(chat_id, each100)
            if len(each100) == 100:
                await sleep(1)
            await edit_message(message, f"Deleted {deleted}/{total_ids} messages.")
    except Exception as e:
        await edit_message(message, str(e))
    finally:
        delete.discard(message_id)
