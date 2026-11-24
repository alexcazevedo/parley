"""Checks the shape of the compose files. Needs no Docker and no database."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())


@pytest.fixture(scope="module")
def services() -> dict:
    return _load("docker-compose.yml")["services"]


@pytest.fixture(scope="module")
def dev_services() -> dict:
    return _load("docker-compose.dev.yml")["services"]


def _command(service: dict) -> str:
    command = service["command"]
    return command if isinstance(command, str) else " ".join(command)


def test_migrate_runs_alembic_once(services):
    migrate = services["migrate"]
    assert "alembic upgrade head" in _command(migrate)
    assert migrate["restart"] == "no"
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_api_command_does_not_migrate(services):
    assert "alembic" not in _command(services["api"])
    assert "uvicorn" in _command(services["api"])


@pytest.mark.parametrize("name", ["api", "worker"])
def test_waits_for_migrate(services, name):
    depends = services[name]["depends_on"]
    assert depends["migrate"]["condition"] == "service_completed_successfully"
    assert depends["postgres"]["condition"] == "service_healthy"
    assert depends["redis"]["condition"] == "service_healthy"


def test_api_keeps_its_port(services):
    assert services["api"]["ports"]


@pytest.mark.parametrize("name", ["postgres", "redis"])
def test_datastores_not_published_by_default(services, name):
    assert "ports" not in services[name]


@pytest.mark.parametrize("name", ["postgres", "redis"])
def test_dev_file_publishes_datastores(dev_services, name):
    assert dev_services[name]["ports"]


def test_dev_file_holds_only_those_ports(dev_services):
    assert set(dev_services) == {"postgres", "redis"}
    for service in dev_services.values():
        assert set(service) == {"ports"}
