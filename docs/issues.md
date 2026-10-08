# Issues

Defects found in operation or on review — one entry each, with its confirmed root cause and fix.

## Record structure

Each issue carries three fields, each with its own lifecycle:

- **Symptom** — what was observed: logs, behaviour, blast radius. Frozen at the report, reflecting
  what was seen rather than the eventual explanation.
- **Root cause** — the confirmed mechanism, with evidence (source references, a reproduction).
  Frozen once confirmed; a later correction is recorded as an added finding, leaving the original
  in place.
- **Fix** — dated, append-only entries covering the proposal, any interim mitigation, and the
  shipped fix. An interim mitigation and the shipped fix are labelled as such.

Status markers: `[ ]` reported, not yet root-caused · `[~]` root-caused, fix pending (a mitigation
may be in place) · `[x]` fixed · `[-]` won't-fix or not-a-bug, with the reason recorded in Fix.

---

## At a glance

| # | Issue | Area | Status |
|---|---|---|---|
| ISSUE-1 | Proxy doesn't self-heal after a backend *session* is terminated (clean 404) | resilience (`resilient.py`) | `[x]` |

---

## ISSUE-1 — Proxy doesn't self-heal after a backend session is terminated (clean 404)

`[x]` · **Area:** resilience model (`resilient.py`) · **Reported:** 2026-07-15 · **Fixed:** 2026-07-15

**Trigger.** Any backend restart that leaves the HTTP listener up but invalidates the
streamable-HTTP session the proxy is holding — e.g. updating the Obsidian *Local REST API with
MCP* plugin (observed on 4.1.7), or restarting Obsidian, while the proxy keeps running.

**Symptom.** After the backend restarts, the still-running proxy serves **zero tools**. Clients
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

**Root cause.** *Confirmed 2026-07-15.* The proxy holds one long-lived backend session. When the
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

**Fix.**

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
behaviour. Regression coverage: a component-tier test terminates a backend's session within one
proxy lifetime and asserts the proxy re-exposes its tools with no restart
(`test_proxy_self_heals_after_a_terminated_backend_session`), and unit tests pin both the break and
the non-break `McpError` case. The clean-404 restart is modelled with an in-process backend double
raising the exact SDK signal rather than a spawned server, because a real backend restart drops the
TCP connection during the swap and so cannot reproduce the listener-up 404 deterministically; the
signal itself is verified from the SDK source cited under Root cause.
