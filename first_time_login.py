"""
Sign in to Telegram twice — once for this laptop, once for the cloud.

They must be SEPARATE logins. Telegram is happy for you to be signed in
on several devices, but it will cancel a login that is used from two
places at the same time, which takes both watchers down at once.

Telegram will send a code for each sign-in. If the second code doesn't
arrive, try the same code again — it is often still valid.
"""

import asyncio
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from watch_tasks import API_ID, API_HASH, SESSION

CLOUD_OUT = Path(__file__).with_name("cloud_session_string.txt")


def clear_old_login() -> None:
    """Remove a cancelled sign-in, otherwise Telethon tries to reuse it and
    fails before it ever offers to sign in again."""
    for suffix in (".session", ".session-journal"):
        old = Path(SESSION + suffix)
        if old.exists():
            try:
                old.unlink()
                print(f"  (cleared the old sign-in: {old.name})")
            except Exception as e:
                print(f"  could not remove {old.name}: {e}")


async def main() -> None:
    print("=" * 62)
    print(" STEP 1 of 2 — sign in for THIS LAPTOP")
    print("=" * 62)
    clear_old_login()
    laptop = TelegramClient(SESSION, API_ID, API_HASH)
    await laptop.start()
    me = await laptop.get_me()
    print(f"\n  Signed in as {me.username or me.first_name}\n")
    await laptop.disconnect()

    print("=" * 62)
    print(" STEP 2 of 2 — a SEPARATE sign-in for the cloud watcher")
    print(" (same phone number; Telegram will send another code)")
    print("=" * 62)
    cloud = TelegramClient(StringSession(), API_ID, API_HASH)
    await cloud.start()
    me = await cloud.get_me()
    string = cloud.session.save()
    CLOUD_OUT.write_text(string, encoding="utf-8")
    await cloud.disconnect()

    print(f"\n  Cloud sign-in created for {me.username or me.first_name}")
    print(f"  Saved to {CLOUD_OUT.name}")
    print("\nAll done — tell Claude it's finished.\n")


if __name__ == "__main__":
    asyncio.run(main())
