"""Guard: every Python module under app/ must be tracked by git.

An over-broad .gitignore rule once hid the app/media/ package; nothing noticed
until a fresh clone failed to import it. Needs no database.
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def untracked_python_modules(repo_root: Path) -> list[str]:
    """Repo-relative POSIX paths of app/**/*.py that git does not track.

    Index-only (staged) files count as tracked; ignored files count as
    untracked, so --exclude-standard is deliberately not used.
    """
    out = subprocess.run(
        ["git", "ls-files", "app"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tracked = set(out.splitlines())
    found = (
        p.relative_to(repo_root).as_posix()
        for p in (repo_root / "app").rglob("*.py")
        if "__pycache__" not in p.parts
    )
    return sorted(path for path in found if path not in tracked)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_all_app_modules_are_tracked():
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    repo_root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("not inside a git checkout")
    missing = untracked_python_modules(repo_root)
    assert missing == [], (
        f"Python modules under app/ are not tracked by git: {missing}. "
        "Check .gitignore for an over-broad rule, or `git add` them."
    )


def test_guard_detects_ignored_module(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("media/\n")
    (tmp_path / "app" / "media").mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "media" / "stt.py").write_text("")
    _git(tmp_path, "add", "app/__init__.py")
    assert untracked_python_modules(tmp_path) == ["app/media/stt.py"]
