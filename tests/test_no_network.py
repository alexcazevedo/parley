"""The network guard in conftest.py: no test reaches a host outside the box."""

import socket

import httpx
import pytest
import respx

from tests.conftest import NetworkGuard


async def test_unmocked_httpx_request_to_example_com_is_refused(network_guard):
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.ConnectError):
            await http.get("https://example.com")

    assert network_guard.attempts == ["example.com"]
    with pytest.raises(AssertionError, match="example.com"):
        network_guard.check()
    network_guard.attempts.clear()  # this test expected it


def test_connection_to_a_stably_resolved_host_is_refused(network_guard):
    # `_stable_dns` resolves *.example.com to a public address: the lookup is
    # fine, the connection is not.
    with pytest.raises(OSError, match="network guard"):
        socket.create_connection(("evolution.example.com", 443), timeout=1)

    assert network_guard.attempts == ["93.184.216.34"]
    network_guard.attempts.clear()


def test_localhost_is_not_refused(network_guard):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=1)
        client.close()
    finally:
        server.close()

    network_guard.check()


@respx.mock
async def test_respx_mocked_request_is_not_refused(network_guard):
    respx.get("https://api.example.com/ping").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as http:
        response = await http.get("https://api.example.com/ping")

    assert response.json() == {"ok": True}
    network_guard.check()


def test_check_lists_every_host_that_was_tried():
    guard = NetworkGuard()
    for host in ("api.openai.com", "api.openai.com", "example.org"):
        with pytest.raises(OSError):
            guard.refuse(host)

    with pytest.raises(AssertionError) as failure:
        guard.check()

    assert "api.openai.com, example.org" in str(failure.value)
