# -*- coding: utf-8 -*-
"""Session snapshots sharing the history transaction and identity boundary."""

from dataclasses import dataclass
from collections.abc import Sequence

from ...agents.context.types import LogEntry
from ..contracts.history import HistoryStore
from .records import encode


@dataclass(frozen=True)
class SessionKey:
    """Preserve channel and user identity without filename sanitization."""

    session_id: str
    user_id: str = ""
    channel: str = ""

    def encode(self) -> str:
        """Use an unambiguous key within the workspace/agent history store."""
        if not self.session_id:
            raise ValueError("Session ID must not be empty")
        return encode([self.channel, self.user_id, self.session_id])


@dataclass(frozen=True)
class SessionSnapshot:
    """Keep the revision with the state that was actually read."""

    state: dict
    revision: int


class SessionRepository:
    """Store session state without allowing blind concurrent replacement."""

    def __init__(self, history: HistoryStore) -> None:
        self.history = history

    async def load(self, key: SessionKey) -> SessionSnapshot | None:
        """Return a detached snapshot, or None when the session is absent."""
        result = await self.history.load_checkpoint(key.encode())
        if result is None:
            return None
        state, revision = result
        return SessionSnapshot(state, revision)

    async def save(
        self,
        key: SessionKey,
        state: dict,
        *,
        revision: int,
        entries: Sequence[tuple[LogEntry, str | None]] = (),
    ) -> int:
        """Commit pending history with the checkpoint, or roll back both."""
        return await self.history.save_checkpoint(
            key.encode(),
            state,
            revision=revision,
            entries=entries,
        )
