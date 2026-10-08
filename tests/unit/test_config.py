"""Unit tests for configuration.

Tests cover validation, defaults, the two backend sources (individual env
fields vs. a standard mcpServers JSON file), and testability via factory (DIP).
"""

import json
from pathlib import Path

import pytest
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import ValidationError

from mcp_call_orchestrator_proxy.client import build_real_backend_client
from mcp_call_orchestrator_proxy.config import ProxySettings, get_settings


def test_settings_require_a_backend_source() -> None:
    # No backend config file and no api key -> nothing to connect to.
    with pytest.raises(ValidationError):
        ProxySettings()


def test_settings_default_values() -> None:
    settings = ProxySettings(backend_api_key="a" * 32)
    assert settings.backend_mcp_url.endswith("/mcp/")
    assert settings.backend_verify_tls is False
    assert settings.max_concurrent_calls == 1
    assert settings.listen_port == 27125


def test_backend_fields_load_from_prefixed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend fields load from MCP_PROXY_BACKEND_* env vars via the prefix."""
    monkeypatch.setenv("MCP_PROXY_BACKEND_API_KEY", "e" * 32)
    monkeypatch.setenv("MCP_PROXY_BACKEND_MCP_URL", "https://example.test/mcp/")
    monkeypatch.setenv("MCP_PROXY_BACKEND_VERIFY_TLS", "true")

    settings = ProxySettings()
    assert settings.backend_api_key == "e" * 32
    assert settings.backend_mcp_url == "https://example.test/mcp/"
    assert settings.backend_verify_tls is True


def test_settings_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PROXY_LISTEN_PORT", "28080")
    settings = ProxySettings(backend_api_key="b" * 32)
    assert settings.backend_api_key == "b" * 32
    assert settings.listen_port == 28080
    # Port can also be overridden at construction for tests.
    settings2 = ProxySettings(backend_api_key="b" * 32, listen_port=28081)
    assert settings2.listen_port == 28081


def test_get_settings_is_factory() -> None:
    # Factory enables test isolation via overrides
    s1 = get_settings(backend_api_key="c" * 32)
    s2 = get_settings(backend_api_key="d" * 32)
    assert isinstance(s1, ProxySettings)
    assert isinstance(s2, ProxySettings)
    assert s1.backend_api_key != s2.backend_api_key


def test_server_name_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinct `MCP_PROXY_SERVER_NAME` per instance is how a client tells two
    deployments apart - some MCP clients (e.g. VS Code/Copilot's tool picker)
    dedupe/label servers by this reported identity, not by whatever key the
    user gave the entry in their own client config."""
    assert ProxySettings(backend_api_key="a" * 32).server_name == (
        "mcp-call-orchestrator-proxy"
    )

    monkeypatch.setenv("MCP_PROXY_SERVER_NAME", "example-proxy")
    settings = ProxySettings(backend_api_key="a" * 32)
    assert settings.server_name == "example-proxy"


def test_resilience_settings_defaults() -> None:
    settings = ProxySettings(backend_api_key="a" * 32)
    assert settings.connect_timeout_seconds == 30.0
    assert settings.reconnect_initial_backoff_seconds == 1.0
    assert settings.reconnect_max_backoff_seconds == 30.0
    assert settings.reconnect_backoff_multiplier == 2.0
    assert settings.backend_health_poll_seconds == 5.0


def test_resilience_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PROXY_CONNECT_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("MCP_PROXY_RECONNECT_MAX_BACKOFF_SECONDS", "7.5")
    settings = ProxySettings(backend_api_key="a" * 32)
    assert settings.connect_timeout_seconds == 3.0
    assert settings.reconnect_max_backoff_seconds == 7.5


def test_resilience_settings_validation() -> None:
    with pytest.raises(ValidationError):
        ProxySettings(backend_api_key="a" * 32, connect_timeout_seconds=0)
    with pytest.raises(ValidationError):
        ProxySettings(backend_api_key="a" * 32, reconnect_backoff_multiplier=0.5)


# --- Backend via a standard mcpServers JSON config file --------------------


def _write_config(tmp_path: Path, servers: dict) -> str:
    path = tmp_path / "backend.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return str(path)


def test_backend_config_makes_api_key_optional(tmp_path: Path) -> None:
    """A JSON config supplies its own auth, so no separate api key is required."""
    config = _write_config(
        tmp_path, {"backend_mcp": {"url": "https://h/mcp/", "auth": "tok"}}
    )
    settings = ProxySettings(backend_config=config)  # no api key -> still valid
    assert settings.backend_api_key is None


def test_backend_config_builds_transport_with_verify(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        {"backend_mcp": {"url": "https://127.0.0.1:27124/mcp/", "auth": "tok-123"}},
    )
    settings = ProxySettings(backend_config=config, backend_verify_tls=False)
    client = build_real_backend_client(settings)
    assert isinstance(client.transport, StreamableHttpTransport)
    # Our TLS-verify knob is applied on top of the standard format.
    assert client.transport.verify is False


def test_backend_config_rejects_multiple_servers(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        {"a": {"url": "https://h/a/mcp/"}, "b": {"url": "https://h/b/mcp/"}},
    )
    settings = ProxySettings(backend_config=config)
    with pytest.raises(ValueError, match="exactly one MCP server"):
        build_real_backend_client(settings)


def test_backend_config_missing_file_errors() -> None:
    settings = ProxySettings(backend_config="/no/such/backend.json")
    with pytest.raises(ValueError):
        build_real_backend_client(settings)


def test_a_misspelled_log_level_is_rejected_rather_than_ignored() -> None:
    """The one switch whose job is turning on detail must not fail silently.

    An operator who asks for `VERBOSE`, gets INFO, and is told nothing will go
    looking for DEBUG records that were never going to be there - and conclude
    the fault is elsewhere.
    """
    with pytest.raises(ValidationError):
        ProxySettings(backend_api_key="a" * 32, log_level="VERBOSE")


def test_a_log_level_may_be_given_in_lower_case() -> None:
    """`--log-level debug` is what people actually type."""
    settings = ProxySettings(backend_api_key="a" * 32, log_level="debug")
    assert settings.log_level == "DEBUG"


def test_the_default_level_shows_the_queue_working() -> None:
    """INFO by default: the queue's per-call records are the product's evidence
    that it is doing its job, and are worthless if they are off by default.
    """
    assert ProxySettings(backend_api_key="a" * 32).log_level == "INFO"
