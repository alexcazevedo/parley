"""Reply splitting + typing-pace calculation for the humanizer (ticket 14).

Pure, stdlib-only module: no I/O, no imports from `app.channels` or
`app.conversations.turn`. Wired into the outbound send path by ticket 16.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

# Threshold (chars) both for triggering the sentence-split fallback and for
# the target chunk size within that fallback — see ticket 14.
_SENTENCE_FALLBACK_CHARS = 280

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n[ \t\n]*")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def split_reply(text: str, max_parts: int) -> list[str]:
    """Split reply text into chat-bubble-sized parts.

    1. Primary: split on blank lines (2+ consecutive newlines).
    2. Fallback: if that yields exactly one part longer than ~280 chars,
       split it on sentence boundaries into ~<=280 char chunks.
    3. Fenced code blocks (``` ... ```) are never split, in either pass.
    4. Capped at `max_parts`; overflow parts are merged into the last one.
    5. Parts are stripped; empties dropped; blank input -> [].
    """
    stripped = (text or "").strip()
    if not stripped:
        return []

    fence_spans = [m.span() for m in _FENCE_RE.finditer(stripped)]
    parts = _split_on_blank_lines(stripped, fence_spans)

    if len(parts) == 1 and len(parts[0]) > _SENTENCE_FALLBACK_CHARS:
        parts = _split_by_sentences(parts[0])

    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return []

    limit = max(max_parts, 1)
    if len(parts) > limit:
        head, tail = parts[: limit - 1], parts[limit - 1 :]
        parts = [*head, "\n\n".join(tail)]

    return parts


def typing_delay_seconds(part: str, settings: Settings) -> float:
    """Simulated typing delay for one bubble, clamped to configured bounds."""
    raw_seconds = len(part) / settings.humanize_chars_per_sec
    return max(
        settings.humanize_min_delay_seconds,
        min(settings.humanize_max_delay_seconds, raw_seconds),
    )


def _split_on_blank_lines(text: str, fence_spans: list[tuple[int, int]]) -> list[str]:
    boundaries = [
        (m.start(), m.end())
        for m in _BLANK_LINE_RE.finditer(text)
        if not _overlaps_any(m.start(), m.end(), fence_spans)
    ]
    if not boundaries:
        return [text]

    parts = []
    prev = 0
    for start, end in boundaries:
        parts.append(text[prev:start])
        prev = end
    parts.append(text[prev:])
    return parts


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _split_by_sentences(text: str, max_len: int = _SENTENCE_FALLBACK_CHARS) -> list[str]:
    fence_spans = [m.span() for m in _FENCE_RE.finditer(text)]
    if not fence_spans:
        return _chunk_sentences(text, max_len)

    chunks: list[str] = []
    pos = 0
    for fence_start, fence_end in fence_spans:
        before = text[pos:fence_start]
        if before.strip():
            chunks.extend(_chunk_sentences(before, max_len))
        chunks.append(text[fence_start:fence_end])
        pos = fence_end
    tail = text[pos:]
    if tail.strip():
        chunks.extend(_chunk_sentences(tail, max_len))
    return chunks


def _chunk_sentences(text: str, max_len: int) -> list[str]:
    sentences = _SENTENCE_BOUNDARY_RE.split(text.strip())
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if not current or len(candidate) <= max_len:
            current = candidate
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks
