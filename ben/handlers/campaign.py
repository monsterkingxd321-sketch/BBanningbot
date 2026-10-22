"""Report campaign wizard and dashboard.

A campaign reports every stored message link with a real Telegram reason and,
when Telegram returns them, real sub-options. It can use one account or a
selected set of active accounts, run once or repeat on a fixed interval, and
stop at a selected total duration. The campaign scheduler performs the automatic
runs and completes the campaign at its deadline.
"""

from datetime import datetime, timedelta

from telethon import Button, events

from bot import bot, db, active_campaigns
from config import (
    CAMPAIGN_DEFAULT_DURATION_HOURS,
    CAMPAIGN_DEFAULT_INTERVAL_HOURS,
    CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE,
    CAMPAIGN_MAX_POSTS,
)
from utils.helpers import log_activity, mask_phone, scrape_recent_messages
from utils.report_options import load_report_choices
from utils.review_utils import format_dt, parse_iso, redact_sensitive
from handlers.report_ui import (
    REPORT_REASON_CHOICES,
    reason_display,
    reason_key_from_label,
    report_option_buttons,
)

CAMPAIGN_STATE = "campaign_flow"
STATUS_LABELS = {
    "ready": "Ready",
    "active": "Active",
    "running": "Running",
    "paused": "Paused",
    "stopped": "Stopped",
    "completed": "Completed",
}


async def _render(event, text, buttons=None):
    """Render in a callback message, or as a new message for text updates."""
    # A text message also exposes an edit method, but the bot cannot edit an
    # incoming user message. Only callback events should use edit.
    if hasattr(event, "edit") and not hasattr(event, "raw_text"):
        try:
            await event.edit(text, buttons=buttons)
            return
        except Exception:
            pass
    if hasattr(event, "respond"):
        await event.respond(text, buttons=buttons)


def _main_button():
    return Button.inline("Main Menu", b"menu:home")


def _cancel_button():
    return Button.inline("Cancel", b"cmp:cancel")


def _campaign_buttons(user_id: int = None):
    buttons = []
    if user_id:
        draft = _get_flow(user_id)
        if draft and (draft.get("target") or draft.get("messages")) and draft.get("step") != "name":
            target_name = draft.get("name") or draft.get("target") or "Draft"
            buttons.append([Button.inline(f"Resume Draft ({target_name[:20]})", b"cmp:resume_draft")])
    buttons.append([Button.inline("New Campaign", b"cmp:new")])
    buttons.append([Button.inline("My Campaigns", b"cmp:list:0"),
         Button.inline("Report Options", b"cmp:options")])
    buttons.append([_main_button()])
    return buttons


def _save_flow(user_id: int, data: dict):
    db.set_user_state(user_id, CAMPAIGN_STATE, data)


def _get_flow(user_id: int):
    state, data = db.get_user_state(user_id)
    if state != CAMPAIGN_STATE or not isinstance(data, dict):
        return None
    return data


def _require_flow(event, user_id):
    data = _get_flow(user_id)
    if not data:
        return None
    return data


def _account_count(data: dict) -> int:
    phones = data.get("selected_phones") or []
    if data.get("accounts_mode") == "all" or not phones:
        return len(db.get_active_accounts())
    return len(phones)


def _dist_text(dist_mode: str) -> str:
    if dist_mode == "round_robin":
        return "1 account per link (Round-Robin)"
    return "All accounts to all links (Full Mass)"


def _total_reports(data: dict) -> int:
    msgs = len(data.get("messages") or [])
    rpm = int(data.get("reports_per_message") or CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE)
    accs = max(1, _account_count(data))
    dist = data.get("dist_mode") or "all"
    if dist == "round_robin" and msgs > 0:
        total_assigned = msgs if accs <= msgs else accs
        return total_assigned * rpm
    else:
        return (msgs if msgs > 0 else 1) * rpm * accs


def _interval_text(hours) -> str:
    hours = float(hours or 0)
    if hours <= 0:
        return "One time"
    if hours == 1:
        return "Every hour"
    if hours == int(hours):
        return f"Every {int(hours)} hours"
    return f"Every {hours:g} hours"


DURATION_OPTIONS = [
    (24, "1 day"),
    (48, "2 days"),
    (72, "3 days"),
    (168, "7 days"),
    (720, "30 days"),
]


def _duration_text(hours, one_time: bool = False) -> str:
    if one_time:
        return "Single run"
    hours = float(hours or 0)
    if hours <= 0:
        return "Unlimited until stopped"
    if hours == 24:
        return "1 day"
    if hours > 0 and hours % 24 == 0:
        days = int(hours // 24)
        return f"{days} day" if days == 1 else f"{days} days"
    if hours == int(hours):
        return f"{int(hours)} hours"
    return f"{hours:g} hours"


def _parse_duration(value: str):
    """Parse a custom duration. Plain numbers are days; 12h means hours."""
    text = (value or "").strip().lower().replace(",", ".")
    if not text:
        return None
    multiplier = 24.0
    if text.endswith("h") or "hour" in text:
        multiplier = 1.0
    text = text.replace("hours", "").replace("hour", "").replace("days", "")
    text = text.replace("day", "").replace("h", "").replace("d", "").strip()
    try:
        amount = float(text)
    except ValueError:
        return None
    if amount < 0:
        return None
    return amount * multiplier


async def show_campaign_menu(event, send: bool = False):
    user_id = event.sender_id
    if not db.is_admin(user_id):
        text = "Reporting campaigns are available to admins only."
        if send:
            await event.respond(text, buttons=[[_main_button()]])
        else:
            try:
                await _render(event, text, buttons=[[_main_button()]])
            except Exception:
                await event.respond(text, buttons=[[_main_button()]])
        return

    campaigns = db.list_report_campaigns(created_by=user_id, limit=100)
    active = sum(1 for c in campaigns if c.get("status") in ("active", "ready", "running"))
    text = (
        "**Reporting Campaigns**\n\n"
        "Build a campaign once, then let the scheduler run it.\n\n"
        f"Total campaigns: {len(campaigns)}\n"
        f"Active or scheduled: {active}\n"
        f"Maximum links per campaign: {CAMPAIGN_MAX_POSTS}\n\n"
        "Each campaign stores the target, message links, account selection, "
        "reports per link, delay, one or more real Telegram report types, "
        "optional real sub-options, comment, repeat interval and total run "
        "duration. Add another reason to send different report types on the "
        "same message."
    )
    if send:
        await event.respond(text, buttons=_campaign_buttons(user_id))
    else:
        try:
            await _render(event, text, buttons=_campaign_buttons(user_id))
        except Exception:
            await event.respond(text, buttons=_campaign_buttons(user_id))


async def show_report_options(event, back_data: bytes = None):
    lines = [
        "**Telegram Report Options**",
        "",
        "Telegram accepts exactly these ten real report reason types. When a "
        "reason has further options, the bot asks Telegram for that exact list "
        "and shows it. Local sub-options are not invented.",
        "",
    ]
    for key, label, description in REPORT_REASON_CHOICES:
        lines.append(f"**{label}** (`{key}`)")
        lines.append(description)
        lines.append("")
    lines.extend([
        "The bot sends the selected real Telegram option, the message IDs and "
        "the optional comment. It never invents unsupported API fields.",
    ])
    buttons = [
        [Button.inline("New Campaign", b"cmp:new")],
        [Button.inline("Campaigns", b"cmp:menu"), _main_button()],
    ]
    if back_data:
        buttons.insert(0, [Button.inline("Back", back_data)])
    await _render(event, 
        "\n".join(lines),
        buttons=buttons,
    )


def _name_prompt():
    return (
        "**Campaign Setup: 1 of 10 - Name**\n\n"
        "Send a name for this campaign, or skip to use the target as the name.\n\n"
        "The wizard then asks for the target channel, accounts, message links, "
        "reports per link, delay, main reason, sub-option, comment, repeat "
        "interval and total run duration."
    )


async def show_name_prompt(event, send: bool = True):
    buttons = [
        [Button.inline("Skip Name", b"cmp:name_skip")],
        [_cancel_button()],
    ]
    if send:
        await event.respond(_name_prompt(), buttons=buttons)
    else:
        await _render(event, _name_prompt(), buttons=buttons)


def _target_prompt(data: dict):
    name = data.get("name") or "Untitled campaign"
    return (
        f"**Campaign Setup: 2 of 10 - Target**\n\nCampaign: {name}\n\n"
        "Send the target channel username or link.\n"
        "Examples: `@channel_name`, `https://t.me/channel_name`, "
        "or a private invite link the selected account can join."
    )


async def show_target_prompt(event, data: dict):
    await _render(event, 
        _target_prompt(data),
        buttons=[
            [Button.inline("Change Name", b"cmp:name_edit")],
            [_cancel_button()],
        ],
    )


def _accounts_prompt_text(data: dict) -> str:
    return (
        "**Campaign Setup: 3 of 10 - Accounts**\n\n"
        f"Target: `{data.get('target')}`\n\n"
        "Choose one account, several accounts, or all active accounts. "
        "Only active accounts can report. The report engine still checks that "
        "each account has joined the target before sending reports."
    )


async def show_accounts_prompt(event, data: dict):
    active = db.get_active_accounts()
    if not active:
        await _render(event, 
            "**No Active Accounts**\n\n"
            "Log in or import at least one active account before creating a campaign.",
            buttons=[
                [Button.inline("Login / Import", b"menu:login")],
                [Button.inline("Back", b"cmp:back_target"), _cancel_button()],
            ],
        )
        return

    buttons = [
        [Button.inline(f"Use All Active Accounts ({len(active)})", b"cmp:acc_all")],
        [Button.inline("Select Multiple Accounts", b"cmp:acc_multi")],
    ]
    for account in active[:30]:
        phone = account[1]
        buttons.append([Button.inline(f"Only {mask_phone(phone)}", f"cmp:acc_only:{phone}".encode())])
    if len(active) > 30:
        buttons.append([Button.inline("Only first 30 accounts shown; use multiple selection", b"cmp:acc_multi")])
    buttons.append([Button.inline("Back", b"cmp:back_target"), _cancel_button()])
    await _render(event, _accounts_prompt_text(data), buttons=buttons)


async def show_multi_accounts(event, data: dict):
    active = db.get_active_accounts()
    selected = set(data.get("selected_phones") or [])
    if not active:
        await show_accounts_prompt(event, data)
        return
    buttons = []
    for account in active[:30]:
        phone = account[1]
        label = ("[X] " if phone in selected else "[ ] ") + mask_phone(phone)
        buttons.append([Button.inline(label, f"cmp:acc_toggle:{phone}".encode())])
    buttons.append([
        Button.inline(f"Done ({len(selected)} selected)", b"cmp:acc_done"),
        Button.inline("Use All", b"cmp:acc_all"),
    ])
    buttons.append([Button.inline("Back", b"cmp:back_accounts"), _cancel_button()])
    await _render(event, 
        "**Campaign Setup: 3 of 10 - Select Accounts**\n\n"
        f"Target: `{data.get('target')}`\n\n"
        "Tap accounts to include or remove them, then tap Done. "
        "At least one account is required.",
        buttons=buttons,
    )


def _fetch_prompt_text(data: dict) -> str:
    return (
        "**Campaign Setup: 4 of 10 - Message Links**\n\n"
        f"Target: `{data.get('target')}`\n\n"
        "The selected account fetches the latest posts and stores every message link. "
        "Choosing Fetch All uses the largest allowed limit.\n\n"
        f"Maximum links: {CAMPAIGN_MAX_POSTS}. "
        "Telegram does not provide an unlimited single request; "
        "this is the configured fetch limit for the campaign."
    )


async def show_fetch_prompt(event, data: dict):
    presets = [value for value in (50, 100, 200, 500) if value < CAMPAIGN_MAX_POSTS]
    buttons = [[Button.inline(f"Fetch All (max {CAMPAIGN_MAX_POSTS})", b"cmp:fetch_all")]]
    row = []
    for value in presets:
        row.append(Button.inline(str(value), f"cmp:fetch:{value}".encode()))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([Button.inline("Custom Count", b"cmp:fetch_custom")])
    buttons.append([Button.inline("Back", b"cmp:back_accounts"), _cancel_button()])
    await _render(event, _fetch_prompt_text(data), buttons=buttons)


async def _fetch_links(event, user_id: int, data: dict, limit: int):
    if limit < 1:
        if hasattr(event, "answer"):
            await event.answer("Choose a valid count.", alert=True)
        return
    limit = min(limit, CAMPAIGN_MAX_POSTS)
    target = data.get("target") or ""
    phones = data.get("selected_phones") or []
    phone = phones[0] if phones else None
    await _render(event, 
        "**Fetching Message Links**\n\n"
        f"Target: `{target}`\n"
        f"Requested: {limit} recent posts\n"
        f"Fetch account: `{mask_phone(phone) if phone else 'first active account'}`\n\n"
        "This can take a moment."
    )
    try:
        links = await scrape_recent_messages(target, limit=limit, phone=phone)
    except Exception as exc:
        error = redact_sensitive(exc, 300)
        log_activity(f"Campaign fetch failed for user {user_id}: {error}")
        data["step"] = "fetch"
        _save_flow(user_id, data)
        await _render(event, 
            "**Fetch Failed**\n\n"
            f"`{error}`\n\n"
            "Check the target, the selected account and its access to the channel, then retry.",
            buttons=[
                [Button.inline("Retry", b"cmp:back_fetch")],
                [Button.inline("Change Target", b"cmp:back_target"), _cancel_button()],
            ],
        )
        return

    unique = []
    seen = set()
    for link in links or []:
        if link and link not in seen:
            unique.append(link)
            seen.add(link)
    if not unique:
        data["step"] = "fetch"
        _save_flow(user_id, data)
        await _render(event, 
            "**No Message Links Found**\n\n"
            "The account found no posts it can access. Try another target or account.",
            buttons=[
                [Button.inline("Retry", b"cmp:back_fetch")],
                [Button.inline("Change Target", b"cmp:back_target"), _cancel_button()],
            ],
        )
        return

    data["messages"] = unique
    data["step"] = "reports"
    _save_flow(user_id, data)
    await show_reports_prompt(event, data)


def _reports_prompt_text(data: dict) -> str:
    return (
        "**Campaign Setup: 5 of 10 - Reports Per Link**\n\n"
        f"Links fetched: {len(data.get('messages') or [])}\n"
        f"Accounts: {_account_count(data)}\n\n"
        "Choose how many reports each account sends for every message link. "
        f"Default and recommended: {CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE}. "
        "With one account and two reports per link, every link receives exactly "
        "two reports. If you add two report types, the two reports use different "
        "real Telegram reasons."
    )


async def show_reports_prompt(event, data: dict):
    buttons = [
        [Button.inline("Use 2 (default)", b"cmp:rpm:2"),
         Button.inline("1", b"cmp:rpm:1")],
        [Button.inline("3", b"cmp:rpm:3"),
         Button.inline("5", b"cmp:rpm:5")],
        [Button.inline("10", b"cmp:rpm:10")],
        [Button.inline("Custom", b"cmp:rpm_custom")],
        [Button.inline("Back", b"cmp:back_fetch"), _cancel_button()],
    ]
    await _render(event, _reports_prompt_text(data), buttons=buttons)


def _delay_prompt_text(data: dict) -> str:
    return (
        "**Campaign Setup: 6 of 10 - Delay**\n\n"
        f"Reports per link: {data.get('reports_per_message')}\n"
        f"Reports per cycle: {_total_reports(data)}\n\n"
        "Choose the delay between individual report requests. "
        "A longer delay reduces FloodWait errors."
    )


async def show_delay_prompt(event, data: dict):
    buttons = [
        [Button.inline("5 seconds (default)", b"cmp:delay:5"),
         Button.inline("10 seconds", b"cmp:delay:10")],
        [Button.inline("30 seconds", b"cmp:delay:30"),
         Button.inline("60 seconds", b"cmp:delay:60")],
        [Button.inline("Custom", b"cmp:delay_custom")],
        [Button.inline("Back", b"cmp:back_reports"), _cancel_button()],
    ]
    await _render(event, _delay_prompt_text(data), buttons=buttons)


def _dist_prompt_text(data: dict) -> str:
    accs = _account_count(data)
    msgs = len(data.get("messages") or [])
    rpm = int(data.get("reports_per_message") or CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE)
    all_tot = (msgs if msgs > 0 else 1) * rpm * accs
    rr_tot = (msgs if accs <= msgs else accs) * rpm if msgs > 0 else accs * rpm
    return (
        "**Campaign Setup: 7 of 11 - Message Distribution**\n\n"
        f"Accounts: {accs}\n"
        f"Message Links: {msgs}\n"
        f"Reports per link: {rpm}\n\n"
        "Choose how reporting accounts report the message links:\n\n"
        "1. **All Accounts to All Links (Full Mass)**\n"
        f"Every active account reports every link ({all_tot} total reports).\n\n"
        "2. **1 Account per Link (Round-Robin / Spread)**\n"
        f"Account 1 reports Link 1, Account 2 reports Link 2, etc. ({rr_tot} total reports)."
    )


async def show_dist_prompt(event, data: dict):
    buttons = [
        [Button.inline("All Accounts to All Links (Full Mass)", b"cmp:dist:all")],
        [Button.inline("1 Account per Link (Round-Robin)", b"cmp:dist:round_robin")],
        [Button.inline("Back", b"cmp:back_delay"), _cancel_button()],
    ]
    await _render(event, _dist_prompt_text(data), buttons=buttons)


def _reason_prompt_text(data: dict) -> str:
    return (
        "**Campaign Setup: 8 of 11 - Main Reason**\n\n"
        "These are the exact report reasons returned by Telegram for this "
        "target. If Telegram returns more options after a reason is chosen, "
        "the next screen shows that exact list too.\n\n"
        f"Selected distribution: {_dist_text(data.get('dist_mode'))}"
    )


MAX_REASON_CHOICES = 10


def _reason_choices(data: dict) -> list:
    choices = data.get("reason_choices")
    return list(choices) if isinstance(choices, list) else []


def _reason_choice_text(choice: dict) -> str:
    return reason_display(choice.get("reason"), choice.get("detail"))


def _reason_choices_text(data: dict) -> str:
    choices = _reason_choices(data)
    if not choices:
        return "No report type selected."
    lines = []
    for index, choice in enumerate(choices, 1):
        txt = f"{index}. {_reason_choice_text(choice)}"
        comm = (choice.get("comment") or "").strip()
        if comm:
            txt += f" | Comment: {comm[:60]}"
        lines.append(txt)
    return "\n".join(lines)


def _append_reason_choice(data: dict, reason: str, detail: str,
                          option: str, comment_required: bool = False,
                          comment: str = "") -> bool:
    """Add one real Telegram reason chain to the campaign."""
    choices = _reason_choices(data)
    candidate = {
        "reason": reason or "Other",
        "detail": detail or "",
        "option": option or "",
        "comment_required": bool(comment_required),
        "comment": comment or "",
    }
    for existing in choices:
        if (
            existing.get("reason") == candidate["reason"]
            and existing.get("detail") == candidate["detail"]
        ):
            return False
    if len(choices) >= MAX_REASON_CHOICES:
        return False
    choices.append(candidate)
    data["reason_choices"] = choices
    data["reports_per_message"] = max(
        int(data.get("reports_per_message") or 2), len(choices)
    )
    data["comment_required"] = any(
        bool(item.get("comment_required")) for item in choices
    )
    first = choices[0]
    data["reason"] = first.get("reason") or "Other"
    data["reason_detail"] = first.get("detail") or ""
    data["reason_option"] = first.get("option") or ""
    return True


def _remove_reason_choice(data: dict) -> bool:
    choices = _reason_choices(data)
    if not choices:
        return False
    choices.pop()
    data["reason_choices"] = choices
    data["comment_required"] = any(
        bool(item.get("comment_required")) for item in choices
    )
    if choices:
        first = choices[0]
        data["reason"] = first.get("reason") or "Other"
        data["reason_detail"] = first.get("detail") or ""
        data["reason_option"] = first.get("option") or ""
    else:
        data["reason"] = ""
        data["reason_detail"] = ""
        data["reason_option"] = ""
    return True


async def show_reason_choices(event, data: dict):
    """Show the report types already added and allow more or continue."""
    choices = _reason_choices(data)
    if not choices:
        await show_reason_prompt(event, data)
        return
    data["step"] = "reason_choices"
    _save_flow(event.sender_id, data)
    text = (
        "**Campaign Setup: 7 of 10 - Report Types**\n\n"
        f"{_reason_choices_text(data)}\n\n"
        f"Reports per link: {data.get('reports_per_message')}\n"
        "Each report number uses the next report type. Add another type to "
        "send different reports on the same message. The reports per link "
        "count was raised automatically when needed."
    )
    buttons = []
    if len(choices) < MAX_REASON_CHOICES:
        buttons.append([Button.inline("Add Another Reason", b"cmp:radd")])
    buttons.append([Button.inline("Continue", b"cmp:rcontinue")])
    buttons.append([Button.inline("Remove Last", b"cmp:rremove")])
    buttons.append([Button.inline("Back to Reasons", b"cmp:back_reason"),
                    _cancel_button()])
    await _render(event, text, buttons=buttons)


async def show_reason_prompt(event, data: dict):
    """Load the exact first page of report reasons from Telegram."""
    target = data.get("target") or ""
    messages = data.get("messages") or []
    if not messages:
        data["step"] = "fetch"
        _save_flow(event.sender_id, data)
        await show_fetch_prompt(event, data)
        return

    phone = (data.get("selected_phones") or [None])[0]
    await _render(
        event,
        "**Loading Telegram Report Reasons**\n\n"
        "Asking Telegram for the exact reasons available for this target.",
        buttons=[[Button.inline("Cancel", b"cmp:cancel")]],
    )
    result = await load_report_choices(phone, target, messages[0], option_bytes=b"")

    if result.get("kind") == "choose" and result.get("options"):
        data["reason_pages"] = [result["options"]]
        data["reason_page"] = 0
        data["reason"] = ""
        data["reason_detail"] = ""
        data["reason_option"] = ""
        data["reason_pending_option"] = ""
        data["comment_required"] = any(
            bool(item.get("comment_required")) for item in _reason_choices(data)
        )
        data["step"] = "reason"
        _save_flow(event.sender_id, data)
        buttons = report_option_buttons(result["options"], "cmp:ropt")
        buttons.append([Button.inline("Explain All Options", b"cmp:options")])
        buttons.append([Button.inline("Try Again", b"cmp:rretry"),
                        Button.inline("Back", b"cmp:back_dist")])
        buttons.append([_cancel_button()])
        await _render(event, _reason_prompt_text(data), buttons=buttons)
        return

    standard_options = [
        {"text": "Spam", "option": ""},
        {"text": "Fake Account / Scam", "option": ""},
        {"text": "Violence", "option": ""},
        {"text": "Pornography", "option": ""},
        {"text": "Child Abuse", "option": ""},
        {"text": "Illegal Drugs", "option": ""},
        {"text": "Personal Details", "option": ""},
        {"text": "Copyright", "option": ""},
    ]
    data["reason_pages"] = [standard_options]
    data["reason_page"] = 0
    data["reason"] = ""
    data["reason_detail"] = ""
    data["reason_option"] = ""
    data["reason_pending_option"] = ""
    data["step"] = "reason"
    _save_flow(event.sender_id, data)
    buttons = report_option_buttons(standard_options, "cmp:ropt")
    buttons.append([Button.inline("Explain All Options", b"cmp:options")])
    buttons.append([Button.inline("Back", b"cmp:back_dist"), _cancel_button()])
    await _render(
        event,
        "**Telegram Report Reasons**\n\n"
        "Choose a report reason below (select 2 types to send 2 reports per message):",
        buttons=buttons,
    )


async def show_stored_reason_page(event, data: dict):
    """Show a saved Telegram reason page without another network request."""
    pages = data.get("reason_pages") or []
    page_index = max(0, min(int(data.get("reason_page") or 0), len(pages) - 1))
    options = pages[page_index] if pages else []
    if not options:
        await show_reason_prompt(event, data)
        return
    data["reason_page"] = page_index
    data["step"] = "reason" if page_index == 0 else "reason_sub"
    _save_flow(event.sender_id, data)
    if page_index == 0:
        text = _reason_prompt_text(data)
        prefix = "cmp:ropt"
    else:
        text = (
            "**Campaign Setup: 7 of 10 - Telegram Options**\n\n"
            f"Reason: {reason_display(data.get('reason'))}\n\n"
            "These are the exact sub-options returned by Telegram. Choose one "
            "or continue with the main reason."
        )
        prefix = "cmp:rsub"
    buttons = report_option_buttons(options, prefix)
    if page_index == 0:
        buttons.append([Button.inline("Explain All Options", b"cmp:options")])
        buttons.append([Button.inline("Try Again", b"cmp:rretry"),
                        Button.inline("Back", b"cmp:back_dist")])
    else:
        buttons.append([Button.inline("Continue with main reason", b"cmp:rskip")])
        buttons.append([Button.inline("Back", b"cmp:back_reason")])
    buttons.append([_cancel_button()])
    await _render(event, text, buttons=buttons)


async def load_reason_next_page(event, data: dict, selected_option: str,
                                selected_text: str):
    """Ask Telegram for the next page after a main reason is selected."""
    await _render(
        event,
        "**Loading Telegram Report Options**\n\n"
        f"Reason: {selected_text}\n\n"
        "Asking Telegram for the exact next options.",
        buttons=[[Button.inline("Cancel", b"cmp:cancel")]],
    )
    phone = (data.get("selected_phones") or [None])[0]
    result = await load_report_choices(
        phone,
        data.get("target") or "",
        (data.get("messages") or [""])[0],
        option_bytes=bytes.fromhex(selected_option),
    )

    if result.get("kind") == "choose" and result.get("options"):
        pages = data.get("reason_pages") or []
        data["reason_pages"] = pages[:1] + [result["options"]]
        data["reason_page"] = 1
        data["reason_option"] = ""
        data["comment_required"] = False
        _save_flow(event.sender_id, data)
        await show_stored_reason_page(event, data)
        return

    if result.get("kind") in ("comment", "reported"):
        option = (
            result.get("option")
            if result.get("kind") == "comment"
            else selected_option
        ) or selected_option
        required = (
            result.get("kind") == "comment"
            and not bool(result.get("optional"))
        )
        _append_reason_choice(
            data,
            data.get("reason") or "Other",
            "",
            option,
            comment_required=required,
        )
        data["reason_detail"] = ""
        data["reason_option"] = option
        data["step"] = "reason_choices"
        _save_flow(event.sender_id, data)
        await show_reason_choices(event, data)
        return

    error = result.get("error") or "Telegram did not return the next options."
    data["reason_option"] = selected_option
    data["comment_required"] = any(
        bool(item.get("comment_required")) for item in _reason_choices(data)
    )
    data["step"] = "reason_sub"
    _save_flow(event.sender_id, data)
    await _render(
        event,
        "**Telegram Options Unavailable**\n\n"
        f"{error}\n\n"
        "Retry, go back to the reasons, or continue with the real reason you "
        "selected.",
        buttons=[
            [Button.inline("Try Again", b"cmp:rretry")],
            [Button.inline("Continue with this reason", b"cmp:rskip")],
            [Button.inline("Back to Reasons", b"cmp:back_reason")],
            [Button.inline("Cancel", b"cmp:cancel")],
        ],
    )


def _comment_prompt_text(data: dict) -> str:
    choices = _reason_choices(data)
    required = " Telegram requires a comment for at least one selected type." if data.get("comment_required") else ""
    lines = [
        "**Campaign Setup: 8 of 10 - Comments**",
        "",
        "You can configure a separate comment for each report type, or one comment for all.",
        "",
        "Current Report Types & Comments:",
    ]
    if choices:
        for index, choice in enumerate(choices, 1):
            detail = choice.get("detail") or choice.get("reason") or "Reason"
            comm = choice.get("comment") or "(None)"
            lines.append(f"{index}. {detail} -> Comment: `{comm}`")
    else:
        comm = data.get("comment") or "(None)"
        lines.append(f"Global Comment: `{comm}`")
    if required:
        lines.append(f"\n{required}")
    return "\n".join(lines)


async def show_comment_prompt(event, data: dict):
    choices = _reason_choices(data)
    back = b"cmp:back_rchoices" if choices else b"cmp:back_reason"
    buttons = []
    if len(choices) > 1:
        for index, choice in enumerate(choices):
            detail = (choice.get("detail") or choice.get("reason") or f"Type {index+1}")[:20]
            status_tag = " (Set)" if choice.get("comment") else ""
            buttons.append([Button.inline(f"Comment for #{index+1} ({detail}){status_tag}", f"cmp:comm_idx:{index}".encode())])
        buttons.append([Button.inline("Paste Separate Comments (1 per line)", b"cmp:comm_multi")])
        buttons.append([Button.inline("Set One Comment for All", b"cmp:comm_all")])
    else:
        buttons.append([Button.inline("Add Comment", b"cmp:comm_all")])
    buttons.append([Button.inline("Continue to Repeat Interval", b"cmp:comm_done")])
    buttons.append([Button.inline("No Comment (Clear All)", b"cmp:comment_no")])
    buttons.append([Button.inline("Back", back), _cancel_button()])
    await _render(event, _comment_prompt_text(data), buttons=buttons)


def _interval_prompt_text(data: dict) -> str:
    return (
        "**Campaign Setup: 9 of 10 - Repeat Interval**\n\n"
        f"Report types:\n{_reason_choices_text(data)}\n"
        f"Comment: {'yes' if data.get('comment') else 'no'}\n\n"
        "Choose how often a complete reporting cycle starts. Every 2 hours is "
        "the default. One time runs one cycle and then completes."
    )


async def show_interval_prompt(event, data: dict):
    await _render(event, 
        _interval_prompt_text(data),
        buttons=[
            [Button.inline("Every 2 hours (default)", b"cmp:interval:2"),
             Button.inline("One time", b"cmp:interval_once")],
            [Button.inline("Every 6 hours", b"cmp:interval:6"),
             Button.inline("Every 12 hours", b"cmp:interval:12")],
            [Button.inline("Every 24 hours", b"cmp:interval:24")],
            [Button.inline("Custom Hours", b"cmp:interval_custom")],
            [Button.inline("Back", b"cmp:back_comment"), _cancel_button()],
        ],
    )


def _duration_prompt_text(data: dict) -> str:
    return (
        "**Campaign Setup: 10 of 10 - Run Duration**\n\n"
        f"Repeat interval: {_interval_text(data.get('interval_hours'))}\n\n"
        "Choose how long the campaign should keep reporting, such as 1 day or "
        "2 days. The deadline is set when you tap Start Now. When the deadline "
        "is reached, the scheduler marks the campaign completed and stops "
        "scheduling new cycles."
    )


async def show_duration_prompt(event, data: dict):
    buttons = [
        [Button.inline("1 day", b"cmp:duration:24"),
         Button.inline("2 days", b"cmp:duration:48")],
        [Button.inline("3 days", b"cmp:duration:72"),
         Button.inline("7 days", b"cmp:duration:168")],
        [Button.inline("30 days", b"cmp:duration:720")],
        [Button.inline("Unlimited until stopped", b"cmp:duration:0")],
        [Button.inline("Custom Duration", b"cmp:duration_custom")],
        [Button.inline("Back", b"cmp:back_interval"), _cancel_button()],
    ]
    await _render(event, _duration_prompt_text(data), buttons=buttons)


def _summary_text(data: dict) -> str:
    accounts = (
        f"All active accounts ({_account_count(data)})"
        if data.get("accounts_mode") == "all"
        else f"{len(data.get('selected_phones') or [])} selected account(s)"
    )
    comment = data.get("comment") or "none"
    if len(comment) > 300:
        comment = comment[:300] + "..."
    return (
        "**Campaign Review**\n\n"
        f"Name: {data.get('name') or 'Untitled campaign'}\n"
        f"Target: `{data.get('target')}`\n"
        f"Accounts: {accounts}\n"
        f"Message links: {len(data.get('messages') or [])}\n"
        f"Distribution: {_dist_text(data.get('dist_mode'))}\n"
        f"Reports per link: {data.get('reports_per_message')}\n"
        f"Reports per cycle: {_total_reports(data)}\n"
        f"Delay: {data.get('delay')} seconds\n"
        f"Report types:\n{_reason_choices_text(data)}\n"
        f"Comment: {comment}\n"
        f"Repeat: {_interval_text(data.get('interval_hours'))}\n"
        f"Run duration: {_duration_text(data.get('duration_hours'), one_time=float(data.get('interval_hours') or 0) <= 0)}\n"
        f"Deadline: set when the campaign starts\n\n"
        "Tap Create and Start Now to save and start immediately, or Create "
        "Campaign to save it for later. There is no need to go back to the "
        "campaign list to start it."
    )


async def show_summary(event, data: dict):
    one_time = float(data.get("interval_hours") or 0) <= 0
    back = b"cmp:back_interval" if one_time else b"cmp:back_duration"
    
    # Ensure reason choices exist; auto-pair 2 report types
    choices = _reason_choices(data)
    if not choices:
        choices = [
            {"reason": "spam", "detail": "Spam", "option": ""},
            {"reason": "fake", "detail": "Fake Account / Scam", "option": ""},
        ]
        data["reason_choices"] = choices
        data["reports_per_message"] = max(2, int(data.get("reports_per_message") or 2))
        data["reason"] = "spam"
        data["reason_detail"] = "Spam"
        data["reason_option"] = ""
        _save_flow(event.sender_id, data)
    elif len(choices) == 1:
        primary = (choices[0].get("reason") or "spam").lower()
        sec = "fake" if "spam" in primary else ("violence" if "fake" in primary else "spam")
        sec_label = "Fake Account / Scam" if sec == "fake" else sec.capitalize()
        choices.append({"reason": sec, "detail": sec_label, "option": ""})
        data["reason_choices"] = choices
        data["reports_per_message"] = max(2, int(data.get("reports_per_message") or 2))
        _save_flow(event.sender_id, data)

    buttons = [
        [Button.inline("CREATE & START CAMPAIGN NOW", b"cmp:create_start")],
        [Button.inline("Save Campaign (Start Later)", b"cmp:create")],
        [Button.inline("Change Report Reasons", b"cmp:back_reason")],
        [Button.inline("Back", back), _cancel_button()],
    ]
    await _render(event, _summary_text(data), buttons=buttons)


def _campaign_reason_text(campaign: dict) -> str:
    choices = campaign.get("reason_choices") or []
    if choices:
        lines = []
        for index, choice in enumerate(choices, 1):
            detail = choice.get("detail") or ""
            line = f"{index}. {reason_display(choice.get('reason'), detail)}"
            comm = choice.get("comment")
            if comm:
                line += f" [Comment: {comm[:50]}]"
            lines.append(line)
        return "\n".join(lines)
    comm = campaign.get("comment")
    base = reason_display(campaign.get("reason"), campaign.get("reason_detail"))
    return f"{base} [Comment: {comm[:50]}]" if comm else base


def _campaign_text(campaign: dict) -> str:
    accounts = campaign.get("account_phones") or []
    if accounts:
        accounts_text = f"{len(accounts)} selected account(s)"
    else:
        accounts_text = "All active accounts"
    comment = campaign.get("comment") or "none"
    if len(comment) > 300:
        comment = comment[:300] + "..."
    status = STATUS_LABELS.get(campaign.get("status"), campaign.get("status"))
    next_run = format_dt(campaign.get("next_run_at")) if campaign.get("next_run_at") else "not scheduled"
    last_run = format_dt(campaign.get("last_run_at")) if campaign.get("last_run_at") else "never"
    ends_at = format_dt(campaign.get("ends_at")) if campaign.get("ends_at") else "no deadline"
    one_time = float(campaign.get("interval_hours") or 0) <= 0
    return (
        f"**Campaign #{campaign.get('id')}: {campaign.get('name') or 'Untitled'}**\n\n"
        f"Status: {status}\n"
        f"Target: `{campaign.get('target')}`\n"
        f"Accounts: {accounts_text}\n"
        f"Message links: {len(campaign.get('messages') or [])}\n"
        f"Distribution: {_dist_text(campaign.get('dist_mode'))}\n"
        f"Reports per link: {campaign.get('reports_per_message')}\n"
        f"Delay: {campaign.get('delay_seconds')} seconds\n"
        f"Report types:\n{_campaign_reason_text(campaign)}\n"
        f"Comment: {comment}\n"
        f"Repeat: {_interval_text(campaign.get('interval_hours'))}\n"
        f"Run duration: {_duration_text(campaign.get('duration_hours'), one_time=one_time)}\n"
        f"Deadline: {ends_at}\n"
        f"Cycles run: {campaign.get('cycle_count') or 0}\n"
        f"Next run: {next_run}\n"
        f"Last run: {last_run}\n"
        f"Last result: {campaign.get('last_result') or 'none'}"
    )


def _campaign_view_buttons(campaign: dict):
    campaign_id = campaign.get("id")
    status = campaign.get("status")
    buttons = []
    if status in ("ready", "paused", "stopped", "completed"):
        if status == "paused":
            label = "Resume"
        elif status in ("completed", "stopped"):
            label = "Start Again"
        else:
            label = "Start Now"
        buttons.append([Button.inline(label, f"cmp:start:{campaign_id}".encode())])
    if status in ("active", "running"):
        buttons.append([Button.inline("Pause", f"cmp:pause:{campaign_id}".encode())])
    if status != "stopped":
        buttons.append([Button.inline("Stop", f"cmp:stop:{campaign_id}".encode())])
    
    # Edit Campaign and Delete Campaign buttons
    buttons.append([Button.inline("Edit Campaign", f"cmp:edit:{campaign_id}".encode()),
                    Button.inline("Delete Campaign", f"cmp:del:{campaign_id}".encode())])

    buttons.append([Button.inline("Campaigns", b"cmp:list:0"), _main_button()])
    return buttons


async def show_campaign_view(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, 
            "Campaign not found.",
            buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]],
        )
        return
    if campaign.get("created_by") != event.sender_id and not db.is_admin(event.sender_id):
        await _render(event, 
            "You cannot view this campaign.",
            buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]],
        )
        return
    await _render(event, _campaign_text(campaign), buttons=_campaign_view_buttons(campaign))


async def show_delete_confirm(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.", buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    text = (
        f"**Delete Campaign #{campaign_id}**\n\n"
        f"Target: `{campaign.get('target')}`\n"
        f"Name: {campaign.get('name') or 'Untitled'}\n\n"
        "Are you sure you want to permanently delete this campaign? This action cannot be undone."
    )
    buttons = [
        [Button.inline("Yes, Delete Permanently", f"cmp:del_confirm:{campaign_id}".encode())],
        [Button.inline("Cancel", f"cmp:view:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_campaign_edit_menu(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
                      buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    rules = campaign.get("account_rules") or {}
    custom_count = len(rules)
    text = (
        f"**Edit Campaign #{campaign_id}: {campaign.get('name') or 'Untitled'}**\n\n"
        f"Target: `{campaign.get('target')}`\n"
        f"Accounts: {len(campaign.get('account_phones') or []) or 'All active'}\n"
        f"Message links: {len(campaign.get('messages') or [])}\n"
        f"Distribution: {_dist_text(campaign.get('dist_mode'))}\n"
        f"Account rules: {custom_count} custom configured\n"
        f"Reports per link: {campaign.get('reports_per_message')}\n"
        f"Delay: {campaign.get('delay_seconds')}s\n"
        f"Report types:\n{_campaign_reason_text(campaign)}\n"
        f"Repeat interval: {_interval_text(campaign.get('interval_hours'))}\n"
        f"Run duration: {_duration_text(campaign.get('duration_hours'), one_time=float(campaign.get('interval_hours') or 0) <= 0)}\n\n"
        "Choose a setting to edit:"
    )
    buttons = [
        [Button.inline("Target", f"cmp:ed_tgt:{campaign_id}".encode()),
         Button.inline("Accounts", f"cmp:ed_acc:{campaign_id}".encode())],
        [Button.inline("Message Links (Scrape)", f"cmp:ed_msg:{campaign_id}".encode()),
         Button.inline("Distribution Mode", f"cmp:ed_dist:{campaign_id}".encode())],
        [Button.inline(f"Account Rules ({custom_count} custom)", f"cmp:ed_acrules:{campaign_id}".encode())],
        [Button.inline("Reports per Link", f"cmp:ed_rpm:{campaign_id}".encode()),
         Button.inline("Delay Seconds", f"cmp:ed_dly:{campaign_id}".encode())],
        [Button.inline("Reasons & Comments", f"cmp:fixreasons:{campaign_id}".encode()),
         Button.inline("Interval & Duration", f"cmp:ed_time:{campaign_id}".encode())],
        [Button.inline("Delete Campaign", f"cmp:del:{campaign_id}".encode())],
        [Button.inline("Back to Campaign", f"cmp:view:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_account_rules_menu(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.", buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    accounts = db.get_active_accounts()
    assigned = campaign.get("account_phones") or []
    if assigned:
        accounts = [acc for acc in accounts if acc[1] in set(assigned)]
    rules = campaign.get("account_rules") or {}

    text = (
        f"**Account Rules & Custom Reporting (Campaign #{campaign_id})**\n\n"
        f"Target: `{campaign.get('target')}`\n"
        f"Accounts: {len(accounts)} | Custom Configured: {len(rules)}\n\n"
        "Configure custom report reasons, comments, or targeted messages for individual accounts. "
        "Accounts without custom rules use the campaign defaults.\n\n"
        "Tap an account to configure:"
    )
    buttons = []
    for acc in accounts:
        phone = acc[1]
        rule = rules.get(phone)
        tag = "[Custom]" if rule else "[Default]"
        masked = mask_phone(phone)
        summary = ""
        if rule:
            r_list = rule.get("reasons") or []
            r_name = r_list[0].get("detail") or r_list[0].get("reason") if r_list else "Default reason"
            m_spec = rule.get("messages")
            if m_spec == "all" or m_spec is None:
                m_txt = "All msgs"
            elif isinstance(m_spec, list):
                m_txt = f"Msgs: {','.join(str(i+1) for i in m_spec)}" if m_spec else "All msgs"
            else:
                m_txt = "All msgs"
            summary = f" ({r_name} | {m_txt})"
        label = f"{tag} {masked}{summary}"[:40]
        buttons.append([Button.inline(label, f"cmp:ed_ar_acc:{campaign_id}:{phone}".encode())])
    
    if rules:
        buttons.append([Button.inline("Clear All Custom Rules", f"cmp:ed_ar_clear_all:{campaign_id}".encode())])
    buttons.append([Button.inline("Back to Edit Menu", f"cmp:edit:{campaign_id}".encode())])
    await _render(event, text, buttons=buttons)


async def show_account_rule_editor(event, campaign_id: int, phone: str):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.")
        return
    rules = campaign.get("account_rules") or {}
    rule = rules.get(phone) or {}
    msgs = campaign.get("messages") or []

    reasons = rule.get("reasons")
    if reasons:
        r_txt = ", ".join(r.get("detail") or r.get("reason") for r in reasons)
    else:
        r_txt = "Campaign Default (" + ", ".join(r.get("detail") or r.get("reason") for r in (campaign.get("reason_choices") or [])) + ")"

    m_spec = rule.get("messages")
    if m_spec == "all" or m_spec is None:
        m_txt = f"All Messages ({len(msgs)} posts) / Campaign Strategy"
    elif isinstance(m_spec, list):
        if not m_spec:
            m_txt = "All Messages"
        else:
            m_txt = ", ".join(f"Message #{i+1}" for i in m_spec)
    else:
        m_txt = "All Messages"

    comm = rule.get("comment")
    if comm is not None and comm != "":
        c_txt = comm
    else:
        c_txt = "Campaign Default (" + (campaign.get("comment") or "none") + ")"

    text = (
        f"**Custom Rule for Account: {mask_phone(phone)}**\n"
        f"Campaign: #{campaign_id} (`{campaign.get('target')}`)\n\n"
        f"Current Report Reasons:\n`{r_txt}`\n\n"
        f"Targeted Messages:\n`{m_txt}`\n\n"
        f"Comment:\n`{c_txt}`\n\n"
        "Customize this account's reporting behavior:"
    )
    buttons = [
        [Button.inline("Choose Report Reason", f"cmp:ed_ar_rsn:{campaign_id}:{phone}".encode()),
         Button.inline("Choose Targeted Messages", f"cmp:ed_ar_msg:{campaign_id}:{phone}".encode())],
        [Button.inline("Set Custom Comment", f"cmp:ed_ar_comm:{campaign_id}:{phone}".encode()),
         Button.inline("Reset to Default", f"cmp:ed_ar_reset:{campaign_id}:{phone}".encode())],
        [Button.inline("Copy Rule to Other Accounts", f"cmp:ed_ar_copy:{campaign_id}:{phone}".encode())],
        [Button.inline("Back to Account Rules", f"cmp:ed_acrules:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_account_reason_picker(event, campaign_id: int, phone: str):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.")
        return
    reasons_list = [
        ("spam", "Spam"),
        ("fake", "Fake Account / Scam"),
        ("violence", "Violence"),
        ("pornography", "Pornography"),
        ("child_abuse", "Child Abuse"),
        ("illegal_drugs", "Illegal Drugs"),
        ("personal_details", "Personal Details"),
        ("copyright", "Copyright"),
    ]
    text = (
        f"**Select Report Reason for {mask_phone(phone)}**\n\n"
        "Choose which reason this specific account will send when reporting:"
    )
    buttons = []
    row = []
    for key, label in reasons_list:
        row.append(Button.inline(label, f"cmp:ed_ar_rsn_pick:{campaign_id}:{phone}:{key}".encode()))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([Button.inline("Use Campaign Default Reasons", f"cmp:ed_ar_rsn_def:{campaign_id}:{phone}".encode())])
    buttons.append([Button.inline("Back", f"cmp:ed_ar_acc:{campaign_id}:{phone}".encode())])
    await _render(event, text, buttons=buttons)


async def show_account_message_picker(event, campaign_id: int, phone: str):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.")
        return
    rules = campaign.get("account_rules") or {}
    rule = rules.get(phone) or {}
    msgs = campaign.get("messages") or []
    m_spec = rule.get("messages")
    is_all = (m_spec == "all" or m_spec is None)
    selected_indices = set(m_spec) if isinstance(m_spec, list) else set()

    text = (
        f"**Targeted Messages for {mask_phone(phone)}**\n\n"
        f"Total Messages in Channel: {len(msgs)}\n\n"
        "Select whether this account reports All Messages or only specific message(s):"
    )
    buttons = []
    all_tag = "[X] " if is_all else "[ ] "
    buttons.append([Button.inline(f"{all_tag}All Messages / Follow Strategy", f"cmp:ed_ar_msgtog:{campaign_id}:{phone}:all".encode())])
    row = []
    for idx in range(len(msgs)):
        tag = "[X] " if (not is_all and idx in selected_indices) else "[ ] "
        row.append(Button.inline(f"{tag}Message #{idx+1}", f"cmp:ed_ar_msgtog:{campaign_id}:{phone}:{idx}".encode()))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([Button.inline("Done / Back", f"cmp:ed_ar_acc:{campaign_id}:{phone}".encode())])
    await _render(event, text, buttons=buttons)


async def show_account_copy_picker(event, campaign_id: int, phone: str):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.")
        return
    accounts = db.get_active_accounts()
    assigned = campaign.get("account_phones") or []
    if assigned:
        accounts = [acc for acc in accounts if acc[1] in set(assigned)]
    other_accounts = [acc for acc in accounts if acc[1] != phone]

    text = (
        f"**Copy Rule from {mask_phone(phone)} to Other Accounts**\n\n"
        "Tap an account below to apply the exact same reporting rule to it, "
        "or apply to all other accounts at once:"
    )
    buttons = []
    for acc in other_accounts:
        o_phone = acc[1]
        buttons.append([Button.inline(f"Apply to {mask_phone(o_phone)}", f"cmp:ed_ar_copy_to:{campaign_id}:{phone}:{o_phone}".encode())])
    if other_accounts:
        buttons.append([Button.inline("Apply to ALL Other Accounts", f"cmp:ed_ar_copy_all:{campaign_id}:{phone}".encode())])
    buttons.append([Button.inline("Back", f"cmp:ed_ar_acc:{campaign_id}:{phone}".encode())])
    await _render(event, text, buttons=buttons)


async def show_edit_target_prompt(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
                      buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    db.set_user_state(event.sender_id, CAMPAIGN_STATE, {
        "step": "edit_target",
        "edit_campaign_id": campaign_id,
    })
    text = (
        f"**Edit Target for Campaign #{campaign_id}**\n\n"
        f"Current Target: `{campaign.get('target')}`\n\n"
        "Send the new channel username or message link (e.g. `@channel` or `https://t.me/channel`):"
    )
    buttons = [
        [Button.inline("Back to Edit Menu", f"cmp:edit:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_edit_accounts_menu(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
                      buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    accounts = db.get_active_accounts()
    selected = set(campaign.get("account_phones") or [])
    is_all = len(selected) == 0
    text = (
        f"**Edit Accounts for Campaign #{campaign_id}**\n\n"
        f"Active Accounts Available: {len(accounts)}\n"
        f"Selected Accounts: {'All Active Accounts' if is_all else f'{len(selected)} Selected'}\n\n"
        "Tap an account to toggle, or tap Use All Active Accounts:"
    )
    buttons = []
    all_btn_text = "[X] Use All Active Accounts" if is_all else "[ ] Use All Active Accounts"
    buttons.append([Button.inline(all_btn_text, f"cmp:ed_acc_all:{campaign_id}".encode())])
    for acc in accounts:
        phone = acc[1]
        masked = mask_phone(phone)
        tag = "[X]" if (phone in selected and not is_all) else "[ ]"
        buttons.append([Button.inline(f"{tag} {masked}", f"cmp:ed_acc_tog:{campaign_id}:{phone}".encode())])
    buttons.append([Button.inline("Back to Edit Menu", f"cmp:edit:{campaign_id}".encode())])
    await _render(event, text, buttons=buttons)


async def show_edit_msg_menu(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
                      buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    msgs = campaign.get("messages") or []
    text = (
        f"**Edit Message Links for Campaign #{campaign_id}**\n\n"
        f"Target: `{campaign.get('target')}`\n"
        f"Current Stored Message Links: {len(msgs)}\n\n"
        "Choose an option to re-scrape recent posts from the channel, or set to peer reporting only:"
    )
    buttons = [
        [Button.inline("Channel Peer Only (0 posts)", f"cmp:ed_scrape:{campaign_id}:0".encode())],
        [Button.inline("Fetch 5 Posts", f"cmp:ed_scrape:{campaign_id}:5".encode()),
         Button.inline("Fetch 10 Posts", f"cmp:ed_scrape:{campaign_id}:10".encode())],
        [Button.inline("Fetch 20 Posts", f"cmp:ed_scrape:{campaign_id}:20".encode()),
         Button.inline("Fetch 50 Posts", f"cmp:ed_scrape:{campaign_id}:50".encode())],
        [Button.inline("Fetch 100 Posts", f"cmp:ed_scrape:{campaign_id}:100".encode()),
         Button.inline("Custom Post Count", f"cmp:ed_msg_cust:{campaign_id}".encode())],
        [Button.inline("Back to Edit Menu", f"cmp:edit:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_edit_dist_menu(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
                      buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    dist_mode = campaign.get("dist_mode") or "all"
    msgs = len(campaign.get("messages") or [])
    accs = len(campaign.get("account_phones") or []) or len(db.get_active_accounts())
    rpm = int(campaign.get("reports_per_message") or 2)
    all_tot = (msgs if msgs > 0 else 1) * rpm * accs
    rr_tot = (msgs if accs <= msgs else accs) * rpm if msgs > 0 else accs * rpm

    text = (
        f"**Edit Distribution Mode for Campaign #{campaign_id}**\n\n"
        f"Current Mode: {_dist_text(dist_mode)}\n"
        f"Accounts: {accs} | Messages: {msgs} | Reports/link: {rpm}\n\n"
        "Select Distribution Strategy:\n\n"
        "1. **All Accounts to All Links (Full Mass)**\n"
        f"Every active account reports every message link ({all_tot} total reports).\n\n"
        "2. **1 Account per Link (Spread / Round-Robin)**\n"
        f"Account 1 reports Link 1, Account 2 reports Link 2, etc. ({rr_tot} total reports)."
    )
    buttons = [
        [Button.inline(f"{'[X] ' if dist_mode == 'all' else '[ ] '}All Accounts -> All Messages", f"cmp:ed_dist_set:{campaign_id}:all".encode())],
        [Button.inline(f"{'[X] ' if dist_mode == 'round_robin' else '[ ] '}Spread / Round-Robin (1-to-1)", f"cmp:ed_dist_set:{campaign_id}:round_robin".encode())],
        [Button.inline("Back to Edit Menu", f"cmp:edit:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_edit_rpm_menu(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
                      buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    rpm = campaign.get("reports_per_message") or 2
    text = (
        f"**Edit Reports Per Link for Campaign #{campaign_id}**\n\n"
        f"Current: {rpm} report(s) per link\n\n"
        "Choose how many reports each account will send for each assigned message link:"
    )
    buttons = [
        [Button.inline("1 Report", f"cmp:ed_rpm_set:{campaign_id}:1".encode()),
         Button.inline("2 Reports (default)", f"cmp:ed_rpm_set:{campaign_id}:2".encode())],
        [Button.inline("3 Reports", f"cmp:ed_rpm_set:{campaign_id}:3".encode()),
         Button.inline("5 Reports", f"cmp:ed_rpm_set:{campaign_id}:5".encode())],
        [Button.inline("10 Reports", f"cmp:ed_rpm_set:{campaign_id}:10".encode()),
         Button.inline("Custom", f"cmp:ed_rpm_cust:{campaign_id}".encode())],
        [Button.inline("Back to Edit Menu", f"cmp:edit:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_edit_delay_menu(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
                      buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    delay = campaign.get("delay_seconds") or 5
    text = (
        f"**Edit Delay for Campaign #{campaign_id}**\n\n"
        f"Current: {delay} seconds\n\n"
        "Choose delay between reports to avoid FloodWait:"
    )
    buttons = [
        [Button.inline("3 seconds", f"cmp:ed_dly_set:{campaign_id}:3".encode()),
         Button.inline("5 seconds (default)", f"cmp:ed_dly_set:{campaign_id}:5".encode())],
        [Button.inline("10 seconds", f"cmp:ed_dly_set:{campaign_id}:10".encode()),
         Button.inline("30 seconds", f"cmp:ed_dly_set:{campaign_id}:30".encode())],
        [Button.inline("60 seconds", f"cmp:ed_dly_set:{campaign_id}:60".encode()),
         Button.inline("Custom Delay", f"cmp:ed_dly_cust:{campaign_id}".encode())],
        [Button.inline("Back to Edit Menu", f"cmp:edit:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_edit_time_menu(event, campaign_id: int):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
                      buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    int_h = float(campaign.get("interval_hours") or 0)
    dur_h = float(campaign.get("duration_hours") or 0)
    text = (
        f"**Edit Interval & Duration for Campaign #{campaign_id}**\n\n"
        f"Current Repeat Interval: {_interval_text(int_h)}\n"
        f"Current Run Duration: {_duration_text(dur_h, one_time=int_h<=0)}\n\n"
        "Choose an interval setting:"
    )
    buttons = [
        [Button.inline("Run Once (No Repeat)", f"cmp:ed_int_set:{campaign_id}:0".encode())],
        [Button.inline("Every 1 Hour", f"cmp:ed_int_set:{campaign_id}:1".encode()),
         Button.inline("Every 2 Hours", f"cmp:ed_int_set:{campaign_id}:2".encode())],
        [Button.inline("Every 6 Hours", f"cmp:ed_int_set:{campaign_id}:6".encode()),
         Button.inline("Every 12 Hours", f"cmp:ed_int_set:{campaign_id}:12".encode())],
        [Button.inline("Custom Interval", f"cmp:ed_int_cust:{campaign_id}".encode()),
         Button.inline("Custom Duration", f"cmp:ed_dur_cust:{campaign_id}".encode())],
        [Button.inline("Back to Edit Menu", f"cmp:edit:{campaign_id}".encode())]
    ]
    await _render(event, text, buttons=buttons)


async def show_campaign_list(event, user_id: int, offset: int = 0):
    page_size = 5
    offset = max(0, int(offset))
    total = db.count_report_campaigns(created_by=user_id)
    campaigns = db.list_report_campaigns(created_by=user_id, limit=page_size, offset=offset)
    if not campaigns:
        await _render(event, 
            "**My Campaigns**\n\n"
            "No campaigns yet. Create one to store a target, message links and schedule.",
            buttons=[
                [Button.inline("New Campaign", b"cmp:new")],
                [Button.inline("Campaigns", b"cmp:menu"), _main_button()],
            ],
        )
        return
    buttons = []
    for campaign in campaigns:
        label = (
            f"#{campaign['id']} {STATUS_LABELS.get(campaign.get('status'), campaign.get('status'))} "
            f"{campaign.get('name') or campaign.get('target')}"
        )[:60]
        buttons.append([Button.inline(label, f"cmp:view:{campaign['id']}".encode())])
    nav = []
    if offset > 0:
        nav.append(Button.inline("Previous", f"cmp:list:{max(0, offset - page_size)}".encode()))
    if offset + page_size < total:
        nav.append(Button.inline("Next", f"cmp:list:{offset + page_size}".encode()))
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline("New Campaign", b"cmp:new")])
    buttons.append([Button.inline("Campaigns", b"cmp:menu"), _main_button()])
    await _render(event, 
        f"**My Campaigns**\n\nShowing {offset + 1}-{offset + len(campaigns)} of {total}.",
        buttons=buttons,
    )


async def create_campaign(event, user_id: int, data: dict, start_now: bool = False):
    target = (data.get("target") or "").strip()
    messages = data.get("messages") or []
    if not target:
        await _render(event, 
            "Campaign data is incomplete. Start again.",
            buttons=[[Button.inline("New Campaign", b"cmp:new"), _main_button()]],
        )
        return
    account_phones = [] if data.get("accounts_mode") == "all" else list(data.get("selected_phones") or [])
    if data.get("accounts_mode") != "all" and not account_phones:
        await _render(event, 
            "Select at least one account before creating the campaign.",
            buttons=[[Button.inline("Back", b"cmp:back_accounts"), _cancel_button()]],
        )
        return
    choices = _reason_choices(data)
    if not choices:
        choices = [
            {"reason": "spam", "detail": "Spam", "option": ""},
            {"reason": "fake", "detail": "Fake Account / Scam", "option": ""},
        ]
        data["reason_choices"] = choices
    elif len(choices) == 1:
        primary = (choices[0].get("reason") or "spam").lower()
        sec = "fake" if "spam" in primary else ("violence" if "fake" in primary else "spam")
        sec_label = "Fake Account / Scam" if sec == "fake" else sec.capitalize()
        choices.append({"reason": sec, "detail": sec_label, "option": ""})
        data["reason_choices"] = choices

    first = choices[0]
    campaign_id = db.create_report_campaign({
        "name": data.get("name") or target,
        "target": target,
        "account_phones": account_phones,
        "messages": messages,
        "reports_per_message": max(1, int(data.get("reports_per_message") or CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE)),
        "delay_seconds": int(data.get("delay") or 5),
        "exec_mode": "safe",
        "dist_mode": data.get("dist_mode") or "all",
        "reason": first.get("reason") or "spam",
        "reason_detail": first.get("detail") or "",
        "reason_option": first.get("option") or "",
        "reason_choices": choices,
        "comment": data.get("comment") or "",
        "interval_hours": float(data.get("interval_hours") or 0),
        "duration_hours": float(data.get("duration_hours") or 0),
        "max_cycles": int(data.get("max_cycles") or 0),
        "status": "ready",
        "next_run_at": None,
        "ends_at": None,
        "created_by": user_id,
    })
    db.clear_user_state(user_id)
    log_activity(f"Campaign #{campaign_id} created by user {user_id}")
    if start_now:
        log_activity(f"Campaign #{campaign_id} started immediately by user {user_id}")
        await _start_campaign(event, campaign_id, reset_cycles=True)
        return
    campaign = db.get_report_campaign(campaign_id)
    buttons = _campaign_view_buttons(campaign)
    buttons.insert(0, [Button.inline("START CAMPAIGN NOW", f"cmp:start:{campaign_id}".encode())])
    await _render(event, 
        f"Campaign #{campaign_id} created successfully.\n\n{_campaign_text(campaign)}",
        buttons=buttons,
    )


async def _start_campaign(event, campaign_id: int, reset_cycles: bool = False):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, 
            "Campaign not found.",
            buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]],
        )
        return

    choices = campaign.get("reason_choices") or []
    if not choices:
        choices = [
            {"reason": campaign.get("reason") or "spam", "detail": campaign.get("reason_detail") or "Spam", "option": campaign.get("reason_option") or ""},
            {"reason": "fake", "detail": "Fake Account / Scam", "option": ""}
        ]
        db.update_report_campaign(campaign_id, reason_choices=choices)

    now = datetime.now()
    ends_at = campaign.get("ends_at")
    if reset_cycles or not ends_at:
        duration_hours = float(campaign.get("duration_hours") or 0)
        ends_at = (
            (now + timedelta(hours=duration_hours)).isoformat(timespec="seconds")
            if duration_hours > 0
            else None
        )

    deadline = parse_iso(ends_at)
    if deadline and deadline <= now:
        db.update_report_campaign(
            campaign_id,
            status="completed",
            next_run_at=None,
            last_result="Duration reached; campaign completed.",
        )
        await show_campaign_view(event, campaign_id)
        return

    fields = {
        "status": "active",
        "next_run_at": now.isoformat(timespec="seconds"),
        "ends_at": ends_at,
    }
    if reset_cycles:
        fields["cycle_count"] = 0
        fields["last_result"] = ""
    db.update_report_campaign(campaign_id, **fields)
    await show_campaign_view(event, campaign_id)


async def start_campaign_reason_fix(event, user_id: int, campaign_id: int):
    """Edit the real Telegram reason chain of an existing campaign."""
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, "Campaign not found.",
            buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    if campaign.get("created_by") != user_id and not db.is_admin(user_id):
        await _render(event, "You cannot edit this campaign.",
            buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    if campaign.get("status") in ("active", "running"):
        await _render(event,
            "Pause or stop the campaign before changing its report reasons.",
            buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
        return
    phones = list(campaign.get("account_phones") or [])
    flow = {
        "step": "reason",
        "edit_campaign_id": campaign_id,
        "target": campaign.get("target") or "",
        "messages": list(campaign.get("messages") or []),
        "selected_phones": phones,
        "accounts_mode": "specific" if phones else "all",
        "reports_per_message": int(
            campaign.get("reports_per_message")
            or CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE
        ),
        "delay": int(campaign.get("delay_seconds") or 5),
        "reason": "",
        "reason_detail": "",
        "reason_option": "",
        "reason_pages": [],
        "reason_page": 0,
        "reason_pending_option": "",
        "reason_choices": [],
        "comment_required": False,
        "comment": campaign.get("comment") or "",
        "interval_hours": float(campaign.get("interval_hours") or 0),
        "duration_hours": float(campaign.get("duration_hours") or 0),
        "max_cycles": int(campaign.get("max_cycles") or 0),
    }
    _save_flow(user_id, flow)
    await show_reason_prompt(event, flow)


async def _finish_reason_edit(event, user_id: int, data: dict):
    campaign_id = int(data.get("edit_campaign_id") or 0)
    choices = _reason_choices(data)
    if not campaign_id or not choices:
        choices = [
            {"reason": "spam", "detail": "Spam", "option": ""},
            {"reason": "fake", "detail": "Fake Account / Scam", "option": ""},
        ]
    first = choices[0]
    db.update_report_campaign(
        campaign_id,
        reason=first.get("reason") or "spam",
        reason_detail=first.get("detail") or "",
        reason_option=first.get("option") or "",
        reason_choices=choices,
        reports_per_message=max(
            int(data.get("reports_per_message") or 2), len(choices)
        ),
        comment=data.get("comment") or "",
        last_result="Report reasons updated from real Telegram options.",
    )
    db.clear_user_state(user_id)
    log_activity(
        f"Campaign #{campaign_id} report reasons updated by user {user_id}"
    )
    await show_campaign_view(event, campaign_id)


async def _pause_campaign(event, campaign_id: int, stopped: bool = False):
    campaign = db.get_report_campaign(campaign_id)
    if not campaign:
        await _render(event, 
            "Campaign not found.",
            buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]],
        )
        return
    db.update_report_campaign(
        campaign_id,
        status="stopped" if stopped else "paused",
        next_run_at=None,
    )
    engine = active_campaigns.get(campaign_id)
    if engine is not None:
        engine.should_stop = True
    await show_campaign_view(event, campaign_id)


def _custom_prompt(text: str, back_data: bytes):
    return text, [[Button.inline("Back", back_data), _cancel_button()]]


async def _resume_campaign_flow(event, user_id: int, data: dict):
    """Re-render the current wizard step after Keep Editing is pressed."""
    step = data.get("step")
    if step == "name":
        await show_name_prompt(event, send=False)
    elif step == "target":
        await show_target_prompt(event, data)
    elif step == "accounts":
        await show_accounts_prompt(event, data)
    elif step == "fetch":
        await show_fetch_prompt(event, data)
    elif step == "reports":
        await show_reports_prompt(event, data)
    elif step == "delay":
        await show_delay_prompt(event, data)
    elif step == "dist":
        await show_dist_prompt(event, data)
    elif step == "reason_choices":
        await show_reason_choices(event, data)
    elif step in ("reason", "reason_sub"):
        if data.get("reason_pages"):
            await show_stored_reason_page(event, data)
        else:
            await show_reason_prompt(event, data)
    elif step == "comment":
        await show_comment_prompt(event, data)
    elif step == "interval":
        await show_interval_prompt(event, data)
    elif step == "duration":
        await show_duration_prompt(event, data)
    elif step == "summary":
        await show_summary(event, data)
    elif step == "custom_fetch":
        text, buttons = _custom_prompt(
            "**Fetch Count**\n\nSend how many recent posts to fetch.",
            b"cmp:back_fetch",
        )
        await _render(event, text, buttons=buttons)
    elif step == "custom_rpm":
        text, buttons = _custom_prompt(
            "**Reports Per Link**\n\nSend a number from 1 to 50.",
            b"cmp:back_reports",
        )
        await _render(event, text, buttons=buttons)
    elif step == "custom_delay":
        text, buttons = _custom_prompt(
            "**Custom Delay**\n\nSend the delay in seconds.",
            b"cmp:back_delay",
        )
        await _render(event, text, buttons=buttons)
    elif step == "comment":
        await show_comment_prompt(event, data)
    elif step == "comment_text" or step == "comment_multi" or (step and str(step).startswith("comment_idx_")):
        await show_comment_prompt(event, data)
    elif step == "custom_interval":
        text, buttons = _custom_prompt(
            "**Custom Interval**\n\nSend the number of hours between cycles, for example 2.",
            b"cmp:back_interval",
        )
        await _render(event, text, buttons=buttons)
    elif step == "custom_duration":
        text, buttons = _custom_prompt(
            "**Custom Run Duration**\n\nSend the duration in days, or use "
            "an h suffix for hours. Examples: 1.5, 2 days, 36h. Use 0 for "
            "unlimited.",
            b"cmp:back_duration",
        )
        await _render(event, text, buttons=buttons)
    else:
        await show_campaign_menu(event, send=False)


async def handle_campaign_callback(event, data: str):
    user_id = event.sender_id
    if not db.is_admin(user_id):
        await event.answer("Admin only.", alert=True)
        return
    await event.answer()
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else None

    try:
        if action == "menu":
            await show_campaign_menu(event, send=False)
            return
        if action == "new":
            db.clear_user_state(user_id)
            _save_flow(user_id, {
                "step": "name",
                "name": "",
                "selected_phones": [],
                "accounts_mode": "all",
                "reports_per_message": CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE,
                "delay": 5,
                "reason": "",
                "reason_detail": "",
                "reason_option": "",
                "reason_pages": [],
                "reason_page": 0,
                "reason_pending_option": "",
                "reason_choices": [],
                "comment_required": False,
                "comment": "",
                "interval_hours": CAMPAIGN_DEFAULT_INTERVAL_HOURS,
                "duration_hours": CAMPAIGN_DEFAULT_DURATION_HOURS,
                "max_cycles": 0,
            })
            await show_name_prompt(event, send=False)
            return
        if action == "list":
            await show_campaign_list(event, user_id, int(arg or 0))
            return
        if action == "options":
            flow = _get_flow(user_id)
            if flow and flow.get("step") == "reason_sub":
                back = b"cmp:back_rsub"
            else:
                back = b"cmp:back_reason" if flow else None
            await show_report_options(event, back)
            return
        if action == "cancel":
            flow = _get_flow(user_id)
            if not flow:
                await show_campaign_menu(event, send=False)
                return
            await _render(event,
                "**Campaign Setup Options**\n\n"
                "Save your progress as a draft and resume anytime, "
                "keep editing, or discard the setup.",
                buttons=[
                    [Button.inline("Save as Draft & Exit", b"cmp:save_draft")],
                    [Button.inline("Keep Editing", b"cmp:cancel_back")],
                    [Button.inline("Discard Completely", b"cmp:cancel_confirm")],
                ],
            )
            return
        if action == "save_draft":
            flow = _get_flow(user_id)
            if flow:
                flow["is_draft"] = True
                _save_flow(user_id, flow)
            await _render(event,
                "**Campaign Saved as Draft**\n\n"
                "Your setup has been preserved. You can resume it anytime "
                "from the Campaigns menu.",
                buttons=[
                    [Button.inline("Resume Campaign", b"cmp:resume_draft")],
                    [Button.inline("Campaigns Menu", b"cmp:menu"), _main_button()],
                ],
            )
            return
        if action == "resume_draft":
            flow = _get_flow(user_id)
            if flow:
                await _resume_campaign_flow(event, user_id, flow)
            else:
                await event.answer("No draft found.", alert=True)
                await show_campaign_menu(event, send=False)
            return
        if action == "cancel_confirm":
            db.clear_user_state(user_id)
            await _render(event,
                "Campaign setup discarded.",
                buttons=[
                    [Button.inline("New Campaign", b"cmp:new")],
                    [Button.inline("Campaigns Menu", b"cmp:menu"), _main_button()],
                ]
            )
            return
        if action == "cancel_back":
            flow = _get_flow(user_id)
            if flow:
                await _resume_campaign_flow(event, user_id, flow)
            else:
                await show_campaign_menu(event, send=False)
            return
        if action == "name_skip":
            flow = _require_flow(event, user_id)
            if not flow:
                await _render(event, "Session expired. Open Campaigns again.",
                                 buttons=[[Button.inline("Campaigns", b"cmp:menu"), _main_button()]])
                return
            flow["step"] = "target"
            flow["name"] = flow.get("name") or ""
            _save_flow(user_id, flow)
            await show_target_prompt(event, flow)
            return
        if action == "name_edit":
            flow = _require_flow(event, user_id)
            if not flow:
                await show_campaign_menu(event, send=False)
                return
            flow["step"] = "name"
            _save_flow(user_id, flow)
            await show_name_prompt(event, send=False)
            return

        # Actions on existing campaigns (no active wizard flow required)
        if action == "view" and arg:
            await show_campaign_view(event, int(arg))
            return
        if action == "start" and arg:
            campaign = db.get_report_campaign(int(arg))
            reset = bool(campaign and campaign.get("status") in ("completed", "stopped"))
            await _start_campaign(event, int(arg), reset_cycles=reset)
            return
        if action == "pause" and arg:
            await _pause_campaign(event, int(arg), stopped=False)
            return
        if action == "stop" and arg:
            await _pause_campaign(event, int(arg), stopped=True)
            return
        if action == "edit" and arg:
            await show_campaign_edit_menu(event, int(arg))
            return
        if action == "fixreasons" and arg:
            await start_campaign_reason_fix(event, user_id, int(arg))
            return
        if action == "ed_tgt" and arg:
            await show_edit_target_prompt(event, int(arg))
            return
        if action == "ed_acc" and arg:
            await show_edit_accounts_menu(event, int(arg))
            return
        if action == "ed_acc_all" and arg:
            db.update_report_campaign(int(arg), account_phones=[])
            await show_edit_accounts_menu(event, int(arg))
            return
        if action == "ed_acc_tog" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            campaign = db.get_report_campaign(cid)
            if campaign and phone:
                selected = list(campaign.get("account_phones") or [])
                if not selected:
                    selected = [phone]
                else:
                    if phone in selected:
                        selected.remove(phone)
                    else:
                        selected.append(phone)
                db.update_report_campaign(cid, account_phones=selected)
            await show_edit_accounts_menu(event, cid)
            return
        if action == "ed_msg" and arg:
            await show_edit_msg_menu(event, int(arg))
            return
        if action == "ed_scrape" and arg:
            cid = int(arg)
            limit = int(parts[3]) if len(parts) > 3 else 0
            campaign = db.get_report_campaign(cid)
            if not campaign:
                await _render(event, "Campaign not found.")
                return
            if limit == 0:
                db.update_report_campaign(cid, messages=[])
                await show_campaign_edit_menu(event, cid)
                return
            await _render(event, f"Scraping {limit} posts from `{campaign.get('target')}`...")
            try:
                links = await scrape_recent_messages(campaign.get("target"), limit=limit)
                db.update_report_campaign(cid, messages=links, last_result=f"Scraped {len(links)} posts.")
            except Exception as e:
                await event.respond(f"Scrape error: {e}")
            await show_campaign_edit_menu(event, cid)
            return
        if action == "ed_msg_cust" and arg:
            db.set_user_state(user_id, CAMPAIGN_STATE, {"step": "edit_msg_cust", "edit_campaign_id": int(arg)})
            text, buttons = _custom_prompt(
                f"**Scrape Posts for Campaign #{arg}**\n\nSend the number of recent posts to scrape (1 to {CAMPAIGN_MAX_POSTS}):",
                f"cmp:ed_msg:{arg}".encode()
            )
            await _render(event, text, buttons=buttons)
            return
        if action == "ed_dist" and arg:
            await show_edit_dist_menu(event, int(arg))
            return
        if action == "ed_dist_set" and arg:
            cid = int(arg)
            mode = parts[3] if len(parts) > 3 else "all"
            db.update_report_campaign(cid, dist_mode=mode)
            await show_edit_dist_menu(event, cid)
            return
        if action == "ed_rpm" and arg:
            await show_edit_rpm_menu(event, int(arg))
            return
        if action == "ed_rpm_set" and arg:
            cid = int(arg)
            val = int(parts[3]) if len(parts) > 3 else 2
            db.update_report_campaign(cid, reports_per_message=val)
            await show_campaign_edit_menu(event, cid)
            return
        if action == "ed_rpm_cust" and arg:
            db.set_user_state(user_id, CAMPAIGN_STATE, {"step": "edit_rpm_cust", "edit_campaign_id": int(arg)})
            text, buttons = _custom_prompt(
                f"**Custom Reports Per Link for Campaign #{arg}**\n\nSend a number from 1 to 50:",
                f"cmp:ed_rpm:{arg}".encode()
            )
            await _render(event, text, buttons=buttons)
            return
        if action == "ed_dly" and arg:
            await show_edit_delay_menu(event, int(arg))
            return
        if action == "ed_dly_set" and arg:
            cid = int(arg)
            val = int(parts[3]) if len(parts) > 3 else 5
            db.update_report_campaign(cid, delay_seconds=val)
            await show_campaign_edit_menu(event, cid)
            return
        if action == "ed_dly_cust" and arg:
            db.set_user_state(user_id, CAMPAIGN_STATE, {"step": "edit_delay_cust", "edit_campaign_id": int(arg)})
            text, buttons = _custom_prompt(
                f"**Custom Delay for Campaign #{arg}**\n\nSend delay in seconds (1 to 3600):",
                f"cmp:ed_dly:{arg}".encode()
            )
            await _render(event, text, buttons=buttons)
            return
        if action == "ed_time" and arg:
            await show_edit_time_menu(event, int(arg))
            return
        if action == "ed_int_set" and arg:
            cid = int(arg)
            hrs = float(parts[3]) if len(parts) > 3 else 0.0
            db.update_report_campaign(cid, interval_hours=hrs, max_cycles=1 if hrs <= 0 else 0)
            await show_edit_time_menu(event, cid)
            return
        if action == "ed_int_cust" and arg:
            db.set_user_state(user_id, CAMPAIGN_STATE, {"step": "edit_int_cust", "edit_campaign_id": int(arg)})
            text, buttons = _custom_prompt(
                f"**Custom Interval for Campaign #{arg}**\n\nSend hours between cycles (0.1 to 720):",
                f"cmp:ed_time:{arg}".encode()
            )
            await _render(event, text, buttons=buttons)
            return
        if action == "ed_dur_cust" and arg:
            db.set_user_state(user_id, CAMPAIGN_STATE, {"step": "edit_dur_cust", "edit_campaign_id": int(arg)})
            text, buttons = _custom_prompt(
                f"**Custom Duration for Campaign #{arg}**\n\nSend duration in days or hours (e.g. 1.5, 2 days, 36h, or 0 for unlimited):",
                f"cmp:ed_time:{arg}".encode()
            )
            await _render(event, text, buttons=buttons)
            return

        if action == "del" and arg:
            await show_delete_confirm(event, int(arg))
            return
        if action == "del_confirm" and arg:
            cid = int(arg)
            engine = active_campaigns.get(cid)
            if engine is not None:
                engine.should_stop = True
            active_campaigns.pop(cid, None)
            success = db.delete_report_campaign(cid)
            if success:
                await _render(event, f"Campaign #{cid} deleted permanently.",
                              buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
            else:
                await _render(event, "Failed to delete campaign or campaign not found.",
                              buttons=[[Button.inline("Campaigns", b"cmp:list:0"), _main_button()]])
            return
        if action == "ed_acrules" and arg:
            await show_account_rules_menu(event, int(arg))
            return
        if action == "ed_ar_acc" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            await show_account_rule_editor(event, cid, phone)
            return
        if action == "ed_ar_rsn" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            await show_account_reason_picker(event, cid, phone)
            return
        if action == "ed_ar_rsn_def" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            campaign = db.get_report_campaign(cid)
            if campaign:
                rules = dict(campaign.get("account_rules") or {})
                if phone in rules and "reasons" in rules[phone]:
                    del rules[phone]["reasons"]
                    if not rules[phone]:
                        del rules[phone]
                    db.update_report_campaign(cid, account_rules=rules)
            await show_account_rule_editor(event, cid, phone)
            return
        if action == "ed_ar_rsn_pick" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            r_key = parts[4] if len(parts) > 4 else "spam"
            campaign = db.get_report_campaign(cid)
            if campaign:
                labels = {
                    "spam": "Spam",
                    "fake": "Fake Account / Scam",
                    "violence": "Violence",
                    "pornography": "Pornography",
                    "child_abuse": "Child Abuse",
                    "illegal_drugs": "Illegal Drugs",
                    "personal_details": "Personal Details",
                    "copyright": "Copyright",
                }
                reason_obj = {"reason": r_key, "detail": labels.get(r_key, r_key), "option": ""}
                rules = dict(campaign.get("account_rules") or {})
                rule = dict(rules.get(phone) or {})
                rule["reasons"] = [reason_obj]
                rules[phone] = rule
                db.update_report_campaign(cid, account_rules=rules)
            await show_account_rule_editor(event, cid, phone)
            return
        if action == "ed_ar_msg" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            await show_account_message_picker(event, cid, phone)
            return
        if action == "ed_ar_msgtog" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            val = parts[4] if len(parts) > 4 else "all"
            campaign = db.get_report_campaign(cid)
            if campaign:
                rules = dict(campaign.get("account_rules") or {})
                rule = dict(rules.get(phone) or {})
                if val == "all":
                    rule["messages"] = "all"
                else:
                    try:
                        idx = int(val)
                    except ValueError:
                        idx = 0
                    curr = rule.get("messages")
                    if curr == "all" or not isinstance(curr, list):
                        rule["messages"] = [idx]
                    else:
                        s = set(curr)
                        if idx in s:
                            s.remove(idx)
                        else:
                            s.add(idx)
                        rule["messages"] = sorted(list(s))
                        if not rule["messages"]:
                            rule["messages"] = "all"
                rules[phone] = rule
                db.update_report_campaign(cid, account_rules=rules)
            await show_account_message_picker(event, cid, phone)
            return
        if action == "ed_ar_comm" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            db.set_user_state(user_id, CAMPAIGN_STATE, {
                "step": "edit_ar_comm",
                "edit_campaign_id": cid,
                "phone": phone
            })
            text, buttons = _custom_prompt(
                f"**Custom Comment for Account {mask_phone(phone)}**\n\n"
                "Send the custom comment text to send with this account's reports (or type 'none' to remove comment):",
                f"cmp:ed_ar_acc:{cid}:{phone}".encode()
            )
            buttons.insert(0, [Button.inline("Use Campaign Default Comment", f"cmp:ed_ar_comm_clr:{cid}:{phone}".encode())])
            await _render(event, text, buttons=buttons)
            return
        if action == "ed_ar_comm_clr" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            campaign = db.get_report_campaign(cid)
            if campaign:
                rules = dict(campaign.get("account_rules") or {})
                if phone in rules and "comment" in rules[phone]:
                    del rules[phone]["comment"]
                    if not rules[phone]:
                        del rules[phone]
                    db.update_report_campaign(cid, account_rules=rules)
            db.clear_user_state(user_id)
            await show_account_rule_editor(event, cid, phone)
            return
        if action == "ed_ar_reset" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            campaign = db.get_report_campaign(cid)
            if campaign:
                rules = dict(campaign.get("account_rules") or {})
                if phone in rules:
                    del rules[phone]
                    db.update_report_campaign(cid, account_rules=rules)
            await show_account_rules_menu(event, cid)
            return
        if action == "ed_ar_clear_all" and arg:
            cid = int(arg)
            db.update_report_campaign(cid, account_rules={})
            await show_account_rules_menu(event, cid)
            return
        if action == "ed_ar_copy" and arg:
            cid = int(arg)
            phone = parts[3] if len(parts) > 3 else ""
            await show_account_copy_picker(event, cid, phone)
            return
        if action == "ed_ar_copy_to" and arg:
            cid = int(arg)
            src = parts[3] if len(parts) > 3 else ""
            tgt = parts[4] if len(parts) > 4 else ""
            campaign = db.get_report_campaign(cid)
            if campaign and src and tgt:
                rules = dict(campaign.get("account_rules") or {})
                src_rule = rules.get(src)
                import copy
                if src_rule:
                    rules[tgt] = copy.deepcopy(src_rule)
                else:
                    if tgt in rules:
                        del rules[tgt]
                db.update_report_campaign(cid, account_rules=rules)
            await show_account_copy_picker(event, cid, src)
            return
        if action == "ed_ar_copy_all" and arg:
            cid = int(arg)
            src = parts[3] if len(parts) > 3 else ""
            campaign = db.get_report_campaign(cid)
            if campaign and src:
                accounts = db.get_active_accounts()
                assigned = campaign.get("account_phones") or []
                if assigned:
                    accounts = [acc for acc in accounts if acc[1] in set(assigned)]
                rules = dict(campaign.get("account_rules") or {})
                src_rule = rules.get(src)
                import copy
                for acc in accounts:
                    o_phone = acc[1]
                    if o_phone != src:
                        if src_rule:
                            rules[o_phone] = copy.deepcopy(src_rule)
                        else:
                            if o_phone in rules:
                                del rules[o_phone]
                db.update_report_campaign(cid, account_rules=rules)
            await show_account_rules_menu(event, cid)
            return

        flow = _require_flow(event, user_id)
        if not flow:
            await _render(event, 
                "Campaign setup expired. Open Campaigns and start again.",
                buttons=[[Button.inline("Campaigns", b"cmp:menu"), _main_button()]],
            )
            return

        if action == "back_target":
            flow["step"] = "target"
            _save_flow(user_id, flow)
            await show_target_prompt(event, flow)
            return
        if action == "back_accounts":
            flow["step"] = "accounts"
            _save_flow(user_id, flow)
            await show_accounts_prompt(event, flow)
            return
        if action == "back_fetch":
            flow["step"] = "fetch"
            _save_flow(user_id, flow)
            await show_fetch_prompt(event, flow)
            return
        if action == "back_reports":
            flow["step"] = "reports"
            _save_flow(user_id, flow)
            await show_reports_prompt(event, flow)
            return
        if action == "back_delay":
            flow["step"] = "delay"
            _save_flow(user_id, flow)
            await show_delay_prompt(event, flow)
            return
        if action == "back_dist":
            flow["step"] = "dist"
            _save_flow(user_id, flow)
            await show_dist_prompt(event, flow)
            return
        if action == "back_reason":
            pages = flow.get("reason_pages") or []
            if pages:
                flow["reason_page"] = 0
                flow["reason_detail"] = ""
                flow["reason_option"] = ""
                _save_flow(user_id, flow)
                await show_stored_reason_page(event, flow)
            else:
                flow["step"] = "dist"
                _save_flow(user_id, flow)
                await show_dist_prompt(event, flow)
            return
        if action == "back_rsub":
            pages = flow.get("reason_pages") or []
            if len(pages) > 1:
                flow["reason_page"] = 1
                _save_flow(user_id, flow)
                await show_stored_reason_page(event, flow)
            else:
                _save_flow(user_id, flow)
                await show_reason_prompt(event, flow)
            return
        if action == "back_rchoices":
            flow["step"] = "reason_choices"
            _save_flow(user_id, flow)
            await show_reason_choices(event, flow)
            return
        if action == "back_comment":
            flow["step"] = "comment"
            _save_flow(user_id, flow)
            await show_comment_prompt(event, flow)
            return
        if action == "back_interval":
            flow["step"] = "interval"
            _save_flow(user_id, flow)
            await show_interval_prompt(event, flow)
            return
        if action == "back_duration":
            flow["step"] = "duration"
            _save_flow(user_id, flow)
            await show_duration_prompt(event, flow)
            return

        if action == "acc_all":
            flow["accounts_mode"] = "all"
            flow["selected_phones"] = []
            flow["step"] = "fetch"
            _save_flow(user_id, flow)
            await show_fetch_prompt(event, flow)
            return
        if action == "acc_multi":
            flow["accounts_mode"] = "specific"
            flow["selected_phones"] = []
            flow["step"] = "accounts"
            _save_flow(user_id, flow)
            await show_multi_accounts(event, flow)
            return
        if action == "acc_only" and arg:
            flow["accounts_mode"] = "specific"
            flow["selected_phones"] = [arg]
            flow["step"] = "fetch"
            _save_flow(user_id, flow)
            await show_fetch_prompt(event, flow)
            return
        if action == "acc_toggle" and arg:
            selected = list(flow.get("selected_phones") or [])
            if arg in selected:
                selected.remove(arg)
            else:
                selected.append(arg)
            flow["selected_phones"] = selected
            _save_flow(user_id, flow)
            await show_multi_accounts(event, flow)
            return
        if action == "acc_done":
            selected = list(flow.get("selected_phones") or [])
            if not selected:
                await event.answer("Select at least one account.", alert=True)
                return
            flow["accounts_mode"] = "specific"
            flow["step"] = "fetch"
            _save_flow(user_id, flow)
            await show_fetch_prompt(event, flow)
            return

        if action == "fetch_all":
            await _fetch_links(event, user_id, flow, CAMPAIGN_MAX_POSTS)
            return
        if action in ("fetch", "fetch_custom"):
            if action == "fetch_custom":
                flow["step"] = "custom_fetch"
                _save_flow(user_id, flow)
                text, buttons = _custom_prompt(
                    "**Fetch Count**\n\nSend how many recent posts to fetch.",
                    b"cmp:back_fetch",
                )
                await _render(event, text, buttons=buttons)
                return
            await _fetch_links(event, user_id, flow, int(arg or 50))
            return

        if action == "rpm":
            flow["reports_per_message"] = max(1, int(arg or 2))
            flow["step"] = "delay"
            _save_flow(user_id, flow)
            await show_delay_prompt(event, flow)
            return
        if action == "rpm_custom":
            flow["step"] = "custom_rpm"
            _save_flow(user_id, flow)
            text, buttons = _custom_prompt(
                "**Reports Per Link**\n\nSend a number from 1 to 50.",
                b"cmp:back_reports",
            )
            await _render(event, text, buttons=buttons)
            return
        if action == "delay":
            flow["delay"] = max(1, int(arg or 5))
            flow["step"] = "dist"
            _save_flow(user_id, flow)
            await show_dist_prompt(event, flow)
            return
        if action == "dist":
            flow["dist_mode"] = arg or "all"
            flow["step"] = "reason"
            _save_flow(user_id, flow)
            await show_reason_prompt(event, flow)
            return
        if action == "delay_custom":
            flow["step"] = "custom_delay"
            _save_flow(user_id, flow)
            text, buttons = _custom_prompt(
                "**Custom Delay**\n\nSend the delay in seconds.",
                b"cmp:back_delay",
            )
            await _render(event, text, buttons=buttons)
            return
        if action == "radd":
            pages = flow.get("reason_pages") or []
            flow["reason"] = ""
            flow["reason_detail"] = ""
            flow["reason_option"] = ""
            flow["reason_pending_option"] = ""
            flow["reason_page"] = 0
            flow["step"] = "reason"
            _save_flow(user_id, flow)
            if pages:
                await show_stored_reason_page(event, flow)
            else:
                await show_reason_prompt(event, flow)
            return
        if action == "rremove":
            if _remove_reason_choice(flow):
                _save_flow(user_id, flow)
                await show_reason_choices(event, flow)
            else:
                await event.answer("No report type to remove.", alert=True)
            return
        if action == "rcontinue":
            choices = _reason_choices(flow)
            if not choices:
                _append_reason_choice(flow, "spam", "Spam", "")
                _append_reason_choice(flow, "fake", "Fake Account / Scam", "")
            elif len(choices) == 1:
                primary = (choices[0].get("reason") or "spam").lower()
                sec = "fake" if "spam" in primary else ("violence" if "fake" in primary else "spam")
                sec_label = "Fake Account / Scam" if sec == "fake" else sec.capitalize()
                _append_reason_choice(flow, sec, sec_label, "")
            
            flow["reports_per_message"] = max(2, int(flow.get("reports_per_message") or 2))
            flow["step"] = "comment"
            _save_flow(user_id, flow)
            await show_comment_prompt(event, flow)
            return
        if action == "ropt" and arg is not None:
            try:
                index = int(arg)
            except ValueError:
                index = -1
            pages = flow.get("reason_pages") or []
            page_index = max(0, int(flow.get("reason_page") or 0))
            options = pages[page_index] if page_index < len(pages) else []
            if not (0 <= index < len(options)):
                await event.answer("That Telegram option is no longer available.", alert=True)
                return
            selected = options[index]
            selected_text = selected.get("text") or "Option"
            selected_option = selected.get("option") or ""
            if page_index > 0 or not selected_option:
                reason_key = reason_key_from_label(selected_text)
                _append_reason_choice(
                    flow,
                    reason_key,
                    selected_text,
                    selected_option,
                )
                flow["reason_detail"] = selected_text
                flow["reason_option"] = selected_option
                flow["step"] = "reason_choices"
                _save_flow(user_id, flow)
                await show_reason_choices(event, flow)
                return
            flow["reason"] = selected_text
            flow["reason_detail"] = ""
            flow["reason_option"] = ""
            flow["reason_pending_option"] = selected_option
            _save_flow(user_id, flow)
            await load_reason_next_page(event, flow, selected_option, selected_text)
            return
        if action == "rretry":
            pending = flow.get("reason_pending_option") or ""
            if pending:
                await load_reason_next_page(
                    event, flow, pending, flow.get("reason") or "selected reason"
                )
            else:
                await show_reason_prompt(event, flow)
            return
        if action == "rskip":
            selected_option = flow.get("reason_pending_option") or ""
            if not selected_option:
                await event.answer("Choose a Telegram reason first.", alert=True)
                return
            _append_reason_choice(
                flow,
                flow.get("reason") or "Other",
                "",
                selected_option,
            )
            flow["reason_detail"] = ""
            flow["reason_option"] = selected_option
            flow["step"] = "reason_choices"
            _save_flow(user_id, flow)
            await show_reason_choices(event, flow)
            return
        if action == "rsub" and arg is not None:
            try:
                index = int(arg)
            except ValueError:
                index = -1
            pages = flow.get("reason_pages") or []
            options = pages[1] if len(pages) > 1 else []
            if not (0 <= index < len(options)):
                await event.answer("That Telegram option is no longer available.", alert=True)
                return
            selected = options[index]
            _append_reason_choice(
                flow,
                flow.get("reason") or "Other",
                selected.get("text") or "",
                selected.get("option") or "",
            )
            flow["reason_detail"] = selected.get("text") or ""
            flow["reason_option"] = selected.get("option") or ""
            flow["step"] = "reason_choices"
            _save_flow(user_id, flow)
            await show_reason_choices(event, flow)
            return
        if action in ("comm_all", "comment_yes"):
            flow["step"] = "comment_text"
            _save_flow(user_id, flow)
            await _render(event,
                "**Comment for All Report Types**\n\nSend the comment to attach to all reports.",
                buttons=[[Button.inline("Back to Comments", b"cmp:back_comment"), _cancel_button()]],
            )
            return
        if action == "comm_idx" and arg is not None:
            try:
                idx = int(arg)
            except ValueError:
                idx = 0
            flow["step"] = f"comment_idx_{idx}"
            _save_flow(user_id, flow)
            choices = _reason_choices(flow)
            detail = choices[idx].get("detail") or choices[idx].get("reason") if idx < len(choices) else f"Type {idx+1}"
            await _render(event,
                f"**Comment for Type {idx+1}: {detail}**\n\n"
                "Send the specific comment to include when reporting with this reason.",
                buttons=[[Button.inline("Back to Comments", b"cmp:back_comment"), _cancel_button()]],
            )
            return
        if action == "comm_multi":
            flow["step"] = "comment_multi"
            _save_flow(user_id, flow)
            await _render(event,
                "**Paste Separate Comments**\n\n"
                "Send separate comments on different lines.\n"
                "Line 1 = Comment for Type 1\n"
                "Line 2 = Comment for Type 2\n"
                "and so on.",
                buttons=[[Button.inline("Back to Comments", b"cmp:back_comment"), _cancel_button()]],
            )
            return
        if action in ("comm_done", "comment_done"):
            flow["step"] = "interval"
            _save_flow(user_id, flow)
            await show_interval_prompt(event, flow)
            return
        if action == "comment_no":
            if flow.get("comment_required"):
                await event.answer("Telegram requires a comment for this option.", alert=True)
                return
            flow["comment"] = ""
            for c in _reason_choices(flow):
                c["comment"] = ""
            if flow.get("edit_campaign_id"):
                await _finish_reason_edit(event, user_id, flow)
                return
            flow["step"] = "interval"
            _save_flow(user_id, flow)
            await show_interval_prompt(event, flow)
            return
        if action == "interval_once":
            flow["interval_hours"] = 0
            flow["duration_hours"] = 0
            flow["max_cycles"] = 1
            flow["step"] = "summary"
            _save_flow(user_id, flow)
            await show_summary(event, flow)
            return
        if action == "interval":
            flow["interval_hours"] = max(0.1, float(arg or 2))
            flow["max_cycles"] = 0
            flow["step"] = "duration"
            _save_flow(user_id, flow)
            await show_duration_prompt(event, flow)
            return
        if action == "interval_custom":
            flow["step"] = "custom_interval"
            _save_flow(user_id, flow)
            text, buttons = _custom_prompt(
                "**Custom Interval**\n\nSend the number of hours between cycles, for example 2.",
                b"cmp:back_interval",
            )
            await _render(event, text, buttons=buttons)
            return
        if action == "duration":
            flow["duration_hours"] = max(0.0, float(arg or 0))
            flow["step"] = "summary"
            _save_flow(user_id, flow)
            await show_summary(event, flow)
            return
        if action == "duration_custom":
            flow["step"] = "custom_duration"
            _save_flow(user_id, flow)
            text, buttons = _custom_prompt(
                "**Custom Run Duration**\n\nSend the duration in days, or use "
                "an h suffix for hours. Examples: 1.5, 2 days, 36h. Use 0 for "
                "unlimited.",
                b"cmp:back_duration",
            )
            await _render(event, text, buttons=buttons)
            return
        if action == "create":
            await create_campaign(event, user_id, flow)
            return
        if action == "create_start":
            await create_campaign(event, user_id, flow, start_now=True)
            return

        await event.answer("This campaign option is no longer available.", alert=True)
    except Exception as exc:
        error = redact_sensitive(exc, 300)
        log_activity(f"Campaign callback error for user {user_id}: {error}")
        try:
            await _render(event, 
                "Campaign action failed.\n\n"
                f"`{error}`",
                buttons=[[Button.inline("Campaigns", b"cmp:menu"), _main_button()]],
            )
        except Exception:
            pass


async def handle_campaign_text(event, data: dict):
    """Handle text input for the campaign wizard."""
    user_id = event.sender_id
    if not db.is_admin(user_id):
        await event.respond("Admin only.")
        return
    step = (data or {}).get("step")
    text = (event.text or "").strip()

    if step == "name":
        if len(text) > 80:
            await event.respond("Name must be 80 characters or fewer.")
            return
        data["name"] = text
        data["step"] = "target"
        _save_flow(user_id, data)
        await event.respond(
            _target_prompt(data),
            buttons=[[Button.inline("Change Name", b"cmp:name_edit"), _cancel_button()]],
        )
        return

    if step == "target":
        if not text:
            await event.respond("Send a channel username or link.")
            return
        data["target"] = text
        data["step"] = "accounts"
        _save_flow(user_id, data)
        await show_accounts_prompt(event, data)
        return

    if step == "custom_fetch":
        try:
            limit = int(text)
        except ValueError:
            await event.respond("Send a valid number.")
            return
        if not 1 <= limit <= CAMPAIGN_MAX_POSTS:
            await event.respond(f"Send a number from 1 to {CAMPAIGN_MAX_POSTS}.")
            return
        await _fetch_links(event, user_id, data, limit)
        return

    if step == "custom_rpm":
        try:
            count = int(text)
        except ValueError:
            await event.respond("Send a valid number.")
            return
        if not 1 <= count <= 50:
            await event.respond("Send a number from 1 to 50.")
            return
        data["reports_per_message"] = count
        data["step"] = "delay"
        _save_flow(user_id, data)
        await show_delay_prompt(event, data)
        return

    if step == "custom_delay":
        try:
            delay = int(text)
        except ValueError:
            await event.respond("Send a valid number.")
            return
        if not 1 <= delay <= 3600:
            await event.respond("Send a delay from 1 to 3600 seconds.")
            return
        data["delay"] = delay
        data["step"] = "dist"
        _save_flow(user_id, data)
        await show_dist_prompt(event, data)
        return

    if step == "edit_target":
        cid = int(data.get("edit_campaign_id") or 0)
        if not text:
            await event.respond("Send a valid channel username or link.")
            return
        campaign = db.get_report_campaign(cid)
        update_fields = {"target": text, "last_result": "Target updated."}
        if campaign and (not campaign.get("name") or campaign.get("name") == campaign.get("target")):
            update_fields["name"] = text
        db.update_report_campaign(cid, **update_fields)
        db.clear_user_state(user_id)
        await show_campaign_edit_menu(event, cid)
        return

    if step == "edit_msg_cust":
        cid = int(data.get("edit_campaign_id") or 0)
        try:
            limit = int(text)
        except ValueError:
            await event.respond("Send a valid number.")
            return
        if not 1 <= limit <= CAMPAIGN_MAX_POSTS:
            await event.respond(f"Send a number from 1 to {CAMPAIGN_MAX_POSTS}.")
            return
        campaign = db.get_report_campaign(cid)
        if not campaign:
            await event.respond("Campaign not found.")
            return
        await event.respond(f"Scraping {limit} posts from `{campaign.get('target')}`...")
        try:
            links = await scrape_recent_messages(campaign.get("target"), limit=limit)
            db.update_report_campaign(cid, messages=links, last_result=f"Scraped {len(links)} posts.")
        except Exception as e:
            await event.respond(f"Scrape error: {e}")
            return
        db.clear_user_state(user_id)
        await show_campaign_edit_menu(event, cid)
        return

    if step == "edit_rpm_cust":
        cid = int(data.get("edit_campaign_id") or 0)
        try:
            count = int(text)
        except ValueError:
            await event.respond("Send a valid number.")
            return
        if not 1 <= count <= 50:
            await event.respond("Send a number from 1 to 50.")
            return
        db.update_report_campaign(cid, reports_per_message=count)
        db.clear_user_state(user_id)
        await show_campaign_edit_menu(event, cid)
        return

    if step == "edit_delay_cust":
        cid = int(data.get("edit_campaign_id") or 0)
        try:
            dly = int(text)
        except ValueError:
            await event.respond("Send a valid number.")
            return
        if not 1 <= dly <= 3600:
            await event.respond("Send a delay from 1 to 3600 seconds.")
            return
        db.update_report_campaign(cid, delay_seconds=dly)
        db.clear_user_state(user_id)
        await show_campaign_edit_menu(event, cid)
        return

    if step == "edit_int_cust":
        cid = int(data.get("edit_campaign_id") or 0)
        try:
            hours = float(text.replace(",", "."))
        except ValueError:
            await event.respond("Send a valid number of hours.")
            return
        if not 0.1 <= hours <= 720:
            await event.respond("Send an interval from 0.1 to 720 hours.")
            return
        db.update_report_campaign(cid, interval_hours=hours, max_cycles=0)
        db.clear_user_state(user_id)
        await show_campaign_edit_menu(event, cid)
        return

    if step == "edit_dur_cust":
        cid = int(data.get("edit_campaign_id") or 0)
        hours = _parse_duration(text)
        if hours is None:
            await event.respond("Send a valid duration, for example 1.5, 2 days or 36h.")
            return
        if hours > 8760:
            await event.respond("Send a duration of 365 days or less.")
            return
        db.update_report_campaign(cid, duration_hours=hours)
        db.clear_user_state(user_id)
        await show_campaign_edit_menu(event, cid)
        return

    if step == "edit_ar_comm":
        cid = int(data.get("edit_campaign_id") or 0)
        phone = data.get("phone") or ""
        campaign = db.get_report_campaign(cid)
        if not campaign:
            await event.respond("Campaign not found.")
            return
        rules = dict(campaign.get("account_rules") or {})
        rule = dict(rules.get(phone) or {})
        clean_txt = text.strip()
        if clean_txt.lower() in ("none", "clear", "default"):
            if "comment" in rule:
                del rule["comment"]
        else:
            rule["comment"] = clean_txt[:1000]
        if rule:
            rules[phone] = rule
        else:
            if phone in rules:
                del rules[phone]
        db.update_report_campaign(cid, account_rules=rules)
        db.clear_user_state(user_id)
        await show_account_rule_editor(event, cid, phone)
        return

    if step == "comment_text":
        if not text:
            await event.respond("Send a comment, or tap Back and choose No Comment.")
            return
        data["comment"] = text[:1000]
        choices = _reason_choices(data)
        for c in choices:
            if not c.get("comment"):
                c["comment"] = text[:1000]
        data["reason_choices"] = choices
        if data.get("edit_campaign_id"):
            await _finish_reason_edit(event, user_id, data)
            return
        data["step"] = "interval"
        _save_flow(user_id, data)
        await show_interval_prompt(event, data)
        return

    if step and str(step).startswith("comment_idx_"):
        try:
            idx = int(str(step).split("_")[-1])
        except Exception:
            idx = 0
        choices = _reason_choices(data)
        if 0 <= idx < len(choices):
            choices[idx]["comment"] = text[:1000]
            data["reason_choices"] = choices

        next_idx = None
        for i in range(idx + 1, len(choices)):
            if not choices[i].get("comment"):
                next_idx = i
                break

        if next_idx is not None:
            data["step"] = f"comment_idx_{next_idx}"
            _save_flow(user_id, data)
            next_detail = choices[next_idx].get("detail") or choices[next_idx].get("reason") or f"Type {next_idx+1}"
            await event.respond(
                f"**Comment for Type {next_idx+1}: {next_detail}**\n\n"
                "Send the comment for this report type, or tap Done.",
                buttons=[
                    [Button.inline("Done with comments", b"cmp:comm_done")],
                    [Button.inline("Back to Comments", b"cmp:back_comment")],
                ]
            )
            return

        data["step"] = "comment"
        _save_flow(user_id, data)
        await show_comment_prompt(event, data)
        return

    if step == "comment_multi":
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        choices = _reason_choices(data)
        for i, choice in enumerate(choices):
            if i < len(lines):
                choice["comment"] = lines[i][:1000]
        data["reason_choices"] = choices
        data["step"] = "comment"
        _save_flow(user_id, data)
        await show_comment_prompt(event, data)
        return

    if step == "custom_interval":
        try:
            hours = float(text.replace(",", "."))
        except ValueError:
            await event.respond("Send a valid number of hours.")
            return
        if not 0.1 <= hours <= 720:
            await event.respond("Send an interval from 0.1 to 720 hours.")
            return
        data["interval_hours"] = hours
        data["max_cycles"] = 0
        data["step"] = "duration"
        _save_flow(user_id, data)
        await show_duration_prompt(event, data)
        return

    if step == "custom_duration":
        hours = _parse_duration(text)
        if hours is None:
            await event.respond("Send a valid duration, for example 1.5, 2 days or 36h.")
            return
        if hours > 8760:
            await event.respond("Send a duration of 365 days or less.")
            return
        data["duration_hours"] = hours
        data["step"] = "summary"
        _save_flow(user_id, data)
        await show_summary(event, data)
        return

    await event.respond(
        "Campaign setup expired. Open Campaigns and start again.",
        buttons=[[Button.inline("Campaigns", b"cmp:menu"), _main_button()]],
    )


@bot.on(events.NewMessage(pattern=r"/campaign(?:@\w+)?$"))
async def campaign_command(event):
    """Handle /campaign."""
    await show_campaign_menu(event, send=True)


@bot.on(events.NewMessage(pattern=r"/options(?:@\w+)?$"))
async def report_options_command(event):
    """Show the real Telegram report reason list."""
    if not db.is_admin(event.sender_id):
        await event.respond("Admin only.")
        return
    await show_report_options(event)

