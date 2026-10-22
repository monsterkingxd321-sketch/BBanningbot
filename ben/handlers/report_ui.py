"""Reusable inline keyboards and Telegram report-reason metadata.

Telegram exposes ten real report reason types. Their display names below are the
official English names used by Telegram clients. Telegram decides whether a
reason has further options; those options are fetched from Telegram at runtime
and are never invented locally.
"""

from telethon import Button

from config import MAX_REPORTS_PER_ACCOUNT, MIN_DELAY, MAX_DELAY


REPORT_REASON_CHOICES = [
    ("spam", "Spam", "Unsolicited advertising, mass messaging or automated spam."),
    ("fake", "Fake Account", "Scams, impersonation or a deceptive account."),
    ("violence", "Violence", "Threats, incitement or graphic violent content."),
    ("child_abuse", "Child Abuse", "Sexual content involving minors or child exploitation."),
    ("pornography", "Pornography", "Adult sexual content where it is not allowed."),
    ("illegal_drugs", "Illegal Drugs", "Drug sales, promotion or drug-related illegal activity."),
    ("personal_details", "Personal Details", "Doxxing, private information or non-consensual sharing."),
    ("copyright", "Copyright", "Unauthorized copyrighted material or stolen content."),
    ("geo_irrelevant", "Geo Irrelevant", "Content irrelevant to the channel's location or topic."),
    ("other", "Other", "Any other violation; add a clear explanation in the comment."),
]

REASON_LABELS = {key: label for key, label, _ in REPORT_REASON_CHOICES}
REASON_DESCRIPTIONS = {key: description for key, _, description in REPORT_REASON_CHOICES}
REASON_KEYS_BY_LABEL = {
    label.strip().lower(): key for key, label, _ in REPORT_REASON_CHOICES
}


def reason_key_from_label(label: str, fallback: str = "other") -> str:
    """Map an official Telegram reason label to its local reason key."""
    value = (label or "").strip().lower()
    if value in REASON_KEYS_BY_LABEL:
        return REASON_KEYS_BY_LABEL[value]
    aliases = {
        "fake account": "fake",
        "scam": "fake",
        "child abuse": "child_abuse",
        "illegal drugs": "illegal_drugs",
        "personal details": "personal_details",
        "geo irrelevant": "geo_irrelevant",
        "irrelevant / geo": "geo_irrelevant",
    }
    return aliases.get(value, fallback)


def reason_display(reason_key: str, detail: str = "") -> str:
    """Return the official reason label plus an optional real sub-option."""
    label = REASON_LABELS.get(reason_key)
    if not label:
        label = reason_key or "Other"
    detail = (detail or "").strip()
    return f"{label} - {detail}" if detail else label


def _preset_rows(values, prefix: str, per_row: int = 3):
    rows = []
    row = []
    for value in values:
        row.append(Button.inline(str(value), f"{prefix}:{value}".encode()))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def count_buttons(max_count: int = MAX_REPORTS_PER_ACCOUNT):
    presets = [1, 2, 3, 5, 10, 25, 50]
    values = [value for value in presets if value <= max_count]
    rows = _preset_rows(values, "count_pick")
    rows.append([Button.inline("Custom count", b"ui_custom_count")])
    return rows


def scrape_buttons():
    rows = _preset_rows([10, 20, 30, 50, 100], "scrape_pick")
    rows.append([Button.inline("Custom count", b"ui_custom_count")])
    return rows


def delay_buttons():
    presets = [1, 3, 5, 10, 30, 60]
    values = [value for value in presets if MIN_DELAY <= value <= MAX_DELAY]
    rows = _preset_rows(values, "delay_pick")
    rows.append([Button.inline("Custom delay", b"ui_custom_count")])
    return rows


def exec_mode_buttons(peer: bool = False):
    prefix = b"peer_exec_mode_" if peer else b"exec_mode_"
    return [
        [Button.inline("Turbo Mode (Parallel)", prefix + b"turbo")],
        [Button.inline("Safe Mode (Sequential)", prefix + b"safe")],
    ]


def comment_buttons(peer: bool = False):
    prefix = b"peer_comment_" if peer else b"comment_"
    return [
        [Button.inline("Add comment", prefix + b"yes")],
        [Button.inline("No comment", prefix + b"no")],
    ]


def _reason_rows(prefix: str):
    rows = []
    row = []
    for key, label, _ in REPORT_REASON_CHOICES:
        row.append(Button.inline(label, f"{prefix}_{key}".encode()))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def message_reason_buttons():
    """Main reason buttons for message reports."""
    return _reason_rows("reason")


def peer_reason_buttons():
    """Main reason buttons for whole-peer and user reports."""
    return _reason_rows("peer_reason")


def report_option_buttons(options, prefix: str = "reason_opt", per_row: int = 2):
    """Buttons for real options returned by Telegram, indexed into saved state."""
    rows = []
    row = []
    for index, option in enumerate(options or []):
        label = str(option.get("text") or "Option")
        row.append(Button.inline(label, f"{prefix}:{index}".encode()))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def nav_buttons():
    return [[Button.inline("Main Menu", b"menu:home")]]


def with_nav(rows):
    return list(rows) + nav_buttons()
