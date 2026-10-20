"""Description overrides, seen by a real client through the served proxy.

The proxy reads a real overrides file and fronts the spawned probe backend over
real HTTP, through its production ``client.py`` path. A client of each protocol
kind must see the operator's texts for the items the file names, the backend's
own names and schema structure, and the backend's instructions joined with the
operator's, and its calls must still reach the backend unchanged.
"""

from __future__ import annotations

import json
import sys
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
