"""SSRF guard for outbound customer-tool HTTP calls and channel endpoints.

Channel `base_url`s use the same address rules with their own operator list;
see "channel endpoints" below.

A tenant controls the `endpoint` of every one of its tools, so an endpoint is
attacker-controlled input from the harness's point of view: without this check
a tenant could point a tool at `http://169.254.169.254/...` (cloud metadata),
at Redis/Postgres on the private network, or at `file://`.

Only globally routable addresses are allowed (`ipaddress.is_global`, minus
multicast). IPv6 forms that embed an IPv4 address (IPv4-mapped, NAT64
`64:ff9b::/96`, 6to4 `2002::/16`) must also embed a global IPv4 address.

Configuration comes from `app.config.get_settings()`, like the rest of the
app, so values set only in `.env` apply:

- `TOOL_ALLOWED_HOSTS` (`Settings.tool_allowed_hosts`) — comma-separated
  hostname allowlist. When set it is authoritative: listed hosts skip the IP
  checks (that is how you point tools at an internal service on purpose),
  everything else is rejected outright.
- `TOOL_SSRF_DISABLED=true` (`Settings.tool_ssrf_disabled`) — disables the
  guard entirely. Development only; every check logs a loud warning.

`get_settings()` is cached: tests that change these env vars must call
`get_settings.cache_clear()`.

Two entry points share the same static checks:

- `validate_tool_endpoint(url)` — synchronous, no DNS. Run when a tool is
  created or updated. A hostname passes unless an allowlist excludes it.
- `assert_safe_url(url)` — run before every call; additionally resolves the
  hostname and checks every address.
"""

import asyncio
import ipaddress
import socket
from typing import cast
from urllib.parse import SplitResult, urlsplit

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_HOSTS_ENV = "TOOL_ALLOWED_HOSTS"
SSRF_DISABLED_ENV = "TOOL_SSRF_DISABLED"

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_SITE_LOCAL = ipaddress.IPv6Network("fec0::/10")
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal")


class SSRFError(ValueError):
    """The URL is not safe to request. Message is safe to show the model."""


def _allowed_hosts() -> set[str]:
    raw = get_settings().tool_allowed_hosts
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _guard_disabled() -> bool:
    return get_settings().tool_ssrf_disabled


def _embedded_ipv4(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The IPv4 address an IPv6 address carries, or `ip` itself."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _SIX_TO_FOUR:
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    if ip in _IPV4_COMPATIBLE and int(ip) > 1:  # not `::` or `::1`
        return ipaddress.IPv4Address(int(ip))
    return ip


def _assert_public_ip(host: str, raw: str) -> None:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError as exc:  # getaddrinfo handed us something unparseable
        raise SSRFError(f"could not resolve host: {host}") from exc
    judged = _embedded_ipv4(ip)
    # `is_global` excludes private, loopback, link-local (169.254.169.254
    # metadata), CGNAT 100.64.0.0/10, reserved and unspecified ranges.
    # Both the address itself and any IPv4 address it embeds must be global:
    # the embedded check catches e.g. CGNAT behind NAT64, the outer check
    # keeps non-global wrappers such as 6to4 `2002::/16` blocked outright.
    # Deprecated ranges Python still reports as global: site-local and
    # IPv4-compatible (`::1.2.3.4`, whatever IPv4 it carries).
    if ip in _SITE_LOCAL or ip in _IPV4_COMPATIBLE:
        raise SSRFError(f"blocked internal address for host {host}: {ip}")
    if any(not addr.is_global or addr.is_multicast for addr in (ip, judged)):
        raise SSRFError(f"blocked internal address for host {host}: {ip}")


def _parse(url: str) -> tuple[SplitResult, str]:
    """Scheme and host checks every URL gets, whatever the configuration."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFError(f"unsupported URL scheme: {scheme or '(none)'}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise SSRFError("URL has no host")
    return parsed, host


def _static_check(url: str) -> tuple[SplitResult, str, bool]:
    """Scheme/host/config/IP-literal checks shared by both tool entry points.

    Returns `(parsed, host, done)`; `done` means the URL is accepted without
    DNS (guard disabled, allowlisted host or public IP literal).
    """
    parsed, host = _parse(url)

    if _guard_disabled():
        logger.warning(
            "ssrf.guard_disabled",
            host=host,
            hint=f"{SSRF_DISABLED_ENV} is set — never do this outside development",
        )
        return parsed, host, True

    allowed = _allowed_hosts()
    if allowed:
        if host in allowed:
            return parsed, host, True
        raise SSRFError(f"host is not in {ALLOWED_HOSTS_ENV}: {host}")

    return parsed, host, _address_check(host)


def _address_check(host: str) -> bool:
    """Internal-name and IP-literal rules. Returns True when `host` is an
    address (already judged), False when it is a name that needs DNS.
    """
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        raise SSRFError(f"blocked internal host: {host}")

    try:
        # An IP literal never hits DNS; check it as-is.
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _assert_public_ip(host, host)
        return True

    # Short, hex, octal and integer IPv4 forms (`127.1`, `0x7f000001`,
    # `2130706433`) are not `ipaddress` literals but resolvers and HTTP
    # clients still treat them as addresses. Judge the dotted form.
    try:
        dotted = socket.inet_ntoa(socket.inet_aton(host))
    except (OSError, UnicodeError):
        return False
    _assert_public_ip(host, dotted)
    return True


def validate_tool_endpoint(url: str) -> None:
    """Raise `SSRFError` unless `url` passes the static checks. No DNS.

    Used when a tool is created or updated; a hostname is accepted here and
    resolved and checked again by `assert_safe_url` on every call.
    """
    _static_check(url)


async def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to. Blocking call, so off the event loop."""
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise SSRFError(f"could not resolve host: {host}") from exc
    return [cast(str, info[4][0]) for info in infos]


async def assert_safe_url(url: str) -> None:
    """Raise `SSRFError` unless `url` is an external http(s) URL.

    Checks *every* address the hostname resolves to, so a DNS record with one
    public and one private answer is still rejected.

    TOCTOU limitation (accepted for MVP): httpx re-resolves the hostname when
    it connects, so a DNS-rebinding attacker could return a private address on
    that second lookup. Closing the gap means pinning the validated IP into the
    connection (custom transport / `httpx` resolver hook). What we do instead
    is disable redirects on the request, which removes the much easier attack
    of a public host 302-ing to `http://169.254.169.254`.
    """
    parsed, host, done = _static_check(url)
    if done:
        return
    await _assert_resolves_public(parsed, host)


async def _assert_resolves_public(parsed: SplitResult, host: str) -> None:
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    for address in await _resolve(host, port):
        _assert_public_ip(host, address)


# --- channel endpoints -----------------------------------------------------
#
# A tenant also controls the `base_url` of its Evolution channels (PRODUCT.md
# F1). Same address rules, different configuration:
#
# - `CHANNEL_ALLOWED_HOSTS` (`Settings.channel_allowed_hosts`) is additive:
#   a listed host skips the address checks (how an operator points a channel
#   at a private Evolution server on purpose); every other host still gets
#   the full checks.
# - `TOOL_ALLOWED_HOSTS` and `TOOL_SSRF_DISABLED` have no effect here.


def _channel_allowed_hosts() -> set[str]:
    raw = get_settings().channel_allowed_hosts
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _channel_static_check(url: str) -> tuple[SplitResult, str, bool]:
    """Static checks for a channel `base_url`. Returns `(parsed, host, done)`
    like `_static_check`; `host` is the name as written (what DNS resolves).
    """
    parsed, host = _parse(url)
    try:
        parsed.port  # noqa: B018 - raises on an out-of-range or non-numeric port
    except ValueError as exc:
        raise SSRFError("URL has an invalid port") from exc
    # `localhost.`, `10.0.0.5.` and `metadata.google.internal.` name the same
    # targets as without the dot, so the rules judge the name without it (and
    # a listed `evolution` matches `evolution.`). Channel path only: the tool
    # checks keep their behaviour (PRODUCT.md, out of scope).
    judged = host.rstrip(".")
    if not judged:
        raise SSRFError("URL has no host")
    if judged in _channel_allowed_hosts():
        return parsed, host, True
    return parsed, host, _address_check(judged)


def validate_channel_base_url(url: str) -> None:
    """Raise `SSRFError` unless a channel `base_url` passes the static checks.

    No DNS. Used when a channel is created or updated; a hostname is accepted
    here and resolved and checked by `assert_safe_channel_url` on every call.
    """
    _channel_static_check(url)


async def assert_safe_channel_url(url: str) -> None:
    """Raise `SSRFError` unless a channel `base_url` is safe to call now.

    Static checks plus every resolved address must be public (same TOCTOU
    limitation as `assert_safe_url`; callers disable redirects).
    """
    parsed, host, done = _channel_static_check(url)
    if done:
        return
    await _assert_resolves_public(parsed, host)
