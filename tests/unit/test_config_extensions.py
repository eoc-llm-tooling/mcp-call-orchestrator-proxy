"""Unit tests for FastMCP's extension keys in the backend config entry.

The proxy reads a real ``mcpServers`` file for the keys, and serves a real
FastMCP backend reached in-process (``backend_factory``), so what a client sees
is what the keys asked for. ``tools`` is honoured in part: descriptions become
text overrides and ``enabled: false`` a deny entry. Every other field, and the
tag keys, stop the proxy at startup with an error naming the key and why.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings


def _backend(calls: list[str] | None = None) -> FastMCP:
    backend = FastMCP("backend")

    @backend.tool
    def search(query: Annotated[str, Field(description="What to look for.")]) -> str:
        """Search the index."""
        if calls is not None:
            calls.append("search")
        return query

    @backend.tool
    def delete(key: str) -> str:
        """Delete an entry."""
        if calls is not None:
            calls.append("delete")
        return key

    return backend


def _config(tmp_path: Path, **keys: Any) -> str:
    path = tmp_path / "backend.json"
    entry = {"command": "some-mcp-server", **keys}
    path.write_text(json.dumps({"mcpServers": {"probe": entry}}))
    return str(path)


def _proxy(
    backend: FastMCP, config: str, **settings: Any
) -> Any:  # the app, served in-process
    return create_proxy_app(
        settings=ProxySettings(backend_config=config, **settings),
        backend_factory=lambda: Client(backend),
    )


@pytest.mark.parametrize("key", ["include_tags", "exclude_tags"])
@pytest.mark.parametrize("value", [["a"], [], None])
def test_a_tag_key_stops_the_proxy_and_points_to_exposure_control(
    tmp_path: Path, key: str, value: Any
) -> None:
    config = _config(tmp_path, **{key: value})

    with pytest.raises(ValueError) as raised:
        _proxy(_backend(), config)

    message = str(raised.value)
    assert f"- {key}: not supported" in message
    assert "MCP_PROXY_TOOL_ALLOW / MCP_PROXY_TOOL_DENY" in message


@pytest.mark.parametrize(
    ("tools", "named"),
    [
        ({"search": {"name": "find"}}, "tools.search.name: it renames the tool"),
        ({"search": {"title": "Find"}}, "tools.search.title: only descriptions"),
        ({"search": {"version": "2"}}, "tools.search.version: it changes the tool's"),
        ({"search": {"tags": ["a"]}}, "tools.search.tags: it changes the tool's"),
        ({"search": {"meta": {"a": 1}}}, "tools.search.meta: it changes the tool's"),
        ({"search": {"bogus": 1}}, "tools.search.bogus: not a field"),
        (
            {"search": {"arguments": {"query": {"name": "q"}}}},
            "tools.search.arguments.query.name: it renames the argument",
        ),
        (
            {"search": {"arguments": {"query": {"hide": True}}}},
            "tools.search.arguments.query.hide: it hides the argument",
        ),
        (
            {"search": {"arguments": {"query": {"default": "x"}}}},
            "tools.search.arguments.query.default: it gives the argument a default",
        ),
        (
            {"search": {"arguments": {"query": {"required": True}}}},
            "tools.search.arguments.query.required: it changes which arguments",
        ),
        (
            {"search": {"arguments": {"query": {"examples": ["x"]}}}},
            "tools.search.arguments.query.examples: it adds examples",
        ),
        (
            {"backend_status": {"enabled": False}},
            "tools.backend_status: the proxy's own tool",
        ),
    ],
)
def test_a_tools_field_the_proxy_does_not_apply_stops_it_at_startup(
    tmp_path: Path, tools: dict[str, Any], named: str
) -> None:
    config = _config(tmp_path, tools=tools)

    with pytest.raises(ValueError, match="sets keys this proxy does not apply") as e:
        _proxy(_backend(), config)

    assert f"- {named}" in str(e.value)
    assert config in str(e.value)


def test_every_refused_key_is_named_in_one_error(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        tools={"search": {"name": "find", "description": "Find."}},
        exclude_tags=["a"],
    )

    with pytest.raises(ValueError) as raised:
        _proxy(_backend(), config)

    lines = str(raised.value).splitlines()
    assert [line.split(":")[0] for line in lines if line.startswith("- ")] == [
        "- exclude_tags",
        "- tools.search.name",
    ]


@pytest.mark.asyncio
async def test_descriptions_in_the_tools_key_reach_the_client(tmp_path: Path) -> None:
    backend = _backend()
    config = _config(
        tmp_path,
        tools={
            "search": {
                "description": "Find entries.",
                "arguments": {"query": {"description": "Plain words."}},
            }
        },
    )
    async with Client(backend) as direct:
        own = {t.name: t for t in await direct.list_tools()}

    async with Client(_proxy(backend, config).mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

    search = tools["search"]
    assert search.description == "Find entries."
    assert search.input_schema["properties"]["query"]["description"] == "Plain words."
    assert search.input_schema["required"] == own["search"].input_schema["required"]
    assert tools["delete"] == own["delete"]


@pytest.mark.asyncio
async def test_a_disabled_tool_is_hidden_and_uncallable(tmp_path: Path) -> None:
    """FastMCP's own handling only drops the tool from listings."""
    calls: list[str] = []
    config = _config(tmp_path, tools={"delete": {"enabled": False}})

    async with Client(_proxy(_backend(calls), config).mcp) as client:
        names = {t.name for t in await client.list_tools()}
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("delete", {"key": "k"})
        # Control: a tool the config leaves enabled reaches the backend.
        await client.call_tool("search", {"query": "q"})

    assert names == {"search", "backend_status"}
    assert calls == ["search"]


@pytest.mark.asyncio
async def test_enabled_true_changes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path, tools={"delete": {"enabled": True}})

    async with Client(_proxy(_backend(), config).mcp) as client:
        names = {t.name for t in await client.list_tools()}

    assert names == {"search", "delete", "backend_status"}


@pytest.mark.asyncio
async def test_a_disabled_tool_stays_hidden_when_the_allow_list_names_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(tmp_path, tools={"delete": {"enabled": False}})
    caplog.set_level(logging.WARNING, logger="mcp_call_orchestrator_proxy")

    proxy_app = _proxy(
        _backend(), config, tool_allow=("search", "delete", "backend_status")
    )
    async with Client(proxy_app.mcp) as client:
        names = {t.name for t in await client.list_tools()}

    assert names == {"search", "backend_status"}
    assert [r.getMessage() for r in caplog.records] == [
        "exposure: tool 'delete' is in the tool allow-list but the backend config "
        "disables it; it stays hidden"
    ]


@pytest.mark.asyncio
async def test_a_disabled_tool_joins_the_deny_list(tmp_path: Path) -> None:
    config = _config(tmp_path, tools={"delete": {"enabled": False}})

    proxy_app = _proxy(_backend(), config, tool_deny=("search",))
    async with Client(proxy_app.mcp) as client:
        names = {t.name for t in await client.list_tools()}

    assert names == {"backend_status"}


@pytest.mark.asyncio
async def test_the_overrides_file_wins_over_the_tools_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(
        tmp_path,
        tools={
            "search": {
                "description": "From the config.",
                "arguments": {"query": {"description": "From the config."}},
            },
            "delete": {"description": "Config only."},
        },
    )
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "tools": {
                    "search": {
                        "description": {"append": "From the file."},
                        "arguments": {"query": {"replace": "From the file."}},
                    }
                }
            }
        )
    )
    caplog.set_level(logging.INFO, logger="mcp_call_orchestrator_proxy")

    proxy_app = _proxy(_backend(), config, overrides=str(overrides))
    async with Client(proxy_app.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

    search = tools["search"]
    assert search.description == "Search the index.\n\nFrom the file."
    assert search.input_schema["properties"]["query"]["description"] == (
        "From the file."
    )
    assert tools["delete"].description == "Config only."
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        f"overrides: tools.search.{item} is set by both the overrides file and the "
        "backend config; the overrides file's entry applies"
        for item in ("description", "arguments.query")
    ]


def test_startup_logs_what_the_tools_key_applies(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(
        tmp_path,
        tools={
            "search": {
                "description": "Find.",
                "arguments": {"query": {"description": "Words."}},
            },
            "delete": {"enabled": False},
        },
    )
    caplog.set_level(logging.INFO, logger="mcp_call_orchestrator_proxy")

    _proxy(_backend(), config)

    assert [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("backend")
    ] == [
        "backend config: tools key applied: tool descriptions 1, argument "
        "descriptions 1, disabled tools: delete"
    ]


def test_an_empty_tools_key_starts_the_proxy(tmp_path: Path) -> None:
    _proxy(_backend(), _config(tmp_path, tools={}))


@pytest.mark.asyncio
async def test_a_tools_key_that_only_disables_checks_no_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With no description to apply, the proxy makes no listing of its own."""
    config = _config(tmp_path, tools={"delete": {"enabled": False}})
    caplog.set_level(logging.INFO, logger="mcp_call_orchestrator_proxy")

    async with Client(_proxy(_backend(), config).mcp) as client:
        await asyncio.sleep(0.2)
        assert "client=proxy" not in caplog.text

    # Control: descriptions in the key are checked once the backend connects.
    described = _config(tmp_path, tools={"absent": {"description": "x"}})
    async with Client(_proxy(_backend(), described).mcp) as client:
        for _ in range(100):
            if "tool 'absent' not found on backend" in caplog.text:
                break
            await asyncio.sleep(0.02)
        await client.list_tools()

    assert "call accepted client=proxy op=list_tools" in caplog.text
    assert "overrides: tool 'absent' not found on backend" in caplog.text
