from datetime import datetime, timedelta, timezone


def format_recent_datetime(value, now=None):
    """Format an ISO timestamp for the conversation list in local time."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    if now is None:
        now = datetime.now().astimezone()
        local = parsed.astimezone()
    else:
        local = parsed.astimezone(now.tzinfo)

    time = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == now.date():
        return f"Today, {time}"
    if local.date() == now.date() - timedelta(days=1):
        return f"Yesterday, {time}"
    return f"{local:%b} {local.day}, {local:%Y} · {time}"
