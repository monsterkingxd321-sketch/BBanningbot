import os
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from config import DATABASE_FILE, DEFAULT_ADMINS

def db_write_lock(func):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return func(self, *args, **kwargs)
    return wrapper


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso_after(hours: float) -> str:
    return (datetime.now() + timedelta(hours=float(hours))).isoformat(timespec="seconds")


class Database:
    def __init__(self, db_file: str = DATABASE_FILE):
        self.db_file = db_file
        self._lock = threading.Lock()
        self.mongo_client = None
        self.mongo_db = None
        self._init_mongo()
        self.init_db()
        self._sync_with_mongo()

    def _init_mongo(self):
        try:
            from config import MONGO_URI, MONGO_DB_NAME
            if not MONGO_URI:
                return
            import pymongo
            self.mongo_client = pymongo.MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000
            )
            self.mongo_client.admin.command('ping')
            self.mongo_db = self.mongo_client[MONGO_DB_NAME]
            print(f"[MongoDB] Connected successfully to '{MONGO_DB_NAME}'")
        except Exception as e:
            print(f"[MongoDB] Connection error: {e}. Falling back to SQLite.")
            self.mongo_client = None
            self.mongo_db = None

    def _mongo_set(self, collection_name: str, filter_doc: dict, set_doc: dict, upsert: bool = True):
        if self.mongo_db is not None:
            try:
                self.mongo_db[collection_name].update_one(filter_doc, {"$set": set_doc}, upsert=upsert)
            except Exception as e:
                print(f"[MongoDB] Error writing to {collection_name}: {e}")

    def _mongo_delete(self, collection_name: str, filter_doc: dict):
        if self.mongo_db is not None:
            try:
                self.mongo_db[collection_name].delete_one(filter_doc)
            except Exception as e:
                print(f"[MongoDB] Error deleting from {collection_name}: {e}")

    def _mongo_insert(self, collection_name: str, doc: dict):
        if self.mongo_db is not None:
            try:
                self.mongo_db[collection_name].insert_one(dict(doc))
            except Exception as e:
                print(f"[MongoDB] Error inserting into {collection_name}: {e}")

    def _sync_with_mongo(self):
        """Bidirectional sync between MongoDB Atlas and local SQLite database."""
        if self.mongo_db is None:
            return
        try:
            conn = self.get_connection()
            cursor = conn.cursor()

            # 1. Admins
            mongo_admins = list(self.mongo_db.admins.find({}, {"_id": 0}))
            if mongo_admins:
                for a in mongo_admins:
                    cursor.execute('''
                        INSERT OR REPLACE INTO admins (user_id, username, added_by, added_date, is_default)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (a.get("user_id"), a.get("username"), a.get("added_by"), a.get("added_date"), a.get("is_default", 0)))
            cursor.execute('SELECT user_id, username, added_by, added_date, is_default FROM admins')
            for row in cursor.fetchall():
                self.mongo_db.admins.update_one(
                    {"user_id": row[0]},
                    {"$set": {"user_id": row[0], "username": row[1], "added_by": row[2], "added_date": row[3], "is_default": row[4]}},
                    upsert=True
                )

            # 2. Approved Users
            mongo_approved = list(self.mongo_db.approved_users.find({}, {"_id": 0}))
            if mongo_approved:
                for u in mongo_approved:
                    cursor.execute('''
                        INSERT OR REPLACE INTO approved_users (user_id, username, approved_by, approved_date)
                        VALUES (?, ?, ?, ?)
                    ''', (u.get("user_id"), u.get("username"), u.get("approved_by"), u.get("approved_date")))
            cursor.execute('SELECT user_id, username, approved_by, approved_date FROM approved_users')
            for row in cursor.fetchall():
                self.mongo_db.approved_users.update_one(
                    {"user_id": row[0]},
                    {"$set": {"user_id": row[0], "username": row[1], "approved_by": row[2], "approved_date": row[3]}},
                    upsert=True
                )

            # 3. Accounts
            mongo_accounts = list(self.mongo_db.accounts.find({}, {"_id": 0}))
            if mongo_accounts:
                for acc in mongo_accounts:
                    cursor.execute('''
                        INSERT OR REPLACE INTO accounts (
                            id, phone, session_file, status, added_date, last_used, added_by,
                            is_banned, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass,
                            enabled, session_user_id, label, last_checked, connection_status, file_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        acc.get("id"), acc.get("phone"), acc.get("session_file"), acc.get("status", "active"),
                        acc.get("added_date"), acc.get("last_used"), acc.get("added_by"),
                        acc.get("is_banned", 0), acc.get("proxy_type"), acc.get("proxy_ip"),
                        acc.get("proxy_port"), acc.get("proxy_user"), acc.get("proxy_pass"),
                        acc.get("enabled", 1), acc.get("session_user_id"), acc.get("label"),
                        acc.get("last_checked"), acc.get("connection_status"), acc.get("file_hash")
                    ))
            cursor.execute('''
                SELECT id, phone, session_file, status, added_date, last_used, added_by,
                       is_banned, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass,
                       enabled, session_user_id, label, last_checked, connection_status, file_hash
                FROM accounts
            ''')
            for row in cursor.fetchall():
                doc = {
                    "id": row[0], "phone": row[1], "session_file": row[2], "status": row[3],
                    "added_date": row[4], "last_used": row[5], "added_by": row[6],
                    "is_banned": row[7], "proxy_type": row[8], "proxy_ip": row[9],
                    "proxy_port": row[10], "proxy_user": row[11], "proxy_pass": row[12],
                    "enabled": row[13], "session_user_id": row[14], "label": row[15],
                    "last_checked": row[16], "connection_status": row[17], "file_hash": row[18]
                }
                self.mongo_db.accounts.update_one({"phone": row[1]}, {"$set": doc}, upsert=True)

            # 4. Report Campaigns
            mongo_campaigns = list(self.mongo_db.report_campaigns.find({}, {"_id": 0}))
            if mongo_campaigns:
                for c in mongo_campaigns:
                    cursor.execute('''
                        INSERT OR REPLACE INTO report_campaigns (
                            id, name, target, account_phones, messages, reports_per_message,
                            delay_seconds, exec_mode, reason, reason_detail, reason_option,
                            reason_choices, comment, interval_hours, duration_hours, max_cycles,
                            cycle_count, status, next_run_at, ends_at, created_by, created_at,
                            last_run_at, last_result
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        c.get("id"), c.get("name"), c.get("target"),
                        json.dumps(c.get("account_phones") if isinstance(c.get("account_phones"), list) else []),
                        json.dumps(c.get("messages") if isinstance(c.get("messages"), list) else []),
                        c.get("reports_per_message", 2), c.get("delay_seconds", 5),
                        c.get("exec_mode", "safe"), c.get("reason", "spam"), c.get("reason_detail", ""),
                        c.get("reason_option", ""),
                        json.dumps(c.get("reason_choices") if isinstance(c.get("reason_choices"), list) else []),
                        c.get("comment", ""), c.get("interval_hours", 0), c.get("duration_hours", 0),
                        c.get("max_cycles", 1), c.get("cycle_count", 0), c.get("status", "ready"),
                        c.get("next_run_at"), c.get("ends_at"), c.get("created_by"), c.get("created_at"),
                        c.get("last_run_at"), c.get("last_result")
                    ))
            cursor.execute('SELECT * FROM report_campaigns')
            cols = [col[0] for col in cursor.description]
            for r in cursor.fetchall():
                c_dict = dict(zip(cols, r))
                self.mongo_db.report_campaigns.update_one({"id": c_dict.get("id")}, {"$set": c_dict}, upsert=True)

            # 5. User states
            mongo_states = list(self.mongo_db.user_states.find({}, {"_id": 0}))
            if mongo_states:
                for s in mongo_states:
                    cursor.execute('''
                        INSERT OR REPLACE INTO user_states (user_id, state, data, updated_at)
                        VALUES (?, ?, ?, ?)
                    ''', (s.get("user_id"), s.get("state"), s.get("data"), s.get("updated_at")))

            # 6. Report stats
            cursor.execute('SELECT * FROM report_stats')
            for r in cursor.fetchall():
                self.mongo_db.report_stats.update_one({"id": r[0]}, {"$set": {"id": r[0], "date": r[1], "total_reports": r[2], "success": r[3], "failed": r[4]}}, upsert=True)

            conn.commit()
            conn.close()
            print("[MongoDB] Synced accounts, admins, campaigns, and state successfully with MongoDB Atlas.")
        except Exception as exc:
            print(f"[MongoDB] Sync warning: {exc}")

    def get_connection(self):
        conn = sqlite3.connect(self.db_file, timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
        except Exception:
            pass
        return conn

    def _ensure_column(self, cursor, table: str, column: str, declaration: str):
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError:
            pass

    def init_db(self):
        """Initialize database tables and run backward-compatible migrations."""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Admins table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_by INTEGER,
                added_date TEXT,
                is_default BOOLEAN DEFAULT 0
            )
        ''')

        # Approved users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS approved_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                approved_by INTEGER,
                approved_date TEXT
            )
        ''')

        # Accounts table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE,
                session_file TEXT,
                status TEXT DEFAULT 'active',
                added_date TEXT,
                last_used TEXT,
                added_by INTEGER,
                is_banned BOOLEAN DEFAULT 0,
                proxy_type TEXT,
                proxy_ip TEXT,
                proxy_port INTEGER,
                proxy_user TEXT,
                proxy_pass TEXT
            )
        ''')

        # Migrations to add proxy columns if database already exists
        for col, col_type in [
            ('proxy_type', 'TEXT'), ('proxy_ip', 'TEXT'), ('proxy_port', 'INTEGER'),
            ('proxy_user', 'TEXT'), ('proxy_pass', 'TEXT'),
            ('enabled', 'INTEGER DEFAULT 1'),
            ('session_user_id', 'INTEGER'),
            ('label', 'TEXT'),
            ('last_checked', 'TEXT'),
            ('connection_status', 'TEXT'),
            ('file_hash', 'TEXT'),
        ]:
            self._ensure_column(cursor, 'accounts', col, col_type)

        # Report statistics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS report_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                total_reports INTEGER DEFAULT 0,
                success INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0
            )
        ''')

        # Report logs
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS report_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_phone TEXT,
                target TEXT,
                message_link TEXT,
                status TEXT,
                reason TEXT,
                timestamp TEXT
            )
        ''')

        # User states for multi-step operations
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT,
                data TEXT,
                updated_at TEXT
            )
        ''')

        # ------------------------------------------------------------------
        # Review manager tables (new, additive)
        # ------------------------------------------------------------------
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT UNIQUE,
                telegram_id INTEGER,
                access_hash INTEGER,
                title TEXT,
                username TEXT,
                added_by INTEGER,
                created_at TEXT,
                last_used TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                label TEXT NOT NULL,
                description TEXT,
                telegram_reason TEXT NOT NULL DEFAULT 'fake',
                enabled INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 100,
                created_at TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_subcategories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                label TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 100,
                UNIQUE(category_id, key)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                category_id INTEGER,
                subcategory_id INTEGER,
                explanation TEXT,
                created_by INTEGER,
                created_at TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_by INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                status TEXT DEFAULT 'draft',
                interval_hours REAL DEFAULT 2,
                max_cycles INTEGER DEFAULT 5,
                current_cycle INTEGER DEFAULT 0,
                next_review_at TEXT,
                created_at TEXT,
                updated_at TEXT,
                started_at TEXT,
                paused_at TEXT,
                completed_at TEXT,
                last_error TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                message_link TEXT,
                message_text TEXT,
                category_id INTEGER,
                subcategory_id INTEGER,
                explanation TEXT,
                selected INTEGER DEFAULT 0,
                configured INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                reviewed_by_account_id INTEGER,
                reviewed_at TEXT,
                created_at TEXT,
                UNIQUE(plan_id, channel_id, message_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                cycle_number INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                started_at TEXT,
                reminded_at TEXT,
                completed_at TEXT,
                UNIQUE(plan_id, cycle_number)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS review_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                cycle_number INTEGER NOT NULL,
                chat_id INTEGER,
                message_id INTEGER,
                created_at TEXT,
                sent_at TEXT,
                UNIQUE(plan_id, cycle_number)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS report_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                target TEXT NOT NULL,
                account_phones TEXT,
                messages TEXT,
                reports_per_message INTEGER DEFAULT 2,
                delay_seconds INTEGER DEFAULT 5,
                exec_mode TEXT DEFAULT 'safe',
                reason TEXT DEFAULT 'spam',
                reason_detail TEXT DEFAULT '',
                reason_option TEXT DEFAULT '',
                reason_choices TEXT DEFAULT '[]',
                comment TEXT,
                interval_hours REAL DEFAULT 0,
                duration_hours REAL DEFAULT 0,
                max_cycles INTEGER DEFAULT 1,
                cycle_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'ready',
                next_run_at TEXT,
                ends_at TEXT,
                created_by INTEGER,
                created_at TEXT,
                last_run_at TEXT,
                last_result TEXT
            )
        ''')

        # Additive migrations for databases created by older versions.
        for col, col_type in [
            ('reason_detail', "TEXT DEFAULT ''"),
            ('reason_option', "TEXT DEFAULT ''"),
            ('reason_choices', "TEXT DEFAULT '[]'"),
            ('duration_hours', 'REAL DEFAULT 0'),
            ('ends_at', 'TEXT'),
        ]:
            self._ensure_column(cursor, 'report_campaigns', col, col_type)

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT
            )
        ''')

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_review_items_plan ON review_items(plan_id, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_review_plans_status ON review_plans(status, next_review_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_report_campaigns_due ON report_campaigns(status, next_run_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_report_logs_time ON report_logs(timestamp)')

        # Insert default admins
        for admin_id in DEFAULT_ADMINS:
            cursor.execute('''
                INSERT OR IGNORE INTO admins (user_id, username, added_by, added_date, is_default)
                VALUES (?, ?, ?, ?, ?)
            ''', (admin_id, 'default_admin', admin_id, _now(), 1))

        self._seed_review_categories(cursor)

        conn.commit()
        conn.close()

    def _seed_review_categories(self, cursor):
        """Seed configurable labels mapped to Telegram's real report reasons."""
        categories = [
            ('scam_fraud', 'Scam or fraud', 'fake', 'Fraudulent schemes and deceptive offers.', 10),
            ('impersonation', 'Impersonation', 'fake', 'Pretending to be another person, brand or organization.', 20),
            ('deceptive_financial_claims', 'Deceptive or unrealistic financial claims', 'fake',
             'Guaranteed returns, unrealistic profits or misleading investment claims.', 30),
            ('fraudulent_seller', 'Fraudulent seller/product/service', 'fake',
             'Non-delivery, counterfeit goods or fraudulent services.', 40),
            ('malware_phishing', 'Malware/phishing', 'spam',
             'Malicious links, credential theft or malware distribution.', 50),
        ]
        subcategories = {
            'scam_fraud': [
                ('advance_fee', 'Advance-fee / upfront payment', 10),
                ('investment_scam', 'Investment scam', 20),
                ('crypto_scam', 'Crypto / wallet scam', 30),
                ('giveaway_scam', 'Fake giveaway or prize', 40),
                ('romance_scam', 'Romance / relationship scam', 50),
            ],
            'impersonation': [
                ('account_impersonation', 'Account impersonation', 10),
                ('brand_impersonation', 'Brand / business impersonation', 20),
                ('government_impersonation', 'Government / official impersonation', 30),
                ('celebrity_impersonation', 'Celebrity impersonation', 40),
            ],
            'deceptive_financial_claims': [
                ('guaranteed_returns', 'Guaranteed returns', 10),
                ('ponzi_scheme', 'Ponzi / pyramid scheme', 20),
                ('fake_profit_proof', 'Fake profit screenshots or proofs', 30),
                ('unlicensed_investment', 'Unlicensed investment offer', 40),
            ],
            'fraudulent_seller': [
                ('non_delivery', 'Paid but no delivery', 10),
                ('counterfeit_goods', 'Counterfeit goods', 20),
                ('fake_service', 'Fake or undelivered service', 30),
                ('refund_scam', 'Refund / escrow scam', 40),
            ],
            'malware_phishing': [
                ('phishing_link', 'Phishing link', 10),
                ('credential_theft', 'Credential / OTP theft', 20),
                ('malware_download', 'Malware or suspicious download', 30),
                ('fake_login_page', 'Fake login page', 40),
            ],
        }

        for key, label, reason, description, order in categories:
            cursor.execute('''
                INSERT OR IGNORE INTO review_categories
                    (key, label, description, telegram_reason, enabled, sort_order, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
            ''', (key, label, description, reason, order, _now()))
            cursor.execute(' SELECT id FROM review_categories WHERE key = ?', (key,))
            row = cursor.fetchone()
            if not row:
                continue
            category_id = row[0]
            for sub_key, sub_label, sub_order in subcategories.get(key, []):
                cursor.execute('''
                    INSERT OR IGNORE INTO review_subcategories
                        (category_id, key, label, enabled, sort_order)
                    VALUES (?, ?, ?, 1, ?)
                ''', (category_id, sub_key, sub_label, sub_order))

    # Admin Management
    def is_admin(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT user_id FROM admins WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None

    @db_write_lock
    def add_admin(self, user_id: int, username: str, added_by: int) -> bool:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO admins (user_id, username, added_by, added_date, is_default)
                VALUES (?, ?, ?, ?, 0)
            ''', (user_id, username, added_by, _now()))
            conn.commit()
            conn.close()
            self._mongo_set("admins", {"user_id": user_id}, {"user_id": user_id, "username": username, "added_by": added_by, "added_date": _now(), "is_default": 0})
            return True
        except Exception as e:
            print(f"Error adding admin: {e}")
            return False

    @db_write_lock
    def remove_admin(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT is_default FROM admins WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        if result and result[0] == 1:
            conn.close()
            return False  # Cannot remove default admin
        cursor.execute(' DELETE FROM admins WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        self._mongo_delete("admins", {"user_id": user_id})
        return True

    def get_all_admins(self) -> list:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT user_id, username, added_by, added_date, is_default FROM admins')
        results = cursor.fetchall()
        conn.close()
        return results

    # User Management
    def is_approved(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT user_id FROM approved_users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None

    @db_write_lock
    def approve_user(self, user_id: int, username: str, approved_by: int) -> bool:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO approved_users (user_id, username, approved_by, approved_date)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, approved_by, _now()))
            conn.commit()
            conn.close()
            self._mongo_set("approved_users", {"user_id": user_id}, {"user_id": user_id, "username": username, "approved_by": approved_by, "approved_date": _now()})
            return True
        except Exception as e:
            print(f"Error approving user: {e}")
            return False

    @db_write_lock
    def revoke_user(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' DELETE FROM approved_users WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        self._mongo_delete("approved_users", {"user_id": user_id})
        return True

    def get_all_approved_users(self) -> list:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT user_id, username, approved_by, approved_date FROM approved_users')
        results = cursor.fetchall()
        conn.close()
        return results

    # Account Management
    @db_write_lock
    def add_account(self, phone: str, session_file: str, added_by: int, proxy_type: str = None, proxy_ip: str = None, proxy_port: int = None, proxy_user: str = None, proxy_pass: str = None) -> bool:
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO accounts (phone, session_file, status, added_date, added_by, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass, enabled, last_checked, connection_status)
                VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, 1, ?, 'connected (login)')
                ON CONFLICT(phone) DO UPDATE SET
                    session_file = excluded.session_file,
                    status = 'active',
                    added_date = excluded.added_date,
                    added_by = excluded.added_by,
                    proxy_type = excluded.proxy_type,
                    proxy_ip = excluded.proxy_ip,
                    proxy_port = excluded.proxy_port,
                    proxy_user = excluded.proxy_user,
                    proxy_pass = excluded.proxy_pass,
                    last_checked = excluded.last_checked,
                    connection_status = excluded.connection_status
            ''', (phone, session_file, _now(), added_by, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass, _now()))
            conn.commit()
            conn.close()
            self._mongo_set("accounts", {"phone": phone}, {
                "phone": phone, "session_file": session_file, "status": "active",
                "added_date": _now(), "added_by": added_by, "proxy_type": proxy_type,
                "proxy_ip": proxy_ip, "proxy_port": proxy_port, "proxy_user": proxy_user,
                "proxy_pass": proxy_pass, "enabled": 1, "last_checked": _now(),
                "connection_status": "connected (login)", "is_banned": 0
            })
            return True
        except Exception as e:
            print(f"Error adding account: {e}")
            return False

    @db_write_lock
    def add_account_record(self, phone: str, session_file: str, added_by: int, session_user_id: int = None,
                           file_hash: str = None, label: str = None) -> bool:
        """Add an imported account without overwriting an existing record."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT id FROM accounts WHERE phone = ?', (phone,))
        if cursor.fetchone():
            conn.close()
            return False
        cursor.execute('''
            INSERT INTO accounts
                (phone, session_file, status, added_date, added_by, enabled,
                 session_user_id, file_hash, label, last_checked, connection_status)
            VALUES (?, ?, 'active', ?, ?, 1, ?, ?, ?, ?, 'connected (import)')
        ''', (phone, session_file, _now(), added_by, session_user_id, file_hash, label, _now()))
        conn.commit()
        conn.close()
        self._mongo_set("accounts", {"phone": phone}, {
            "phone": phone, "session_file": session_file, "status": "active",
            "added_date": _now(), "added_by": added_by, "enabled": 1,
            "session_user_id": session_user_id, "file_hash": file_hash, "label": label,
            "last_checked": _now(), "connection_status": "connected (import)", "is_banned": 0
        })
        return True

    @db_write_lock
    def update_account_status(self, phone: str, status: str, is_banned: bool = False):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE accounts SET status = ?, is_banned = ?, last_used = ?
            WHERE phone = ?
        ''', (status, 1 if is_banned else 0, _now(), phone))
        conn.commit()
        conn.close()
        self._mongo_set("accounts", {"phone": phone}, {
            "status": status, "is_banned": 1 if is_banned else 0, "last_used": _now()
        })

    @db_write_lock
    def update_account_proxy(self, phone: str, proxy_type: str, proxy_ip: str, proxy_port: int, proxy_user: str = None, proxy_pass: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE accounts
            SET proxy_type = ?, proxy_ip = ?, proxy_port = ?, proxy_user = ?, proxy_pass = ?
            WHERE phone = ?
        ''', (proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass, phone))
        conn.commit()
        conn.close()
        self._mongo_set("accounts", {"phone": phone}, {
            "proxy_type": proxy_type, "proxy_ip": proxy_ip, "proxy_port": proxy_port,
            "proxy_user": proxy_user, "proxy_pass": proxy_pass
        })

    def get_accounts(self) -> list:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT id, phone, session_file, status, added_date, last_used, is_banned, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass, COALESCE(enabled, 1) FROM accounts')
        results = cursor.fetchall()
        conn.close()
        return results

    def get_account_records(self) -> list:
        """Return account rows with review-manager metadata as dictionaries."""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, phone, session_file, status, added_date, last_used, is_banned,
                   proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass,
                   COALESCE(enabled, 1) AS enabled, session_user_id, label,
                   last_checked, connection_status, file_hash
            FROM accounts ORDER BY id
        ''')
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def get_account_by_id(self, account_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(' SELECT * FROM accounts WHERE id = ?', (account_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def account_exists_by_phone(self, phone: str) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT id FROM accounts WHERE phone = ?', (phone,))
        result = cursor.fetchone()
        conn.close()
        return result is not None

    @db_write_lock
    def toggle_account_enabled(self, account_id: int, enabled: bool) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' UPDATE accounts SET enabled = ? WHERE id = ?', (1 if enabled else 0, account_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def set_account_check(self, account_id: int, connection_status: str, authorized: bool = None,
                          session_user_id: int = None, phone: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        status = 'active'if authorized else ('error'if authorized is False else None)
        if phone:
            cursor.execute(' SELECT id FROM accounts WHERE phone = ? AND id != ?', (phone, account_id))
            if cursor.fetchone():
                phone = None
        if phone:
            cursor.execute('''
                UPDATE accounts
                SET connection_status = ?, last_checked = ?, status = COALESCE(?, status),
                    session_user_id = COALESCE(?, session_user_id), phone = ?
                WHERE id = ?
            ''', (connection_status, _now(), status, session_user_id, phone, account_id))
        else:
            cursor.execute('''
                UPDATE accounts
                SET connection_status = ?, last_checked = ?, status = COALESCE(?, status),
                    session_user_id = COALESCE(?, session_user_id)
                WHERE id = ?
            ''', (connection_status, _now(), status, session_user_id, account_id))
        conn.commit()
        conn.close()

    def get_active_accounts(self) -> list:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, phone, session_file, status, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_pass
            FROM accounts
            WHERE status = 'active' AND is_banned = 0 AND COALESCE(enabled, 1) = 1
            ORDER BY id
        ''')
        results = cursor.fetchall()
        conn.close()
        return results

    def get_enabled_accounts(self) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM accounts
            WHERE COALESCE(enabled, 1) = 1
            ORDER BY id
        ''')
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    @db_write_lock
    def remove_account(self, phone: str) -> bool:
        from config import SESSIONS_FOLDER
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT session_file FROM accounts WHERE phone = ?', (phone,))
        result = cursor.fetchone()
        if result:
            session_file = result[0]
            # Delete the session file only when it is inside the sessions folder.
            if session_file and os.path.exists(session_file):
                try:
                    root = os.path.realpath(SESSIONS_FOLDER)
                    target = os.path.realpath(session_file)
                    if os.path.commonpath([root, target]) == root:
                        os.remove(target)
                except Exception:
                    pass
            cursor.execute(' DELETE FROM accounts WHERE phone = ?', (phone,))
            conn.commit()
            conn.close()
            self._mongo_delete("accounts", {"phone": phone})
            return True
        conn.close()
        return False

    # Report Statistics
    @db_write_lock
    def add_report_stats(self, total: int, success: int, failed: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute(' SELECT id FROM report_stats WHERE date = ?', (today,))
        existing = cursor.fetchone()

        if existing:
            cursor.execute('''
                UPDATE report_stats
                SET total_reports = total_reports + ?,
                    success = success + ?,
                    failed = failed + ?
                WHERE date = ?
            ''', (total, success, failed, today))
        else:
            cursor.execute('''
                INSERT INTO report_stats (date, total_reports, success, failed)
                VALUES (?, ?, ?, ?)
            ''', (today, total, success, failed))

        conn.commit()
        cursor.execute('SELECT total_reports, success, failed FROM report_stats WHERE date = ?', (today,))
        row = cursor.fetchone()
        conn.close()
        if row:
            self._mongo_set("report_stats", {"date": today}, {
                "date": today, "total_reports": row[0], "success": row[1], "failed": row[2]
            })

    def get_stats(self) -> dict:
        conn = self.get_connection()
        cursor = conn.cursor()

        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute(' SELECT total_reports, success, failed FROM report_stats WHERE date = ?', (today,))
        today_stats = cursor.fetchone() or (0, 0, 0)

        cursor.execute(' SELECT SUM(total_reports), SUM(success), SUM(failed) FROM report_stats')
        all_time = cursor.fetchone() or (0, 0, 0)

        cursor.execute(' SELECT COUNT(*), SUM(CASE WHEN status="active" THEN 1 ELSE 0 END), SUM(CASE WHEN is_banned=1 THEN 1 ELSE 0 END) FROM accounts')
        account_stats = cursor.fetchone() or (0, 0, 0)

        conn.close()

        return {
            'today': {'total': today_stats[0], 'success': today_stats[1], 'failed': today_stats[2]},
            'all_time': {'total': all_time[0] or 0, 'success': all_time[1] or 0, 'failed': all_time[2] or 0},
            'accounts': {'total': account_stats[0], 'active': account_stats[1], 'banned': account_stats[2]}
        }

    @db_write_lock
    def add_report_log(self, account_phone: str, target: str, message_link: str, status: str, reason: str = ""):
        conn = self.get_connection()
        cursor = conn.cursor()
        now_ts = _now()
        cursor.execute('''
            INSERT INTO report_logs (account_phone, target, message_link, status, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (account_phone, target, message_link, status, reason, now_ts))
        conn.commit()
        conn.close()
        self._mongo_insert("report_logs", {
            "account_phone": account_phone, "target": target, "message_link": message_link,
            "status": status, "reason": reason, "timestamp": now_ts
        })

    def get_report_logs(self, offset: int = 0, limit: int = 100) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(' SELECT * FROM report_logs ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    # User States
    @db_write_lock
    def set_user_state(self, user_id: int, state: str, data: dict = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        now_ts = _now()
        cursor.execute('''
            INSERT OR REPLACE INTO user_states (user_id, state, data, updated_at)
            VALUES (?, ?, ?, ?)
        ''', (user_id, state, json.dumps(data or {}), now_ts))
        conn.commit()
        conn.close()
        self._mongo_set("user_states", {"user_id": user_id}, {
            "user_id": user_id, "state": state, "data": json.dumps(data or {}), "updated_at": now_ts
        })

    def get_user_state(self, user_id: int) -> tuple:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT state, data FROM user_states WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        if result:
            try:
                return result[0], json.loads(result[1] or '{}')
            except (TypeError, ValueError):
                return result[0], {}
        return None, {}

    @db_write_lock
    def clear_user_state(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' DELETE FROM user_states WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        self._mongo_delete("user_states", {"user_id": user_id})

    # ------------------------------------------------------------------
    # Review categories / presets
    # ------------------------------------------------------------------
    def get_review_categories(self, include_disabled: bool = False) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = ' SELECT * FROM review_categories'
        if not include_disabled:
            query += ' WHERE enabled = 1'
        query += ' ORDER BY sort_order, id'
        cursor.execute(query)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def get_category_by_key(self, key: str):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(' SELECT * FROM review_categories WHERE key = ?', (key,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_category_by_id(self, category_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(' SELECT * FROM review_categories WHERE id = ?', (category_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_subcategories(self, category_id: int, include_disabled: bool = False) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = ' SELECT * FROM review_subcategories WHERE category_id = ?'
        if not include_disabled:
            query += ' AND enabled = 1'
        query += ' ORDER BY sort_order, id'
        cursor.execute(query, (category_id,))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def get_subcategory_by_id(self, subcategory_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(' SELECT * FROM review_subcategories WHERE id = ?', (subcategory_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @db_write_lock
    def add_review_category(self, key: str, label: str, telegram_reason: str,
                            description: str = "", sort_order: int = 200) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO review_categories
                    (key, label, description, telegram_reason, enabled, sort_order, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    label = excluded.label,
                    description = excluded.description,
                    telegram_reason = excluded.telegram_reason,
                    sort_order = excluded.sort_order
            ''', (key, label, description, telegram_reason, sort_order, _now()))
            conn.commit()
            return True
        except Exception as e:
            print(f"Error adding review category: {e}")
            return False
        finally:
            conn.close()

    @db_write_lock
    def add_review_subcategory(self, category_id: int, key: str, label: str, sort_order: int = 200) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO review_subcategories (category_id, key, label, enabled, sort_order)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(category_id, key) DO UPDATE SET
                    label = excluded.label,
                    sort_order = excluded.sort_order
            ''', (category_id, key, label, sort_order))
            conn.commit()
            return True
        except Exception as e:
            print(f"Error adding review subcategory: {e}")
            return False
        finally:
            conn.close()

    @db_write_lock
    def toggle_review_category(self, key: str) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' UPDATE review_categories SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE key = ?', (key,))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    def list_presets(self, limit: int = 10) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT p.*, c.label AS category_label, s.label AS subcategory_label
            FROM review_presets p
            LEFT JOIN review_categories c ON c.id = p.category_id
            LEFT JOIN review_subcategories s ON s.id = p.subcategory_id
            ORDER BY p.id DESC LIMIT ?
        ''', (limit,))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def get_preset(self, preset_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(' SELECT * FROM review_presets WHERE id = ?', (preset_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @db_write_lock
    def create_preset(self, name: str, category_id: int, subcategory_id: int,
                      explanation: str, created_by: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO review_presets (name, category_id, subcategory_id, explanation, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    category_id = excluded.category_id,
                    subcategory_id = excluded.subcategory_id,
                    explanation = excluded.explanation,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at
            ''', (name.strip()[:80], category_id, subcategory_id, explanation, created_by, _now()))
            conn.commit()
            return True
        except Exception as e:
            print(f"Error saving preset: {e}")
            return False
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Review channels / plans / items
    # ------------------------------------------------------------------
    @db_write_lock
    def upsert_review_channel(self, identifier: str, telegram_id: int = None, access_hash: int = None,
                              title: str = None, username: str = None, added_by: int = None) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO review_channels
                (identifier, telegram_id, access_hash, title, username, added_by, created_at, last_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identifier) DO UPDATE SET
                telegram_id = COALESCE(excluded.telegram_id, review_channels.telegram_id),
                access_hash = COALESCE(excluded.access_hash, review_channels.access_hash),
                title = COALESCE(excluded.title, review_channels.title),
                username = COALESCE(excluded.username, review_channels.username),
                last_used = excluded.last_used
        ''', (identifier, telegram_id, access_hash, title, username, added_by, _now(), _now()))
        cursor.execute(' SELECT id FROM review_channels WHERE identifier = ?', (identifier,))
        row = cursor.fetchone()
        conn.commit()
        conn.close()
        return row[0] if row else None

    def get_review_channel(self, channel_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(' SELECT * FROM review_channels WHERE id = ?', (channel_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def count_review_channels(self) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT COUNT(*) FROM review_channels')
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_recent_review_channels(self, limit: int = 5) -> list:
        """Return recently used review channels for quick report-target buttons."""
        try:
            limit = max(1, min(int(limit), 20))
        except (TypeError, ValueError):
            limit = 5
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM review_channels
            ORDER BY COALESCE(last_used, created_at) DESC, id DESC
            LIMIT ?
        ''', (limit,))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    @db_write_lock
    def create_review_draft(self, user_id: int, channel_id: int,
                            interval_hours: float = None, max_cycles: int = None) -> int:
        from config import REVIEW_DEFAULT_INTERVAL_HOURS, REVIEW_DEFAULT_MAX_CYCLES
        conn = self.get_connection()
        cursor = conn.cursor()
        # Discard this user's previous unfinished draft so no stale selectors remain.
        cursor.execute(" SELECT id FROM review_plans WHERE created_by = ? AND status = 'draft'", (user_id,))
        old_ids = [row[0] for row in cursor.fetchall()]
        for old_id in old_ids:
            cursor.execute(' DELETE FROM review_items WHERE plan_id = ?', (old_id,))
        cursor.execute(" DELETE FROM review_plans WHERE created_by = ? AND status = 'draft'", (user_id,))

        cursor.execute('''
            INSERT INTO review_plans
                (created_by, channel_id, status, interval_hours, max_cycles, current_cycle,
                 next_review_at, created_at, updated_at)
            VALUES (?, ?, 'draft', ?, ?, 0, NULL, ?, ?)
        ''', (
            user_id, channel_id,
            float(interval_hours or REVIEW_DEFAULT_INTERVAL_HOURS),
            int(max_cycles or REVIEW_DEFAULT_MAX_CYCLES),
            _now(), _now()
        ))
        plan_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return plan_id

    @db_write_lock
    def add_review_candidates(self, plan_id: int, channel_id: int, posts: list) -> int:
        """Insert fetched posts as unselected candidates; duplicates are ignored."""
        conn = self.get_connection()
        cursor = conn.cursor()
        inserted = 0
        for post in posts:
            cursor.execute('''
                INSERT OR IGNORE INTO review_items
                    (plan_id, channel_id, message_id, message_link, message_text,
                     selected, configured, status, created_at)
                VALUES (?, ?, ?, ?, ?, 0, 0, 'candidate', ?)
            ''', (
                plan_id, channel_id, int(post['message_id']), post.get('message_link'),
                (post.get('message_text') or '')[:1000], _now()
            ))
            inserted += cursor.rowcount
        conn.commit()
        conn.close()
        return inserted

    def get_review_candidates(self, plan_id: int, selected_only: bool = False) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = ' SELECT * FROM review_items WHERE plan_id = ?'
        if selected_only:
            query += ' AND selected = 1'
        query += ' ORDER BY message_id ASC'
        cursor.execute(query, (plan_id,))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def count_review_candidates(self, plan_id: int, selected_only: bool = False) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        query = ' SELECT COUNT(*) FROM review_items WHERE plan_id = ?'
        if selected_only:
            query += ' AND selected = 1'
        cursor.execute(query, (plan_id,))
        count = cursor.fetchone()[0]
        conn.close()
        return count

    @db_write_lock
    def toggle_review_candidate(self, item_id: int, plan_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT selected FROM review_items WHERE id = ? AND plan_id = ?', (item_id, plan_id))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        new_value = 0 if row[0] else 1
        cursor.execute(' UPDATE review_items SET selected = ? WHERE id = ? AND plan_id = ?', (new_value, item_id, plan_id))
        conn.commit()
        conn.close()
        return bool(new_value)

    @db_write_lock
    def set_all_review_candidates(self, plan_id: int, selected: bool):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' UPDATE review_items SET selected = ? WHERE plan_id = ?', (1 if selected else 0, plan_id))
        conn.commit()
        conn.close()

    @db_write_lock
    def activate_selected_candidates(self, plan_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_items
            SET status = 'pending', configured = 0
            WHERE plan_id = ? AND selected = 1
        ''', (plan_id,))
        changed = cursor.rowcount
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def remove_unselected_candidates(self, plan_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' DELETE FROM review_items WHERE plan_id = ? AND selected = 0', (plan_id,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted

    @db_write_lock
    def set_item_category(self, item_id: int, category_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_items
            SET category_id = ?, subcategory_id = NULL, configured = 0
            WHERE id = ?
        ''', (category_id, item_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def set_item_subcategory(self, item_id: int, subcategory_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' UPDATE review_items SET subcategory_id = ?, configured = 0 WHERE id = ?', (subcategory_id, item_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def set_item_explanation(self, item_id: int, explanation: str, configured: bool = True) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_items SET explanation = ?, configured = ?
            WHERE id = ?
        ''', ((explanation or '').strip()[:1000], 1 if configured else 0, item_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def apply_preset_to_item(self, item_id: int, preset_id: int) -> bool:
        preset = self.get_preset(preset_id)
        if not preset:
            return False
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_items
            SET category_id = ?, subcategory_id = ?, explanation = ?, configured = 1
            WHERE id = ?
        ''', (preset.get('category_id'), preset.get('subcategory_id'),
              preset.get('explanation') or '', item_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    def get_review_item(self, item_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT i.*, c.label AS category_label, s.label AS subcategory_label,
                   p.status AS plan_status, p.created_by
            FROM review_items i
            LEFT JOIN review_categories c ON c.id = i.category_id
            LEFT JOIN review_subcategories s ON s.id = i.subcategory_id
            LEFT JOIN review_plans p ON p.id = i.plan_id
            WHERE i.id = ?
        ''', (item_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_first_unconfigured_item(self, plan_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM review_items
            WHERE plan_id = ? AND selected = 1 AND configured = 0
            ORDER BY message_id ASC LIMIT 1
        ''', (plan_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def count_unconfigured_items(self, plan_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT COUNT(*) FROM review_items WHERE plan_id = ? AND selected = 1 AND configured = 0', (plan_id,))
        count = cursor.fetchone()[0]
        conn.close()
        return count

    @db_write_lock
    def delete_review_draft(self, plan_id: int, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(" SELECT id FROM review_plans WHERE id = ? AND created_by = ? AND status = 'draft'", (plan_id, user_id))
        if not cursor.fetchone():
            conn.close()
            return False
        cursor.execute(' DELETE FROM review_items WHERE plan_id = ?', (plan_id,))
        cursor.execute(" DELETE FROM review_plans WHERE id = ? AND created_by = ? AND status = 'draft'", (plan_id, user_id))
        conn.commit()
        conn.close()
        return True

    @db_write_lock
    def set_review_plan_settings(self, plan_id: int, interval_hours: float, max_cycles: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_plans SET interval_hours = ?, max_cycles = ?, updated_at = ?
            WHERE id = ? AND status = 'draft'
        ''', (float(interval_hours), int(max_cycles), _now(), plan_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def finalize_review_plan(self, plan_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM review_items WHERE plan_id = ? AND selected = 1
        ''', (plan_id,))
        selected = cursor.fetchone()[0]
        cursor.execute('''
            SELECT COUNT(*) FROM review_items WHERE plan_id = ? AND selected = 1 AND configured = 1
        ''', (plan_id,))
        configured = cursor.fetchone()[0]
        if selected == 0 or selected != configured:
            conn.close()
            return False
        cursor.execute('''
            UPDATE review_plans SET status = 'ready', updated_at = ?
            WHERE id = ? AND status = 'draft'
        ''', (_now(), plan_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    def get_review_plan(self, plan_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT p.*, c.title AS channel_title, c.identifier AS channel_identifier,
                   c.username AS channel_username
            FROM review_plans p
            LEFT JOIN review_channels c ON c.id = p.channel_id
            WHERE p.id = ?
        ''', (plan_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_review_plans(self, status: str = None, offset: int = 0, limit: int = 10) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = '''
            SELECT p.*, c.title AS channel_title, c.identifier AS channel_identifier,
                   c.username AS channel_username,
                   (SELECT COUNT(*) FROM review_items i WHERE i.plan_id = p.id AND i.selected = 1) AS item_count,
                   (SELECT COUNT(*) FROM review_items i WHERE i.plan_id = p.id AND i.selected = 1 AND i.status = 'reviewed') AS reviewed_count
            FROM review_plans p
            LEFT JOIN review_channels c ON c.id = p.channel_id
            WHERE p.status != 'draft'
        '''
        params = []
        if status:
            query += ' AND p.status = ?'
            params.append(status)
        query += ' ORDER BY p.id DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        cursor.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def count_review_plans(self, status: str = None) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        query = " SELECT COUNT(*) FROM review_plans WHERE status != 'draft'"
        params = []
        if status:
            query += ' AND status = ?'
            params.append(status)
        cursor.execute(query, params)
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_plan_items(self, plan_id: int, selected_only: bool = True, status: str = None,
                       offset: int = 0, limit: int = 10) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = '''
            SELECT i.*, c.label AS category_label, s.label AS subcategory_label,
                   a.phone AS reviewer_phone, a.label AS reviewer_label
            FROM review_items i
            LEFT JOIN review_categories c ON c.id = i.category_id
            LEFT JOIN review_subcategories s ON s.id = i.subcategory_id
            LEFT JOIN accounts a ON a.id = i.reviewed_by_account_id
            WHERE i.plan_id = ?
        '''
        params = [plan_id]
        if selected_only:
            query += ' AND i.selected = 1'
        if status:
            query += ' AND i.status = ?'
            params.append(status)
        query += ' ORDER BY i.message_id ASC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        cursor.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def count_plan_items(self, plan_id: int, selected_only: bool = True, status: str = None) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        query = ' SELECT COUNT(*) FROM review_items WHERE plan_id = ?'
        params = [plan_id]
        if selected_only:
            query += ' AND selected = 1'
        if status:
            query += ' AND status = ?'
            params.append(status)
        cursor.execute(query, params)
        count = cursor.fetchone()[0]
        conn.close()
        return count

    @db_write_lock
    def start_review_plan(self, plan_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT status, interval_hours FROM review_plans WHERE id = ?', (plan_id,))
        row = cursor.fetchone()
        if not row or row[0] != 'ready':
            conn.close()
            return False
        next_at = _iso_after(row[1])
        cursor.execute('''
            UPDATE review_plans
            SET status = 'active', current_cycle = 1, next_review_at = ?,
                started_at = ?, updated_at = ?, paused_at = NULL, completed_at = NULL
            WHERE id = ?
        ''', (next_at, _now(), _now(), plan_id))
        cursor.execute('''
            INSERT OR IGNORE INTO review_cycles
                (plan_id, cycle_number, status, started_at)
            VALUES (?, 1, 'active', ?)
        ''', (plan_id, _now()))
        conn.commit()
        conn.close()
        return True

    @db_write_lock
    def pause_review_plan(self, plan_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_plans SET status = 'paused', paused_at = ?, updated_at = ?
            WHERE id = ? AND status = 'active'
        ''', (_now(), _now(), plan_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def resume_review_plan(self, plan_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(" SELECT interval_hours FROM review_plans WHERE id = ? AND status = 'paused'", (plan_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        cursor.execute('''
            UPDATE review_plans SET status = 'active', next_review_at = ?,
                updated_at = ?, paused_at = NULL
            WHERE id = ?
        ''', (_iso_after(row[0]), _now(), plan_id))
        conn.commit()
        conn.close()
        return True

    @db_write_lock
    def stop_review_plan(self, plan_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_plans SET status = 'stopped', next_review_at = NULL, updated_at = ?
            WHERE id = ? AND status IN ('ready', 'active', 'paused')
        ''', (_now(), plan_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    def get_due_review_plans(self, now_iso: str = None) -> list:
        now_iso = now_iso or _now()
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT p.*, c.title AS channel_title, c.identifier AS channel_identifier,
                   c.username AS channel_username
            FROM review_plans p
            LEFT JOIN review_channels c ON c.id = p.channel_id
            WHERE p.status = 'active' AND p.next_review_at IS NOT NULL
              AND p.next_review_at <= ?
            ORDER BY p.next_review_at ASC
        ''', (now_iso,))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    @db_write_lock
    def claim_review_reminder(self, plan_id: int, cycle_number: int) -> bool:
        """Atomically claim a reminder. Returns False if it was already generated."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO review_reminders
                (plan_id, cycle_number, created_at)
            VALUES (?, ?, ?)
        ''', (plan_id, cycle_number, _now()))
        claimed = cursor.rowcount > 0
        if claimed:
            cursor.execute('''
                INSERT OR IGNORE INTO review_cycles
                    (plan_id, cycle_number, status, started_at)
                VALUES (?, ?, 'reminded', ?)
            ''', (plan_id, cycle_number, _now()))
            cursor.execute('''
                UPDATE review_cycles SET status = 'reminded', reminded_at = ?
                WHERE plan_id = ? AND cycle_number = ?
            ''', (_now(), plan_id, cycle_number))
        conn.commit()
        conn.close()
        return claimed

    @db_write_lock
    def complete_review_reminder(self, plan_id: int, cycle_number: int,
                                 chat_id: int = None, message_id: int = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_reminders
            SET chat_id = ?, message_id = ?, sent_at = ?
            WHERE plan_id = ? AND cycle_number = ?
        ''', (chat_id, message_id, _now(), plan_id, cycle_number))
        conn.commit()
        conn.close()

    @db_write_lock
    def advance_review_plan(self, plan_id: int, cycle_number: int, error: str = None) -> str:
        """Advance to the next cycle or complete the plan after a reminder."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT current_cycle, max_cycles, interval_hours FROM review_plans WHERE id = ?', (plan_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return 'missing'
        current, maximum, interval = row[0], row[1], row[2]
        if current >= maximum:
            cursor.execute('''
                UPDATE review_plans SET status = 'completed', next_review_at = NULL,
                    completed_at = ?, updated_at = ?, last_error = COALESCE(?, last_error)
                WHERE id = ?
            ''', (_now(), _now(), error, plan_id))
            result = 'completed'
        else:
            cursor.execute('''
                UPDATE review_plans SET current_cycle = current_cycle + 1,
                    next_review_at = ?, updated_at = ?, last_error = COALESCE(?, last_error)
                WHERE id = ?
            ''', (_iso_after(interval), _now(), error, plan_id))
            result = 'advanced'
        cursor.execute('''
            UPDATE review_cycles SET status = 'completed', completed_at = ?
            WHERE plan_id = ? AND cycle_number = ?
        ''', (_now(), plan_id, cycle_number))
        conn.commit()
        conn.close()
        return result

    @db_write_lock
    def set_plan_error(self, plan_id: int, error: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' UPDATE review_plans SET last_error = ?, updated_at = ? WHERE id = ?',
                       ((error or '')[:500], _now(), plan_id))
        conn.commit()
        conn.close()

    @db_write_lock
    def mark_item_reviewed(self, item_id: int, account_id: int = None) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_items
            SET status = 'reviewed', reviewed_by_account_id = ?, reviewed_at = ?
            WHERE id = ?
        ''', (account_id, _now(), item_id))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def mark_item_pending(self, item_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE review_items
            SET status = 'pending', reviewed_by_account_id = NULL, reviewed_at = NULL
            WHERE id = ?
        ''', (item_id,))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    # ------------------------------------------------------------------
    # Audit / dashboard / history / exports
    # ------------------------------------------------------------------
    @db_write_lock
    def log_audit(self, user_id: int, action: str, details: str = ""):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO audit_logs (user_id, action, details, created_at)
            VALUES (?, ?, ?, ?)
        ''', (user_id, (action or 'unknown')[:80], (details or '')[:1000], _now()))
        conn.commit()
        conn.close()

    def get_audit_logs(self, offset: int = 0, limit: int = 10, action: str = None) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = ' SELECT * FROM audit_logs'
        params = []
        if action:
            query += ' WHERE action = ?'
            params.append(action)
        query += ' ORDER BY id DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        cursor.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def count_audit_logs(self, action: str = None) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        query = ' SELECT COUNT(*) FROM audit_logs'
        params = []
        if action:
            query += ' WHERE action = ?'
            params.append(action)
        cursor.execute(query, params)
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_review_dashboard_stats(self) -> dict:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(' SELECT COUNT(*), SUM(CASE WHEN COALESCE(enabled,1)=1 THEN 1 ELSE 0 END) FROM accounts')
        session_row = cursor.fetchone() or (0, 0)
        cursor.execute(' SELECT COUNT(*) FROM review_channels')
        channel_count = cursor.fetchone()[0]
        cursor.execute('''
            SELECT COUNT(*) FROM review_items i
            JOIN review_plans p ON p.id = i.plan_id
            WHERE p.status != 'draft' AND i.selected = 1
        ''')
        selected_count = cursor.fetchone()[0]
        cursor.execute(" SELECT COUNT(*) FROM review_plans WHERE status = 'active'")
        active_plans = cursor.fetchone()[0]
        cursor.execute(" SELECT COUNT(*) FROM review_plans WHERE status = 'paused'")
        paused_plans = cursor.fetchone()[0]
        cursor.execute(" SELECT COUNT(*) FROM review_plans WHERE status = 'ready'")
        ready_plans = cursor.fetchone()[0]
        cursor.execute('''
            SELECT id, current_cycle, max_cycles, next_review_at
            FROM review_plans
            WHERE status = 'active' AND next_review_at IS NOT NULL
            ORDER BY next_review_at ASC LIMIT 1
        ''')
        latest = cursor.fetchone()
        if not latest:
            cursor.execute('''
                SELECT id, current_cycle, max_cycles, next_review_at
                FROM review_plans WHERE status != 'draft'
                ORDER BY id DESC LIMIT 1
            ''')
            latest = cursor.fetchone()
        cursor.execute(" SELECT COUNT(*) FROM audit_logs WHERE action = 'error'")
        errors = cursor.fetchone()[0]
        cursor.execute('''
            SELECT
              SUM(CASE WHEN i.status = 'pending' THEN 1 ELSE 0 END),
              SUM(CASE WHEN i.status = 'reviewed' THEN 1 ELSE 0 END)
            FROM review_items i JOIN review_plans p ON p.id = i.plan_id
            WHERE p.status != 'draft' AND i.selected = 1
        ''')
        status_row = cursor.fetchone() or (0, 0)
        conn.close()
        return {
            'sessions_total': session_row[0] or 0,
            'sessions_enabled': session_row[1] or 0,
            'channels': channel_count,
            'selected_messages': selected_count,
            'active_plans': active_plans,
            'paused_plans': paused_plans,
            'ready_plans': ready_plans,
            'current_cycle': latest[1] if latest else 0,
            'max_cycles': latest[2] if latest else 0,
            'next_review_at': latest[3] if latest else None,
            'errors': errors,
            'pending': status_row[0] or 0,
            'reviewed': status_row[1] or 0,
        }

    def search_review_history(self, query: str = None, status: str = None,
                              offset: int = 0, limit: int = 10) -> list:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        sql = '''
            SELECT i.*, p.status AS plan_status, p.current_cycle, p.max_cycles,
                   c.title AS channel_title, c.identifier AS channel_identifier,
                   cat.label AS category_label, sub.label AS subcategory_label,
                   a.phone AS reviewer_phone, a.label AS reviewer_label
            FROM review_items i
            JOIN review_plans p ON p.id = i.plan_id
            LEFT JOIN review_channels c ON c.id = i.channel_id
            LEFT JOIN review_categories cat ON cat.id = i.category_id
            LEFT JOIN review_subcategories sub ON sub.id = i.subcategory_id
            LEFT JOIN accounts a ON a.id = i.reviewed_by_account_id
            WHERE p.status != 'draft' AND i.selected = 1
        '''
        params = []
        if status in ('pending', 'reviewed'):
            sql += ' AND i.status = ?'
            params.append(status)
        if query:
            like = f'%{query}%'
            sql += ''' AND (
                c.title LIKE ? OR c.identifier LIKE ? OR i.message_link LIKE ? OR
                i.message_text LIKE ? OR i.explanation LIKE ? OR cat.label LIKE ? OR sub.label LIKE ?
            )'''
            params.extend([like] * 7)
        sql += ' ORDER BY i.id DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        cursor.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def count_review_history(self, query: str = None, status: str = None) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        sql = '''
            SELECT COUNT(*)
            FROM review_items i
            JOIN review_plans p ON p.id = i.plan_id
            LEFT JOIN review_channels c ON c.id = i.channel_id
            LEFT JOIN review_categories cat ON cat.id = i.category_id
            LEFT JOIN review_subcategories sub ON sub.id = i.subcategory_id
            WHERE p.status != 'draft' AND i.selected = 1
        '''
        params = []
        if status in ('pending', 'reviewed'):
            sql += ' AND i.status = ?'
            params.append(status)
        if query:
            like = f'%{query}%'
            sql += ''' AND (
                c.title LIKE ? OR c.identifier LIKE ? OR i.message_link LIKE ? OR
                i.message_text LIKE ? OR i.explanation LIKE ? OR cat.label LIKE ? OR sub.label LIKE ?
            )'''
            params.extend([like] * 7)
        cursor.execute(sql, params)
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_export_data(self, max_rows: int = 5000) -> dict:
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT p.id, p.status, p.interval_hours, p.max_cycles, p.current_cycle,
                   p.next_review_at, p.created_at, p.started_at, p.completed_at,
                   c.identifier AS channel_identifier, c.title AS channel_title,
                   c.username AS channel_username
            FROM review_plans p LEFT JOIN review_channels c ON c.id = p.channel_id
            WHERE p.status != 'draft' ORDER BY p.id DESC LIMIT ?
        ''', (max_rows,))
        plans = [dict(row) for row in cursor.fetchall()]
        cursor.execute('''
            SELECT i.plan_id, i.message_id, i.message_link, i.message_text,
                   i.status, i.explanation, i.reviewed_at, i.created_at,
                   cat.label AS category, sub.label AS subcategory,
                   a.id AS account_id, a.label AS account_label, a.phone AS account_phone
            FROM review_items i
            JOIN review_plans p ON p.id = i.plan_id
            LEFT JOIN review_categories cat ON cat.id = i.category_id
            LEFT JOIN review_subcategories sub ON sub.id = i.subcategory_id
            LEFT JOIN accounts a ON a.id = i.reviewed_by_account_id
            WHERE p.status != 'draft' AND i.selected = 1
            ORDER BY i.plan_id DESC, i.message_id ASC LIMIT ?
        ''', (max_rows,))
        items = [dict(row) for row in cursor.fetchall()]
        cursor.execute(' SELECT plan_id, cycle_number, status, started_at, reminded_at, completed_at FROM review_cycles ORDER BY id DESC LIMIT ?', (max_rows,))
        cycles = [dict(row) for row in cursor.fetchall()]
        cursor.execute(' SELECT user_id, action, details, created_at FROM audit_logs ORDER BY id DESC LIMIT ?', (max_rows,))
        audit = [dict(row) for row in cursor.fetchall()]
        cursor.execute(" SELECT id, COALESCE(label, '') AS label, phone, status, COALESCE(enabled,1) AS enabled FROM accounts ORDER BY id")
        accounts = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return {
            'plans': plans,
            'items': items,
            'cycles': cycles,
            'audit_logs': audit,
            'accounts': accounts,
        }

    # ------------------------------------------------------------------
    # Reporting campaigns
    # ------------------------------------------------------------------
    @db_write_lock
    def create_report_campaign(self, data: dict) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO report_campaigns
                (name, target, account_phones, messages, reports_per_message,
                 delay_seconds, exec_mode, reason, reason_detail, reason_option,
                 reason_choices, comment, interval_hours, duration_hours, max_cycles,
                 cycle_count, status, next_run_at, ends_at, created_by, created_at,
                 last_run_at, last_result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL, NULL)
        ''', (
            data.get('name') or '',
            data['target'],
            json.dumps(data.get('account_phones') or []),
            json.dumps(data.get('messages') or []),
            int(data.get('reports_per_message') or 2),
            int(data.get('delay_seconds') or 5),
            data.get('exec_mode') or 'safe',
            data.get('reason') or 'spam',
            data.get('reason_detail') or '',
            data.get('reason_option') or '',
            json.dumps(data.get('reason_choices') or []),
            data.get('comment') or '',
            float(data.get('interval_hours') or 0),
            float(data.get('duration_hours') or 0),
            (
                1 if data.get('max_cycles') is None
                else int(data.get('max_cycles') or 0)
            ),
            data.get('status') or 'ready',
            data.get('next_run_at'),
            data.get('ends_at'),
            data.get('created_by'),
            _now(),
        ))
        campaign_id = cursor.lastrowid
        conn.commit()
        conn.close()
        doc = dict(data)
        doc['id'] = campaign_id
        doc['created_at'] = _now()
        self._mongo_set("report_campaigns", {"id": campaign_id}, doc)
        return campaign_id

    def _campaign_from_row(self, row) -> dict:
        campaign = dict(row)
        for field in ('account_phones', 'messages', 'reason_choices'):
            try:
                campaign[field] = json.loads(campaign.get(field) or '[]')
            except (TypeError, ValueError):
                campaign[field] = []
        return campaign

    def get_report_campaign(self, campaign_id: int):
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(' SELECT * FROM report_campaigns WHERE id = ?', (campaign_id,))
        row = cursor.fetchone()
        conn.close()
        return self._campaign_from_row(row) if row else None

    def list_report_campaigns(self, created_by: int = None, limit: int = 20, offset: int = 0) -> list:
        try:
            limit = max(1, min(int(limit), 100))
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            limit, offset = 20, 0
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if created_by is None:
            cursor.execute(' SELECT * FROM report_campaigns ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))
        else:
            cursor.execute(' SELECT * FROM report_campaigns WHERE created_by = ? ORDER BY id DESC LIMIT ? OFFSET ?',
                           (created_by, limit, offset))
        rows = [self._campaign_from_row(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def count_report_campaigns(self, created_by: int = None) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        if created_by is None:
            cursor.execute(' SELECT COUNT(*) FROM report_campaigns')
        else:
            cursor.execute(' SELECT COUNT(*) FROM report_campaigns WHERE created_by = ?', (created_by,))
        count = cursor.fetchone()[0]
        conn.close()
        return count

    @db_write_lock
    def update_report_campaign(self, campaign_id: int, **fields) -> bool:
        allowed = {
            'name', 'target', 'account_phones', 'messages', 'reports_per_message',
            'delay_seconds', 'exec_mode', 'reason', 'reason_detail',
            'reason_option', 'reason_choices', 'comment', 'interval_hours', 'duration_hours',
            'max_cycles', 'cycle_count',
            'status', 'next_run_at', 'ends_at', 'last_run_at', 'last_result',
        }
        updates = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ('account_phones', 'messages', 'reason_choices'):
                value = json.dumps(value or [])
            updates.append(f"{key} = ?")
            values.append(value)
        if not updates:
            return False
        values.append(campaign_id)
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(f" UPDATE report_campaigns SET {', '.join(updates)} WHERE id = ?", values)
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        self._mongo_set("report_campaigns", {"id": campaign_id}, fields)
        return changed

    @db_write_lock
    def claim_report_campaign(self, campaign_id: int, now: str = None) -> bool:
        now = now or _now()
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE report_campaigns
            SET status = 'running'
            WHERE id = ? AND status IN ('ready', 'active')
              AND (ends_at IS NULL OR ends_at > ?)
        ''', (campaign_id, now))
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        if changed:
            self._mongo_set("report_campaigns", {"id": campaign_id}, {"status": "running", "last_run_at": now})
        return changed

    def get_due_report_campaigns(self, now: str = None, limit: int = 10) -> list:
        now = now or _now()
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM report_campaigns
            WHERE status IN ('ready', 'active')
              AND next_run_at IS NOT NULL
              AND next_run_at <= ?
              AND (ends_at IS NULL OR ends_at > ?)
            ORDER BY next_run_at ASC, id ASC
            LIMIT ?
        ''', (now, now, max(1, int(limit))))
        rows = [self._campaign_from_row(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    @db_write_lock
    def expire_report_campaigns(self, now: str = None) -> int:
        """Complete campaigns whose selected run duration has ended."""
        now = now or _now()
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE report_campaigns
            SET status = 'completed',
                next_run_at = NULL,
                last_result = 'Duration reached; campaign completed.'
            WHERE status IN ('ready', 'active', 'paused')
              AND ends_at IS NOT NULL
              AND ends_at <= ?
        ''', (now,))
        changed = cursor.rowcount
        conn.commit()
        conn.close()
        return changed

    @db_write_lock
    def reset_interrupted_campaigns(self) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, interval_hours FROM report_campaigns WHERE status = 'running'")
        rows = cursor.fetchall()
        now = datetime.now()
        for r in rows:
            cid, interval_h = r[0], r[1]
            if interval_h and float(interval_h) > 0:
                next_time = (now + timedelta(hours=float(interval_h))).isoformat(timespec="seconds")
            else:
                next_time = _now()
            cursor.execute('''
                UPDATE report_campaigns
                SET status = 'active', next_run_at = ?
                WHERE id = ?
            ''', (next_time, cid))
            self._mongo_set("report_campaigns", {"id": cid}, {"status": "active", "next_run_at": next_time})
        changed = len(rows)
        conn.commit()
        conn.close()
        return changed

    def finish_report_campaign_cycle(self, campaign_id: int, cycle_count: int,
                                     status: str, next_run_at: str,
                                     last_result: str) -> bool:
        return self.update_report_campaign(
            campaign_id,
            cycle_count=cycle_count,
            status=status,
            next_run_at=next_run_at,
            last_run_at=_now(),
            last_result=last_result,
        )
