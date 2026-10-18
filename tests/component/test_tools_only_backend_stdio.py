"""A backend that implements tools only, reached over stdio through the proxy.

Such a backend advertises the tools capability and nothing else, so the proxy
advertises nothing else to its clients either. A client that lists them anyway is
answered by the backend with JSON-RPC -32601 "Method not found". That is a normal
answer, so it must stay below WARNING and carry no traceback (ARCHITECTURE.md,
"Severity carries meaning"), while clients still get empty listings. The backend is a real MCP server
(``tests/support/tools_only_backend_server.py``), so the error reaches the proxy
in the shape the SDK actually produces.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings

pytestmark = pytest.mark.component

TOOLS_ONLY_BACKEND_SERVER = (
    Path(__file__).parent.parent / "support" / "tools_only_backend_server.py"
)
PROXY_LOGGER = "mcp_call_orchestrator_proxy"


def _write_config(tmp_path: Path) -> str:
    path = tmp_path / "backend.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tools-only-backend": {
                        "command": sys.executable,
                        "args": [str(TOOLS_ONLY_BACKEND_SERVER)],
                    }
                }
            }
        )
    )
    return str(path)


@pytest.mark.asyncio
async def test_unsupported_discovery_is_logged_below_warning_without_traceback(
    tmp_path: Path, serve_over_http, caplog: pytest.LogCaptureFixture
) -> None:
    """Two clients each list tools, prompts, resources and resource templates.

    Each unsupported listing is empty for the client and leaves one INFO record
    without a traceback; nothing from the proxy reaches WARNING.
    """
    caplog.set_level(logging.DEBUG, logger=PROXY_LOGGER)
    settings = ProxySettings(backend_config=_write_config(tmp_path))
    proxy_app = create_proxy_app(settings=settings)

    async with serve_over_http(proxy_app.mcp) as url:
        for _ in range(2):
            async with Client(url) as client:
                tools = {tool.name for tool in await client.list_tools()}
                assert tools == {"echo", "backend_status"}
                assert await client.list_prompts() == []
                assert await client.list_resources() == []
                assert await client.list_resource_templates() == []

    records = [r for r in caplog.records if r.name.startswith(PROXY_LOGGER)]
    loud = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert not loud, f"an unsupported method was logged at WARNING or above: {loud}"

    declined = [r for r in records if "declined by the backend" in r.getMessage()]
    for op in ("list_prompts", "list_resources", "list_resource_templates"):
        for_op = [r for r in declined if f"op={op} " in r.getMessage()]
        assert len(for_op) == 2, f"expected one record per client for {op}"
        assert all(r.levelno == logging.INFO for r in for_op)
        assert all(r.exc_info is None for r in for_op), f"{op} logged a traceback"


@pytest.mark.asyncio
@pytest.mark.parametrize("client_mode", ["legacy", "auto"])
async def test_only_the_capabilities_the_backend_has_are_advertised(
    tmp_path: Path, serve_over_http, client_mode: str
) -> None:
    """The counterpart, a backend that has them, is in test_protocol_matrix_http."""
    proxy_app = create_proxy_app(
        settings=ProxySettings(backend_config=_write_config(tmp_path))
    )

    async with (
        serve_over_http(proxy_app.mcp) as url,
        Client(url, mode=client_mode) as client,
    ):
        capabilities = client.server_capabilities
        assert capabilities is not None
        assert capabilities.tools is not None
        assert capabilities.model_dump(exclude_none=True).keys() == {"tools"}
