"""The ``login`` and ``logout`` commands for an OAuth backend.

Both use the settings ``run`` uses and take the token directory's lock, so
neither runs while a proxy for the same instance is running.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

from mcp_call_orchestrator_proxy.client import (
    build_real_backend_client,
    check_backend_config,
)
from mcp_call_orchestrator_proxy.config import ProxySettings
from mcp_call_orchestrator_proxy.oauth import (
    LoginOAuth,
    OAuthBackend,
    ServiceOAuth,
    open_backend_store,
    resolve_oauth_backend,
)
from mcp_call_orchestrator_proxy.oauth_store import TokenDirLock


def require_oauth_backend(settings: ProxySettings) -> OAuthBackend:
    """The OAuth settings of the configured backend; ``ValueError`` without OAuth."""
    check_backend_config(settings)
    backend = resolve_oauth_backend(settings)
    if backend is None:
        raise ValueError(
            'The backend config has no HTTP server entry with "auth": "oauth"; '
            "there is nothing to sign in to."
        )
    return backend


def _check_callback_port(backend: OAuthBackend) -> None:
    """Refuse at once when the callback port is taken.

    uvicorn, which serves the callback, exits the process on a failed bind.
    """
    family = socket.AF_INET6 if ":" in backend.callback_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((backend.callback_host, backend.callback_port))
        except OSError as exc:
            raise RuntimeError(
                f"The OAuth callback address {backend.callback_host}:"
                f"{backend.callback_port} cannot be used ({exc.strerror}); set "
                "MCP_PROXY_OAUTH_CALLBACK_PORT to a free port."
            ) from None


async def sign_in(
    settings: ProxySettings,
    *,
    timeout: float,
    announce: Callable[[str], None],
    open_browser: bool = True,
) -> list[str]:
    """Sign in to the backend, store the tokens, and return its tool names.

    Stored tokens that still work, or refresh, need no browser. Otherwise the
    authorization URL goes to ``announce`` (and to a browser when
    ``open_browser``), and the callback listener waits up to ``timeout``
    seconds for the person to finish.
    """
    backend = require_oauth_backend(settings)
    token_dir, store = open_backend_store(backend)
    with TokenDirLock(token_dir, role="login"):
        _check_callback_port(backend)
        provider = LoginOAuth(
            backend,
            store,
            callback_timeout=timeout,
            announce=announce,
            open_browser=open_browser,
        )
        client = build_real_backend_client(
            settings,
            auth=provider,
            init_timeout=timeout + settings.connect_timeout_seconds,
        )
        async with client:
            tools = await client.list_tools()
    return [tool.name for tool in tools]


async def sign_out(settings: ProxySettings) -> OAuthBackend:
    """Delete the stored tokens, client registration and metadata of the backend."""
    backend = require_oauth_backend(settings)
    token_dir, store = open_backend_store(backend)
    with TokenDirLock(token_dir, role="logout"):
        await ServiceOAuth(backend, store).forget()
    return backend
