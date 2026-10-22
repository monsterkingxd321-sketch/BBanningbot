"""Background scheduler for automatic reporting campaigns."""

import asyncio
from datetime import datetime, timedelta

from bot import active_campaigns
from config import CAMPAIGN_TICK_SECONDS, get_now
from utils.helpers import ReportEngine, log_activity
from utils.review_utils import format_dt, parse_iso, redact_sensitive


class CampaignScheduler:
    """Find due campaigns and run one cycle for each without blocking others."""

    def __init__(self, bot_client, database):
        self.bot = bot_client
        self.db = database
        self._task = None
        self._stop_event = asyncio.Event()
        self._tasks = set()

    def start(self):
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        log_activity("Campaign scheduler started")

    async def stop(self):
        self._stop_event.set()
        if self._task:
            self._task.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        log_activity("Campaign scheduler stopped")

    async def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception as exc:
                log_activity(f"Campaign scheduler tick error: {redact_sensitive(exc, 300)}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=CAMPAIGN_TICK_SECONDS
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def tick(self):
        if hasattr(self.db, "sync_campaigns_from_mongo"):
            try:
                self.db.sync_campaigns_from_mongo()
            except Exception as exc:
                log_activity(f"Campaign scheduler Mongo sync error: {redact_sensitive(exc, 200)}")
        expired = self.db.expire_report_campaigns()
        if expired:
            log_activity(f"Campaign scheduler completed {expired} campaign(s) at their deadline")
        for campaign in self.db.get_due_report_campaigns(limit=10):
            campaign_id = campaign.get("id")
            if campaign_id in active_campaigns:
                continue
            if not self.db.claim_report_campaign(campaign_id):
                continue
            active_campaigns[campaign_id] = None
            task = asyncio.create_task(self.run_campaign(campaign))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def run_campaign(self, campaign: dict):
        campaign_id = campaign.get("id")
        creator = campaign.get("created_by")
        progress_message = None
        try:
            if not creator:
                raise RuntimeError("Campaign has no creator to notify.")

            current = self.db.get_report_campaign(campaign_id)
            deadline = parse_iso((current or campaign).get("ends_at"))
            if deadline and deadline <= get_now():
                self.db.update_report_campaign(
                    campaign_id,
                    status="completed",
                    next_run_at=None,
                    last_result="Duration reached; campaign completed.",
                )
                log_activity(f"Campaign #{campaign_id} reached its duration before the cycle started")
                return

            active_accounts = self.db.get_active_accounts()
            selected_phones = list(campaign.get("account_phones") or [])
            if selected_phones:
                active_accounts = [
                    account for account in active_accounts
                    if account[1] in set(selected_phones)
                ]
            if not active_accounts:
                raise RuntimeError(
                    "No active accounts are available for this campaign. "
                    "Activate or import an account, then resume the campaign."
                )

            try:
                progress_message = await self.bot.send_message(
                    creator,
                    "**Campaign Started**\n\n"
                    f"Campaign #{campaign_id}: {campaign.get('name') or 'Untitled'}\n"
                    f"Target: `{campaign.get('target')}`\n"
                    f"Links: {len(campaign.get('messages') or [])}\n"
                    f"Accounts: {len(active_accounts)}\n"
                    f"Reports per link: {campaign.get('reports_per_message')}\n"
                    f"Report types: {len(campaign.get('reason_choices') or []) or 1}\n"
                    f"Cycle: {(campaign.get('cycle_count') or 0) + 1}"
                    + (
                        f"/{campaign.get('max_cycles')}"
                        if campaign.get("max_cycles")
                        else "/unlimited"
                    ),
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Could not send the campaign progress message: "
                    f"{redact_sensitive(exc, 200)}"
                )

            engine = ReportEngine(creator)
            active_campaigns[campaign_id] = engine

            # A pause or stop may arrive between claiming the campaign and
            # creating the engine. Re-check before the first report.
            current = self.db.get_report_campaign(campaign_id)
            if current and current.get("status") not in ("running", "active"):
                engine.should_stop = True

            allowed = selected_phones or None
            dist_mode = campaign.get("dist_mode") or "all"
            account_rules = campaign.get("account_rules") or {}
            if campaign.get("exec_mode") == "turbo":
                result = await engine.run_parallel_report_session(
                    campaign.get("target"),
                    list(campaign.get("messages") or []),
                    int(campaign.get("reports_per_message") or 2),
                    campaign.get("reason") or "spam",
                    campaign.get("comment") or "",
                    progress_message,
                    allowed_phones=allowed,
                    reason_option=campaign.get("reason_option") or "",
                    reason_choices=campaign.get("reason_choices") or [],
                    dist_mode=dist_mode,
                    account_rules=account_rules,
                )
            else:
                result = await engine.run_report_session(
                    campaign.get("target"),
                    list(campaign.get("messages") or []),
                    int(campaign.get("reports_per_message") or 2),
                    int(campaign.get("delay_seconds") or 5),
                    campaign.get("reason") or "spam",
                    campaign.get("comment") or "",
                    progress_message,
                    allowed_phones=allowed,
                    reason_option=campaign.get("reason_option") or "",
                    reason_choices=campaign.get("reason_choices") or [],
                    dist_mode=dist_mode,
                    account_rules=account_rules,
                )
            result = result or {}

            fresh = self.db.get_report_campaign(campaign_id) or campaign
            now = get_now()
            deadline = parse_iso(fresh.get("ends_at"))
            duration_reached = bool(deadline and deadline <= now)
            cycle_count = int(fresh.get("cycle_count") or 0) + 1
            if fresh.get("status") in ("paused", "stopped"):
                final_status = fresh.get("status")
                next_run_at = None
                duration_reached = False
            elif duration_reached:
                final_status = "completed"
                next_run_at = None
            else:
                max_cycles = int(fresh.get("max_cycles") or 0)
                interval_hours = float(fresh.get("interval_hours") or 0)
                if max_cycles and cycle_count >= max_cycles:
                    final_status = "completed"
                    next_run_at = None
                elif interval_hours > 0:
                    next_dt = now + timedelta(hours=interval_hours)
                    if deadline and next_dt >= deadline:
                        final_status = "completed"
                        next_run_at = None
                        duration_reached = True
                    else:
                        final_status = "active"
                        next_run_at = next_dt.isoformat(timespec="seconds")
                else:
                    final_status = "completed"
                    next_run_at = None

            result_text = (
                f"Cycle {cycle_count}: {result.get('success', 0)} successful, "
                f"{result.get('failed', 0)} failed, "
                f"{result.get('completed', 0)}/{result.get('total', 0)} attempted."
            )
            if duration_reached:
                result_text += " Duration reached; campaign completed."
            self.db.finish_report_campaign_cycle(
                campaign_id, cycle_count, final_status, next_run_at, result_text
            )
            log_activity(f"Campaign #{campaign_id} cycle {cycle_count}: {result_text}")

            next_text = (
                f"Next run: {format_dt(next_run_at)}"
                if next_run_at
                else "No further runs are scheduled."
            )
            try:
                await progress_message.edit(
                    "**Campaign Cycle Complete**\n\n"
                    f"Campaign #{campaign_id}: {campaign.get('name') or 'Untitled'}\n"
                    f"Status: {final_status}\n"
                    f"{result_text}\n"
                    f"{next_text}"
                )
            except Exception:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = redact_sensitive(exc, 300)
            log_activity(f"Campaign #{campaign_id} failed: {error}")
            try:
                self.db.update_report_campaign(
                    campaign_id,
                    status="paused",
                    next_run_at=None,
                    last_result=f"Error: {error}",
                )
            except Exception:
                pass
            if progress_message is not None:
                try:
                    await progress_message.edit(
                        "**Campaign Paused After an Error**\n\n"
                        f"Campaign #{campaign_id}\n"
                        f"`{error}`\n\n"
                        "Fix the problem, then resume the campaign."
                    )
                except Exception:
                    pass
            else:
                try:
                    await self.bot.send_message(
                        creator,
                        "**Campaign Paused After an Error**\n\n"
                        f"Campaign #{campaign_id}\n"
                        f"`{error}`",
                    )
                except Exception:
                    pass
        finally:
            active_campaigns.pop(campaign_id, None)
