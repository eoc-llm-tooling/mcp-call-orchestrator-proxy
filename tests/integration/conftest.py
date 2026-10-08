"""Pytest fixtures and configuration for integration tests.

These tests require a running backend MCP server.

They are skipped by default unless explicitly requested with `-m integration`,
and further skipped unless a backend config file exists.

Configuration is a single standard ``mcpServers`` JSON file at
``tests/integration/backend.json`` (gitignored - copy ``backend.example.json``
and fill in the real URL + token). The auth token lives inside that file (and
thus inside the built transport), never inside ``ProxySettings`` - so the
settings object is safe to print.
"""

import logging
from pathlib import Path
from typing import Any

import pytest

from mcp_call_orchestrator_proxy.client import build_real_backend_client
from mcp_call_orchestrator_proxy.config import ProxySettings

_BACKEND_JSON = Path(__file__).parent / "backend.json"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: integration tests against a real backend MCP server",
    )


def _backend_config_path() -> str:
    if not _BACKEND_JSON.exists():
        pytest.skip(
            f"Integration backend config not found: {_BACKEND_JSON}. "
            "Copy backend.example.json to backend.json and fill in the token."
        )
    return str(_BACKEND_JSON)


@pytest.fixture(scope="session")
def real_settings() -> ProxySettings:
    """Settings pointed at the real backend via the single mcpServers JSON file.

    The auth token lives inside the JSON (and the built transport), never in
    these settings - safe to expose to test bodies and print.
    """
    return ProxySettings(backend_config=_backend_config_path())


@pytest.fixture(autouse=True, scope="session")
def _suppress_sensitive_logging():
    """Prevent auth tokens / headers from being printed in logs during tests."""
    for name in ("httpx", "mcp", "httpcore", "anyio"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Extra filter to redact any "Bearer ..." that might leak in logs.
    class RedactBearerFilter(logging.Filter):
        def filter(self, record):
            if (
                hasattr(record, "msg")
                and isinstance(record.msg, str)
                and "Bearer " in record.msg
            ):
                record.msg = record.msg.replace(
                    record.msg.split("Bearer ")[-1].split()[0]
                    if " " in record.msg.split("Bearer ")[-1]
                    else "",
                    "<redacted>",
                )
            return True

    for name in ("httpx", "mcp", "httpcore"):
        logging.getLogger(name).addFilter(RedactBearerFilter())

    yield


@pytest.fixture
async def real_client(real_settings):
    """Provide a connected real backend Client for the duration of a test."""
    client = build_real_backend_client(real_settings)
    try:
        await client.__aenter__()
    except Exception as e:
        msg = str(e)
        if (
            isinstance(e, (ConnectionError, OSError))
            or "ConnectError" in str(type(e))
            or "connection" in msg.lower()
            or "All connection attempts failed" in msg
        ):
            pytest.skip(f"Could not connect to backend MCP: {e}")
        # For ExceptionGroup from anyio, check sub exceptions
        if hasattr(e, "exceptions"):
            for sub in getattr(e, "exceptions", []):
                sub_msg = str(sub)
                if "ConnectError" in sub_msg or "connection" in sub_msg.lower():
                    pytest.skip(f"Could not connect to backend MCP: {sub}")
        raise
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


@pytest.fixture
async def probe_tool(real_client) -> tuple[str, dict[str, Any]]:
    """Select a backend tool + args that can be used to exercise calls through the proxy.

    Prefers tools declaring no required parameters (safest to call with {}).
    Falls back to the first available tool. Skips the suite if the backend
    provides no tools at all.

    This keeps the integration tests backend-neutral: no hardcoded tool names.
    """
    tools = await real_client.list_tools()
    if not tools:
        pytest.skip("Backend MCP server exposes no tools")

    # Prefer tool with no required args
    for tool in tools:
        schema = tool.inputSchema or {}
        if not schema.get("required"):
            return tool.name, {}

    # Fallback: first tool (call may require specific args for some backends,
    # but still exercises proxy routing, queue, sessions, and parity)
    first = tools[0]
    return first.name, {}
