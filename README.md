# BBanningbot - Telegram Reporter, Campaigns + Review Manager

This is the existing Python/Telethon bot extended **in place** with a configurable
multi-account review manager. Existing reporting, account login, proxy, admin and
statistics features are preserved. Database migrations are additive.

The project code lives in `ben/`. Run commands from that directory unless stated
otherwise.

## What was added

- Phone login with OTP and 2FA (existing flow hardened; temporary login state is
  always cleaned up).
- Session import from one `.session` file or a ZIP of multiple `.session` files.
  Files are validated against Telegram, previewed, checked for duplicates, and
  only imported after confirmation. Imports are admin-only by default.
- Session list with connection status, last check time, and per-session
  enable/disable controls.
- Channel resolution plus recent-post listing, and selecting one, several, or all
  fetched posts.
- Five configurable category labels mapped to Telegram's real report reasons,
  with configurable subcategories, optional per-message explanations, and reusable
  presets.
- Per-message settings stored independently. A plan can use a different category
  and subcategory for every message; each individual Telegram report still carries
  one reason, because that is what Telegram's API accepts.
- Review plans with configurable reminder interval (2 hours is a one-tap option),
  configurable maximum cycles (5 is a one-tap option), start/pause/resume/stop,
  next-reminder time, cycle counter, pending/reviewed status, and database-level
  duplicate prevention.
- Dashboard, plan history, search, filters, pagination, audit logs, error history,
  and CSV/JSON exports.
- Manual attribution of reviewed items to a specific session/account.
- Button-driven main menu, login presets, report count/delay/mode/reason pickers,
  saved-channel shortcuts and consistent back/home navigation.
- Reporting preflight joins every active account to the selected target before any
  report is sent. Accounts that fail to join are skipped and listed.
- Admin-only reporting campaigns that select one account or several accounts, fetch
  every available post link from a public channel or joinable invite, send two
  reports per link by default, and repeat on a configurable interval such as every
  two hours.
- A campaign stores the target, message links, account selection, reports per link,
  delay, the exact Telegram reason option, any real sub-option returned by Telegram,
  optional comment, repeat interval and total run duration such as 1 day or 2 days.
  A separate campaign scheduler runs each cycle without blocking other campaigns
  and completes the campaign when its deadline is reached.
- A Report Options screen lists all ten Telegram reason types. Telegram returns the
  first page of report options, and the bot asks Telegram for the next page after a
  reason is selected. Only real server-returned options are displayed; no local
  sub-options or unsupported API fields are invented.
- Message-link fetching counts real posts only. Service messages (channel created,
  photo changed, pins, joins) and duplicate album items are ignored, so the link
  count matches the visible posts.

> **Important:** the review scheduler sends reminders and updates cycle/review
> status only. It does **not** submit reports. The campaign scheduler is a separate
> feature and does submit reports, but only after an admin creates a campaign and
> explicitly taps Start Now. The existing `/report` workflow remains a separate,
> explicitly started feature.

## Setup

```powershell
cd F:\BBanningbot\ben

# Optional but recommended
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

`ben/.env` already exists locally with the project's previous credentials and is
excluded from Git. To configure a new installation:

```powershell
Copy-Item .env.example .env
notepad .env
```

Required values:

```dotenv
API_ID=123456
API_HASH=your_api_hash
BOT_TOKEN=123456789:your_bot_token
DEFAULT_ADMINS=123456789
```

Useful optional values:

| Variable | Default | Purpose |
|---|---:|---|
| `DATABASE_FILE` | `ben/database.db` | SQLite database path |
| `SESSIONS_FOLDER` | `ben/sessions` | Authorized session files |
| `LOGS_FOLDER` | `ben/logs` | Redacted activity logs |
| `EXPORTS_FOLDER` | `ben/exports` | Temporary export files |
| `REVIEW_DEFAULT_INTERVAL_HOURS` | `2` | Default reminder interval |
| `REVIEW_DEFAULT_MAX_CYCLES` | `5` | Default maximum cycles |
| `REVIEW_PAGE_SIZE` | `5` | Pagination size |
| `REVIEW_MAX_POSTS` | `50` | Recent posts fetched per channel |
| `REVIEW_MAX_ZIP_MB` | `50` | Upload/expansion limit |
| `REVIEW_MAX_IMPORT_SESSIONS` | `30` | Sessions accepted per import |
| `REVIEW_SESSION_TIMEOUT` | `20` | Session validation timeout (seconds) |
| `REVIEW_SCHEDULER_TICK_SECONDS` | `30` | Reminder check interval |
| `ADMIN_ONLY_IMPORTS` | `true` | Restrict session management/imports |
| `CAMPAIGN_MAX_POSTS` | `500` | Maximum links fetched per campaign |
| `CAMPAIGN_DEFAULT_REPORTS_PER_MESSAGE` | `2` | Default reports per message link |
| `CAMPAIGN_DEFAULT_INTERVAL_HOURS` | `2` | Default repeat interval |
| `CAMPAIGN_DEFAULT_DURATION_HOURS` | `24` | Default total campaign duration |
| `CAMPAIGN_TICK_SECONDS` | `30` | Campaign scheduler check interval |

Start the bot:

```powershell
cd F:\BBanningbot\ben
python main.py
```

`config.py` resolves the database, sessions, logs, exports and bot session
relative to `ben/`, so starting from another working directory is safe as long as
you run `python F:\BBanningbot\ben\main.py`.

## Button-driven UI

Send `/start` or `/menu`. Every major action is available as an inline button:

- **Start Reporting** - choose a saved channel or paste a target, then pick
  specific messages or the whole channel, report count, delay, execution mode,
  comment and reason from presets. Custom numeric values are still supported.
- **Reporting Campaigns** - admin-only campaign manager. Create a campaign, select
  one or more accounts, fetch all message links, choose reports per link, delay,
  the exact Telegram reason and its real returned options, comment, repeat interval
  and run duration, review the summary, then Start Now. The campaign view has pause
  and stop controls.
- **Report Options** - the full list of ten real Telegram report reasons with
  plain-English guidance. Real sub-options are loaded from Telegram when available.
- **Login / Import** - one-tap account-count presets, custom count, and (for
  admins) `.session`/ZIP import. OTP and 2FA prompts stay in the private chat.
- **My Accounts** - status overview with refresh, login and reporting shortcuts.
- **Review Manager** - the complete plan/reminder/history UI.
- **Statistics / Help / Admin tools** - navigation buttons and return-to-menu.

All bot text and buttons are English-only and contain no emoji.

### Join-before-report guarantee

When you press **Join All & Start** on the report confirmation screen:

1. The bot connects with every active, enabled account.
2. Each account joins the target public channel/group or private invite.
3. "Already a participant" counts as success.
4. A pending join request, expired invite, unauthorized session or other failure is
   treated as not joined.
5. Only successfully joined accounts are passed to the report engine. If none
   join, the session does not start and the failed reason is shown.

The engine also re-checks membership per account and skips that account if the join
cannot be confirmed, so it cannot silently report from an account that failed to
join.

## Existing database

The application uses `ben/database.db` (the database next to `main.py`). A
root-level `database.db` is a separate legacy artifact and is not used when the bot
runs from `ben/`.

The first start runs backward-compatible migrations on `ben/database.db`. Existing
admins, approved users, accounts, report logs, statistics and user states are
preserved. New columns default to `enabled = 1`, so existing accounts keep working.
It is still sensible to make a backup before upgrading:

```powershell
Copy-Item F:\BBanningbot\ben\database.db F:\BBanningbot\ben\database.db.backup
```

## Using the review manager

1. Send `/review`.
2. **Sessions** -> import a `.session` or ZIP (admin) or use `/login` for phone login.
   Preview shows authorized/invalid/duplicate sessions. Import only the valid,
   non-duplicate ones.
3. **New Review Plan** -> send a public channel username or link.
4. Toggle posts, choose **Select All** or select individual posts, then Continue.
5. For each message choose a category, subcategory, and optional explanation or
   skip. Existing presets can be applied.
6. Choose reminder interval (2h is available) and maximum cycles (5 is available),
   review the complete preview, and confirm.
7. Open the plan and press **Start**. Reminders are sent to the plan creator when
   due. Pause/resume/stop controls are on the plan screen.
8. Mark items reviewed and choose the session that performed the manual review.
   Attribution is recorded; no report is submitted by this action.

### Review category mapping

These labels belong only to the review manager's manual tracking. They are not
Telegram report options. Telegram exposes a fixed set of report reasons; each
review category must map to one real reason:

| Label | Telegram reason |
|---|---|
| Scam or fraud | `fake` |
| Impersonation | `fake` |
| Deceptive or unrealistic financial claims | `fake` |
| Fraudulent seller/product/service | `fake` |
| Malware/phishing | `spam` |

Supported reason keys are `spam`, `fake`, `violence`, `child_abuse`,
`pornography`, `illegal_drugs`, `personal_details`, `copyright`,
`geo_irrelevant`, and `other`.

Admin category commands:

```text
/add_review_category key|label|reason|description
/add_review_subcategory category_key|sub_key|label
/toggle_review_category key
/add_preset name|category_key|sub_key|optional explanation
/review_categories
```

## Reporting campaigns

Open the campaign manager with `/campaign` or the **Reporting Campaigns** button.
Campaigns are admin-only.

1. Tap **New Campaign** and send a name, or skip it.
2. Send the target channel username or link. A public target is recommended; a
   private invite works when the selected fetch account can join it.
3. Choose one account, select multiple accounts, or use all active accounts.
4. Fetch all available post links, or choose a preset/custom fetch limit. The
   largest allowed limit is `CAMPAIGN_MAX_POSTS` (default 500). Service messages and
   duplicate album items are not counted.
5. Choose reports per link. The default is 2, so one account sends two reports to
   every stored link.
6. Choose the delay between individual report requests.
7. Choose the exact report reason returned by Telegram. Use **Explain All Options**
   to see the ten reason types. If Telegram returns real sub-options after a reason
   is selected, choose one from that exact list or continue with the main reason.
   After a report type is saved, tap **Add Another Reason** to add a second or third
   real type. Reports per link is raised automatically when needed, so one message
   can receive different real report types in the same cycle. Duplicate types are
   not added twice.
8. Add an optional comment. Telegram may require a comment for some options.
9. Choose a repeat interval. **Every 2 hours** is the default; **One time** is also
   available, as are 6, 12 and 24 hours and a custom interval.
10. Choose how long the campaign should report: 1 day, 2 days, 3 days, 7 days,
    30 days, unlimited until stopped, or a custom duration. The deadline is set when
    the campaign starts.
11. Review the summary. Tap **Create and Start Now** to save and start in one step,
    or **Create Campaign** to save it for later. The first cycle runs at the next
    scheduler check. Later cycles are scheduled after the configured interval, and
    the campaign completes automatically when the deadline is reached.

Cancel asks for confirmation before discarding an unsaved setup. A campaign that
was already created is never deleted by Cancel.

The campaign view shows status, target, account selection, link count, reports per
link, delay, every selected real report type, comment, interval, run duration,
deadline, cycle count, next run, last run and the last result. Pause stops the
current cycle and future runs. Stop marks the campaign stopped. Resume or Start
Again schedules it at the next scheduler check. Start Again resets the run duration
from that moment. A campaign created by an older version without a final Telegram
option shows **Choose Real Reasons**; select the real reasons again before starting.
The bot refuses to start a campaign that Telegram would reject for a missing
sub-option.

The campaign scheduler claims each due campaign in the database before starting it,
so a campaign cannot run twice at the same time. A campaign interrupted by a bot
restart is reset to active at startup and becomes due again.

## Commands

```text
/start or /menu             Button-driven main menu
/login                      Login one or more accounts
/report                     Start message/channel reporting
/campaign                   Reporting campaign manager (admin)
/options                    List all Telegram report options (admin)
/user                       Report a user or bot
/stop                       Stop an active report
/cancel                     Cancel the current operation
/stats                      Statistics
/review                     Review dashboard
/review_help                Review help
/review_plan <id>           Open a plan
/review_search <text>       Search history
/review_export csv|json     Export review data (admin)
/accounts or /sessions      Session management (admin)
/import_sessions            Import .session/ZIP (admin)
/review_categories          List categories and subcategories
```

All existing admin commands for approval, broadcast and account removal remain
available. The menu exposes the common actions so you do not need to remember them.

## Scheduler semantics

### Review reminders

- Start: status `active`, current cycle `1`, next reminder = now + interval.
- When due: one reminder is claimed with a database uniqueness constraint, sent to
  the plan creator, then the plan advances to the next cycle.
- At the configured maximum cycle the plan becomes `completed` with no next
  reminder.
- Pause stops reminders without losing the cycle. Resume sets a fresh next time.
- Stop ends the plan permanently.
- Restarting the bot does not duplicate a reminder: the unique
  `(plan_id, cycle_number)` row is checked before sending.

### Campaign reports

- Create: status `ready` with no scheduled run.
- Start: status `active`, next run = now, and the deadline is set from the selected
  run duration. The scheduler claims the row by changing status to `running`, then
  starts the report engine.
- Cycle complete: the cycle counter increases. If the deadline has passed or the
  next interval would fall after the deadline, status becomes `completed`.
  Otherwise the next run is set to now + interval.
- Deadline: the scheduler also completes any `ready`, `active` or `paused` campaign
  whose deadline has passed, so no new cycle starts after the selected duration.
- Pause or Stop during a cycle requests a stop and prevents future scheduling.
- If a campaign run raises an error, the campaign becomes `paused` with the error
  stored in Last result. Fix the problem and resume it.
- On startup, rows left in `running` are reset to `active`. Expired deadlines are
  completed on the next scheduler check.

## Security notes

- Credentials are read from `ben/.env` or environment variables; they are no
  longer hard-coded in `config.py`.
- `.env`, `*.session`, database files, exports, logs and ZIPs are excluded by
  `.gitignore`.
- Activity logs redact phone numbers and bot tokens. Session file paths and
  credentials are never included in exports.
- Imported session files are copied with user-only permissions where the platform
  supports it (`0600`). Temporary import directories are deleted after confirm or
  cancel.
- ZIP extraction ignores paths, accepts only `.session` entries, rejects encrypted
  entries, caps entry count and expanded size.
- Session management and imports are admin-only while `ADMIN_ONLY_IMPORTS=true`.
- Exports contain masked reviewer phone numbers only.

## Validation

```powershell
cd F:\BBanningbot\ben
python -m compileall -q .
python -m unittest discover -s tests -v
```

## Limitations

- Review-manager channel post listing works for public usernames/links. Private
  invite links are rejected there because review plans store public channel
  identifiers; the report auto-scrape flow supports private invite targets.
- Specific-message reporting supports public targets and private invite links.
  Whole-channel/group peer reporting needs a public username, link or numeric ID.
- "All posts" means all posts fetched within `REVIEW_MAX_POSTS` (default 50), not an
  unbounded channel crawl. Campaign "Fetch All" means the configured
  `CAMPAIGN_MAX_POSTS` limit (default 500), because Telegram does not provide an
  unbounded single request.
- Campaign reporting is admin-only. Use it for illegal content only and keep the
  delay, interval and duration settings reasonable to avoid Telegram flood limits.
- A campaign deadline is checked before each cycle and at scheduler ticks. A cycle
  that is already running may finish slightly after the deadline if no stop is
  requested.
- Telegram does not provide separate report reasons for impersonation, financial
  claims, or phishing. They map to `fake`/`spam` as shown above; the application
  does not pretend unsupported API reasons exist.
- A reminder is sent only to the user who created the plan.
