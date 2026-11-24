import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_package_name_is_parley() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["name"] == "parley"


def test_api_title_is_parley() -> None:
    from app.main import app

    assert app.title == "Parley"


def test_no_ticket_numbers_in_app_sources() -> None:
    pattern = re.compile(r"ticket \d")
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in (ROOT / "app").rglob("*.py")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []
