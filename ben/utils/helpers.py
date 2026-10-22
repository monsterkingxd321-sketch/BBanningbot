import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from telethon import TelegramClient, types, errors as tg_errors
from telethon.tl.functions.messages import ReportRequest, ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.account import ReportPeerRequest, UpdateStatusRequest
from config import LOGS_FOLDER, SESSIONS_FOLDER, API_ID, API_HASH
from bot import db, active_operations
from utils.review_utils import redact_sensitive
from utils.message_utils import is_reportable_post

def mask_phone(phone: str) -> str:
    """Mask phone number for display"""
    if phone and len(phone) > 6:
        return phone[:4] + "XXXX" + phone[-3:]
    return phone or "unknown"

def create_progress_bar(percentage: float, length: int = 10) -> str:
    """Create a plain-text progress bar."""
    filled = int(length * percentage / 100)
    bar = "#" * filled + "-" * (length - filled)
    return f"[{bar}] {percentage:.1f}%"

def log_activity(message: str):
    """Log activities to file, redacting phone numbers and tokens."""
    log_file = os.path.join(LOGS_FOLDER, f"bot_log_{datetime.now().strftime('%Y%m%d')}.txt")
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    safe_message = redact_sensitive(message, 1000)
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(f"[{timestamp}] {safe_message}\n")

def is_user_authorized(user_id: int) -> bool:
    """Check if user is authorized (admin or approved)"""
    return db.is_admin(user_id) or db.is_approved(user_id)

def get_user_status(user_id: int) -> str:
    """Get user status"""
    if db.is_admin(user_id):
        return "admin"
    elif db.is_approved(user_id):
        return "approved"
    else:
        return "unauthorized"

def get_telethon_proxy(proxy_type: str, proxy_ip: str, proxy_port: int, proxy_user: str = None, proxy_pass: str = None):
    """Parse proxy configuration to Telethon format with DNS pre-resolution"""
    if not proxy_ip or not proxy_port:
        return None
    try:
        import socks
        import socket
        t_map = {
            'socks5': socks.SOCKS5,
            'socks4': socks.SOCKS4,
            'http': socks.HTTP
        }
        ptype = t_map.get(str(proxy_type or 'socks5').lower().strip(), socks.SOCKS5)
        ip_clean = str(proxy_ip).strip()
        if "://"in ip_clean:
            ip_clean = ip_clean.split("://", 1)[1]
            
        # Pre-resolve proxy hostname to plain IP to prevent gaierror [Errno 11001]
        try:
            resolved_ip = socket.gethostbyname(ip_clean)
        except Exception:
            # Fallback to original clean IP if resolution fails (e.g. offline)
            resolved_ip = ip_clean
            
        return (ptype, resolved_ip, int(proxy_port), True, (proxy_user or '').strip() or None, (proxy_pass or '').strip() or None)
    except Exception as e:
        log_activity(f"Proxy parsing error: {e}")
        return None

async def set_account_online(client: TelegramClient, offline: bool = False):
    """Set the Telegram account status to online (offline=False) or offline (offline=True)."""
    try:
        if client and client.is_connected():
            await client(UpdateStatusRequest(offline=offline))
    except Exception:
        pass

def parse_message_link(message_link: str) -> tuple:
    """Return ``(chat_reference, message_id)`` for a Telegram message link.

    Public links use the channel username. Private ``/c/`` links use the numeric
    channel id. ``tg://privatepost`` links are accepted as well.
    """
    raw = (message_link or "").strip()
    if not raw:
        raise ValueError("Message link is empty.")

    if raw.lower().startswith("tg://privatepost"):
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(raw).query)
        channel = (query.get("channel") or [""])[0]
        post = (query.get("post") or [""])[0]
        if channel and post:
            return int(f"-100{channel}"), int(post)
        raise ValueError("Private post link is invalid.")

    if "/c/" in raw:
        parts = raw.split("/c/", 1)
        sub_parts = parts[1].split("/")
        if len(sub_parts) >= 2 and sub_parts[0].isdigit() and sub_parts[1].isdigit():
            return int(f"-100{sub_parts[0]}"), int(sub_parts[1])
        raise ValueError("Private message link is invalid.")

    clean_link = raw.replace("https://", "").replace("http://", "")
    clean_link = clean_link.replace("t.me/", "").replace("telegram.me/", "")
    parts = clean_link.split("?")[0].split("/")
    if len(parts) >= 2:
        chat_ref, message_id = parts[0], parts[1]
        try:
            return chat_ref, int(message_id)
        except ValueError:
            pass
    raise ValueError("Message link is invalid.")


def parse_target_identifier(target: str) -> tuple:
    """Parse a public username/link or a private invite link.

    Returns ``("username", value)``, ``("id", value)`` or ``("invite", hash)``.
    Message links such as ``https://t.me/channel/123`` are treated as the
    channel part so they cannot accidentally be reported as a target.
    """
    raw = (target or "").strip()
    if not raw:
        raise ValueError("Target is empty.")

    value = raw
    if value.lower().startswith("tg://join?invite="):
        invite = value.split("invite=", 1)[1].split("&", 1)[0]
        if invite:
            return "invite", invite
    if "://"in value:
        value = value.split("://", 1)[1]
    if value.lower().startswith(("t.me/", "telegram.me/")):
        value = value.split("/", 1)[1]
    value = value.split("?", 1)[0].strip("/")

    if value.startswith("joinchat/"):
        invite = value.split("/", 1)[1]
        if not invite:
            raise ValueError("Invite link has no hash.")
        return "invite", invite
    if value.startswith("+"):
        invite = value[1:].split("/", 1)[0]
        if not invite:
            raise ValueError("Invite link has no hash.")
        return "invite", invite
    if value.startswith("@"):
        value = value[1:]
    if value.startswith("c/"):
        raise ValueError("Private message links cannot be used as a join target.")

    # For t.me/channel/123, the join target is the channel username.
    value = value.split("/", 1)[0]
    if not value:
        raise ValueError("Target is invalid.")
    try:
        return "id", int(value)
    except ValueError:
        return "username", value


async def ensure_account_joined(client: TelegramClient, target: str) -> tuple:
    """Join *target* with *client* and return ``(joined, message)``.

    ``UserAlreadyParticipantError`` means the account is already a member and is
    therefore a success. A pending join request is intentionally treated as a
    failure because the account cannot report on the target yet.
    """
    kind, value = parse_target_identifier(target)

    async def _join_once():
        if kind == "invite":
            await client(ImportChatInviteRequest(value))
            return
        entity = value
        if kind == "username":
            try:
                entity = await client.get_entity(value)
            except Exception:
                # Joining by username can still work when entity resolution is
                # temporarily unavailable.
                entity = value
        await client(JoinChannelRequest(entity))

    for attempt in range(2):
        try:
            await _join_once()
            return True, "joined"
        except tg_errors.UserAlreadyParticipantError:
            return True, "already joined"
        except tg_errors.InviteRequestSentError:
            return False, "join request sent; admin approval is pending"
        except (tg_errors.InviteHashExpiredError, tg_errors.InviteHashInvalidError):
            return False, "invite link is expired or invalid"
        except tg_errors.FloodWaitError as exc:
            if attempt == 0 and exc.seconds <= 15:
                await asyncio.sleep(exc.seconds)
                continue
            return False, f"Telegram flood wait ({exc.seconds}s)"
        except Exception as exc:
            message = redact_sensitive(exc, 200)
            if attempt == 0 and "already"in message.lower():
                return True, "already joined"
            return False, message
    return False, "could not join target"


async def join_accounts_to_target(target: str, progress_message=None, accounts: list = None) -> list:
    """Join every active account to *target* before reporting.

    Returns a list of dictionaries with ``phone``, ``joined`` and ``message``.
    The function never raises for an individual account; the caller can use the
    joined phone numbers as an allow-list for the report engine.
    """
    active_accounts = list(accounts if accounts is not None else db.get_active_accounts())
    results = []
    if not active_accounts:
        return results

    total = len(active_accounts)
    joined_count = 0
    failed_count = 0

    for index, account in enumerate(active_accounts, start=1):
        phone, session_file = account[1], account[2]
        proxy_type, proxy_ip, proxy_port = account[4], account[5], account[6]
        proxy_user, proxy_pass = account[7], account[8]

        if progress_message is not None:
            try:
                await progress_message.edit(
                    "**Joining accounts to target**\n\n"
                    f"Progress: {index}/{total}\n"
                    f"Current: `{mask_phone(phone)}`\n\n"
                    f"Joined: {joined_count}\n"
                    f"Failed: {failed_count}"
                )
            except Exception:
                pass

        client = None
        ok = False
        message = ""
        try:
            if not session_file or not os.path.isfile(session_file):
                message = "session file is missing"
            else:
                proxy_tuple = get_telethon_proxy(
                    proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass
                )
                client = TelegramClient(
                    session_file.replace('.session', ''), API_ID, API_HASH, proxy=proxy_tuple
                )
                await client.connect()
                if not await client.is_user_authorized():
                    message = "session is not authorized"
                    try:
                        db.update_account_status(phone, "error", False)
                    except Exception:
                        pass
                else:
                    ok, message = await ensure_account_joined(client, target)
        except Exception as exc:
            message = redact_sensitive(exc, 200)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        if ok:
            joined_count += 1
        else:
            failed_count += 1
            log_activity(f"Join failed for {mask_phone(phone)} on {target}: {message}")

        results.append({"phone": phone, "joined": ok, "message": message})
        await asyncio.sleep(0.25)

    if progress_message is not None:
        try:
            failed_names = ", ".join(
                mask_phone(item["phone"]) for item in results if not item["joined"]
            )
            text = (
                "**Join check complete**\n\n"
                f"Joined: {joined_count}/{total}\n"
                f"Failed: {failed_count}/{total}"
            )
            if failed_names:
                text += f"\n\nFailed accounts: {failed_names[:300]}"
            await progress_message.edit(text)
        except Exception:
            pass

    return results


class SessionManager:
    @staticmethod
    async def login_account(phone: str, user_id: int, progress_message, proxy_type: str = None, proxy_ip: str = None, proxy_port: int = None, proxy_user: str = None, proxy_pass: str = None) -> Optional[str]:
        """Login a Telegram account and return session file path."""
        client = None
        try:
            # Create unique session name
            session_name = f"{SESSIONS_FOLDER}/account_{phone.replace('+', '')}"
            
            # Setup proxy
            proxy_tuple = get_telethon_proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
            
            client = TelegramClient(session_name, API_ID, API_HASH, proxy=proxy_tuple)
            
            await client.connect()
            
            if not await client.is_user_authorized():
                await client.send_code_request(phone)
                await progress_message.edit(
                    f"Login for: {mask_phone(phone)}\n\n"
                    f"OTP sent successfully!\n"
                    f"Please send the verification code as a text message."
                )
                
                # Create event and wait
                event = asyncio.Event()
                active_operations[user_id] = {'event': event, 'data': {}}
                
                # Wait for message handler to set the event
                await event.wait()
                if active_operations[user_id]['data'].get('cancelled'):
                    raise ValueError("Login cancelled by user.")
                
                # Get OTP from data
                otp = (active_operations[user_id]['data'].get('otp') or '').strip()
                if not otp:
                    raise ValueError("No verification code received.")
                
                try:
                    await client.sign_in(phone, code=otp)
                except Exception as e:
                    # Check for 2FA
                    from telethon.errors import SessionPasswordNeededError
                    if isinstance(e, SessionPasswordNeededError):
                        await progress_message.edit(
                            f" 2FA is enabled for {mask_phone(phone)}\n"
                            f"Please send your 2FA password as a text message."
                        )
                        # Reset event and wait for password
                        event.clear()
                        state_data = active_operations[user_id]['data']
                        state_data['awaiting_password'] = True
                        await event.wait()
                        if state_data.get('cancelled'):
                            raise ValueError("Login cancelled by user.")
                        password = (state_data.get('password') or '').strip()
                        if not password:
                            raise ValueError("No 2FA password received.")
                        await client.sign_in(password=password)
                    else:
                        raise e
                
            # Save session
            session_file = f"{session_name}.session"
            try:
                os.chmod(session_file, 0o600)
            except OSError:
                pass
            db.add_account(phone, session_file, user_id, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
            
            await progress_message.edit(
                f"Account login successful!\n"
                f"Phone: {mask_phone(phone)}"
            )
            
            log_activity(f"Account {mask_phone(phone)} logged in by user {user_id}")
            return session_file
            
        except Exception as e:
            error = redact_sensitive(e, 300)
            await progress_message.edit(f"Login failed for {mask_phone(phone)}: {error}")
            log_activity(f"Login error for {mask_phone(phone)}: {error}")
            return None
        finally:
            active_operations.pop(user_id, None)
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

async def scrape_recent_messages(target: str, limit: int = 10, phone: str = None) -> list:
    """Connect using an active account and scrape recent messages from a target."""
    active_accounts = db.get_active_accounts()
    if not active_accounts:
        raise Exception("No active accounts available to scrape target!")
    
    # Use the requested account, or the first active account.
    if phone:
        matching = [acc for acc in active_accounts if acc[1] == phone]
        if not matching:
            raise Exception("Selected account is not active or was not found.")
        account = matching[0]
    else:
        account = active_accounts[0]
    session_file = account[2]
    proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass = account[4], account[5], account[6], account[7], account[8]
    
    proxy_tuple = get_telethon_proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
    client = TelegramClient(session_file.replace('.session', ''), API_ID, API_HASH, proxy=proxy_tuple)
    
    await client.connect()
    
    try:
        kind, value = parse_target_identifier(target)
        entity = None

        if kind == "invite":
            from telethon.tl.functions.messages import CheckChatInviteRequest
            try:
                invite_result = await client(ImportChatInviteRequest(value))
                entity = invite_result.chats[0]
            except tg_errors.UserAlreadyParticipantError:
                check = await client(CheckChatInviteRequest(value))
                entity = getattr(check, 'chat', None)
                if entity is None:
                    raise Exception("Invite is valid but the account is not a member yet.")
        else:
            joined, join_message = await ensure_account_joined(client, target)
            if not joined:
                raise Exception(join_message)
            entity = await client.get_entity(value)

        # Scrape only real channel posts. Telegram also returns service
        # messages (channel created, photo changed, pinned, joins, etc.) in the
        # same history. Those are not reportable posts and must not inflate the
        # link count. Iterate until the requested number of real posts is
        # collected instead of stopping after that many raw history entries.
        messages = []
        grouped_seen = set()
        async for msg in client.iter_messages(entity):
            if len(messages) >= limit:
                break
            # Multi-photo/video albums are one visible post but contain several
            # message IDs. Count the album once instead of inflating the post
            # count shown to the user.
            if not is_reportable_post(msg, grouped_seen):
                continue
            if getattr(entity, 'username', None):
                link = f"https://t.me/{entity.username}/{msg.id}"
            else:
                raw_id = str(entity.id)
                if raw_id.startswith("-100"):
                    raw_id = raw_id[4:]
                elif raw_id.startswith("-"):
                    raw_id = raw_id[1:]
                link = f"https://t.me/c/{raw_id}/{msg.id}"
            messages.append(link)

        return messages
    finally:
        await client.disconnect()

class ReportEngine:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.should_stop = False
    
    async def report_message(
        self, client: TelegramClient, target: str, message_link: str,
        reason: str, comment: str = "", reason_option: str = ""
    ) -> bool:
        """Report one message using Telegram's current option-based flow.

        ``reason_option`` is the real option selected from Telegram's report
        screens. When it is absent, the serialized reason type is used as the
        first option and Telegram's continuation response is followed.
        """
        try:
            try:
                chat_id, message_id = parse_message_link(message_link)
            except ValueError:
                log_activity(f"Failed to parse link: {message_link}")
                return False

            try:
                entity = await client.get_entity(chat_id)
            except Exception:
                try:
                    entity = await client.get_entity(target)
                except Exception as e:
                    log_activity(f"Failed to get entity for {chat_id}: {str(e)}")
                    return False

            reason_obj = types.InputReportReasonSpam()
            if reason == "child_abuse":
                reason_obj = types.InputReportReasonChildAbuse()
            elif reason == "violence":
                reason_obj = types.InputReportReasonViolence()
            elif reason in ("illegal_drugs", "illegal_activities"):
                reason_obj = types.InputReportReasonIllegalDrugs()
            elif reason == "pornography":
                reason_obj = types.InputReportReasonPornography()
            elif reason in ("scam", "fake"):
                reason_obj = types.InputReportReasonFake()
            elif reason == "copyright":
                reason_obj = types.InputReportReasonCopyright()
            elif reason == "personal_details":
                reason_obj = types.InputReportReasonPersonalDetails()
            elif reason == "geo_irrelevant":
                reason_obj = types.InputReportReasonGeoIrrelevant()
            elif reason == "other":
                reason_obj = types.InputReportReasonOther()

            if reason_option:
                try:
                    option = bytes.fromhex(reason_option)
                except Exception:
                    option = reason_obj._bytes()
            else:
                option = reason_obj._bytes()

            # Send the selected option and follow Telegram's real continuation
            # response (another option page or a comment request).
            from telethon.errors import FloodWaitError
            for attempt in range(2):
                try:
                    result = await client(ReportRequest(
                        peer=entity,
                        id=[message_id],
                        option=option,
                        message=comment or ''
                    ))
                    if isinstance(result, types.ReportResultReported):
                        return True
                    if isinstance(result, types.ReportResultAddComment):
                        follow_up = await client(ReportRequest(
                            peer=entity,
                            id=[message_id],
                            option=result.option,
                            message=comment or 'Terms of service violation'
                        ))
                        return True
                    if isinstance(result, types.ReportResultChooseOption):
                        # Telegram requires a sub-option. Choose matching or first available real option!
                        chosen_opt = None
                        opts = getattr(result, "options", None) or []
                        if opts:
                            for opt in opts:
                                opt_text = (getattr(opt, "text", "") or "").lower()
                                if reason.lower() in opt_text:
                                    chosen_opt = opt.option
                                    break
                            if not chosen_opt:
                                chosen_opt = opts[0].option
                        if chosen_opt:
                            sub_res = await client(ReportRequest(
                                peer=entity,
                                id=[message_id],
                                option=chosen_opt,
                                message=comment or ''
                            ))
                            if isinstance(sub_res, types.ReportResultAddComment):
                                await client(ReportRequest(
                                    peer=entity,
                                    id=[message_id],
                                    option=sub_res.option,
                                    message=comment or 'Terms of service violation'
                                ))
                            return True
                        return True
                    return True
                except FloodWaitError as fe:
                    log_activity(f"FloodWait on message report: waiting {fe.seconds}s")
                    if fe.seconds > 10:
                        return False
                    await asyncio.sleep(fe.seconds)
                except Exception as e:
                    # Fallback to Channel/Message ReportSpamRequest
                    try:
                        from telethon.tl.functions.channels import ReportSpamRequest as ChanReportSpam
                        await client(ChanReportSpam(channel=entity, participant=entity, id=[message_id]))
                        return True
                    except Exception:
                        pass
                    try:
                        from telethon.tl.functions.messages import ReportSpamRequest as MsgReportSpam
                        await client(MsgReportSpam(peer=entity))
                        return True
                    except Exception:
                        pass
                    log_activity(f"Report execution error: {str(e)}")
                    return False
            return False

        except Exception as e:
            log_activity(f"Report error for {message_link}: {str(e)}")
            return False
            
    async def report_peer(
        self, client: TelegramClient, peer_id: str, reason: str, comment: str = ""
    ) -> bool:
        """Report a user or bot using the client"""
        try:
            try:
                entity = await client.get_entity(peer_id)
            except Exception as e:
                log_activity(f"Failed to get entity for peer {peer_id}: {str(e)}")
                return False
            
            reason_obj = types.InputReportReasonSpam()
            
            if reason == "child_abuse":
                reason_obj = types.InputReportReasonChildAbuse()
            elif reason == "violence":
                reason_obj = types.InputReportReasonViolence()
            elif reason in ("illegal_drugs", "illegal_activities"):
                reason_obj = types.InputReportReasonIllegalDrugs()
            elif reason == "pornography":
                reason_obj = types.InputReportReasonPornography()
            elif reason == "fake"or reason == "scam":
                reason_obj = types.InputReportReasonFake()
            elif reason == "personal_details":
                reason_obj = types.InputReportReasonPersonalDetails()
            elif reason == "copyright":
                reason_obj = types.InputReportReasonCopyright()
            elif reason == "geo_irrelevant":
                reason_obj = types.InputReportReasonGeoIrrelevant()
            elif reason == "other":
                reason_obj = types.InputReportReasonOther()
            
            from telethon.errors import FloodWaitError
            for attempt in range(2):
                try:
                    await client(ReportPeerRequest(
                        peer=entity,
                        reason=reason_obj,
                        message=comment or ''
                    ))
                    return True
                except FloodWaitError as fe:
                    log_activity(f"FloodWait on peer report: waiting {fe.seconds}s")
                    if fe.seconds > 10:
                        return False
                    await asyncio.sleep(fe.seconds)
                except Exception as e:
                    log_activity(f"Peer report execution error: {str(e)}")
                    return False
            return False
            
        except Exception as e:
            log_activity(f"Report peer error for {peer_id}: {str(e)}")
            return False
    
    async def run_report_session(
        self, target: str, message_links: list, reports_per_account: int,
        delay: int, reason: str, comment: str, progress_message,
        allowed_phones=None, reason_option: str = "", reason_choices: list = None,
        dist_mode: str = "all", account_rules: dict = None
    ):
        """Run complete report session sequentially."""
        active_accounts = db.get_active_accounts()
        if allowed_phones is not None:
            allowed_phones = set(allowed_phones)
            active_accounts = [acc for acc in active_accounts if acc[1] in allowed_phones]
        if not active_accounts:
            await progress_message.edit("No joined/active accounts available.")
            return {"success": 0, "failed": 0, "completed": 0, "total": 0}
        
        total_accounts = len(active_accounts)
        items_list = list(message_links) if message_links else [None]
        total_assigned = 0
        for a_idx, a_acc in enumerate(active_accounts):
            a_phone = a_acc[1]
            a_rule = (account_rules or {}).get(a_phone) or {}
            if a_rule.get("messages") is not None and a_rule.get("messages") != "all" and message_links:
                r_indices = a_rule.get("messages")
                if isinstance(r_indices, list):
                    a_count = len([i for i in r_indices if 0 <= i < len(message_links)])
                else:
                    a_count = len(items_list)
                total_assigned += max(1, a_count)
            elif dist_mode == "round_robin" and message_links:
                num_msgs = len(message_links)
                if total_accounts <= num_msgs:
                    a_count = len([m for i, m in enumerate(message_links) if i % total_accounts == a_idx])
                else:
                    a_count = 1
                total_assigned += a_count
            else:
                total_assigned += len(items_list)
        total_reports = total_assigned * reports_per_account
        completed_reports = 0
        success_count = 0
        failed_count = 0
        
        start_time = datetime.now()
        
        for acc_idx, account in enumerate(active_accounts):
            if self.should_stop:
                await progress_message.edit("Report session stopped by user!")
                break

            account_id, phone, session_file, status, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass = account
            client = None

            try:
                proxy_tuple = get_telethon_proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
                client = TelegramClient(session_file.replace('.session', ''), API_ID, API_HASH, proxy=proxy_tuple)
                await client.connect()
                # Set account status to ONLINE
                await set_account_online(client, offline=False)

                joined, join_message = await ensure_account_joined(client, target)
                if not joined:
                    log_activity(f"Account {phone} not joined to {target}; skipping: {join_message}")
                    continue
                log_activity(f"Account {phone} joined {target} ({join_message})")

                a_rule = (account_rules or {}).get(phone) or {}
                if a_rule.get("messages") is not None and a_rule.get("messages") != "all" and message_links:
                    r_indices = a_rule.get("messages")
                    if isinstance(r_indices, list):
                        acc_items = [message_links[i] for i in r_indices if 0 <= i < len(message_links)]
                    else:
                        acc_items = items_list
                    if not acc_items:
                        acc_items = items_list
                elif dist_mode == "round_robin" and message_links:
                    num_msgs = len(message_links)
                    if total_accounts <= num_msgs:
                        acc_items = [m for i, m in enumerate(message_links) if i % total_accounts == acc_idx]
                    else:
                        acc_items = [message_links[acc_idx % num_msgs]]
                else:
                    acc_items = items_list

                for msg_idx, message_link in enumerate(acc_items):
                    if self.should_stop:
                        break

                    for report_num in range(reports_per_account):
                        if self.should_stop:
                            break

                        await set_account_online(client, offline=False)

                        if a_rule.get("reasons"):
                            choices = a_rule.get("reasons")
                        else:
                            choices = reason_choices or []

                        if len(choices) >= 2:
                            choice = choices[report_num % len(choices)]
                        elif len(choices) == 1:
                            if report_num % 2 == 0:
                                choice = choices[0]
                            else:
                                primary_r = (choices[0].get("reason") or "").lower()
                                sec_r = "fake" if "spam" in primary_r else ("violence" if "fake" in primary_r else "spam")
                                choice = {"reason": sec_r, "detail": "Fake / Scam" if sec_r == "fake" else sec_r.capitalize(), "option": ""}
                        else:
                            types_cycle = [
                                {"reason": reason or "spam", "detail": (reason or "spam").capitalize(), "option": reason_option or ""},
                                {"reason": "fake" if reason != "fake" else "violence", "detail": "Fake / Scam", "option": ""}
                            ]
                            choice = types_cycle[report_num % 2]
                        report_reason = choice.get("reason") or reason or "spam"
                        report_option = choice.get("option") or reason_option or ""
                        report_label = choice.get("detail") or report_reason

                        choice_comment = (choice.get("comment") or "").strip()
                        if choice_comment:
                            report_comment = choice_comment
                        else:
                            active_comment = a_rule.get("comment") if (a_rule.get("comment") is not None and a_rule.get("comment") != "") else comment
                            if active_comment:
                                c_lines = [l.strip() for l in active_comment.split("\n") if l.strip()]
                                if len(c_lines) > 1:
                                    report_comment = c_lines[report_num % len(c_lines)]
                                else:
                                    report_comment = active_comment
                            else:
                                report_comment = ""

                        if message_link:
                            success = await self.report_message(
                                client, target, message_link, report_reason, report_comment,
                                report_option
                            )
                        else:
                            success = await self.report_peer(
                                client, target, report_reason, report_comment
                            )

                        log_item = message_link or target
                        if success:
                            success_count += 1
                            db.add_report_log(phone, target, log_item, "success", report_label)
                        else:
                            failed_count += 1
                            db.add_report_log(phone, target, log_item, "failed", report_label)

                        completed_reports += 1

                        percentage = (completed_reports / total_reports) * 100 if total_reports > 0 else 100
                        progress_bar = create_progress_bar(percentage)

                        elapsed = datetime.now() - start_time
                        eta = (elapsed / completed_reports) * (total_reports - completed_reports) if completed_reports > 0 else timedelta()

                        status_text = (
                            f"**Reporting Progress (Sequential)**\n\n"
                            f"Account: {acc_idx + 1}/{total_accounts}\n"
                            f"Phone: {mask_phone(phone)}\n"
                            f"Message: {msg_idx + 1}/{len(message_links)}\n"
                            f"Reports: {report_num + 1}/{reports_per_account}\n\n"
                            f"{progress_bar}\n\n"
                            f"Success: {success_count}\n"
                            f"Failed: {failed_count}\n"
                            f"Total: {completed_reports}/{total_reports}\n\n"
                            f"Elapsed: {str(elapsed).split('.')[0]}\n"
                            f"ETA: {str(eta).split('.')[0]}"
                        )

                        try:
                            await progress_message.edit(status_text)
                        except:
                            pass

                        if not self.should_stop and (completed_reports < total_reports):
                            await asyncio.sleep(delay)

                db.update_account_status(phone, "active")

            except Exception as e:
                log_activity(f"Account {phone} error: {str(e)}")
                db.update_account_status(phone, "error", True)
            finally:
                if client is not None:
                    try:
                        await set_account_online(client, offline=True)
                        await client.disconnect()
                    except Exception:
                        pass
        
        db.add_report_stats(total_reports, success_count, failed_count)
        
        final_text = (
            f"**Report Session Complete**\n\n"
            f"Total Reports: {completed_reports}/{total_reports}\n"
            f"Successful: {success_count}\n"
            f"Failed: {failed_count}\n"
            f"Duration: {str(datetime.now() - start_time).split('.')[0]}\n"
            f"Accounts Used: {total_accounts}\n\n"
            f"Target: {target}\n"
        )
        
        try:
            await progress_message.edit(final_text)
        except:
            pass
        return {
            "success": success_count,
            "failed": failed_count,
            "completed": completed_reports,
            "total": total_reports,
        }

    async def run_parallel_report_session(
        self, target: str, message_links: list, reports_per_account: int,
        reason: str, comment: str, progress_message,
        allowed_phones=None, reason_option: str = "", reason_choices: list = None,
        dist_mode: str = "all", account_rules: dict = None
    ):
        """Run complete report session concurrently (Turbo Mode)."""
        active_accounts = db.get_active_accounts()
        if allowed_phones is not None:
            allowed_phones = set(allowed_phones)
            active_accounts = [acc for acc in active_accounts if acc[1] in allowed_phones]
        if not active_accounts:
            await progress_message.edit("No joined/active accounts available.")
            return {"success": 0, "failed": 0, "completed": 0, "total": 0}
        
        total_accounts = len(active_accounts)
        items_list = list(message_links) if message_links else [None]
        total_assigned = 0
        for a_idx, a_acc in enumerate(active_accounts):
            a_phone = a_acc[1]
            a_rule = (account_rules or {}).get(a_phone) or {}
            if a_rule.get("messages") is not None and a_rule.get("messages") != "all" and message_links:
                r_indices = a_rule.get("messages")
                if isinstance(r_indices, list):
                    a_count = len([i for i in r_indices if 0 <= i < len(message_links)])
                else:
                    a_count = len(items_list)
                total_assigned += max(1, a_count)
            elif dist_mode == "round_robin" and message_links:
                num_msgs = len(message_links)
                if total_accounts <= num_msgs:
                    a_count = len([m for i, m in enumerate(message_links) if i % total_accounts == a_idx])
                else:
                    a_count = 1
                total_assigned += a_count
            else:
                total_assigned += len(items_list)
        total_reports = total_assigned * reports_per_account
        completed_reports = 0
        success_count = 0
        failed_count = 0
        
        start_time = datetime.now()
        progress_lock = asyncio.Lock()
        
        async def update_progress_ui(phone: str, acc_num: int):
            async with progress_lock:
                percentage = (completed_reports / total_reports) * 100 if total_reports > 0 else 100
                progress_bar = create_progress_bar(percentage)
                
                elapsed = datetime.now() - start_time
                eta = (elapsed / completed_reports) * (total_reports - completed_reports) if completed_reports > 0 else timedelta()
                
                status_text = (
                    f"**Turbo Mode Active (Parallel Processing)**\n\n"
                    f"Running {total_accounts} accounts in parallel...\n"
                    f"Active Worker: {mask_phone(phone)} (#{acc_num})\n"
                    f"Target: {target}\n"
                    f"Reason: {reason}\n\n"
                    f"{progress_bar}\n\n"
                    f"Success: {success_count}\n"
                    f"Failed: {failed_count}\n"
                    f"Total: {completed_reports}/{total_reports}\n\n"
                    f"Elapsed: {str(elapsed).split('.')[0]}\n"
                    f"ETA: {str(eta).split('.')[0]}"
                )
                try:
                    await progress_message.edit(status_text)
                except:
                    pass

        async def account_worker(account_idx: int, account_data: tuple):
            nonlocal completed_reports, success_count, failed_count
            _, phone, session_file, _, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass = account_data

            if self.should_stop:
                return

            client = None
            try:
                proxy_tuple = get_telethon_proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
                client = TelegramClient(session_file.replace('.session', ''), API_ID, API_HASH, proxy=proxy_tuple)
                await client.connect()
                # Set account status to ONLINE
                await set_account_online(client, offline=False)

                joined, join_message = await ensure_account_joined(client, target)
                if not joined:
                    log_activity(f"[Worker {phone}] Not joined to {target}; skipping: {join_message}")
                    return

                a_rule = (account_rules or {}).get(phone) or {}
                if a_rule.get("messages") is not None and a_rule.get("messages") != "all" and message_links:
                    r_indices = a_rule.get("messages")
                    if isinstance(r_indices, list):
                        acc_items = [message_links[i] for i in r_indices if 0 <= i < len(message_links)]
                    else:
                        acc_items = items_list
                    if not acc_items:
                        acc_items = items_list
                elif dist_mode == "round_robin" and message_links:
                    num_msgs = len(message_links)
                    if total_accounts <= num_msgs:
                        acc_items = [m for i, m in enumerate(message_links) if i % total_accounts == account_idx]
                    else:
                        acc_items = [message_links[account_idx % num_msgs]]
                else:
                    acc_items = items_list

                for msg_idx, message_link in enumerate(acc_items):
                    if self.should_stop:
                        break

                    for report_num in range(reports_per_account):
                        if self.should_stop:
                            break

                        await set_account_online(client, offline=False)

                        if a_rule.get("reasons"):
                            choices = a_rule.get("reasons")
                        else:
                            choices = reason_choices or []

                        if len(choices) >= 2:
                            choice = choices[report_num % len(choices)]
                        elif len(choices) == 1:
                            if report_num % 2 == 0:
                                choice = choices[0]
                            else:
                                primary_r = (choices[0].get("reason") or "").lower()
                                sec_r = "fake" if "spam" in primary_r else ("violence" if "fake" in primary_r else "spam")
                                choice = {"reason": sec_r, "detail": "Fake / Scam" if sec_r == "fake" else sec_r.capitalize(), "option": ""}
                        else:
                            types_cycle = [
                                {"reason": reason or "spam", "detail": (reason or "spam").capitalize(), "option": reason_option or ""},
                                {"reason": "fake" if reason != "fake" else "violence", "detail": "Fake / Scam", "option": ""}
                            ]
                            choice = types_cycle[report_num % 2]
                        report_reason = choice.get("reason") or reason or "spam"
                        report_option = choice.get("option") or reason_option or ""
                        report_label = choice.get("detail") or report_reason

                        choice_comment = (choice.get("comment") or "").strip()
                        if choice_comment:
                            report_comment = choice_comment
                        else:
                            active_comment = a_rule.get("comment") if (a_rule.get("comment") is not None and a_rule.get("comment") != "") else comment
                            if active_comment:
                                c_lines = [l.strip() for l in active_comment.split("\n") if l.strip()]
                                if len(c_lines) > 1:
                                    report_comment = c_lines[report_num % len(c_lines)]
                                else:
                                    report_comment = active_comment
                            else:
                                report_comment = ""

                        if message_link:
                            success = await self.report_message(
                                client, target, message_link, report_reason, report_comment,
                                report_option
                            )
                        else:
                            success = await self.report_peer(
                                client, target, report_reason, report_comment
                            )

                        log_item = message_link or target
                        async with progress_lock:
                            if success:
                                success_count += 1
                                db.add_report_log(phone, target, log_item, "success", report_label)
                            else:
                                failed_count += 1
                                db.add_report_log(phone, target, log_item, "failed", report_label)

                            completed_reports += 1

                        await update_progress_ui(phone, account_idx + 1)
                        await asyncio.sleep(0.5)

                db.update_account_status(phone, "active")

            except Exception as e:
                log_activity(f"[Worker {phone}] Fatal error: {str(e)}")
                db.update_account_status(phone, "error", True)
            finally:
                if client is not None:
                    try:
                        await set_account_online(client, offline=True)
                        await client.disconnect()
                    except Exception:
                        pass
        
        tasks = []
        sem = asyncio.Semaphore(10)
        
        async def sem_worker(idx, account):
            async with sem:
                await account_worker(idx, account)
                
        for idx, account in enumerate(active_accounts):
            tasks.append(sem_worker(idx, account))
            
        await asyncio.gather(*tasks)
        
        db.add_report_stats(total_reports, success_count, failed_count)
        
        final_text = (
            f"**Turbo Report Session Complete**\n\n"
            f"Mode: Turbo (Parallel)\n"
            f"Total Reports: {completed_reports}/{total_reports}\n"
            f"Successful: {success_count}\n"
            f"Failed: {failed_count}\n"
            f"Duration: {str(datetime.now() - start_time).split('.')[0]}\n"
            f"Accounts Used: {total_accounts}\n\n"
            f"Target: {target}\n"
        )
        try:
            await progress_message.edit(final_text)
        except:
            pass
        return {
            "success": success_count,
            "failed": failed_count,
            "completed": completed_reports,
            "total": total_reports,
        }

    async def run_peer_report_session(
        self, peer_id: str, reason: str, reports_per_account: int, comment: str, progress_message,
        allowed_phones=None
    ):
        """Run complete report session for a user/bot sequentially"""
        active_accounts = db.get_active_accounts()
        if allowed_phones is not None:
            allowed_phones = set(allowed_phones)
            active_accounts = [acc for acc in active_accounts if acc[1] in allowed_phones]
        if not active_accounts:
            await progress_message.edit("No joined/active accounts available.")
            return {"success": 0, "failed": 0, "completed": 0, "total": 0}
        
        total_accounts = len(active_accounts)
        total_reports = total_accounts * reports_per_account
        completed_reports = 0
        success_count = 0
        failed_count = 0
        
        start_time = datetime.now()
        
        for acc_idx, account in enumerate(active_accounts):
            if self.should_stop:
                await progress_message.edit("Report session stopped by user!")
                break
            
            account_id, phone, session_file, status, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass = account
            
            try:
                proxy_tuple = get_telethon_proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
                client = TelegramClient(session_file.replace('.session', ''), API_ID, API_HASH, proxy=proxy_tuple)
                await client.connect()
                await set_account_online(client, offline=False)
                
                for report_num in range(reports_per_account):
                    if self.should_stop:
                        break
                        
                    success = await self.report_peer(client, peer_id, reason, comment)
                    
                    if success:
                        success_count += 1
                    else:
                        failed_count += 1
                    
                    completed_reports += 1
                    
                    percentage = (completed_reports / total_reports) * 100
                    progress_bar = create_progress_bar(percentage)
                    
                    status_text = (
                        f"**User/Bot Reporting Progress (Sequential)**\n\n"
                        f"Account: {acc_idx + 1}/{total_accounts}\n"
                        f"Phone: {mask_phone(phone)}\n"
                        f"Reports: {report_num + 1}/{reports_per_account}\n\n"
                        f"{progress_bar}\n\n"
                        f"Success: {success_count}\n"
                        f"Failed: {failed_count}\n"
                        f"Total: {completed_reports}/{total_reports}"
                    )
                    
                    try:
                        await progress_message.edit(status_text)
                    except:
                        pass
                    
                    await asyncio.sleep(1)
                
                await set_account_online(client, offline=True)
                await client.disconnect()
                await asyncio.sleep(1)
                
            except Exception as e:
                log_activity(f"Account {phone} error: {str(e)}")
                continue
        
        final_text = (
            f"**User/Bot Report Complete**\n\n"
            f"Total Reports: {completed_reports}/{total_reports}\n"
            f"Successful: {success_count}\n"
            f"Failed: {failed_count}\n"
            f"Duration: {str(datetime.now() - start_time).split('.')[0]}\n"
            f"Accounts Used: {total_accounts}\n\n"
            f"Target: {peer_id}\n"
        )
        
        try:
            await progress_message.edit(final_text)
        except:
            pass

    async def run_parallel_peer_report_session(
        self, peer_id: str, reason: str, reports_per_account: int, comment: str, progress_message,
        allowed_phones=None
    ):
        """Run complete parallel peer report session (Turbo Mode)"""
        active_accounts = db.get_active_accounts()
        if allowed_phones is not None:
            allowed_phones = set(allowed_phones)
            active_accounts = [acc for acc in active_accounts if acc[1] in allowed_phones]
        if not active_accounts:
            await progress_message.edit("No joined/active accounts available.")
            return {"success": 0, "failed": 0, "completed": 0, "total": 0}
        
        total_accounts = len(active_accounts)
        total_reports = total_accounts * reports_per_account
        completed_reports = 0
        success_count = 0
        failed_count = 0
        start_time = datetime.now()
        
        progress_lock = asyncio.Lock()
        
        async def update_progress_ui(phone: str, acc_num: int):
            async with progress_lock:
                percentage = (completed_reports / total_reports) * 100
                progress_bar = create_progress_bar(percentage)
                
                status_text = (
                    f"**Turbo Peer Report Active (Parallel)**\n\n"
                    f"Running {total_accounts} accounts in parallel...\n"
                    f"Active Worker: {mask_phone(phone)} (# {acc_num})\n"
                    f"Target: {peer_id}\n"
                    f"Reason: {reason}\n\n"
                    f"{progress_bar}\n\n"
                    f"Success: {success_count}\n"
                    f"Failed: {failed_count}\n"
                    f"Total: {completed_reports}/{total_reports}"
                )
                try:
                    await progress_message.edit(status_text)
                except:
                    pass

        async def peer_worker(account_idx: int, account_data: tuple):
            nonlocal completed_reports, success_count, failed_count
            _, phone, session_file, _, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass = account_data
            
            if self.should_stop:
                return
                
            try:
                proxy_tuple = get_telethon_proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass)
                client = TelegramClient(session_file.replace('.session', ''), API_ID, API_HASH, proxy=proxy_tuple)
                await client.connect()
                await set_account_online(client, offline=False)
                
                for report_num in range(reports_per_account):
                    if self.should_stop:
                        break
                        
                    success = await self.report_peer(client, peer_id, reason, comment)
                    
                    async with progress_lock:
                        if success:
                            success_count += 1
                        else:
                            failed_count += 1
                        completed_reports += 1
                        
                    await update_progress_ui(phone, account_idx + 1)
                    await asyncio.sleep(0.5)
                    
                await set_account_online(client, offline=True)
                await client.disconnect()
            except Exception as e:
                log_activity(f"[Peer Worker {phone}] Error: {str(e)}")
                
        tasks = []
        sem = asyncio.Semaphore(10)
        async def sem_worker(idx, account):
            async with sem:
                await peer_worker(idx, account)
                
        for idx, account in enumerate(active_accounts):
            tasks.append(sem_worker(idx, account))
            
        await asyncio.gather(*tasks)
        
        final_text = (
            f"**Turbo Peer Report Complete**\n\n"
            f"Mode: Turbo (Parallel)\n"
            f"Total Reports: {completed_reports}/{total_reports}\n"
            f"Successful: {success_count}\n"
            f"Failed: {failed_count}\n"
            f"Duration: {str(datetime.now() - start_time).split('.')[0]}\n"
            f"Accounts Used: {total_accounts}\n\n"
            f"Target: {peer_id}\n"
        )
        try:
            await progress_message.edit(final_text)
        except:
            pass
