"""Checks the Makefile's targets without touching a database."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def _make(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_test_invokes_pytest() -> None:
    result = _make("-n", "test")
    assert result.returncode == 0, result.stderr
    assert "uv run pytest" in result.stdout


def test_dry_run_check_runs_ruff_before_pytest() -> None:
    result = _make("-n", "check")
    assert result.returncode == 0, result.stderr
    assert "ruff check" in result.stdout
    assert "pytest" in result.stdout
    assert result.stdout.index("ruff check") < result.stdout.index("pytest")


def test_dry_run_check_runs_mypy_between_ruff_and_pytest() -> None:
    result = _make("-n", "check")
    assert result.returncode == 0, result.stderr
    assert result.stdout.index("ruff check") < result.stdout.index("mypy") < result.stdout.index(
        "pytest"
    )


def test_help_lists_every_public_target() -> None:
    result = _make("help")
    assert result.returncode == 0, result.stderr
    for target in ("test", "lint", "typecheck", "check", "db"):
        assert target in result.stdout


def test_dry_run_db_shows_port_test_and_compose_command() -> None:
    result = _make("-n", "db")
    assert result.returncode == 0, result.stderr
    assert "/dev/tcp/localhost/" in result.stdout
    assert "docker compose up -d --wait postgres" in result.stdout
