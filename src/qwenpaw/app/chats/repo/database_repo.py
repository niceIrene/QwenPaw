# -*- coding: utf-8 -*-
"""Workspace chat registry persisted as one revisioned SQL document."""

from pydantic import PrivateAttr

from .base import BaseChatRepository
from ..models import ChatsFile
from ....storage.errors import StorageConflictError
from ....storage.models import StorageScope
from ....storage.repositories.records import Records, encode


class _RegistrySnapshot(ChatsFile):
    """Keep the read revision on the snapshot, outside its JSON payload."""

    _revision: int = PrivateAttr(default=0)
    _owner: object = PrivateAttr(default=None)


class DatabaseChatRepository(BaseChatRepository):
    """Preserve the registry API and reject stale read/modify/write cycles."""

    # The private snapshot type belongs exclusively to this repository.
    # pylint: disable=protected-access

    def __init__(self, records: Records, scope: StorageScope) -> None:
        if records.tenant_id != scope.tenant_id:
            raise ValueError("Chat registry tenant does not match its scope")
        self.records = records
        self.key = encode([scope.workspace_id, scope.agent_id])
        self.path = f"database:chats:{self.key}"

    async def load(self) -> ChatsFile:
        """Load an independent snapshot including its exact revision."""
        record = await self.records.get("chats", self.key)
        snapshot = _RegistrySnapshot.model_validate(
            record[0] if record else {},
        )
        snapshot._revision = record[1] if record else 0
        snapshot._owner = self
        return snapshot

    async def save(self, chats_file: ChatsFile) -> None:
        """Save only a snapshot loaded from this repository."""
        if (
            not isinstance(chats_file, _RegistrySnapshot)
            or chats_file._owner is not self
        ):
            raise StorageConflictError("Load the chat registry before saving")
        chats_file._revision = await self.records.put(
            "chats",
            self.key,
            chats_file.model_dump(mode="json"),
            revision=chats_file._revision,
        )
