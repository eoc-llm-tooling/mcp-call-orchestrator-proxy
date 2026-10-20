"""Unit tests for the operator's description overrides.

The file's schema and text operations are checked on their own; what a client
sees is checked through the proxy, in front of a real FastMCP server reached
in-process, so the items carry the descriptions and schemas a real backend
lists. Names and schema structure must come through exactly as the backend has
them; only the description texts the file names may change.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastmcp import Client, FastMCP
from mcp import types as mcp_types
from pydantic import Field

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings
from mcp_call_orchestrator_proxy.metadata import BackendMetadataMiddleware
from mcp_call_orchestrator_proxy.overrides import TextOverride, load_overrides
from mcp_call_orchestrator_proxy.resilient import BackendMetadata


def _backend() -> FastMCP:
    backend = FastMCP("backend", instructions="BACKEND INSTRUCTIONS")

    @backend.tool
    def search(
        query: Annotated[str, Field(description="What to look for.")],
        limit: int = 3,
    ) -> str:
        """Search the index."""
        return query

    @backend.tool
    def untouched(value: str) -> str:
        """Left as the backend has it."""
        return value

    @backend.prompt
    def summarize(style: Annotated[str, Field(description="Tone.")]) -> str:
        """Summarize a text."""
        return style

    @backend.resource("data://readme")
    def readme() -> str:
        """The readme."""
        return "readme"

    @backend.resource("data://item/{name}")
    def item(name: str) -> str:
        """One item."""
        return name

    return backend


def _write(tmp_path: Path, content: dict[str, Any] | str) -> str:
    path = tmp_path / "overrides.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return str(path)


def _proxy(overrides_path: str | None, backend: FastMCP) -> Any:
    return create_proxy_app(
        settings=ProxySettings(backend_api_key="a" * 32, overrides=overrides_path),
        backend_factory=lambda: Client(backend),
    )


def _without_descriptions(schema: Any) -> Any:
    """``schema`` with every ``description`` key removed, at any depth."""
    if isinstance(schema, dict):
        return {
            k: _without_descriptions(v) for k, v in schema.items() if k != "description"
        }
    if isinstance(schema, list):
        return [_without_descriptions(v) for v in schema]
    return schema


@pytest.mark.parametrize(
    ("override", "backend_text", "seen"),
    [
        ({"replace": "OP"}, "BACKEND", "OP"),
        ({"replace": "OP"}, None, "OP"),
        ({"append": "OP"}, "BACKEND", "BACKEND\n\nOP"),
        ({"prepend": "OP"}, "BACKEND", "OP\n\nBACKEND"),
        ({"prepend": "A", "append": "B"}, "BACKEND", "A\n\nBACKEND\n\nB"),
        ({"append": "OP"}, None, "OP"),
        ({"prepend": "OP"}, "", "OP"),
    ],
)
def test_each_operation_gives_the_text_a_client_sees(
    override: dict[str, str], backend_text: str | None, seen: str
) -> None:
    assert TextOverride.model_validate(override).apply(backend_text) == seen


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ({"tools": {"t": {"description": {}}}}, "give 'replace', 'prepend'"),
        (
            {"tools": {"t": {"description": {"replace": "a", "append": "b"}}}},
            "cannot be combined",
        ),
        ({"tools": {"t": {"description": {"insert": "a"}}}}, "insert"),
        ({"tools": {"t": {"name": "renamed"}}}, "name"),
        ({"servers": {}}, "servers"),
        ({"tools": {"backend_status": {"description": {"replace": "a"}}}}, "own tool"),
        ("{not json", "not valid"),
    ],
)
def test_a_file_the_proxy_cannot_honour_stops_it_at_startup(
    tmp_path: Path, content: dict[str, Any] | str, reason: str
) -> None:
    path = _write(tmp_path, content)

    with pytest.raises(ValueError, match="Overrides file") as raised:
        _proxy(path, _backend())

    assert path in str(raised.value)
    assert reason in str(raised.value)


def test_a_missing_file_stops_the_proxy_at_startup(tmp_path: Path) -> None:
    path = str(tmp_path / "absent.json")

    with pytest.raises(ValueError, match="cannot be read"):
        _proxy(path, _backend())


def test_startup_logs_a_summary_with_the_files_hash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path,
        {
            "instructions": {"append": "OP"},
            "tools": {"search": {"arguments": {"query": {"append": "OP"}}}},
            "resources": {"data://readme": {"description": {"append": "OP"}}},
        },
    )
    caplog.set_level(logging.INFO, logger="mcp_call_orchestrator_proxy")

    _proxy(path, _backend())

    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    [record] = [r for r in caplog.records if r.getMessage().startswith("overrides:")]
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        f"overrides: {path} (sha256 {digest}): tools 1 (arguments 1), "
        "prompts 0 (arguments 0), resources 1, instructions yes"
    )
    assert load_overrides(path).sha256 == digest


@pytest.mark.asyncio
async def test_each_named_description_changes_and_nothing_else(tmp_path: Path) -> None:
    backend = _backend()
    path = _write(
        tmp_path,
        {
            "tools": {
                "search": {
                    "description": {"prepend": "Prefer this tool."},
                    "arguments": {
                        "query": {"append": "Plain words."},
                        "limit": {"replace": "At most this many."},
                    },
                }
            },
            "prompts": {
                "summarize": {
                    "description": {"replace": "Summarize briefly."},
                    "arguments": {"style": {"append": "Formal."}},
                }
            },
            "resources": {
                "data://readme": {"description": {"append": "Read first."}},
                "data://item/{name}": {"description": {"replace": "An item."}},
            },
        },
    )

    async with Client(backend) as direct:
        own_tools = {t.name: t for t in await direct.list_tools()}
        own_prompts = {p.name: p for p in await direct.list_prompts()}
        own_resources = await direct.list_resources()
        own_templates = await direct.list_resource_templates()

    async with Client(_proxy(path, backend).mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        prompts = {p.name: p for p in await client.list_prompts()}
        resources = await client.list_resources()
        templates = await client.list_resource_templates()

        assert (await client.call_tool("search", {"query": "q"})).data == "q"

    assert set(tools) == set(own_tools) | {"backend_status"}
    search = tools["search"]
    assert search.description == "Prefer this tool.\n\nSearch the index."
    properties = search.input_schema["properties"]
    assert properties["query"]["description"] == "What to look for.\n\nPlain words."
    assert properties["limit"]["description"] == "At most this many."
    for name, own in own_tools.items():
        assert _without_descriptions(tools[name].input_schema) == (
            _without_descriptions(own.input_schema)
        )
        assert tools[name].output_schema == own.output_schema
    assert tools["untouched"] == own_tools["untouched"]

    assert set(prompts) == set(own_prompts)
    summarize = prompts["summarize"]
    assert summarize.description == "Summarize briefly."
    assert summarize.arguments is not None
    [style] = summarize.arguments
    assert (style.name, style.required) == ("style", True)
    assert own_prompts["summarize"].arguments is not None
    [own_style] = own_prompts["summarize"].arguments
    assert style.description == f"{own_style.description}\n\nFormal."

    assert [str(r.uri) for r in resources] == [str(r.uri) for r in own_resources]
    assert resources[0].description == "The readme.\n\nRead first."
    assert [t.uri_template for t in templates] == [
        t.uri_template for t in own_templates
    ]
    assert templates[0].description == "An item."


@pytest.mark.asyncio
async def test_a_lookup_sees_the_overridden_description(tmp_path: Path) -> None:
    path = _write(tmp_path, {"tools": {"search": {"description": {"replace": "New"}}}})
    proxy_app = _proxy(path, _backend())

    async with Client(proxy_app.mcp) as client:
        await client.list_tools()
        tool = await proxy_app.mcp.get_tool("search")

    assert tool is not None
    assert tool.description == "New"


@pytest.mark.asyncio
async def test_the_cached_schema_stays_the_backends_own(tmp_path: Path) -> None:
    """Each listing applies the overrides afresh, never on top of the last one."""
    path = _write(
        tmp_path,
        {"tools": {"search": {"arguments": {"query": {"append": "Plain words."}}}}},
    )

    async with Client(_proxy(path, _backend()).mcp) as client:
        first = {t.name: t for t in await client.list_tools()}["search"]
        second = {t.name: t for t in await client.list_tools()}["search"]

    assert copy.deepcopy(first.input_schema) == second.input_schema
    assert second.input_schema["properties"]["query"]["description"] == (
        "What to look for.\n\nPlain words."
    )


@pytest.mark.asyncio
async def test_names_the_backend_does_not_have_are_reported_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path,
        {
            "tools": {
                "search": {
                    "arguments": {"query": {"append": "x"}, "page": {"append": "x"}}
                },
                "missing_tool": {"description": {"replace": "x"}},
            },
            "prompts": {"missing_prompt": {"description": {"replace": "x"}}},
            "resources": {
                "data://readme": {"description": {"append": "x"}},
                "data://item/{name}": {"description": {"append": "x"}},
                "data://missing": {"description": {"append": "x"}},
            },
        },
    )
    caplog.set_level(logging.WARNING, logger="mcp_call_orchestrator_proxy")

    async with Client(_proxy(path, _backend()).mcp) as client:
        await client.list_tools()
        await client.list_prompts()
        await client.list_resources()
        # Only once both resource listings are in: a name may be a template's.
        assert not any("data://" in r.getMessage() for r in caplog.records)
        await client.list_resource_templates()
        for _ in range(2):
            await client.list_tools()
            await client.list_prompts()
            await client.list_resources()
            await client.list_resource_templates()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert sorted(warnings) == [
        "overrides: prompt 'missing_prompt' not found on backend",
        "overrides: resource 'data://missing' not found on backend",
        "overrides: tool 'missing_tool' not found on backend",
        "overrides: tool 'search' has no argument 'page' on backend",
    ]


@pytest.mark.asyncio
async def test_without_a_file_the_backends_texts_pass_through() -> None:
    backend = _backend()
    async with Client(backend) as direct:
        own = {t.name: t for t in await direct.list_tools()}

    async with Client(_proxy(None, backend).mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert client.instructions == "BACKEND INSTRUCTIONS"

    for name, tool in own.items():
        assert tools[name] == tool


@pytest.mark.asyncio
@pytest.mark.parametrize("client_mode", ["legacy", "auto"])
@pytest.mark.parametrize(
    ("override", "seen"),
    [
        ({"replace": "OP"}, "OP"),
        ({"append": "OP"}, "BACKEND INSTRUCTIONS\n\nOP"),
        ({"prepend": "OP"}, "OP\n\nBACKEND INSTRUCTIONS"),
    ],
)
async def test_the_instructions_a_client_receives_follow_the_file(
    tmp_path: Path, client_mode: str, override: dict[str, str], seen: str
) -> None:
    path = _write(tmp_path, {"instructions": override})

    async with Client(_proxy(path, _backend()).mcp, mode=client_mode) as client:
        assert client.instructions == seen


def test_instructions_are_unchanged_until_the_backend_has_connected() -> None:
    """Before the first connect the proxy's own answer goes out as it is."""
    result = mcp_types.InitializeResult(
        protocol_version="2025-11-25",
        capabilities=mcp_types.ServerCapabilities(),
        server_info=mcp_types.Implementation(name="proxy", version="0"),
    )
    override = TextOverride(replace="OP")
    connected = BackendMetadata(
        protocol_version="2025-11-25", capabilities=None, instructions=None
    )

    assert BackendMetadataMiddleware(lambda: None, override)._apply(result) == result
    assert (
        BackendMetadataMiddleware(lambda: connected, override)
        ._apply(result)
        .instructions
        == "OP"
    )
