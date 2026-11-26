"""README and AGENTS.md stay short and point at the guides."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GUIDES = [
    "quickstart",
    "configuration",
    "setup",
    "manifest",
    "messaging",
    "operators",
    "project-channel",
    "business-tools",
    "agent-behaviour",
    "api-conventions",
    "operations",
]


def test_line_limits():
    assert len((ROOT / "README.md").read_text().splitlines()) <= 200
    assert len((ROOT / "AGENTS.md").read_text().splitlines()) <= 120


def test_vendezap_brief_is_short():
    text = (ROOT / "docs" / "integrations" / "vendezap.md").read_text()
    assert len(text.splitlines()) <= 150


def test_no_history_references():
    for name in ("README.md", "AGENTS.md"):
        text = (ROOT / name).read_text()
        for pattern in (r"ticket \d", r"PRODUCT\.md", r"PRD"):
            assert not re.search(pattern, text), f"{name} matches {pattern}"


def test_readme_links_llms_txt_and_guides():
    text = (ROOT / "README.md").read_text()
    assert "llms.txt" in text
    for name in GUIDES:
        assert f"docs/guides/{name}.md" in text


def test_architecture_is_short_and_has_no_history_references():
    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    assert len(text.splitlines()) <= 930
    for pattern in (r"ticket \d", r"PRODUCT\.md", r"PRD"):
        assert not re.search(pattern, text), f"ARCHITECTURE.md matches {pattern}"


def test_follow_ups_has_open_now_index():
    text = (ROOT / "docs" / "FOLLOW_UPS.md").read_text()
    assert re.search(r"^## Open now$", text, re.MULTILINE)


def test_prd_is_tracked():
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "PRD.md"],
        cwd=ROOT,
        capture_output=True,
    )
    assert tracked.returncode == 0
