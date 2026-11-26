"""Create the vendezap tenant on production parley and hand its keys to vendezap.

Run it yourself, in your own terminal:

    DOKPLOY_API_KEY=... python3 scripts/setup_vendezap_tenant.py

What it does, in order:

1. Reads `ADMIN_API_KEY` from the `parley-api` app in Dokploy.
2. First run: creates the tenant `vendezap` (`POST /tenants`).
   Later runs: reuses the tenant key already stored on the vendezap app, so
   the script is safe to run again after editing the manifest.
3. Dry-runs, then applies `deploy/vendezap.yaml` (`POST /manifest/apply`):
   agent, departments and the `external` channel that pushes to vendezap.
4. Merges `PARLEY_URL`, `PARLEY_API_KEY`, `PARLEY_CHANNEL_ID` and
   `PARLEY_EVENTS_SECRET` into the environment of the vendezap app in Dokploy,
   keeping every variable already there. It does not redeploy vendezap: the
   values take effect on vendezap's next deploy.
5. Writes the same four values to `~/.config/parley/vendezap-production.env`
   (mode 600). The tenant key and the events secret are shown by parley only
   once, so this file is the backup copy.

It prints variable names and the planned changes, never a secret value.
Standard library only.

Environment:
  DOKPLOY_API_KEY  required
  DOKPLOY_URL      default https://dokploy.site.plugu.solutions
  PARLEY_URL       default https://parley.site.plugu.solutions
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "deploy" / "vendezap.yaml"
BACKUP = Path.home() / ".config" / "parley" / "vendezap-production.env"
PARLEY_API_APP = "-6s6VA8yXzQOos13jVmJl"
VENDEZAP_APP = "TqPzkCuINkgitncZXBzgm"
CHANNEL_KEY = "vendezap"
TENANT_NAME = "vendezap"

DOKPLOY = os.environ.get("DOKPLOY_URL", "https://dokploy.site.plugu.solutions").rstrip("/")
PARLEY = os.environ.get("PARLEY_URL", "https://parley.site.plugu.solutions").rstrip("/")


def die(message: str) -> None:
    sys.exit(f"error: {message}")


def call(method: str, url: str, headers: dict[str, str], body: bytes | None = None):
    request = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        # Error bodies of both services name the problem and echo no secret.
        shown = url.split("?")[0]
        die(f"{method} {shown}: HTTP {error.code} {error.read()[:600].decode(errors='replace')}")
    except urllib.error.URLError as error:
        die(f"{method} {url.split('?')[0]}: {error.reason}")


def dokploy(method: str, path: str, body: dict | None = None, query: dict | None = None):
    url = f"{DOKPLOY}/api/{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return call(
        method,
        url,
        {"x-api-key": os.environ["DOKPLOY_API_KEY"], "Content-Type": "application/json"},
        json.dumps(body).encode() if body is not None else None,
    )


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def apply_manifest(tenant_key: str, *, dry_run: bool) -> dict:
    return call(
        "POST",
        f"{PARLEY}/manifest/apply?dry_run={'true' if dry_run else 'false'}",
        {"X-API-Key": tenant_key, "Content-Type": "application/yaml"},
        MANIFEST.read_bytes(),
    )


def show(result: dict) -> None:
    for change in result["changes"]:
        fields = f" ({', '.join(change['fields'])})" if change["fields"] else ""
        print(f"  {change['action']:9} {change['kind']} {change['key']}{fields}")


def write_backup(values: dict[str, str]) -> None:
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.touch(mode=0o600, exist_ok=True)
    BACKUP.chmod(0o600)
    BACKUP.write_text("".join(f"{key}={value}\n" for key, value in values.items()))


def main() -> None:
    if not os.environ.get("DOKPLOY_API_KEY"):
        die("DOKPLOY_API_KEY is not set")

    parley_app = dokploy("GET", "application.one", query={"applicationId": PARLEY_API_APP})
    admin_key = parse_env(parley_app.get("env") or "").get("ADMIN_API_KEY", "")
    if not admin_key:
        die("parley-api has no ADMIN_API_KEY; run scripts/dokploy_set_secrets.py first")

    vendezap_app = dokploy("GET", "application.one", query={"applicationId": VENDEZAP_APP})
    vendezap_env = parse_env(vendezap_app.get("env") or "")
    known = {name: value for name, value in vendezap_env.items() if name.startswith("PARLEY_")}
    if not known.get("PARLEY_API_KEY") and BACKUP.exists():
        # A first run that created the tenant but could not reach vendezap's settings.
        known = parse_env(BACKUP.read_text())

    tenant_key = known.get("PARLEY_API_KEY", "")
    if tenant_key:
        print("tenant: reusing the key already stored")
    else:
        created = call(
            "POST",
            f"{PARLEY}/tenants",
            {"X-Admin-Key": admin_key, "Content-Type": "application/json"},
            json.dumps({"name": TENANT_NAME}).encode(),
        )
        tenant_key = created["api_key"]
        print(f"tenant: created {created['id']}")
        # Saved at once: parley never shows this key again.
        write_backup({"PARLEY_URL": PARLEY, "PARLEY_API_KEY": tenant_key})

    print("dry run:")
    show(apply_manifest(tenant_key, dry_run=True))
    result = apply_manifest(tenant_key, dry_run=False)
    print("applied:")
    show(result)

    channel = next(
        change
        for change in result["changes"]
        if change["kind"] == "channel" and change["key"] == CHANNEL_KEY
    )
    generated = result.get("generated", {}).get("channels", {}).get(CHANNEL_KEY, {})
    events_secret = generated.get("events_secret") or known.get("PARLEY_EVENTS_SECRET", "")
    if not events_secret:
        die(
            "the channel exists but its events secret is not stored anywhere; it cannot be "
            "read again. Rename the channel key in deploy/vendezap.yaml to get a new one."
        )

    values = {
        "PARLEY_URL": PARLEY,
        "PARLEY_API_KEY": tenant_key,
        "PARLEY_CHANNEL_ID": channel["id"],
        "PARLEY_EVENTS_SECRET": events_secret,
    }
    write_backup(values)
    print(f"backup: {BACKUP} (mode 600)")

    dokploy(
        "POST",
        "application.saveEnvironment",
        body={
            "applicationId": VENDEZAP_APP,
            "env": "\n".join(f"{k}={v}" for k, v in {**vendezap_env, **values}.items()),
            "buildArgs": vendezap_app.get("buildArgs") or "",
            "buildSecrets": vendezap_app.get("buildSecrets") or "",
            "createEnvFile": bool(vendezap_app.get("createEnvFile")),
        },
    )
    print(f"vendezap: set {', '.join(values)}")
    print("done. Redeploy vendezap for the values to take effect; no value was printed.")


if __name__ == "__main__":
    main()
