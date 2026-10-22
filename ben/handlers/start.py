from telethon import events, Button

from bot import bot, db, active_reports
from utils.helpers import get_user_status, log_activity


def _main_menu_buttons(status: str, user_id: int):
    if status == "admin":
        buttons = [
            [Button.inline("Start Reporting", b"menu:report"),
             Button.inline("Reporting Campaigns", b"menu:campaign")],
            [Button.inline("Review Manager", b"rv:dash"),
             Button.inline("Login / Import", b"menu:login")],
            [Button.inline("My Accounts", b"menu:accounts"),
             Button.inline("Statistics", b"menu:stats")],
            [Button.inline("Approved Users", b"menu:users"),
             Button.inline("Broadcast", b"menu:broadcast")],
            [Button.inline("Session Manager", b"rv:accounts:1"),
             Button.inline("Import Sessions", b"rv:imp")],
            [Button.inline("Help", b"menu:help")],
        ]
    else:
        buttons = [
            [Button.inline("Start Reporting", b"menu:report"),
             Button.inline("Review Manager", b"rv:dash")],
            [Button.inline("Login / Import", b"menu:login"),
             Button.inline("My Accounts", b"menu:accounts")],
            [Button.inline("Statistics", b"menu:stats"),
             Button.inline("Help", b"menu:help")],
        ]
    if user_id in active_reports:
        buttons.append([Button.inline("Stop Active Report", b"stop_report")])
    return buttons


async def show_main_menu(event, send: bool = True):
    """Render the main menu in both command and callback contexts."""
    user_id = event.sender_id
    try:
        sender = await event.get_sender()
    except Exception:
        sender = None

    username = getattr(sender, "username", None) or "Unknown"
    first_name = getattr(sender, "first_name", None) or "User"
    status = get_user_status(user_id)

    if status == "admin":
        # Keep the admin marked as an approved user for backwards compatibility.
        if getattr(event, "is_private", True) and not db.is_approved(user_id):
            try:
                db.approve_user(user_id, username, user_id)
            except Exception:
                pass

        text = (
            f"**Admin Control Center**\n\n"
            f"Welcome {first_name}.\n\n"
            "Use the buttons below to manage reporting, campaigns, accounts and reviews. "
            "Reporting joins the target with every active account first, then sends "
            "reports only from accounts that joined.\n\n"
            "Reporting Campaigns store a target, all fetched message links, one or more "
            "accounts, reports per link, delay, reason and a repeat interval.\n\n"
            "Review Manager sends reminders and tracks manual review status. "
            "It never submits reports automatically."
        )
        buttons = _main_menu_buttons("admin", user_id)
    elif status == "approved":
        text = (
            f"**User Panel**\n\n"
            f"Welcome {first_name}.\n\n"
            "**Reporting:** choose a target, select messages, then confirm. "
            "The bot joins the target with the active accounts before reporting.\n\n"
            "**Review Manager:** manage planned manual reviews, categories, reminders and history."
        )
        buttons = _main_menu_buttons("approved", user_id)
    else:
        log_activity(f"Unauthorized access attempt by {user_id} (@{username})")
        text = (
            "**Access Denied**\n\n"
            "You are not authorized to use this bot.\n"
            "Please contact the admin for access.\n\n"
            f"Your User ID: `{user_id}`\n"
            "Share this ID with the admin to get approved."
        )
        buttons = None

    if send:
        await event.respond(text, buttons=buttons)
    else:
        try:
            await event.edit(text, buttons=buttons)
        except Exception:
            await event.respond(text, buttons=buttons)


@bot.on(events.NewMessage(pattern=r"/(?:start|menu)(?:@\w+)?$"))
async def start_command(event):
    """Handle /start and /menu."""
    await show_main_menu(event, send=True)
