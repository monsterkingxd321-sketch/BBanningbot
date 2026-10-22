"""Configurable multi-account review manager UI and callbacks.

This module stores review plans, reminders and per-message settings. It does
not submit reports automatically and does not coordinate accounts.
"""

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime

from telethon import events, Button

from bot import bot, db
from config import (
    ADMIN_ONLY_IMPORTS,
    DEFAULT_ADMINS,
    EXPORTS_FOLDER,
    REVIEW_CYCLE_OPTIONS,
    REVIEW_DEFAULT_INTERVAL_HOURS,
    REVIEW_DEFAULT_MAX_CYCLES,
    REVIEW_EXPORT_MAX_ROWS,
    REVIEW_INTERVAL_OPTIONS,
    REVIEW_MAX_IMPORT_SESSIONS,
    REVIEW_MAX_POSTS,
    REVIEW_MAX_ZIP_MB,
    REVIEW_PAGE_SIZE,
    REVIEW_SESSION_TIMEOUT,
    SESSIONS_FOLDER,
)
from utils.helpers import is_user_authorized
from utils.review_utils import (
    SUPPORTED_REASON_KEYS,
    display_channel,
    format_dt,
    mask_phone,
    parse_channel_identifier,
    redact_sensitive,
    resolve_channel_posts,
    split_text,
    validate_session_file,
)

MAX_UPLOAD_BYTES = REVIEW_MAX_ZIP_MB * 1024 * 1024
CALLBACK_PREFIX = "rv:"


def _b(label: str, data: str) -> Button:
    return Button.inline(label, data.encode("utf-8"))


def _is_admin(user_id: int) -> bool:
    return db.is_admin(user_id)


def _require_admin(user_id: int) -> bool:
    return (not ADMIN_ONLY_IMPORTS) or db.is_admin(user_id) or (user_id in DEFAULT_ADMINS)


def _status_icon(status: str, enabled: int = 1) -> str:
    if not enabled:
        return ""
    return {
        "active": "",
        "ready": "",
        "paused": "",
        "stopped": "",
        "completed": "",
        "error": "",
        "banned": "",
    }.get(status, "")


def _snippet(text: str, length: int = 45) -> str:
    value = (text or "").replace("\n", " ").strip()
    return value[:length] + ("..."if len(value) > length else "")


async def _safe_edit(event, text: str, buttons=None):
    try:
        await event.edit(text, buttons=buttons, link_preview=False)
    except Exception:
        await event.respond(text, buttons=buttons, link_preview=False)


async def _send_or_edit(event, text: str, buttons=None, edit: bool = True):
    if edit:
        await _safe_edit(event, text, buttons)
    else:
        await event.respond(text, buttons=buttons, link_preview=False)


def _can_access_plan(user_id: int, plan: dict) -> bool:
    return bool(plan) and (_is_admin(user_id) or plan.get("created_by") == user_id)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
@bot.on(events.NewMessage(pattern=r"/review(?:s)?$"))
async def review_command(event):
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        await event.respond("Access Denied!")
        return
    await show_dashboard(event, send=True)


@bot.on(events.NewMessage(pattern=r"/review_help$"))
async def review_help_command(event):
    if not is_user_authorized(event.sender_id):
        await event.respond("Access Denied!")
        return
    await event.respond(
        "Review Manager Help\n\n"
        "/review - dashboard and controls\n"
        "/review_plan <id> - open a plan\n"
        "/review_search <text> - search history\n"
        "/review_export csv|json - export history (admin)\n"
        "/import_sessions - import .session or ZIP (admin)\n"
        "/review_categories - list category labels\n\n"
        "Review plans only send reminders and track manual review status. "
        "They never submit reports or coordinate accounts automatically.\n\n"
        "Categories map to Telegram's real report reasons. Supported reason keys: "
        + ", ".join(SUPPORTED_REASON_KEYS)
    )


@bot.on(events.NewMessage(pattern=r"/review_search(?:\s+(.+))?$"))
async def review_search_command(event):
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        await event.respond("Access Denied!")
        return
    match = re.match(r"/review_search(?:\s+(.+))?$", event.raw_text or "", re.S)
    query = (match.group(1) if match else "") or ""
    query = query.strip()[:200]
    if not query:
        db.set_user_state(user_id, "review_search", {"step": 1})
        await event.respond("Send the text to search in review history:")
        return
    db.set_user_state(user_id, "review_search_active", {"query": query})
    await _show_history(event, user_id, "all", 1, query=query, send=True)


@bot.on(events.NewMessage(pattern=r"/review_export(?:\s+(csv|json))?$"))
async def review_export_command(event):
    user_id = event.sender_id
    if not _is_admin(user_id):
        await event.respond("Admin only command!")
        return
    match = re.match(r"/review_export(?:\s+(csv|json))?$", event.raw_text or "", re.I)
    fmt = (match.group(1) if match else "csv") or "csv"
    await _send_export(event, user_id, fmt.lower())


@bot.on(events.NewMessage(pattern=r"/(?:accounts|sessions)$"))
async def sessions_command(event):
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        await event.respond("Access Denied!")
        return
    await _show_accounts(event, 1, send=True)


@bot.on(events.NewMessage(pattern=r"/import_sessions$"))
async def import_sessions_command(event):
    user_id = event.sender_id
    if not _require_admin(user_id):
        await event.respond("Session imports are restricted to administrators.")
        return
    db.set_user_state(user_id, "review_import_upload", {"step": 1})
    await event.respond(
        "Session Import\n\n"
        "Send one .session file or a ZIP containing multiple .session files.\n"
        f"Limits: {REVIEW_MAX_IMPORT_SESSIONS} sessions, {REVIEW_MAX_ZIP_MB} MB upload.\n\n"
        "Files are validated before import. Invalid, unauthorized and duplicate sessions are skipped. "
        "Uploaded archives are deleted after processing. Send /cancel to abort."
    )


@bot.on(events.NewMessage(pattern=r"/review_plan(?:\s+(\d+))?$"))
async def review_plan_command(event):
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        await event.respond("Access Denied!")
        return
    match = re.match(r"/review_plan(?:\s+(\d+))?$", event.raw_text or "")
    if not match:
        await event.respond("Usage: /review_plan <plan_id>")
        return
    plan = db.get_review_plan(int(match.group(1)))
    if not _can_access_plan(user_id, plan):
        await event.respond("Plan not found!")
        return
    await _show_plan(event, user_id, plan["id"], 1, send=True)


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------
async def show_dashboard(event, send: bool = False):
    if not is_user_authorized(event.sender_id):
        await _send_or_edit(event, "Access Denied!", None, edit=not send)
        return
    stats = db.get_review_dashboard_stats()
    latest_plan = None
    plans = db.get_review_plans(offset=0, limit=1)
    if plans:
        latest_plan = plans[0]
    text = (
        "Review Manager\n\n"
        f"Sessions: {stats['sessions_total']} (enabled: {stats['sessions_enabled']})\n"
        f"Channels: {stats['channels']}\n"
        f"Selected messages: {stats['selected_messages']}\n"
        f"Plans: {stats['ready_plans']} ready, {stats['active_plans']} active, {stats['paused_plans']} paused\n"
        f"Current cycle: {stats['current_cycle']}/{stats['max_cycles'] or '-'}\n"
        f"Next reminder: {format_dt(stats['next_review_at'])}\n"
        f"Pending: {stats['pending']} |  Reviewed: {stats['reviewed']}\n"
        f"Errors: {stats['errors']}\n"
    )
    if latest_plan:
        text += f"\nLatest plan: #{latest_plan['id']} - {latest_plan.get('channel_title') or latest_plan.get('channel_identifier') or 'channel'}"
    text += (
        "\n\nReminders only - this manager never submits reports automatically."
    )
    buttons = [
        [_b("New Review Plan", "rv:new"), _b("Sessions", "rv:accounts:1")],
        [_b("Plans", "rv:plans:1"), _b("History", "rv:hist:all:1")],
        [_b("Audit Logs", "rv:audit:1"), _b("Errors", "rv:errors:1")],
        [_b("Export CSV", "rv:csv"), _b("Export JSON", "rv:json")],
        [_b("Search", "rv:search"), _b("Refresh", "rv:dash")],
    ]
    await _send_or_edit(event, text, buttons, edit=not send)


# ----------------------------------------------------------------------
# Session administration
# ----------------------------------------------------------------------
def _accounts_text(accounts: list, page: int) -> str:
    lines = [
        "Sessions\n",
        f"Total: {len(accounts)}",
        "disabled sessions are never used for reporting.\n",
    ]
    start = (page - 1) * REVIEW_PAGE_SIZE
    for account in accounts[start:start + REVIEW_PAGE_SIZE]:
        label = account.get("label") or mask_phone(account.get("phone"))
        lines.append(
            f"{_status_icon(account.get('status'), account.get('enabled', 1))} "
            f"#{account['id']} {label}\n"
            f"phone: {mask_phone(account.get('phone'))} | status: {account.get('status')} | "
            f"{'enabled'if account.get('enabled') else 'disabled'}\n"
            f"last check: {format_dt(account.get('last_checked'))} "
            f"({account.get('connection_status') or 'unknown'})"
        )
    return "\n".join(lines)


def _accounts_buttons(accounts: list, page: int):
    rows = []
    start = (page - 1) * REVIEW_PAGE_SIZE
    for account in accounts[start:start + REVIEW_PAGE_SIZE]:
        toggle = "Disable"if account.get("enabled") else "Enable"
        rows.append([
            _b(f"{toggle} #{account['id']}", f"rv:atoggle:{account['id']}"),
            _b(f"Check #{account['id']}", f"rv:acheck:{account['id']}"),
        ])
    pages = max(1, (len(accounts) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    nav = []
    if page > 1:
        nav.append(_b("Prev", f"rv:accounts:{page - 1}"))
    nav.append(_b(f"{page}/{pages}", "rv:accounts:1"))
    if page < pages:
        nav.append(_b("Next ", f"rv:accounts:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_b("Import Sessions", "rv:imp"), _b("Refresh", f"rv:accounts:{page}")])
    rows.append([_b("Dashboard", "rv:dash")])
    return rows


async def _show_accounts(event, page: int = 1, send: bool = False):
    user_id = event.sender_id
    if not _require_admin(user_id):
        await _send_or_edit(event, "Session management is restricted to administrators.", None, edit=not send)
        return
    accounts = db.get_account_records()
    pages = max(1, (len(accounts) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(max(1, page), pages)
    await _send_or_edit(event, _accounts_text(accounts, page), _accounts_buttons(accounts, page), edit=not send)


async def _toggle_account(event, user_id: int, account_id: int):
    if not _require_admin(user_id):
        await event.answer("Admin only.", alert=True)
        return
    account = db.get_account_by_id(account_id)
    if not account:
        await event.answer("Account not found.", alert=True)
        return
    new_state = not bool(account.get("enabled"))
    db.toggle_account_enabled(account_id, new_state)
    db.log_audit(user_id, "session_toggle", f"Account #{account_id} enabled={new_state}")
    await event.answer("Enabled"if new_state else "Disabled")
    await _show_accounts(event, 1)


async def _check_account(event, user_id: int, account_id: int):
    if not _require_admin(user_id):
        await event.answer("Admin only.", alert=True)
        return
    account = db.get_account_by_id(account_id)
    if not account:
        await event.answer("Account not found.", alert=True)
        return
    await event.answer("Checking session...")
    try:
        from utils.review_utils import connect_account
        client, me = await connect_account(account)
        try:
            db.set_account_check(
                account_id, "connected", authorized=True,
                session_user_id=getattr(me, "id", None), phone=getattr(me, "phone", None)
            )
            db.log_audit(user_id, "session_check", f"Account #{account_id} connected")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    except Exception as exc:
        error = redact_sensitive(exc, 200)
        db.set_account_check(account_id, f"error: {error}", authorized=False)
        db.log_audit(user_id, "error", f"Session check failed for account #{account_id}: {error}")
    await _show_accounts(event, 1)


# ----------------------------------------------------------------------
# Session import (.session / ZIP)
# ----------------------------------------------------------------------
def _cleanup_import_dir(path: str):
    if not path:
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def cancel_review_state(user_id: int):
    """Clean temporary import files and clear a pending review state."""
    state, data = db.get_user_state(user_id)
    if state == "review_import_preview":
        _cleanup_import_dir(data.get("temp_root", ""))
    if state:
        db.clear_user_state(user_id)


def _safe_session_name(entry: dict) -> str:
    key = entry.get("phone") or f"user_{entry.get('user_id') or 'unknown'}"
    key = re.sub(r"[^A-Za-z0-9_]+", "", str(key))[:40] or "session"
    digest = (entry.get("file_hash") or uuid.uuid4().hex)[:8]
    return f"imported_{key}_{digest}.session"


async def handle_review_import_upload(event, data: dict):
    if not _require_admin(event.sender_id):
        db.clear_user_state(event.sender_id)
        await event.respond("Session imports are restricted to administrators.")
        return
    await _prepare_import(event, event.sender_id)


async def _prepare_import(event, user_id: int):
    # Remove any previous preview/temp files before starting a new upload.
    old_state, old_data = db.get_user_state(user_id)
    if old_state == "review_import_preview":
        _cleanup_import_dir(old_data.get("temp_root", ""))
    if not event.document:
        await event.respond("Send a .session file or a ZIP archive as a document.")
        return
    file_name = (getattr(event.file, "name", None) or "upload").strip()
    lower = file_name.lower()
    size = getattr(event.document, "size", 0) or 0
    if size > MAX_UPLOAD_BYTES:
        await event.respond(f"Upload is too large. Maximum {REVIEW_MAX_ZIP_MB} MB.")
        return

    progress = await event.respond("Downloading and validating sessions...")
    try:
        content = await event.download_media(bytes)
    except Exception as exc:
        await progress.edit(f"Download failed: {redact_sensitive(exc)}")
        return
    if not content or len(content) > MAX_UPLOAD_BYTES:
        await progress.edit("Empty or oversized upload.")
        return

    # Telegram may strip or rename the extension. Detect the real container
    # from its signature so a valid ZIP or SQLite session still works.
    is_zip = lower.endswith(".zip") or content.startswith(b"PK\x03\x04")
    is_session = (
        lower.endswith(".session")
        or content.startswith(b"SQLite format 3\x00")
    )
    if not (is_zip or is_session):
        await event.respond(
            "Only .session files or .zip archives are accepted. "
            "If the ZIP has no extension, send it as a document with a .zip "
            "name. TDesktop tdata and Pyrogram sessions are not supported."
        )
        return

    temp_root = os.path.join(SESSIONS_FOLDER, ".imports", f"{user_id}_{uuid.uuid4().hex}")
    os.makedirs(temp_root, mode=0o700, exist_ok=True)
    extracted = []
    errors = []
    try:
        if is_zip:
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    total = 0
                    for info in archive.infolist():
                        if info.is_dir():
                            continue
                        clean_fn = info.filename.replace('\\', '/')
                        if '/.' in clean_fn or clean_fn.startswith('.'):
                            continue
                        if '__macosx' in clean_fn.lower():
                            continue
                        base = os.path.basename(clean_fn)
                        if base.startswith('.'):
                            continue
                        if not (base.lower().endswith(".session") or base.lower().endswith(".txt") or base.lower().endswith(".json")):
                            continue
                        if len(extracted) >= REVIEW_MAX_IMPORT_SESSIONS:
                            errors.append(f"More than {REVIEW_MAX_IMPORT_SESSIONS} sessions; extra files skipped.")
                            break
                        if info.flag_bits & 0x1:
                            errors.append(f"{base}: encrypted ZIP entries are not supported.")
                            continue
                        total += info.file_size
                        if total > MAX_UPLOAD_BYTES:
                            errors.append("ZIP expands beyond the allowed size; remaining files skipped.")
                            break
                        out_path = os.path.join(temp_root, f"{uuid.uuid4().hex}_{base}")
                        with archive.open(info) as src, open(out_path, "wb") as dst:
                            dst.write(src.read())
                        try:
                            os.chmod(out_path, 0o600)
                        except OSError:
                            pass
                        extracted.append((base, out_path))
            except zipfile.BadZipFile:
                await progress.edit("Not a valid ZIP archive.")
                _cleanup_import_dir(temp_root)
                return
        else:
            out_path = os.path.join(temp_root, "uploaded.session")
            with open(out_path, "wb") as handle:
                handle.write(content)
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
            extracted.append((file_name, out_path))

        if not extracted:
            await progress.edit("No .session files found in the upload.")
            _cleanup_import_dir(temp_root)
            return

        records = db.get_account_records()
        existing_phones = {r.get("phone") for r in records if r.get("phone")}
        existing_users = {r.get("session_user_id") for r in records if r.get("session_user_id")}
        existing_hashes = {r.get("file_hash") for r in records if r.get("file_hash")}
        batch_keys = set()
        batch_hashes = set()
        semaphore = asyncio.Semaphore(5)

        async def validate_one(original, path):
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            async with semaphore:
                result = await validate_session_file(
                    path, timeout=REVIEW_SESSION_TIMEOUT
                )
                if not result.get("ok") and "timed out" in str(
                    result.get("error") or ""
                ).lower():
                    await asyncio.sleep(1)
                    result = await validate_session_file(
                        path, timeout=REVIEW_SESSION_TIMEOUT
                    )
            return {
                "original": original,
                "temp_path": path,
                "ok": bool(result.get("ok") and result.get("authorized")),
                "phone": result.get("phone"),
                "user_id": result.get("user_id"),
                "username": result.get("username"),
                "first_name": result.get("first_name"),
                "file_hash": digest,
                "duplicate": None,
                "existing": False,
                "error": result.get("error"),
            }

        entries = list(await asyncio.gather(*[
            validate_one(original, path) for original, path in extracted
        ]))

        for entry in entries:
            phone = entry.get("phone")
            session_user_id = entry.get("user_id")
            key = phone or (f"user_{session_user_id}" if session_user_id else None)
            entry["key"] = key
            if not entry["ok"]:
                continue
            if not key:
                entry["duplicate"] = "No phone/user id available"
            elif phone in existing_phones or session_user_id in existing_users:
                entry["duplicate"] = None
                entry["existing"] = True
            elif key in batch_keys or entry["file_hash"] in batch_hashes:
                entry["duplicate"] = "Duplicate in this upload"
            if key:
                batch_keys.add(key)
            batch_hashes.add(entry["file_hash"])

        usable = [e for e in entries if e["ok"] and not e["duplicate"]]
        text = ["**Session Import Preview**\n"]
        for entry in entries:
            masked = mask_phone(entry.get("phone")) if entry.get("phone") else "no phone"
            if entry.get("existing"):
                state_text = "authorized (update existing)"
                icon = "[UPDATE]"
            elif entry["duplicate"]:
                state_text = f"DUPLICATE: {entry['duplicate']}"
                icon = "[DUPLICATE]"
            elif entry["ok"]:
                state_text = "authorized (new)"
                icon = "[OK]"
            else:
                state_text = f"invalid: {entry.get('error') or 'not authorized'}"
                icon = "[INVALID]"
            text.append(f"{icon} `{entry['original']}` - {masked} - {state_text}")
        if errors:
            text.append("\nArchive notes:")
            text.extend(f"- {item}" for item in errors[:5])
        text.append(
            f"\nUsable sessions: {len(usable)}/{len(entries)}\n"
            "Ready to import."
        )
        preview_text = "\n".join(text)
        db.set_user_state(user_id, "review_import_preview", {
            "entries": entries,
            "temp_root": temp_root,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        buttons = [
            [_b(f"Import {len(usable)} session(s)", "rv:impconfirm")] if usable else [],
            [_b("Cancel", "rv:impcancel")],
        ]
        buttons = [row for row in buttons if row]
        try:
            await progress.edit(preview_text[:4000], buttons=buttons, link_preview=False)
        except Exception:
            await event.respond(preview_text[:4000], buttons=buttons, link_preview=False)
    except Exception as exc:
        _cleanup_import_dir(temp_root)
        await progress.edit(f"Import preparation failed: {redact_sensitive(exc)}")
        db.log_audit(user_id, "error", f"Session import preparation failed: {redact_sensitive(exc)}")


async def _confirm_import(event, user_id: int):
    if not _require_admin(user_id):
        await event.answer("Admin only.", alert=True)
        return
    state, data = db.get_user_state(user_id)
    if state != "review_import_preview":
        await event.edit("Import preview expired. Start again with /import_sessions.")
        return
    entries = data.get("entries", [])
    temp_root = data.get("temp_root", "")
    records = db.get_account_records()
    existing_phones = {r.get("phone") for r in records if r.get("phone")}
    existing_users = {r.get("session_user_id") for r in records if r.get("session_user_id")}
    existing_hashes = {r.get("file_hash") for r in records if r.get("file_hash")}

    imported = 0
    skipped = 0
    failures = []
    for entry in entries:
        if not entry.get("ok") or entry.get("duplicate"):
            skipped += 1
            continue
        phone = entry.get("phone")
        session_user_id = entry.get("user_id")
        key = phone or f"user_{session_user_id}"
        source = entry.get("temp_path")
        if not source or not os.path.isfile(source):
            failures.append(f"{entry.get('original')}: temporary file missing")
            skipped += 1
            continue

        destination = os.path.join(SESSIONS_FOLDER, _safe_session_name(entry))
        try:
            os.makedirs(SESSIONS_FOLDER, exist_ok=True)
            shutil.copyfile(source, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass

            if phone and phone in existing_phones:
                # Update existing account record
                db.add_account(phone, destination, user_id)
                imported += 1
                continue

            label = entry.get("username") or entry.get("first_name")
            if db.add_account_record(key, destination, user_id, session_user_id,
                                     file_hash=entry.get("file_hash"), label=label):
                imported += 1
                if phone:
                    existing_phones.add(phone)
                if session_user_id:
                    existing_users.add(session_user_id)
                existing_hashes.add(entry.get("file_hash"))
            else:
                db.add_account(key, destination, user_id)
                imported += 1
        except Exception as exc:
            skipped += 1
            failures.append(f"{entry.get('original')}: {redact_sensitive(exc, 120)}")

    _cleanup_import_dir(temp_root)
    db.clear_user_state(user_id)
    db.log_audit(user_id, "session_import", f"Imported {imported}, skipped {skipped}")
    text = (
        "**Session Import Complete**\n\n"
        f"Imported/Updated: {imported}\n"
        f"Skipped: {skipped}\n"
    )
    if failures:
        text += "\nFailures:\n" + "\n".join(f"- {item}"for item in failures[:10])
    await event.edit(text, buttons=[[_b("Sessions", "rv:accounts:1")]])


def _cancel_import(user_id: int):
    state, data = db.get_user_state(user_id)
    if state == "review_import_preview":
        _cleanup_import_dir(data.get("temp_root", ""))
    db.clear_user_state(user_id)


# ----------------------------------------------------------------------
# New plan / channel resolution
# ----------------------------------------------------------------------
async def _start_new_plan(event, user_id: int):
    db.set_user_state(user_id, "review_channel", {"step": 1})
    await event.edit(
        "New Review Plan - Channel\n\n"
        "Send a public channel username or link:\n"
        "- @channel_name\n"
        "- https://t.me/channel_name\n\n"
        f"Up to {REVIEW_MAX_POSTS} recent posts will be listed. /cancel to abort.",
        buttons=[[_b("Cancel", "rv:dash")]]
    )


async def handle_review_channel(event, data: dict):
    user_id = event.sender_id
    raw = (event.raw_text or "").strip()
    try:
        identifier = parse_channel_identifier(raw)
    except ValueError as exc:
        await event.respond(f" {exc}")
        return

    accounts = db.get_enabled_accounts()
    if not accounts:
        await event.respond("No enabled sessions. Import or log in a session first.")
        db.clear_user_state(user_id)
        return

    progress = await event.respond("Resolving channel and fetching recent posts...")
    result = None
    errors = []
    for account in accounts[:3]:
        try:
            result = await resolve_channel_posts(account, identifier, REVIEW_MAX_POSTS)
            if result.get("posts"):
                break
            errors.append("No accessible posts found.")
        except Exception as exc:
            errors.append(redact_sensitive(exc, 120))
            result = None
    if not result or not result.get("posts"):
        db.clear_user_state(user_id)
        await progress.edit(
            "Could not list posts for this channel.\n\n"
            + ("\n".join(f"- {item}"for item in errors[:3]) if errors else "")
        )
        return

    try:
        channel_id = db.upsert_review_channel(
            identifier=result.get("username") or identifier,
            telegram_id=result.get("telegram_id"),
            access_hash=result.get("access_hash"),
            title=result.get("title"),
            username=result.get("username"),
            added_by=user_id,
        )
        plan_id = db.create_review_draft(
            user_id, channel_id,
            REVIEW_DEFAULT_INTERVAL_HOURS, REVIEW_DEFAULT_MAX_CYCLES
        )
        inserted = db.add_review_candidates(plan_id, channel_id, result["posts"])
    except Exception as exc:
        db.clear_user_state(user_id)
        await progress.edit(f"Could not save plan: {redact_sensitive(exc)}")
        return

    db.clear_user_state(user_id)
    db.log_audit(user_id, "plan_draft", f"Plan #{plan_id}, channel {identifier}, posts {inserted}")
    try:
        await progress.delete()
    except Exception:
        pass
    await _show_candidates(event, plan_id, 1, send=True)


def _candidate_page(plan_id: int, page: int):
    items = db.get_review_candidates(plan_id)
    total = len(items)
    pages = max(1, (total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(max(1, page), pages)
    start = (page - 1) * REVIEW_PAGE_SIZE
    return items, items[start:start + REVIEW_PAGE_SIZE], page, pages


async def _show_candidates(event, plan_id: int, page: int = 1, send: bool = False):
    user_id = event.sender_id
    plan = db.get_review_plan(plan_id)
    if not _can_access_plan(user_id, plan):
        await _send_or_edit(event, "Plan not found!", None, edit=not send)
        return
    items, visible, page, pages = _candidate_page(plan_id, page)
    selected = sum(1 for item in items if item["selected"])
    text = [
        "Select Posts",
        "",
        f"Channel: {display_channel(plan.get('channel_identifier'), plan.get('channel_title'), plan.get('channel_username'))}",
        f"Plan: #{plan_id} | Page {page}/{pages}",
        f"Selected: {selected}/{len(items)}",
        "",
    ]
    for item in visible:
        mark = ""if item["selected"] else ""
        text.append(f"{mark} #{item['message_id']} - {_snippet(item.get('message_text'), 60)}")
    text.append("\nTap a post to toggle it. 'Select all'selects every fetched post.")
    rows = []
    for item in visible:
        mark = ""if item["selected"] else ""
        rows.append([_b(f"{mark} #{item['message_id']} {_snippet(item.get('message_text'), 28)}", f"rv:tsel:{item['id']}")])
    nav = []
    if page > 1:
        nav.append(_b("Prev", f"rv:tpage:{plan_id}:{page - 1}"))
    nav.append(_b(f"{page}/{pages}", f"rv:tpage:{plan_id}:{page}"))
    if page < pages:
        nav.append(_b("Next ", f"rv:tpage:{plan_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_b("Select All", f"rv:tall:{plan_id}:1"), _b("Clear", f"rv:tall:{plan_id}:0")])
    rows.append([_b(f"Continue ({selected})", f"rv:tcont:{plan_id}")])
    rows.append([_b("Cancel Plan", f"rv:cancel:{plan_id}"), _b("Dashboard", "rv:dash")])
    await _send_or_edit(event, "\n".join(text), rows, edit=not send)


async def _candidate_toggle(event, user_id: int, item_id: int):
    item = db.get_review_item(item_id)
    if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
        await event.answer("Not found.", alert=True)
        return
    new_state = db.toggle_review_candidate(item_id, item["plan_id"])
    items = db.get_review_candidates(item["plan_id"])
    index = next((idx for idx, row in enumerate(items) if row["id"] == item_id), 0)
    page = (index // REVIEW_PAGE_SIZE) + 1
    await event.answer("Selected"if new_state else "Unselected")
    await _show_candidates(event, item["plan_id"], page)


# ----------------------------------------------------------------------
# Per-item configuration
# ----------------------------------------------------------------------
async def _show_item_config(event, plan_id: int, send: bool = False):
    user_id = event.sender_id
    plan = db.get_review_plan(plan_id)
    if not _can_access_plan(user_id, plan):
        await _send_or_edit(event, "Plan not found!", None, edit=not send)
        return
    item = db.get_first_unconfigured_item(plan_id)
    if not item:
        await _show_settings(event, plan_id, "interval", send=send)
        return
    total = db.count_plan_items(plan_id, selected_only=True)
    done = total - db.count_unconfigured_items(plan_id)
    text = (
        f"Configure Message {done + 1}/{total}\n\n"
        f"Post: #{item['message_id']}\n"
        f"{item.get('message_link') or ''}\n\n"
        f"{_snippet(item.get('message_text'), 300)}\n\n"
        "Choose a category. Each message stores its own category, "
        "subcategory and optional explanation."
    )
    rows = []
    categories = db.get_review_categories()
    current_row = []
    for cat in categories:
        current_row.append(_b(cat["label"][:28], f"rv:cat:{item['id']}:{cat['id']}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    presets = db.list_presets(limit=5)
    for preset in presets:
        rows.append([_b(f"Preset: {preset['name'][:30]}", f"rv:preset:{item['id']}:{preset['id']}")])
    rows.append([_b("Cancel Plan", f"rv:cancel:{plan_id}"), _b("Dashboard", "rv:dash")])
    await _send_or_edit(event, text, rows, edit=not send)


async def _show_subcategories(event, item_id: int, category_id: int, send: bool = False):
    user_id = event.sender_id
    item = db.get_review_item(item_id)
    if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
        await _send_or_edit(event, "Item not found!", None, edit=not send)
        return
    category = db.get_category_by_id(category_id)
    if not category:
        await _send_or_edit(event, "Category not found!", None, edit=not send)
        return
    subs = db.get_subcategories(category_id)
    if not subs:
        db.set_item_category(item_id, category_id)
        await _ask_explanation(event, item_id, send=send)
        return
    text = (
        f"Subcategory for #{item['message_id']}\n\n"
        f"Category: {category['label']}\n"
        "Select the closest subcategory:"
    )
    rows = []
    for sub in subs:
        rows.append([_b(sub["label"][:45], f"rv:sub:{item_id}:{sub['id']}")])
    rows.append([_b("Back", f"rv:cfg:{item['plan_id']}")])
    await _send_or_edit(event, text, rows, edit=not send)


async def _ask_explanation(event, item_id: int, send: bool = False):
    user_id = event.sender_id
    item = db.get_review_item(item_id)
    if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
        await _send_or_edit(event, "Item not found!", None, edit=not send)
        return
    db.set_user_state(user_id, "review_explanation", {
        "item_id": item_id,
        "plan_id": item["plan_id"],
    })
    await _send_or_edit(
        event,
        f"Optional explanation for #{item['message_id']}\n\n"
        "Send the explanation text, or tap Skip. It is stored only for this message.",
        [[_b("Skip", f"rv:expskip:{item_id}")]],
        edit=not send,
    )


async def _after_item_configured(event, plan_id: int):
    await _show_item_config(event, plan_id, send=False)


async def handle_review_explanation(event, data: dict):
    user_id = event.sender_id
    item_id = data.get("item_id")
    item = db.get_review_item(item_id) if item_id else None
    if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
        db.clear_user_state(user_id)
        await event.respond("Item not found!")
        return
    explanation = (event.raw_text or "").strip()
    db.set_item_explanation(item_id, explanation, configured=True)
    db.clear_user_state(user_id)
    await _after_item_configured(event, item["plan_id"])


# ----------------------------------------------------------------------
# Plan interval / cycles / preview
# ----------------------------------------------------------------------
async def _show_settings(event, plan_id: int, stage: str = "interval", send: bool = False):
    user_id = event.sender_id
    plan = db.get_review_plan(plan_id)
    if not _can_access_plan(user_id, plan):
        await _send_or_edit(event, "Plan not found!", None, edit=not send)
        return
    if stage == "cycles":
        text = (
            "Maximum Review Cycles\n\n"
            f"Selected interval: every {plan['interval_hours']:g} hours\n"
            f"Current value: {plan['max_cycles']} cycles\n\n"
            "Choose the maximum number of reminder cycles:"
        )
        rows = []
        current = []
        for value in REVIEW_CYCLE_OPTIONS:
            current.append(_b(f"{value} cycles", f"rv:cyc:{plan_id}:{value}"))
        rows.append(current)
        rows.append([_b("Custom", f"rv:cyccustom:{plan_id}")])
    else:
        text = (
            "Review Reminder Interval\n\n"
            f"Current value: every {plan['interval_hours']:g} hours\n"
            "Choose how often reminder cycles should run:"
        )
        rows = []
        current = []
        for value in REVIEW_INTERVAL_OPTIONS:
            current.append(_b(f"{value}h", f"rv:int:{plan_id}:{value}"))
        rows.append(current)
        rows.append([_b("Custom", f"rv:intcustom:{plan_id}")])
    rows.append([_b("Cancel Plan", f"rv:cancel:{plan_id}"), _b("Dashboard", "rv:dash")])
    await _send_or_edit(event, text, rows, edit=not send)


async def _show_plan_preview(event, plan_id: int, send: bool = False):
    user_id = event.sender_id
    plan = db.get_review_plan(plan_id)
    if not _can_access_plan(user_id, plan):
        await _send_or_edit(event, "Plan not found!", None, edit=not send)
        return
    items = db.get_plan_items(plan_id, selected_only=True, limit=1000)
    lines = [
        f"Review Plan Preview - #{plan_id}",
        f"Channel: {display_channel(plan.get('channel_identifier'), plan.get('channel_title'), plan.get('channel_username'))}",
        f"Interval: every {plan['interval_hours']:g} hours",
        f"Maximum cycles: {plan['max_cycles']}",
        f"Selected messages: {len(items)}",
        "",
    ]
    for item in items:
        lines.append(
            f"#{item['message_id']} - {item.get('category_label') or 'no category'} / "
            f"{item.get('subcategory_label') or 'no subcategory'}\n"
            f"  {item.get('message_link') or ''}\n"
            f"Explanation: {_snippet(item.get('explanation'), 300) or '(none)'}"
        )
    lines.append("\nConfirmation creates a ready plan. Press Start on the plan to begin reminders.")
    parts = split_text("\n".join(lines), 3700)
    await _safe_edit(event, parts[0])
    for part in parts[1:]:
        await event.respond(part)
    await event.respond(
        "Confirm this plan?",
        buttons=[
            [_b("Confirm Plan", f"rv:confirm:{plan_id}")],
            [_b("Cancel Plan", f"rv:cancel:{plan_id}"), _b("Dashboard", "rv:dash")],
        ],
    )


async def handle_review_interval_custom(event, data: dict):
    user_id = event.sender_id
    plan_id = data.get("plan_id")
    plan = db.get_review_plan(plan_id) if plan_id else None
    if not _can_access_plan(user_id, plan):
        db.clear_user_state(user_id)
        await event.respond("Plan not found!")
        return
    try:
        value = float((event.raw_text or "").strip())
    except ValueError:
        await event.respond("Enter a number of hours, for example 2 or 1.5.")
        return
    if value < 0.1 or value > 168:
        await event.respond("Interval must be between 0.1 and 168 hours.")
        return
    db.set_review_plan_settings(plan_id, value, plan["max_cycles"])
    db.clear_user_state(user_id)
    await _show_settings(event, plan_id, "cycles", send=False)


async def handle_review_cycles_custom(event, data: dict):
    user_id = event.sender_id
    plan_id = data.get("plan_id")
    plan = db.get_review_plan(plan_id) if plan_id else None
    if not _can_access_plan(user_id, plan):
        db.clear_user_state(user_id)
        await event.respond("Plan not found!")
        return
    try:
        value = int((event.raw_text or "").strip())
    except ValueError:
        await event.respond("Enter a whole number of cycles, for example 5.")
        return
    if value < 1 or value > 100:
        await event.respond("Cycles must be between 1 and 100.")
        return
    db.set_review_plan_settings(plan_id, plan["interval_hours"], value)
    db.clear_user_state(user_id)
    await _show_plan_preview(event, plan_id)


# ----------------------------------------------------------------------
# Plan display / reviewer tracking
# ----------------------------------------------------------------------
def _plan_header(plan: dict) -> str:
    return (
        f"Review Plan #{plan['id']}\n\n"
        f"Channel: {display_channel(plan.get('channel_identifier'), plan.get('channel_title'), plan.get('channel_username'))}\n"
        f"Status: {_status_icon(plan.get('status'))} {plan.get('status')}\n"
        f"Interval: every {plan['interval_hours']:g} hours\n"
        f"Cycle: {plan.get('current_cycle') or 0}/{plan.get('max_cycles') or 0}\n"
        f"Next reminder: {format_dt(plan.get('next_review_at'))}\n"
    )


async def _show_plan(event, user_id: int, plan_id: int, page: int = 1, send: bool = False):
    plan = db.get_review_plan(plan_id)
    if not _can_access_plan(user_id, plan):
        await _send_or_edit(event, "Plan not found!", None, edit=not send)
        return
    total = db.count_plan_items(plan_id, selected_only=True)
    pending = db.count_plan_items(plan_id, selected_only=True, status="pending")
    reviewed = db.count_plan_items(plan_id, selected_only=True, status="reviewed")
    pages = max(1, (total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(max(1, page), pages)
    items = db.get_plan_items(plan_id, selected_only=True, offset=(page - 1) * REVIEW_PAGE_SIZE,
                              limit=REVIEW_PAGE_SIZE)
    text = [
        _plan_header(plan),
        f"Messages: {total} | pending: {pending} | reviewed: {reviewed}",
        f"Page {page}/{pages}",
        "",
    ]
    for item in items:
        reviewer = item.get("reviewer_label") or mask_phone(item.get("reviewer_phone"))
        text.append(
            f"{''if item['status'] == 'reviewed'else ''} #{item['message_id']} "
            f"[{item['status']}] {item.get('message_link') or ''}\n"
            f"   {item.get('category_label') or '-'} / {item.get('subcategory_label') or '-'}\n"
            f"Explanation: {_snippet(item.get('explanation'), 160) or '(none)'}\n"
            f"Reviewed by: {reviewer if item['status'] == 'reviewed'else '-'}"
            + (f"at {format_dt(item.get('reviewed_at'))}"if item.get('reviewed_at') else "")
        )
    if plan.get("last_error"):
        text.append(f"\n Last error: {plan['last_error']}")

    rows = []
    for item in items:
        if item["status"] == "reviewed":
            rows.append([_b(f"Pending #{item['message_id']}", f"rv:unmark:{item['id']}")])
        else:
            rows.append([_b(f"Reviewed #{item['message_id']}", f"rv:mark:{item['id']}")])
    nav = []
    if page > 1:
        nav.append(_b("Prev", f"rv:plan:{plan_id}:{page - 1}"))
    nav.append(_b(f"{page}/{pages}", f"rv:plan:{plan_id}:{page}"))
    if page < pages:
        nav.append(_b("Next ", f"rv:plan:{plan_id}:{page + 1}"))
    if nav:
        rows.append(nav)

    controls = []
    if plan["status"] == "ready":
        controls.append(_b("Start", f"rv:start:{plan_id}"))
    elif plan["status"] == "active":
        controls.append(_b("Pause", f"rv:pause:{plan_id}"))
    elif plan["status"] == "paused":
        controls.append(_b("Resume", f"rv:resume:{plan_id}"))
    if plan["status"] in ("ready", "active", "paused"):
        controls.append(_b("Stop", f"rv:stop:{plan_id}"))
    if controls:
        rows.append(controls)
    rows.append([_b("History", "rv:hist:all:1"), _b("Dashboard", "rv:dash")])
    await _send_or_edit(event, "\n".join(text), rows, edit=not send)


async def _show_reviewer_accounts(event, user_id: int, item_id: int):
    item = db.get_review_item(item_id)
    plan = db.get_review_plan(item["plan_id"]) if item else None
    if not item or not _can_access_plan(user_id, plan):
        await event.answer("Item not found.", alert=True)
        return
    accounts = db.get_enabled_accounts()
    text = (
        f"Mark message #{item['message_id']} as reviewed\n\n"
        "Choose the session that performed this manual review. "
        "This only records attribution; nothing is submitted automatically."
    )
    rows = []
    for account in accounts:
        label = account.get("label") or mask_phone(account.get("phone"))
        rows.append([_b(f"#{account['id']} {label}", f"rv:acct:{item_id}:{account['id']}")])
    rows.append([_b("Unassigned", f"rv:acct:{item_id}:0")])
    rows.append([_b("Back to Plan", f"rv:plan:{plan['id']}:1")])
    await _safe_edit(event, text, rows)


# ----------------------------------------------------------------------
# History / audit / exports
# ----------------------------------------------------------------------
def _history_text(items: list, total: int, page: int, pages: int, filter_name: str,
                  query: str = None) -> str:
    lines = [
        "Review History\n",
        f"Filter: {filter_name}" + (f" | search: {query}"if query else ""),
        f"Results: {total} | Page {page}/{pages}\n",
    ]
    for item in items:
        reviewer = item.get("reviewer_label") or mask_phone(item.get("reviewer_phone"))
        lines.append(
            f"Plan #{item['plan_id']} | {item.get('channel_title') or item.get('channel_identifier') or 'channel'}\n"
            f"  {''if item['status'] == 'reviewed'else ''} #{item['message_id']} - {_snippet(item.get('message_text'), 70)}\n"
            f"  {item.get('category_label') or '-'} / {item.get('subcategory_label') or '-'}\n"
            f"Explanation: {_snippet(item.get('explanation'), 160) or '(none)'}\n"
            f"Reviewed by: {reviewer if item['status'] == 'reviewed'else '-'}\n"
            f"  {item.get('message_link') or ''}"
        )
    return "\n".join(lines)


async def _show_history(event, user_id: int, filter_name: str = "all", page: int = 1,
                        query: str = None, send: bool = False):
    status = filter_name if filter_name in ("pending", "reviewed") else None
    total = db.count_review_history(query=query, status=status)
    pages = max(1, (total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(max(1, page), pages)
    items = db.search_review_history(query=query, status=status,
                                     offset=(page - 1) * REVIEW_PAGE_SIZE,
                                     limit=REVIEW_PAGE_SIZE)
    text = _history_text(items, total, page, pages, filter_name, query)
    rows = [
        [_b("All", "rv:hist:all:1"), _b("Pending", "rv:hist:pending:1"), _b("Reviewed", "rv:hist:reviewed:1")],
    ]
    nav = []
    if query:
        if page > 1:
            nav.append(_b("Prev", f"rv:hsearch:{page - 1}"))
        nav.append(_b(f"{page}/{pages}", f"rv:hsearch:{page}"))
        if page < pages:
            nav.append(_b("Next ", f"rv:hsearch:{page + 1}"))
    else:
        if page > 1:
            nav.append(_b("Prev", f"rv:hist:{filter_name}:{page - 1}"))
        nav.append(_b(f"{page}/{pages}", f"rv:hist:{filter_name}:{page}"))
        if page < pages:
            nav.append(_b("Next ", f"rv:hist:{filter_name}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_b("Search", "rv:search"), _b("Dashboard", "rv:dash")])
    await _send_or_edit(event, text, rows, edit=not send)


async def handle_review_search(event, data: dict):
    user_id = event.sender_id
    query = (event.raw_text or "").strip()[:200]
    if not query:
        await event.respond("Search text cannot be empty.")
        return
    db.set_user_state(user_id, "review_search_active", {"query": query})
    await _show_history(event, user_id, "all", 1, query=query, send=True)


async def _show_audit(event, user_id: int, page: int = 1, errors_only: bool = False):
    if not _is_admin(user_id):
        await event.edit("Audit logs are restricted to administrators.")
        return
    action = "error"if errors_only else None
    total = db.count_audit_logs(action=action)
    pages = max(1, (total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(max(1, page), pages)
    logs = db.get_audit_logs(offset=(page - 1) * REVIEW_PAGE_SIZE,
                             limit=REVIEW_PAGE_SIZE, action=action)
    lines = [
        "Error History"if errors_only else "Audit Logs",
        f"Entries: {total} | Page {page}/{pages}\n",
    ]
    for entry in logs:
        lines.append(
            f"#{entry['id']} {format_dt(entry.get('created_at'))}\n"
            f"user: {entry.get('user_id') or 'system'} | {entry.get('action')}\n"
            f"  {_snippet(entry.get('details'), 240)}"
        )
    nav = []
    base = "rv:errors"if errors_only else "rv:audit"
    if page > 1:
        nav.append(_b("Prev", f"{base}:{page - 1}"))
    nav.append(_b(f"{page}/{pages}", f"{base}:{page}"))
    if page < pages:
        nav.append(_b("Next ", f"{base}:{page + 1}"))
    rows = [nav] if nav else []
    if not errors_only:
        rows.append([_b("Errors", "rv:errors:1")])
    rows.append([_b("Dashboard", "rv:dash")])
    await _safe_edit(event, "\n".join(lines), rows)


def _csv_cell(value) -> str:
    text = ""if value is None else str(value)
    # Prevent spreadsheet formula injection in exported content.
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _write_export(user_id: int, fmt: str) -> tuple:
    data = db.get_export_data(max_rows=REVIEW_EXPORT_MAX_ROWS)
    os.makedirs(EXPORTS_FOLDER, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"review_export_{stamp}.{fmt}"
    path = os.path.join(EXPORTS_FOLDER, filename)

    if fmt == "json":
        payload = dict(data)
        for account in payload.get("accounts", []):
            account["phone"] = mask_phone(account.get("phone"))
        for item in payload.get("items", []):
            item["account_phone"] = mask_phone(item.get("account_phone"))
        payload["exported_at"] = datetime.now().isoformat(timespec="seconds")
        payload["exported_by"] = user_id
        payload["note"] = "Review records only. No session files or API credentials are included."
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
    else:
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "plan_id", "plan_status", "current_cycle", "max_cycles", "channel",
                "message_id", "message_link", "message_text", "category", "subcategory",
                "explanation", "item_status", "reviewed_by_account", "reviewed_by_phone",
                "reviewed_at", "created_at",
            ])
            plans = {plan["id"]: plan for plan in data.get("plans", [])}
            for item in data.get("items", []):
                plan = plans.get(item.get("plan_id"), {})
                writer.writerow([
                    _csv_cell(item.get("plan_id")), _csv_cell(plan.get("status")),
                    _csv_cell(plan.get("current_cycle")), _csv_cell(plan.get("max_cycles")),
                    _csv_cell(plan.get("channel_title") or plan.get("channel_identifier")),
                    _csv_cell(item.get("message_id")), _csv_cell(item.get("message_link")),
                    _csv_cell(item.get("message_text")), _csv_cell(item.get("category")),
                    _csv_cell(item.get("subcategory")), _csv_cell(item.get("explanation")),
                    _csv_cell(item.get("status")), _csv_cell(item.get("account_id")),
                    _csv_cell(mask_phone(item.get("account_phone"))),
                    _csv_cell(item.get("reviewed_at")), _csv_cell(item.get("created_at")),
                ])
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path, filename


async def _send_export(event, user_id: int, fmt: str):
    if fmt not in ("csv", "json"):
        fmt = "csv"
    try:
        path, filename = _write_export(user_id, fmt)
    except Exception as exc:
        db.log_audit(user_id, "error", f"Export failed: {redact_sensitive(exc)}")
        await event.respond(f"Export failed: {redact_sensitive(exc)}")
        return
    try:
        await event.respond(file=path, caption=f" {filename}")
        db.log_audit(user_id, "export", f"Exported {fmt.upper()} history")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Category configuration commands
# ----------------------------------------------------------------------
@bot.on(events.NewMessage(pattern=r"/review_categories$"))
async def review_categories_command(event):
    if not is_user_authorized(event.sender_id):
        await event.respond("Access Denied!")
        return
    categories = db.get_review_categories(include_disabled=True)
    lines = ["Review Categories\n"]
    for category in categories:
        subs = db.get_subcategories(category["id"], include_disabled=True)
        lines.append(
            f"{''if category['enabled'] else ''} {category['key']} - {category['label']}\n"
            f"Telegram reason: {category['telegram_reason']}\n"
            f"Subcategories: {', '.join(sub['label'] for sub in subs) or '(none)'}"
        )
    lines.append(
        "\nAdmin commands:\n"
        "/add_review_category key|label|reason|description\n"
        "/add_review_subcategory category_key|sub_key|label\n"
        "/toggle_review_category key\n"
        "/add_preset name|category_key|sub_key|explanation"
    )
    text = "\n".join(lines)
    for chunk in split_text(text):
        await event.respond(chunk)


@bot.on(events.NewMessage(pattern=r"/add_review_category\s+"))
async def add_review_category_command(event):
    user_id = event.sender_id
    if not _is_admin(user_id):
        await event.respond("Admin only command!")
        return
    raw = re.sub(r"^/add_review_category\s+", "", event.raw_text or "", flags=re.I)
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 3:
        await event.respond("Usage: /add_review_category key|label|reason|description")
        return
    key, label, reason = parts[0], parts[1], parts[2].lower()
    description = parts[3] if len(parts) > 3 else ""
    if not re.match(r"^[a-z0-9_]{2,40}$", key):
        await event.respond("Key must use lowercase letters, numbers and underscores.")
        return
    if reason not in SUPPORTED_REASON_KEYS:
        await event.respond("Unsupported Telegram reason. Allowed: " + ", ".join(SUPPORTED_REASON_KEYS))
        return
    if not label or len(label) > 80:
        await event.respond("Label must be 1-80 characters.")
        return
    if db.add_review_category(key, label, reason, description[:300]):
        db.log_audit(user_id, "category_add", f"Category {key}")
        await event.respond(f"Category '{key}'saved.")
    else:
        await event.respond("Could not save category.")


@bot.on(events.NewMessage(pattern=r"/add_review_subcategory\s+"))
async def add_review_subcategory_command(event):
    user_id = event.sender_id
    if not _is_admin(user_id):
        await event.respond("Admin only command!")
        return
    raw = re.sub(r"^/add_review_subcategory\s+", "", event.raw_text or "", flags=re.I)
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 3:
        await event.respond("Usage: /add_review_subcategory category_key|sub_key|label")
        return
    category = db.get_category_by_key(parts[0])
    if not category:
        await event.respond("Unknown category key.")
        return
    key = parts[1]
    label = parts[2]
    if not re.match(r"^[a-z0-9_]{2,40}$", key):
        await event.respond("Subcategory key must use lowercase letters, numbers and underscores.")
        return
    if db.add_review_subcategory(category["id"], key, label[:80]):
        db.log_audit(user_id, "subcategory_add", f"{parts[0]}/{key}")
        await event.respond(f"Subcategory '{key}'saved.")
    else:
        await event.respond("Could not save subcategory.")


@bot.on(events.NewMessage(pattern=r"/toggle_review_category(?:\s+(\S+))?$"))
async def toggle_review_category_command(event):
    user_id = event.sender_id
    if not _is_admin(user_id):
        await event.respond("Admin only command!")
        return
    match = re.match(r"/toggle_review_category(?:\s+(\S+))?$", event.raw_text or "")
    key = match.group(1) if match else None
    if not key:
        await event.respond("Usage: /toggle_review_category key")
        return
    if db.toggle_review_category(key):
        db.log_audit(user_id, "category_toggle", key)
        await event.respond(f"Category '{key}'toggled.")
    else:
        await event.respond("Unknown category key.")


@bot.on(events.NewMessage(pattern=r"/add_preset\s+"))
async def add_preset_command(event):
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        await event.respond("Access Denied!")
        return
    raw = re.sub(r"^/add_preset\s+", "", event.raw_text or "", flags=re.I)
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) < 3:
        await event.respond("Usage: /add_preset name|category_key|sub_key|optional explanation")
        return
    name, category_key, sub_key = parts[0], parts[1], parts[2]
    explanation = parts[3] if len(parts) > 3 else ""
    category = db.get_category_by_key(category_key)
    if not category:
        await event.respond("Unknown category key.")
        return
    subcategory_id = None
    if sub_key:
        match = next((s for s in db.get_subcategories(category["id"], include_disabled=True)
                      if s["key"] == sub_key), None)
        if not match:
            await event.respond("Unknown subcategory key for this category.")
            return
        subcategory_id = match["id"]
    if db.create_preset(name, category["id"], subcategory_id, explanation, user_id):
        db.log_audit(user_id, "preset_add", name)
        await event.respond(f"Preset '{name}'saved.")
    else:
        await event.respond("Could not save preset.")


# ----------------------------------------------------------------------
# Callback router
# ----------------------------------------------------------------------
async def handle_review_callback(event, data: str):
    try:
        await event.answer()
    except Exception:
        pass
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        await event.edit("Access Denied!")
        return
    try:
        await _route_callback(event, user_id, data)
    except Exception as exc:
        error = redact_sensitive(exc)
        db.log_audit(user_id, "error", f"Review callback {data[:60]} failed: {error}")
        try:
            await event.answer("Something went wrong. Check audit logs.", alert=True)
        except Exception:
            pass


async def _route_callback(event, user_id: int, data: str):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "dash":
        await show_dashboard(event, send=False)
    elif action == "new":
        await _start_new_plan(event, user_id)
    elif action == "accounts":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await _show_accounts(event, page)
    elif action == "atoggle"and len(parts) > 2:
        await _toggle_account(event, user_id, int(parts[2]))
    elif action == "acheck"and len(parts) > 2:
        await _check_account(event, user_id, int(parts[2]))
    elif action == "imp":
        if not _require_admin(user_id):
            await event.edit("Session imports are restricted to administrators.")
            return
        db.set_user_state(user_id, "review_import_upload", {"step": 1})
        await _safe_edit(
            event,
            "Send one .session file or a ZIP with multiple .session files.\n"
            f"Limits: {REVIEW_MAX_IMPORT_SESSIONS} sessions, {REVIEW_MAX_ZIP_MB} MB. "
            "Use /cancel to abort.",
            [[_b("Cancel", "rv:dash")]],
        )
    elif action == "impconfirm":
        await _confirm_import(event, user_id)
    elif action == "impcancel":
        _cancel_import(user_id)
        await event.edit("Session import cancelled.", buttons=[[_b("Dashboard", "rv:dash")]])

    elif action == "plans":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await _show_plans(event, user_id, page)
    elif action == "plan"and len(parts) > 2:
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        await _show_plan(event, user_id, int(parts[2]), page)

    elif action == "tsel"and len(parts) > 2:
        await _candidate_toggle(event, user_id, int(parts[2]))
    elif action == "tpage"and len(parts) > 3:
        await _show_candidates(event, int(parts[2]), int(parts[3]))
    elif action == "tall"and len(parts) > 3:
        plan = db.get_review_plan(int(parts[2]))
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        db.set_all_review_candidates(int(parts[2]), parts[3] == "1")
        await _show_candidates(event, int(parts[2]), 1)
    elif action == "tcont"and len(parts) > 2:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        if db.count_review_candidates(plan_id, selected_only=True) == 0:
            await event.answer("Select at least one post.", alert=True)
            return
        db.remove_unselected_candidates(plan_id)
        db.activate_selected_candidates(plan_id)
        db.log_audit(user_id, "plan_messages_selected", f"Plan #{plan_id}")
        await _show_item_config(event, plan_id)

    elif action == "cfg"and len(parts) > 2:
        await _show_item_config(event, int(parts[2]))

    elif action == "cat"and len(parts) > 3:
        item_id, category_id = int(parts[2]), int(parts[3])
        item = db.get_review_item(item_id)
        if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
            await event.answer("Item not found.", alert=True)
            return
        db.set_item_category(item_id, category_id)
        await _show_subcategories(event, item_id, category_id)

    elif action == "sub"and len(parts) > 3:
        item_id, sub_id = int(parts[2]), int(parts[3])
        sub = db.get_subcategory_by_id(sub_id)
        item = db.get_review_item(item_id)
        if not sub or not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
            await event.answer("Subcategory not found.", alert=True)
            return
        db.set_item_subcategory(item_id, sub_id)
        await _ask_explanation(event, item_id)

    elif action == "expskip"and len(parts) > 2:
        item_id = int(parts[2])
        item = db.get_review_item(item_id)
        if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
            await event.answer("Item not found.", alert=True)
            return
        db.set_item_explanation(item_id, "", configured=True)
        db.clear_user_state(user_id)
        await _after_item_configured(event, item["plan_id"])

    elif action == "preset"and len(parts) > 3:
        item_id, preset_id = int(parts[2]), int(parts[3])
        item = db.get_review_item(item_id)
        if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
            await event.answer("Item not found.", alert=True)
            return
        if db.apply_preset_to_item(item_id, preset_id):
            await _after_item_configured(event, item["plan_id"])
        else:
            await event.answer("Preset not found.", alert=True)

    elif action == "int"and len(parts) > 3:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        try:
            hours = float(parts[3])
        except ValueError:
            await event.answer("Invalid interval.", alert=True)
            return
        if hours < 0.1 or hours > 168:
            await event.answer("Interval must be 0.1-168 hours.", alert=True)
            return
        db.set_review_plan_settings(plan_id, hours, plan["max_cycles"])
        await _show_settings(event, plan_id, "cycles")

    elif action == "intcustom"and len(parts) > 2:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        db.set_user_state(user_id, "review_interval_custom", {"plan_id": plan_id})
        await _safe_edit(
            event,
            "Send the reminder interval in hours (0.1-168). Example: 2",
            [[_b("Back", f"rv:cfg:{plan_id}")]],
        )

    elif action == "cyc"and len(parts) > 3:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        try:
            cycles = int(parts[3])
        except ValueError:
            await event.answer("Invalid cycle count.", alert=True)
            return
        if cycles < 1 or cycles > 100:
            await event.answer("Cycles must be 1-100.", alert=True)
            return
        db.set_review_plan_settings(plan_id, plan["interval_hours"], cycles)
        await _show_plan_preview(event, plan_id)

    elif action == "cyccustom"and len(parts) > 2:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        db.set_user_state(user_id, "review_cycles_custom", {"plan_id": plan_id})
        await _safe_edit(
            event,
            "Send the maximum number of review cycles (1-100). Example: 5",
            [[_b("Back", f"rv:cfg:{plan_id}")]],
        )

    elif action == "confirm"and len(parts) > 2:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        if not db.finalize_review_plan(plan_id):
            await event.edit(
                "Plan cannot be confirmed. Every selected message needs a category, subcategory and saved explanation/skip.",
                buttons=[[_b("Configure", f"rv:cfg:{plan_id}")]],
            )
            return
        db.log_audit(user_id, "plan_ready", f"Plan #{plan_id}")
        await _show_plan(event, user_id, plan_id, 1)

    elif action == "cancel"and len(parts) > 2:
        plan_id = int(parts[2])
        if db.delete_review_draft(plan_id, user_id):
            db.log_audit(user_id, "plan_cancelled", f"Draft #{plan_id}")
            await show_dashboard(event, send=False)
        else:
            await event.edit("Only drafts can be cancelled here.")

    elif action == "start"and len(parts) > 2:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        if db.start_review_plan(plan_id):
            db.log_audit(user_id, "plan_start", f"Plan #{plan_id}")
            await event.answer("Plan started")
        else:
            await event.answer("Only a ready plan can be started.", alert=True)
        await _show_plan(event, user_id, plan_id, 1)

    elif action == "pause"and len(parts) > 2:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        if db.pause_review_plan(plan_id):
            db.log_audit(user_id, "plan_pause", f"Plan #{plan_id}")
            await event.answer("Paused")
        else:
            await event.answer("Plan is not active.", alert=True)
        await _show_plan(event, user_id, plan_id, 1)

    elif action == "resume"and len(parts) > 2:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        if db.resume_review_plan(plan_id):
            db.log_audit(user_id, "plan_resume", f"Plan #{plan_id}")
            await event.answer("Resumed")
        else:
            await event.answer("Plan is not paused.", alert=True)
        await _show_plan(event, user_id, plan_id, 1)

    elif action == "stop"and len(parts) > 2:
        plan_id = int(parts[2])
        plan = db.get_review_plan(plan_id)
        if not _can_access_plan(user_id, plan):
            await event.answer("Plan not found.", alert=True)
            return
        if db.stop_review_plan(plan_id):
            db.log_audit(user_id, "plan_stop", f"Plan #{plan_id}")
            await event.answer("Stopped")
        else:
            await event.answer("Plan cannot be stopped.", alert=True)
        await _show_plan(event, user_id, plan_id, 1)

    elif action == "mark"and len(parts) > 2:
        await _show_reviewer_accounts(event, user_id, int(parts[2]))
    elif action == "acct"and len(parts) > 3:
        item_id, account_id = int(parts[2]), int(parts[3])
        item = db.get_review_item(item_id)
        if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
            await event.answer("Item not found.", alert=True)
            return
        if account_id and not db.get_account_by_id(account_id):
            await event.answer("Account not found.", alert=True)
            return
        db.mark_item_reviewed(item_id, account_id or None)
        db.log_audit(user_id, "item_reviewed", f"Item #{item_id}, account #{account_id or 'unassigned'}")
        await event.answer("Marked reviewed")
        await _show_plan(event, user_id, item["plan_id"], 1)
    elif action == "unmark"and len(parts) > 2:
        item_id = int(parts[2])
        item = db.get_review_item(item_id)
        if not item or not _can_access_plan(user_id, db.get_review_plan(item["plan_id"])):
            await event.answer("Item not found.", alert=True)
            return
        db.mark_item_pending(item_id)
        db.log_audit(user_id, "item_pending", f"Item #{item_id}")
        await event.answer("Marked pending")
        await _show_plan(event, user_id, item["plan_id"], 1)

    elif action == "hist"and len(parts) > 3:
        filter_name = parts[2] if parts[2] in ("all", "pending", "reviewed") else "all"
        page = int(parts[3]) if parts[3].isdigit() else 1
        db.clear_user_state(user_id)
        await _show_history(event, user_id, filter_name, page)
    elif action == "hsearch"and len(parts) > 2:
        state, state_data = db.get_user_state(user_id)
        query = state_data.get("query") if state == "review_search_active"else None
        page = int(parts[2]) if parts[2].isdigit() else 1
        await _show_history(event, user_id, "all", page, query=query)
    elif action == "search":
        db.set_user_state(user_id, "review_search", {"step": 1})
        await _safe_edit(event, "Send the text to search in review history:")
    elif action == "audit":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await _show_audit(event, user_id, page, errors_only=False)
    elif action == "errors":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await _show_audit(event, user_id, page, errors_only=True)
    elif action in ("csv", "json"):
        if not _is_admin(user_id):
            await event.answer("Admin only.", alert=True)
            return
        await _send_export(event, user_id, action)
    else:
        await event.answer("Unknown action.", alert=True)


async def _show_plans(event, user_id: int, page: int = 1):
    total = db.count_review_plans()
    pages = max(1, (total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
    page = min(max(1, page), pages)
    plans = db.get_review_plans(offset=(page - 1) * REVIEW_PAGE_SIZE, limit=REVIEW_PAGE_SIZE)
    lines = [
        "Review Plans",
        f"Total: {total} | Page {page}/{pages}\n",
    ]
    for plan in plans:
        lines.append(
            f"#{plan['id']} {_status_icon(plan['status'])} {plan['status']} - "
            f"{plan.get('channel_title') or plan.get('channel_identifier') or 'channel'}\n"
            f"items: {plan.get('reviewed_count', 0)}/{plan.get('item_count', 0)} reviewed | "
            f"cycle {plan.get('current_cycle') or 0}/{plan.get('max_cycles') or 0}\n"
            f"next: {format_dt(plan.get('next_review_at'))}"
        )
    rows = []
    for plan in plans:
        rows.append([_b(f"#{plan['id']} {plan['status']} - {_snippet(plan.get('channel_title') or plan.get('channel_identifier'), 26)}",
                        f"rv:plan:{plan['id']}:1")])
    nav = []
    if page > 1:
        nav.append(_b("Prev", f"rv:plans:{page - 1}"))
    nav.append(_b(f"{page}/{pages}", f"rv:plans:{page}"))
    if page < pages:
        nav.append(_b("Next ", f"rv:plans:{page + 1}"))
    rows.append(nav)
    rows.append([_b("New Plan", "rv:new"), _b("Dashboard", "rv:dash")])
    await _safe_edit(event, "\n".join(lines), rows)
