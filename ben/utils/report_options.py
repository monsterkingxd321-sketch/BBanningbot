"""Read Telegram's real report reason options.

Telegram's ``messages.report`` API returns an option page, a comment request or
a completed report. The bot uses this module to show the exact options Telegram
returns instead of inventing local sub-options.
"""

from telethon import TelegramClient, types
from telethon.tl.functions.messages import ReportRequest

from config import API_ID, API_HASH
from utils.helpers import get_telethon_proxy, parse_message_link
from utils.review_utils import redact_sensitive


def _option_hex(value) -> str:
    return bytes(value or b"").hex()


def _option_rows(result) -> list:
    rows = []
    for option in getattr(result, "options", []) or []:
        rows.append({
            "text": str(getattr(option, "text", "") or "Option"),
            "option": _option_hex(getattr(option, "option", b"")),
        })
    return rows


async def load_report_choices(phone: str, target: str, message_link: str,
                              option_bytes: bytes = b"", message: str = "") -> dict:
    """Ask Telegram for the report options after a reason is selected.

    An empty ``option_bytes`` requests Telegram's first page. Passing a real
    option requests the next page (for example the sub-options of Fake Account).
    """
    from bot import db

    accounts = db.get_active_accounts()
    if not accounts:
        return {"kind": "error", "error": "No active account is available."}

    candidates = []
    if phone:
        chosen = next((row for row in accounts if row[1] == phone), None)
        if chosen:
            candidates.append(chosen)
    candidates.extend([row for row in accounts if row not in candidates])

    last_error = "No active account is available."
    for account in candidates:
        _, account_phone, session_file, _, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass = account
        client = None
        try:
            proxy = get_telethon_proxy(
                proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass
            )
            client = TelegramClient(
                session_file.replace(".session", ""), API_ID, API_HASH, proxy=proxy
            )
            await client.connect()
            if not await client.is_user_authorized():
                last_error = f"Account {account_phone} is not authorized."
                continue

            chat_ref, message_id = parse_message_link(message_link)
            try:
                entity = await client.get_entity(chat_ref)
            except Exception:
                try:
                    entity = await client.get_entity(target)
                except Exception as ent_err:
                    last_error = redact_sensitive(ent_err, 300)
                    continue

            result = await client(ReportRequest(
                peer=entity,
                id=[message_id],
                option=option_bytes or b"",
                message=message or "",
            ))

            if isinstance(result, types.ReportResultChooseOption):
                return {
                    "kind": "choose",
                    "title": str(getattr(result, "title", "") or ""),
                    "options": _option_rows(result),
                    "phone": account_phone,
                }
            if isinstance(result, types.ReportResultAddComment):
                return {
                    "kind": "comment",
                    "optional": bool(getattr(result, "optional", False)),
                    "option": _option_hex(getattr(result, "option", b"")),
                    "phone": account_phone,
                }
            if isinstance(result, types.ReportResultReported):
                return {"kind": "reported", "phone": account_phone}
            return {
                "kind": "error",
                "error": "Telegram returned an unsupported report response.",
            }
        except Exception as exc:
            last_error = redact_sensitive(exc, 300)
            continue
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    return {"kind": "error", "error": last_error}
