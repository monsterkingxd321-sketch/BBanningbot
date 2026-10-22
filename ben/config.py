"""Runtime configuration.

Credentials are read from environment variables or a local ``.env`` file.
Never commit the ``.env`` file, session files, tokens, or exported credential
archives to source control.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Load a minimal KEY=VALUE .env file without adding a dependency."""
    if not path.is_file():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "="not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        # A missing/unreadable .env is handled by the validation below.
        pass


_load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# API Credentials (environment or ben/.env only)
API_ID = _env_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError(
        "Telegram credentials are missing. Create ben/.env (or set environment "
        "variables) with API_ID, API_HASH and BOT_TOKEN. See .env.example."
    )

# Default Admin User IDs (comma separated in DEFAULT_ADMINS)
_default_admins = os.getenv("DEFAULT_ADMINS", "").strip()
DEFAULT_ADMINS = [
    int(part.strip())
    for part in _default_admins.split(",")
    if part.strip().lstrip("-").isdigit()
]

def _env_path(name: str, default: Path) -> str:
    value = (os.getenv(name) or "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path)


# Database and folders (absolute so startup location does not matter)
DATABASE_FILE = _env_path("DATABASE_FILE", BASE_DIR / "database.db")
MONGO_URI = (os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "").strip()
MONGO_DB_NAME = (os.getenv("MONGO_DB_NAME") or "banning_bot").strip()
SESSIONS_FOLDER = _env_path("SESSIONS_FOLDER", BASE_DIR / "sessions")
LOGS_FOLDER = _env_path("LOGS_FOLDER", BASE_DIR / "logs")
EXPORTS_FOLDER = _env_path("EXPORTS_FOLDER", BASE_DIR / "exports")
BOT_SESSION_FILE = _env_path("BOT_SESSION_FILE", BASE_DIR / "mass_report_bot")

# Existing reporting feature limits
MAX_REPORTS_PER_ACCOUNT = _env_int("MAX_REPORTS_PER_ACCOUNT", 50)
MIN_DELAY = _env_int("MIN_DELAY", 1)
MAX_DELAY = _env_int("MAX_DELAY", 60)
MAX_ACCOUNTS_PER_USER = _env_int("MAX_ACCOUNTS_PER_USER", 20)

# Review manager settings
REVIEW_DEFAULT_INTERVAL_HOURS = _env_float("REVIEW_DEFAULT_INTERVAL_HOURS", 2.0)
REVIEW_INTERVAL_OPTIONS = [1, 2, 6, 12, 24]
REVIEW_DEFAULT_MAX_CYCLES = _env_int("REVIEW_DEFAULT_MAX_CYCLES", 5)
REVIEW_CYCLE_OPTIONS = [1, 3, 5, 10]
REVIEW_PAGE_SIZE = max(1, _env_int("REVIEW_PAGE_SIZE", 5))
REVIEW_MAX_POSTS = max(1, _env_int("REVIEW_MAX_POSTS", 50))
REVIEW_MAX_ZIP_MB = max(1, _env_int("REVIEW_MAX_ZIP_MB", 50))
REVIEW_MAX_IMPORT_SESSIONS = max(1, _env_int("REVIEW_MAX_IMPORT_SESSIONS", 30))
REVIEW_SESSION_TIMEOUT = max(5, _env_int("REVIEW_SESSION_TIMEOUT", 20))
REVIEW_SCHEDULER_TICK_SECONDS = max(10, _env_int("REVIEW_SCHEDULER_TICK_SECONDS", 30))
REVIEW_EXPORT_MAX_ROWS = max(100, _env_int("REVIEW_EXPORT_MAX_ROWS", 5000))
ADMIN_ONLY_IMPORTS = _env_bool("ADMIN_ONLY_IMPORTS", True)

# Reporting campaign settings
CAMPAIGN_MAX_POSTS = max(1, _env_int("CAMPAIGN_MAX_POSTS", 500))
CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE = max(1, _env_int("CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE", 2))
CAMPAIGN_DEFAULT_INTERVAL_HOURS = max(0.0, _env_float("CAMPAIGN_DEFAULT_INTERVAL_HOURS", 2.0))
CAMPAIGN_DEFAULT_DURATION_HOURS = max(0.0, _env_float("CAMPAIGN_DEFAULT_DURATION_HOURS", 24.0))
CAMPAIGN_TICK_SECONDS = max(15, _env_int("CAMPAIGN_TICK_SECONDS", 30))
