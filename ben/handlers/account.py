from telethon import events, Button

from bot import bot, db, active_operations
from config import MAX_ACCOUNTS_PER_USER, ADMIN_ONLY_IMPORTS
from utils.helpers import is_user_authorized, mask_phone


def _login_count_buttons(user_id: int):
    presets = [1, 2, 3, 5, 10, MAX_ACCOUNTS_PER_USER]
    values = []
    for value in presets:
        if 1 <= value <= MAX_ACCOUNTS_PER_USER and value not in values:
            values.append(value)
    rows = []
    row = []
    for value in values:
        row.append(Button.inline(f"{value} account{'s'if value > 1 else ''}", f"login_count:{value}".encode()))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Button.inline("Custom Count", b"login_custom")])
    if db.is_admin(user_id) or not ADMIN_ONLY_IMPORTS:
        rows.append([Button.inline("Import .session / ZIP", b"rv:imp")])
    rows.append([Button.inline("Main Menu", b"menu:home")])
    return rows


async def show_login_menu(event, send: bool = True):
    """Show the login options with inline buttons."""
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        if send:
            await event.respond("Access denied.")
        else:
            await event.edit("Access denied.")
        return

    # Starting a new login menu cancels any previous OTP/2FA wait.
    operation = active_operations.get(user_id)
    if operation:
        data = operation.get('data') or {}
        data['cancelled'] = True
        try:
            operation['event'].set()
        except Exception:
            pass

    db.set_user_state(user_id, "login_count", {"step": 1})
    text = (
        "**Account Login**\n\n"
        "How many accounts do you want to log in? Choose a count below or send a "
        f"custom number from 1 to {MAX_ACCOUNTS_PER_USER}.\n\n"
        "For each account, the bot asks for the phone number and then OTP/2FA in this "
        "private chat.\n\n"
        "A proxy is optional. Use the format `[phone] [proxy]`."
    )
    buttons = _login_count_buttons(user_id)
    if send:
        await event.respond(text, buttons=buttons)
    else:
        try:
            await event.edit(text, buttons=buttons)
        except Exception:
            await event.respond(text, buttons=buttons)


async def begin_login_count(event, data: dict, count: int, send: bool = True):
    """Start the multi-account login flow after a count is chosen."""
    user_id = event.sender_id
    if count < 1 or count > MAX_ACCOUNTS_PER_USER:
        text = f"Please enter a number between 1 and {MAX_ACCOUNTS_PER_USER}."
        if send:
            await event.respond(text)
        else:
            await event.edit(text, buttons=[[Button.inline("Back", b"menu:login")]])
        return

    data = dict(data or {})
    data["step"] = 1
    data["total"] = count
    data["current"] = 0
    data["phones"] = []
    db.set_user_state(user_id, "login_phone", data)

    text = (
        f"**Account 1/{count}**\n\n"
        "Send the phone number with country code.\n"
        "Optional proxy format: `[phone] [proxy]`\n\n"
        "Example: `+91XXXXXXXXXX`\n"
        "Example: `+91XXXXXXXXXX socks5://12.34.56.78:1080`"
    )
    buttons = [
        [Button.inline("Cancel Login", b"ui_cancel")],
        [Button.inline("Main Menu", b"menu:home")],
    ]
    if send:
        await event.respond(text, buttons=buttons)
    else:
        try:
            await event.edit(text, buttons=buttons)
        except Exception:
            await event.respond(text, buttons=buttons)


@bot.on(events.NewMessage(pattern='/login'))
async def login_command(event):
    """Handle /login command."""
    await show_login_menu(event, send=True)


@bot.on(events.NewMessage(pattern='/list_accounts'))
async def list_accounts_command(event):
    """Handle /list_accounts command."""
    await show_accounts_menu(event, send=True)


async def show_accounts_menu(event, send: bool = True):
    """Display a compact account overview with navigation buttons."""
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        if send:
            await event.respond("Access denied.")
        else:
            await event.edit("Access denied.")
        return

    accounts = db.get_accounts()
    if accounts:
        active = sum(1 for acc in accounts if acc[3] == "active"and not acc[6] and acc[12])
        text = f"**My Accounts**\n\nTotal: {len(accounts)} | Active: {active}\n\n"
        for acc in accounts:
            if acc[6]:
                state = "banned"
            elif not acc[12]:
                state = "disabled"
            elif acc[3] == "active":
                state = "active"
            else:
                state = acc[3] or "unknown"
            text += f"`{mask_phone(acc[1])}` - {state}\n"
    else:
        text = (
            "**My Accounts**\n\n"
            "No account is logged in yet.\n\n"
            "Use Login for phone/OTP login, or an admin can import a .session/ZIP archive."
        )

    buttons = [
        [Button.inline("Refresh", b"menu:accounts"),
         Button.inline("Login", b"menu:login")],
        [Button.inline("Start Reporting", b"menu:report")],
    ]
    if db.is_admin(user_id):
        buttons.append([Button.inline("Manage Sessions", b"rv:accounts:1")])
        buttons.append([Button.inline("Import Sessions", b"rv:imp")])
    buttons.append([Button.inline("Main Menu", b"menu:home")])

    if send:
        await event.respond(text, buttons=buttons)
    else:
        try:
            await event.edit(text, buttons=buttons)
        except Exception:
            await event.respond(text, buttons=buttons)


@bot.on(events.NewMessage(pattern='/remove_account'))
async def remove_account_command(event):
    """Handle /remove_account command."""
    user_id = event.sender_id

    if not is_user_authorized(user_id):
        await event.respond("Access denied.")
        return

    await event.respond(
        "Which account do you want to remove?\n"
        "Send the phone number:",
        buttons=[[Button.inline("Main Menu", b"menu:home")]],
    )

    db.set_user_state(user_id, "remove_account", {"step": 1})
