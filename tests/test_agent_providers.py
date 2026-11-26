"""An agent saves only with a provider the server serves; a run records what answered."""

import copy
import os

import pytest
from sqlalchemy import func, select

from app.agents.runtime import AgentRuntime
from app.config import get_settings
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.llm.providers.native import NATIVE_PROVIDERS, key_setting
from app.models.agent import Agent
from tests import native_fakes
from tests.test_manifest_apply import MANIFEST
from tests.test_native_providers import _runs, _send, _setup, _turn

native_model = native_fakes.native_model
UNKNOWN = "unknown provider: use openai_compatible, openai, anthropic or google"
KEYS = {p: key_setting(p) for p in NATIVE_PROVIDERS}


def _not_set(provider: str) -> str:
    return f"provider '{provider}' is not configured on this server: {KEYS[provider]} is not set"


@pytest.fixture
def keys(monkeypatch):
    """Set the named providers' keys, blank the rest."""

    def apply(*enabled: str) -> None:
        for provider, setting in KEYS.items():
            monkeypatch.setenv(setting, f"sk-secret-{provider}" if provider in enabled else "")
        get_settings.cache_clear()

    apply()
    yield apply
    get_settings.cache_clear()


async def _tenant(client) -> dict:
    r = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": r.json()["api_key"]}


def _body(provider: str) -> dict:
    return {"name": "Bot", "instructions": "x", "provider": provider, "model": "m"}


@pytest.mark.parametrize("provider", NATIVE_PROVIDERS)
async def test_native_provider_needs_its_key(client, keys, provider):
    headers = await _tenant(client)
    r = await client.post("/agents", json=_body(provider), headers=headers)
    assert r.status_code == 422
    assert _not_set(provider) in r.text

    keys(provider)
    r = await client.post("/agents", json=_body(provider), headers=headers)
    assert r.status_code == 201, r.text
    assert r.json()["provider"] == provider
    assert f"sk-secret-{provider}" not in r.text


@pytest.mark.parametrize("provider", NATIVE_PROVIDERS)
async def test_update_to_a_native_provider(client, keys, provider):
    headers = await _tenant(client)
    created = await client.post("/agents", json=_body("openai_compatible"), headers=headers)
    url = f"/agents/{created.json()['id']}"
    r = await client.patch(url, json={"provider": provider}, headers=headers)
    assert r.status_code == 422
    assert _not_set(provider) in r.text
    assert f"sk-secret-{provider}" not in r.text
    assert (await client.get(url, headers=headers)).json()["provider"] == "openai_compatible"

    keys(provider)
    r = await client.patch(url, json={"provider": provider}, headers=headers)
    assert r.status_code == 200
    assert r.json()["provider"] == provider


async def test_unknown_provider_is_refused(client, keys):
    headers = await _tenant(client)
    r = await client.post("/agents", json=_body("mistral"), headers=headers)
    assert r.status_code == 422
    assert UNKNOWN in r.text
    created = await client.post("/agents", json=_body("openai_compatible"), headers=headers)
    r = await client.patch(
        f"/agents/{created.json()['id']}", json={"provider": "mistral"}, headers=headers
    )
    assert r.status_code == 422
    assert UNKNOWN in r.text


async def test_an_update_that_leaves_the_provider_alone_works(client, keys, db_session):
    headers = await _tenant(client)
    keys("openai")
    created = await client.post("/agents", json=_body("openai"), headers=headers)
    keys()  # the key is gone; the stored agent stays as it is
    r = await client.patch(
        f"/agents/{created.json()['id']}", json={"name": "Renamed"}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["provider"] == "openai"


async def test_manifest_reports_both_problems_and_writes_nothing(client, keys, db_session):
    headers = await _tenant(client)
    before = await db_session.scalar(select(func.count()).select_from(Agent))
    data = copy.deepcopy(MANIFEST)
    data["tools"] = []
    data["agents"][0]["tool_names"] = []
    data["agents"].append({**data["agents"][0], "key": "second", "provider": "mistral"})
    data["agents"][0]["provider"] = "anthropic"
    for dry_run in ("true", "false"):
        r = await client.post(f"/manifest/apply?dry_run={dry_run}", json=data, headers=headers)
        assert r.status_code == 422
        details = {d["field"]: d["message"] for d in r.json()["error"]["details"]}
        assert details == {
            "agents[0].provider": _not_set("anthropic"),
            "agents[1].provider": UNKNOWN,
        }
    assert await db_session.scalar(select(func.count()).select_from(Agent)) == before

    keys("anthropic")
    data["agents"].pop()
    r = await client.post("/manifest/apply?dry_run=false", json=data, headers=headers)
    assert r.status_code == 200, r.text


async def test_run_records_openai_compatible(client, fake_redis, db_session, keys):
    headers, channel_id, _ = await _setup(client, "openai_compatible")
    conversation_id = await _send(client, headers, channel_id, "oi")
    runtime = AgentRuntime(provider=FakeLLMProvider([LLMResponse(content="Oi!")]))
    await _turn(fake_redis, db_session, runtime)
    [run] = await _runs(db_session, conversation_id)
    assert (run.provider, run.model) == ("openai_compatible", "openai_compatible-model")


@pytest.mark.parametrize("provider", NATIVE_PROVIDERS)
async def test_run_records_a_native_provider(
    client, fake_redis, db_session, keys, native_model, provider
):
    keys(provider)
    headers, channel_id, _ = await _setup(client, provider)
    conversation_id = await _send(client, headers, channel_id, "oi")
    native_model.reply("Olá!")
    await _turn(fake_redis, db_session, AgentRuntime())
    [run] = await _runs(db_session, conversation_id)
    assert (run.provider, run.model) == (provider, f"{provider}-model")
