"""
Telegram task watcher.

Watches chosen topics in your Telegram groups and pings you (via your
Task Alert bot) the moment something new is posted — so you don't have
to keep checking Telegram.

Each alert includes:
  - the important lines of the task (reward, deadline, links)
  - a "tap to compose" Twitter link pre-filled with the task's required
    link and hashtags (you write your own words around them)
  - a quick preview of the linked report so you can skim it fast

It is read-only on Telegram: it never posts, replies, or claims anything.
"""

import os
import re
import html
import json
import socket
import asyncio
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests
from telethon import TelegramClient, events, utils
from telethon.tl.functions.messages import GetForumTopicsRequest

# ----------------------- SETTINGS -----------------------

# Your keys live in settings_local.py (kept on this PC, never uploaded).
# In the cloud they arrive as secure settings instead.
try:
    import settings_local as LOCAL
except ImportError:
    LOCAL = None


def _setting(name: str, default: str = "") -> str:
    return (os.environ.get(name)
            or (getattr(LOCAL, name, "") if LOCAL else "")
            or default)


API_ID = int(_setting("TG_API_ID") or 0)
API_HASH = _setting("TG_API_HASH")
BOT_TOKEN = _setting("BOT_TOKEN")

# Which groups and topics to watch.
# Group names can be partial (helpful when titles are long) — topic
# names should match what Telegram shows.
WATCH = {
    "Bitget Builders": [
        "Quick UGC Tasks",
        "Bitget Global Post to Earn Task",
    ],
    "PIC Bitget SEA Community": [
        "PIC Tasks",
    ],
    "BGB Holders Community SEA": [
        "Activities",
    ],
}

# Topics that announce events (not posting tasks). These get an
# "add to calendar" link instead of post-drafting help.
EVENT_TOPICS = {"Activities"}

# Times without a zone are assumed Philippine time
LOCAL_UTC_OFFSET = 8

# Which Google Calendar to write events into (your Google account's email)
CALENDAR_ID = _setting("CALENDAR_ID")

# --------------------------------------------------------

STATE_FILE = Path(__file__).with_name("state.json")
SESSION = str(Path(__file__).with_name("my_session"))
TEMP_DIR = Path(__file__).with_name("images")
TEMP_DIR.mkdir(exist_ok=True)
LOG_FILE = Path(__file__).with_name("status.log")


def log(msg: str) -> None:
    """Record what the watcher is doing (it runs with no window)."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


URL_RE = r"https?://[^\s)>\]]+"

# Links that are forms/submissions, not content worth previewing
SKIP_PREVIEW = ("forms.gle", "docs.google.com/forms", "tinyurl.com", "t.me/",
                "twitter.com", "x.com", "facebook.com")


MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
DATE_RE = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) +
    r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", re.I)
TIME_AMPM_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.I)
TIME_24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
ZONE_RE = re.compile(r"\b(PHT|PHST|SGT|UTC|GMT)\b", re.I)
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF←-⇿⌀-➿⬀-⯿️]+")


def clean_title(line: str) -> str:
    return EMOJI_RE.sub("", line).strip(" -–—:•|!").strip()


def parse_event_time(text: str) -> datetime | None:
    """Find the event's start time and return it in UTC."""
    date_m = DATE_RE.search(text)
    if not date_m:
        return None
    month = MONTHS[date_m.group(1).lower()]
    day = int(date_m.group(2))
    year = int(date_m.group(3)) if date_m.group(3) else datetime.now().year

    # Prefer a time stated after the date, else anywhere in the message
    tail = text[date_m.end():]
    t = TIME_AMPM_RE.search(tail) or TIME_AMPM_RE.search(text)
    if t:
        hour = int(t.group(1)) % 12
        minute = int(t.group(2) or 0)
        if t.group(3).lower() == "p":
            hour += 12
        zone_src = tail
    else:
        t = TIME_24_RE.search(tail) or TIME_24_RE.search(text)
        if not t:
            return None
        hour, minute = int(t.group(1)), int(t.group(2))
        zone_src = tail

    zone_m = ZONE_RE.search(zone_src) or ZONE_RE.search(text)
    zone = (zone_m.group(1).upper() if zone_m else "")
    offset = 0 if zone in ("UTC", "GMT") else LOCAL_UTC_OFFSET

    try:
        local = datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    start_utc = local - timedelta(hours=offset)
    # A date with no year that already passed almost certainly means next year
    if not date_m.group(3):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if start_utc < now - timedelta(days=30):
            start_utc = start_utc.replace(year=start_utc.year + 1)
    return start_utc


def create_calendar_event(title: str, start_utc: datetime, details: str = "",
                          location: str = "", hours: int = 2) -> bool:
    """Write the event straight into Google Calendar.

    Needs google_calendar_key.json (a service account key) sitting next to
    this script, and your calendar shared with that key's email address.
    Returns False if that isn't set up — the alert then falls back to a
    one-tap 'add to calendar' link.
    """
    key_file = Path(__file__).with_name("google_calendar_key.json")
    if not key_file.exists():
        # In the cloud the key arrives as a secure setting instead of a file
        env_key = os.environ.get("GOOGLE_KEY")
        if not env_key:
            return False
        key_file.write_text(env_key, encoding="utf-8")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build as gbuild

        creds = service_account.Credentials.from_service_account_file(
            str(key_file), scopes=["https://www.googleapis.com/auth/calendar"])
        service = gbuild("calendar", "v3", credentials=creds,
                         cache_discovery=False)
        body = {
            "summary": title[:200],
            "description": details[:2000],
            "location": location[:200],
            "start": {"dateTime": start_utc.isoformat() + "Z"},
            "end": {"dateTime": (start_utc + timedelta(hours=hours)).isoformat()
                    + "Z"},
            "reminders": {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 10},
            ]},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
        return True
    except Exception as e:
        log(f"Calendar add failed ({e}) — falling back to a tap link.")
        return False


def calendar_link(title: str, start_utc: datetime, details: str = "",
                  location: str = "", hours: int = 2) -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    end = start_utc + timedelta(hours=hours)
    params = {
        "action": "TEMPLATE",
        "text": title[:200],
        "dates": f"{start_utc.strftime(fmt)}/{end.strftime(fmt)}",
        "details": details[:800],
        "location": location[:200],
    }
    return ("https://calendar.google.com/calendar/render?"
            + urllib.parse.urlencode(params))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def split_for_telegram(text: str, limit: int = 3800) -> list:
    """Telegram caps one message at ~4096 characters — split on line breaks."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:            # a single monster line
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks


def notify_photo(chat_id: int, path: str, caption: str = "") -> bool:
    """Send an image the task came with."""
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"photo": f}, timeout=120,
            )
        return bool(r.json().get("ok"))
    except Exception as e:
        log(f"Could not send image: {e}")
        return False


def notify(chat_id: int, text: str, use_html: bool = False) -> None:
    for part in split_for_telegram(text):
        _notify_one(chat_id, part, use_html)


def _notify_one(chat_id: int, text: str, use_html: bool = False) -> None:
    payload = {"chat_id": chat_id, "text": text,
               "disable_web_page_preview": True}
    if use_html:
        payload["parse_mode"] = "HTML"
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json=payload, timeout=30,
    )
    # If Telegram rejects the HTML formatting, resend as plain text
    if use_html and not r.json().get("ok"):
        payload.pop("parse_mode", None)
        payload["text"] = re.sub(r"</?[a-z][^>]*>", "", payload["text"])
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=30,
        )


def summarize(text: str) -> str:
    """Pull out the lines that matter so the alert is readable at a glance."""
    important = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(
            r"reward|deadline|slots|platform|task\b|form|submit|due|https?://"
            r"|prize|pool|host|join|when\b|where\b|venue|starts?\b"
            r"|\d{1,2}\s*:\s*\d{2}|\bpm\b|\bam\b",
            stripped,
            re.IGNORECASE,
        ):
            important.append(stripped)
    summary = "\n".join(important[:12]) if important else text[:600]
    return summary[:1500]


def find_required_link(text: str) -> str | None:
    """The link the task says you must include in your post."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"include (this )?link|press release|must include|\blink\s*:|\bshare\b", line, re.I):
            for j in range(i, min(i + 3, len(lines))):
                m = re.search(URL_RE, lines[j])
                if m:
                    return m.group(0)
    return None


def compose_link(text: str) -> str | None:
    """Twitter compose window pre-filled with the task's required pieces."""
    parts = []
    required = find_required_link(text)
    if required:
        parts.append(required)
    hashtags = list(dict.fromkeys(re.findall(r"#[A-Za-z]\w+", text)))
    if hashtags:
        parts.append(" ".join(hashtags[:6]))
    if not parts:
        return None
    prefill = "\n\n" + "\n".join(parts)
    return "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(prefill)


def categorize_links(text: str) -> dict:
    """Sort every link in the task into: required-in-post, submit, read-first."""
    cats = {"required": [], "submit": [], "read": []}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for m in re.finditer(URL_RE, line):
            url = m.group(0)
            context = " ".join(lines[max(0, i - 1): i + 1]).lower()
            if ("forms.gle" in url.lower()
                    or "docs.google.com/forms" in url.lower()
                    or re.search(r"submit|submission|proof\b|form\b", context)):
                cats["submit"].append(url)
            elif re.search(r"include (this )?link|press release|must include|\blink\s*:|\bshare\b", context):
                cats["required"].append(url)
            else:
                cats["read"].append(url)
    for k in cats:
        cats[k] = list(dict.fromkeys(cats[k]))
    cats["read"] = [u for u in cats["read"]
                    if u not in cats["required"] and u not in cats["submit"]]
    return cats


def platform_of(text: str) -> str:
    """Which platform the task wants you to post on."""
    m = re.search(r"(?:promotion\s+)?platform\s*:?\s*([^\n]+)", text, re.I)
    if m:
        line = m.group(1).lower()
        if "facebook" in line or re.search(r"\bfb\b", line):
            return "facebook"
        if "twitter" in line or re.search(r"\bx\b", line):
            return "x"
    low = text.lower()
    fb = low.count("facebook") + len(re.findall(r"\bfb\b", low))
    tw = (low.count("twitter") + low.count("x.com")
          + len(re.findall(r"\bx\b", low)))
    return "facebook" if fb > tw else "x"


# Where to go and post, per platform
PLATFORM_OPEN = {
    "x": ("X (Twitter)", "https://x.com/compose/post"),
    "facebook": ("Facebook", "https://www.facebook.com/"),
}


def pick_report_link(text: str) -> str | None:
    """The link most likely to be the report/brief to read."""
    lines = text.splitlines()
    candidates = []
    for i, line in enumerate(lines):
        for m in re.finditer(URL_RE, line):
            url = m.group(0)
            if any(s in url.lower() for s in SKIP_PREVIEW):
                continue
            context = " ".join(lines[max(0, i - 1): i + 1]).lower()
            score = 1
            if re.search(r"report|brief|article|read|research|insight", context):
                score = 2
            candidates.append((score, url))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    return candidates[0][1]


def fetch_doc_brief(url: str) -> str | None:
    """Public Google Doc briefs: pull the opening text and any links inside."""
    m = re.search(r"docs\.google\.com/document/d/([\w-]+)", url)
    if not m:
        return None
    export = f"https://docs.google.com/document/d/{m.group(1)}/export?format=txt"
    try:
        r = requests.get(export, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or "<html" in r.text[:200].lower():
            return None
        doc = re.sub(r"\r\n?", "\n", r.text).strip()
        if len(doc) < 40:
            return None
        links = list(dict.fromkeys(re.findall(URL_RE, doc)))
        # The caption/tracking links matter most; forms usually appear elsewhere
        links = [u for u in links
                 if "forms.gle" not in u and "docs.google.com" not in u][:4]
        snippet = re.sub(r"\n{3,}", "\n\n", doc)[:700]
        out = f"📄 Inside the brief:\n{snippet}…"
        if links:
            out += "\n\n🔗 Links found in the brief:\n" + "\n".join(links)
        return out
    except Exception:
        return None


def fetch_preview(url: str) -> str | None:
    """Title + opening content of a web page, as a quick skim."""
    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code >= 400:
            return None
        page = r.text
        if re.search(r"access denied|forbidden|are you a robot|captcha",
                     page[:3000], re.I):
            return None
        title_m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        title = html.unescape(title_m.group(1).strip()) if title_m else url
        # The page's own summary (written for search engines) is usually
        # the cleanest description of the article
        desc_m = re.search(
            r'<meta[^>]+(?:name="description"|property="og:description")'
            r'[^>]+content="([^"]{40,})"',
            page, re.I,
        ) or re.search(
            r'<meta[^>]+content="([^"]{40,})"[^>]+'
            r'(?:name="description"|property="og:description")',
            page, re.I,
        )
        if desc_m:
            return f"{title}\n{html.unescape(desc_m.group(1))[:600]}"
        body = re.sub(r"(?is)<(script|style|nav|header|footer|svg)[^>]*>.*?</\1>",
                      " ", page)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = html.unescape(re.sub(r"\s+", " ", body)).strip()
        if len(body) < 100:
            return title
        # Keep real sentences, skip menu/price-ticker clutter
        sentences = re.findall(r"[A-Z][^.!?]{30,300}[.!?]", body)
        good = []
        for s in sentences:
            digits = sum(c.isdigit() for c in s)
            if digits / len(s) < 0.15 and len(s.split()) >= 8:
                good.append(s.strip())
            if len(good) == 4:
                break
        content = " ".join(good) if good else body
        return f"{title}\n{content[:600]}…"
    except Exception:
        return None


def message_text(message) -> str:
    """Message text, including URLs hidden behind clickable words."""
    text = message.message or "(no text — probably an image or file)"
    try:
        for ent, ent_text in message.get_entities_text():
            url = getattr(ent, "url", None)
            if url and url not in text:
                text += f"\n{ent_text}: {url}"
    except Exception:
        pass
    return text


async def build_alert(text: str, group_key: str, topic_title: str) -> str:
    """The message your bot sends you for one post."""
    is_event = any(e.lower() in topic_title.lower() for e in EVENT_TOPICS)

    header = f"🚨 New post in {topic_title} ({group_key})"
    if is_event:
        header = f"📅 New activity in {topic_title} ({group_key})"
    if re.search(r"closed", text, re.IGNORECASE):
        header = f"ℹ️ {topic_title} update (looks like a closure notice)"

    blocks = [f"<b>{html.escape(header)}</b>", html.escape(text)]

    if is_event:
        first_line = next((l for l in text.splitlines() if l.strip()), "")
        title = clean_title(first_line) or f"Activity — {group_key}"
        start = parse_event_time(text)
        join = next((u for u in re.findall(URL_RE, text)), "")
        if start:
            shown = (start + timedelta(hours=LOCAL_UTC_OFFSET)).strftime(
                "%a %d %b %Y, %I:%M %p")
            added = await asyncio.to_thread(
                create_calendar_event, title, start, text, join)
            if added:
                blocks.append(f"✅ Added to your Google Calendar\n{shown} (PHT)")
            else:
                cal = calendar_link(title, start, details=text, location=join)
                blocks.append(
                    f'🗓 <a href="{html.escape(cal)}">Add to Google Calendar</a>\n'
                    f"Detected: {shown} (PHT) — check it before saving"
                )
        else:
            blocks.append("🗓 No clear date/time found — add it manually if "
                          "this is an event.")
        if join:
            blocks.append(f"🔗 Join link: {html.escape(join)}")
        return "\n\n".join(blocks)

    links = categorize_links(text)
    platform = platform_of(text)
    name, open_url = PLATFORM_OPEN[platform]

    link_lines = [f'📱 Post on {name}: {open_url}']
    if platform == "facebook":
        if links["required"]:
            share_url = ("https://www.facebook.com/sharer/sharer.php?u="
                         + urllib.parse.quote(links["required"][0]))
            link_lines.append(
                f'📝 <a href="{html.escape(share_url)}">Share it on Facebook</a> '
                "(required link pre-filled — add your own words)")
    else:
        compose = compose_link(text)
        if compose:
            link_lines.append(
                f'📝 <a href="{html.escape(compose)}">Open X composer</a> '
                "(required link + hashtags pre-filled — add your own words)")
    for u in links["required"]:
        link_lines.append(f"✅ Must include in your post: {html.escape(u)}")
    for u in links["read"]:
        link_lines.append(f"📖 Read before writing: {html.escape(u)}")
    for u in links["submit"]:
        link_lines.append(f"📋 Submit here after posting: {html.escape(u)}")
    if link_lines:
        blocks.append("🔗 Key links:\n" + "\n".join(link_lines))

    # Only preview genuine "read this first" material, not the press release
    report = next((u for u in links["read"]
                   if not any(s in u.lower() for s in SKIP_PREVIEW)), None)
    if report:
        if "docs.google.com/document" in report:
            preview = await asyncio.to_thread(fetch_doc_brief, report)
        else:
            preview = await asyncio.to_thread(fetch_preview, report)
            if preview:
                preview = "📖 Quick skim of the report:\n" + preview
        if preview:
            blocks.append(html.escape(preview))

    return "\n\n".join(blocks)


LOCAL_SEEN_FILE = Path(__file__).with_name("local_seen.json")


def record_handled(group_key: str, topic_title: str, msg_id: int) -> None:
    """Tell the cloud copy this post is already dealt with.

    Both watchers run at once — this one instantly, the GitHub one every few
    minutes. Without this you'd get every alert twice and duplicate calendar
    entries. We publish our position to a file of our own, so the two never
    fight over the same file.
    """
    import subprocess
    try:
        seen = {}
        if LOCAL_SEEN_FILE.exists():
            seen = json.loads(LOCAL_SEEN_FILE.read_text(encoding="utf-8"))
        slot = f"{group_key}::{topic_title}"
        if msg_id <= seen.get(slot, 0):
            return
        seen[slot] = msg_id
        LOCAL_SEEN_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")

        here = str(Path(__file__).parent)

        def run(*args):
            return subprocess.run(args, cwd=here, timeout=90,
                                  capture_output=True)

        run("git", "pull", "--rebase", "--autostash", "-q")
        run("git", "add", "local_seen.json")
        run("git", "-c", "user.name=task-watcher",
            "-c", "user.email=task-watcher@users.noreply.github.com",
            "commit", "-q", "-m", "handled locally")
        pushed = run("git", "push", "-q")
        if pushed.returncode != 0:
            log("Could not reach the cloud copy — it may repeat this alert.")
    except Exception as e:
        log(f"Could not record position: {e}")


async def send_alert(client, chat_id: int, message, group_key: str,
                     topic_title: str, prefix: str = "") -> None:
    """Send one post's alert: its image (if any), then the full details."""
    text = message_text(message)
    alert = prefix + await build_alert(text, group_key, topic_title)

    # If the task came with an image (poster, brief, guideline), send it too
    if getattr(message, "photo", None):
        tmp = Path(TEMP_DIR) / f"task_{message.id}.jpg"
        try:
            await client.download_media(message, file=str(tmp))
            if tmp.exists():
                first = next((l for l in text.splitlines() if l.strip()), "")
                await asyncio.to_thread(
                    notify_photo, chat_id, str(tmp),
                    f"📎 Image attached to: {clean_title(first)[:150]}")
                tmp.unlink(missing_ok=True)
        except Exception as e:
            log(f"Image download failed: {e}")

    notify(chat_id, alert, use_html=True)


async def main() -> None:
    # Only one copy of the watcher should run at a time
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47653))
    except OSError:
        log("The watcher is already running — nothing to do.")
        return

    chat_id = load_state().get("chat_id")
    if not chat_id:
        log("state.json is missing your chat id. Message your bot, then rerun.")
        return

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()

    # Find each group by (partial) title
    groups = {}  # watch-key -> entity
    async for dialog in client.iter_dialogs():
        for key in WATCH:
            if key.lower() in (dialog.name or "").lower():
                groups.setdefault(key, dialog.entity)
    for k in [k for k in WATCH if k not in groups]:
        log(f'WARNING: could not find a group matching "{k}" — skipping it.')
    if not groups:
        log("No watched groups found in your chats.")
        return

    # Find the wanted topics inside each group
    watching = {}  # chat id -> {topic_id: (group_key, topic_title)}
    entities = []
    watched_labels = []
    for key, entity in groups.items():
        topics = await client(
            GetForumTopicsRequest(
                peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=100
            )
        )
        found = {}
        for topic in topics.topics:
            title = getattr(topic, "title", "") or ""
            for wanted in WATCH[key]:
                if wanted.lower() in title.lower():
                    found[topic.id] = (key, title)
        for wanted in WATCH[key]:
            if not any(wanted.lower() in t.lower() for _, t in found.values()):
                log(f'WARNING: topic "{wanted}" not found in "{key}" — skipping it.')
        if found:
            watching[entity.id] = found
            watching[utils.get_peer_id(entity)] = found
            entities.append(entity)
            watched_labels += [f"{t} ({k})" for k, t in found.values()]

    if not watching:
        log("No watched topics found.")
        return

    log("Watching:")
    for label in watched_labels:
        log(f"  - {label}")
    log("Leave this running.")
    notify(chat_id, "✅ Watcher started. Now watching:\n"
           + "\n".join(f"• {l}" for l in watched_labels))

    @client.on(events.NewMessage(chats=entities))
    async def handler(event):
        topics_here = watching.get(event.chat_id) or watching.get(
            getattr(event.chat, "id", None), {}
        )
        reply = event.message.reply_to
        topic_hit = None
        if reply is not None:
            for tid, info in topics_here.items():
                if (
                    getattr(reply, "reply_to_top_id", None) == tid
                    or getattr(reply, "reply_to_msg_id", None) == tid
                ):
                    topic_hit = info
                    break
        if topic_hit is None:
            return
        group_key, topic_title = topic_hit
        await send_alert(client, chat_id, event.message, group_key, topic_title)
        # Stop the cloud copy repeating this alert in a few minutes' time
        await asyncio.to_thread(record_handled, group_key, topic_title,
                                event.message.id)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
