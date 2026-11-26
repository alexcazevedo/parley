"""Set parley's production secrets on its two Dokploy apps, without showing them.

Run it yourself, in your own terminal:

    DOKPLOY_API_KEY=... python3 scripts/dokploy_set_secrets.py --llm-from-vendezap

What it does, in order:

1. Takes the model settings. With `--llm-from-vendezap` it copies
   `OPENAI_API_KEY`, `OPENAI_API_URL` and `OPENAI_MODEL` from the vendezap app
   in Dokploy to `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL`. Without the
   flag it reads `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` and
   `LLM_MAX_TOKENS_PARAM` from the local `.env`. Placeholder values are refused.
2. Asks Dokploy for the shared Postgres and Redis services (`shared-infra`).
3. Creates the `parley` database and role with vps-infra's
   `scripts/provision-db.sh` (skipped when `DATABASE_URL` is already in the
   environment, for example on a second run).
4. Generates `ADMIN_API_KEY` and `SECRETS_KEY` (a Fernet key), unless the api
   app already has them: a new `SECRETS_KEY` would make stored secrets
   unreadable.
5. Merges everything into the environment of `parley-api` and
   `parley-worker`, keeping the variables already there.

Only the model settings are copied from vendezap. Its `DATABASE_URL` is never
reused: both projects keep their migration state in a table named
`alembic_version`, so sharing one database would corrupt both.

It prints variable names only, never a value. Standard library only.

Environment:
  DOKPLOY_API_KEY  required
  DOKPLOY_URL      default https://dokploy.site.plugu.solutions
  PG_ADMIN_URL     optional; an admin URL this machine can reach. Without it,
                   psql runs inside the Postgres container over SSH
  PG_SSH_HOST      default plugu-prod01
  DATABASE_URL     optional; an already provisioned URL, skips step 3
  REDIS_INDEX      default 2 (see vps-infra docs/SHARED_REDIS.md)
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VPS_INFRA = Path.home() / "dev" / "vps-infra"
API_APP = "-6s6VA8yXzQOos13jVmJl"
WORKER_APP = "h3dd6vx5SBUCoDt80O8qo"
POSTGRES_ID = "saIpnX_itd9MEJd5zSFWN"
REDIS_ID = "Mpug9Gy6wB2DkHJOFpZA5"
VENDEZAP_APP = "TqPzkCuINkgitncZXBzgm"
VENDEZAP_LLM_NAMES = {
    "OPENAI_API_KEY": "LLM_API_KEY",
    "OPENAI_API_URL": "LLM_BASE_URL",
    "OPENAI_MODEL": "LLM_MODEL",
}
LLM_NAMES = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_MAX_TOKENS_PARAM")
PLACEHOLDER_MARKS = ("changeme", "your-", "your_", "<", "xxx", "placeholder")

BASE = os.environ.get("DOKPLOY_URL", "https://dokploy.site.plugu.solutions").rstrip("/")


def die(message: str) -> None:
    sys.exit(f"error: {message}")


def api(method: str, path: str, body: dict | None = None, query: dict | None = None):
    url = f"{BASE}/api/{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "x-api-key": os.environ["DOKPLOY_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        # The body of a Dokploy error names the problem and holds no secret we sent.
        die(f"{method} {path}: HTTP {error.code} {error.read()[:300].decode(errors='replace')}")


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


def checked_llm_settings(
    found: dict[str, str],
    source: str,
    required: tuple[str, ...] = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"),
) -> dict[str, str]:
    for name in required:
        value = found.get(name, "")
        if not value:
            die(f"{name} is not set in {source}")
        if name == "LLM_API_KEY" and any(mark in value.lower() for mark in PLACEHOLDER_MARKS):
            die(f"LLM_API_KEY in {source} is a placeholder; put the real key there first")
    return found


def local_llm_settings() -> dict[str, str]:
    env_file = ROOT / ".env"
    if not env_file.exists():
        die(f"{env_file} does not exist")
    local = parse_env(env_file.read_text())
    found = {name: local[name] for name in LLM_NAMES if local.get(name)}
    return checked_llm_settings(found, "the local .env")


def vendezap_llm_settings() -> dict[str, str]:
    app = api("GET", "application.one", query={"applicationId": VENDEZAP_APP})
    theirs = parse_env(app.get("env") or "")
    found = {ours: theirs[name] for name, ours in VENDEZAP_LLM_NAMES.items() if theirs.get(name)}
    # Vendezap may rely on its defaults for the URL and the model; parley's
    # own defaults (the OpenAI URL, `gpt-4o-mini`) then apply.
    return checked_llm_settings(found, "the vendezap app in Dokploy", required=("LLM_API_KEY",))


def psql_over_ssh(service: str) -> str:
    """A directory holding a `psql` that runs inside the Postgres container.

    It drops `-h` and `-p` (the container's own socket is used, which the
    official image trusts for local connections, so no password travels) and
    passes everything else, with the SQL on stdin, through `ssh` and
    `docker exec`.
    """
    host = os.environ.get("PG_SSH_HOST", "plugu-prod01")
    folder = Path(tempfile.mkdtemp(prefix="parley-psql-"))
    shim = folder / "psql"
    shim.write_text(
        f"""#!/usr/bin/env python3
import shlex, subprocess, sys
args, skip = [], False
for arg in sys.argv[1:]:
    if skip:
        skip = False
    elif arg in ("-h", "-p"):
        skip = True
    else:
        args.append(arg)
remote = (
    'c=$(docker ps -q -f name={service} | head -1); '
    '[ -n "$c" ] || {{ echo "no running container named {service}" >&2; exit 1; }}; '
    'exec docker exec -i "$c" psql ' + " ".join(shlex.quote(a) for a in args)
)
sys.exit(subprocess.run(["ssh", {host!r}, remote]).returncode)
"""
    )
    shim.chmod(0o700)
    return str(folder)


def database_url(postgres: dict) -> str:
    """A `postgresql+asyncpg://` URL for the parley database."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        admin = os.environ.get("PG_ADMIN_URL", "")
        env = {**os.environ, "PG_APP_HOST": postgres["appName"], "PG_APP_PORT": "5432"}
        if not admin:
            # The shared Postgres listens on the Docker network only. Run psql
            # inside its container over SSH: a stand-in `psql` goes first on PATH.
            user = urllib.parse.quote(postgres["databaseUser"], safe="")
            admin = f"postgres://{user}:unused@localhost:5432/postgres"
            env["PATH"] = f"{psql_over_ssh(postgres['appName'])}{os.pathsep}{env['PATH']}"
        env["PG_ADMIN_URL"] = admin
        # stderr goes to the terminal: SSH may print a Tailscale check link there.
        result = subprocess.run(
            [str(VPS_INFRA / "scripts" / "provision-db.sh"), "parley"],
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        lines = [line for line in result.stdout.splitlines() if line.startswith("DATABASE_URL=")]
        if result.returncode != 0 or not lines:
            # stderr of the script carries no credential (see its header).
            die(
                "provision-db.sh failed (its message is above). "
                "If the database already exists, rerun with DATABASE_URL set."
            )
        url = lines[-1].removeprefix("DATABASE_URL=")
    scheme, _, rest = url.partition("://")
    if scheme not in ("postgres", "postgresql", "postgresql+asyncpg"):
        die("DATABASE_URL must be a postgres:// URL")
    return f"postgresql+asyncpg://{rest}"


def redis_url(redis: dict) -> str:
    password = urllib.parse.quote(redis["databasePassword"], safe="")
    index = os.environ.get("REDIS_INDEX", "2")
    return f"redis://:{password}@{redis['appName']}:6379/{index}"


def main() -> None:
    if not os.environ.get("DOKPLOY_API_KEY"):
        die("DOKPLOY_API_KEY is not set")

    if "--llm-from-vendezap" in sys.argv[1:]:
        values = vendezap_llm_settings()
    else:
        values = local_llm_settings()
    postgres = api("GET", "postgres.one", query={"postgresId": POSTGRES_ID})
    redis = api("GET", "redis.one", query={"redisId": REDIS_ID})
    api_app = api("GET", "application.one", query={"applicationId": API_APP})
    worker_app = api("GET", "application.one", query={"applicationId": WORKER_APP})
    current_api = parse_env(api_app.get("env") or "")

    if current_api.get("DATABASE_URL") and not os.environ.get("DATABASE_URL"):
        values["DATABASE_URL"] = current_api["DATABASE_URL"]
    else:
        values["DATABASE_URL"] = database_url(postgres)
    values["REDIS_URL"] = redis_url(redis)
    # Kept when present: both apps must share them, and data is encrypted with SECRETS_KEY.
    values["ADMIN_API_KEY"] = current_api.get("ADMIN_API_KEY") or secrets.token_urlsafe(48)
    values["SECRETS_KEY"] = (
        current_api.get("SECRETS_KEY") or base64.urlsafe_b64encode(os.urandom(32)).decode()
    )

    for name, app_id, app in (
        ("parley-api", API_APP, api_app),
        ("parley-worker", WORKER_APP, worker_app),
    ):
        merged = {**parse_env(app.get("env") or ""), **values}
        api(
            "POST",
            "application.saveEnvironment",
            body={
                "applicationId": app_id,
                "env": "\n".join(f"{key}={value}" for key, value in merged.items()),
                "buildArgs": app.get("buildArgs") or "",
                "buildSecrets": app.get("buildSecrets") or "",
                "createEnvFile": False,
            },
        )
        print(f"{name}: set {', '.join(sorted(merged))}")

    print("done. No value was printed; ADMIN_API_KEY is in the Dokploy dashboard.")


if __name__ == "__main__":
    main()
