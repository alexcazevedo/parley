"""SSRF guard for outbound customer-tool HTTP calls.

A tenant controls the `endpoint` of every one of its tools, so an endpoint is
attacker-controlled input from the harness's point of view: without this check
a tenant could point a tool at `http://169.254.169.254/...` (cloud metadata),
at Redis/Postgres on the private network, or at `file://`.

Configuration is read from `os.environ` at call time rather than from
`app.config.Settings`:

- `TOOL_ALLOWED_HOSTS` — comma-separated hostname allowlist. When set it is
  authoritative: listed hosts skip the IP checks (that is how you point tools
  at an internal service on purpose), everything else is rejected outright.
- `TOOL_SSRF_DISABLED=true` — disables the guard entirely. Development only;
  every call logs a loud warning.

(These belong in `Settings` eventually; they live here because ticket 06 may
not touch `app/config.py` while other tickets are editing it in parallel.)
"""

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

import structlog

logger = structlog.get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_HOSTS_ENV = "TOOL_ALLOWED_HOSTS"
SSRF_DISABLED_ENV = "TOOL_SSRF_DISABLED"


class SSRFError(ValueError):
    """The URL is not safe to request. Message is safe to show the model."""


def _allowed_hosts() -> set[str]:
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _guard_disabled() -> bool:
    return os.environ.get(SSRF_DISABLED_ENV, "").strip().lower() in {"1", "true", "yes"}


def _assert_public_ip(host: str, raw: str) -> None:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError as exc:  # getaddrinfo handed us something unparseable
        raise SSRFError(f"could not resolve host: {host}") from exc
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # covers the 169.254.169.254 metadata endpoint
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified  # 0.0.0.0 / ::
    ):
        raise SSRFError(f"blocked internal address for host {host}: {ip}")


async def _resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to. Blocking call, so off the event loop."""
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise SSRFError(f"could not resolve host: {host}") from exc
    return [info[4][0] for info in infos]


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
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFError(f"unsupported URL scheme: {scheme or '(none)'}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise SSRFError("URL has no host")

    if _guard_disabled():
        logger.warning(
            "ssrf.guard_disabled",
            host=host,
            hint=f"{SSRF_DISABLED_ENV} is set — never do this outside development",
        )
        return

    allowed = _allowed_hosts()
    if allowed:
        if host in allowed:
            return
        raise SSRFError(f"host is not in {ALLOWED_HOSTS_ENV}: {host}")

    try:
        # An IP literal never hits DNS; check it as-is.
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _assert_public_ip(host, host)
        return

    port = parsed.port or (443 if scheme == "https" else 80)
    for address in await _resolve(host, port):
        _assert_public_ip(host, address)
