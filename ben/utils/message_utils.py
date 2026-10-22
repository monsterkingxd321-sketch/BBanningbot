"""Helpers for deciding which Telegram messages count as reportable posts."""


def is_reportable_post(message, grouped_seen: set) -> bool:
    """Return True for a real post, ignoring service messages and album duplicates."""
    if not getattr(message, "id", None):
        return False
    if getattr(message, "action", None):
        return False
    if not (getattr(message, "message", None) or getattr(message, "media", None)):
        return False
    grouped_id = getattr(message, "grouped_id", None)
    if grouped_id:
        if grouped_id in grouped_seen:
            return False
        grouped_seen.add(grouped_id)
    return True
