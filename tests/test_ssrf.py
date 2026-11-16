"""The SSRF guard: what a tenant-controlled tool endpoint is allowed to reach.

DNS is faked so the suite is hermetic and can pretend a hostname resolves
wherever a test needs it to.
"""

import socket

import pytest

from app.config import get_settings
from app.tools.ssrf import SSRFError, assert_safe_url, validate_tool_endpoint

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


@pytest.fixture(autouse=True)
def fresh_settings():
    """The guard reads `get_settings()`, which is cached; tests change env vars."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


# Not globally routable, yet none of is_private/is_loopback/... caught them
# all before: CGNAT, IPv4 embedded in IPv6 (mapped, NAT64, 6to4), IETF
# protocol assignments, multicast.
NON_GLOBAL_URLS = [
    "http://100.64.0.1/",
    "http://[::ffff:127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/",
    "http://[2002:7f00:1::]/",
    "http://192.0.0.1/",
    "http://224.0.0.1/",
    # Already blocked by the old flag list only because Python marks all of
    # 64:ff9b::/96 reserved; a bare `is_global` check would let these through
    # unless the embedded IPv4 is unwrapped.
    "http://[64:ff9b::6440:1]/",
    "http://[::ffff:100.64.0.1]/",
    "http://[2002:6440:1::]/",
]


@pytest.mark.parametrize("url", NON_GLOBAL_URLS)
async def test_non_global_addresses_are_rejected(url):
    with pytest.raises(SSRFError, match="blocked internal address"):
        await assert_safe_url(url)


@pytest.mark.parametrize("url", ["http://93.184.216.34/", "http://[2606:4700:4700::1111]/"])
async def test_global_ip_literals_are_allowed(url):
    await assert_safe_url(url)


@pytest.fixture
def no_dns(monkeypatch):
    def getaddrinfo(*args, **kwargs):
        raise AssertionError("validate_tool_endpoint must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    "url",
    [
        *NON_GLOBAL_URLS,
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "//example.test/no-scheme",
    ],
)
def test_validate_tool_endpoint_rejects_unsafe_urls(no_dns, url):
    with pytest.raises(SSRFError):
        validate_tool_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/orders",
        "https://nope.invalid/orders",  # hostnames are not resolved at create time
        "http://93.184.216.34/",
        "http://[2606:4700:4700::1111]/",
    ],
)
def test_validate_tool_endpoint_allows_public_urls_without_dns(no_dns, url):
    validate_tool_endpoint(url)


def test_validate_tool_endpoint_honours_allowlist(no_dns, monkeypatch):
    monkeypatch.setenv("TOOL_ALLOWED_HOSTS", "mock-tools")
    validate_tool_endpoint("http://mock-tools:9000/orders")
    with pytest.raises(SSRFError, match="TOOL_ALLOWED_HOSTS"):
        validate_tool_endpoint("https://example.com/x")


def test_validate_tool_endpoint_respects_disabled_guard(no_dns, monkeypatch):
    monkeypatch.setenv("TOOL_SSRF_DISABLED", "true")
    validate_tool_endpoint("http://127.0.0.1:6379/")
    with pytest.raises(SSRFError):
        validate_tool_endpoint("file:///etc/passwd")


def test_guard_reads_settings_not_os_environ(no_dns, monkeypatch):
    """Values that live only in `.env` reach `Settings`, never `os.environ`."""
    from app.config import Settings
    from app.tools import ssrf

    monkeypatch.setattr(
        ssrf, "get_settings", lambda: Settings(tool_allowed_hosts="internal.test")
    )
    validate_tool_endpoint("http://internal.test/orders")
    with pytest.raises(SSRFError, match="TOOL_ALLOWED_HOSTS"):
        validate_tool_endpoint("https://example.com/x")


async def test_six_to_four_with_public_ipv4_stays_blocked() -> None:
    # 2002::/16 is not globally routable itself, whatever IPv4 it embeds.
    with pytest.raises(SSRFError):
        await assert_safe_url("http://[2002:808:808::1]/")


async def test_public_ip_literals_still_allowed() -> None:
    await assert_safe_url("http://8.8.8.8/")
    await assert_safe_url("http://[2606:4700:4700::1111]/")
    await assert_safe_url("http://[64:ff9b::808:808]/")


@pytest.mark.parametrize(
    "url",
    ["http://[::7f00:1]/", "http://[::1.2.3.4]/", "http://[fec0::1]/"],
)
async def test_leftover_ipv6_ranges_are_blocked(url):
    with pytest.raises(SSRFError, match="blocked internal address"):
        await assert_safe_url(url)
    with pytest.raises(SSRFError, match="blocked internal address"):
        validate_tool_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://app.localhost/x",
        "http://printer.local/x",
        "http://metadata.google.internal/",
    ],
)
def test_validate_rejects_local_hostnames(url):
    with pytest.raises(SSRFError, match="blocked internal host"):
        validate_tool_endpoint(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1/",
        "http://0x7f000001/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f.1/",
        "http://169.254.43/",  # short form of link-local
    ],
)
def test_validate_rejects_non_canonical_ipv4_forms(url):
    with pytest.raises(SSRFError, match="blocked internal address"):
        validate_tool_endpoint(url)


def test_validate_allows_public_non_canonical_ipv4_and_names():
    validate_tool_endpoint("http://134744072/")  # 8.8.8.8 as an integer
    validate_tool_endpoint("http://8.8.8.8/")
    validate_tool_endpoint("https://example.com/")
    validate_tool_endpoint("https://localhost.example.com/")


def test_allowlist_skips_the_name_and_ipv4_form_rules(monkeypatch):
    monkeypatch.setenv("TOOL_ALLOWED_HOSTS", "billing.internal,localhost,127.1")
    validate_tool_endpoint("http://billing.internal/x")
    validate_tool_endpoint("http://localhost/x")
    validate_tool_endpoint("http://127.1/x")
