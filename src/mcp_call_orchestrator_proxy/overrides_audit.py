"""Checks the overrides file's names against the backend, once it first connects.

The proxy starts whether or not its backend is up, so at startup the names in
the overrides file may have nothing to be checked against. ``OverridesAudit``
runs when the backend first connects (a connect listener on
``ResilientBackend``): it lists every surface the backend advertises through
the queue, as the proxy's own calls, and warns once per name the backend does
not have - a tool, prompt or resource, or an argument of one that exists.
Resources are checked against resource URIs and template ``uriTemplate``s
together. A check that does not complete, because the backend went away
during it, runs again on the next connect; once one completes, later connects
do not repeat it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.interfaces import MCPBackendClient
from mcp_call_orchestrator_proxy.overrides import Overrides
from mcp_call_orchestrator_proxy.resilient import MethodNotSupportedError

logger = logging.getLogger(__name__)


class OverridesAudit:
    """Warns once per overrides-file name the backend does not have."""

    def __init__(
        self,
        overrides: Overrides,
        backend: MCPBackendClient,
        capabilities: Callable[[], mcp_types.ServerCapabilities | None],
    ) -> None:
        self._overrides = overrides
        self._backend = backend
        self._capabilities = capabilities
        self._done = False
        self._lock = asyncio.Lock()

    async def check(self) -> None:
        """Check the names, unless a check has already completed. Never raises."""
        async with self._lock:
            if self._done:
                return
            try:
                findings = await self._findings()
            # A connect listener must not fail: whatever stopped the check, the
            # next connect runs it again.
            except Exception as error:
                logger.info(
                    "overrides: name check did not complete (%s); it runs again "
                    "when the backend next connects",
                    error,
                )
                return
            self._done = True
        for finding in findings:
            logger.warning("overrides: %s", finding)

    async def _findings(self) -> list[str]:
        capabilities = self._capabilities()

        def has(surface: str) -> bool:
            return capabilities is None or getattr(capabilities, surface) is not None

        tools = await _listing(has("tools"), self._backend.list_tools)
        prompts = await _listing(has("prompts"), self._backend.list_prompts)
        resources = await _listing(has("resources"), self._backend.list_resources)
        templates = await _listing(
            has("resources"), self._backend.list_resource_templates
        )

        findings = _missing(
            "tool",
            self._overrides.tools,
            {
                tool.name: set(tool.input_schema.get("properties") or ())
                for tool in tools
            },
        )
        findings += _missing(
            "prompt",
            self._overrides.prompts,
            {
                prompt.name: {argument.name for argument in prompt.arguments or ()}
                for prompt in prompts
            },
        )
        listed = {str(r.uri) for r in resources} | {t.uri_template for t in templates}
        findings += [
            f"resource {name!r} not found on backend"
            for name in sorted(set(self._overrides.resources) - listed)
        ]
        return findings


async def _listing[T](
    advertised: bool, list_items: Callable[[], Awaitable[Sequence[T]]]
) -> Sequence[T]:
    """The backend's items of one surface; none where it does not serve it."""
    if not advertised:
        return ()
    try:
        return await list_items()
    except MethodNotSupportedError:
        return ()


def _missing(
    kind: str, overrides: Mapping[str, Any], listed: Mapping[str, set[str]]
) -> list[str]:
    findings: list[str] = []
    for name in sorted(overrides):
        if name not in listed:
            findings.append(f"{kind} {name!r} not found on backend")
            continue
        for argument in sorted(set(overrides[name].arguments) - listed[name]):
            findings.append(f"{kind} {name!r} has no argument {argument!r} on backend")
    return findings
