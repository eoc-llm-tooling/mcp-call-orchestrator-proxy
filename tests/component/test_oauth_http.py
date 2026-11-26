"""An OAuth backend end to end: ``login`` signs in once, ``run`` then only uses
and refreshes the stored tokens.

The backend and its authorization server are FastMCP's in-memory OAuth
provider in this process (``serve_oauth_backend``), so a test can read and
change what it has registered and issued. The browser is ``FakeBrowser``: it
follows the authorization redirect to the ``login`` callback listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.auth import oauth as fastmcp_oauth
from fastmcp.client.auth.oauth import TokenStorageAdapter
from mcp.client.auth import oauth2 as sdk_oauth
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from mcp_call_orchestrator_proxy.app import ProxyApp, create_proxy_app
from mcp_call_orchestrator_proxy.config import ProxySettings
from mcp_call_orchestrator_proxy.oauth_login import sign_in
from mcp_call_orchestrator_proxy.oauth_store import TokenDirBusyError, open_token_store

pytestmark = pytest.mark.component

SIGN_IN_REQUIRED = "OAuth sign-in required: run `mcp-call-orchestrator-proxy login`"

# Yields the conftest's `OAuthBackendServer` (`.url`, `.auth`).
ServeBackend = Callable[[], contextlib.AbstractAsyncContextManager[Any]]
ServeProxy = Callable[[FastMCP], contextlib.AbstractAsyncContextManager[str]]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


class FakeBrowser:
    """Stands in for ``webbrowser.open``.

    Follows the redirect from a thread: the authorization server and the
    callback listener both run on the test's event loop, which the caller of
    ``webbrowser.open`` is blocking. The listener starts only after the
    browser is opened, so the callback is retried until it accepts. Uses
    ``urllib``, which logs nothing: a real browser is not this process, and
    its requests are not in the proxy's log.
    """

    def __init__(self) -> None:
        self.opened: list[str] = []

    def __call__(self, url: str, *args: Any, **kwargs: Any) -> bool:
        self.opened.append(url)
        threading.Thread(target=self._follow, args=(url,), daemon=True).start()
        return True

    def _follow(self, url: str) -> None:
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(url)
        except urllib.error.HTTPError as redirect:
            location = redirect.headers["location"]
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(location).close()
                return
            except urllib.error.URLError:
                time.sleep(0.05)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> FakeBrowser:
    fake = FakeBrowser()
    monkeypatch.setattr(webbrowser, "open", fake)
    return fake


def _forbid_interaction(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """From here on, record any browser or callback listener as a fault."""
    seen: list[str] = []

    def _browser(url: str, *args: Any, **kwargs: Any) -> bool:
        seen.append("browser")
        return False

    def _listener(*args: Any, **kwargs: Any) -> Any:
        seen.append("listener")
        raise AssertionError("run started an OAuth callback listener")

    monkeypatch.setattr(webbrowser, "open", _browser)
    monkeypatch.setattr(fastmcp_oauth, "create_oauth_callback_server", _listener)
    return seen


def _settings(tmp_path: Path, url: str, **extra: Any) -> ProxySettings:
    config = tmp_path / f"backend-{abs(hash(url))}.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "oauth-backend": {"url": url, "transport": "http", "auth": "oauth"}
                }
            }
        )
    )
    values: dict[str, Any] = {
        "backend_config": str(config),
        "oauth_token_dir": str(tmp_path / "tokens"),
        "oauth_callback_port": _free_port(),
        "reconnect_initial_backoff_seconds": 0.05,
        "reconnect_max_backoff_seconds": 0.2,
        "connect_timeout_seconds": 10.0,
    }
    values.update(extra)
    return ProxySettings(**values)


async def _login(settings: ProxySettings) -> list[str]:
    return await sign_in(settings, timeout=20.0, announce=lambda _url: None)


def _stored(settings: ProxySettings, url: str) -> TokenStorageAdapter:
    assert settings.oauth_token_dir is not None
    store = open_token_store(Path(settings.oauth_token_dir))
    return TokenStorageAdapter(store, server_url=url)


async def _stored_tokens(settings: ProxySettings, url: str) -> OAuthToken:
    tokens = await _stored(settings, url).get_tokens()
    assert tokens is not None
    return tokens


async def _expire_stored_access_token(settings: ProxySettings, url: str) -> None:
    """Make the stored access token look expired to the next ``run``."""
    adapter = _stored(settings, url)
    await adapter._key_value_store.put(
        key=adapter._get_token_expiry_cache_key(),
        value={"expires_at": time.time() - 60},
        collection="mcp-oauth-token-expiry",
    )


@contextlib.asynccontextmanager
async def _running(
    serve_over_http: ServeProxy, settings: ProxySettings
) -> AsyncIterator[tuple[ProxyApp, str]]:
    app = create_proxy_app(settings)
    async with serve_over_http(app.mcp) as proxy_url:
        yield app, proxy_url


async def _echo(proxy_url: str, value: str) -> str:
    async with Client(proxy_url) as client:
        result = await client.call_tool("echo", {"value": value})
    return str(result.data)


async def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.02)


async def test_login_then_run_uses_and_refreshes_the_stored_tokens(
    tmp_path: Path,
    serve_oauth_backend: ServeBackend,
    serve_over_http: ServeProxy,
    browser: FakeBrowser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serve_oauth_backend() as backend:
        settings = _settings(tmp_path, backend.url)
        assert await _login(settings) == ["echo"]
        assert len(browser.opened) == 1
        assert len(backend.auth.clients) == 1

        interaction = _forbid_interaction(monkeypatch)
        await _expire_stored_access_token(settings, backend.url)
        signed_in = (await _stored_tokens(settings, backend.url)).refresh_token

        async with _running(serve_over_http, settings) as (app, proxy_url):
            assert await _echo(proxy_url, "first") == "first"
            refreshed = await _stored_tokens(settings, backend.url)
            # The refresh rotated the refresh token, and the new one is stored.
            assert refreshed.refresh_token != signed_in
            assert signed_in not in backend.auth.refresh_tokens
            assert refreshed.refresh_token in backend.auth.refresh_tokens

            resilient = app.backend_client
            connected_at = resilient.status_snapshot()["last_connected_at"]
            resilient._broken.set()
            await _wait_for(
                lambda: (
                    resilient.status_snapshot()["connected"]
                    and resilient.status_snapshot()["last_connected_at"] != connected_at
                )
            )
            assert await _echo(proxy_url, "after reconnect") == "after reconnect"

            # The backend stops accepting the access token: one refresh, retried.
            del backend.auth.access_tokens[refreshed.access_token]
            assert await _echo(proxy_url, "after 401") == "after 401"
            assert (
                await _stored_tokens(settings, backend.url)
            ).refresh_token != refreshed.refresh_token

        assert interaction == []
        assert len(backend.auth.clients) == 1


async def test_run_without_stored_tokens_reports_sign_in_required_at_once(
    tmp_path: Path,
    serve_oauth_backend: ServeBackend,
    serve_over_http: ServeProxy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _forbid_interaction(monkeypatch)
    async with (
        serve_oauth_backend() as backend,
        _running(serve_over_http, _settings(tmp_path, backend.url)) as (app, url),
    ):
        # The first attempt resolves before the proxy starts serving.
        status = app.backend_client.status_snapshot()
        assert status["connected"] is False
        assert SIGN_IN_REQUIRED in status["last_error"]
        async with Client(url) as client:
            reported = await client.call_tool("backend_status", {})
        assert SIGN_IN_REQUIRED in reported.data["last_error"]
    assert interaction == []
    assert backend.auth.clients == {}


async def test_login_refuses_while_run_holds_the_token_directory(
    tmp_path: Path,
    serve_oauth_backend: ServeBackend,
    serve_over_http: ServeProxy,
    browser: FakeBrowser,
) -> None:
    async with serve_oauth_backend() as backend:
        settings = _settings(tmp_path, backend.url)
        async with _running(serve_over_http, settings):
            with pytest.raises(TokenDirBusyError, match="stop the proxy service"):
                await _login(settings)
        assert browser.opened == []
        # Once the proxy has stopped, the lock is free again.
        assert await _login(settings) == ["echo"]


async def test_tokens_for_one_backend_url_are_not_used_for_another(
    tmp_path: Path,
    serve_oauth_backend: ServeBackend,
    serve_over_http: ServeProxy,
    browser: FakeBrowser,
) -> None:
    async with serve_oauth_backend() as first, serve_oauth_backend() as second:
        await _login(_settings(tmp_path, first.url))
        other = _settings(tmp_path, second.url)
        async with _running(serve_over_http, other) as (app, _url):
            status = app.backend_client.status_snapshot()
        assert SIGN_IN_REQUIRED in status["last_error"]
        assert "no tokens are stored" in status["last_error"]
        assert await _stored(other, second.url).get_tokens() is None
        assert await _stored(other, first.url).get_tokens() is not None


async def test_a_client_the_server_forgot_means_sign_in_for_run_and_a_new_registration_for_login(
    tmp_path: Path,
    serve_oauth_backend: ServeBackend,
    serve_over_http: ServeProxy,
    browser: FakeBrowser,
) -> None:
    async with serve_oauth_backend() as backend:
        settings = _settings(tmp_path, backend.url)
        await _login(settings)
        (forgotten,) = backend.auth.clients
        backend.auth.clients.clear()
        await _expire_stored_access_token(settings, backend.url)

        async with _running(serve_over_http, settings) as (app, _url):
            status = app.backend_client.status_snapshot()
        assert SIGN_IN_REQUIRED in status["last_error"]
        assert "rejected the stored refresh token" in status["last_error"]

        assert await _login(settings) == ["echo"]
        (registered,) = backend.auth.clients
        assert registered != forgotten
        client_info = await _stored(settings, backend.url).get_client_info()
        assert client_info is not None
        assert client_info.client_id == registered


async def test_token_files_are_private_under_a_permissive_umask(
    tmp_path: Path, serve_oauth_backend: ServeBackend, browser: FakeBrowser
) -> None:
    previous = os.umask(0o022)
    try:
        async with serve_oauth_backend() as backend:
            settings = _settings(tmp_path, backend.url)
            await _login(settings)
    finally:
        os.umask(previous)

    root = Path(str(settings.oauth_token_dir))
    files = [path for path in root.rglob("*") if path.is_file()]
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    assert any(path.suffix == ".json" for path in files)
    assert len(directories) > 1
    for directory in directories:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    for file in files:
        assert stat.S_IMODE(file.stat().st_mode) == 0o600, file


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(self.format(record))


@pytest.fixture
def debug_log() -> Iterator[_Capture]:
    """Every record at DEBUG, including FastMCP's, which do not propagate."""
    capture = _Capture()
    loggers = [logging.getLogger(), logging.getLogger("fastmcp")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
    yield capture
    for logger, level in zip(loggers, levels, strict=True):
        logger.removeHandler(capture)
        logger.setLevel(level)


def _record_secrets(backend: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect every code, verifier and token the flows produce."""
    secrets: list[str] = []
    auth = backend.auth
    authorize = auth.authorize
    exchange_code = auth.exchange_authorization_code
    exchange_refresh = auth.exchange_refresh_token
    generate = sdk_oauth.PKCEParameters.generate

    async def _authorize(*args: Any) -> str:
        redirect = await authorize(*args)
        secrets.extend(parse_qs(urlparse(redirect).query)["code"])
        return redirect

    async def _exchange_code(*args: Any) -> OAuthToken:
        token = await exchange_code(*args)
        secrets.extend([token.access_token, str(token.refresh_token)])
        return token

    async def _exchange_refresh(*args: Any) -> OAuthToken:
        token = await exchange_refresh(*args)
        secrets.extend([token.access_token, str(token.refresh_token)])
        return token

    def _generate() -> sdk_oauth.PKCEParameters:
        params = generate()
        secrets.append(params.code_verifier)
        return params

    monkeypatch.setattr(auth, "authorize", _authorize)
    monkeypatch.setattr(auth, "exchange_authorization_code", _exchange_code)
    monkeypatch.setattr(auth, "exchange_refresh_token", _exchange_refresh)
    monkeypatch.setattr(sdk_oauth.PKCEParameters, "generate", _generate)
    return secrets


async def test_no_token_code_verifier_or_secret_reaches_the_logs(
    tmp_path: Path,
    serve_oauth_backend: ServeBackend,
    serve_over_http: ServeProxy,
    browser: FakeBrowser,
    debug_log: _Capture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client_secret = "static-client-secret-" + "x" * 24
    async with serve_oauth_backend() as backend:
        secrets = _record_secrets(backend, monkeypatch)
        callback_port = _free_port()
        backend.auth.clients["static-client"] = OAuthClientInformationFull(
            client_id="static-client",
            client_secret=client_secret,
            redirect_uris=[f"http://127.0.0.1:{callback_port}/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post",
        )
        settings = _settings(
            tmp_path,
            backend.url,
            oauth_callback_port=callback_port,
            oauth_client_id="static-client",
            oauth_client_secret=client_secret,
            log_level="DEBUG",
        )
        # Sign in with a pre-registered confidential client: no registration.
        await _login(settings)
        assert list(backend.auth.clients) == ["static-client"]

        await _expire_stored_access_token(settings, backend.url)
        async with _running(serve_over_http, settings) as (_app, proxy_url):
            # Connect with a refresh, then a call the backend refuses outright.
            assert await _echo(proxy_url, "x") == "x"
            backend.auth.access_tokens.clear()
            backend.auth.refresh_tokens.clear()
            with pytest.raises(Exception, match="not available"):
                await _echo(proxy_url, "y")

    output = capsys.readouterr()
    logged = "\n".join([*debug_log.text, output.out, output.err])
    # The capture saw the flows: their token requests are logged by URL.
    assert f"{backend.url.removesuffix('/mcp')}/token" in logged
    assert SIGN_IN_REQUIRED in logged
    assert len(secrets) >= 5
    for secret in [*secrets, client_secret]:
        assert secret not in logged
