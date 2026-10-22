import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests never use real credentials or the network.
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ["MONGO_URI"] = ""

from database import Database
from utils.review_scheduler import ReviewScheduler
from utils.review_utils import (
    SUPPORTED_REASON_KEYS,
    build_report_reason,
    parse_channel_identifier,
)


class ReviewUtilityTests(unittest.TestCase):
    def test_channel_parser(self):
        self.assertEqual(parse_channel_identifier("@Example_Channel"), "Example_Channel")
        self.assertEqual(parse_channel_identifier("https://t.me/Example_Channel"), "Example_Channel")
        self.assertEqual(parse_channel_identifier("t.me/Example_Channel/123"), "Example_Channel")
        with self.assertRaises(ValueError):
            parse_channel_identifier("https://t.me/+privateInvite")
        with self.assertRaises(ValueError):
            parse_channel_identifier("not a username!")

    def test_message_link_parser(self):
        from utils.helpers import parse_message_link

        self.assertEqual(
            parse_message_link("https://t.me/example/123"), ("example", 123)
        )
        self.assertEqual(
            parse_message_link("https://t.me/c/123456789/77"),
            (-100123456789, 77),
        )
        self.assertEqual(
            parse_message_link("tg://privatepost?channel=123456789&post=88"),
            (-100123456789, 88),
        )
        with self.assertRaises(ValueError):
            parse_message_link("not a message link")

    def test_every_category_reason_is_a_real_telethon_type(self):
        self.assertIn("fake", SUPPORTED_REASON_KEYS)
        for key in SUPPORTED_REASON_KEYS:
            reason = build_report_reason(key)
            self.assertIsNotNone(reason)
            self.assertEqual(reason.__class__.__name__.startswith("InputReportReason"), True)

    def test_button_data_is_short_and_bytes(self):
        from handlers.report_ui import (
            count_buttons,
            delay_buttons,
            message_reason_buttons,
            peer_reason_buttons,
            REPORT_REASON_CHOICES,
        )

        rows = count_buttons() + delay_buttons() + message_reason_buttons() + peer_reason_buttons()
        for row in rows:
            for button in row:
                data = button.type.data
                self.assertIsInstance(data, bytes)
                self.assertLessEqual(len(data), 64)
        self.assertEqual(
            {key for key, _, _ in REPORT_REASON_CHOICES},
            set(SUPPORTED_REASON_KEYS),
        )
        self.assertEqual(len(REPORT_REASON_CHOICES), 10)

    def test_scraped_post_filter_ignores_service_and_album_duplicates(self):
        from utils.message_utils import is_reportable_post

        class Message:
            def __init__(self, message_id, text="", media=None, action=None, grouped_id=None):
                self.id = message_id
                self.message = text
                self.media = media
                self.action = action
                self.grouped_id = grouped_id

        grouped_seen = set()
        self.assertFalse(is_reportable_post(Message(1, action="service"), grouped_seen))
        self.assertFalse(is_reportable_post(Message(2), grouped_seen))
        self.assertTrue(is_reportable_post(Message(3, text="post"), grouped_seen))
        self.assertTrue(is_reportable_post(Message(4, media=object(), grouped_id=88), grouped_seen))
        self.assertFalse(is_reportable_post(Message(5, media=object(), grouped_id=88), grouped_seen))
        self.assertTrue(is_reportable_post(Message(6, media=object(), grouped_id=99), grouped_seen))

    def test_report_engine_follows_real_telegram_report_results(self):
        import asyncio

        from telethon import types

        from utils.helpers import ReportEngine

        class FakeClient:
            def __init__(self, results):
                self.results = list(results)
                self.calls = []

            async def get_entity(self, reference):
                return object()

            async def __call__(self, request):
                self.calls.append(request)
                return self.results.pop(0)

        reported = types.ReportResultReported()
        client = FakeClient([reported])
        engine = ReportEngine(1)
        self.assertTrue(asyncio.run(engine.report_message(
            client, "example", "https://t.me/example/1", "Fake Account",
            "comment", "00"
        )))
        self.assertEqual(client.calls[0].option, b"\x00")

        add_comment = types.ReportResultAddComment(b"\x02", False)
        client = FakeClient([add_comment, reported])
        self.assertTrue(asyncio.run(engine.report_message(
            client, "example", "https://t.me/example/1", "Fake Account",
            "required comment", "00"
        )))
        self.assertEqual(client.calls[1].option, b"\x02")
        self.assertEqual(client.calls[1].message, "required comment")

        choose = types.ReportResultChooseOption(
            "Report",
            [types.MessageReportOption("Scam", b"\x03")],
        )
        client = FakeClient([choose])
        self.assertFalse(asyncio.run(engine.report_message(
            client, "example", "https://t.me/example/1", "Fake Account",
            "", "00"
        )))

    def test_reason_labels_match_telegram_and_options_are_server_driven(self):
        from handlers.report_ui import (
            REPORT_REASON_CHOICES,
            message_reason_buttons,
            peer_reason_buttons,
            reason_display,
            reason_key_from_label,
            report_option_buttons,
        )

        labels = {key: label for key, label, _ in REPORT_REASON_CHOICES}
        self.assertEqual(labels["fake"], "Fake Account")
        self.assertEqual(labels["child_abuse"], "Child Abuse")
        self.assertEqual(labels["illegal_drugs"], "Illegal Drugs")
        self.assertEqual(labels["personal_details"], "Personal Details")
        self.assertEqual(labels["geo_irrelevant"], "Geo Irrelevant")
        self.assertEqual(reason_key_from_label("Fake Account"), "fake")
        self.assertEqual(reason_display("fake"), "Fake Account")
        self.assertEqual(reason_display("fake", "Scam"), "Fake Account - Scam")

        # Sub-options are whatever Telegram returns at runtime, not a local list.
        options = [
            {"text": "Scam", "option": "00"},
            {"text": "Impersonation", "option": "01"},
        ]
        rows = report_option_buttons(options)
        callback_data = [row[0].type.data for row in rows if row]
        self.assertTrue(callback_data)
        for row in rows:
            for button in row:
                self.assertIsInstance(button.type.data, bytes)
                self.assertLessEqual(len(button.type.data), 64)

        for row in message_reason_buttons() + peer_reason_buttons():
            for button in row:
                self.assertIsInstance(button.type.data, bytes)
                self.assertLessEqual(len(button.type.data), 64)


class ReviewDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "test.db")
        self.db = Database(self.db_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def _configured_plan(self, interval=2, cycles=5):
        self.assertTrue(self.db.add_account_record(
            "+15551234567", os.path.join(self.tempdir.name, "sessions", "a.session"),
            added_by=1, session_user_id=1001, file_hash="hash-a", label="tester"
        ))
        account = self.db.get_account_records()[0]
        category = self.db.get_category_by_key("scam_fraud")
        subcategory = self.db.get_subcategories(category["id"])[0]
        channel_id = self.db.upsert_review_channel(
            "review_channel", telegram_id=-100123, title="Review Channel",
            username="review_channel", added_by=1
        )
        plan_id = self.db.create_review_draft(1, channel_id, interval, cycles)
        posts = [
            {"message_id": 10, "message_text": "message ten", "message_link": "https://t.me/review_channel/10"},
            {"message_id": 11, "message_text": "message eleven", "message_link": "https://t.me/review_channel/11"},
            # Duplicate candidate must be ignored by the unique constraint.
            {"message_id": 10, "message_text": "duplicate", "message_link": "https://t.me/review_channel/10"},
        ]
        self.assertEqual(self.db.add_review_candidates(plan_id, channel_id, posts), 2)
        for item in self.db.get_review_candidates(plan_id):
            self.db.toggle_review_candidate(item["id"], plan_id)
        self.db.remove_unselected_candidates(plan_id)
        self.db.activate_selected_candidates(plan_id)
        for item in self.db.get_plan_items(plan_id, selected_only=True):
            self.db.set_item_category(item["id"], category["id"])
            self.db.set_item_subcategory(item["id"], subcategory["id"])
            self.db.set_item_explanation(item["id"], "manual explanation", configured=True)
        self.assertTrue(self.db.finalize_review_plan(plan_id))
        return plan_id, account

    def test_backward_compatible_migration_preserves_legacy_account(self):
        legacy_path = os.path.join(self.tempdir.name, "legacy.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute('''
            CREATE TABLE accounts (
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
        conn.execute(
            " INSERT INTO accounts (phone, session_file, status, added_date, added_by) "
            " VALUES (?, ?, 'active', '2024-01-01T00:00:00', 1)",
            ("+15550000001", "sessions/legacy.session")
        )
        conn.commit()
        conn.close()

        migrated = Database(legacy_path)
        records = migrated.get_account_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["phone"], "+15550000001")
        self.assertEqual(records[0]["enabled"], 1)
        self.assertTrue(migrated.get_review_categories())

    def test_plan_lifecycle_and_duplicate_reminders(self):
        plan_id, account = self._configured_plan(interval=2, cycles=5)
        self.assertTrue(self.db.start_review_plan(plan_id))
        plan = self.db.get_review_plan(plan_id)
        self.assertEqual(plan["status"], "active")
        self.assertEqual(plan["current_cycle"], 1)
        self.assertIsNotNone(plan["next_review_at"])

        self.assertTrue(self.db.claim_review_reminder(plan_id, 1))
        self.assertFalse(self.db.claim_review_reminder(plan_id, 1))
        self.db.complete_review_reminder(plan_id, 1, 111, 222)
        self.db.advance_review_plan(plan_id, 1)
        self.assertEqual(self.db.get_review_plan(plan_id)["current_cycle"], 2)

        for cycle in range(2, 6):
            self.assertTrue(self.db.claim_review_reminder(plan_id, cycle))
            self.db.advance_review_plan(plan_id, cycle)
        plan = self.db.get_review_plan(plan_id)
        self.assertEqual(plan["status"], "completed")
        self.assertEqual(plan["current_cycle"], 5)

        item = self.db.get_plan_items(plan_id, selected_only=True)[0]
        self.assertTrue(self.db.mark_item_reviewed(item["id"], account["id"]))
        stored = self.db.get_review_item(item["id"])
        self.assertEqual(stored["reviewed_by_account_id"], account["id"])
        self.assertEqual(self.db.count_plan_items(plan_id, status="reviewed"), 1)
        self.assertEqual(self.db.count_review_history(query="manual explanation"), 2)

    def test_pause_resume_stop_and_enable_disable(self):
        plan_id, account = self._configured_plan()
        self.assertTrue(self.db.start_review_plan(plan_id))
        self.assertTrue(self.db.pause_review_plan(plan_id))
        self.assertEqual(self.db.get_review_plan(plan_id)["status"], "paused")
        self.assertTrue(self.db.resume_review_plan(plan_id))
        self.assertEqual(self.db.get_review_plan(plan_id)["status"], "active")
        self.assertTrue(self.db.stop_review_plan(plan_id))
        self.assertEqual(self.db.get_review_plan(plan_id)["status"], "stopped")

        self.assertTrue(self.db.toggle_account_enabled(account["id"], False))
        self.assertEqual(self.db.get_active_accounts(), [])
        self.assertTrue(self.db.toggle_account_enabled(account["id"], True))
        self.assertEqual(len(self.db.get_active_accounts()), 1)

    def test_export_omits_session_files(self):
        self._configured_plan()
        data = self.db.get_export_data()
        self.assertTrue(data["plans"])
        self.assertTrue(data["items"])
        self.assertTrue(data["accounts"])
        self.assertNotIn("session_file", data["accounts"][0])

    def test_recent_review_channels_for_report_shortcuts(self):
        first = self.db.upsert_review_channel("first", title="First", added_by=1)
        second = self.db.upsert_review_channel("second", title="Second", added_by=1)
        conn = self.db.get_connection()
        conn.execute(" UPDATE review_channels SET last_used = '2024-01-01T00:00:00' WHERE id = ?", (first,))
        conn.execute(" UPDATE review_channels SET last_used = '2024-02-01T00:00:00' WHERE id = ?", (second,))
        conn.commit()
        conn.close()
        channels = self.db.get_recent_review_channels(2)
        self.assertEqual([channel["identifier"] for channel in channels], ["second", "first"])


class CampaignDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tempdir.name, "campaign.db"))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_campaign_lifecycle_and_due_claim(self):
        campaign_id = self.db.create_report_campaign({
            "name": "Test campaign",
            "target": "@example",
            "account_phones": ["+15551234567"],
            "messages": ["https://t.me/example/1", "https://t.me/example/2"],
            "reports_per_message": 2,
            "delay_seconds": 5,
            "reason": "spam",
            "comment": "test",
            "interval_hours": 2,
            "max_cycles": 5,
            "created_by": 1,
        })
        campaign = self.db.get_report_campaign(campaign_id)
        self.assertEqual(campaign["status"], "ready")
        self.assertIsNone(campaign["next_run_at"])
        self.assertEqual(campaign["messages"][0], "https://t.me/example/1")
        self.assertEqual(campaign["account_phones"], ["+15551234567"])
        self.assertEqual(self.db.count_report_campaigns(created_by=1), 1)
        self.assertEqual(self.db.get_due_report_campaigns(), [])

        self.assertTrue(self.db.claim_report_campaign(campaign_id))
        self.assertFalse(self.db.claim_report_campaign(campaign_id))
        self.assertEqual(self.db.get_report_campaign(campaign_id)["status"], "running")

        self.assertEqual(self.db.reset_interrupted_campaigns(), 1)
        campaign = self.db.get_report_campaign(campaign_id)
        self.assertEqual(campaign["status"], "active")
        self.assertIsNotNone(campaign["next_run_at"])

        self.db.update_report_campaign(
            campaign_id,
            next_run_at="2000-01-01T00:00:00",
        )
        due = self.db.get_due_report_campaigns()
        self.assertEqual([item["id"] for item in due], [campaign_id])

        self.db.finish_report_campaign_cycle(
            campaign_id, 1, "completed", None, "Cycle 1: 4 successful"
        )
        campaign = self.db.get_report_campaign(campaign_id)
        self.assertEqual(campaign["status"], "completed")
        self.assertEqual(campaign["cycle_count"], 1)
        self.assertEqual(campaign["last_result"], "Cycle 1: 4 successful")
        self.assertIsNone(campaign["next_run_at"])

    def test_campaign_reason_detail_duration_and_deadline(self):
        ends_at = (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds")
        campaign_id = self.db.create_report_campaign({
            "name": "Duration campaign",
            "target": "@example",
            "account_phones": [],
            "messages": ["https://t.me/example/1"],
            "reason": "fake",
            "reason_detail": "Impersonation",
            "reason_option": "00",
            "interval_hours": 2,
            "duration_hours": 48,
            "max_cycles": 0,
            "ends_at": ends_at,
            "created_by": 1,
        })
        campaign = self.db.get_report_campaign(campaign_id)
        self.assertEqual(campaign["reason_detail"], "Impersonation")
        self.assertEqual(campaign["reason_option"], "00")
        self.assertEqual(campaign["duration_hours"], 48)
        self.assertEqual(campaign["max_cycles"], 0)
        self.assertEqual(campaign["ends_at"], ends_at)

        past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
        self.db.update_report_campaign(
            campaign_id, status="active", next_run_at=past, ends_at=past
        )
        self.assertFalse(self.db.claim_report_campaign(campaign_id))
        self.assertEqual(self.db.get_due_report_campaigns(), [])
        self.assertEqual(self.db.expire_report_campaigns(), 1)
        campaign = self.db.get_report_campaign(campaign_id)
        self.assertEqual(campaign["status"], "completed")
        self.assertIsNone(campaign["next_run_at"])
        self.assertIn("Duration reached", campaign["last_result"])

    def test_campaign_audit_log_is_not_deadlocked(self):
        campaign_id = self.db.create_report_campaign({
            "name": "Audit",
            "target": "@example",
            "account_phones": [],
            "messages": ["https://t.me/example/1"],
            "created_by": 1,
        })
        self.assertGreater(campaign_id, 0)
        self.assertEqual(self.db.get_report_campaign(campaign_id)["name"], "Audit")


class _FakeMessage:
    chat_id = 999
    id = 1234


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return _FakeMessage()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_sends_one_reminder_per_cycle(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db = Database(os.path.join(tempdir, "test.db"))
            db.add_account_record("+15551234567", "sessions/a.session", 1, 1001, "hash")
            category = db.get_category_by_key("impersonation")
            sub = db.get_subcategories(category["id"])[0]
            channel_id = db.upsert_review_channel("sched", title="Sched", username="sched", added_by=1)
            plan_id = db.create_review_draft(1, channel_id, 2, 5)
            db.add_review_candidates(plan_id, channel_id, [
                {"message_id": 1, "message_text": "post", "message_link": "https://t.me/sched/1"}
            ])
            item = db.get_review_candidates(plan_id)[0]
            db.toggle_review_candidate(item["id"], plan_id)
            db.activate_selected_candidates(plan_id)
            db.set_item_category(item["id"], category["id"])
            db.set_item_subcategory(item["id"], sub["id"])
            db.set_item_explanation(item["id"], "", configured=True)
            db.finalize_review_plan(plan_id)
            db.start_review_plan(plan_id)

            conn = db.get_connection()
            conn.execute(" UPDATE review_plans SET next_review_at = '2000-01-01T00:00:00' WHERE id = ?", (plan_id,))
            conn.commit()
            conn.close()

            bot = _FakeBot()
            scheduler = ReviewScheduler(bot, db, interval_seconds=999)
            await scheduler.tick()
            self.assertEqual(len(bot.sent), 1)
            self.assertEqual(db.count_audit_logs(action="reminder_sent"), 1)
            self.assertEqual(db.get_review_plan(plan_id)["current_cycle"], 2)

            # No duplicate reminder: next date is in the future and the cycle was claimed.
            await scheduler.tick()
            self.assertEqual(len(bot.sent), 1)

    async def test_campaign_scheduler_due_detection(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from config import get_now
            from utils.campaign_scheduler import CampaignScheduler
            from utils.review_utils import parse_iso
            db = Database(os.path.join(tempdir, "test.db"))
            cid = db.create_report_campaign({
                "name": "Test due",
                "target": "https://t.me/testchannel",
                "created_by": 1,
                "interval_hours": 2.0,
                "duration_hours": 24.0,
            })
            # When created, next_run_at is set to now (or past)
            past_time = (get_now() - timedelta(minutes=10)).isoformat(timespec="seconds")
            db.update_report_campaign(cid, status="active", next_run_at=past_time)

            due = db.get_due_report_campaigns()
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["id"], cid)

            # Test parse_iso with aware string
            aware_str = "2026-10-22T16:54:58+00:00"
            parsed = parse_iso(aware_str)
            self.assertIsNotNone(parsed)
            self.assertIsNone(parsed.tzinfo)  # Must be naive converted to BOT_TZ


if __name__ == "__main__":
    unittest.main()
