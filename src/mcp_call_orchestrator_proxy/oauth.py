"""OAuth 2.1 for an HTTP backend whose config entry says ``"auth": "oauth"``.

The proxy builds the provider itself rather than letting FastMCP turn
``"oauth"`` into a default ``OAuth(url)``: that one keeps tokens in memory, picks
a random callback port and opens a browser from inside a headless service.
Two subclasses of FastMCP's ``OAuth`` share one on-disk store
(``oauth_store.py``), keyed by the backend URL:

- ``ServiceOAuth`` (``run``) only uses and refreshes stored tokens. It never
  discovers, registers, opens a browser or listens for a callback: a missing
  or unusable token fails the request at once with ``SignInRequiredError``.
  One instance serves every client ``ResilientBackend`` builds, and re-reads
  the store on each connection attempt (``reload``).
- ``LoginOAuth`` (``login``) runs the full interactive authorization-code flow
  with PKCE, on the configured loopback callback host and port.

The authorization server's metadata is stored with the tokens. The MCP SDK
holds it only in memory, and without it a refresh goes to ``/token`` on the
backend's own origin, which is wrong for an authorization server elsewhere.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import webbrowser
from collections.abc import AsyncGenerator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, override

import httpx2
from fastmcp.client.auth.oauth import ClientNotFoundError, OAuth
from fastmcp.mcp_config import MCPConfig, RemoteMCPServer
from key_value.aio.errors import BaseKeyValueError
from key_value.aio.protocols import AsyncKeyValue
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthMetadata,
    ProtectedResourceMetadata,
)
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from pydantic import ValidationError

from mcp_call_orchestrator_proxy.config import ProxySettings
from mcp_call_orchestrator_proxy.oauth_store import (
    default_token_dir,
    open_token_store,
    prepare_token_dir,
    read_encryption_key,
)

_DISCOVERY_COLLECTION = "mcp-proxy-oauth-discovery"
logger = logging.getLogger(__name__)

_LOGIN_COMMAND = "mcp-call-orchestrator-proxy login"
_AUTHORIZE_OK = frozenset({200, 302, 303, 307, 308})


class SignInRequiredError(RuntimeError):
    """The backend cannot be reached until a person signs in with ``login``.

    A ``RuntimeError``, so ``ResilientBackend`` treats it as a failed
    connection attempt and keeps retrying, with the message as its last error.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"OAuth sign-in required: run `{_LOGIN_COMMAND}` ({reason})")


class TokenEndpointUnavailableError(RuntimeError):
    """The token endpoint failed transiently (HTTP 5xx or 429); retry later."""


class TokenStoreUnreadableError(RuntimeError):
    """The stored tokens exist but cannot be read: a wrong key, or damage."""


@dataclass(frozen=True, slots=True)
class OAuthBackend:
    """What the proxy needs to authenticate to one OAuth backend."""

    url: str
    token_dir: Path
    callback_host: str
    callback_port: int
    client_name: str
    scopes: tuple[str, ...] | None = None
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    encryption_key_file: Path | None = None


def resolve_oauth_backend(settings: ProxySettings) -> OAuthBackend | None:
    """The OAuth settings for the configured backend, or ``None`` without OAuth.

    OAuth is on when the backend config's single entry is an HTTP/SSE server
    with ``"auth": "oauth"``. Raises ``ValueError`` for settings it cannot
    honour. Reads the config file; touches nothing else.
    """
    if not settings.backend_config:
        return None
    config = MCPConfig.from_file(Path(settings.backend_config))
    if len(config.mcpServers) != 1:
        return None
    server = next(iter(config.mcpServers.values()))
    if not isinstance(server, RemoteMCPServer) or server.auth != "oauth":
        return None
    if "backend_verify_tls" in settings.model_fields_set and not (
        settings.backend_verify_tls
    ):
        raise ValueError(
            "The backend uses OAuth, which sends tokens to the backend and its "
            "authorization server, so their TLS certificates are always verified: "
            "remove MCP_PROXY_BACKEND_VERIFY_TLS=false."
        )
    secret = settings.oauth_client_secret
    if secret is not None and not settings.oauth_client_id:
        raise ValueError(
            "MCP_PROXY_OAUTH_CLIENT_SECRET is set without MCP_PROXY_OAUTH_CLIENT_ID."
        )
    token_dir = (
        Path(settings.oauth_token_dir)
        if settings.oauth_token_dir
        else default_token_dir(settings.server_name)
    )
    key_file = settings.oauth_encryption_key_file
    return OAuthBackend(
        url=server.url,
        token_dir=token_dir,
        callback_host=settings.oauth_callback_host,
        callback_port=settings.oauth_callback_port,
        client_name=settings.oauth_client_name or settings.server_name,
        scopes=settings.oauth_scopes,
        client_id=settings.oauth_client_id,
        client_secret=secret.get_secret_value() if secret is not None else None,
        encryption_key_file=Path(key_file) if key_file else None,
    )


def open_backend_store(backend: OAuthBackend) -> tuple[Path, AsyncKeyValue]:
    """Prepare the backend's token directory and open its store.

    Returns the absolute directory and the store. Raises ``TokenDirError`` for
    a directory or key file that cannot be used.
    """
    directory = prepare_token_dir(backend.token_dir)
    key = (
        read_encryption_key(backend.encryption_key_file)
        if backend.encryption_key_file is not None
        else None
    )
    return directory, open_token_store(directory, encryption_key=key)


class _StoredOAuth(OAuth):
    """FastMCP's ``OAuth`` with the authorization server's metadata stored too."""

    def __init__(
        self,
        backend: OAuthBackend,
        store: AsyncKeyValue,
        *,
        callback_timeout: float = 300.0,
    ) -> None:
        self._store = store
        super().__init__(
            mcp_url=backend.url,
            scopes=list(backend.scopes) if backend.scopes else None,
            client_name=backend.client_name,
            token_storage=store,
            callback_port=backend.callback_port,
            callback_host=backend.callback_host,
            callback_timeout=callback_timeout,
            client_id=backend.client_id,
            client_secret=backend.client_secret,
        )

    def reload(self) -> None:
        """Read the store afresh on the next request."""
        self._initialized = False

    def _discovery_key(self) -> str:
        return f"{self.mcp_url}/discovery"

    @override
    async def _initialize(self) -> None:
        try:
            await super()._initialize()
            discovery = (
                await self._store.get(
                    self._discovery_key(), collection=_DISCOVERY_COLLECTION
                )
                or {}
            )
            prm = discovery.get("protected_resource_metadata")
            asm = discovery.get("oauth_metadata")
            if prm:
                self.context.protected_resource_metadata = (
                    ProtectedResourceMetadata.model_validate(prm)
                )
            if asm:
                self.context.oauth_metadata = OAuthMetadata.model_validate(asm)
        except (BaseKeyValueError, ValidationError) as exc:
            raise TokenStoreUnreadableError(
                f"The stored OAuth tokens cannot be read ({type(exc).__name__}): "
                "check MCP_PROXY_OAUTH_ENCRYPTION_KEY_FILE, or run "
                "`mcp-call-orchestrator-proxy logout` and sign in again"
            ) from None
        if auth_server_url := discovery.get("auth_server_url"):
            self.context.auth_server_url = auth_server_url

    @override
    async def _handle_token_response(self, response: httpx2.Response) -> None:
        await super()._handle_token_response(response)
        context = self.context
        prm = context.protected_resource_metadata
        asm = context.oauth_metadata
        await self._store.put(
            self._discovery_key(),
            {
                "protected_resource_metadata": (
                    prm.model_dump(mode="json", exclude_none=True) if prm else None
                ),
                "oauth_metadata": (
                    asm.model_dump(mode="json", exclude_none=True) if asm else None
                ),
                "auth_server_url": context.auth_server_url,
            },
            collection=_DISCOVERY_COLLECTION,
        )

    async def forget(self) -> None:
        """Delete this backend's tokens, client registration and metadata."""
        await self.token_storage_adapter.clear()
        await self._store.delete(
            self._discovery_key(), collection=_DISCOVERY_COLLECTION
        )


@contextlib.contextmanager
def _without_http_trace() -> Iterator[None]:
    """Keep httpcore's DEBUG trace, which logs response headers, quiet.

    An authorization server that approves at once answers with the code in
    the ``Location`` header.
    """
    trace = logging.getLogger("httpcore2")
    level = trace.level
    trace.setLevel(logging.INFO)
    try:
        yield
    finally:
        trace.setLevel(level)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ServiceOAuth(_StoredOAuth):
    """The provider for ``run``: stored tokens and refresh, nothing interactive.

    Unlike the MCP SDK's flow, it holds the provider's lock only while it
    prepares a token, not while the request itself runs, so it does not
    serialize calls the queue lets run concurrently. Each request makes at
    most one refresh. ``login`` cannot run while ``run`` holds the token
    directory, so only a restart after a new ``login`` brings tokens this
    process has not seen; until then it never sends again:

    - a refresh token the authorization server rejected, or one it answered
      with HTTP 200 that could not be used: the server has probably rotated
      it, and many servers revoke the whole grant when a rotated token is
      reused;
    - an access token the backend refused right after a refresh.

    When refreshed tokens cannot be stored, they are kept in memory and
    ``reload`` keeps them until a later refresh stores its successors: the
    store holds only the spent refresh token.
    """

    def __init__(self, backend: OAuthBackend, store: AsyncKeyValue) -> None:
        super().__init__(backend, store)
        self._rejected: set[str] = set()
        self._refused: set[str] = set()
        self._unsaved = False

    @override
    def reload(self) -> None:
        if not self._unsaved:
            super().reload()

    @override
    async def redirect_handler(self, authorization_url: str) -> None:
        raise SignInRequiredError("the backend asked for a new authorization")

    @override
    async def callback_handler(self) -> AuthorizationCodeResult:
        raise SignInRequiredError("the backend asked for a new authorization")

    @override
    async def _auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        context = self.context
        async with context.lock:
            if not self._initialized:
                await self._initialize()
            context.protocol_version = request.headers.get(MCP_PROTOCOL_VERSION_HEADER)
            if context.current_tokens is None or context.client_info is None:
                raise SignInRequiredError("no tokens are stored for this backend")
            if _fingerprint(context.current_tokens.access_token) in self._refused:
                raise SignInRequiredError(
                    "the backend rejected a refreshed access token"
                )
            refreshed = not context.is_token_valid()
            if refreshed:
                refresh = await self._refresh_request("the stored access token expired")
                await self._accept_refresh((yield refresh))
            sent = self._authorize(request)
        response = yield request
        if response.status_code != 401:
            return
        if refreshed:
            self._refuse(sent)
        async with context.lock:
            # Another request may have refreshed while this one was out.
            if self._access_token() == sent:
                refresh = await self._refresh_request(
                    "the backend rejected the stored access token"
                )
                await self._accept_refresh((yield refresh))
            sent = self._authorize(request)
        response = yield request
        if response.status_code == 401:
            self._refuse(sent)

    def _refuse(self, access_token: str | None) -> NoReturn:
        if access_token is not None:
            self._refused.add(_fingerprint(access_token))
        raise SignInRequiredError("the backend rejected a refreshed access token")

    def _access_token(self) -> str | None:
        tokens = self.context.current_tokens
        return tokens.access_token if tokens is not None else None

    def _refresh_token_value(self) -> str | None:
        tokens = self.context.current_tokens
        return tokens.refresh_token if tokens is not None else None

    def _authorize(self, request: httpx2.Request) -> str | None:
        self._add_auth_header(request)
        return self._access_token()

    async def _refresh_request(self, reason: str) -> httpx2.Request:
        refresh_token = self._refresh_token_value()
        if not refresh_token or self.context.client_info is None:
            raise SignInRequiredError(f"{reason} and no refresh token is stored")
        if _fingerprint(refresh_token) in self._rejected:
            raise SignInRequiredError(
                "the authorization server rejected the stored refresh token"
            )
        return await self._refresh_token()

    async def _accept_refresh(self, response: httpx2.Response) -> None:
        sent = self._refresh_token_value()
        spent = _fingerprint(sent) if sent else None
        status = response.status_code
        if status == 200:
            try:
                accepted = await self._handle_refresh_response(response)
            except Exception as exc:
                if spent is not None:
                    self._rejected.add(spent)
                renewed = self._refresh_token_value()
                if renewed is None or _fingerprint(renewed) == spent:
                    raise
                # The SDK holds the new tokens before it stores them.
                self._unsaved = True
                logger.warning(
                    "oauth: the refreshed tokens could not be stored (%s: %s); "
                    "they are kept in memory until a later refresh stores them",
                    type(exc).__name__,
                    exc,
                )
                return
            if accepted:
                self._unsaved = False
                return
            if spent is not None:
                self._rejected.add(spent)
            raise SignInRequiredError("the token endpoint's answer held no token")
        if status >= 500 or status == 429:
            raise TokenEndpointUnavailableError(
                f"The OAuth token endpoint answered HTTP {status}; will retry"
            )
        if spent is not None:
            self._rejected.add(spent)
        raise SignInRequiredError(
            f"the authorization server rejected the stored refresh token "
            f"(HTTP {status})"
        )


class LoginOAuth(_StoredOAuth):
    """The provider for ``login``: the full authorization-code flow.

    ``announce`` receives the authorization URL, which carries no secret: the
    PKCE verifier stays in this process and the code comes back to the
    callback listener. When the authorization server no longer knows the
    stored client, FastMCP's flow clears the stored registration and registers
    again, once.
    """

    def __init__(
        self,
        backend: OAuthBackend,
        store: AsyncKeyValue,
        *,
        callback_timeout: float,
        announce: Callable[[str], None],
        open_browser: bool = True,
    ) -> None:
        super().__init__(backend, store, callback_timeout=callback_timeout)
        self._announce = announce
        self._open_browser = open_browser

    @override
    async def redirect_handler(self, authorization_url: str) -> None:
        # FastMCP's check for a stale client: an authorization server answers
        # an unknown client_id with HTTP 400, before any person is involved.
        with _without_http_trace():
            async with self.httpx_client_factory() as client:
                response = await client.get(authorization_url, follow_redirects=False)
        if response.status_code == 400:
            raise ClientNotFoundError(
                "OAuth client not found - cached credentials may be stale"
            )
        if response.status_code not in _AUTHORIZE_OK:
            raise RuntimeError(
                f"Unexpected authorization response: HTTP {response.status_code}"
            )
        self._announce(authorization_url)
        if self._open_browser:
            with contextlib.suppress(webbrowser.Error):
                webbrowser.open(authorization_url)
