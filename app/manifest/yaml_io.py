"""YAML in and out for manifests. The input is untrusted.

`load` uses a `SafeLoader` (no Python tags) that also refuses anchors and
aliases (and so merge keys): a 1 MiB body cannot stop a small document that
expands to gigabytes (`a: &a [x, x]`, `b: &b [*a, *a]`, ...), and a manifest
has no use for them. The loaded value must be plain JSON data: a YAML date,
binary, set or non-string key would otherwise reach the JSONB columns.

YAML 1.1 typing still applies (`no`, `on`, `1.0` and `2024-01-01` are not
strings): the item models take strings strictly, so such a value in a string
field fails validation at its path instead of being turned into text.
"""

import math
from typing import Any

import yaml

MAX_DEPTH = 100


class YamlError(ValueError):
    """The text is not a usable manifest; `line` is 1-based when known."""

    def __init__(self, message: str, line: int | None = None, path: str = "(root)") -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.path = path


class _Loader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise YamlError("anchors and aliases are not allowed", event.start_mark.line + 1)
        return super().compose_node(parent, index)


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True  # never write an anchor: `load` refuses them


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_representer)


def _check(value: Any, path: str, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise YamlError("nested too deeply")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise YamlError("a key is not a string", path=path or "(root)")
            _check(item, f"{path}.{key}" if path else key, depth + 1)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]", depth + 1)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise YamlError("not a finite number", path=path)
    elif value is not None and not isinstance(value, str | int | bool):
        raise YamlError(
            f"a {type(value).__name__} value is not allowed; use a quoted string", path=path
        )


def load(text: str) -> dict[str, Any]:
    """Parse a manifest document; `YamlError` for anything else than a mapping
    of plain data. Messages never carry the document's values."""
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 (a SafeLoader subclass)
    except YamlError as exc:
        raise exc from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise YamlError(str(problem), mark.line + 1 if mark is not None else None) from None
    except RecursionError:
        raise YamlError("nested too deeply") from None
    if not isinstance(data, dict):
        raise YamlError("the document must be a mapping", 1)
    try:
        _check(data, "", 0)
    except RecursionError:
        raise YamlError("nested too deeply") from None
    return data


def dump(manifest: dict[str, Any]) -> str:
    """Block style, keys in the given order, multi-line strings as `|` blocks."""
    return yaml.dump(
        manifest,
        Dumper=_Dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
    )
