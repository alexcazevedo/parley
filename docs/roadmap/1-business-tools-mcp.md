# Brief 1: business tools over MCP

Mode: `mvp`. Routing profile `cloud`, sequential.

## The problem

The first tenant, vendezap (a WhatsApp sales inbox for shops), needs this conversation to work:

1. Customer: "Vocês têm cartolina branca?"
2. The agent sees a tool `search_product` with a clear description and calls it with "cartolina branca".
3. Agent: "Sim, temos 2 cartolinas brancas. Gostaria de levar alguma delas?"
4. Customer: "Sim, a primeira."
5. The agent calls `add_to_quote` with that product's id.
6. Agent: "Perfeito! Anotei aqui. Deseja algo mais?"

## What parley has today

- `POST /tools` registers one HTTP endpoint: `name`, `description`, `input_schema`, `endpoint`, `method`, `auth_config`, `timeout_seconds` (`app/api/tools.py`).
- The agent's `tool_names` lists which of the tenant's tools it may call.
- `app/tools/http.py` validates the model's arguments against the schema and sends them as a query string (`GET`, `DELETE`) or a JSON body (others), with bearer or header auth, the SSRF guard, a response cap (`TOOL_MAX_RESPONSE_BYTES`) and a row in `tool_executions` written before the call (`app/tools/ledger.py`).
- The manifest has a `tools` section; tokens are secret references.
- Docs: `docs/guides/setup.md` step 2, `docs/guides/manifest.md`, and one tool in `examples/manifests/store.yaml`.

Step 2 of the conversation above works today with a real model key. The rest does not.

## What is missing

1. **No customer context.** The call carries only the model's arguments and the auth header. `add_to_quote` cannot know whose quote it is.
2. **No contract for the app.** The docs say how to register a tool. They do not say what the app must implement: what arrives, what to return, how to say "sem estoque" so the agent can pass it on, limits, repeats.
3. **No discovery.** Every tool is registered by hand, one call each.
4. **No call id.** A non-`GET` tool is sent once and never repeated, but the app gets nothing to deduplicate on.
5. **No signature.** Event pushes to an external channel are signed (`app/events/signing.py`); tool calls are not.
6. **Cannot be tried without a model key.** The stub model calls only `handoff_to_human` and `search_knowledge`.
7. `docs/integrations/vendezap.md` lists catalog, pedido and kit tools as out of scope.

## Decided

**Protocol.** Parley is an MCP client. An app exposes its functions by running an MCP server. Reasons: MCP already defines discovery (`tools/list`), the call (`tools/call`), the result and its error flag (`isError`), and a marker for tools that only read (`readOnlyHint` and the other annotations). Any system with an MCP server plugs in with no parley-specific code. Rejected: a tool-list document of parley's own (reinvents MCP), importing OpenAPI (a later option), LangChain or LangGraph adapters (parley has no framework and needs none for this).

**Transport.** Streamable HTTP only. No stdio: parley is a service and never starts a tenant's process.

**Registration.** With the tenant key, a tenant registers an MCP server: a name, the URL and a credential (bearer or header token, stored encrypted like a tool's `auth_config`). Full CRUD, paged lists, tenant-scoped. The URL passes the same checks as a tool endpoint (`TOOL_ALLOWED_HOSTS`, the SSRF guard in `app/tools/ssrf.py`) at registration and on every connection. Redirects are not followed.

**Discovery.** Parley lists the server's tools at registration, on request, and on a schedule in the worker. It keeps a copy (name, description, input schema, annotations), so a turn never waits on discovery. Tools that disappear from the server are removed. A server that cannot be reached keeps its last known tools and shows an error status with the reason.

**Attaching to an agent.** A server is attached to an agent as a whole, or tools are picked from it. The model sees each tool under a name prefixed with the server's name, so two servers cannot collide with each other, with hand-made tools or with the built-ins (`remember`, `handoff_to_human`, `search_knowledge`).

**What parley adds on top of MCP**, the same for every server:

- *Customer context* on every `tools/call`, in the request's `_meta` under parley's own keys and never inside the arguments the model writes: the conversation id, the channel id, the user id the app itself gave parley for that customer (on an external channel that is the project's own id; for vendezap, its conversa id), and the contact's display name when known. A server that ignores `_meta` still works for lookups.
- *A call id*, unique per call and the same when parley repeats that call, so a server can make a write safe to receive twice.
- *A signature* the server can check: timestamp and HMAC, in the style of the signed event pushes, with a secret shown once.
- The same three are also sent on existing HTTP tools, as headers, so both kinds of tool give an app the same information. Existing tools keep working: nothing is added to their arguments.

**Repeats.** A tool marked read-only may be retried, as a `GET` tool is today. Anything else is called at most once per model request and never retried by parley. A missing annotation means not read-only.

**Results.** Text content is what the model reads, under `TOOL_MAX_RESPONSE_BYTES`. A result with `isError` is a refusal the model may explain to the customer. A transport failure or a timeout is a failure the model apologises for. Other content types (images, files) are replaced by a short placeholder.

**Safety.** Tool results are data, not instructions, as for `search_knowledge`: the tool description and the result wrapper say so, and the boundary check still applies to the final reply. Every call is recorded in `tool_executions` through the existing ledger, with the server, the context sent and the result.

**Manifest.** MCP servers and their attachment to agents are in the manifest: apply, dry run, export, removal, credentials as secret names. Older files stay valid.

**Trying it without a model key.** The stub model (`scripts/mock_llm_server.py`) calls a tenant tool in a fixed, documented way.

**A try route.** With the tenant key: server, tool, arguments and a user id in; the result out. For debugging a server without a conversation.

**Example.** `examples/project_inbox` gains an MCP server with `search_product` and `add_to_quote` over a small fixed catalog. It reads the user id from `_meta` to keep one quote per customer, and uses the call id so a repeated add does nothing.

**Docs.** A guide under `docs/guides` for the app side, written as the contract: how to expose an MCP server parley can use, the `_meta` keys, the call id, annotations and repeats, the signature check with a code sample, errors, limits, a checklist, and what a third-party server that knows nothing of parley can and cannot do. Linked from `llms.txt`, setup, manifest and project-channel. `docs/integrations/vendezap.md` gets a section naming the tools vendezap should serve, as a follow-up brief for the vendezap repository.

## Open for the grill

1. The official MCP Python SDK as a dependency, or a small client over parley's own HTTP client. Judge by: fitting the SSRF guard on every connection, the no-network test rule, and the size of the dependency.
2. The signature: in this milestone, or a documented follow-up with the bearer token as the minimum.
3. Attaching: one switch per server on the agent, tools picked one by one, or both.
4. What a server that requires OAuth gets (static credentials only in this milestone): refused at registration with a clear message, or accepted and failing at discovery.
5. The fixed way the stub model calls a tenant tool.

## Read before planning

- The current MCP specification: `tools/list`, `tools/call`, the streamable HTTP transport and whether it can run without a session, tool annotations, the `_meta` field, `isError`, authorization.
- The official MCP Python SDK documentation: the client, how to give it an HTTP client or hooks, timeouts.
- In this repository: `PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/guides/setup.md` (step 2), `docs/guides/manifest.md`, `docs/guides/project-channel.md`, `app/tools/registry.py`, `app/tools/http.py`, `app/tools/base.py` (`ToolContext`, `ToolExecutor`), `app/tools/builtin.py`, `app/tools/ledger.py`, `app/tools/ssrf.py`, `app/api/tools.py`, `app/events/signing.py`, `app/manifest/`, `examples/project_inbox/app.py`, `scripts/mock_llm_server.py`.

Follow the patterns already there rather than adding new ones.

## Tests

Against an in-process fake MCP server, never the network: discovery and re-sync (create, update, remove), name prefixes, context and call id on the wire, a read-only and a changing tool inside a turn, an `isError` result reaching the model, an unreachable server, tenant isolation, the address checks, existing HTTP tools unchanged apart from the new headers.

## Out of scope

Parley as an MCP server. The stdio transport. MCP resources, prompts, sampling and elicitation. OAuth authorization. Importing an OpenAPI file. Asking the customer to confirm before a tool that changes something runs. Tools that return files or images. Per-customer permissions on tools. Any change in the vendezap repository. Any admin screen. Any agent framework.

## Done when

With the example project running beside parley and a scripted model, I register the example's MCP server with one call, attach it to an agent, and send "Vocês têm cartolina branca?" as a customer on an external channel: the agent calls `search_product` and replies with the two products. I answer "Sim, a primeira": the agent calls `add_to_quote` with that product's id, the example shows the item in the quote of that customer and of no other, and the agent confirms. The same add sent again with the same call id adds nothing. A second customer asking the same gets their own quote. `tool_executions` shows both calls with the customer context. The same path runs on the stub stack with no model key.
