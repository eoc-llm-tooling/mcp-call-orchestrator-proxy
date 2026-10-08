"""Unit tests for exposure-control policy resolution and audit."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp import types as mcp_types

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings
from mcp_call_orchestrator_proxy.exposure import (
    BACKEND_STATUS_TOOL_NAME,
    PolicyMode,
    Surface,
    resolve_policies,
)
from mcp_call_orchestrator_proxy.exposure_audit import ExposureAuditBackend


def _settings(**kwargs: Any) -> ProxySettings:
    return ProxySettings(backend_api_key="a" * 32, **kwargs)


# ---------------------------------------------------------------------------
# U6 resolve_policies
# ---------------------------------------------------------------------------


def test_resolve_all_unset_is_noop() -> None:
    res = resolve_policies(_settings())
    for surface in Surface:
        assert res.policies[surface].mode == PolicyMode.UNSET
        assert res.policies[surface].names == frozenset()
    assert res.warnings == ()


def test_resolve_allow_only() -> None:
    res = resolve_policies(_settings(tool_allow=("echo", "slow_echo")))
    pol = res.policies[Surface.TOOL]
    assert pol.mode == PolicyMode.ALLOW
    assert pol.names == frozenset({"echo", "slow_echo"})
    assert res.policies[Surface.RESOURCE].mode == PolicyMode.UNSET
    # backend_status not in allow-list → startup warning
    assert any("backend_status" in w for w in res.warnings)


def test_resolve_deny_only() -> None:
    res = resolve_policies(_settings(tool_deny=("hang",)))
    pol = res.policies[Surface.TOOL]
    assert pol.mode == PolicyMode.DENY
    assert pol.names == frozenset({"hang"})
    assert res.warnings == ()


def test_resolve_allow_beats_deny_and_warns() -> None:
    res = resolve_policies(
        _settings(
            prompt_allow=("summarize",),
            prompt_deny=("draft",),
        )
    )
    pol = res.policies[Surface.PROMPT]
    assert pol.mode == PolicyMode.ALLOW
    assert pol.names == frozenset({"summarize"})
    assert "draft" not in pol.names
    assert len(res.warnings) == 1
    assert "prompt" in res.warnings[0]
    assert "deny-list discarded" in res.warnings[0]


def test_resolve_backend_status_deny_warns() -> None:
    res = resolve_policies(_settings(tool_deny=(BACKEND_STATUS_TOOL_NAME,)))
    assert any("backend_status" in w for w in res.warnings)


def test_resolve_backend_status_in_allow_no_req7() -> None:
    res = resolve_policies(_settings(tool_allow=("echo", BACKEND_STATUS_TOOL_NAME)))
    assert not any("backend_status" in w for w in res.warnings)


def test_resolve_empty_string_fields_are_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_PROXY_BACKEND_API_KEY", "a" * 32)
    monkeypatch.setenv("MCP_PROXY_TOOL_ALLOW", "")
    monkeypatch.setenv("MCP_PROXY_TOOL_DENY", "  ,  ")
    settings = ProxySettings()
    assert settings.tool_allow is None
    assert settings.tool_deny is None
    res = resolve_policies(settings)
    assert res.policies[Surface.TOOL].mode == PolicyMode.UNSET


def test_parse_name_list_trims_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_PROXY_BACKEND_API_KEY", "a" * 32)
    monkeypatch.setenv("MCP_PROXY_TOOL_ALLOW", " echo , slow_echo,echo ")
    settings = ProxySettings()
    assert settings.tool_allow == ("echo", "slow_echo")


# ---------------------------------------------------------------------------
# U8 ExposureAuditBackend
# ---------------------------------------------------------------------------


class _ListingBackend:
    """Minimal Protocol fake for audit unit tests."""

    def __init__(
        self,
        *,
        tools: list[str] | None = None,
        resources: list[str] | None = None,
        templates: list[str] | None = None,
        prompts: list[str] | None = None,
    ) -> None:
        self._tools = tools or []
        self._resources = resources or []
        self._templates = templates or []
        self._prompts = prompts or []

    async def __aenter__(self) -> _ListingBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def initialize(self) -> mcp_types.InitializeResult:
        return mcp_types.InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="fake", version="0"),
        )

    async def ping(self) -> bool:
        return True

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(name=n, description="", inputSchema={"type": "object"})
            for n in self._tools
        ]

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        return mcp_types.CallToolResult(content=[])

    async def list_resources(self) -> list[mcp_types.Resource]:
        return [
            mcp_types.Resource(uri=u, name=u)  # type: ignore[arg-type]
            for u in self._resources
        ]

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        return [
            mcp_types.ResourceTemplate(uriTemplate=t, name=t) for t in self._templates
        ]

    async def read_resource(self, uri: str) -> list[Any]:
        return []

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return [mcp_types.Prompt(name=n) for n in self._prompts]

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.GetPromptResult:
        return mcp_types.GetPromptResult(messages=[])


@pytest.mark.asyncio
async def test_audit_warns_once_for_unknown_tool_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    res = resolve_policies(_settings(tool_allow=("echo", "deleteall")))
    audit = ExposureAuditBackend(
        _ListingBackend(tools=["echo", "slow_echo"]), res.policies
    )
    with caplog.at_level(
        logging.WARNING, logger="mcp_call_orchestrator_proxy.exposure_audit"
    ):
        await audit.list_tools()
        await audit.list_tools()  # second client — must not re-warn

    msgs = [r.getMessage() for r in caplog.records]
    assert sum("deleteall" in m for m in msgs) == 1
    assert not any("echo" in m and "not found" in m for m in msgs)


@pytest.mark.asyncio
async def test_audit_skips_discarded_deny_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I-1: discarded deny-list names must not be audited."""
    res = resolve_policies(
        _settings(tool_allow=("echo",), tool_deny=("never_existed",))
    )
    assert res.policies[Surface.TOOL].mode == PolicyMode.ALLOW
    assert "never_existed" not in res.policies[Surface.TOOL].names
    audit = ExposureAuditBackend(_ListingBackend(tools=["echo"]), res.policies)
    with caplog.at_level(
        logging.WARNING, logger="mcp_call_orchestrator_proxy.exposure_audit"
    ):
        await audit.list_tools()
    assert not any("never_existed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_audit_backend_status_not_a_typo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    res = resolve_policies(_settings(tool_allow=(BACKEND_STATUS_TOOL_NAME,)))
    audit = ExposureAuditBackend(_ListingBackend(tools=[]), res.policies)
    with caplog.at_level(
        logging.WARNING, logger="mcp_call_orchestrator_proxy.exposure_audit"
    ):
        await audit.list_tools()
    assert not any("backend_status" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_audit_resource_waits_for_both_listings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    res = resolve_policies(
        _settings(resource_allow=("resource://notes/todo", "vault://{id}"))
    )
    audit = ExposureAuditBackend(
        _ListingBackend(resources=["resource://notes/todo"], templates=[]),
        res.policies,
    )
    with caplog.at_level(
        logging.WARNING, logger="mcp_call_orchestrator_proxy.exposure_audit"
    ):
        await audit.list_resources()
        # only one listing so far — no warn yet
        assert not caplog.records
        await audit.list_resource_templates()
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("vault://{id}" in m for m in msgs) == 1
    assert not any("resource://notes/todo" in m and "not found" in m for m in msgs)


# ---------------------------------------------------------------------------
# End-to-end via create_proxy_app: listing, the backend_status warning, defaults
# ---------------------------------------------------------------------------


class MultiToolBackend:
    """Fake backend with two tools for allow/deny filtering tests."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __aenter__(self) -> MultiToolBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def initialize(self) -> mcp_types.InitializeResult:
        return mcp_types.InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=mcp_types.Implementation(name="fake", version="0"),
        )

    async def ping(self) -> bool:
        return True

    async def list_tools(self) -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name="echo",
                description="echo",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            ),
            mcp_types.Tool(
                name="hang",
                description="hang",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        self.calls.append(name)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="ok")]
        )

    async def list_resources(self) -> list[mcp_types.Resource]:
        return []

    async def list_resource_templates(self) -> list[mcp_types.ResourceTemplate]:
        return []

    async def read_resource(self, uri: str) -> list[Any]:
        return []

    async def list_prompts(self) -> list[mcp_types.Prompt]:
        return []

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> mcp_types.GetPromptResult:
        return mcp_types.GetPromptResult(messages=[])


@pytest.mark.asyncio
async def test_default_exposes_all_tools_unchanged() -> None:
    """No filter settings → full surface including backend_status."""
    app = create_proxy_app(settings=_settings(), backend_client=MultiToolBackend())
    async with Client(app.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"echo", "hang", "backend_status"}


@pytest.mark.asyncio
async def test_tool_allow_list_hides_others_and_rejects_call() -> None:
    backend = MultiToolBackend()
    app = create_proxy_app(
        settings=_settings(tool_allow=("echo",)),
        backend_client=backend,
    )
    async with Client(app.mcp) as client:
        names = {t.name for t in await client.list_tools()}
        assert names == {"echo"}
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("hang", {})
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("backend_status", {})
    # Filtered calls never reach the backend.
    assert backend.calls == []


@pytest.mark.asyncio
async def test_tool_deny_list_hides_named_only() -> None:
    app = create_proxy_app(
        settings=_settings(tool_deny=("hang",)),
        backend_client=MultiToolBackend(),
    )
    async with Client(app.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"echo", "backend_status"}


@pytest.mark.asyncio
async def test_allow_list_including_backend_status() -> None:
    app = create_proxy_app(
        settings=_settings(tool_allow=("echo", "backend_status")),
        backend_client=MultiToolBackend(),
    )
    async with Client(app.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"echo", "backend_status"}


@pytest.mark.asyncio
async def test_misconfig_does_not_block_startup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An allow+deny conflict and unknown names still build a working app."""
    with caplog.at_level(logging.WARNING):
        app = create_proxy_app(
            settings=_settings(
                tool_allow=("echo", "typo_tool"),
                tool_deny=("hang",),
            ),
            backend_client=MultiToolBackend(),
        )
    assert any("deny-list discarded" in r.getMessage() for r in caplog.records)
    assert any("backend_status" in r.getMessage() for r in caplog.records)

    async with Client(app.mcp) as client:
        # handshake works
        assert client.initialize_result is not None
        names = {t.name for t in await client.list_tools()}
    assert names == {"echo"}
