# -*- coding: utf-8 -*-
"""Explicit offline workspace import; no filename-based identity guessing."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict, Field

from ...app.chats.models import ChatsFile
from ..contracts.database import Database
from ..errors import StorageIdentityError
from ..repositories.sessions import SessionKey
from .legacy import _read_checkpoint, import_history


class LegacySession(BaseModel):
    """Bind an optional checkpoint and legacy history ID to a runtime key."""

    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1)
    user_id: str
    channel: str
    path: Path | None = None


class WorkspaceImport(BaseModel):
    """Paths are relative to the manifest, identities are explicit."""

    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    history: Path | None = None
    chats: Path
    sessions: list[LegacySession]


async def import_workspace(
    db: Database,
    manifest_path: Path,
    *,
    tenant_id: str,
    epoch: int,
    reference_report: list[dict] | None = None,
) -> str:
    """Import declared files atomically into an unused workspace scope.

    All application workers must be stopped. The source files are never
    changed, and the temporary SQLite snapshot is removed on success or error.
    """
    manifest = WorkspaceImport.model_validate(
        await asyncio.to_thread(_read_checkpoint, manifest_path),
    )
    root = manifest_path.resolve().parent
    chats = ChatsFile.model_validate(
        await asyncio.to_thread(_read_checkpoint, root / manifest.chats),
    )
    checkpoints = {}
    history_sessions = {}
    source_paths = set()
    keys = set()
    for session in manifest.sessions:
        key = SessionKey(
            session.session_id,
            session.user_id,
            session.channel,
        ).encode()
        if key in keys:
            raise StorageIdentityError("Duplicate session key in manifest")
        keys.add(key)
        if (
            manifest.history is not None
            and session.session_id in history_sessions
        ):
            raise StorageIdentityError("Ambiguous legacy history session ID")
        history_sessions[session.session_id] = key
        if session.path is not None:
            path = (root / session.path).resolve()
            if path in source_paths:
                raise StorageIdentityError("Checkpoint mapped more than once")
            source_paths.add(path)
            checkpoints[key] = path
    for chat in chats.chats:
        key = SessionKey(chat.session_id, chat.user_id, chat.channel).encode()
        if key not in keys:
            raise StorageIdentityError("Chat registry has an unmapped session")
    with TemporaryDirectory(prefix="qwenpaw-import-") as directory:
        return await import_history(
            db,
            root / manifest.history if manifest.history is not None else None,
            tenant_id=tenant_id,
            workspace_id=manifest.workspace_id,
            agent_id=manifest.agent_id,
            epoch=epoch,
            backup_directory=Path(directory),
            checkpoints=checkpoints,
            reference_report=reference_report,
            history_sessions=history_sessions,
            chat_registry=chats.model_dump(mode="json"),
        )
