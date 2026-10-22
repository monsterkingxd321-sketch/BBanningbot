"""Shared utilities for the configurable review manager.

The review manager only stores review plans, generates reminders and tracks
manual review status. It never submits reports automatically.
"""

import asyncio
import os
import re
from datetime import datetime

from telethon import TelegramClient, types
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from config import (
    API_HASH,
    API_ID,
    REVIEW_SESSION_TIMEOUT,
)

# Telegram only exposes these report reasons. Category labels are configurable
# but must map to one of these real InputReportReason* types.
TELEGRAM_REPORT_REASONS = {
    "spam": ("Spam", types.InputReportReasonSpam),
    "fake": ("Fake Account", types.InputReportReasonFake),
    "violence": ("Violence", types.InputReportReasonViolence),
    "child_abuse": ("Child Abuse", types.InputReportReasonChildAbuse),
    "pornography": ("Pornography", types.InputReportReasonPornography),
    "illegal_drugs": ("Illegal Drugs", types.InputReportReasonIllegalDrugs),
    "personal_details": ("Personal Details", types.InputReportReasonPersonalDetails),
    "copyright": ("Copyright", types.InputReportReasonCopyright),
    "geo_irrelevant": ("Geo Irrelevant", types.InputReportReasonGeoIrrelevant),
    "other": ("Other", types.InputReportReasonOther),
}
SUPPORTED_REASON_KEYS = tuple(TELEGRAM_REPORT_REASONS.keys())

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,63}$")
_PHONE_RE = re.compile(r"\+\d{7,15}")
_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{25,}\b")


def mask_phone(phone: str) -> str:
    if not phone:
        return "unknown"
    phone = str(phone)
    if len(phone) > 6:
        return phone[:4] + "XXXX" + phone[-3:]
    return phone


def redact_sensitive(text: str, limit: int = 500) -> str:
    """Remove phone numbers and bot tokens before writing logs/audit details."""
    value = str(text or "")
    value = _BOT_TOKEN_RE.sub("[redacted-token]", value)
    value = _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), value)
    return value[:limit]


def parse_channel_identifier(value: str) -> str:
    """Normalise a public channel username or t.me link."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Channel username or link is required.")
    raw = raw.split("?")[0].strip()
    raw = re.sub(r"^https?://", "", raw, flags=re.I)
    raw = re.sub(r"^(www\.)?t\.me/", "", raw, flags=re.I)
    raw = re.sub(r"^telegram\.me/", "", raw, flags=re.I)
    if raw.lower().startswith("s/"):
        raw = raw[2:]
    raw = raw.strip("/")
    if raw.startswith("+"):
        raise ValueError(
            "Private invite links cannot be listed reliably. Use the channel's public username or t.me link."
        )
    if "/"in raw:
        raw = raw.split("/", 1)[0]
    raw = raw.lstrip("@")
    if not _USERNAME_RE.match(raw):
        raise ValueError("Invalid channel username. Example: @channel_name or https://t.me/channel_name")
    return raw


def display_channel(identifier: str, title: str = None, username: str = None) -> str:
    name = f"@{username}"if username else (f"@{identifier}"if identifier else "channel")
    return f"{title} ({name})"if title else name


def session_base_path(session_file: str) -> str:
    if session_file.lower().endswith(".session"):
        return session_file[:-8]
    return session_file


def _build_client(session_file: str, proxy_type=None, proxy_ip=None, proxy_port=None,
                  proxy_user=None, proxy_pass=None) -> TelegramClient:
    from utils.helpers import get_telethon_proxy

    proxy = get_telethon_proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
    return TelegramClient(session_base_path(session_file), API_ID, API_HASH, proxy=proxy)


async def _close_client(client):
    try:
        await client.disconnect()
    except Exception:
        pass


def ensure_telethon_session(session_path: str) -> bool:
    """Ensure a session file is formatted as a valid Telethon SQLite session.

    Automatically detects and converts Pyrogram SQLite sessions and StringSessions.
    """
    if not os.path.isfile(session_path):
        return False
    try:
        with open(session_path, "rb") as handle:
            header = handle.read(16)

        # Check for plain text StringSession
        if not header.startswith(b"SQLite format 3\x00"):
            try:
                with open(session_path, "r", encoding="utf-8", errors="ignore") as tf:
                    raw_text = tf.read().strip()
                if len(raw_text) > 80:
                    from telethon.sessions import StringSession, SQLiteSession
                    ss = StringSession(raw_text)
                    sq = SQLiteSession(session_base_path(session_path))
                    sq.set_dc(ss.dc_id, ss.server_address, ss.port)
                    sq.auth_key = ss.auth_key
                    sq.save()
                    sq.close()
                    return True
            except Exception:
                pass
            return False

        # SQLite file: check if it has Pyrogram schema
        import sqlite3
        conn = sqlite3.connect(session_path)
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if 'sessions' in tables:
            cols = [r[1] for r in cur.execute("PRAGMA table_info(sessions)").fetchall()]
            if 'server_address' not in cols and 'auth_key' in cols:
                # Convert Pyrogram SQLite session to Telethon SQLite format
                row = cur.execute("SELECT dc_id, auth_key FROM sessions").fetchone()
                if row and row[1]:
                    dc_id, auth_key = row[0], row[1]
                    dc_map = {
                        1: "149.154.175.53",
                        2: "149.154.167.51",
                        3: "149.154.175.100",
                        4: "149.154.167.91",
                        5: "91.108.56.130",
                    }
                    ip = dc_map.get(dc_id, "149.154.167.51")
                    cur.execute("DROP TABLE sessions")
                    cur.execute("CREATE TABLE sessions (dc_id integer primary key, server_address text, port integer, auth_key blob, takeout_id integer)")
                    cur.execute("INSERT INTO sessions VALUES (?, ?, 443, ?, NULL)", (dc_id, ip, auth_key))
                    cur.execute("CREATE TABLE IF NOT EXISTS version (version integer)")
                    cur.execute("DELETE FROM version")
                    cur.execute("INSERT INTO version VALUES (7)")
                    cur.execute("CREATE TABLE IF NOT EXISTS entities (id integer primary key, hash integer not null, username text, phone integer, name text)")
                    cur.execute("CREATE TABLE IF NOT EXISTS sent_files (md5_digest blob, file_size integer, type integer, id integer, hash integer, primary key(md5_digest, file_size, type))")
                    cur.execute("CREATE TABLE IF NOT EXISTS update_state (id integer primary key, pts integer, qts integer, date integer, seq integer)")
                    conn.commit()
        conn.close()
        return True
    except Exception as exc:
        print(f"Error checking session format: {exc}")
        return False


async def validate_session_file(session_path: str, timeout: int = None) -> dict:
    """Connect to an uploaded session and verify that it is authorized."""
    timeout = timeout or REVIEW_SESSION_TIMEOUT
    if not os.path.isfile(session_path):
        return {"ok": False, "error": "Session file not found.", "authorized": False}

    # Auto-convert Pyrogram or StringSession to Telethon SQLite format
    ensure_telethon_session(session_path)

    try:
        with open(session_path, "rb") as handle:
            header = handle.read(16)
        if not header.startswith(b"SQLite format 3\x00"):
            return {
                "ok": False,
                "authorized": False,
                "error": "Not a recognized Telethon or Pyrogram SQLite session file.",
            }
    except OSError as exc:
        return {
            "ok": False,
            "authorized": False,
            "error": f"Could not read session file: {exc}",
        }

    client = _build_client(session_path)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=timeout)
        if not authorized:
            return {"ok": False, "authorized": False, "error": "Session is not authorized (logged out or revoked)."}
        me = await asyncio.wait_for(client.get_me(), timeout=timeout)
        if not me:
            return {"ok": False, "authorized": False, "error": "Could not fetch session profile."}
        return {
            "ok": True,
            "authorized": True,
            "user_id": getattr(me, "id", None),
            "phone": getattr(me, "phone", None),
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
            "error": None,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "authorized": False, "error": "Connection timed out (Telegram cold connection)."}
    except Exception as exc:
        return {"ok": False, "authorized": False, "error": redact_sensitive(str(exc), 200)}
    finally:
        await _close_client(client)


async def connect_account(account: dict, timeout: int = None):
    """Open a client for a stored account and return (client, me)."""
    timeout = timeout or REVIEW_SESSION_TIMEOUT
    client = _build_client(
        account.get("session_file"), account.get("proxy_type"), account.get("proxy_ip"),
        account.get("proxy_port"), account.get("proxy_user"), account.get("proxy_pass")
    )
    await asyncio.wait_for(client.connect(), timeout=timeout)
    if not await asyncio.wait_for(client.is_user_authorized(), timeout=timeout):
        await _close_client(client)
        raise RuntimeError("Stored session is not authorized.")
    me = await asyncio.wait_for(client.get_me(), timeout=timeout)
    return client, me


def _message_link(entity, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    raw_id = str(getattr(entity, "id", ""))
    if raw_id.startswith("-100"):
        raw_id = raw_id[4:]
    elif raw_id.startswith("-"):
        raw_id = raw_id[1:]
    return f"https://t.me/c/{raw_id}/{message_id}"


async def resolve_channel_posts(account: dict, identifier: str, limit: int,
                                timeout: int = None) -> dict:
    """Resolve a public channel and return its most recent posts.

    Returns metadata plus the available posts (up to ``limit``). Selecting
    "all posts"in the UI means all posts fetched within this limit.
    """
    timeout = timeout or REVIEW_SESSION_TIMEOUT
    client, me = await connect_account(account, timeout=timeout)
    try:
        entity = None
        raw = (identifier or "").strip()
        if "+"in raw:
            hash_part = raw.split("+", 1)[1]
            try:
                invite = await asyncio.wait_for(client(ImportChatInviteRequest(hash_part)), timeout=timeout)
                entity = invite.chats[0] if invite.chats else None
            except Exception:
                entity = None
        if entity is None:
            entity = await asyncio.wait_for(client.get_entity(identifier), timeout=timeout)
            try:
                await asyncio.wait_for(client(JoinChannelRequest(entity)), timeout=timeout)
            except Exception:
                # Joining is not always required and errors are not fatal for listing.
                pass

        async def collect_posts():
            collected = []
            async for message in client.iter_messages(entity, limit=limit):
                if not getattr(message, "id", None):
                    continue
                text = getattr(message, "message", None) or ""
                if not text and getattr(message, "media", None):
                    text = "[media post]"
                if not text:
                    continue
                collected.append({
                    "message_id": int(message.id),
                    "message_text": text[:1000],
                    "message_link": _message_link(entity, int(message.id)),
                })
            return collected

        posts = await asyncio.wait_for(collect_posts(), timeout=max(timeout * 3, 60))
        return {
            "ok": True,
            "identifier": identifier,
            "telegram_id": getattr(entity, "id", None),
            "access_hash": getattr(entity, "access_hash", None),
            "title": getattr(entity, "title", None),
            "username": getattr(entity, "username", None),
            "posts": posts,
            "account_phone": getattr(me, "phone", None),
        }
    finally:
        await _close_client(client)


def parse_iso(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def format_dt(value: str) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return "not set"
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def build_report_reason(reason_key: str):
    """Return a real Telethon InputReportReason instance for a stored key."""
    entry = TELEGRAM_REPORT_REASONS.get((reason_key or "fake").lower())
    if not entry:
        entry = TELEGRAM_REPORT_REASONS["other"]
    return entry[1]()


def reason_label(reason_key: str) -> str:
    return TELEGRAM_REPORT_REASONS.get((reason_key or "other").lower(), ("Other", None))[0]


def clamp_page(page: int, total: int, page_size: int) -> int:
    if total <= 0:
        return 1
    pages = max(1, (total + page_size - 1) // page_size)
    return min(max(1, int(page)), pages)


def split_text(text: str, limit: int = 3800) -> list:
    if len(text) <= limit:
        return [text]
    return [text[i:i + limit] for i in range(0, len(text), limit)]
