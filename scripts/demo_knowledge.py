"""The knowledge success demo against a running stack with the stub model.

    uv run python scripts/demo_knowledge.py --base-url http://localhost:8001 --admin-key <key>

Creates its own tenant, adds an entry, a pair and a PDF, waits for the PDF to
be ready, then sends the same question on a channel whose agent has
`knowledge_enabled` on and on one where it is off. Prints each step, exits
non-zero on the first failed expectation and prints `knowledge demo: ok` last.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
import uuid
from typing import Any

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

FACT = "The quokka warranty lasts ninety-seven days."
QUESTION = "How long is the quokka warranty?"
TIMEOUT_SECONDS = 60
STUB_AFTER_SEARCH = "Stub answer from knowledge:"


def fail(message: str) -> None:
    print(f"FAILED: {message}")
    sys.exit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def make_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 10 Tf 10 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def poll(what: str, fetch: Any, done: Any) -> Any:
    deadline = time.time() + TIMEOUT_SECONDS
    while True:
        value = fetch()
        if done(value):
            return value
        if time.time() >= deadline:
            fail(f"timed out after {TIMEOUT_SECONDS}s waiting for {what}")
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--admin-key", required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    with httpx.Client(timeout=30.0) as client:

        def call(method: str, path: str, status: int, headers: dict, **kwargs: Any) -> Any:
            response = client.request(method, f"{base}{path}", headers=headers, **kwargs)
            expect(
                response.status_code == status,
                f"{method} {path}: {response.status_code} {response.text}",
            )
            return response.json()

        print("== waiting for the API ==")
        poll("the API", lambda: _healthy(client, base), bool)

        print("== creating a tenant ==")
        tenant = call(
            "POST", "/tenants", 201, {"X-Admin-Key": args.admin_key},
            json={"name": f"Knowledge demo {uuid.uuid4().hex[:8]}"},
        )
        headers = {"X-API-Key": tenant["api_key"]}

        print("== adding an entry, a question-and-answer pair and a PDF ==")
        call("POST", "/knowledge/entries", 201, headers,
             json={"title": "Opening hours", "text": "We open at nine and close at six."})
        call("POST", "/knowledge/qa", 201, headers,
             json={"question": "Do you ship abroad?", "answer": "Yes, to every country in Europe."})
        pdf = call("POST", "/knowledge/documents", 201, headers,
                   files={"file": ("warranty.pdf", make_pdf(FACT), "application/pdf")},
                   data={"title": "Warranty"})
        expect(pdf["status"] == "processing", f"new document status is {pdf['status']}")

        print("== waiting for the PDF to be ready ==")
        item = poll(
            "the PDF",
            lambda: call("GET", f"/knowledge/{pdf['id']}", 200, headers),
            lambda i: i["status"] in ("ready", "failed"),
        )
        expect(item["status"] == "ready", f"the PDF is {item['status']}: {item.get('error')}")
        print(f"  ready, {item['passages']} passage(s)")

        def ask(name: str, enabled: bool) -> list[dict[str, Any]]:
            print(f"== agent {name}: knowledge_enabled={enabled} ==")
            agent = call("POST", "/agents", 201, headers, json={
                "name": name, "instructions": "You answer customers of Acme.",
                "model": "stub", "knowledge_enabled": enabled,
                "boundary_check_enabled": False,
            })
            channel = call("POST", "/channels", 201, headers, json={
                "type": "webchat", "name": f"Web {name}", "agent_id": agent["id"]})
            sent = call("POST", "/channels/webchat/messages", 201, headers, json={
                "channel_id": channel["id"], "user_id": f"demo-{uuid.uuid4().hex[:8]}",
                "message": QUESTION})
            path = f"/conversations/{sent['conversation_id']}/messages"
            messages = poll(
                "the reply",
                lambda: call("GET", path, 200, headers),
                lambda ms: bool(ms) and ms[-1]["role"] == "assistant",
            )
            for message in messages:
                print(f"  [{message['role']}/{message['type']}] {message['content']}")
            return messages

        on = ask("on", True)
        expect(FACT in on[-1]["content"], "the reply does not contain the PDF's fact")
        # The API does not list tool calls; the stub only answers this way after
        # a search_knowledge result (the run's execution row is checked by the test).
        expect(
            on[-1]["content"].startswith(STUB_AFTER_SEARCH),
            "the reply is not the stub's answer after a search_knowledge call",
        )

        off = ask("off", False)
        expect(
            not off[-1]["content"].startswith(STUB_AFTER_SEARCH),
            "the agent with the switch off answered from a search_knowledge call",
        )
        expect(FACT not in off[-1]["content"], "the reply with the switch off has the fact")

    print("knowledge demo: ok")
    return 0


def _healthy(client: httpx.Client, base: str) -> bool:
    try:
        return client.get(f"{base}/health").status_code == 200
    except httpx.HTTPError:
        return False


if __name__ == "__main__":
    sys.exit(main())
