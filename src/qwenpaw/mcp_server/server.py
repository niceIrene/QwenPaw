# -*- coding: utf-8 -*-
"""FastMCP server exposing Copilot Digest over streamable-HTTP.

Eight tools are registered:

**Direct workspace tools** (fast, no assistant round-trip):
* ``list_reading_list(status?, timeframe?, topic?, limit?)``
* ``get_article(article_id)``
* ``mark_read(article_id)``
* ``get_stats()``

**Assistant-proxied tools** (need LLM for summarization / conversation):
* ``send_message(text)``
* ``ingest_url(url, title?)``
* ``ingest_file(path, title?)``

**Session management:**
* ``reset_session()``

Direct workspace tools require ``--workspace`` to be set on the CLI.
When omitted, only the assistant-proxied and session tools are available.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .client import ClientConfig, ingest_file, send_message
from .workspace import (
    get_article as ws_get_article,
    get_stats as ws_get_stats,
    list_items as ws_list_items,
    mark_read as ws_mark_read,
    mark_unread as ws_mark_unread,
)

logger = logging.getLogger(__name__)

_MCP_USER_ID = "mcp_claude"


class _SessionRegistry:
    """Map MCP connection IDs to QwenPaw session IDs."""

    def __init__(self) -> None:
        self._by_mcp_session: dict[str, str] = {}
        self._fallback: str = uuid.uuid4().hex

    def get(self, mcp_session_id: str | None) -> str:
        key = mcp_session_id or "__fallback__"
        session = self._by_mcp_session.get(key)
        if session is None:
            session = self._fallback if key == "__fallback__" else uuid.uuid4().hex
            self._by_mcp_session[key] = session
        return session

    def reset(self, mcp_session_id: str | None) -> str:
        key = mcp_session_id or "__fallback__"
        new_session = uuid.uuid4().hex
        self._by_mcp_session[key] = new_session
        if key == "__fallback__":
            self._fallback = new_session
        return new_session


def _mcp_session_id(ctx: Context | None) -> str | None:
    """Best-effort extraction of the MCP session identifier.

    FastMCP's ``Context`` wraps the underlying request context; the
    streamable-HTTP transport stores a session id there. The exact
    attribute name has shifted across SDK minor versions, so probe
    defensively and degrade to the per-process fallback on miss.
    """
    if ctx is None:
        return None
    for attr in ("session_id", "client_id", "request_id"):
        value = getattr(ctx, attr, None)
        if isinstance(value, str) and value:
            return value
    request_ctx: Any = getattr(ctx, "request_context", None)
    if request_ctx is not None:
        for attr in ("session_id", "client_id"):
            value = getattr(request_ctx, attr, None)
            if isinstance(value, str) and value:
                return value
    return None


def build_mcp_server(
    config: ClientConfig,
    workspace_dir: str | None = None,
) -> FastMCP:
    """Create a FastMCP instance wired to ``config``.

    Parameters
    ----------
    config : ClientConfig
        Connection settings for the QwenPaw backend.
    workspace_dir : str | None
        Path to the Copilot Digest workspace directory. When provided,
        the direct workspace tools (``list_reading_list``,
        ``get_article``, ``mark_read``, ``get_stats``) are registered.
    """
    mcp = FastMCP(
        "qwenpaw-copilot-digest",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    sessions = _SessionRegistry()
    ws_path = Path(workspace_dir) if workspace_dir else None

    client_config = ClientConfig(
        base_url=config.base_url,
        agent_id=config.agent_id,
        user_id=_MCP_USER_ID,
        timeout=config.timeout,
    )

    # ------------------------------------------------------------------
    # Direct workspace tools (fast, no assistant round-trip)
    # ------------------------------------------------------------------

    if ws_path is not None:

        @mcp.tool()
        async def list_reading_list_tool(
            ctx: Context,
            status: str = "unread",
            timeframe: str = "all",
            topic: str | None = None,
            limit: int = 50,
        ) -> str:
            """Browse the user's PERSONAL reading list and knowledge base.

            YOU MUST call this tool when the user asks about their saved
            articles, reading list, bookmarks, or unread items. Examples:
            - "What's in my reading list?"
            - "Show me my unread articles"
            - "What did I save this week?"
            - "Any new items on fintech?"
            - "What's new?"

            You CANNOT answer these questions from your own knowledge.
            The user's reading list is stored on their machine and only
            accessible through this tool.

            Returns article metadata and summaries. To read the full text
            of a specific article, use get_article with the article ID.

            Parameters:
            - status: "unread" (default), "read", or "all"
            - timeframe: "all" (default), "today", "yesterday", "week", "month"
            - topic: filter to items matching this topic (optional)
            - limit: max items to return (default 50)
            """
            return ws_list_items(
                ws_path, status=status, timeframe=timeframe,
                topic=topic, limit=limit,
            )

        list_reading_list_tool.__name__ = "list_reading_list"

        @mcp.tool()
        async def get_article_tool(article_id: str, ctx: Context) -> str:
            """Retrieve the FULL TEXT of a specific article from the user's
            personal knowledge base.

            YOU MUST call this tool when the user asks about a specific
            article's content — for example "tell me about the paper I
            saved", "what does the Nvidia article say?", "summarize
            article abc123", or any question about a saved article.

            Do NOT answer from your own knowledge. The article content is
            stored on the user's machine and only accessible through this
            tool. Always retrieve the article first, then use its content
            to answer the user's question.

            IMPORTANT: When presenting this article to the user, show the
            content EXACTLY as returned by this tool. Do NOT summarize,
            paraphrase, or condense the returned text. The user wants to
            see what is stored in their knowledge base, not your
            interpretation of it. Present the full text verbatim.

            WORKFLOW: First call list_reading_list to find the article's
            exact ID (e.g. "a1b2c3d4"), then pass that ID here. Do NOT
            pass the article title or user's query as article_id.

            Parameters:
            - article_id: The exact item ID from list_reading_list results
              (e.g. "a1b2c3d4")
            """
            return ws_get_article(ws_path, article_id)

        get_article_tool.__name__ = "get_article"

        @mcp.tool()
        async def mark_read_tool(article_id: str, ctx: Context) -> str:
            """Mark an article as read in the user's knowledge base.

            Call this after the user has reviewed an article or when they
            ask to mark items as read. This updates the reading status in
            the index — it does NOT delete the article.

            Parameters:
            - article_id: The item ID to mark as read
            """
            return ws_mark_read(ws_path, article_id)

        mark_read_tool.__name__ = "mark_read"

        @mcp.tool()
        async def mark_unread_tool(article_id: str, ctx: Context) -> str:
            """Mark an article as unread in the user's knowledge base.

            Use when the user wants to mark a previously read article back
            as unread so it appears in unread listings again.

            Parameters:
            - article_id: The item ID to mark as unread
            """
            return ws_mark_unread(ws_path, article_id)

        mark_unread_tool.__name__ = "mark_unread"

        @mcp.tool()
        async def get_stats_tool(ctx: Context) -> str:
            """Show statistics about the user's knowledge base.

            Use when the user asks "how many articles do I have?",
            "knowledge base stats", or "what topics do I track?".

            Returns: total items, unread/read counts, items by source type,
            top topics, and recent activity.
            """
            return ws_get_stats(ws_path)

        get_stats_tool.__name__ = "get_stats"

        logger.info(
            "Workspace tools enabled (workspace=%s): "
            "list_reading_list, get_article, mark_read, get_stats",
            ws_path,
        )
    else:
        logger.warning(
            "No --workspace provided. Direct workspace tools "
            "(list_reading_list, get_article, mark_read, get_stats) "
            "are DISABLED. Pass --workspace to enable them.",
        )

    # ------------------------------------------------------------------
    # Assistant-proxied tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def send_message_tool(text: str, ctx: Context) -> str:
        """Send a message to the user's Copilot Digest assistant for
        conversation, discussion, or complex tasks.

        Use this tool for:
        - Discussing a specific article in depth ("let's discuss the SEC case")
        - Requesting work output ("draft a summary", "write action items")
        - Configuring interests ("add fintech to my topics")
        - Generating briefings ("give me today's briefing")
        - Any conversational follow-up with the assistant

        For simple lookups (listing articles, reading content, checking
        stats), prefer the faster direct tools: list_reading_list,
        get_article, get_stats.
        """
        session_id = sessions.get(_mcp_session_id(ctx))

        async def _progress(msg: str) -> None:
            await ctx.info(msg)

        await ctx.info("Forwarding to Copilot Digest assistant...")
        return await send_message(
            text, client_config, session_id, progress_cb=_progress,
        )

    send_message_tool.__name__ = "send_message"

    @mcp.tool()
    async def ingest_url_tool(
        url: str,
        ctx: Context,
        title: str | None = None,
    ) -> str:
        """Save a web URL to the user's personal reading list.

        YOU MUST call this tool when the user shares a URL and wants to
        save, bookmark, or add it to their reading list. Examples:
        - "Save this: https://example.com/article"
        - "Add this to my reading list: <url>"
        - "Bookmark this URL"

        The assistant will fetch the page, extract the content, generate
        a summary, and add it to the knowledge base. This may take 30-60
        seconds.

        Parameters:
        - url: The web URL to save
        - title: Optional custom title (auto-detected if omitted)
        """
        session_id = sessions.get(_mcp_session_id(ctx))
        prompt_lines = [f"Save this URL to my reading list: {url}"]
        if title:
            prompt_lines.append(f"Title: {title}")

        async def _progress(msg: str) -> None:
            await ctx.info(msg)

        await ctx.info(f"Ingesting URL: {url}")
        return await send_message(
            "\n".join(prompt_lines), client_config, session_id,
            progress_cb=_progress,
        )

    ingest_url_tool.__name__ = "ingest_url"

    @mcp.tool()
    async def ingest_file_tool(
        path: str,
        ctx: Context,
        title: str | None = None,
    ) -> str:
        """Save a local file (PDF, CSV, TXT, MD) to the user's personal
        reading list.

        Use when the user wants to add a file from their computer to their
        knowledge base. The path must be on the machine running QwenPaw.

        The assistant will extract the content, generate a summary, and
        add it to the knowledge base. This may take 30-60 seconds.

        Parameters:
        - path: Absolute file path on the QwenPaw machine
        - title: Optional custom title (auto-detected if omitted)
        """
        session_id = sessions.get(_mcp_session_id(ctx))
        await ctx.info(f"Ingesting file: {path}")
        return await ingest_file(path, title, client_config, session_id)

    ingest_file_tool.__name__ = "ingest_file"

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @mcp.tool()
    async def reset_session_tool(ctx: Context) -> str:
        """Reset the conversation with the Copilot Digest assistant.

        Use when the user says "start over", "forget previous conversation",
        or "reset". This clears conversation memory only — it does NOT
        delete any saved articles or knowledge base content.
        """
        new_session = sessions.reset(_mcp_session_id(ctx))
        logger.info("Reset MCP session -> QwenPaw session %s", new_session)
        return "Session reset. The assistant will start fresh."

    reset_session_tool.__name__ = "reset_session"

    return mcp
