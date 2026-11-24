"""The agent docs must not name anything the code does not have. Needs no database."""

import re

import pytest

from app.api.agents import AgentCreate
from app.config import Settings
from app.main import app
from tests.docs_drift import (
    AGENT_DOCS,
    HIDDEN_ROUTES,
    NON_SETTINGS_NAMES,
    PENDING_GUIDES,
    ROOT,
    endpoints_in,
    env_vars_in,
    normalize_path,
)

GUIDES = ROOT / "docs" / "guides"


def _routes() -> set[tuple[str, str]]:
    routes = set(HIDDEN_ROUTES)
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            routes.add((method.upper(), normalize_path(path)))
    return routes


def test_helpers_find_endpoints_and_variables():
    text = "`GET /a/{x}/b` and `LLM_API_KEY` and `DEMO_MODE=true` and `plain` and `ONE`\n"
    text += "```\nDATABASE_URL=x\nHUMAN_HANDOFF\n```\n"
    assert endpoints_in(text) == [("GET", "/a/{}/b")]
    assert env_vars_in(text) == {"LLM_API_KEY", "DEMO_MODE", "DATABASE_URL"}


def test_endpoints_exist():
    routes = _routes()
    missing = [
        f"{doc.name}: {m} {p}"
        for doc in AGENT_DOCS
        for m, p in endpoints_in(doc.read_text())
        if (m, p) not in routes
    ]
    assert not missing, missing


def test_env_vars_are_settings():
    known = {name.upper() for name in Settings.model_fields} | NON_SETTINGS_NAMES
    unknown = [
        f"{doc.name}: {v}"
        for doc in AGENT_DOCS
        for v in sorted(env_vars_in(doc.read_text()))
        if v not in known
    ]
    assert not unknown, unknown


def test_configuration_guide_lists_every_setting():
    guide = env_vars_in((GUIDES / "configuration.md").read_text())
    missing = [n.upper() for n in Settings.model_fields if n.upper() not in guide]
    assert not missing, missing


def test_every_agent_field_is_documented():
    if not (GUIDES / "setup.md").exists() or not (GUIDES / "agent-behaviour.md").exists():
        pytest.skip("setup.md and agent-behaviour.md are not written yet")
    text = "\n".join(p.read_text() for p in GUIDES.glob("*.md"))
    missing = [f for f in AgentCreate.model_fields if f"`{f}`" not in text]
    assert not missing, missing


def test_relative_links_resolve():
    broken = []
    for doc in AGENT_DOCS:
        for target in re.findall(r"\]\(([^)\s]+)\)", doc.read_text()):
            target = target.split("#", 1)[0]
            if not target or "://" in target or target.startswith(("/", "mailto:")):
                continue  # anchors, URLs and absolute paths such as /openapi.json
            path = (doc.parent / target).resolve()
            if path.exists():
                continue
            if path.parent == GUIDES and path.stem in PENDING_GUIDES:
                continue
            broken.append(f"{doc.name}: {target}")
    assert not broken, broken
