"""Helpers that compare the agent docs with the code.

Conventions every guide follows:
- An endpoint is written in backticks as `METHOD /path`, path parameters in
  braces: `GET /conversations/{conversation_id}/messages`.
- An environment variable is written in backticks in upper snake case:
  `LLM_API_KEY`. In a fenced block, a line `NAME=value` also names one.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

AGENT_DOCS: list[Path] = [
    ROOT / "llms.txt",
    *sorted((ROOT / "docs" / "guides").glob("*.md")),
    *[p for p in (ROOT / "README.md", ROOT / "AGENTS.md") if p.exists()],
]

# Guides linked from llms.txt but not written yet; later tickets remove names.
PENDING_GUIDES: set[str] = set()

# Routes hidden from the OpenAPI schema (include_in_schema=False).
HIDDEN_ROUTES: set[tuple[str, str]] = {
    ("GET", "/llms.txt"),
    ("GET", "/chat"),
    ("GET", "/operator"),
    ("GET", "/docs/guides/{}.md"),
    ("GET", "/docs/ARCHITECTURE.md"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
}

# Upper-snake names in the docs that are not fields of Settings.
NON_SETTINGS_NAMES: set[str] = {
    # Read by docker-compose.yml, not by the app.
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_PORT",
    "REDIS_PORT",
    "API_PORT",
    # Values of the conversation `state` field (app/models/conversation.py).
    "ACTIVE_AI",
    "HUMAN_HANDOFF",
    # Shell variables the guides set in example commands (docs/guides/setup.md,
    # quickstart.md); fenced `NAME=value` lines look like environment variables.
    "BASE_URL",
    "TENANT_ID",
    "TENANT_KEY",
    "AGENT_ID",
    "TOOL_ID",
    "CHANNEL_ID",
    "CONTACT_ID",
    # Added by messaging.md, operators.md and agent-behaviour.md.
    "USER_ID",
    "CONVERSATION_ID",
    "LAST_SEQ",
    "OPERATOR_ID",
    "CATEGORY_ID",
    "MEMORY_ID",
    "EVOLUTION_KEY",
}

_METHODS = "GET|POST|PUT|PATCH|DELETE"
_ENDPOINT_RE = re.compile(rf"`({_METHODS}) (/[^`\s]*)`")
_PARAM_RE = re.compile(r"\{[^}]*\}")
_NAME = r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
_SPAN_RE = re.compile(rf"`({_NAME})(?:=[^`]*)?`")
_FENCE_RE = re.compile(r"^```.*?^```", re.S | re.M)
_FENCE_LINE_RE = re.compile(rf"^\s*(?:export\s+)?({_NAME})=", re.M)


def normalize_path(path: str) -> str:
    """Drop a query string and replace parameter names with `{}`."""
    return _PARAM_RE.sub("{}", path.split("?", 1)[0])


def endpoints_in(text: str) -> list[tuple[str, str]]:
    """`(METHOD, path)` for each `` `METHOD /path` `` in the text, params as `{}`."""
    return [(m, normalize_path(p)) for m, p in _ENDPOINT_RE.findall(text)]


def env_vars_in(text: str) -> set[str]:
    """Upper-snake names (two or more parts) written as environment variables."""
    found: set[str] = set()
    for block in _FENCE_RE.findall(text):
        found.update(_FENCE_LINE_RE.findall(block))
    found.update(_SPAN_RE.findall(_FENCE_RE.sub("", text)))
    return found
