"""Text checks on docker/Dockerfile; no Docker needed."""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "Dockerfile"


def _lines() -> list[str]:
    return [
        line.strip()
        for line in DOCKERFILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_no_curl() -> None:
    assert "curl" not in "\n".join(_lines()).lower()


def test_no_piped_install_script() -> None:
    assert not re.search(r"\|\s*(ba)?sh\b", "\n".join(_lines()))


def test_user_is_not_root_and_after_last_run() -> None:
    lines = _lines()
    users = [i for i, line in enumerate(lines) if line.startswith("USER ")]
    runs = [i for i, line in enumerate(lines) if line.startswith("RUN ")]
    assert users, "no USER line"
    last_user = users[-1]
    assert lines[last_user].split()[1].split(":")[0] not in ("root", "0")
    assert last_user > max(runs)


def test_uv_image_has_exact_version_tag() -> None:
    match = re.search(r"--from=ghcr\.io/astral-sh/uv:(\S+)\s", "\n".join(_lines()) + " ")
    assert match, "uv is not copied from the pinned image"
    assert re.fullmatch(r"\d+\.\d+\.\d+", match.group(1))
