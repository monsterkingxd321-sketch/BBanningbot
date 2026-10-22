import asyncio
import os
import sys

# Ensure ben directory is on sys.path so it runs seamlessly from root or inside ben
current_dir = os.path.abspath(os.path.dirname(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Fix Windows console encoding so non-ASCII output does not crash.
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from bot import bot, db
from config import (
    SESSIONS_FOLDER,
    LOGS_FOLDER,
    EXPORTS_FOLDER,
    DATABASE_FILE,
    BOT_TOKEN,
    REVIEW_DEFAULT_INTERVAL_HOURS,
    REVIEW_DEFAULT_MAX_CYCLES,
    CAMPAIGN_DEFAULT_INTERVAL_HOURS,
    CAMPAIGN_DEFAULT_DURATION_HOURS,
    CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE,
)
from utils.helpers import log_activity

# Import handlers to register them.
from handlers import (
    start as start_handlers,
    account as account_handlers,
    report as report_handlers,
    admin as admin_handlers,
    callback as callback_handlers,
    message as message_handlers,
    review as review_handlers,
    campaign as campaign_handlers,
)

_HANDLER_MODULES = (
    start_handlers,
    account_handlers,
    report_handlers,
    admin_handlers,
    callback_handlers,
    message_handlers,
    review_handlers,
    campaign_handlers,
)

from utils.review_scheduler import ReviewScheduler
from utils.campaign_scheduler import CampaignScheduler

async def _start_health_server():
    """Start lightweight HTTP server when deployed on cloud services expecting a web port (e.g. Render Web Service)."""
    port_str = os.environ.get("PORT")
    if not port_str:
        return None
    try:
        port = int(port_str)
    except ValueError:
        return None

    async def handle_client(reader, writer):
        try:
            await reader.readline()
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 18\r\n"
                b"Connection: close\r\n\r\n"
                b"ReportingBot Live!"
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handle_client, "0.0.0.0", port)
        print(f"Health check web server running on port {port}")
        return server
    except Exception as e:
        print(f"Health server note: {e}")
        return None


async def main():
    """Main function to run the bot"""
    print("Bot starting...")
    print(f"Sessions folder: {SESSIONS_FOLDER}")
    print(f"Logs folder: {LOGS_FOLDER}")
    print(f"Exports folder: {EXPORTS_FOLDER}")
    print(f"Database: {DATABASE_FILE}")
    
    # Ensure folders exist
    os.makedirs(SESSIONS_FOLDER, exist_ok=True)
    os.makedirs(LOGS_FOLDER, exist_ok=True)
    os.makedirs(EXPORTS_FOLDER, exist_ok=True)
    
    # Start the bot
    await bot.start(bot_token=BOT_TOKEN)
    
    me = await bot.get_me()
    print(f"Bot started as @{me.username}")
    
    # Print admin IDs
    admins = db.get_all_admins()
    print(f"Admins: {[admin[0] for admin in admins]}")
    print(
        f"Review defaults: every {REVIEW_DEFAULT_INTERVAL_HOURS:g}h, "
        f"max {REVIEW_DEFAULT_MAX_CYCLES} cycles (reminders only)"
    )
    print(
        f"Campaign defaults: {CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE} reports per link, "
        f"every {CAMPAIGN_DEFAULT_INTERVAL_HOURS:g}h, "
        f"{CAMPAIGN_DEFAULT_DURATION_HOURS:g}h total duration"
    )
    
    log_activity("Bot started")

    health_server = await _start_health_server()
    scheduler = ReviewScheduler(bot, db)
    scheduler.start()
    db.reset_interrupted_campaigns()
    campaign_scheduler = CampaignScheduler(bot, db)
    campaign_scheduler.start()
    try:
        # Keep the bot running
        await bot.run_until_disconnected()
    finally:
        if health_server is not None:
            try:
                health_server.close()
                await health_server.wait_closed()
            except Exception:
                pass
        await campaign_scheduler.stop()
        await scheduler.stop()

if __name__ == "__main__":
    print("="* 50)
    print("MASS REPORT BOT (Telethon)")
    print("="* 50)
    print("\nIMPORTANT: This bot is for reporting illegal content only.")
    print("Review plans send reminders only; they never auto-submit reports.")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n Bot stopped by user")
    except Exception as e:
        print(f"\n Error: {e}")
