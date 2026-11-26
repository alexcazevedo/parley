"""Manifest version 1: the file a project applies with `POST /manifest/apply`.

Every item model is the API's own create model (`AgentCreate`, `CategoryCreate`,
`ToolCreate`) with unknown keys rejected, so the file accepts exactly what the
endpoints accept. `knowledge` is an optional section of entries and pairs.
"""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.api.agents import AgentCreate
from app.api.categories import CategoryCreate
from app.api.knowledge import EntryCreate, QACreate
from app.api.mcp_servers import McpServerCreate
from app.api.secrets import SECRET_NAME_PATTERN
from app.api.tools import ToolCreate
from app.models.channel import ChannelType

AGENT_KEY_PATTERN = r"^[a-z0-9_-]{1,64}$"
AgentKey = Annotated[str, Field(pattern=AGENT_KEY_PATTERN)]

MAX_AGENTS = 50
MAX_DEPARTMENTS = 200
MAX_TOOLS = 200
MAX_CHANNELS = 50
MAX_MCP_SERVERS = 20
MAX_KNOWLEDGE_ENTRIES = 500
MAX_KNOWLEDGE_QA = 500

NOT_A_REFERENCE = 'credentials must be secret references; use {"secret": "<name>"}'


class SecretRef(BaseModel):
    """`{"secret": "<name>"}`: a credential by the name of a tenant secret."""

    model_config = ConfigDict(extra="forbid")

    secret: str = Field(pattern=SECRET_NAME_PATTERN)


def is_secret_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"secret"}
        and isinstance(value["secret"], str)
        and re.match(SECRET_NAME_PATTERN, value["secret"]) is not None
    )


class ManifestAgent(AgentCreate):
    """An agent; `key` names it in the file (agent names are not unique)."""

    model_config = ConfigDict(extra="forbid")

    key: AgentKey


class ManifestDepartment(CategoryCreate):
    """A department (a category); identified by `name`."""

    model_config = ConfigDict(extra="forbid")


class ManifestTool(ToolCreate):
    """An HTTP tool; identified by `name`."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("auth_config")
    @classmethod
    def _token_is_a_reference(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        # The error never carries the input: a refused value may be a real token.
        if value is not None and "token" in value and not is_secret_ref(value["token"]):
            raise PydanticCustomError("secret_reference", NOT_A_REFERENCE)
        return value


class ManifestMcpServer(McpServerCreate):
    """An MCP server; identified by `name`. The token is a secret reference."""

    model_config = ConfigDict(extra="forbid")

    # Replaces the parent's check (a plain token string), same method name.
    @field_validator("auth_config")
    @classmethod
    def _auth(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        if str(value.get("type") or "").lower() not in ("bearer", "header"):
            raise ValueError("auth_config.type must be 'bearer' or 'header'")
        # The error never carries the input: a refused value may be a real token.
        if not is_secret_ref(value.get("token")):
            raise PydanticCustomError("secret_reference", NOT_A_REFERENCE)
        return value


class ManifestChannel(BaseModel):
    """A channel; `key` names it in the file, `agent` is an agent key of the file."""

    model_config = ConfigDict(extra="forbid")

    key: AgentKey
    type: ChannelType
    name: str
    agent: AgentKey
    config: dict[str, Any] = Field(default_factory=dict)
    secret: SecretRef | None = None

    @field_validator("secret", mode="before")
    @classmethod
    def _secret_is_a_reference(cls, value: Any) -> Any:
        if value is not None and not is_secret_ref(value):
            raise PydanticCustomError("secret_reference", NOT_A_REFERENCE)
        return value


class ManifestKnowledgeEntry(EntryCreate):
    """A knowledge entry; identified by `title`."""

    model_config = ConfigDict(extra="forbid")


class ManifestKnowledgeQA(QACreate):
    """A question-and-answer pair; identified by `question`."""

    model_config = ConfigDict(extra="forbid")


class ManifestKnowledge(BaseModel):
    """Entries and pairs of the knowledge base. Documents are uploaded through the API."""

    model_config = ConfigDict(extra="forbid")

    entries: list[ManifestKnowledgeEntry] = Field(
        default_factory=list, max_length=MAX_KNOWLEDGE_ENTRIES
    )
    qa: list[ManifestKnowledgeQA] = Field(default_factory=list, max_length=MAX_KNOWLEDGE_QA)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    agents: list[ManifestAgent] = Field(default_factory=list, max_length=MAX_AGENTS)
    departments: list[ManifestDepartment] = Field(
        default_factory=list, max_length=MAX_DEPARTMENTS
    )
    tools: list[ManifestTool] = Field(default_factory=list, max_length=MAX_TOOLS)
    channels: list[ManifestChannel] = Field(default_factory=list, max_length=MAX_CHANNELS)
    # None: the file says nothing about MCP servers, so none is touched.
    mcp_servers: list[ManifestMcpServer] | None = Field(default=None, max_length=MAX_MCP_SERVERS)
    # None: the file says nothing about knowledge, so none is touched.
    knowledge: ManifestKnowledge | None = None
