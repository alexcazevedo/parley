"""Speech-to-text: turns a voice note's bytes into text.

One OpenAI-compatible implementation (`POST {base}/audio/transcriptions`,
multipart) — the same "one provider shape" decision as
`app.llm.providers.openai_compatible` (PRODUCT.md). `stt_base_url`/
`stt_api_key` default to the LLM provider's own base URL/key so a single
OpenAI key covers both chat and Whisper; set them separately only when STT
should hit a different backend.
"""

import httpx
import structlog

from app.config import Settings
from app.http_client import shared_client
from app.http_retry import send_with_retry

logger = structlog.get_logger(__name__)

# Filename per accepted mime: Whisper-compatible endpoints infer the audio
# format from the file extension, not the multipart content-type alone.
_FILENAMES: dict[str, str] = {
    "audio/ogg": "audio.ogg",
    "audio/mpeg": "audio.mp3",
    "audio/mp3": "audio.mp3",
    "audio/mp4": "audio.m4a",
    "audio/webm": "audio.webm",
    "audio/wav": "audio.wav",
    "audio/x-wav": "audio.wav",
}


class STTError(RuntimeError):
    """Raised when transcription fails: transport, HTTP status, or a body we
    cannot parse. Callers (`app.media.prepare`) degrade to a fixed
    placeholder rather than letting a broken STT provider cost the customer
    a reply.
    """


async def transcribe(data: bytes, mime: str, *, settings: Settings) -> str:
    """POST the audio bytes to the configured Whisper-compatible endpoint.

    Returns the transcript text, stripped. Raises `STTError` on any failure.
    """
    base_url = (settings.stt_base_url or settings.llm_base_url).rstrip("/")
    api_key = settings.stt_api_key or settings.llm_api_key
    url = f"{base_url}/audio/transcriptions"

    form_data: dict[str, str] = {"model": settings.stt_model, "response_format": "json"}
    if settings.stt_language:
        form_data["language"] = settings.stt_language

    filename = _FILENAMES.get(mime, "audio.bin")

    try:
        response = await send_with_retry(
            lambda: shared_client().post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                data=form_data,
                files={"file": (filename, data, mime)},
                timeout=settings.stt_timeout_seconds,
            ),
            settings=settings,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        raise STTError(
            f"{exc.response.status_code} from {url}: {exc.response.text[:500]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise STTError(f"transport error talking to {url}: {exc}") from exc

    try:
        return str(body["text"]).strip()
    except (KeyError, TypeError) as exc:
        raise STTError(f"unexpected transcription body: {str(body)[:500]}") from exc
