"""Description overrides, seen by a real client through the served proxy.

The proxy reads a real overrides file and fronts the spawned probe backend over
real HTTP, through its production ``client.py`` path. A client of each protocol
kind must see the operator's texts for the items the file names, the backend's
own names and schema structure, and the backend's instructions joined with the
operator's, and its calls must still reach the backend unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

sys.path.insert(0, str(Path(__file__).parent.parent / "support"))
from probe_backend_server import INSTRUCTIONS

pytestmark = pytest.mark.component

OVERRIDES = {
    "instructions": {"append": "Call echo before anything else."},
    "tools": {
        "echo": {
            "description": {"replace": "Repeat a value."},
            "arguments": {"value": {"replace": "The value to repeat."}},
        },
        "slow_echo": {"description": {"prepend": "Slow."}},
    },
}


def _without_descriptions(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {
            k: _without_descriptions(v) for k, v in schema.items() if k != "description"
        }
    if isinstance(schema, list):
        return [_without_descriptions(v) for v in schema]
    return schema


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend_legacy", [True, False], ids=["backend-2025", "backend-2026"]
)
@pytest.mark.parametrize("client_mode", ["legacy", "auto"])
async def test_a_client_sees_the_operators_texts(
    spawn_backend,
    backend_config_file,
    serve_over_http,
    tmp_path: Path,
    client_mode: str,
    backend_legacy: bool,
) -> None:
    backend = spawn_backend(legacy=backend_legacy)
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(OVERRIDES))
    proxy_app = create_proxy_app(
        settings=ProxySettings(
            backend_config=backend_config_file(backend.url), overrides=str(overrides)
        )
    )

    async with Client(backend.url) as direct:
        own = {t.name: t for t in await direct.list_tools()}

    async with (
        serve_over_http(proxy_app.mcp) as url,
        Client(url, mode=client_mode) as client,
    ):
        assert (
            client.instructions == f"{INSTRUCTIONS}\n\nCall echo before anything else."
        )
        tools = {t.name: t for t in await client.list_tools()}
        echoed = await client.call_tool("echo", {"value": "unchanged"})
        assert echoed.content[0].text == "unchanged"

    assert set(tools) == set(own) | {"backend_status"}
    echo = tools["echo"]
    assert echo.description == "Repeat a value."
    assert echo.input_schema["properties"]["value"]["description"] == (
        "The value to repeat."
    )
    assert tools["slow_echo"].description == f"Slow.\n\n{own['slow_echo'].description}"
    for name, tool in own.items():
        assert _without_descriptions(tools[name].input_schema) == (
            _without_descriptions(tool.input_schema)
        )
        if name not in OVERRIDES["tools"]:
            assert tools[name].description == tool.description


async def _until(condition: Any, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"condition not met within {timeout} s")


@pytest.mark.asyncio
async def test_unknown_names_are_reported_once_across_reconnects(
    spawn_backend,
    backend_config_file,
    serve_over_http,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The check runs on the first connect, as the proxy's own listings, and a
    backend that restarts is not checked again."""
    backend = spawn_backend()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps({"tools": {"absent": {"description": {"replace": "x"}}}})
    )
    proxy_app = create_proxy_app(
        settings=ProxySettings(
            backend_config=backend_config_file(backend.url),
            overrides=str(overrides),
            reconnect_initial_backoff_seconds=0.05,
            reconnect_max_backoff_seconds=0.2,
            backend_health_poll_seconds=0.05,
        )
    )
    caplog.set_level(logging.INFO, logger="mcp_call_orchestrator_proxy")

    def warned() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.getMessage() == "overrides: tool 'absent' not found on backend"
        ]

    async with serve_over_http(proxy_app.mcp) as url, Client(url) as client:
        await _until(lambda: len(warned()) == 1)
        proxy_listings = [
            r.getMessage()
            for r in caplog.records
            if "call accepted client=proxy op=list_" in r.getMessage()
        ]
        assert len(proxy_listings) == 4

        backend.kill()
        # Break detection makes no traffic of its own: a failed call reveals it.
        with pytest.raises(Exception):  # noqa: B017 - any error, as long as it is one
            await client.call_tool("echo", {"value": "x"})
        await _until(
            lambda: not proxy_app.backend_client.status_snapshot()["connected"]
        )
        backend.start()
        await _until(lambda: proxy_app.backend_client.status_snapshot()["connected"])
        # Time for a second check to show, had one been started.
        await asyncio.sleep(0.5)

    assert any("backend reconnected" in r.getMessage() for r in caplog.records)
    assert len(warned()) == 1
    assert (
        sum(
            "call accepted client=proxy op=list_" in r.getMessage()
            for r in caplog.records
        )
        == 4
    )
