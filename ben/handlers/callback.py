from datetime import datetime

from telethon import events, Button

from bot import bot, db, active_reports, active_operations
from config import MAX_ACCOUNTS_PER_USER, MAX_REPORTS_PER_ACCOUNT, MIN_DELAY, MAX_DELAY
from utils.helpers import (
    ReportEngine,
    join_accounts_to_target,
    log_activity,
)
from utils.report_options import load_report_choices
from utils.review_utils import redact_sensitive

from handlers.start import show_main_menu
from handlers.account import show_login_menu, show_accounts_menu, begin_login_count
from handlers.report import (
    show_report_start,
    show_target_type,
    show_manual_target_prompt,
    show_scrape_prompt,
    show_manual_entry,
    run_scrape_flow,
    show_delay_step,
    show_exec_mode_step,
    show_comment_choice,
    show_message_reason_step,
    show_stored_reason_page,
    show_peer_reason_step,
)
from handlers.report_ui import reason_display, reason_key_from_label
from handlers.admin import (
    show_stats,
    list_users_command,
    broadcast_command,
    show_help,
)


@bot.on(events.CallbackQuery)
async def handle_callbacks(event):
    """Handle all callback queries."""
    user_id = event.sender_id
    data = event.data.decode('utf-8')

    # Review manager has its own callback namespace.
    if data.startswith("rv:"):
        from handlers.review import handle_review_callback
        await handle_review_callback(event, data)
        return

    # Reporting campaigns have their own callback namespace and answer
    # their own callbacks after the admin check.
    if data.startswith("cmp:"):
        from handlers.campaign import handle_campaign_callback
        await handle_campaign_callback(event, data)
        return

    try:
        await event.answer()
    except Exception:
        pass

    try:
        await _route_callback(event, user_id, data)
    except Exception as exc:
        error = redact_sensitive(exc, 300)
        log_activity(f"Callback error for user {user_id} ({data[:80]}): {error}")
        try:
            await event.answer("Something went wrong. Please use /start.", alert=True)
        except Exception:
            pass


async def _route_callback(event, user_id: int, data: str):
    # ------------------------------------------------------------------
    # Main menu / navigation namespace
    # ------------------------------------------------------------------
    if data.startswith("menu:"):
        action = data.split(":", 1)[1]
        if action == "home":
            _cancel_operations(user_id)
            db.clear_user_state(user_id)
            await show_main_menu(event, send=False)
        elif action == "report":
            await show_report_start(event, edit=True)
        elif action == "login":
            await show_login_menu(event, send=False)
        elif action == "accounts":
            _cancel_operations(user_id)
            db.clear_user_state(user_id)
            await show_accounts_menu(event, send=False)
        elif action == "stats":
            db.clear_user_state(user_id)
            await show_stats(event, edit=True)
        elif action == "help":
            db.clear_user_state(user_id)
            await show_help(event, edit=True)
        elif action == "review":
            db.clear_user_state(user_id)
            from handlers.review import show_dashboard
            await show_dashboard(event, send=False)
        elif action == "campaign":
            _cancel_operations(user_id)
            db.clear_user_state(user_id)
            from handlers.campaign import show_campaign_menu
            await show_campaign_menu(event, send=False)
        elif action == "users":
            db.clear_user_state(user_id)
            if not db.is_admin(user_id):
                await event.edit("Admin only.")
                return
            await list_users_command(event)
        elif action == "broadcast":
            db.clear_user_state(user_id)
            await broadcast_command(event)
        elif action == "cancel":
            _cancel_operations(user_id)
            db.clear_user_state(user_id)
            await show_main_menu(event, send=False)
        else:
            await event.answer("This menu option is no longer available.", alert=True)
        return

    # ------------------------------------------------------------------
    # Login buttons
    # ------------------------------------------------------------------
    if data.startswith("login_count:"):
        _, data_state = db.get_user_state(user_id)
        try:
            count = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await event.answer("Invalid count.", alert=True)
            return
        await begin_login_count(event, data_state, count, send=False)
        return

    if data == "login_custom":
        db.set_user_state(user_id, "login_count", {"step": 1})
        await event.edit(
            f"Send the number of accounts to log in (1-{MAX_ACCOUNTS_PER_USER}):",
            buttons=[
                [Button.inline("Back", b"menu:login")],
                [Button.inline("Main Menu", b"menu:home")],
            ],
        )
        return

    # ------------------------------------------------------------------
    # Generic preset pickers
    # ------------------------------------------------------------------
    if data.startswith("count_pick:"):
        await _handle_count_pick(event, user_id, data)
        return

    if data.startswith("scrape_pick:"):
        await _handle_count_pick(event, user_id, data)
        return

    if data.startswith("delay_pick:"):
        state, data_state = db.get_user_state(user_id)
        if state != "report_delay":
            await event.edit("Session expired. Start again from the menu.")
            return
        try:
            delay = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await event.answer("Invalid delay.", alert=True)
            return
        data_state['delay'] = delay
        await show_exec_mode_step(event, data_state, send=False, peer=False)
        return

    if data == "ui_custom_count":
        await _show_custom_input_prompt(event, user_id)
        return

    if data == "ui_cancel":
        _cancel_operations(user_id)
        db.clear_user_state(user_id)
        await event.edit(
            "Operation cancelled.",
            buttons=[
                [Button.inline("Start Reporting", b"menu:report"),
                 Button.inline("Main Menu", b"menu:home")],
            ],
        )
        return

    # ------------------------------------------------------------------
    # Saved report target
    # ------------------------------------------------------------------
    if data.startswith("report_saved:"):
        try:
            channel_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await event.answer("Invalid channel.", alert=True)
            return
        channel = db.get_review_channel(channel_id)
        if not channel:
            await event.edit(
                "Saved channel not found. Start again.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
            return
        target = channel.get("identifier") or channel.get("username") or ""
        if not target:
            await event.edit(
                "Saved channel has no usable identifier.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
            return
        await show_target_type(event, {"step": 1, "target": target}, send=False)
        return

    if data == "report_manual_target":
        await show_manual_target_prompt(event, send=False)
        return

    # ------------------------------------------------------------------
    # Target type / message selection
    # ------------------------------------------------------------------
    if data == "target_messages":
        state, data_state = db.get_user_state(user_id)
        if not data_state or not data_state.get('target'):
            await event.edit("Session expired. Start again with the menu.")
            return
        data_state['step'] = 2
        db.set_user_state(user_id, "report_target", data_state)
        await event.edit(
            "**Step 2/6 - Message Links Selection**\n\n"
            f"Target: `{data_state['target']}`\n\n"
            "Do you want to fetch recent posts automatically, or send message links manually?",
            buttons=[
                [Button.inline("Auto Scrape Recent", b"scrape_recent"),
                 Button.inline("Manual Links", b"manual_entry")],
                [Button.inline("Back", b"menu:report")],
                [Button.inline("Main Menu", b"menu:home")],
            ],
        )

    elif data == "target_peer":
        state, data_state = db.get_user_state(user_id)
        if not data_state or not data_state.get('target'):
            await event.edit("Session expired. Start again with the menu.")
            return
        target = str(data_state.get('target', ''))
        target_lower = target.lower()
        if (
            't.me/+'in target_lower
            or 'joinchat/'in target_lower
            or target.strip().startswith('+')
            or 'tg://join'in target_lower
        ):
            await event.edit(
                "Private invite targets do not support whole-channel peer reports.\n"
                "Select specific messages instead.",
                buttons=[
                    [Button.inline("Specific Messages", b"target_messages")],
                    [Button.inline("Main Menu", b"menu:home")],
                ],
            )
            return
        from handlers.report import show_peer_count_step
        await show_peer_count_step(event, data_state, send=False)

    elif data == "scrape_recent":
        state, data_state = db.get_user_state(user_id)
        if not data_state or not data_state.get('target'):
            await event.edit("Session expired. Start again with the menu.")
            return
        await show_scrape_prompt(event, data_state, send=False)

    elif data == "manual_entry":
        state, data_state = db.get_user_state(user_id)
        if not data_state or not data_state.get('target'):
            await event.edit("Session expired. Start again with the menu.")
            return
        await show_manual_entry(event, data_state, send=False)

    # ------------------------------------------------------------------
    # Execution mode
    # ------------------------------------------------------------------
    elif data in ("exec_mode_turbo", "exec_mode_safe"):
        state, data_state = db.get_user_state(user_id)
        if not data_state:
            await event.edit("Session expired. Start again with the menu.")
            return
        data_state['exec_mode'] = 'turbo'if data.endswith('turbo') else 'safe'
        await show_comment_choice(event, data_state, send=False, peer=False)

    elif data in ("peer_exec_mode_turbo", "peer_exec_mode_safe"):
        state, data_state = db.get_user_state(user_id)
        if not data_state:
            await event.edit("Session expired. Start again with the menu.")
            return
        data_state['exec_mode'] = 'turbo'if data.endswith('turbo') else 'safe'
        await show_comment_choice(event, data_state, send=False, peer=True)

    # ------------------------------------------------------------------
    # Comment choice
    # ------------------------------------------------------------------
    elif data == "comment_yes":
        state, data_state = db.get_user_state(user_id)
        db.set_user_state(user_id, "report_comment", data_state)
        await event.edit(
            "**Comment**\n\nSend the comment or explanation:",
            buttons=[[Button.inline("Main Menu", b"menu:home")]],
        )

    elif data == "comment_no":
        state, data_state = db.get_user_state(user_id)
        data_state['comment'] = ""
        await show_message_reason_step(event, data_state, send=False)

    elif data == "peer_comment_yes":
        state, data_state = db.get_user_state(user_id)
        db.set_user_state(user_id, "report_peer_comment", data_state)
        await event.edit(
            "**Comment**\n\nSend the comment or explanation:",
            buttons=[[Button.inline("Main Menu", b"menu:home")]],
        )

    elif data == "peer_comment_no":
        state, data_state = db.get_user_state(user_id)
        data_state['comment'] = ""
        await show_peer_reason_step(event, data_state, send=False, state="report_peer_reason")

    # ------------------------------------------------------------------
    # Report reasons
    # ------------------------------------------------------------------
    elif data == "reason_back":
        state, data_state = db.get_user_state(user_id)
        if not data_state:
            await event.edit(
                "Report session expired. Start again from the menu.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
            return
        pages = data_state.get("reason_pages") or []
        page_index = int(data_state.get("reason_page") or 0)
        if pages and page_index > 0:
            data_state["reason_page"] = page_index - 1
            data_state["reason_detail"] = ""
            data_state["reason_option"] = ""
            await show_stored_reason_page(event, data_state)
        else:
            await show_message_reason_step(event, data_state, send=False)

    elif data == "reason_retry":
        state, data_state = db.get_user_state(user_id)
        if not data_state:
            await event.edit(
                "Report session expired. Start again from the menu.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
            return
        await show_message_reason_step(event, data_state, send=False)

    elif data == "reason_skip":
        state, data_state = db.get_user_state(user_id)
        if not data_state:
            await event.edit(
                "Report session expired. Start again from the menu.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
            return
        data_state["reason_option"] = (
            data_state.get("reason_pending_option")
            or data_state.get("reason_option")
            or ""
        )
        data_state["reason_detail"] = ""
        data_state["comment_required"] = False
        db.set_user_state(user_id, "report_reason", data_state)
        await confirm_report(event, data_state.get("reason") or "Other")

    elif data.startswith("reason_opt:"):
        state, data_state = db.get_user_state(user_id)
        try:
            index = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            index = -1
        pages = (data_state or {}).get("reason_pages") or []
        page_index = max(0, int((data_state or {}).get("reason_page") or 0))
        options = pages[page_index] if page_index < len(pages) else []
        if not data_state or not (0 <= index < len(options)):
            await event.edit(
                "This Telegram option is no longer available. Start again.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
            return

        selected = options[index]
        selected_text = selected.get("text") or "Option"
        selected_option = selected.get("option") or ""

        if page_index > 0:
            data_state["reason_detail"] = selected_text
            data_state["reason_option"] = selected_option
            db.set_user_state(user_id, "report_reason", data_state)
            await confirm_report(event, data_state.get("reason") or "Other")
            return

        data_state["reason"] = selected_text
        data_state["reason_key"] = reason_key_from_label(selected_text)
        data_state["reason_pending_option"] = selected_option
        data_state["reason_detail"] = ""
        data_state["reason_option"] = ""
        try:
            await event.edit(
                "Loading the exact next report options from Telegram...",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
        except Exception:
            pass

        result = await load_report_choices(
            None,
            data_state.get("target") or "",
            (data_state.get("messages") or [""])[0],
            option_bytes=bytes.fromhex(selected_option),
        )

        if result.get("kind") == "choose" and result.get("options"):
            data_state["reason_pages"] = pages[:1] + [result["options"]]
            data_state["reason_page"] = 1
            data_state["reason_option"] = ""
            data_state["comment_required"] = False
            db.set_user_state(user_id, "report_reason", data_state)
            await show_stored_reason_page(event, data_state)
            return

        if result.get("kind") == "comment":
            data_state["reason_option"] = result.get("option") or selected_option
            data_state["comment_required"] = not bool(result.get("optional"))
            data_state["reason_detail"] = ""
            db.set_user_state(user_id, "report_reason", data_state)
            await confirm_report(event, selected_text)
            return

        if result.get("kind") == "reported":
            data_state["reason_option"] = selected_option
            data_state["comment_required"] = False
            data_state["reason_detail"] = ""
            db.set_user_state(user_id, "report_reason", data_state)
            await confirm_report(event, selected_text)
            return

        error = result.get("error") or "Telegram did not return the next options."
        data_state["reason_option"] = selected_option
        data_state["comment_required"] = False
        db.set_user_state(user_id, "report_reason", data_state)
        await event.edit(
            "**Telegram Options Unavailable**\n\n"
            f"{error}\n\n"
            "You can retry, go back to the reasons, or continue with the real "
            "reason you selected.",
            buttons=[
                [Button.inline("Try Again", b"reason_retry")],
                [Button.inline("Continue with this reason", b"reason_skip")],
                [Button.inline("Back to Reasons", b"reason_back")],
                [Button.inline("Main Menu", b"menu:home")],
            ],
        )

    elif data.startswith("peer_reason_"):
        reason_map = {
            "peer_reason_spam": "spam",
            "peer_reason_violence": "violence",
            "peer_reason_child_abuse": "child_abuse",
            "peer_reason_fake": "fake",
            "peer_reason_pornography": "pornography",
            "peer_reason_illegal_drugs": "illegal_drugs",
            "peer_reason_personal_details": "personal_details",
            "peer_reason_copyright": "copyright",
            "peer_reason_geo_irrelevant": "geo_irrelevant",
            "peer_reason_other": "other",
        }
        reason = reason_map.get(data, "other")
        state, data_state = db.get_user_state(user_id)
        if not data_state:
            await event.edit(
                "Report session expired. Start again from the menu.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
            return
        data_state['reason'] = reason
        data_state['reason_detail'] = ""
        db.set_user_state(user_id, state, data_state)
        await start_peer_report_session(event, reason)

    # ------------------------------------------------------------------
    # User / bot peer type
    # ------------------------------------------------------------------
    elif data.startswith("peer_type_"):
        peer_type = data.split('_')[2]
        data_state = {"type": peer_type, "step": 2}
        db.set_user_state(user_id, "report_user_target", data_state)
        await event.edit(
            f"**Report {peer_type.capitalize()}**\n\n"
            f"Send the {peer_type} username or ID:",
            buttons=[[Button.inline("Main Menu", b"menu:home")]],
        )

    # ------------------------------------------------------------------
    # Confirmation / session start
    # ------------------------------------------------------------------
    elif data == "confirm_yes":
        await start_report_session(event)

    elif data == "peer_retry":
        state, data_state = db.get_user_state(user_id)
        pending_reason = (data_state or {}).get('pending_reason')
        if not pending_reason:
            await event.edit(
                "Retry session expired. Start again.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
            return
        await start_peer_report_session(event, pending_reason)

    elif data == "confirm_no":
        db.clear_user_state(user_id)
        await event.edit(
            "Report cancelled.",
            buttons=[
                [Button.inline("Start Reporting", b"menu:report"),
                 Button.inline("Main Menu", b"menu:home")],
            ],
        )

    # ------------------------------------------------------------------
    # Backwards-compatible navigation callbacks
    # ------------------------------------------------------------------
    elif data == "start_report":
        await show_report_start(event, edit=True)

    elif data == "login_prompt":
        await show_login_menu(event, send=False)

    elif data == "view_stats":
        db.clear_user_state(user_id)
        await show_stats(event, edit=True)

    elif data == "list_accounts":
        _cancel_operations(user_id)
        db.clear_user_state(user_id)
        await show_accounts_menu(event, send=False)

    elif data == "list_users":
        db.clear_user_state(user_id)
        await list_users_command(event)

    elif data == "broadcast_prompt":
        db.clear_user_state(user_id)
        await broadcast_command(event)

    elif data == "help_menu":
        db.clear_user_state(user_id)
        await show_help(event, edit=True)

    elif data == "stop_report":
        engine = active_reports.get(user_id, {}).get('engine')
        if engine:
            engine.should_stop = True
            await event.edit(
                "Stop request sent.",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
        else:
            await event.edit(
                "No report session is active.",
                buttons=[[Button.inline("Start Reporting", b"menu:report")],
                         [Button.inline("Main Menu", b"menu:home")]],
            )

    else:
        await event.answer("This option is no longer available.", alert=True)


async def _handle_count_pick(event, user_id: int, data: str):
    state, data_state = db.get_user_state(user_id)
    try:
        count = int(data.split(":", 1)[1])
    except (TypeError, ValueError):
        await event.answer("Invalid number.", alert=True)
        return

    if state == "login_count":
        await begin_login_count(event, data_state, count, send=False)
        return

    if state == "report_scrape_count":
        await run_scrape_flow(event, data_state, count, edit=True)
        return

    if state == "report_count":
        if not 1 <= count <= MAX_REPORTS_PER_ACCOUNT:
            await event.answer(f"Choose 1-{MAX_REPORTS_PER_ACCOUNT}.", alert=True)
            return
        data_state['reports_per_account'] = count
        await show_delay_step(event, data_state, send=False)
        return

    if state == "report_peer_count":
        if not 1 <= count <= MAX_REPORTS_PER_ACCOUNT:
            await event.answer(f"Choose 1-{MAX_REPORTS_PER_ACCOUNT}.", alert=True)
            return
        data_state['reports_per_account'] = count
        await show_exec_mode_step(event, data_state, send=False, peer=True)
        return

    if state == "report_user_count":
        if not 1 <= count <= MAX_REPORTS_PER_ACCOUNT:
            await event.answer(f"Choose 1-{MAX_REPORTS_PER_ACCOUNT}.", alert=True)
            return
        data_state['reports_per_account'] = count
        if data_state.get('type') == 'bot':
            db.set_user_state(user_id, "report_user_comment", data_state)
            await event.edit(
                "**Comment**\n\nSend the comment or explanation for the bot report:",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
        else:
            await show_peer_reason_step(
                event, data_state, send=False, state="report_user_reason"
            )
        return

    await event.edit("Session expired. Start again from the menu.")


async def _show_custom_input_prompt(event, user_id: int):
    state, _ = db.get_user_state(user_id)
    prompts = {
        "login_count": f"Send the number of accounts to log in (1-{MAX_ACCOUNTS_PER_USER}):",
        "report_scrape_count": "Send how many recent messages to fetch (1-100):",
        "report_count": f"Send reports per account (1-{MAX_REPORTS_PER_ACCOUNT}):",
        "report_peer_count": f"Send reports per account (1-{MAX_REPORTS_PER_ACCOUNT}):",
        "report_user_count": f"Send reports per account (1-{MAX_REPORTS_PER_ACCOUNT}):",
        "report_delay": f"Send the delay in seconds ({MIN_DELAY}-{MAX_DELAY}):",
    }
    text = prompts.get(state)
    if not text:
        await event.edit("Session expired. Start again from the menu.")
        return
    back = b"menu:login"if state == "login_count"else b"menu:home"
    await event.edit(
        text,
        buttons=[
            [Button.inline("Back", back)],
            [Button.inline("Main Menu", b"menu:home")],
        ],
    )


def _cancel_operations(user_id: int):
    operation = active_operations.get(user_id)
    if operation:
        data = operation.get('data') or {}
        data['cancelled'] = True
        try:
            operation['event'].set()
        except Exception:
            pass


async def confirm_report(event, reason: str):
    """Show confirmation before starting a report."""
    user_id = event.sender_id
    state, data = db.get_user_state(user_id)

    if not state or not data:
        await event.edit("Session expired! Start again from the menu.")
        return

    data['reason'] = reason
    db.set_user_state(user_id, "confirming", data)

    active_accounts = db.get_active_accounts()
    total_accounts = len(active_accounts)

    if total_accounts == 0:
        text = (
            "No active accounts are available.\n\n"
            "Log in or import a session first, then start reporting."
        )
        buttons = [
            [Button.inline("Login", b"menu:login"),
             Button.inline("Accounts", b"menu:accounts")],
            [Button.inline("Main Menu", b"menu:home")],
        ]
        try:
            await event.edit(text, buttons=buttons)
        except Exception:
            await event.respond(text, buttons=buttons)
        return

    if data.get('comment_required') and not (data.get('comment') or '').strip():
        db.set_user_state(user_id, "report_required_comment", data)
        await event.edit(
            "**Comment Required**\n\n"
            "Telegram requires a comment for this report reason. Send the "
            "comment now.",
            buttons=[
                [Button.inline("Back to Reasons", b"reason_back")],
                [Button.inline("Main Menu", b"menu:home")],
            ],
        )
        return

    total_reports = total_accounts * data['reports_per_account'] * len(data['messages'])
    reason_text = reason_display(reason, data.get('reason_detail'))

    confirm_text = (
        f"**Report Summary**\n\n"
        f"Target: `{data['target']}`\n"
        f"Messages to report: {len(data['messages'])}\n"
        f"Reports per account: {data['reports_per_account']}\n"
        f"Execution mode: {data.get('exec_mode', 'safe').capitalize()}\n"
        f"Estimated reports: {total_reports}\n"
        f"Delay: {data.get('delay', 0)}s\n"
        f"Comment: {data.get('comment') or 'No'}\n"
        f"Reason: {reason_text}\n"
        f"Active accounts: {total_accounts}\n\n"
        f"**All active accounts will join the target before reporting starts.** "
        f"Accounts that fail to join will be skipped.\n\n"
        f"Start this report session?"
    )

    buttons = [
        [Button.inline("Join All & Start", b"confirm_yes")],
        [Button.inline("Cancel", b"confirm_no")],
    ]
    try:
        await event.edit(confirm_text, buttons=buttons)
    except Exception:
        await event.respond(confirm_text, buttons=buttons)


async def start_report_session(event):
    """Join all active accounts, then start reporting with joined accounts."""
    user_id = event.sender_id
    state, data = db.get_user_state(user_id)

    if not state or not data:
        await event.edit("Session expired! Start again from the menu.")
        return
    if user_id in active_reports:
        await event.edit(
            "A report is already running.",
            buttons=[[Button.inline("Stop Report", b"stop_report")],
                     [Button.inline("Main Menu", b"menu:home")]],
        )
        return
    if not data.get('messages'):
        await event.edit(
            "No messages selected. Start again.",
            buttons=[[Button.inline("Main Menu", b"menu:home")]],
        )
        return

    progress_message = await event.edit("Checking and joining all active accounts...")

    # Preflight: every active account must join the target before reporting.
    join_results = await join_accounts_to_target(data['target'], progress_message)
    joined = [item for item in join_results if item.get('joined')]
    failed = [item for item in join_results if not item.get('joined')]
    allowed_phones = {item['phone'] for item in joined}

    if not joined:
        if not join_results:
            reason = "No active account is available."
        else:
            reason = join_results[0].get('message') or "the accounts could not join the target."
        await progress_message.edit(
            f"Reporting cannot start.\n\n{reason}\n\n"
            "Check the accounts and log in if needed, then retry.",
            buttons=[
                [Button.inline("Retry Join", b"confirm_yes")],
                [Button.inline("Login", b"menu:login"),
                 Button.inline("Accounts", b"menu:accounts")],
                [Button.inline("Main Menu", b"menu:home")],
            ],
        )
        return

    engine = ReportEngine(user_id)
    active_reports[user_id] = {
        'engine': engine,
        'started_at': datetime.now(),
    }

    db.clear_user_state(user_id)

    try:
        exec_mode = data.get('exec_mode', 'safe')
        if exec_mode == 'turbo':
            await engine.run_parallel_report_session(
                target=data['target'],
                message_links=data['messages'],
                reports_per_account=data['reports_per_account'],
                reason=data['reason'],
                comment=data.get('comment', ''),
                progress_message=progress_message,
                allowed_phones=allowed_phones,
                reason_option=data.get('reason_option') or '',
            )
        else:
            await engine.run_report_session(
                target=data['target'],
                message_links=data['messages'],
                reports_per_account=data['reports_per_account'],
                delay=data.get('delay', 5),
                reason=data['reason'],
                comment=data.get('comment', ''),
                progress_message=progress_message,
                allowed_phones=allowed_phones,
                reason_option=data.get('reason_option') or '',
            )
    except Exception as exc:
        error = redact_sensitive(exc, 300)
        log_activity(f"Report session error for user {user_id}: {error}")
        try:
            await progress_message.edit(
                f"Report session error: {error}",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
        except Exception:
            pass
    finally:
        active_reports.pop(user_id, None)

    note = ""
    if failed:
        note = f"\n{len(failed)} account(s) could not join and were skipped."
    await event.respond(
        f"Report session finished.{note}\n\nWhat would you like to do next?",
        buttons=[
            [Button.inline("New Report", b"menu:report"),
             Button.inline("Stats", b"menu:stats")],
            [Button.inline("Main Menu", b"menu:home")],
        ],
    )


async def start_peer_report_session(event, reason: str):
    """Start report session for a user/bot target."""
    user_id = event.sender_id
    state, data = db.get_user_state(user_id)

    if not state or not data or not data.get('target'):
        await event.edit("Session expired! Start again with the menu.")
        return
    if user_id in active_reports:
        await event.edit(
            "A report is already running.",
            buttons=[[Button.inline("Stop Report", b"stop_report")],
                     [Button.inline("Main Menu", b"menu:home")]],
        )
        return

    target = data['target']
    reports_per_account = data.get('reports_per_account', 1)
    comment = data.get('comment', '')
    is_channel_target = 'type'not in data
    data['pending_reason'] = reason
    db.set_user_state(user_id, state, data)
    allowed_phones = None
    failed = []

    if is_channel_target:
        progress_message = await event.edit(
            "Checking and joining all active accounts to the target..."
        )
        join_results = await join_accounts_to_target(target, progress_message)
        joined = [item for item in join_results if item.get('joined')]
        failed = [item for item in join_results if not item.get('joined')]
        allowed_phones = {item['phone'] for item in joined}
        if not joined:
            reason_text = (
                "No active account is available."
                if not join_results
                else (join_results[0].get('message') or "the accounts could not join the target.")
            )
            await progress_message.edit(
                f"Reporting cannot start.\n\n{reason_text}",
                buttons=[
                    [Button.inline("Retry Join", b"peer_retry")],
                    [Button.inline("Login", b"menu:login"),
                     Button.inline("Accounts", b"menu:accounts")],
                    [Button.inline("Main Menu", b"menu:home")],
                ],
            )
            return
    else:
        progress_message = await event.edit(
            f"Starting report session for `{target}`..."
        )

    engine = ReportEngine(user_id)
    active_reports[user_id] = {
        'engine': engine,
        'started_at': datetime.now(),
    }

    db.clear_user_state(user_id)

    try:
        exec_mode = data.get('exec_mode', 'safe')
        if exec_mode == 'turbo':
            await engine.run_parallel_peer_report_session(
                peer_id=target,
                reason=reason,
                reports_per_account=reports_per_account,
                comment=comment,
                progress_message=progress_message,
                allowed_phones=allowed_phones,
            )
        else:
            await engine.run_peer_report_session(
                peer_id=target,
                reason=reason,
                reports_per_account=reports_per_account,
                comment=comment,
                progress_message=progress_message,
                allowed_phones=allowed_phones,
            )
    except Exception as exc:
        error = redact_sensitive(exc, 300)
        log_activity(f"Peer report session error for user {user_id}: {error}")
        try:
            await progress_message.edit(
                f"Report session error: {error}",
                buttons=[[Button.inline("Main Menu", b"menu:home")]],
            )
        except Exception:
            pass
    finally:
        active_reports.pop(user_id, None)

    note = ""
    if failed:
        note = f"\n{len(failed)} account(s) could not join and were skipped."
    label = "Channel/Group peer"if is_channel_target else "User/Bot"
    await event.respond(
        f"{label} report session finished.{note}\n\nWhat would you like to do next?",
        buttons=[
            [Button.inline("New Report", b"menu:report"),
             Button.inline("Stats", b"menu:stats")],
            [Button.inline("Main Menu", b"menu:home")],
        ],
    )
