"""
The cloud version of the watcher.

GitHub's scheduler is unreliable — it silently drops most scheduled runs,
so checks meant for every 5 minutes were arriving hours apart. Instead of
starting a fresh run each time, one run now stays alive for several hours
and checks every couple of minutes from inside. The schedule only has to
restart it occasionally rather than drive every check.

Run it locally for a single pass:  python check_once.py
"""

import os
import sys
import json
import time
import asyncio
import subprocess
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetForumTopicsRequest

from watch_tasks import (API_ID, API_HASH, WATCH, load_state, log, send_alert,
                         send_loose_attachment, has_attachment,
                         _calendar_credentials)

HERE = Path(__file__).parent
SEEN_FILE = HERE / "last_seen.json"
# What the watcher on the laptop has already handled, so we don't repeat it
LOCAL_SEEN_FILE = HERE / "local_seen.json"

# Never fire off more than this many tasks at once (stops a surprise flood
# if the state file is ever lost)
MAX_PER_TOPIC = 3

# How long one run stays alive, and how often it looks, in the cloud
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES") or 0)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL") or 120)
IN_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))


def _read(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_seen() -> dict:
    """Furthest point either watcher has reached, per topic."""
    seen = _read(SEEN_FILE)
    for slot, msg_id in _read(LOCAL_SEEN_FILE).items():
        seen[slot] = max(seen.get(slot, 0), msg_id)
    return seen


def _git(*args) -> int:
    try:
        return subprocess.run(("git",) + args, cwd=str(HERE), timeout=120,
                              capture_output=True).returncode
    except Exception as e:
        log(f"git {args[0]} failed: {e}")
        return 1


def publish_seen(seen: dict) -> None:
    """Save where we got to, and share it so the laptop copy agrees.

    Written after every pass, not just at the end, so a run that is cut
    short doesn't forget and repeat itself.
    """
    SEEN_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")
    if not IN_CLOUD:
        return
    _git("config", "user.name", "task-watcher")
    _git("config", "user.email", "task-watcher@users.noreply.github.com")
    _git("add", "last_seen.json")
    if _git("diff", "--staged", "--quiet") == 0:
        return  # nothing changed
    _git("commit", "-q", "-m", "checked for new tasks")
    _git("pull", "--rebase", "--autostash", "-q")
    if _git("push", "-q") != 0:
        log("Could not publish position — may repeat an alert.")


def refresh_from_laptop(seen: dict) -> dict:
    """Pick up anything the laptop watcher has handled since we last looked."""
    if IN_CLOUD:
        _git("pull", "--rebase", "--autostash", "-q")
    for slot, msg_id in _read(LOCAL_SEEN_FILE).items():
        seen[slot] = max(seen.get(slot, 0), msg_id)
    return seen


async def resolve_topics(client) -> list:
    """Work out which topics to watch. Done once, then reused every pass."""
    groups = {}
    async for dialog in client.iter_dialogs():
        for key in WATCH:
            if (key.lower() in (dialog.name or "").lower()
                    and getattr(dialog.entity, "forum", False)):
                groups.setdefault(key, dialog.entity)

    found = []
    for key, entity in groups.items():
        try:
            topics = await client(GetForumTopicsRequest(
                peer=entity, offset_date=None, offset_id=0,
                offset_topic=0, limit=100))
        except Exception as e:
            log(f'Could not read topics in "{key}" ({e}) — skipping it.')
            continue
        for topic in topics.topics:
            title = getattr(topic, "title", "") or ""
            if any(w.lower() in title.lower() for w in WATCH[key]):
                found.append((key, entity, topic.id, title))
    return found


async def one_pass(client, chat_id: int, topics: list, seen: dict,
                   first_run: bool) -> int:
    """Look at every watched topic once. Returns how many alerts went out."""
    sent = 0
    for key, entity, topic_id, title in topics:
        slot = f"{key}::{title}"
        last_id = seen.get(slot, 0)

        fresh = []
        async for msg in client.iter_messages(entity, limit=20,
                                              reply_to=topic_id):
            if msg.id <= last_id:
                break
            # Keep textless posts too when they carry a file — guidelines
            # are often posted on their own right after the task
            if (msg.message or "").strip() or has_attachment(msg):
                fresh.append(msg)

        if not fresh:
            continue

        newest = max(m.id for m in fresh)
        # On the very first run just record where we are — don't replay the
        # whole backlog into your phone
        if first_run:
            seen[slot] = newest
            log(f"first run — starting from id {newest} in {title}")
            continue

        # Cap how many *tasks* go out at once, not how many messages — a
        # five-image album is five messages but only one task, and counting
        # messages would crowd the task's own text out.
        in_order = sorted(fresh, key=lambda m: m.id)
        task_posts = [m for m in in_order if (m.message or "").strip()]
        keep = {m.id for m in task_posts[-MAX_PER_TOPIC:]}
        floor = min(keep) if keep else 0
        batch = [m for m in in_order
                 if m.id in keep
                 or (not (m.message or "").strip() and m.id >= floor)]

        forwarded = set()
        for msg in batch:
            if (msg.message or "").strip():
                forwarded |= await send_alert(client, chat_id, msg, key, title,
                                              already_sent=forwarded)
                sent += 1
            elif msg.id not in forwarded:
                forwarded.add(msg.id)
                forwarded |= await send_loose_attachment(client, chat_id, msg,
                                                         title)
                sent += 1
            else:
                continue  # already went out with its album
            await asyncio.sleep(1)

        seen[slot] = newest
        log(f"sent {len(keep)} task(s) for {title}")
    return sent


async def main() -> None:
    chat_id = os.environ.get("CHAT_ID") or load_state().get("chat_id")
    if not chat_id:
        log("No CHAT_ID — cannot send alerts.")
        sys.exit(1)
    chat_id = int(chat_id)

    # Report the calendar key's health. A broken key used to fail quietly,
    # dropping events without anyone noticing.
    try:
        log("calendar key: "
            + ("ok" if _calendar_credentials() else "not configured"))
    except Exception as e:
        log(f"calendar key PROBLEM — events will not be saved: {e}")

    session_str = os.environ.get("TG_SESSION")
    if session_str:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    else:  # running on your PC, reuse the normal login
        from watch_tasks import SESSION
        client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()

    topics = await resolve_topics(client)
    if not topics:
        log("No watched topics found.")
        await client.disconnect()
        return
    log(f"watching {len(topics)} topic(s); "
        + (f"checking every {CHECK_INTERVAL}s for {LOOP_MINUTES} min"
           if LOOP_MINUTES else "single pass"))

    seen = load_seen()
    first_run = not seen
    deadline = time.time() + LOOP_MINUTES * 60
    total = 0

    while True:
        try:
            sent = await one_pass(client, chat_id, topics, seen, first_run)
            total += sent
            first_run = False
            publish_seen(seen)
        except Exception as e:
            # One bad pass shouldn't end a run that has hours left
            log(f"check failed ({e}) — trying again next time.")

        if not LOOP_MINUTES or time.time() >= deadline:
            break
        await asyncio.sleep(CHECK_INTERVAL)
        seen = refresh_from_laptop(seen)

    log(f"run complete — {total} alert(s) sent")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
