"""Operator boundary floor: file loading, merge into effective boundaries, startup."""

import json
from types import SimpleNamespace

import pytest

from app.agents.boundaries import (
    DEFAULT_REFUSAL_MESSAGE,
    BoundaryConfigError,
    Floor,
    effective_boundaries,
    load_floor,
)
from app.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _clear_floor_cache():
    load_floor.cache_clear()
    yield
    load_floor.cache_clear()


def _agent(scope=None, never=None, check=True, refusal=None):
    return SimpleNamespace(
        boundary_scope=scope,
        boundary_never=never or [],
        boundary_check_enabled=check,
        boundary_refusal_message=refusal,
    )


def _floor_settings(tmp_path, content, **extra) -> Settings:
    path = tmp_path / "floor.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return Settings(boundary_floor_file=str(path), **extra)


def test_floor_applies_to_agent_without_boundaries(tmp_path):
    s = _floor_settings(tmp_path, {"scope": "support", "never": ["politics"]})
    b = effective_boundaries(_agent(), s)
    assert b is not None
    assert b.scopes == ("support",)
    assert b.never == ("politics",)
    assert b.check_enabled is True
    assert b.refusal_message == DEFAULT_REFUSAL_MESSAGE


def test_merge_order_and_no_duplicates(tmp_path):
    s = _floor_settings(tmp_path, {"scope": "floor scope", "never": ["a", "b"]})
    b = effective_boundaries(_agent(scope="agent scope", never=["b", "c"], refusal="Nope."), s)
    assert b.scopes == ("floor scope", "agent scope")
    assert b.never == ("a", "b", "c")
    assert b.refusal_message == "Nope."


def test_floor_without_scope_skips_it(tmp_path):
    s = _floor_settings(tmp_path, {"never": ["a"]})
    assert effective_boundaries(_agent(scope="mine"), s).scopes == ("mine",)


def test_check_cannot_be_switched_off_under_floor(tmp_path):
    s = _floor_settings(tmp_path, {"never": ["a"]}, boundary_check_enabled=False)
    assert effective_boundaries(_agent(check=False), s).check_enabled is True
    assert effective_boundaries(_agent(scope="x", check=False), s).check_enabled is True


def test_agent_flag_respected_without_floor():
    b = effective_boundaries(_agent(scope="x", check=False), Settings())
    assert b.check_enabled is False


def test_nothing_set_gives_none():
    assert effective_boundaries(_agent(), Settings()) is None
    assert effective_boundaries(_agent(), Settings(boundary_floor_file="")) is None


def test_empty_object_floor_is_empty(tmp_path):
    s = _floor_settings(tmp_path, {})
    assert effective_boundaries(_agent(), s) is None


def test_empty_path_is_empty_floor():
    assert load_floor("") == Floor(scope=None, never=())


def test_load_floor_caches(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"never": ["a"]}))
    first = load_floor(str(path))
    path.write_text(json.dumps({"never": ["changed"]}))
    assert load_floor(str(path)) is first


def test_default_settings_use_get_settings(tmp_path, monkeypatch):
    from app.config import get_settings

    path = tmp_path / "f.json"
    path.write_text(json.dumps({"never": ["a"]}))
    monkeypatch.setenv("BOUNDARY_FLOOR_FILE", str(path))
    get_settings.cache_clear()
    try:
        assert effective_boundaries(_agent()).never == ("a",)
    finally:
        monkeypatch.delenv("BOUNDARY_FLOOR_FILE")
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "content",
    [
        "not json {",
        "[]",
        '{"scope": ""}',
        '{"scope": 3}',
        '{"never": "x"}',
        '{"never": [""]}',
        '{"never": [1]}',
        '{"other": 1}',
    ],
)
def test_bad_file_raises_naming_file(tmp_path, content):
    path = tmp_path / "floor.json"
    path.write_text(content)
    with pytest.raises(BoundaryConfigError, match="floor.json"):
        load_floor(str(path))


def test_missing_file_raises(tmp_path):
    with pytest.raises(BoundaryConfigError, match="nope.json"):
        load_floor(str(tmp_path / "nope.json"))
    with pytest.raises(BoundaryConfigError):
        effective_boundaries(_agent(), Settings(boundary_floor_file=str(tmp_path / "nope.json")))


def test_create_app_with_broken_floor_raises(tmp_path):
    path = tmp_path / "floor.json"
    path.write_text("nope")
    with pytest.raises(BoundaryConfigError):
        create_app(Settings(demo_mode=True, boundary_floor_file=str(path)))


def test_create_app_with_good_floor(tmp_path):
    s = _floor_settings(tmp_path, {"never": ["a"]}, demo_mode=True)
    create_app(s)
