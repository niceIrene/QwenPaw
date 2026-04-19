# -*- coding: utf-8 -*-
"""Bearer-token auth for the remote MCP server.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Awaitable, Callable, MutableMapping

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "qp_mcp_"
_TOKEN_HEX_BYTES = 32  # 256 bits


def load_or_create_token(path: Path) -> str:
    """Return the bearer token at ``path``, creating it on first call.

    The file is created with ``O_CREAT|O_EXCL|O_WRONLY`` and mode 0600
    so (a) concurrent ``serve`` invocations can't race and produce two
    different tokens, and (b) the token is never world-readable.
    """
    path = path.expanduser()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if not token.startswith(TOKEN_PREFIX):
            raise ValueError(
                f"Token file {path} does not look like a qwenpaw-mcp "
                f"token (missing '{TOKEN_PREFIX}' prefix). Delete it to "
                "regenerate.",
            )
        return token

    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{TOKEN_PREFIX}{secrets.token_hex(_TOKEN_HEX_BYTES)}"
    fd = os.open(
        str(path),
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    logger.info("Generated new MCP bearer token at %s", path)
    return token


Scope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[MutableMapping[str, object]]]
Send = Callable[[MutableMapping[str, object]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class BearerAuthMiddleware:
    """Reject requests without a valid ``Authorization: Bearer`` header."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token_bytes = token.encode("utf-8")

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        raw_headers = scope.get("headers") or []
        headers = dict(raw_headers)  # type: ignore[call-overload]
        auth = headers.get(b"authorization", b"")
        if not self._is_valid_bearer(auth):
            await self._reject(send)
            return

        await self._app(scope, receive, send)

    def _is_valid_bearer(self, raw: bytes) -> bool:
        if not raw.startswith(b"Bearer "):
            return False
        provided = raw[len(b"Bearer ") :].strip()
        return hmac.compare_digest(provided, self._token_bytes)

    @staticmethod
    async def _reject(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (
                        b"www-authenticate",
                        b'Bearer realm="qwenpaw-mcp"',
                    ),
                    (b"content-length", b"0"),
                ],
            },
        )
        await send({"type": "http.response.body", "body": b""})
