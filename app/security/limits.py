"""Message size limits enforcement."""

from app.config import get_settings


def check_message_size(message: str, enforce_strict: bool = False) -> tuple[bool, str]:
    """Check message size against MAX_MESSAGE_CHARS.

    Args:
        message: The message text to check
        enforce_strict: If True, return (False, message) if exceeded (for 413 response).
                       If False, return (False, truncated) if exceeded (for logging).

    Returns:
        (within_limit, message_or_truncated)
    """
    settings = get_settings()
    max_chars = settings.max_message_chars

    if len(message) <= max_chars:
        return True, message

    # Message exceeds limit
    if enforce_strict:
        return False, message

    # Truncate for webhooks, but return False so caller knows truncation occurred
    return False, message[:max_chars]
