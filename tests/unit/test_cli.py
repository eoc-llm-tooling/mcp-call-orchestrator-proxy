"""Unit tests for the command line entry point."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fastmcp
import pytest
from fastmcp.utilities import version_check
from typer.testing import CliRunner

from mcp_call_orchestrator_proxy import cli


@pytest.fixture
def pypi_fetches(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record each time FastMCP would ask pypi.org for its latest version."""
    fetches: list[bool] = []

    def _fetch(include_prereleases: bool = False) -> str | None:
        fetches.append(include_prereleases)
        return None

    monkeypatch.setattr(version_check, "_fetch_latest_version", _fetch)
    monkeypatch.setattr(version_check, "_read_cache", lambda *_args: (None, 0.0))
    monkeypatch.setattr(fastmcp.settings, "check_for_updates", "stable")
    return fetches


def test_fastmcp_asks_pypi_when_its_update_check_is_on(
    pypi_fetches: list[bool],
) -> None:
    """Control: the recorder sees the check FastMCP's startup banner runs."""
    version_check.check_for_newer_version()
    assert pypi_fetches == [False]


def test_run_turns_off_fastmcps_update_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pypi_fetches: list[bool]
) -> None:
    """The banner FastMCP prints when a server starts would otherwise ask pypi.org
    for a newer release; the proxy makes no outbound call of its own."""
    config = tmp_path / "backend.json"
    config.write_text('{"mcpServers": {"b": {"url": "http://127.0.0.1:1/mcp/"}}}')

    def _run(**_kwargs: Any) -> None:
        # What FastMCP's startup banner does before the server starts serving.
        version_check.check_for_newer_version()

    monkeypatch.setattr(
        cli,
        "create_proxy_app",
        lambda settings: SimpleNamespace(mcp=SimpleNamespace(run=_run)),
    )

    result = CliRunner().invoke(
        cli.app, ["run", "--transport", "stdio", "--backend-config", str(config)]
    )

    assert result.exit_code == 0, result.output
    assert fastmcp.settings.check_for_updates == "off"
    assert pypi_fetches == []
