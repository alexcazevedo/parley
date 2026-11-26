"""Shared paging helpers: an opaque cursor and the `X-Next-Cursor` header."""

import base64
import binascii
import json

from fastapi import HTTPException, Response

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
NEXT_CURSOR_HEADER = "X-Next-Cursor"


def encode_cursor(values: dict) -> str:
    raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None) -> dict | None:
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        raise HTTPException(status_code=422, detail="invalid cursor") from None
    if not isinstance(values, dict):
        raise HTTPException(status_code=422, detail="invalid cursor")
    return values


def set_next_cursor(response: Response, values: dict | None) -> None:
    if values is not None:
        response.headers[NEXT_CURSOR_HEADER] = encode_cursor(values)
