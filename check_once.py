"""
The cloud version of the watcher.

Instead of listening non-stop (which needs a computer that's always on),
this wakes up, checks each watched topic for anything posted since last
time, sends you those alerts, remembers where it got to, and exits.

GitHub runs it every few minutes, so it works even with your laptop off.
Locally, watch_tasks.py is still the instant-alert version.
"""

import os
import sys
import json
import asyncio
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetForumTopicsRequest

from watch_tasks import (API_ID, API_HASH, WATCH, load_state, log, send_alert)

SEEN_FILE = Path(__file__).with_name("last_seen.json")

# Never fire off more than this many alerts in one run (stops a surprise
# flood if the state file is ever lost)
MAX_PER_TOPIC = 3


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")


async def main() -> None:
    chat_id = os.environ.get("CHAT_ID") or load_state().get("chat_id")
    if not chat_id:
        log("No CHAT_ID — cannot send alerts.")
        sys.exit(1)
    chat_id = int(chat_id)

    session_str = os.environ.get("TG_SESSION")
    if session_str:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    else:  # running on your PC, reuse the normal login
        from watch_tasks import SESSION
        client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()

    seen = load_seen()
    first_run = not seen
    sent = 0

    groups = {}
    async for dialog in client.iter_dialogs():
        for key in WATCH:
            if key.lower() in (dialog.name or "").lower():
                groups.setdefault(key, dialog.entity)

    for key, entity in groups.items():
        topics = await client(GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0,
            offset_topic=0, limit=100))
        for topic in topics.topics:
            title = getattr(topic, "title", "") or ""
            if not any(w.lower() in title.lower() for w in WATCH[key]):
                continue

            slot = f"{key}::{title}"
            last_id = seen.get(slot, 0)

            fresh = []
            async for msg in client.iter_messages(
                    entity, limit=20, reply_to=topic.id):
                if msg.id <= last_id:
                    break
                if (msg.message or "").strip():
                    fresh.append(msg)

            if not fresh:
                continue

            newest = max(m.id for m in fresh)
            # On the very first run just record where we are — don't replay
            # the whole backlog into your phone
            if first_run:
                seen[slot] = newest
                log(f"first run — starting from id {newest} in {title}")
                continue

            for msg in list(reversed(fresh))[-MAX_PER_TOPIC:]:
                await send_alert(client, chat_id, msg, key, title)
                sent += 1
                await asyncio.sleep(1)
            seen[slot] = newest
            log(f"sent {len(fresh)} alert(s) for {title}")

    save_seen(seen)
    log(f"run complete — {sent} alert(s) sent")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
