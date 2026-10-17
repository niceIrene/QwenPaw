# -*- coding: utf-8 -*-
"""Chat metadata round trips and stale-write protection on both backends."""

from datetime import datetime, timezone

import pytest

from qwenpaw.app.chats.models import ChatGroup, ChatSpec
from qwenpaw.app.chats.repo.database_repo import DatabaseChatRepository
from qwenpaw.storage.errors import StorageConflictError
from qwenpaw.storage.models import StorageScope
from qwenpaw.storage.repositories.records import Records


def repository(db, workspace="workspace"):
    return DatabaseChatRepository(
        Records(db, "tenant", 1),
        StorageScope("tenant", workspace, "agent"),
    )


async def test_registry_roundtrip_and_isolation(db):
    repo = repository(db)
    registry = await repo.load()
    registry.groups.append(ChatGroup(id="custom", name="Projects"))
    registry.chats.append(
        ChatSpec(
            session_id="session",
            user_id="user",
            name="Renamed chat",
            group_id="custom",
            pinned=True,
            archived_at=datetime.now(timezone.utc),
        ),
    )
    await repo.save(registry)
    restored = await repository(db).load()
    assert restored.model_dump() == registry.model_dump()
    assert (await repository(db, "other").load()).chats == []
    payload, _ = await repo.records.get("chats", repo.key)
    assert "_revision" not in payload
    assert "_owner" not in payload


async def test_registry_snapshot_revision_rejects_stale_save(db):
    repo = repository(db)
    first, stale = await repo.load(), await repo.load()
    first.groups.append(ChatGroup(name="Keep me"))
    await repo.save(first)
    with pytest.raises(StorageConflictError):
        await repo.save(stale)
    # Saving the same successfully updated snapshot remains supported.
    await repo.save(first)
    assert (await repo.load()).model_dump() == first.model_dump()


async def test_workspace_rebinds_reused_manager_to_current_storage(
    db,
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from qwenpaw.app.chats.database_session import DatabaseSession
    from qwenpaw.app.chats.manager import ChatManager
    from qwenpaw.app.chats.repo.json_repo import JsonChatRepository
    from qwenpaw.app.workspace.service_factories import create_chat_service
    from qwenpaw.storage.config import StorageConfig
    from qwenpaw.storage.factory import StorageRuntime

    monkeypatch.setattr(
        "qwenpaw.browser.runtime.links.link_for",
        lambda _: None,
    )
    session = DatabaseSession(
        StorageConfig(),
        tmp_path,
        StorageScope("tenant", "workspace", "agent"),
    )
    session.runtime = StorageRuntime(db, 1)
    ws = SimpleNamespace(
        session=session,
        workspace_dir=tmp_path,
        agent_id="agent",
    )
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    await create_chat_service(ws, manager, lambda _: None)
    await manager.create_chat(ChatSpec(session_id="s", user_id="u", name="PG"))
    assert (await repository(db).load()).chats[0].name == "PG"
    assert not (tmp_path / "chats.json").exists()
    ws.session = None
    await create_chat_service(ws, manager, lambda _: None)
    assert await manager.list_chats() == []
