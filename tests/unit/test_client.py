"""Unit tests for the real backend client factory.

No network I/O: verifies `build_real_backend_client()` wires settings
onto the resulting `Client`'s transport correctly. Constructing a
`StdioTransport` does not spawn a subprocess either - only `.connect()` does -
so the keep_alive tests below stay offline like the rest of this tier.
"""

import json
from pathlib import Path

from fastmcp.client.auth.bearer import BearerAuth
from fastmcp.client.transports import StdioTransport
from fastmcp.client.transports.http import StreamableHttpTransport

from mcp_call_orchestrator_proxy.client import build_real_backend_client
from mcp_call_orchestrator_proxy.config import ProxySettings


def test_build_real_backend_client_wires_settings() -> None:
    settings = ProxySettings(
        backend_mcp_url="https://127.0.0.1:27124/mcp/",
        backend_api_key="a" * 32,
        backend_verify_tls=False,
    )

    client = build_real_backend_client(settings)

    assert isinstance(client.transport, StreamableHttpTransport)
    assert client.transport.url == settings.backend_mcp_url
    assert client.transport.verify == settings.backend_verify_tls

    assert isinstance(client.transport.auth, BearerAuth)
    assert client.transport.auth.token.get_secret_value() == settings.backend_api_key


def test_stdio_backend_keep_alive_is_forced_off_by_default(tmp_path: Path) -> None:
    """No `keep_alive` in the config: still forced off.

    ResilientBackend discards a stdio transport on every reconnect without an
    explicit close(); left at fastmcp's own default (`True`), that would leak
    the subprocess. See the comment in `client.py::_single_server_transport`.
    """
    config_path = tmp_path / "backend.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"probe": {"command": "some-mcp-server"}}})
    )

    client = build_real_backend_client(ProxySettings(backend_config=str(config_path)))

    assert isinstance(client.transport, StdioTransport)
    assert client.transport.keep_alive is False


def test_stdio_backend_keep_alive_true_in_config_is_overridden(tmp_path: Path) -> None:
    """Even a config that explicitly asks for `keep_alive: true` is overridden.

    "No orphaned child" is this proxy's own invariant, not a per-deployment
    choice a backend config file gets to opt out of.
    """
    config_path = tmp_path / "backend.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "probe": {"command": "some-mcp-server", "keep_alive": True}
                }
            }
        )
    )

    client = build_real_backend_client(ProxySettings(backend_config=str(config_path)))

    assert isinstance(client.transport, StdioTransport)
    assert client.transport.keep_alive is False
