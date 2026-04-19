# -*- coding: utf-8 -*-
"""``qwenpaw-mcp`` console entry point.

Wires the FastMCP instance built in :mod:`.server` behind the
:class:`.auth.BearerAuthMiddleware` and serves it with uvicorn on
``streamable-HTTP``. Designed to sit behind a Cloudflare / ngrok /
Tailscale tunnel so Claude mobile and claude.ai can reach it as a
custom connector.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from .auth import BearerAuthMiddleware, load_or_create_token
from .client import ClientConfig

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_FILE = Path("~/.qwenpaw/mcp_token")
_DEFAULT_BACKEND_FALLBACK = "http://127.0.0.1:8088"


def _resolve_base_url(explicit: str | None) -> str:
    """Pick the QwenPaw backend URL: CLI flag → last-api → fallback."""
    if explicit:
        return explicit.rstrip("/")
    try:
        from ..config.utils import read_last_api
    except Exception:  # noqa: BLE001 - config import must never block startup
        return _DEFAULT_BACKEND_FALLBACK
    try:
        last = read_last_api()
    except Exception:  # noqa: BLE001
        last = None
    if last:
        host, port = last
        return f"http://{host}:{port}"
    return _DEFAULT_BACKEND_FALLBACK


@click.group()
def cli() -> None:
    """QwenPaw remote MCP server."""


@cli.command()
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Refuses 0.0.0.0 unless --allow-public-bind is set.",
)
@click.option(
    "--port",
    default=8089,
    show_default=True,
    type=int,
    help="Port for the MCP HTTP listener.",
)
@click.option(
    "--base-url",
    default=None,
    help="QwenPaw backend URL. Defaults to last-api config or "
    "http://127.0.0.1:8088.",
)
@click.option(
    "--agent-id",
    default="copilot-digest",
    show_default=True,
    help="QwenPaw agent id to forward prompts to. Provision first with: "
    "qwenpaw agents create --name 'Copilot Digest' "
    "--agent-id copilot-digest --skill copilot_digest",
)
@click.option(
    "--workspace",
    default=None,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Copilot Digest workspace directory (contains index.json, articles/). "
    "Enables fast direct-read tools: list_reading_list, get_article, "
    "mark_read, get_stats. Without this flag only assistant-proxied tools "
    "are available.",
)
@click.option(
    "--token-file",
    default=str(_DEFAULT_TOKEN_FILE),
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Where to read or write the bearer token.",
)
@click.option(
    "--print-token",
    is_flag=True,
    default=False,
    help="Print the bearer token to stderr on startup.",
)
@click.option(
    "--timeout",
    default=180.0,
    show_default=True,
    type=float,
    help="Per-request timeout (seconds) when talking to QwenPaw.",
)
@click.option(
    "--log-level",
    default="info",
    show_default=True,
    type=click.Choice(
        ["critical", "error", "warning", "info", "debug"],
        case_sensitive=False,
    ),
)
@click.option(
    "--allow-public-bind",
    is_flag=True,
    default=False,
    help="Permit binding to non-loopback addresses. Off by default.",
)
@click.option(
    "--no-auth",
    is_flag=True,
    default=False,
    help="Disable bearer-token authentication. Useful when Claude.ai "
    "connectors cannot pass a token and the tunnel URL is obscure.",
)
def serve(
    host: str,
    port: int,
    base_url: str | None,
    agent_id: str,
    workspace: str | None,
    token_file: str,
    print_token: bool,
    timeout: float,
    log_level: str,
    allow_public_bind: bool,
    no_auth: bool,
) -> None:
    """Run the remote MCP server."""
    _configure_logging(log_level)

    if not _is_loopback(host) and not allow_public_bind:
        raise click.UsageError(
            f"Refusing to bind to {host}. Use a tunnel in front of "
            "127.0.0.1, or pass --allow-public-bind to override.",
        )

    try:
        from .server import build_mcp_server
    except ImportError as exc:
        raise click.ClickException(
            f"Missing MCP dependency ({exc}). "
            "Install with: pip install 'qwenpaw[mcp]'",
        )

    resolved_base = _resolve_base_url(base_url)
    config = ClientConfig(
        base_url=resolved_base,
        agent_id=agent_id,
        user_id="mcp_claude",
        timeout=timeout,
    )

    mcp = build_mcp_server(config, workspace_dir=workspace)
    try:
        asgi_app = mcp.streamable_http_app()
    except AttributeError as exc:
        raise click.ClickException(
            "Installed MCP SDK does not expose streamable_http_app(). "
            "Upgrade with: pip install -U 'mcp>=1.2.0'",
        ) from exc

    if no_auth:
        logger.warning("Authentication DISABLED — any client can call this server.")
        app = asgi_app
    else:
        token_path = Path(token_file).expanduser()
        token = load_or_create_token(token_path)
        if print_token:
            click.echo(f"MCP bearer token: {token}", err=True)
            click.echo(f"Stored at: {token_path}", err=True)
        app = BearerAuthMiddleware(asgi_app, token)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - uvicorn is a base dep
        raise click.ClickException(f"uvicorn not installed: {exc}")

    logger.info(
        "qwenpaw-mcp listening on http://%s:%d (backend=%s, agent=%s)",
        host,
        port,
        resolved_base,
        agent_id,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _configure_logging(level: str) -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level.upper())
        return
    logging.basicConfig(
        level=level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":
    main()
