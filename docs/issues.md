# Issues

Problem-resolution log: one Problem Record (`PRB-N`) per defect found in operation or on review,
tracked to a verified closure. A record holds the durable facts; an investigation trail lives in
a linked note, not here.

## Record structure

- **Problem** — what was observed; frozen at report.
- **Impact / Severity** — who or what is affected, and how badly.
- **Cause** — the confirmed mechanism with evidence; frozen once confirmed, corrections appended.
- **Resolution** — dated, append-only; the corrective action and the change that shipped it.
- **Verification** — how the fix was confirmed, and closure.

Status: `[ ]` open · `[~]` analyzed, fix pending · `[x]` closed (resolved and verified) · `[-]`
rejected (won't fix or not a bug, reason in Resolution).

---

## At a glance

| # | Issue | Component | Severity | Status |
|---|---|---|---|---|
| [PRB-1](#prb-1--proxy-doesnt-self-heal-after-a-backend-session-is-terminated-clean-404) | Proxy doesn't self-heal after a backend *session* is terminated (clean 404) | resilience (`resilient.py`) | major | [x] |
| [PRB-2](#prb-2--fastmcp-transform-keys-in-the-backend-entry-bypass-the-stdio-keep_alive-override-and-the-tls-setting) | FastMCP transform keys in the backend entry bypass the stdio `keep_alive` override and the TLS setting | backend client (`client.py`) | major | [x] |
| [PRB-3](#prb-3--unbounded-fastmcp-and-mcp-ranges-let-a-fresh-install-pull-versions-the-proxy-cannot-import) | Unbounded `fastmcp` and `mcp` ranges let a fresh install pull versions the proxy cannot import | packaging (`pyproject.toml`) | major | [x] |
| [PRB-4](#prb-4--tool-schemas-reach-clients-with-ref-inlined) | Tool schemas reach clients with `$ref` inlined | proxy server (`server.py`) | major | [x] |
| [PRB-5](#prb-5--a-config-with-more-than-one-server-is-not-refused-at-startup) | A config with more than one server is not refused at startup | backend client (`client.py`) | major | [x] |
| [PRB-6](#prb-6--a-tool-result-that-does-not-match-its-output-schema-tears-down-the-backend-connection) | A tool result that does not match its output schema tears down the backend connection | backend client (`client.py`) | major | [x] |
| [PRB-7](#prb-7--a-queued-calls-timeout-counts-its-wait-and-a-call-whose-caller-timed-out-still-runs) | A queued call's timeout counts its wait, and a call whose caller timed out still runs | queue (`orchestrator.py`) | major | [x] |

---

## PRB-1 — Proxy doesn't self-heal after a backend session is terminated (clean 404)

**Status:** Closed
**Severity:** major · **Component:** resilience model ([`resilient.py`](../src/mcp_call_orchestrator_proxy/resilient.py))
**Raised:** 2026-07-15 · **Closed:** 2026-07-15

**Problem.** Any backend restart that leaves the HTTP listener up but invalidates the
streamable-HTTP session the proxy is holding — e.g. updating the Obsidian *Local REST API with
MCP* plugin (observed on 4.1.7), or restarting Obsidian, while the proxy keeps running.

After the backend restarts, the still-running proxy serves **zero tools**. Clients
complete the MCP handshake with the proxy (initialize is answered locally), but every `list_tools`
/ `list_prompts` / `list_resources` fails. The service log shows, repeated indefinitely:

```
[httpx] HTTP Request: POST https://.../mcp/ "HTTP/1.1 404 Not Found"
ERROR [...orchestrator] orchestrator: call raised an unexpected error (op=list_tools)
    ...
mcp.shared.exceptions.McpError: Session terminated
```

The proxy does not recover on its own — the state persists across minutes and many client calls,
and clears only on a manual restart of the proxy service. This behaviour
is narrower than the roadmap's *"Self-healing — survives backend restarts"* description: the proxy
recovers from restarts that drop the TCP connection, but not from those that keep the listener up
and reject the stale session.

**Impact.** Every client of the affected proxy loses the backend's tools, prompts and resources
until the proxy service is restarted by hand, and the next backend restart repeats it.

**Cause.** *Confirmed 2026-07-15.* The proxy holds one long-lived backend session. When the
backend restarts it forgets that session id; per the MCP streamable-HTTP spec it then answers
**HTTP 404** to any request still carrying the stale id
([`streamable_http.py:350`](../.venv/lib/python3.12/site-packages/mcp/client/streamable_http.py#L350)),
and the SDK converts that 404 into `McpError(code=32600, "Session terminated")`
([`streamable_http.py:519`](../.venv/lib/python3.12/site-packages/mcp/client/streamable_http.py#L519)).

`ResilientBackend`'s break-detection set
[`_CONNECTION_ERRORS`](../src/mcp_call_orchestrator_proxy/resilient.py#L53) is
`(RuntimeError, TimeoutError, httpx.HTTPError, anyio.ClosedResourceError, anyio.EndOfStream,
anyio.BrokenResourceError)`. `McpError`'s MRO is `McpError → Exception` — none of those — so the
`except _CONNECTION_ERRORS` clauses in
[`list_tools` and its siblings](../src/mcp_call_orchestrator_proxy/resilient.py#L321-L378) do not
catch it. The 404 is handled as an ordinary per-call failure: `_broken` stays unset, the supervisor
does not tear down and rebuild the client, and the dead session is reused indefinitely.

*Why 4.1.7 exposed it — a latent proxy bug, not a plugin regression:* earlier plugin versions
dropped the TCP connection on restart, surfacing an `httpx`/`anyio` error that is in
`_CONNECTION_ERRORS` and so was detected as a break and reconnected. 4.1.7 keeps the HTTP server up
and returns a clean 404 for the unknown session (the correct MCP behaviour), which the current
break-detection set does not recognise.

**Urgent action.** The per-incident restart of the proxy service, recorded as the interim
mitigation under Resolution.

**Resolution.**

2026-07-15 — *Interim mitigation.* Restarting the affected proxy service rebuilt the client and
reconnected (`resilient: backend connected`), re-exposing the backend's expected tools. Confirmed on
a systemd user service fronting an HTTP backend. The mitigation is
per-incident — the defect recurs on the next backend restart until the code fix lands.

2026-07-15 — *Proposed fix.* Classify a session-terminated `McpError` as a connection break, so the
supervisor rebuilds the client and reconnects. The match is scoped to that specific signal —
JSON-RPC error `code == 32600` / message `"Session terminated"` — not the whole `McpError` class: a
normal failed or unknown tool call also raises `McpError`, and widening the match to the class would
turn an ordinary per-call error into a full connection teardown. Scope of the change: a
component-tier regression test that restarts the *spawned* backend within one proxy lifetime (fresh
session id, listener still up) and asserts the next `list_tools` succeeds without a manual
reconnect; a note in the resilience section of [`ARCHITECTURE.md`](../ARCHITECTURE.md); and removal of
the roadmap caveat once the behaviour holds.

2026-07-15 — *Fixed.* [`resilient.py`](../src/mcp_call_orchestrator_proxy/resilient.py) now treats
the session-terminated signal as a break. A predicate matches `McpError` with `code == 32600` and
message `"Session terminated"`, and every delegated call routes through one break-classifier that
marks the connection broken — waking the supervisor to rebuild the client on a fresh session — on
either a transport-level error or that signal. The match stays scoped to that exact code/message, so
any other `McpError` (an unknown or failed tool call) remains a per-call error and leaves the
connection live. The resilience section of [`ARCHITECTURE.md`](../ARCHITECTURE.md) records the
behaviour.

**Verification.** 2026-07-15 — Regression coverage: a component-tier test terminates a backend's session within one
proxy lifetime and asserts the proxy re-exposes its tools with no restart
(`test_proxy_self_heals_after_a_terminated_backend_session`), and unit tests pin both the break and
the non-break `McpError` case. The clean-404 restart is modelled with an in-process backend double
raising the exact SDK signal rather than a spawned server, because a real backend restart drops the
TCP connection during the swap and so cannot reproduce the listener-up 404 deterministically; the
signal itself is verified from the SDK source cited under Cause.

**Reproduction.** The defect appears when, within one proxy lifetime:

1. the proxy is running against a live streamable-HTTP backend with a working `tools/list`;
2. the backend is restarted, invalidating its sessions, while the proxy keeps running;
3. the next `tools/list` through the proxy returns `McpError: Session terminated` and does not
   recover.

The integration and component suites do not cover this. Integration tests each open a fresh
session, and the existing resilience test (`test_real_backend_http.py`) kills the backend process,
producing a transport-level error already in `_CONNECTION_ERRORS` rather than the clean-404 path.
The uncovered case is a backend restart that cleanly rejects the old session within one proxy
lifetime.

---

## PRB-2 — FastMCP transform keys in the backend entry bypass the stdio keep_alive override and the TLS setting

**Status:** Closed
**Severity:** major · **Component:** backend client ([`client.py`](../src/mcp_call_orchestrator_proxy/client.py))
**Raised:** 2026-10-18 · **Closed:** 2026-10-18

**Problem.** When the single `mcpServers` entry contains `tools`, `include_tags` or
`exclude_tags`, the proxy reaches the backend through a nested in-process FastMCP proxy around
the real transport. The `StdioTransport` check never matches, so `keep_alive=False` is not
applied; the HTTP/SSE check never matches, so the TLS setting is not applied. Nothing is logged.

**Impact.** An operator who uses FastMCP's documented transform keys silently loses the guarantee
that a stdio backend's subprocess is not left behind, loses their TLS setting, and gets an extra
proxy layer. Workaround: do not use those keys.

**Cause.** `client.py` called `to_transport()` on the parsed entry. FastMCP parses an entry with
transform keys as a transforming server, whose `to_transport()` returns
`FastMCPTransport(create_proxy(Client(<real transport>)))`, so the overrides in `client.py` saw
only the outer transport. Reproduced on FastMCP 3.4.4 and 4.1.0: the same entry gives a
`FastMCPTransport` with `tools` and a `StdioTransport` without.
→ analysis trail: [issue #1](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/1)

**Urgent action.** none

**Resolution.** 2026-10-18 — The transport is built from the plain entry type, so the overrides
always reach the real transport, and an entry carrying any of the three keys is refused at
startup with an error naming the key. Shipped in [pull request #13](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/pull/13) (merge
`ebc890a`), released in v0.4.3. Honouring the safe subset of the `tools` key is planned
separately ([issue #8](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/8)).

**Verification.** 2026-10-18 — Unit tests that fail before the fix and pass after:
`test_stdio_entry_with_a_transform_key_still_gets_keep_alive_off`,
`test_http_entry_with_a_transform_key_still_gets_the_tls_setting`,
`test_a_transform_key_in_the_entry_stops_the_proxy_at_startup`,
`test_every_transform_key_present_is_named`; `test_a_plain_entry_starts_the_proxy` covers the
unchanged path.

---

## PRB-3 — Unbounded fastmcp and mcp ranges let a fresh install pull versions the proxy cannot import

**Status:** Closed
**Severity:** major · **Component:** packaging ([`pyproject.toml`](../pyproject.toml))
**Raised:** 2026-10-18 · **Closed:** 2026-10-18

**Problem.** `pyproject.toml` declared `fastmcp>=3.4.4` and `mcp>=1.28.1`. FastMCP 4.1.0, which
pulls MCP SDK 2.3.0, satisfies both, and with it importing the application fails:
`ImportError: cannot import name 'McpError' from 'mcp.shared.exceptions'`.

**Impact.** `uv sync` against the committed `uv.lock` is unaffected. Any fresh resolution —
`uv lock --upgrade`, a dependency bot, an install that ignores the lock, an image built without
it — gives a proxy that does not start.

**Cause.** MCP SDK 2 renamed `McpError` to `MCPError`, and `resilient.py` imports the old name.
Beyond the import, FastMCP 4 fails to connect to a backend and to complete client handshakes with
the proxy's facade, so moving to it is a project of its own ([issue #5](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/5)).
→ analysis trail: [issue #2](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/2)

**Urgent action.** none

**Resolution.** 2026-10-18 — Ranges capped at `fastmcp>=3.4.4,<4` and `mcp>=1.28.1,<2`; the
locked versions are unchanged. Shipped in [pull request #10](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/pull/10) (merge `b1ba380`),
released in v0.4.3.

**Verification.** 2026-10-18 — A fresh resolution of the capped ranges picks FastMCP 3.4.9 and
MCP SDK 1.30.0, and the check command passes on them; with the old ranges it picks FastMCP 4.1.1
and MCP SDK 2.3.0.

---

## PRB-4 — Tool schemas reach clients with `$ref` inlined

**Status:** Closed
**Severity:** major · **Component:** proxy server ([`server.py`](../src/mcp_call_orchestrator_proxy/server.py))
**Raised:** 2026-10-18 · **Closed:** 2026-10-18

**Problem.** A backend tool whose input schema uses `$ref` — to `$defs`, or an internal pointer
such as `#/properties/destination/properties/parent/anyOf/0` — reached clients with the
referenced subschema copied in place and `$defs` removed. Recursive schemas passed through
unchanged, so the proxy altered some schemas and not others. The integration parity test failed
against a real backend whose schemas use `$ref`.

**Impact.** Accepted inputs stayed the same, but the documented guarantee that names and schema
structure reach clients unchanged failed, and any client or test comparing schemas saw a
difference.

**Cause.** FastMCP's server defaults to `dereference_schemas=True`, which adds
`DereferenceRefsMiddleware` to inline `$ref` on listing; `build_proxy_server` did not turn it off.
With the middleware removed, every reproduced schema is byte-identical to the backend's.
→ analysis trail: [issue #4](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/4)

**Urgent action.** none

**Resolution.** 2026-10-18 — The proxy's server is built with `dereference_schemas=False`, keeping
exact parity; FastMCP's inlining serves clients that cannot read `$ref`, and an opt-in setting
for them would be a separate feature. Shipped in [pull request #11](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/pull/11) (merge
`5e44e5f`), released in v0.4.3.

**Verification.** 2026-10-18 — `test_schemas_with_ref_reach_clients_unchanged` (component tier)
fails before the fix and passes after, for a `$defs` schema and an internal-pointer schema; the
integration parity test passes against a real backend whose schemas use `$ref`.

---

## PRB-5 — A config with more than one server is not refused at startup

**Status:** Closed
**Severity:** major · **Component:** backend client ([`client.py`](../src/mcp_call_orchestrator_proxy/client.py))
**Raised:** 2026-10-18 · **Closed:** 2026-10-18

**Problem.** With an `mcpServers` file that defines two servers, the proxy started and served.
Every backend connection attempt then logged `resilient: unexpected error in backend supervisor;
will retry` at ERROR with a traceback ending in the single-backend `ValueError`, for as long as
the process ran. Clients could connect and saw only `backend_status`.

**Impact.** An operator with a multi-server file got a proxy that looked started but never reached
a backend, with ERROR tracebacks at every backoff step. Workaround: define exactly one server.

**Cause.** The one-backend check ran only in `client.py::_single_server_transport`, which the
backend factory calls on each connect attempt inside `ResilientBackend._supervise`; its
`ValueError` reached the supervisor's generic exception handler and was retried. Nothing on the
startup path counted the servers.
→ analysis trail: [issue #14](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/14)

**Urgent action.** none

**Resolution.** 2026-10-18 — `check_backend_config`, called by `create_proxy_app` at startup,
refuses a config that does not define exactly one server; the per-connect check stays. A config
with no server is refused with the same error. Shipped in [pull request #16](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/pull/16)
(merge `4f4e695`), released in v0.4.3.

**Verification.** 2026-10-18 — `test_a_config_without_exactly_one_server_stops_the_proxy_at_startup`
fails before the fix and passes after, with two servers and with none; `run` with a two-server
config exits with status 1 before it listens.

## PRB-6 — A tool result that does not match its output schema tears down the backend connection

**Status:** Closed
**Severity:** major · **Component:** backend client ([`client.py`](../src/mcp_call_orchestrator_proxy/client.py))
**Raised:** 2026-10-18 · **Closed:** 2026-10-19

**Problem.** When a backend tool declared an `outputSchema` and returned `structuredContent`
that did not match it, the caller received "Backend is not available … Invalid structured
content returned by tool …" instead of the result. The proxy tore down and rebuilt the backend
connection, restarting a stdio backend, and a call from another client just after it failed with
"Backend is not available … Client is not connected".

**Impact.** Every call in flight or queued behind such a call failed as an outage, at `WARNING`,
for any backend whose output does not match its own schema. The proxy reshaped a result it should
relay (OCP-C-002). No workaround in configuration.

**Cause.** The proxy's backend client used the MCP SDK's `ClientSession`, which checks every tool
result against the tool's output schema and raises `RuntimeError` on a mismatch.
`ResilientBackend._guarded` counts `RuntimeError` as a broken connection, because FastMCP raises
it for a client that is not connected, so the supervisor rebuilt the connection.
→ analysis trail: [issue #20](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/20)

**Urgent action.** none

**Resolution.** 2026-10-19 — the backend client uses `RelayClientSession`, a `ClientSession`
whose result check does nothing, on every backend source; the proxy's own clients check what they
receive. `RuntimeError` stays a broken connection. Shipped in
[pull request #22](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/pull/22) (merge `fbd4aaa`), released in v0.5.1.

**Verification.** 2026-10-19 — `test_a_result_off_its_output_schema_reaches_the_caller_unchanged`
fails before the fix and passes after, over a stdio backend and HTTP backends at both protocol
versions: the caller receives the backend's `structuredContent`, a second client's call
succeeds, and the connection is not rebuilt. `test_a_checking_client_rejects_the_relayed_result_itself`
shows a client of the proxy still rejects the result in its own session.

## PRB-7 — A queued call's timeout counts its wait, and a call whose caller timed out still runs

**Status:** Closed
**Severity:** major · **Component:** queue ([`orchestrator.py`](../src/mcp_call_orchestrator_proxy/orchestrator.py))
**Raised:** 2026-10-18 · **Closed:** 2026-10-19

**Problem.** `call_timeout_seconds` ran from the moment a call was queued. A call that waited
behind a hung call failed when its own timeout ran out, with only what was left of it to run. A
call whose caller had received the timeout error, or had been cancelled, stayed in the queue and
still ran at the backend, logged as `call completed`. The error had an empty message
(`Error calling tool 'echo': `), and a hung call was sometimes logged at `ERROR` as an unexpected
fault with a traceback.

**Impact.** OCP-N-002 failed: a call queued behind a hung one failed with it. For a tool with side
effects the client was told the call failed while the backend performed it, and a retry performed
it twice. The backend did work no caller waited for, and clients could not tell a wait in the
queue from a backend that did not answer. A longer timeout only narrowed the window.

**Cause.** `CallOrchestrator.submit` waited for the result with one deadline covering the wait in
the queue and the run. On a timeout it cancelled the result future and left the call queued; the
dispatcher started every queued call whatever the state of its future. It re-raised
`wait_for`'s own `TimeoutError()`, which has no message. The backend client's MCP request timeout
had the same length as the queue's, and when both fired the SDK's `MCPError` reached the
orchestrator's catch-all.
→ analysis trail: [issue #23](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/issues/23)

**Urgent action.** none

**Resolution.** 2026-10-19 — `call_timeout_seconds` bounds the wait in the queue and the backend
call separately, each from its own start. A call whose caller is gone before it starts, by a
timeout in the queue or by cancellation, is withdrawn and never reaches the backend. The errors
say what timed out and after how long. The backend client's MCP request timeout runs 5 s longer
than a call, so the queue's fires first. Shipped in [pull request #26](https://github.com/eoc-llm-tooling/mcp-call-orchestrator-proxy/pull/26) (merge
`28d893d`), released in v0.5.1.

**Verification.** 2026-10-19 — `test_a_call_whose_caller_timed_out_never_reaches_the_backend` and
`test_a_queued_call_runs_for_its_whole_timeout_after_the_wait` fail before the fix and pass after,
as do the unit tests for withdrawal on timeout and on cancellation and for the error texts. With
real clients on both protocol versions, a call whose client cancelled or disconnected while it
waited was withdrawn and never reached the backend. No `call raised an unexpected error` record
in three runs of the HTTP component tests.
