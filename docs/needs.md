# Needs

What `mcp-call-orchestrator-proxy` must do (needs, `OCP-N-NNN`) and the limits on how it is built
(constraints, `OCP-C-NNN`). An ID is never renumbered or reused; an item that no longer applies is
marked `retired`. A test that verifies an item carries `@pytest.mark.need("<ID>")`.

Status: `proposed` · `agreed` · `retired`. Priority: `must` · `should` · `could`.

## Sharing one backend

### OCP-N-001 — Several clients share one backend safely

**Statement.** When several clients use the proxy at once, the proxy shall pass their calls to
the backend no more than the configured number at a time (one by default), be the backend's only
client, ending each client's requests at the proxy whatever protocol version the client uses, and
deliver every result only to the client that made the call.

**Rationale.** Many MCP servers cannot serve concurrent clients: a single stdio pipe or a single
backend session serves one caller at a time. The proxy exists to make such a server usable by
several agents.

**Acceptance.** Concurrent clients calling a slow tool never cause more than the limit of calls
to run at the backend at once, and no client receives another client's result.

**Priority.** must · **Status.** agreed

### OCP-N-002 — A hung call does not block the others

**Statement.** When a backend call does not complete within the configured per-call timeout, the
proxy shall fail that call and serve the calls queued behind it.

**Rationale.** With calls serialized, one call the backend never answers would otherwise stop
every client.

**Acceptance.** A tool that never returns fails at the timeout, and a call queued behind it
completes.

**Priority.** must · **Status.** agreed

### OCP-N-003 — Clients see the backend as it is

**Statement.** The proxy shall present the backend's tools, resources, resource templates and
prompts under their real names and with their schemas' structure unchanged, and shall add no item
of its own except `backend_status`.

**Rationale.** Agents and their instructions are written against the backend's own surface; a
renamed or reshaped item breaks them, and a hand-maintained copy drifts.

**Acceptance.** A client's listings through the proxy match the backend's listings in names and
schema structure, plus `backend_status`.

**Priority.** must · **Status.** agreed

### OCP-N-004 — Selected tools can run outside the serialized lane

**Statement.** Where the operator names tools as concurrency-safe, the proxy shall run calls to
them in a second lane with its own concurrency limit, alongside the serialized lane, and shall
keep every other tool under the global limit.

**Rationale.** A cheap, read-only tool that is called constantly otherwise waits behind expensive
calls. Only the operator knows which tools are safe to run concurrently. A second lane keeps the
total load on the backend bounded, and no order between the two lanes is promised.

**Acceptance.** Under load, the backend serves several calls to the named tools at once while
every other tool runs one at a time, and a hung call in the second lane does not delay the
serialized lane.

**Priority.** could · **Status.** agreed

## Resilience

### OCP-N-005 — The proxy stays available while the backend is not

**Statement.** The proxy shall accept client connections whether or not the backend is reachable,
reconnect to the backend with bounded, jittered backoff after it becomes unreachable or
invalidates the proxy's session, and stop promptly on `SIGTERM`.

**Rationale.** Agents start and connect independently of the backend; a proxy that fails with
its backend multiplies the outage, and one that needs a manual restart after a backend restart
turns a short outage into a long one.

**Acceptance.** The proxy starts with the backend down and serves the backend's items once it
comes up; after a backend restart, including one that keeps the listener up and rejects the old
session, calls succeed again without restarting the proxy.

**Priority.** must · **Status.** agreed

### OCP-N-006 — A client can ask whether the backend is reachable

**Statement.** The proxy shall offer a `backend_status` tool that reports whether the backend is
connected, without making a call to the backend.

**Rationale.** An agent otherwise learns that the backend is down only from a failing call, and
cannot tell an outage from a fault in the call it made.

**Acceptance.** With the backend down, `backend_status` reports it disconnected and returns
without waiting on the backend.

**Priority.** must · **Status.** agreed

## Backends

### OCP-N-007 — A stdio backend can be fronted

**Statement.** Where the backend is described by a command, its arguments, environment and
working directory, the proxy shall start it as a child process and give it the same
serialization, session termination, parity and resilience as an HTTP backend.

**Rationale.** Most MCP servers are distributed as stdio servers, and a stdio pipe is the case
that serves only one caller at a time.

**Acceptance.** A stdio backend described in the standard `mcpServers` form serves its tools over
HTTP through the proxy, with two clients' calls serialized.

**Priority.** should · **Status.** agreed

### OCP-N-008 — The proxy owns a spawned backend's lifecycle

**Statement.** Where the proxy starts the backend process, it shall restart it after it exits
unexpectedly, and leave no backend process running after the proxy stops.

**Rationale.** An orphaned child process holds resources and may hold the backend's own locks.

**Acceptance.** Killing the child makes `backend_status` report it down and the proxy bring it
back; stopping the proxy leaves no child process.

**Priority.** should · **Status.** agreed

### OCP-N-009 — Backend configurations from the FastMCP ecosystem work or fail clearly

**Statement.** When a backend description carries keys that FastMCP defines beyond a plain
server entry, the proxy shall honour those it can apply without breaking its other needs and
constraints, and shall refuse to start with an error naming each key it cannot apply and why.

**Rationale.** Operators reuse backend descriptions written for FastMCP clients. A key that is
silently ignored, or that silently changes how the proxy reaches the backend, leaves the
operator with behaviour they did not configure.

**Acceptance.** A description with a supported key starts the proxy and applies it; one with an
unsupported key stops it at startup with an error naming the key; in neither case does a key
change the transport settings the proxy applies to the backend.

**Priority.** should · **Status.** proposed

### OCP-N-010 — Current MCP clients and protocol versions are served

**Statement.** The proxy shall serve clients that use any MCP protocol version its MCP library
supports, and shall reach a backend that uses any of those versions, whatever the pairing.

**Rationale.** Clients and servers move to new protocol versions at different times, and the
proxy sits between the two.

**Acceptance.** Each pairing of an older and a newer protocol version, on the client side and on
the backend side, completes discovery and a tool call through the proxy.

**Priority.** must · **Status.** agreed

## Exposure and guidance

### OCP-N-011 — The operator controls what clients can see and use

**Statement.** Where the operator configures an allow-list or a deny-list of exact names for
tools, resources or prompts, the proxy shall leave every item the configuration excludes out of
the listings and refuse calls to it by name; where both lists are set for one surface, the
allow-list shall decide alone.

**Rationale.** A backend's destructive items need not be reachable by an agent that only reads,
and every listed item's schema costs context in every session.

**Acceptance.** With an allow-list of a few tools, a client lists exactly those and a call to any
other tool by name is refused; the same holds for resources and prompts.

**Priority.** should · **Status.** agreed

### OCP-N-012 — The operator can tune what agents are told about the backend's items

**Statement.** Where the operator's overrides file gives text for a tool, an argument, a resource
or a prompt, the proxy shall replace the backend's description with it, or append or prepend it,
as the file says, and shall leave every name and the structure of every schema unchanged.

**Rationale.** A backend's descriptions are written for a general audience; an operator who knows
how their agents use the backend can make the descriptions say what those agents need, without
changing the backend.

**Acceptance.** Each of `replace`, `append` and `prepend` changes the description a client sees
for the item named, and the item's name and schema structure stay as the backend has them.

**Priority.** should · **Status.** proposed

### OCP-N-013 — Agents receive guidance about the backend as a whole

**Statement.** The proxy shall pass the backend's server instructions to its clients, and where
the operator's overrides file gives instructions text, shall replace or extend them with it as
the file says.

**Rationale.** Many clients put a server's instructions into the model's context; they are the
channel a backend's author, or the operator, has for guidance that belongs to no single tool.

**Acceptance.** A client connecting through the proxy receives the backend's instructions, or
the operator's text in their place or beside them.

**Priority.** should · **Status.** proposed

### OCP-N-014 — The operator can add guidance the backend does not have

**Statement.** Where the operator configures a skills directory or prompts of their own, the
proxy shall serve them to clients beside the backend's items, and shall refuse to start when one
of them has the name of a backend prompt.

**Rationale.** Guidance about using a backend in a particular setting belongs to the operator,
not to the backend's author.

**Acceptance.** A configured skill is listed as a resource and a configured prompt as a prompt; a
prompt whose name collides with a backend prompt stops the proxy at startup with an error naming
it.

**Priority.** could · **Status.** proposed

## Logs

### OCP-N-015 — Log severity reflects operational meaning

**Statement.** The proxy shall log at `WARNING` and above only the backend becoming unavailable,
the backend coming back and faults that need the operator, and shall log a degradation it
handles by design below `WARNING`.

**Rationale.** Severity is the operator's first filter and what an alert rule watches; a level
that also carries routine events carries no information.

**Acceptance.** Filtered to `WARNING` and above, the log of a busy period with a healthy backend
is empty, and the log of a backend outage shows the outage and the recovery.

**Priority.** must · **Status.** agreed

### OCP-N-016 — Default-level logs show the queue at work, per client

**Statement.** At the default level, the proxy shall log two records per call: one when the call
is accepted, with the client as it names itself, the operation and the queue depth, and one when
it completes, with the time spent waiting and running. Where the client's protocol version has
sessions, the client's name shall carry a number per session.

**Rationale.** That the proxy serializes calls is its whole value; the operator can only confirm
it from the order of arrival against the order of service, attributed to clients. A call with an
acceptance and no completion shows a hang. From protocol version 2026-07-28 a client has no
session, so two instances of one client that give the same name cannot be told apart, and the
log does not pretend to.

**Acceptance.** After two clients call concurrently, the default-level log shows which calls came
from which client, told apart by session number on a protocol version with sessions and by their
names on 2026-07-28, in what order they were served, and that they did not overlap at the
backend.

**Priority.** must · **Status.** agreed

### OCP-N-017 — A verbose level separates waiting from running

**Statement.** At the verbose level, the proxy shall also log when each call starts running at
the backend.

**Rationale.** A call waiting in the queue and a call the backend has not answered look the same
from outside; telling them apart is what diagnoses a hang.

**Acceptance.** During a reproduced hang at the verbose level, the log shows whether the hung
call is still queued or running at the backend.

**Priority.** must · **Status.** agreed

## Deployment

### OCP-N-018 — The proxy runs as a user service or under a process supervisor

**Statement.** The proxy shall ship a configuration for a `systemd --user` service and one for a
per-user `supervisord`, each restarting it when it exits and stopping it promptly.

**Rationale.** Hosts differ in which service manager is available to a user.

**Acceptance.** Under each, killing the proxy process brings it back, and a stop ends it without
waiting for a timeout.

**Priority.** should · **Status.** agreed

### OCP-N-019 — The proxy runs as a container

**Statement.** The project shall provide a container image that fronts an HTTP backend,
configured and given its backend secret by the means a container operator normally uses, with
no secret in any image layer.

**Rationale.** Where an operator's services run in containers, a service that needs a Python
toolchain on the host is not adopted.

**Acceptance.** The image started with a backend configuration and a port mapping serves the
backend's tools, and no layer of the image contains the secret.

**Priority.** should · **Status.** agreed

### OCP-N-020 — A fresh install gives a proxy that runs

**Statement.** The project shall declare its dependencies with ranges that exclude major versions
it has not been verified against, so that a resolution made without the lock file installs
versions the proxy runs with.

**Rationale.** A lock file protects only installs that use it; an upgraded lock, a dependency
bot or a build without the lock otherwise installs a major version that breaks the proxy.

**Acceptance.** A fresh resolution of the declared ranges installs a set with which the check
command passes.

**Priority.** must · **Status.** proposed

## Constraints

### OCP-C-001 — One backend per proxy process

**Statement.** A proxy process shall front exactly one backend. Further kinds of backend may be
added; a second backend in one process may not.

**Rationale.** Serialization, parity and resilience are defined against one backend. A second
backend is served by a second proxy.

**Priority.** must · **Status.** agreed

### OCP-C-002 — The backend's behaviour is not changed

**Statement.** The proxy shall control access to the backend and what agents are told about it,
and shall not retry, wrap or reshape the backend's results or errors.

**Rationale.** An agent relies on the backend's semantics; a proxy that alters them hides faults
and makes the backend's documentation untrue. A tool that errors keeps erroring.

**Priority.** must · **Status.** agreed

### OCP-C-003 — Calls are serialized by default

**Statement.** The global concurrency limit shall default to `1`, and concurrency beyond it shall
be possible only for tools the operator names.

**Rationale.** Serialization is the safe default for a backend whose concurrency behaviour is
unknown.

**Priority.** must · **Status.** agreed

### OCP-C-004 — Break detection makes no traffic of its own

**Statement.** The proxy shall detect a broken backend connection from the outcome of the calls
it already makes, and shall send no periodic request to the backend.

**Rationale.** A periodic request is load the backend did not ask for, and it competes with
clients' calls for the serialized lane.

**Priority.** must · **Status.** agreed

### OCP-C-005 — Secrets stay out of code, settings and logs

**Statement.** Backend secrets shall come from the environment, a `.env` file or the backend
description, and shall not appear in code, in the settings object or in a log record. A stdio
backend shall receive only the environment its description declares, beside a minimal safe
base.

**Rationale.** Logs and settings are shared and copied; the proxy's own environment holds secrets
that belong to no backend.

**Priority.** must · **Status.** agreed

### OCP-C-006 — Existing configuration keeps working

**Statement.** A release shall accept the configuration of the release before it with unchanged
behaviour, unless the configuration relied on a defect; new capability shall arrive as optional
configuration whose default preserves earlier behaviour.

**Rationale.** An operator upgrades a running service and expects it to behave as before.

**Priority.** must · **Status.** agreed

### OCP-C-007 — Misconfiguration is reported at startup

**Statement.** When the configuration refers to something that does not exist, or contradicts
itself, the proxy shall say so at startup; it shall resolve an ambiguous configuration in the
narrower direction with a warning, and refuse to start only on configuration it cannot honour.

**Rationale.** A proxy that silently runs with a surface or a policy the operator did not intend
is the failure to avoid; a proxy that refuses to start is not available to anyone.

**Priority.** must · **Status.** agreed

### OCP-C-008 — The check command runs offline

**Statement.** The check command shall pass on a machine with no network and no backend; with a
backend configured it shall also run the integration tier, and without one it shall state that
the tier was skipped.

**Rationale.** A gate that needs a backend cannot run on a fresh clone, and an opt-in tier that
has to be remembered is skipped.

**Priority.** must · **Status.** agreed

### OCP-C-009 — The guarantees are tested against a real MCP server

**Statement.** The offline tests shall run the proxy against a purpose-built server on a
third-party MCP server implementation, over real transports, started and stopped by the tests.

**Rationale.** A mock written beside the proxy shares the proxy's understanding of the protocol
and cannot catch a mistake in it; the test server's tools are purpose-built so that slow,
failing and hanging calls can be produced at will.

**Priority.** must · **Status.** agreed

### OCP-C-010 — The integration suite runs against any backend

**Statement.** The integration tier shall run against any MCP backend given only its backend
description, without changes to test code.

**Rationale.** The proxy's guarantees do not depend on the backend, so a suite that names one
backend's tools tests that backend, not the proxy.

**Priority.** must · **Status.** agreed

### OCP-C-011 — Shipped artifacts presume no backend product

**Statement.** No deployment artifact, example or comment shipped with the project shall presume
a particular backend product.

**Rationale.** The proxy serves any MCP backend; an example tied to one product misleads on first
contact.

**Priority.** must · **Status.** agreed

### OCP-C-012 — A new kind of backend leaves the core untouched

**Statement.** Support for a further kind of backend shall be confined to building the backend
client, without changes to the queueing, session termination or resilience code.

**Rationale.** Those layers carry the proxy's guarantees and are the ones a peripheral addition
must not destabilize.

**Priority.** could · **Status.** agreed

### OCP-C-013 — Python 3.12 is the floor

**Statement.** The project shall run on Python 3.12, with its linting and type checking targeting
3.12.

**Rationale.** Targeting the floor in the tools enforces it rather than only declaring it.

**Priority.** must · **Status.** agreed
