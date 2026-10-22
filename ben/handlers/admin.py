from telethon import events, Button
from bot import bot, db, active_reports
from utils.helpers import is_user_authorized, log_activity


def _stats_text() -> str:
    stats = db.get_stats()
    return (
        f"**Bot Statistics**\n\n"
        f"**Accounts:**\n"
        f"Total: {stats['accounts']['total']}\n"
        f"Active: {stats['accounts']['active']}\n"
        f"Banned: {stats['accounts']['banned']}\n\n"
        f"**Today's Reports:**\n"
        f"Total: {stats['today']['total']}\n"
        f"Success: {stats['today']['success']}\n"
        f"Failed: {stats['today']['failed']}\n\n"
        f"**All Time:**\n"
        f"Total: {stats['all_time']['total']}\n"
        f"Success: {stats['all_time']['success']}\n"
        f"Failed: {stats['all_time']['failed']}\n\n"
        f"Active report sessions: {'Yes'if active_reports else 'No'}"
    )


def _stats_buttons():
    return [
        [Button.inline("Refresh", b"menu:stats"),
         Button.inline("Report", b"menu:report")],
        [Button.inline("Main Menu", b"menu:home")],
    ]


async def show_stats(event, edit: bool = False):
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        if edit:
            await event.edit("Access denied.")
        else:
            await event.respond("Access denied.")
        return
    text = _stats_text()
    if edit:
        try:
            await event.edit(text, buttons=_stats_buttons())
        except Exception:
            await event.respond(text, buttons=_stats_buttons())
    else:
        await event.respond(text, buttons=_stats_buttons())


@bot.on(events.NewMessage(pattern='/stats'))
async def stats_command(event):
    """Handle /stats command."""
    await show_stats(event, edit=False)


def _help_text() -> str:
    return (
        "**Help Menu**\n\n"
        "**Button-driven flow:**\n"
        "Use /start or /menu and follow the inline buttons. No steps to remember.\n\n"
        "**Reporting:**\n"
        "1. Start Reporting\n"
        "2. Choose or paste the target channel\n"
        "3. Select specific messages or the whole channel peer\n"
        "4. Choose count, delay, execution mode, comment and the real Telegram reason\n"
        "5. On confirm, the bot joins the target with all active accounts, "
        "then only joined accounts send reports\n\n"
        "**Reporting Campaigns (admin):**\n"
        "A campaign stores one selected account or several accounts, every fetched "
        "message link, reports per link, delay, one or more real Telegram report "
        "types, optional comment, repeat interval and total run duration such as 1 "
        "or 2 days. After choosing a real type, tap Add Another Reason to send a "
        "different type on the same message. Review the summary, then tap Create and "
        "Start Now or Create Campaign. The scheduler runs each cycle and stops "
        "automatically when the deadline is reached. A campaign without a final "
        "Telegram option cannot start; use Choose Real Reasons first.\n\n"
        "**Telegram report options:**\n"
        "Telegram supports ten reason types: Spam, Fake Account, Violence, Child "
        "Abuse, Pornography, Illegal Drugs, Personal Details, Copyright, Geo "
        "Irrelevant and Other. When Telegram returns further real options, the bot "
        "shows that exact list. Telegram receives the selected real option and the "
        "optional comment only. The Report Options screen explains each one.\n\n"
        "**Accounts:**\n"
        "Log in with phone/OTP/2FA, or an admin can import .session/ZIP files. "
        "Use My Accounts to view status.\n\n"
        "**Review Manager:**\n"
        "Plans, channel posts, per-message categories/explanations, reminders, history, "
        "audit logs and CSV/JSON export. Review plans only send reminders and track "
        "manual review status. They never submit reports automatically.\n\n"
        "**Useful commands:**\n"
        "/start, /menu, /login, /report, /campaign, /options, /stop, /cancel, /stats, "
        "/review, /review_help, /help\n\n"
        "Maximum 50 reports per account per session. The minimum delay is set in config."
    )


async def show_help(event, edit: bool = False):
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        if edit:
            await event.edit("Access denied.")
        else:
            await event.respond("Access denied.")
        return
    text = _help_text()
    buttons = [
        [Button.inline("Start Reporting", b"menu:report"),
         Button.inline("Review Manager", b"rv:dash")],
        [Button.inline("Main Menu", b"menu:home")],
    ]
    if edit:
        try:
            await event.edit(text, buttons=buttons)
        except Exception:
            await event.respond(text, buttons=buttons)
    else:
        await event.respond(text, buttons=buttons)


@bot.on(events.NewMessage(pattern='/help'))
async def help_command(event):
    """Handle /help command."""
    await show_help(event, edit=False)


@bot.on(events.NewMessage(pattern='/add_admin'))
async def add_admin_command(event):
    """Handle /add_admin command - Admin only."""
    user_id = event.sender_id

    if not db.is_admin(user_id):
        await event.respond("Admin only command.")
        return

    if event.is_reply:
        reply_msg = await event.get_reply_message()
        target_user = await reply_msg.get_sender()
        target_id = target_user.id
        target_username = target_user.username or "Unknown"

        if db.add_admin(target_id, target_username, user_id):
            await event.respond(f"User {target_id} added as admin.")
        else:
            await event.respond("Failed to add admin.")
    else:
        await event.respond(
            "Reply to a user's message with /add_admin to add them as admin."
        )


@bot.on(events.NewMessage(pattern='/remove_admin'))
async def remove_admin_command(event):
    """Handle /remove_admin command - Admin only."""
    user_id = event.sender_id

    if not db.is_admin(user_id):
        await event.respond("Admin only command.")
        return

    parts = event.text.split()
    if len(parts) > 1:
        try:
            target_id = int(parts[1])
            if db.remove_admin(target_id):
                await event.respond(f"Admin {target_id} removed.")
            else:
                await event.respond("The default admin cannot be removed.")
        except ValueError:
            await event.respond("Invalid user ID.")
    else:
        await event.respond("Usage: /remove_admin [user_id]")


@bot.on(events.NewMessage(pattern='/approve'))
async def approve_command(event):
    """Handle /approve command - Admin only."""
    user_id = event.sender_id

    if not db.is_admin(user_id):
        await event.respond("Admin only command.")
        return

    if event.is_reply:
        reply_msg = await event.get_reply_message()
        target_user = await reply_msg.get_sender()
        target_id = target_user.id
        target_username = target_user.username or "Unknown"

        if db.approve_user(target_id, target_username, user_id):
            await event.respond(f"User {target_id} approved.")
            log_activity(f"User {target_id} approved by admin {user_id}")
        else:
            await event.respond("Failed to approve user.")
    else:
        parts = event.text.split()
        if len(parts) > 1:
            try:
                target_id = int(parts[1])
                if db.approve_user(target_id, "Unknown", user_id):
                    await event.respond(f"User {target_id} approved.")
                else:
                    await event.respond("Failed to approve user.")
            except ValueError:
                await event.respond("Invalid user ID.")
        else:
            await event.respond(
                "Reply to a user's message with /approve or use /approve [user_id]."
            )


@bot.on(events.NewMessage(pattern='/revoke'))
async def revoke_command(event):
    """Handle /revoke command - Admin only."""
    user_id = event.sender_id

    if not db.is_admin(user_id):
        await event.respond("Admin only command.")
        return

    parts = event.text.split()
    if len(parts) > 1:
        try:
            target_id = int(parts[1])
            if db.revoke_user(target_id):
                await event.respond(f"User {target_id} access revoked.")
                log_activity(f"User {target_id} revoked by admin {user_id}")
            else:
                await event.respond("Failed to revoke user.")
        except ValueError:
            await event.respond("Invalid user ID.")
    else:
        await event.respond("Usage: /revoke [user_id]")


@bot.on(events.NewMessage(pattern='/list_users'))
async def list_users_command(event):
    """Handle /list_users command."""
    user_id = event.sender_id

    if not db.is_admin(user_id):
        await event.respond("Admin only command.")
        return

    admins = db.get_all_admins()
    users = db.get_all_approved_users()

    text = "**Admins:**\n"
    for admin in admins:
        text += f"- {admin[0]} (@{admin[1]}) {'[Default]'if admin[4] else ''}\n"

    text += "\n**Approved Users:**\n"
    for user in users:
        text += f"- {user[0]} (@{user[1]}) - Approved by {user[2]}\n"

    if len(text) > 4000:
        parts = [text[i:i + 4000] for i in range(0, len(text), 4000)]
        for part in parts:
            await event.respond(part, parse_mode="markdown")
        await event.respond(
            "Use the buttons below to continue.",
            buttons=[[Button.inline("Main Menu", b"menu:home")]],
        )
    else:
        await event.respond(
            text,
            parse_mode="markdown",
            buttons=[[Button.inline("Main Menu", b"menu:home")]],
        )


@bot.on(events.NewMessage(pattern='/broadcast'))
async def broadcast_command(event):
    """Handle /broadcast command - Admin only."""
    user_id = event.sender_id

    if not db.is_admin(user_id):
        await event.respond("Admin only command.")
        return

    await event.respond(
        "Send the broadcast message that approved users should receive:",
        buttons=[
            [Button.inline("Cancel", b"ui_cancel")],
            [Button.inline("Main Menu", b"menu:home")],
        ],
    )

    db.set_user_state(user_id, "broadcast", {"step": 1})


@bot.on(events.NewMessage(pattern='/check'))
async def check_command(event):
    """Handle /check command - Admin diagnostics for accounts and proxies."""
    user_id = event.sender_id

    if not is_user_authorized(user_id):
        await event.respond("Access denied.")
        return

    accounts = db.get_accounts()
    if not accounts:
        await event.respond("No accounts are registered in the database.")
        return

    progress = await event.respond(f"Starting diagnostic audit for {len(accounts)} accounts...")

    report_lines = []

    from telethon import TelegramClient
    from utils.helpers import API_ID, API_HASH, get_telethon_proxy, mask_phone
    import asyncio

    for idx, acc in enumerate(accounts):
        phone = acc[1]
        session_file = acc[2]
        proxy_type, proxy_ip, proxy_port = acc[4], acc[5], acc[6]
        proxy_user, proxy_pass = acc[7], acc[8]

        proxy_str = "Direct connection"
        if proxy_ip and proxy_port:
            proxy_str = f"{proxy_type or 'socks5'}://{proxy_ip}:{proxy_port}"

        client = None
        conn_ok = False
        err_msg = ""

        try:
            proxy_tuple = get_telethon_proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
            client = TelegramClient(session_file.replace('.session', ''), API_ID, API_HASH, proxy=proxy_tuple)
            await client.connect()

            if await client.is_user_authorized():
                conn_ok = True
                db.update_account_status(phone, "active")
            else:
                db.update_account_status(phone, "banned")
                err_msg = "Not authorized (banned or session expired)"

            await client.disconnect()
        except Exception as e:
            err_msg = str(e)
            db.update_account_status(phone, "error", True)

        masked = mask_phone(phone)
        status_display = "active"if conn_ok else ("banned"if "authorized"in err_msg else "error")

        report_lines.append(
            f"**Account #{idx + 1}**: `{masked}`\n"
            f"   - **Proxy**: `{proxy_str}`\n"
            f"   - **Status**: `{status_display.upper()}`"
            + (f"\n   - **Error**: `{err_msg}`"if err_msg else "")
        )

        await progress.edit(f"Auditing accounts: {idx + 1}/{len(accounts)} processed...")
        await asyncio.sleep(0.2)

    final_text = "**Account and Proxy Diagnostics Audit**\n\n" + "\n\n".join(report_lines)

    if len(final_text) > 4000:
        parts = [final_text[i:i + 4000] for i in range(0, len(final_text), 4000)]
        for part in parts:
            await event.respond(part, parse_mode="markdown")
    else:
        await progress.edit(final_text, parse_mode="markdown")


@bot.on(events.NewMessage(pattern='/upload_proxies'))
async def upload_proxies_prompt(event):
    """Prompt the user to upload a proxy .txt file."""
    user_id = event.sender_id
    if not is_user_authorized(user_id):
        await event.respond("Access denied.")
        return

    db.set_user_state(user_id, "upload_proxies", {"step": 1})
    await event.respond(
        "**Upload Proxies (.txt)**\n\n"
        "Send a `.txt` file with one proxy per line, or paste the proxies as a text message.\n\n"
        "**Supported formats:**\n"
        "- `ip:port`\n"
        "- `ip:port:user:pass`\n"
        "- `socks5://user:pass@ip:port`\n"
        "- `http://user:pass@ip:port`\n\n"
        "The bot distributes the proxies across your registered accounts."
    )
