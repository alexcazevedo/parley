"""Scripted end-to-end demo of the PRODUCT.md media success demo against a
*running* `docker compose up` stack — the same style as `scripts/demo.py`
(plain `httpx`, no test framework), but driving `/chat`'s upload flow
instead of plain text.

Uploads `tests/fixtures/order_label.png` (shows "PEDIDO 12345") with a
caption, then `tests/fixtures/voice_note.mp3` (a real voice note asking
about the order) within the debounce window so both land in the same turn,
then asserts the agent replies with the order status pulled from both the
image and the transcribed audio.

Usage:
    cp .env.example .env   # then set a real LLM_API_KEY
    docker compose up -d --build
    uv run scripts/demo_media.py

Exit codes: 0 success, 2 `.env` still has the placeholder LLM key (expected
in CI/sandboxed runs with no real key — not a pipeline failure), 1 any
expectation about the pipeline's behaviour was not met.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

PLACEHOLDER_LLM_KEY = "changeme-llm-key"
DEFAULT_API_PORT = "8001"

HEALTH_TIMEOUT_SECONDS = 30
HEALTH_POLL_SECONDS = 1
REPLY_TIMEOUT_SECONDS = 60
REPLY_POLL_SECONDS = 1

ORDER_ID = "12345"
# Whisper transcribes pt-BR numbers with a thousands separator (see
# tickets/30-media-pages-demo.md's note from ticket 28's live probe).
ORDER_ID_FORMATTED = "12.345"

AUDIO_FAILED_TEXT = "[áudio não compreendido]"
AUDIO_EMPTY_TEXT = "[áudio sem fala detectada]"

NOT_FOUND_PHRASES = (
    "não encontrado",
    "não foi encontrado",
    "não localizado",
    "não localizei",
    "not found",
)


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _settings() -> dict[str, str]:
    """`.env` values, with real environment variables taking priority —
    mirrors pydantic-settings' own precedence (see app/config.py).
    """
    return {**_load_env_file(ROOT / ".env"), **os.environ}


def _wait_for_api(client: httpx.Client, base_url: str) -> bool:
    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            resp = client.get(f"{base_url}/health")
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(HEALTH_POLL_SECONDS)
    return False


def _get_messages(
    client: httpx.Client, base_url: str, headers: dict[str, str], conversation_id: str
) -> list[dict[str, Any]]:
    resp = client.get(f"{base_url}/conversations/{conversation_id}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json()


def _wait_for_new_message(
    client: httpx.Client,
    base_url: str,
    headers: dict[str, str],
    conversation_id: str,
    baseline_count: int,
) -> list[dict[str, Any]]:
    """Poll until the transcript grew past `baseline_count` AND the newest
    message is a reply (not one of the user's own uploads, which also grow
    the transcript)."""

    def _got_reply(msgs: list[dict[str, Any]]) -> bool:
        return len(msgs) > baseline_count and msgs[-1]["role"] == "assistant"

    deadline = time.time() + REPLY_TIMEOUT_SECONDS
    messages = _get_messages(client, base_url, headers, conversation_id)
    while not _got_reply(messages):
        if time.time() >= deadline:
            raise TimeoutError(
                f"timed out after {REPLY_TIMEOUT_SECONDS}s waiting for a reply "
                f"(still {len(messages)} messages, expected more than {baseline_count})"
            )
        time.sleep(REPLY_POLL_SECONDS)
        messages = _get_messages(client, base_url, headers, conversation_id)
    return messages


def _print_transcript(messages: list[dict[str, Any]]) -> None:
    print("-- transcript --")
    for message in messages:
        media_note = f" (media_url={message['media_url']})" if message.get("media_url") else ""
        print(f"  [{message['role']}/{message['type']}] {message['content']!r}{media_note}")


def main() -> int:
    env = _settings()

    llm_key = env.get("LLM_API_KEY", "")
    if not llm_key or llm_key == PLACEHOLDER_LLM_KEY:
        print(
            "LLM_API_KEY in .env is still the placeholder "
            f"({PLACEHOLDER_LLM_KEY!r}). Copy .env.example to .env and set a "
            "real OpenAI-compatible key before running the live media demo."
        )
        return 2

    api_port = env.get("API_PORT", DEFAULT_API_PORT)
    base_url = f"http://localhost:{api_port}"

    image_bytes = (FIXTURES / "order_label.png").read_bytes()
    audio_bytes = (FIXTURES / "voice_note.mp3").read_bytes()

    with httpx.Client(timeout=30.0) as client:
        print(f"== waiting for the API at {base_url} ==")
        if not _wait_for_api(client, base_url):
            print(f"FAILED: {base_url}/health never returned 200 within {HEALTH_TIMEOUT_SECONDS}s")
            return 1

        print("== bootstrapping the /chat demo tenant/agent/channel ==")
        bootstrap_resp = client.post(f"{base_url}/chat/bootstrap")
        bootstrap_resp.raise_for_status()
        bootstrap = bootstrap_resp.json()
        headers = {"X-API-Key": bootstrap["api_key"]}
        channel_id = bootstrap["channel_id"]
        user_id = f"demo-media-user-{int(time.time())}"
        print(f"  channel: {channel_id}")

        print("\n== uploading order_label.png with caption 'esse é meu pedido' ==")
        image_resp = client.post(
            f"{base_url}/channels/webchat/media",
            data={"channel_id": channel_id, "user_id": user_id, "caption": "esse é meu pedido"},
            files={"file": ("order_label.png", image_bytes, "image/png")},
            headers=headers,
        )
        image_resp.raise_for_status()
        image_out = image_resp.json()
        conversation_id = image_out["conversation_id"]
        image_message_id = image_out["message_id"]
        print(f"  -> conversation: {conversation_id}, message: {image_message_id}")

        baseline = len(_get_messages(client, base_url, headers, conversation_id))

        print("== uploading voice_note.mp3 within the debounce window ==")
        audio_resp = client.post(
            f"{base_url}/channels/webchat/media",
            data={"channel_id": channel_id, "user_id": user_id},
            files={"file": ("voice_note.mp3", audio_bytes, "audio/mpeg")},
            headers=headers,
        )
        audio_resp.raise_for_status()
        audio_out = audio_resp.json()
        audio_message_id = audio_out["message_id"]
        print(f"  -> message: {audio_message_id}")

        print(f"\n  waiting up to {REPLY_TIMEOUT_SECONDS}s for the agent's reply...")
        messages = _wait_for_new_message(client, base_url, headers, conversation_id, baseline)
        _print_transcript(messages)

        by_id = {m["id"]: m for m in messages}

        audio_message = by_id.get(audio_message_id)
        if audio_message is None:
            print(f"\nFAILED: audio message {audio_message_id} not found in transcript")
            return 1
        transcript_text = audio_message["content"]
        print(f"\ntranscribed audio content: {transcript_text!r}")
        if not transcript_text or transcript_text in (AUDIO_FAILED_TEXT, AUDIO_EMPTY_TEXT):
            print("FAILED: audio message content is empty or the STT-failure placeholder")
            return 1
        print("OK: audio message has a non-empty, non-placeholder transcript")

        assistant_replies = [
            m
            for m in messages
            if m["role"] == "assistant"
            and (ORDER_ID in m["content"] or ORDER_ID_FORMATTED in m["content"])
        ]
        if not assistant_replies:
            print(
                "\nFAILED: no assistant reply mentions the order "
                f"({ORDER_ID}/{ORDER_ID_FORMATTED})"
            )
            return 1
        order_reply = assistant_replies[-1]
        print(f"\nassistant reply mentioning the order: {order_reply['content']!r}")
        lowered = order_reply["content"].lower()
        if any(phrase in lowered for phrase in NOT_FOUND_PHRASES):
            print("FAILED: the reply says the order was not found")
            return 1
        print(f"OK: agent answered about order {ORDER_ID}, without a 'not found'")

        for label, message_id in (("image", image_message_id), ("audio", audio_message_id)):
            message = by_id.get(message_id)
            if message is None:
                print(f"\nFAILED: {label} message {message_id} missing from transcript")
                return 1
            media_url = message.get("media_url")
            if not media_url:
                print(f"\nFAILED: {label} message has no media_url")
                return 1
            media_resp = client.get(f"{base_url}{media_url}", headers=headers)
            if media_resp.status_code != 200:
                print(f"\nFAILED: GET {media_url} returned {media_resp.status_code}")
                return 1
            print(
                f"OK: {label} media_url {media_url} returns 200 "
                f"({len(media_resp.content)} bytes)"
            )

    print("\nMedia demo completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
