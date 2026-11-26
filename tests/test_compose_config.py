"""The shipped compose file must not hardcode the tool or channel host lists:
they come from `.env` (or from an override file), never from `docker-compose.yml`."""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
HOST_SETTINGS = ("TOOL_ALLOWED_HOSTS", "CHANNEL_ALLOWED_HOSTS")


def _environment(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return env


def test_api_and_worker_hardcode_no_host_list():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    for name in ("api", "worker"):
        env = _environment(services[name])
        for setting in HOST_SETTINGS:
            value = str(env.get(setting, ""))
            assert value == "" or value.startswith("${"), f"{name} hardcodes {setting}: {value}"


def test_tool_hosts_come_from_the_environment():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    for name in ("api", "worker"):
        assert _environment(services[name]).get("TOOL_ALLOWED_HOSTS") == "${TOOL_ALLOWED_HOSTS:-}"
