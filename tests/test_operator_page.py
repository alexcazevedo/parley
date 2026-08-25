"""`GET /operator` serves the self-contained operator page (ticket 25) and
needs no API key itself — the page does its own `X-API-Key` gate in JS,
unlike the REST endpoints it drives.
"""


async def test_operator_page_serves_html(client):
    response = await client.get("/operator")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "harness_operator_api_key" in body
    assert "Caixa de entrada" in body
    assert "Assumir" in body


async def test_operator_page_needs_no_api_key_but_conversations_does(client):
    operator_response = await client.get("/operator")
    assert operator_response.status_code == 200

    conversations_response = await client.get("/conversations")
    assert conversations_response.status_code in (401, 403)
