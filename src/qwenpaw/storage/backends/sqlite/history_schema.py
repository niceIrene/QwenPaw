# -*- coding: utf-8 -*-
"""SQLite history tables and derived search index shared by local writers."""

import sqlite3

RECALL_TOOL_NAMES = ("recall_history_python", "recall_history")


def create_history_tables(connection: sqlite3.Connection) -> None:
    """Create portable history tables and lookup indexes."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_history (
            seq          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            agent_id     TEXT,
            kind         TEXT NOT NULL,
            role         TEXT,
            name         TEXT,
            content      TEXT,
            tool_call_id TEXT,
            tool_input   TEXT,
            tool_state   TEXT,
            headline     TEXT,
            blocks       TEXT,
            metadata     TEXT,
            created_at   TEXT,
            dedup_key    TEXT
        )
        """,
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ch_session "
        "ON conversation_history(session_id)",
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ch_agent "
        "ON conversation_history(agent_id)",
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ch_kind ON conversation_history(kind)",
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ch_created_at "
        "ON conversation_history(created_at)",
    )
    # Idempotency net: a second append of the same logical event, such
    # as a resume re-persisting its restored window, collides here and
    # is dropped by ON CONFLICT rather than duplicating a row. NULL
    # dedup_key never conflicts, so un-keyed rows are simply never
    # deduped.
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_dedup "
        "ON conversation_history(session_id, dedup_key)",
    )


def create_history_fts(connection: sqlite3.Connection) -> None:
    """Create FTS5, raising if this SQLite build lacks the module."""
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_history_fts "
        "USING fts5(content, content='conversation_history', "
        "content_rowid='seq', tokenize='porter unicode61')",
    )


def rebuild_history_fts(connection: sqlite3.Connection) -> None:
    """Index source rows, excluding recall tools from keyword search."""
    connection.execute(
        "INSERT INTO conversation_history_fts"
        "(conversation_history_fts) VALUES('rebuild')",
    )
    # Rebuild includes every source row. Remove the deliberately excluded
    # rows now, while we know they are indexed, in the same transaction.
    for row in connection.execute(
        "SELECT seq, content FROM conversation_history "
        "WHERE name IN (?, ?)",
        RECALL_TOOL_NAMES,
    ).fetchall():
        connection.execute(
            "INSERT INTO conversation_history_fts"
            "(conversation_history_fts, rowid, content) "
            "VALUES('delete', ?, ?)",
            (row["seq"], row["content"] or ""),
        )
