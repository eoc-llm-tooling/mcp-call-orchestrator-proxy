# Coding standards

Conventions to match when writing code in this repo: Python usage, design principles, and
style. Toolchain and commands (how to install, lint, test, run the gate) live in the
[`README.md`](../README.md) `## Development` section, not here.

## Python & typing

- **Python 3.12+ is the real floor.** PEP 695 generics (`class _QueuedCall[T]`,
  `async def submit[T]` in `orchestrator.py`) are what require it — write code as modern as
  3.12 allows and no further. A construct that only works on 3.13+ is a floor violation, not a
  style choice.
- **`from __future__ import annotations`** at the top of every module.
- **Fully typed, strict mypy.** No untyped/incomplete defs; no implicit `Optional`; unused
  ignores are errors. Public async surfaces are annotated end to end.

## SOLID & design

- **Single Responsibility.** One concern per module (config, client-building, orchestration,
  resilience, server-wiring, …). A new concern gets a new module rather than a new corner of an
  existing one.
- **Dependency Inversion.** Collaborators arrive via constructor params or factories
  (`backend_factory` / `backend_client` injection points), wired once at the composition root
  (`app.py::create_proxy_app`). No global singletons, no module-level state, no I/O at import
  time.
- **Interface Segregation / Open-Closed via Protocols, not inheritance.** `MCPBackendClient` in
  `interfaces.py` is the one structural contract every layer implements. A new backend kind or
  proxy layer is added by satisfying the Protocol, not by subclassing or branching on type; add
  a method to the Protocol itself first when the backend surface needs to grow.
- **Async-first.** Backend-facing work is `async`; long waits are event-driven and interruptible
  (see the interruptible backoff / `_race` helper in `resilient.py`) so shutdown stays prompt
  instead of blocking on a sleep.

## Style

- Line length **88**, **double quotes**, modern idioms over legacy ones (e.g. `X | None` over
  `Optional[X]`, comprehensions over manual accumulation). The enforced rule set lives in
  `pyproject.toml` — this is the human-readable summary, not a second source of truth.

## Errors, logging, secrets

- **Error model:** a *tool* failure is a normal `CallToolResult(isError=True)` and flows
  through untouched; only *transport/connection* problems surface as exceptions. Connection
  errors are caught against the `_CONNECTION_ERRORS` set and converted to
  `BackendUnavailableError` (a `RuntimeError` subclass FastMCP maps to a clean MCP error).
  Don't swallow connection errors as tool errors or vice versa.
- **Logging:** module-level `logger = logging.getLogger(__name__)`. Never `print` in library
  code (the CLI uses `typer.echo` for user-facing lines, which is fine). Log the *why*;
  reserve `logger.exception` for genuinely unexpected paths.
- **Secrets** (API keys / bearer tokens) come only from the environment, `.env`, or the
  backend JSON — never hard-coded, never placed on `ProxySettings` fields that get logged.
  The backend token lives inside the built transport, keeping the settings object safe to print.

## Docstrings & comments

- **Module docstring** on every module stating its role and the non-obvious *why* (see the
  existing modules for the tone).
- Docstring public classes and methods. Prefer comments that explain a decision or a subtle
  invariant over comments that restate the code.
