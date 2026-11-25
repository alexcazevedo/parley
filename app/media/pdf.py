"""Text of a PDF, for a customer's document. No OCR: a scanned PDF has no text."""

import io

import structlog
from pypdf import PdfReader

logger = structlog.get_logger(__name__)


def extract_pdf_text(data: bytes, max_pages: int, max_chars: int) -> str:
    """Text of the first `max_pages` pages, cut at `max_chars`, stripped.

    Never raises: a broken, encrypted or text-less file gives `""`. Blocking
    work; callers run it off the event loop.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return ""
        parts: list[str] = []
        total = 0
        for page in reader.pages[:max_pages]:
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(text)
                total += len(text) + 2
            if total >= max_chars:
                break
        return "\n\n".join(parts)[:max_chars].strip()
    except Exception as exc:  # pypdf raises many types on bad input
        logger.warning("media.pdf_unreadable", error=type(exc).__name__)
        return ""
