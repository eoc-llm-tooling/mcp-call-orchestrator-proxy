"""Unit tests for the real backend client factory.

No network I/O: verifies `build_real_backend_client()` wires settings
onto the resulting `Client`'s transport correctly. Constructing a
`StdioTransport` does not spawn a subprocess either - only `.connect()` does -
so the keep_alive tests below stay offline like the rest of this tier.

Also verifies that a backend entry carrying a FastMCP transform key stops the
proxy at startup, and that the transport is built from the plain entry type
regardless.
"""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastmcp.client.auth.bearer import BearerAuth
from fastmcp.client.transports import StdioTransport
from fastmcp.client.transports.http import StreamableHttpTransport

from mcp_call_orchestrator_proxy import client as client_module
from mcp_call_orchestrator_proxy.app import create_proxy_app
from mcp_call_orchestrator_proxy.client import (
    RelayClientSession,
    build_real_backend_client,
)
from mcp_call_orchestrator_proxy.config import ProxySettings

TRANSFORM_KEYS: list[tuple[str, Any]] = [
    ("tools", {"echo": {"description": "d"}}),
    ("include_tags", ["a"]),
    ("exclude_tags", ["a"]),
]


def _write_config(tmp_path: Path, entry: dict[str, Any]) -> str:
    config_path = tmp_path / "backend.json"
    config_path.write_text(json.dumps({"mcpServers": {"probe": entry}}))
    return str(config_path)


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


@pytest.mark.parametrize(("key", "value"), TRANSFORM_KEYS)
def test_stdio_entry_with_a_transform_key_still_gets_keep_alive_off(
    tmp_path: Path, key: str, value: Any
) -> None:
    """The transport is built from the plain entry type, so a transform key can
    never put a nested proxy between the override and the real transport."""
    config = _write_config(tmp_path, {"command": "some-mcp-server", key: value})

    client = build_real_backend_client(ProxySettings(backend_config=config))

    assert isinstance(client.transport, StdioTransport)
    assert client.transport.keep_alive is False


@pytest.mark.parametrize(("key", "value"), TRANSFORM_KEYS)
def test_http_entry_with_a_transform_key_still_gets_the_tls_setting(
    tmp_path: Path, key: str, value: Any
) -> None:
    config = _write_config(
        tmp_path, {"url": "https://127.0.0.1:27124/mcp/", key: value}
    )

    client = build_real_backend_client(
        ProxySettings(backend_config=config, backend_verify_tls=True)
    )

    assert isinstance(client.transport, StreamableHttpTransport)
    assert client.transport.verify is True


@pytest.mark.parametrize(
    ("key", "value"),
    [*TRANSFORM_KEYS, ("tools", {}), ("include_tags", None)],
)
def test_a_transform_key_in_the_entry_stops_the_proxy_at_startup(
    tmp_path: Path, key: str, value: Any
) -> None:
    """Refused whatever its value - including one FastMCP itself reads as
    absent - with an error naming the key."""
    config = _write_config(tmp_path, {"command": "some-mcp-server", key: value})

    with pytest.raises(ValueError, match=f"'probe' sets '{key}'"):
        create_proxy_app(settings=ProxySettings(backend_config=config))


def test_every_transform_key_present_is_named(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        {"command": "some-mcp-server", "tools": {"echo": {}}, "exclude_tags": ["a"]},
    )

    with pytest.raises(ValueError, match="sets 'tools', 'exclude_tags'"):
        create_proxy_app(settings=ProxySettings(backend_config=config))


def test_a_plain_entry_starts_the_proxy(tmp_path: Path) -> None:
    config = _write_config(tmp_path, {"command": "some-mcp-server"})

    create_proxy_app(settings=ProxySettings(backend_config=config))


@pytest.mark.parametrize(
    "servers",
    [
        {
            "a": {"command": "some-mcp-server"},
            "b": {"url": "https://127.0.0.1:27124/mcp/"},
        },
        {},
    ],
)
def test_a_config_without_exactly_one_server_stops_the_proxy_at_startup(
    tmp_path: Path, servers: dict[str, Any]
) -> None:
    """Refused by ``create_proxy_app``, before any connection is attempted - not
    left to the backend factory, whose error the supervisor would retry."""
    config_path = tmp_path / "backend.json"
    config_path.write_text(json.dumps({"mcpServers": servers}))

    with pytest.raises(ValueError, match=f"found {len(servers)}"):
        create_proxy_app(settings=ProxySettings(backend_config=str(config_path)))


@pytest.mark.parametrize("verify", [True, False])
@pytest.mark.parametrize("source", ["config", "fields"])
def test_the_backend_http_client_reads_longer_than_a_call_may_take(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, verify: bool
) -> None:
    """A call that outlasts its timeout must fail on the call timeout, not on the
    HTTP read timeout, which would end the whole connection; the TLS setting
    must survive the custom HTTP client."""
    url = "https://127.0.0.1:27124/mcp/"
    common: dict[str, Any] = {"call_timeout_seconds": 7.0, "backend_verify_tls": verify}
    if source == "config":
        settings = ProxySettings(
            backend_config=_write_config(tmp_path, {"url": url}), **common
        )
    else:
        settings = ProxySettings(
            backend_mcp_url=url, backend_api_key="a" * 32, **common
        )
    built: list[dict[str, Any]] = []
    monkeypatch.setattr(
        client_module.httpx2, "AsyncClient", lambda **kwargs: built.append(kwargs)
    )

    client = build_real_backend_client(settings)
    assert isinstance(client.transport, StreamableHttpTransport)
    factory = client.transport.httpx_client_factory
    assert factory is not None
    factory(timeout=httpx2.Timeout(30.0, read=7.0))

    (kwargs,) = built
    assert kwargs["verify"] is verify
    assert kwargs["timeout"].read > settings.call_timeout_seconds
    assert kwargs["timeout"].connect == 30.0


@pytest.mark.parametrize("source", ["http-config", "stdio-config", "fields"])
def test_the_backend_client_relays_tool_results_unchecked(
    tmp_path: Path, source: str
) -> None:
    """Each source builds a client whose session skips the output-schema check:
    a mismatch raises ``RuntimeError``, which reads as a broken connection."""
    if source == "fields":
        settings = ProxySettings(
            backend_mcp_url="https://127.0.0.1:27124/mcp/", backend_api_key="a" * 32
        )
    else:
        entry: dict[str, Any] = (
            {"url": "https://127.0.0.1:27124/mcp/"}
            if source == "http-config"
            else {"command": "probe-backend", "args": []}
        )
        settings = ProxySettings(backend_config=_write_config(tmp_path, entry))

    client = build_real_backend_client(settings)

    options = client._transport_options
    assert options is not None
    assert options.session_class is RelayClientSession
