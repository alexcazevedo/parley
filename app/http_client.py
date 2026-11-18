"""The process-wide HTTP client for every outbound call (model, speech-to-text,
channel, HTTP tools).

The API and the worker each hold one. Because it is shared, it carries no
timeout of its own: every call passes `timeout=` per request. Redirects stay
off (they are the cheap way around the SSRF check, see `app.tools.ssrf`).

Its cookie jar accepts nothing. httpx stores every `Set-Cookie` by default and
sends it on later requests to the same host; with one client shared by every
tenant, one tenant's session cookie would go out on another tenant's call. A
caller that needs a cookie passes an explicit `Cookie` header per request.
"""

from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

_client: httpx.AsyncClient | None = None


def _no_cookies_jar() -> CookieJar:
    """A jar whose policy refuses every cookie, so none is stored or sent."""
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


def shared_client() -> httpx.AsyncClient:
    """The client of this process; created on first use and again after a close."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(follow_redirects=False, cookies=_no_cookies_jar())
    return _client


async def close_shared_client() -> None:
    """Close the client if there is one. Safe to call twice."""
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()
