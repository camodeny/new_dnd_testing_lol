from datetime import UTC, datetime


def utcnow():
    """Return the current UTC time in the app's existing naive-datetime format."""
    return datetime.now(UTC).replace(tzinfo=None)
