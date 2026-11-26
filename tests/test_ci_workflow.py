"""Checks the shape of the CI workflow file. Needs no database."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _runs(workflow: dict) -> list[str]:
    return [s["run"] for s in workflow["jobs"]["check"]["steps"] if "run" in s]


def test_commands_run_in_order(workflow):
    runs = _runs(workflow)
    wanted = ["uv run ruff check .", "uv run mypy", "uv run pytest -q"]
    assert [r for r in runs if r in wanted] == wanted


def test_uv_sync_is_frozen_and_comes_first(workflow):
    runs = _runs(workflow)
    assert "uv sync --frozen" in runs
    assert runs.index("uv sync --frozen") < runs.index("uv run ruff check .")


def test_postgres_service_maps_port_5434(workflow):
    ports = workflow["jobs"]["check"]["services"]["postgres"]["ports"]
    assert any(str(p).startswith("5434:") for p in ports)


def test_no_pull_request_trigger(workflow):
    # YAML parses the key `on` as boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert "push" in triggers
    assert "pull_request" not in triggers
