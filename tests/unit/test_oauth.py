"""Unit tests for the OAuth settings and providers.

``ServiceOAuth`` is driven as an httpx auth over a ``MockTransport`` that plays
both the backend and an authorization server on another origin, so each test
sees exactly which requests the provider makes.
"""

from __future__ import annotations

import asyncio
import json
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
from fastmcp.client.auth.oauth import ClientNotFoundError, TokenStorageAdapter
from key_value.aio.protocols import AsyncKeyValue
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

from mcp_call_orchestrator_proxy.config import ProxySettings
from mcp_call_orchestrator_proxy.oauth import (
    LoginOAuth,
    OAuthBackend,
    ServiceOAuth,
    SignInRequiredError,
    TokenEndpointUnavailableError,
    TokenStoreUnreadableError,
    resolve_oauth_backend,
)
from mcp_call_orchestrator_proxy.oauth_store import open_token_store, prepare_token_dir

URL = "https://mcp.example.com/mcp"
TOKEN_ENDPOINT = "https://auth.example.com/oauth2/token"
METADATA = OAuthMetadata.model_validate(
    {
        "issuer": "https://auth.example.com/",
        "authorization_endpoint": "https://auth.example.com/oauth2/authorize",
        "token_endpoint": TOKEN_ENDPOINT,
        "response_types_supported": ["code"],
    }
)


def _write_config(tmp_path: Path, entry: dict[str, Any]) -> str:
    path = tmp_path / "backend.json"
    path.write_text(json.dumps({"mcpServers": {"b": entry}}))
    return str(path)


def _oauth_settings(tmp_path: Path, **extra: Any) -> ProxySettings:
    config = _write_config(tmp_path, {"url": URL, "auth": "oauth"})
    return ProxySettings(backend_config=config, **extra)


# -- settings -----------------------------------------------------------------


def test_oauth_is_off_without_the_oauth_auth_value(tmp_path: Path) -> None:
    for entry in (
        {"url": URL},
        {"url": URL, "auth": "a-bearer-token"},
        {"command": "some-mcp-server"},
    ):
        settings = ProxySettings(backend_config=_write_config(tmp_path, entry))
        assert resolve_oauth_backend(settings) is None
    assert resolve_oauth_backend(ProxySettings(backend_api_key="k")) is None


def test_oauth_settings_resolve_with_their_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    backend = resolve_oauth_backend(_oauth_settings(tmp_path, server_name="inst"))
    assert backend == OAuthBackend(
        url=URL,
        token_dir=tmp_path / "state/mcp-call-orchestrator-proxy/inst/oauth",
        callback_host="127.0.0.1",
        callback_port=27126,
        client_name="inst",
    )


def test_oauth_settings_carry_scopes_and_a_static_client(tmp_path: Path) -> None:
    backend = resolve_oauth_backend(
        _oauth_settings(
            tmp_path,
            oauth_scopes="read, write offline_access",
            oauth_client_id="cid",
            oauth_client_secret="the-secret",
            oauth_token_dir=str(tmp_path / "t"),
        )
    )
    assert backend is not None
    assert backend.scopes == ("read", "write", "offline_access")
    assert backend.client_secret == "the-secret"
    assert "the-secret" not in repr(backend)


def test_oauth_refuses_tls_verification_turned_off(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="MCP_PROXY_BACKEND_VERIFY_TLS=false"):
        resolve_oauth_backend(_oauth_settings(tmp_path, backend_verify_tls=False))


def test_oauth_refuses_a_secret_without_a_client_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="without MCP_PROXY_OAUTH_CLIENT_ID"):
        resolve_oauth_backend(_oauth_settings(tmp_path, oauth_client_secret="s"))


# -- ServiceOAuth -------------------------------------------------------------


def _backend(tmp_path: Path, url: str = URL) -> OAuthBackend:
    return OAuthBackend(
        url=url,
        token_dir=tmp_path / "oauth",
        callback_host="127.0.0.1",
        callback_port=27126,
        client_name="test",
    )


def _store(tmp_path: Path) -> AsyncKeyValue:
    return open_token_store(prepare_token_dir(tmp_path / "oauth"))


async def _seed(
    store: AsyncKeyValue,
    *,
    access: str = "access-1",
    refresh: str | None = "refresh-1",
    expired: bool = False,
) -> None:
    adapter = TokenStorageAdapter(store, server_url=URL)
    await adapter.set_client_info(
        OAuthClientInformationFull(
            client_id="client-1",
            redirect_uris=["http://127.0.0.1:27126/callback"],
            token_endpoint_auth_method="none",
        )
    )
    await adapter.set_tokens(
        OAuthToken(access_token=access, expires_in=3600, refresh_token=refresh)
    )
    if expired:
        await store.put(
            f"{URL}/token_expiry",
            {"expires_at": time.time() - 60},
            collection="mcp-oauth-token-expiry",
        )
    await store.put(
        f"{URL}/discovery",
        {"oauth_metadata": METADATA.model_dump(mode="json", exclude_none=True)},
        collection="mcp-proxy-oauth-discovery",
    )


class FakeServers:
    """The backend at ``URL`` and the authorization server at ``TOKEN_ENDPOINT``."""

    def __init__(self) -> None:
        self.accepted = {"access-1"}
        self.refreshable = {"refresh-1"}
        self.token_status = 200
        self.requests: list[httpx2.Request] = []
        self.issued = 1

    def token_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if str(r.url) == TOKEN_ENDPOINT]

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_ENDPOINT:
            return self._token(request)
        if str(request.url) != URL:
            return httpx2.Response(404)
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        return httpx2.Response(200 if bearer in self.accepted else 401)

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        form = parse_qs(request.content.decode())
        if self.token_status != 200:
            return httpx2.Response(self.token_status, json={"error": "server_error"})
        if form["refresh_token"][0] not in self.refreshable:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        self.refreshable.discard(form["refresh_token"][0])
        self.issued += 1
        access, refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.accepted = {access}
        self.refreshable.add(refresh)
        return httpx2.Response(
            200,
            json={
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": refresh,
            },
        )


async def _send(provider: ServiceOAuth, servers: FakeServers) -> httpx2.Response:
    transport = httpx2.MockTransport(servers)
    async with httpx2.AsyncClient(transport=transport, auth=provider) as http:
        return await http.post(URL, json={})


async def _stored_refresh(store: AsyncKeyValue) -> str | None:
    tokens = await TokenStorageAdapter(store, server_url=URL).get_tokens()
    return tokens.refresh_token if tokens else None


async def test_no_stored_tokens_fail_at_once_without_a_request(tmp_path: Path) -> None:
    servers = FakeServers()
    provider = ServiceOAuth(_backend(tmp_path), _store(tmp_path))
    with pytest.raises(SignInRequiredError, match="no tokens are stored"):
        await _send(provider, servers)
    assert servers.requests == []


async def test_a_stored_token_is_sent_as_a_bearer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await _seed(store)
    servers = FakeServers()
    response = await _send(ServiceOAuth(_backend(tmp_path), store), servers)
    assert response.status_code == 200
    assert len(servers.requests) == 1


async def test_an_expired_token_is_refreshed_at_the_stored_token_endpoint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await _seed(store, expired=True)
    servers = FakeServers()
    response = await _send(ServiceOAuth(_backend(tmp_path), store), servers)
    assert response.status_code == 200
    assert [str(r.url) for r in servers.requests] == [TOKEN_ENDPOINT, URL]
    assert await _stored_refresh(store) == "refresh-2"


async def test_a_rejected_token_is_refreshed_once_and_the_request_retried(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await _seed(store)
    servers = FakeServers()
    servers.accepted = set()

    response = await _send(ServiceOAuth(_backend(tmp_path), store), servers)
    assert response.status_code == 200
    assert [str(r.url) for r in servers.requests] == [URL, TOKEN_ENDPOINT, URL]
    assert await _stored_refresh(store) == "refresh-2"


async def test_a_backend_that_rejects_a_refreshed_token_means_sign_in(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await _seed(store)
    servers = FakeServers()
    servers.accepted = set()

    async def _never(request: httpx2.Request) -> httpx2.Response:
        # Whatever was just issued is refused on the next request.
        response = await servers(request)
        servers.accepted = set()
        return response

    transport = httpx2.MockTransport(_never)
    provider = ServiceOAuth(_backend(tmp_path), store)
    async with httpx2.AsyncClient(transport=transport, auth=provider) as http:
        with pytest.raises(SignInRequiredError, match="rejected a refreshed"):
            await http.post(URL, json={})
    assert len(servers.token_requests()) == 1


async def test_a_rejected_refresh_token_is_never_sent_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await _seed(store, expired=True)
    servers = FakeServers()
    servers.refreshable = set()
    provider = ServiceOAuth(_backend(tmp_path), store)

    with pytest.raises(SignInRequiredError, match=r"rejected .* \(HTTP 400\)"):
        await _send(provider, servers)
    provider.reload()
    with pytest.raises(SignInRequiredError, match="rejected the stored refresh"):
        await _send(provider, servers)
    assert len(servers.token_requests()) == 1


async def test_a_failing_token_endpoint_is_transient_and_keeps_the_tokens(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await _seed(store, expired=True)
    servers = FakeServers()
    servers.token_status = 503
    provider = ServiceOAuth(_backend(tmp_path), store)

    with pytest.raises(TokenEndpointUnavailableError, match="HTTP 503"):
        await _send(provider, servers)
    assert await _stored_refresh(store) == "refresh-1"
    servers.token_status = 200
    provider.reload()
    assert (await _send(provider, servers)).status_code == 200


async def test_an_expired_token_without_a_refresh_token_means_sign_in(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await _seed(store, refresh=None, expired=True)
    servers = FakeServers()
    with pytest.raises(SignInRequiredError, match="no refresh token is stored"):
        await _send(ServiceOAuth(_backend(tmp_path), store), servers)
    assert servers.requests == []


async def test_tokens_are_bound_to_the_backend_url(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await _seed(store)
    other = ServiceOAuth(_backend(tmp_path, "https://mcp.example.com/other"), store)
    with pytest.raises(SignInRequiredError, match="no tokens are stored"):
        await _send(other, FakeServers())


async def test_reload_reads_the_store_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await _seed(store)
    servers = FakeServers()
    provider = ServiceOAuth(_backend(tmp_path), store)
    await _send(provider, servers)

    await _seed(store, access="access-9")
    servers.accepted = {"access-9"}
    provider.reload()
    assert (await _send(provider, servers)).status_code == 200
    assert servers.token_requests() == []


async def test_requests_are_not_serialized_by_the_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await _seed(store)
    release = asyncio.Event()
    first_arrived = asyncio.Event()

    async def _slow_first(request: httpx2.Request) -> httpx2.Response:
        if not first_arrived.is_set():
            first_arrived.set()
            await release.wait()
        return httpx2.Response(200)

    provider = ServiceOAuth(_backend(tmp_path), store)
    transport = httpx2.MockTransport(_slow_first)
    async with httpx2.AsyncClient(transport=transport, auth=provider) as http:
        slow = asyncio.create_task(http.post(URL, json={}))
        await first_arrived.wait()
        # The second request completes while the first is still out.
        fast = await asyncio.wait_for(http.post(URL, json={}), timeout=5)
        assert fast.status_code == 200
        release.set()
        assert (await slow).status_code == 200


async def test_the_service_provider_never_starts_an_interactive_flow(
    tmp_path: Path,
) -> None:
    provider = ServiceOAuth(_backend(tmp_path), _store(tmp_path))
    with pytest.raises(SignInRequiredError):
        await provider.redirect_handler("https://auth.example.com/authorize")
    with pytest.raises(SignInRequiredError):
        await provider.callback_handler()


async def test_the_authorization_server_metadata_survives_a_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    provider = ServiceOAuth(_backend(tmp_path), store)
    provider.context.oauth_metadata = METADATA
    token = httpx2.Response(
        200,
        json={"access_token": "a", "token_type": "Bearer", "refresh_token": "r"},
        request=httpx2.Request("POST", TOKEN_ENDPOINT),
    )
    await provider._handle_token_response(token)

    restarted = ServiceOAuth(_backend(tmp_path), store)
    await restarted._initialize()
    assert restarted.context.oauth_metadata == METADATA


async def test_forget_removes_everything_stored_for_the_backend(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await _seed(store)
    await ServiceOAuth(_backend(tmp_path), store).forget()
    adapter = TokenStorageAdapter(store, server_url=URL)
    assert await adapter.get_tokens() is None
    assert await adapter.get_client_info() is None
    assert (
        await store.get(f"{URL}/discovery", collection="mcp-proxy-oauth-discovery")
        is None
    )


# -- LoginOAuth -----------------------------------------------------------------


def _login_provider(tmp_path: Path, status: int, announced: list[str]) -> LoginOAuth:
    provider = LoginOAuth(
        _backend(tmp_path),
        _store(tmp_path),
        callback_timeout=1.0,
        announce=announced.append,
    )

    def _factory(**_kwargs: Any) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda _r: httpx2.Response(status))
        )

    provider.httpx_client_factory = _factory
    return provider


async def test_login_announces_the_url_and_opens_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    announced: list[str] = []
    url = "https://auth.example.com/oauth2/authorize?client_id=c"
    await _login_provider(tmp_path, 302, announced).redirect_handler(url)
    assert announced == [url]
    assert opened == [url]


async def test_login_reports_a_client_the_server_does_not_know(tmp_path: Path) -> None:
    announced: list[str] = []
    with pytest.raises(ClientNotFoundError):
        await _login_provider(tmp_path, 400, announced).redirect_handler(
            "https://auth.example.com/oauth2/authorize?client_id=gone"
        )
    assert announced == []


async def test_a_request_refreshes_at_most_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await _seed(store, expired=True)
    servers = FakeServers()

    async def _refuse_the_new_token(request: httpx2.Request) -> httpx2.Response:
        response = await servers(request)
        if str(request.url) == TOKEN_ENDPOINT:
            servers.accepted = set()
        return response

    provider = ServiceOAuth(_backend(tmp_path), store)
    transport = httpx2.MockTransport(_refuse_the_new_token)
    async with httpx2.AsyncClient(transport=transport, auth=provider) as http:
        with pytest.raises(SignInRequiredError, match="rejected a refreshed"):
            await http.post(URL, json={})
    assert len(servers.token_requests()) == 1


async def test_unreadable_stored_metadata_is_reported_as_such(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await _seed(store)
    await store.put(
        f"{URL}/discovery",
        {"oauth_metadata": {"issuer": "not a url"}},
        collection="mcp-proxy-oauth-discovery",
    )
    with pytest.raises(TokenStoreUnreadableError, match="ValidationError"):
        await _send(ServiceOAuth(_backend(tmp_path), store), FakeServers())


async def test_an_unusable_200_never_replays_the_spent_refresh_token(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await _seed(store, expired=True)
    servers = FakeServers()

    async def _garbled(request: httpx2.Request) -> httpx2.Response:
        servers.requests.append(request)
        if str(request.url) == TOKEN_ENDPOINT:
            return httpx2.Response(200, content=b"not a token")
        return httpx2.Response(200)

    provider = ServiceOAuth(_backend(tmp_path), store)
    transport = httpx2.MockTransport(_garbled)
    for _attempt in range(2):
        provider.reload()
        async with httpx2.AsyncClient(transport=transport, auth=provider) as http:
            with pytest.raises(SignInRequiredError):
                await http.post(URL, json={})
    # The store still holds refresh-1, but it is not sent a second time.
    assert await _stored_refresh(store) == "refresh-1"
    assert len(servers.token_requests()) == 1


async def test_refreshed_tokens_that_cannot_be_stored_are_kept_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = _store(tmp_path)
    await _seed(store, expired=True)
    servers = FakeServers()
    provider = ServiceOAuth(_backend(tmp_path), store)

    async def _disk_full(tokens: OAuthToken) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provider.token_storage_adapter, "set_tokens", _disk_full)
    assert (await _send(provider, servers)).status_code == 200
    assert "could not be stored" in caplog.text
    assert "refresh-2" not in caplog.text
    monkeypatch.undo()

    # A reconnect does not roll back to the spent refresh-1 on disk.
    provider.reload()
    assert (await _send(provider, servers)).status_code == 200
    assert len(servers.token_requests()) == 1
    assert await _stored_refresh(store) == "refresh-1"


async def test_an_access_token_refused_after_a_refresh_is_not_sent_again(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await _seed(store)
    servers = FakeServers()
    servers.accepted = set()

    async def _refuse_everything(request: httpx2.Request) -> httpx2.Response:
        response = await servers(request)
        servers.accepted = set()
        return response

    provider = ServiceOAuth(_backend(tmp_path), store)
    transport = httpx2.MockTransport(_refuse_everything)
    for _attempt in range(3):
        provider.reload()
        async with httpx2.AsyncClient(transport=transport, auth=provider) as http:
            with pytest.raises(SignInRequiredError, match="rejected a refreshed"):
                await http.post(URL, json={})
    assert len(servers.token_requests()) == 1
    # Two backend requests (access-1, then its successor) and one refresh.
    assert len(servers.requests) == 3


async def test_a_missing_client_registration_is_named_as_such(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await _seed(store)
    await store.delete(f"{URL}/client_info", collection="mcp-oauth-client-info")
    servers = FakeServers()
    with pytest.raises(SignInRequiredError, match="no client registration is stored"):
        await _send(ServiceOAuth(_backend(tmp_path), store), servers)
    assert servers.requests == []
