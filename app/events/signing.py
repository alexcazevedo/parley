"""Signatures of event pushes (`app/events/delivery.py`).

`X-Parley-Signature: sha256=<hex>` is HMAC-SHA256, keyed with the channel
secret, of the string `"<timestamp>.<raw body>"`, where `<timestamp>` is the
`X-Parley-Timestamp` header (unix seconds). A receiver recomputes it over the
exact bytes it received and refuses an old timestamp (replay).
"""

import hashlib
import hmac
import time

SIGNATURE_PREFIX = "sha256="
DEFAULT_TOLERANCE_SECONDS = 300


def _digest(secret: str, timestamp: int | str, body: bytes) -> str:
    message = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    """The `X-Parley-Signature` value: `sha256=<hex>`."""
    return SIGNATURE_PREFIX + _digest(secret, timestamp, body)


def verify(
    secret: str,
    timestamp: int | str,
    body: bytes,
    signature: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    *,
    now: float | None = None,
) -> bool:
    """True when `signature` signs `body` at `timestamp` with `secret` and the
    timestamp is within `tolerance_seconds` of `now` (default: the clock).

    Constant-time comparison; never raises on malformed input.
    """
    try:
        ts = int(str(timestamp))
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_seconds:
        return False
    expected = sign(secret, ts, body)
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))
