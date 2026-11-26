"""`GET /operator` serves the self-contained operator page (ticket 25) and
needs no API key itself — the page does its own `X-API-Key` gate in JS,
unlike the REST endpoints it drives.
"""

import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"


async def test_operator_page_serves_html(client):
    response = await client.get("/operator")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "harness_operator_api_key" in body
    assert "Filas" in body
    assert "Assumir" in body
    for needle in (
        "Minhas",
        "Atender",
        "Devolver à fila",
        "Sem categoria",
        "category-select",
        "operator-id",
        "harness_operator_id",
        "/lines",
        "pane-claim-btn",
        "Em atendimento por",
        "Esta conversa já está com outro atendente.",
    ):
        assert needle in body, needle


async def test_operator_page_needs_no_api_key_but_conversations_does(client):
    operator_response = await client.get("/operator")
    assert operator_response.status_code == 200

    conversations_response = await client.get("/conversations")
    assert conversations_response.status_code in (401, 403)


async def test_operator_page_has_no_demo_key(client):
    response = await client.get("/operator")
    assert response.status_code == 200
    assert "demo-webchat-key" not in response.text


_ASSIGN_RE = re.compile(r"(?:\.innerHTML|\b(?:html|actionsHtml))\s*\+?=(?!=)")
_ALLOWED_IDENTIFIERS = {"html", "actionsHtml", "kind"}


def _strip_strings_and_find_end(source: str, start: int) -> str:
    """Return the right-hand side from `start` up to the terminating `;`
    (outside string literals), with string literals removed."""
    out: list[str] = []
    i = start
    quote = None
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == ";":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


@pytest.mark.parametrize("page", ["operator.html", "chat.html"])
def test_static_pages_never_inject_api_data_as_html(page):
    source = (STATIC_DIR / page).read_text(encoding="utf-8")
    checked = 0
    for match in _ASSIGN_RE.finditer(source):
        rhs = _strip_strings_and_find_end(source, match.end())
        statement = source[match.start() : match.end()] + " " + rhs.strip()
        leftovers = rhs
        for identifier in re.findall(r"[A-Za-z_$][\w$]*", rhs):
            assert identifier in _ALLOWED_IDENTIFIERS, (
                f"{page}: non-static identifier {identifier!r} in: {statement}"
            )
        leftovers = re.sub(r"[A-Za-z_$][\w$]*|===|[+()?:\s]", "", leftovers)
        assert leftovers == "", f"{page}: unexpected tokens {leftovers!r} in: {statement}"
        checked += 1
    assert checked > 0


_FORBIDDEN_SINKS = re.compile(r"insertAdjacentHTML|outerHTML|document\s*\.\s*write|srcdoc")


def _forbidden_sinks(source: str) -> list[str]:
    return _FORBIDDEN_SINKS.findall(source)


def test_sink_scan_catches_bad_source():
    bad = "el.insertAdjacentHTML('beforeend', x); a.outerHTML = y; document.write(z); f.srcdoc = w"
    assert len(_forbidden_sinks(bad)) == 4
    assert _forbidden_sinks("el.textContent = x;") == []


@pytest.mark.parametrize("page", sorted(p.name for p in STATIC_DIR.glob("*.html")))
def test_static_pages_use_no_other_html_sinks(page):
    source = (STATIC_DIR / page).read_text(encoding="utf-8")
    assert _forbidden_sinks(source) == [], f"{page}: forbidden HTML sink"
