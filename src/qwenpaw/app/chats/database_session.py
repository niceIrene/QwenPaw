# -*- coding: utf-8 -*-
"""Async session API backed by the selected SQL storage runtime."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from collections.abc import Sequence

from ...exceptions import AgentStateError, ConfigurationException
from ...storage.config import StorageConfig
from ...storage.errors import StorageError
from ...storage.factory import create_storage
from ...storage.models import StorageScope
from ...storage.repositories.sessions import SessionKey, SessionRepository


class DatabaseSession:
    """Own a workspace connection pool and preserve per-request revisions."""

    def __init__(
        self,
        config: StorageConfig,
        root: Path,
        scope: StorageScope,
    ) -> None:
        self.config = config.model_copy(deep=True)
        self.root = root
        self.scope = scope
        self._context = None
        self.runtime = None
        self.repository: SessionRepository | None = None
        self._revisions: ContextVar[dict[SessionKey, int]] = ContextVar(
            "session_revisions",
            default={},
        )

    async def start(self) -> None:
        """Validate initialized storage before accepting requests."""
        if self._context is not None:
            raise StorageError("Session storage is already open")
        context = create_storage(self.config, self.root)
        runtime = await context.__aenter__()
        try:
            repository = await runtime.sessions(self.scope)
        except BaseException:
            await context.__aexit__(None, None, None)
            raise
        self._context = context
        self.runtime = runtime
        self.repository = repository

    async def close(self) -> None:
        """Retire handles and release the pool, including failed startup."""
        context, self._context = self._context, None
        self.repository = None
        self.runtime = None
        if context is not None:
            await context.__aexit__(None, None, None)

    def _repository(self) -> SessionRepository:
        if self.repository is None:
            raise StorageError("Session storage is not open")
        return self.repository

    def _remember_revision(self, key: SessionKey, revision: int) -> None:
        # Contexts are inherited by child tasks (including shielded saves).
        # Keep other requests' read versions; never mutate shared dictionaries.
        self._revisions.set({**self._revisions.get(), key: revision})

    async def get_session_state_dict(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        allow_not_exist: bool = True,
    ) -> dict:
        """Read state and remember exactly the revision seen by this task."""
        key = SessionKey(session_id, user_id, channel)
        snapshot = await self._repository().load(key)
        self._remember_revision(key, snapshot.revision if snapshot else 0)
        if snapshot is not None:
            return snapshot.state
        if not allow_not_exist:
            raise AgentStateError(
                session_id=session_id,
                message="Session does not exist",
            )
        return {}

    async def load_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        allow_not_exist: bool = True,
        **state_modules_mapping,
    ) -> None:
        """Restore state modules using the same API as local sessions."""
        states = await self.get_session_state_dict(
            session_id,
            user_id,
            channel,
            allow_not_exist,
        )
        for name, module in state_modules_mapping.items():
            if name in states:
                module.load_state_dict(states[name])

    async def save_session_state(
        self,
        session_id: str,
        user_id: str = "",
        channel: str = "",
        **state_modules_mapping,
    ) -> None:
        """Replace the version read by this task; blind saves only create."""
        key = SessionKey(session_id, user_id, channel)
        state = {
            name: module.state_dict()
            for name, module in state_modules_mapping.items()
        }
        revision = await self._repository().save(
            key,
            state,
            revision=self._revisions.get().get(key, 0),
        )
        self._remember_revision(key, revision)

    async def update_session_state(
        self,
        session_id: str,
        key: str | Sequence[str],
        value,
        user_id: str = "",
        channel: str = "",
        create_if_not_exist: bool = True,
    ) -> None:
        """Update a nested value without overwriting a concurrent writer."""
        path = key.split(".") if isinstance(key, str) else list(key)
        if not path or any(not part for part in path):
            raise ConfigurationException(
                config_key="session.key",
                message="key path is empty",
            )
        state = await self.get_session_state_dict(
            session_id,
            user_id,
            channel,
            create_if_not_exist,
        )
        current = state
        for part in path[:-1]:
            if not isinstance(current.get(part), dict):
                current[part] = {}
            current = current[part]
        current[path[-1]] = value
        session_key = SessionKey(session_id, user_id, channel)
        revision = await self._repository().save(
            session_key,
            state,
            revision=self._revisions.get()[session_key],
        )
        self._remember_revision(session_key, revision)
