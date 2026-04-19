# -*- coding: utf-8 -*-
# pylint: disable=unused-argument
"""FastMCP server exposing Copilot Digest over streamable-HTTP.

Tools are registered in two categories:

**Direct workspace tools** (fast, no assistant round-trip):
* ``list_reading_list(status?, timeframe?, topic?, limit?)``
* ``get_article(article_id)``
* ``get_briefing(timeframe?, group_by?, filter_topic?, ...)``
* ``mark_read(article_id)``
* ``mark_unread(article_id)``
* ``mark_discussed(article_id)``
* ``get_stats()``
* ``get_config()``
* ``update_config(add_topics?, remove_topics?, ...)``
* ``save_work_output(output_type, content, article_id?)``
* ``export_briefing(item_ids?, include_all_work?, title?)``

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
    export_briefing as ws_export_briefing,
    get_article as ws_get_article,
    get_briefing as ws_get_briefing,
    get_config as ws_get_config,
    get_stats as ws_get_stats,
    list_items as ws_list_items,
    mark_discussed as ws_mark_discussed,
    mark_read as ws_mark_read,
    mark_unread as ws_mark_unread,
    save_work_output as ws_save_work_output,
    update_config as ws_update_config,
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
            session = (
                self._fallback if key == "__fallback__" else uuid.uuid4().hex
            )
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
                ws_path,
                status=status,
                timeframe=timeframe,
                topic=topic,
                limit=limit,
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

            This tool automatically returns the curated script/summary
            version of the article if one exists (the ``_script.md``
            file), otherwise the raw article text, otherwise the
            inline summary from the index.

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

        @mcp.tool()
        async def mark_discussed_tool(article_id: str, ctx: Context) -> str:
            """Mark an article as discussed in the user's knowledge base.

            Call this after the user has had a discussion about an article.
            This sets both discussed=true and read=true on the item.

            Use when the user says "we discussed this", or after a
            substantive conversation about an article's content.

            Parameters:
            - article_id: The item ID to mark as discussed
            """
            return ws_mark_discussed(ws_path, article_id)

        mark_discussed_tool.__name__ = "mark_discussed"

        @mcp.tool()
        async def get_briefing_tool(
            ctx: Context,
            timeframe: str = "today",
            group_by: str = "topic",
            filter_topic: str | None = None,
            filter_status: str = "all",
            top_n: int = 0,
        ) -> str:
            """Get a ranked, scored briefing of the user's reading list.

            YOU MUST call this tool when the user asks for a briefing,
            digest, or wants to know what's new. Examples:
            - "What's new today?"
            - "This week's briefing"
            - "Catch me up"
            - "Unread items on fintech"
            - "What should I read first?"

            Unlike list_reading_list, this tool RANKS items by relevance,
            recency, and source authority, and groups them by topic or
            date. Use this for briefings; use list_reading_list for
            simple filtered listings.

            Parameters:
            - timeframe: "today" (default), "yesterday", "week", "month", "all"
            - group_by: "topic" (default), "date", or "none"
            - filter_topic: filter to items matching this topic (optional)
            - filter_status: "all" (default), "unread", "read", "discussed"
            - top_n: limit to top N items, 0 for all (default 0)
            """
            return ws_get_briefing(
                ws_path,
                timeframe=timeframe,
                group_by=group_by,
                filter_topic=filter_topic,
                filter_status=filter_status,
                top_n=top_n,
            )

        get_briefing_tool.__name__ = "get_briefing"

        @mcp.tool()
        async def get_config_tool(ctx: Context) -> str:
            """Show the user's Copilot Digest configuration.

            Use when the user asks about their settings, tracked sources,
            topics of interest, or preferences. Examples:
            - "What sources am I tracking?"
            - "What are my topics?"
            - "Show my config"
            - "What's my fetch schedule?"
            """
            return ws_get_config(ws_path)

        get_config_tool.__name__ = "get_config"

        @mcp.tool()
        async def update_config_tool(
            ctx: Context,
            add_topics: str | None = None,
            remove_topics: str | None = None,
            add_source_name: str | None = None,
            add_source_url: str | None = None,
            remove_sources: str | None = None,
            set_summary_length: str | None = None,
            set_fetch_cron: str | None = None,
        ) -> str:
            """Update the user's Copilot Digest configuration.

            Use when the user wants to change their interests, sources,
            or preferences. Examples:
            - "Add AI policy to my topics"
            - "Remove crypto from my topics"
            - "Add TechCrunch as a source"
            - "Set briefing detail to detailed"

            Parameters:
            - add_topics: comma-separated topics to add
              (e.g. "AI policy, quantum computing")
            - remove_topics: comma-separated topics to remove
            - add_source_name: name of a new source to add
            - add_source_url: URL of the new source (use with add_source_name)
            - remove_sources: comma-separated source names to remove
            - set_summary_length: "brief", "standard", or "detailed"
            - set_fetch_cron: cron expression for auto-fetch schedule
            """
            add_t = (
                [t.strip() for t in add_topics.split(",") if t.strip()]
                if add_topics
                else None
            )
            rem_t = (
                [t.strip() for t in remove_topics.split(",") if t.strip()]
                if remove_topics
                else None
            )
            add_s = (
                [{"name": add_source_name, "url": add_source_url or ""}]
                if add_source_name
                else None
            )
            rem_s = (
                [s.strip() for s in remove_sources.split(",") if s.strip()]
                if remove_sources
                else None
            )

            return ws_update_config(
                ws_path,
                add_topics=add_t,
                remove_topics=rem_t,
                add_sources=add_s,
                remove_sources=rem_s,
                set_summary_length=set_summary_length,
                set_fetch_cron=set_fetch_cron,
            )

        update_config_tool.__name__ = "update_config"

        @mcp.tool()
        async def save_work_output_tool(
            ctx: Context,
            output_type: str,
            content: str,
            article_id: str | None = None,
        ) -> str:
            """Save discussion notes, takeaways, or action items to disk.

            YOU MUST call this tool whenever the user asks to save,
            record, persist, or write down notes, takeaways, action
            items, or discussion output. Trigger phrases include:
            - "save my notes"
            - "save my discussion notes"
            - "help me save what we discussed"
            - "record the takeaways"
            - "write down the action items"
            - "capture this discussion"
            - "persist these notes"

            Do NOT simply print the notes in chat — the user wants
            them saved to a file on disk so they can reference them
            later. Always call this tool to write the file.

            You must compose the ``content`` parameter yourself as
            well-formatted markdown that captures the key points from
            the conversation so far.

            Parameters:
            - output_type: "notes", "takeaways", or "action_items"
            - content: Well-formatted markdown you compose from the
              conversation
            - article_id: Optional article ID this relates to
            """
            return ws_save_work_output(
                ws_path,
                output_type=output_type,
                content=content,
                article_id=article_id,
            )

        save_work_output_tool.__name__ = "save_work_output"

        @mcp.tool()
        async def export_briefing_tool(
            ctx: Context,
            item_ids: str | None = None,
            include_all_work: bool = False,
            title: str | None = None,
        ) -> str:
            """Export a compiled briefing document with articles and work
            outputs.

            Use when the user asks to export or compile their briefing.
            Examples:
            - "Export today's briefing"
            - "Compile my notes and articles"
            - "Generate an export"

            By default, includes all read/discussed articles from today
            and today's work files. The export is saved to the exports/
            directory and returned as text.

            Parameters:
            - item_ids: comma-separated article IDs to include (optional,
              defaults to today's read/discussed items)
            - include_all_work: include ALL work files, not just today's
            - title: custom document title
            """
            ids = (
                [i.strip() for i in item_ids.split(",") if i.strip()]
                if item_ids
                else None
            )

            return ws_export_briefing(
                ws_path,
                item_ids=ids,
                include_all_work=include_all_work,
                title=title,
            )

        export_briefing_tool.__name__ = "export_briefing"

        logger.info(
            "Workspace tools enabled (workspace=%s): "
            "list_reading_list, get_article, mark_read, mark_unread, "
            "mark_discussed, get_briefing, get_config, update_config, "
            "get_stats, save_work_output, export_briefing",
            ws_path,
        )
    else:
        logger.warning(
            "No --workspace provided. Direct workspace tools "
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
            text,
            client_config,
            session_id,
            progress_cb=_progress,
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
            "\n".join(prompt_lines),
            client_config,
            session_id,
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
