"""The SSRF guard: what a tenant-controlled tool endpoint is allowed to reach.

DNS is faked so the suite is hermetic and can pretend a hostname resolves
wherever a test needs it to.
"""

import socket

import pytest

from app.tools.ssrf import SSRFError, assert_safe_url

# Hostname -> addresses `socket.getaddrinfo` will pretend to return.
FAKE_DNS = {
    "example.test": ["93.184.216.34"],
    "localhost": ["127.0.0.1"],
    "internal.test": ["10.1.2.3"],
    # Classic DNS-rebinding record: one public answer, one private.
    "rebind.test": ["93.184.216.34", "192.168.1.10"],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def getaddrinfo(host, port, *args, **kwargs):
        try:
            addresses = FAKE_DNS[host]
        except KeyError:
            raise socket.gaierror(f"no fake DNS entry for {host}") from None
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 80))
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("TOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("TOOL_SSRF_DISABLED", raising=False)


async def test_public_https_endpoint_is_allowed():
    await assert_safe_url("https://example.test/orders?order_id=12345")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.test/x",
        "gopher://example.test/",
        "//example.test/no-scheme",
    ],
)
async def test_non_http_schemes_are_rejected(url):
    with pytest.raises(SSRFError):
        await assert_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/admin",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://localhost/",  # via DNS, not a literal
    ],
)
async def test_internal_addresses_are_rejected(url):
    with pytest.raises(SSRFError):
        await assert_safe_url(url)


async def test_host_resolving_to_both_public_and_private_is_rejected():
    with pytest.raises(SSRFError):
        await assert_safe_url("https://rebind.test/orders")


async def test_unresolvable_host_is_rejected():
    with pytest.raises(SSRFError, match="could not resolve"):
        await assert_safe_url("https://nope.invalid/orders")


async def test_allowlist_overrides_the_ip_checks(monkeypatch):
    monkeypatch.setenv("TOOL_ALLOWED_HOSTS", "internal.test, other.test")
    await assert_safe_url("http://internal.test/orders")


async def test_allowlist_rejects_hosts_outside_it(monkeypatch):
    monkeypatch.setenv("TOOL_ALLOWED_HOSTS", "internal.test")
    with pytest.raises(SSRFError, match="TOOL_ALLOWED_HOSTS"):
        await assert_safe_url("https://example.test/orders")


async def test_guard_can_be_disabled_for_development(monkeypatch):
    monkeypatch.setenv("TOOL_SSRF_DISABLED", "true")
    await assert_safe_url("http://127.0.0.1:6379/")


async def test_disabled_flag_still_rejects_non_http_schemes(monkeypatch):
    monkeypatch.setenv("TOOL_SSRF_DISABLED", "true")
    with pytest.raises(SSRFError):
        await assert_safe_url("file:///etc/passwd")
