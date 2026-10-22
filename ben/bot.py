from telethon import TelegramClient
from config import API_ID, API_HASH, BOT_SESSION_FILE
from database import Database

# Initialize database
db = Database()

# Initialize bot client (Telethon appends .session itself)
_bot_session = BOT_SESSION_FILE[:-8] if BOT_SESSION_FILE.lower().endswith(".session") else BOT_SESSION_FILE
bot = TelegramClient(_bot_session, API_ID, API_HASH)

# Global state
active_reports = {}
active_operations = {}  # For waiting on events like OTP
active_campaigns = {}  # Campaign ID -> ReportEngine or None while starting
