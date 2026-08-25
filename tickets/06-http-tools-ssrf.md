---
id: 06
status: todo
model: opus
depends_on: [05]
max_attempts: 3
files:
  - app/tools/http.py
  - app/tools/ssrf.py
  - app/tools/validation.py
  - app/tools/registry.py
  - tests/test_http_tools.py
  - tests/test_ssrf.py
---
## Goal
Customer HTTP tools executed safely: schema-validated args, SSRF-guarded URL, auth from encrypted config, ToolExecution recorded, errors returned to LLM as data.

## Context
`HttpToolExecutor.execute(name, args, ctx)`: load `Tool` by `(ctx.tenant_id, name)` (only names in `agent.tool_names`); `validation.py` validates args with `jsonschema` (add dep) → error result on failure; `ssrf.py::assert_safe_url(url)`: scheme http/https only, resolve host via `socket.getaddrinfo`, reject loopback/private/link-local/multicast/`0.0.0.0`/metadata `169.254.169.254`; optional env `TOOL_ALLOWED_HOSTS` allowlist (comma list) overrides. Also skip check when `TOOL_SSRF_DISABLED=true` (dev only, logged loudly). GET → args as query; POST/PUT/PATCH → JSON body. Auth config `{type: none|bearer|header, token, header_name}` decrypted at call time, never logged. `httpx` timeout = `tool.timeout_seconds`, no redirects. Response: JSON if parseable else `{text}` truncated 8KB. Any exception → `ToolResult(ok=False, error="<safe message>")`, still returned to LLM. Always persist `ToolExecution` with latency. Register in `registry.py`.

Tests mock with `respx` (add dev dep).

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run pytest -q tests/test_http_tools.py tests/test_ssrf.py`
3. `uv run pytest -q`

## Done when
- `get_order` tool → GET `https://example.test/orders?order_id=12345` with bearer header; result JSON reaches LLM; ToolExecution ok.
- 500 / timeout / invalid args → ToolExecution error, agent loop continues with error payload.
- `http://127.0.0.1`, `http://169.254.169.254`, `file://` rejected before request.

## Not included
- Built-in tools, retries on tool calls, media.
