"""Review reminder scheduler.

This background task only sends reminders and advances cycle state. It never
submits Telegram reports and never coordinates repeated reports between
accounts.
"""

import asyncio

from config import REVIEW_SCHEDULER_TICK_SECONDS
from utils.review_utils import format_dt, redact_sensitive


class ReviewScheduler:
    def __init__(self, bot, db, interval_seconds: int = None):
        self.bot = bot
        self.db = db
        self.interval_seconds = interval_seconds or REVIEW_SCHEDULER_TICK_SECONDS
        self._task = None
        self._running = False

    def start(self):
        if self._task and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._run(), name="review-reminder-scheduler")
        return self._task

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    self.db.log_audit(None, "error", f"Scheduler error: {redact_sensitive(exc)}")
                except Exception:
                    pass
            await asyncio.sleep(self.interval_seconds)

    async def tick(self):
        due_plans = self.db.get_due_review_plans()
        for plan in due_plans:
            plan_id = plan["id"]
            cycle = int(plan.get("current_cycle") or 0)
            if cycle < 1:
                continue
            # Atomic duplicate prevention: one reminder per plan/cycle.
            if not self.db.claim_review_reminder(plan_id, cycle):
                continue

            text = self._build_reminder(plan, cycle)
            error = None
            message = None
            try:
                message = await self.bot.send_message(
                    plan["created_by"], text, link_preview=False
                )
                self.db.complete_review_reminder(
                    plan_id, cycle,
                    chat_id=getattr(message, "chat_id", plan["created_by"]),
                    message_id=getattr(message, "id", None),
                )
                self.db.log_audit(
                    plan["created_by"], "reminder_sent",
                    f"Plan #{plan_id}, cycle {cycle}/{plan.get('max_cycles')}"
                )
            except Exception as exc:
                error = redact_sensitive(exc)
                self.db.log_audit(
                    plan["created_by"], "error",
                    f"Reminder failed for plan #{plan_id}, cycle {cycle}: {error}"
                )
                self.db.set_plan_error(plan_id, f"Reminder failed: {error}")

            self.db.advance_review_plan(plan_id, cycle, error=error)

    def _build_reminder(self, plan: dict, cycle: int) -> str:
        pending = self.db.count_plan_items(plan["id"], status="pending")
        reviewed = self.db.count_plan_items(plan["id"], status="reviewed")
        channel = plan.get("channel_title") or plan.get("channel_identifier") or "channel"
        identifier = plan.get("channel_username") or plan.get("channel_identifier") or ""
        handle = f"@{identifier}"if identifier and not str(identifier).startswith("@") else identifier
        maximum = plan.get("max_cycles") or 0
        next_at = plan.get("next_review_at")
        return (
            "Review reminder\n\n"
            f"Plan: #{plan['id']} - {channel} {handle}\n"
            f"Cycle: {cycle}/{maximum}\n"
            f"Pending items: {pending}\n"
            f"Reviewed items: {reviewed}\n"
            f"Scheduled at: {format_dt(next_at)}\n\n"
            "This is a reminder only. No reports were submitted automatically. "
            "Open /review to update review status or control the plan."
        )
