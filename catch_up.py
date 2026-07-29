"""
One-time catch-up: replays the most recent posts from the watched topics
so you get alerts for things posted before the watcher was running.

Run it with:  python catch_up.py
"""

import asyncio

from telethon import TelegramClient
from telethon.tl.functions.messages import GetForumTopicsRequest

from watch_tasks import (API_ID, API_HASH, SESSION, WATCH, load_state,
                         send_alert)

HOW_MANY = 3  # most recent posts per topic


async def main() -> None:
    chat_id = load_state().get("chat_id")
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()

    groups = {}
    async for dialog in client.iter_dialogs():
        for key in WATCH:
            if key.lower() in (dialog.name or "").lower():
                groups.setdefault(key, dialog.entity)

    for key, entity in groups.items():
        topics = await client(GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=100))
        for topic in topics.topics:
            title = getattr(topic, "title", "") or ""
            if not any(w.lower() in title.lower() for w in WATCH[key]):
                continue
            msgs = [m async for m in client.iter_messages(
                entity, limit=HOW_MANY, reply_to=topic.id)]
            for msg in reversed(msgs):
                if not (msg.message or "").strip():
                    continue
                await send_alert(client, chat_id, msg, key, title,
                                 prefix="🕘 (catching up)\n\n")
                print(f"sent: {title} — {(msg.message or '')[:50]}…")
                await asyncio.sleep(1)

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
