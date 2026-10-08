"""Unit tests for the generic call orchestrator.

Verifies:
- Forwarding and result propagation
- Serialization under max_concurrent=1
- Bounded concurrency under max_concurrent>1
- Error propagation
- Timeout handling, including the worker-side fix (a hung call must not
  wedge the queue for later calls)
- Lifecycle (start/stop idempotency)
"""

import asyncio
import logging

import pytest

from mcp_call_orchestrator_proxy.orchestrator import CallOrchestrator


@pytest.mark.asyncio
async def test_submit_forwards_and_returns() -> None:
    async def fn() -> str:
        return "result"

    async with CallOrchestrator() as orch:
        assert await orch.submit(fn) == "result"


@pytest.mark.asyncio
async def test_submit_serializes_calls_by_default() -> None:
    """With max_concurrent=1, calls should never overlap."""
    active = 0
    max_active = 0

    async def fn() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1

    async with CallOrchestrator(max_concurrent=1) as orch:
        await asyncio.gather(*(orch.submit(fn) for _ in range(4)))

    assert max_active == 1


@pytest.mark.asyncio
async def test_submit_bounds_concurrency() -> None:
    """With max_concurrent=2, up to 2 calls should run at once, never more."""
    active = 0
    max_active = 0

    async def fn() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1

    async with CallOrchestrator(max_concurrent=2) as orch:
        await asyncio.gather(*(orch.submit(fn) for _ in range(6)))

    assert max_active == 2


@pytest.mark.asyncio
async def test_submit_propagates_errors() -> None:
    async def fn() -> None:
        raise ValueError("boom")

    async with CallOrchestrator() as orch:
        with pytest.raises(ValueError, match="boom"):
            await orch.submit(fn)


@pytest.mark.asyncio
async def test_submit_times_out() -> None:
    async def fn() -> None:
        await asyncio.sleep(10)

    orch = CallOrchestrator(call_timeout=0.2)
    await orch.start()
    try:
        with pytest.raises(TimeoutError):
            await orch.submit(fn)
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_hung_call_does_not_wedge_the_queue() -> None:
    """The worker-side timeout must recover the queue after a hung call.

    Without it, the worker stays permanently stuck inside the hung call
    and every later submission queues forever - wedging the proxy for
    every other client, which is the whole reason this project exists.
    """

    async def hangs_forever() -> None:
        await asyncio.sleep(10)

    async def quick() -> str:
        return "ok"

    orch = CallOrchestrator(max_concurrent=1, call_timeout=0.2)
    await orch.start()
    try:
        with pytest.raises(TimeoutError):
            await orch.submit(hangs_forever)

        # A later call must still be served - the worker recovered.
        assert await orch.submit(quick) == "ok"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent() -> None:
    orch = CallOrchestrator()
    await orch.start()
    await orch.start()
    await orch.stop()
    await orch.stop()


# --- What the logs let an operator see ---

ORCHESTRATOR_LOGGER = "mcp_call_orchestrator_proxy.orchestrator"


def messages(caplog: pytest.LogCaptureFixture, containing: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(ORCHESTRATOR_LOGGER) and containing in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_a_call_is_reported_once_on_arrival_and_once_on_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two records per call, and no more.

    The pair is what makes arrival order and service order both readable - the
    only way to show from a log that the queue is doing its job. Two is also the
    budget: a record per call per event is affordable, a record per state change
    is not, and an operator will not read what they cannot scroll.
    """
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)

    async def quick() -> str:
        return "ok"

    async with CallOrchestrator() as orch:
        await orch.submit(quick, label="test-op", client="agent-a")

    accepted = messages(caplog, "call accepted")
    completed = messages(caplog, "call completed")

    assert len(accepted) == 1
    assert len(completed) == 1
    assert "client=agent-a" in accepted[0]
    assert "op=test-op" in accepted[0]
    assert "depth=" in accepted[0]
    assert "client=agent-a" in completed[0]
    assert "wait=" in completed[0] and "exec=" in completed[0]


@pytest.mark.asyncio
async def test_a_busy_period_raises_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Severity is the operator's first filter, so a healthy proxy must not trip it.

    A warning that fires when nothing is wrong trains the operator to ignore
    warnings, and then the one that matters is ignored too.
    """
    caplog.set_level(logging.DEBUG, logger=ORCHESTRATOR_LOGGER)

    async def quick() -> str:
        return "ok"

    async with CallOrchestrator() as orch:
        await asyncio.gather(*(orch.submit(quick, label="hammer") for _ in range(20)))

    noisy = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(ORCHESTRATOR_LOGGER) and r.levelno >= logging.WARNING
    ]
    assert not noisy, f"a healthy busy period tripped the warning filter: {noisy}"


@pytest.mark.asyncio
async def test_an_absorbed_failure_and_a_genuine_one_are_told_apart_by_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point of the severity model: an outage the proxy is designed to
    absorb must not be reported like a bug nobody saw coming.

    A degradation gets a WARNING and no traceback; anything unanticipated keeps
    the full-traceback ERROR. If both looked the same, the level would carry no
    information and the operator would be back to reading message text.
    """
    caplog.set_level(logging.DEBUG, logger=ORCHESTRATOR_LOGGER)

    class BackendDown(RuntimeError):
        """Stands in for the real BackendUnavailableError; the queue is generic."""

    async def degraded() -> str:
        raise BackendDown("backend is not available")

    async def bug() -> str:
        raise ValueError("something nobody anticipated")

    async with CallOrchestrator(expected_errors=(BackendDown,)) as orch:
        with pytest.raises(BackendDown):
            await orch.submit(degraded, label="call_tool:x", client="agent-a")
        with pytest.raises(ValueError):
            await orch.submit(bug, label="call_tool:y", client="agent-a")

    records = [r for r in caplog.records if r.name.startswith(ORCHESTRATOR_LOGGER)]
    absorbed = [r for r in records if "backend degraded" in r.getMessage()]
    unexpected = [r for r in records if "unexpected error" in r.getMessage()]

    assert len(absorbed) == 1
    assert absorbed[0].levelno == logging.WARNING
    assert absorbed[0].exc_info is None, "an absorbed degradation logged a traceback"
    assert "client=agent-a" in absorbed[0].getMessage()

    assert len(unexpected) == 1
    assert unexpected[0].levelno == logging.ERROR
    assert unexpected[0].exc_info is not None, "a genuine fault lost its traceback"


@pytest.mark.asyncio
async def test_the_verbose_level_separates_a_queued_call_from_a_running_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The two hangs an operator otherwise cannot tell apart.

    A call stuck in the queue and a call the backend never answered look
    identical at the default level - both are an acceptance with no completion.
    The DEBUG record is what separates them, and it must be absent by default:
    detail that diagnoses a rare fault is exactly the detail that must not be
    present when nothing is wrong.
    """

    async def quick() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    async with CallOrchestrator() as orch:
        await orch.submit(quick, label="slowish", client="agent-a")
    assert not messages(caplog, "call starting execution"), (
        "the verbose record leaked into the default level"
    )

    caplog.clear()
    caplog.set_level(logging.DEBUG, logger=ORCHESTRATOR_LOGGER)
    async with CallOrchestrator() as orch:
        await orch.submit(quick, label="slowish", client="agent-a")

    starting = messages(caplog, "call starting execution")
    assert len(starting) == 1
    assert "client=agent-a" in starting[0]
    assert "waited=" in starting[0]
