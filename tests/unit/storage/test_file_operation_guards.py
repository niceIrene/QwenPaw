# -*- coding: utf-8 -*-
"""File-only operations must not silently omit PostgreSQL state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import agents, fork, workspace
from qwenpaw.app.chats.database_session import DatabaseSession
from qwenpaw.checkpoints.models import CheckpointError
from qwenpaw.checkpoints.runtime import CheckpointRuntime
from qwenpaw.config.config import Config
from qwenpaw.storage.models import StorageScope


@pytest.mark.parametrize("operation", ["download", "upload"])
async def test_pg_workspace_archive_rejected_before_file_access(
    operation,
    monkeypatch,
):
    cfg = Config(storage={"backend": "postgresql", "deployment_id": "test"})
    monkeypatch.setattr(workspace, "load_config", lambda: cfg)
    resolve = AsyncMock()
    monkeypatch.setattr(workspace, "get_agent_for_request", resolve)
    upload = AsyncMock()
    with pytest.raises(HTTPException) as caught:
        if operation == "download":
            await workspace.download_workspace(None)
        else:
            await workspace.upload_workspace(None, upload)
    assert caught.value.status_code == 409
    assert "Back up the database" in caught.value.detail
    resolve.assert_not_awaited()
    upload.read.assert_not_awaited()


async def test_pg_fork_rejected_before_reading_legacy_session(monkeypatch):
    cfg = Config(storage={"backend": "postgresql", "deployment_id": "test"})
    monkeypatch.setattr("qwenpaw.config.utils.load_config", lambda: cfg)
    monkeypatch.setattr(fork, "_enforce_localhost", lambda _: None)
    source = Mock()
    monkeypatch.setattr(fork, "_get_project_dir", source)
    with pytest.raises(HTTPException) as caught:
        await fork.fork_agent(SimpleNamespace(), None)
    assert caught.value.status_code == 409
    assert "Forking PostgreSQL sessions" in caught.value.detail
    source.assert_not_called()


async def test_pg_copy_jobs_rejected_before_creating_workspace(monkeypatch):
    cfg = Config(storage={"backend": "postgresql", "deployment_id": "test"})
    monkeypatch.setattr(agents, "load_config", lambda: cfg)
    source = Mock()
    monkeypatch.setattr(agents, "load_agent_config", source)
    with pytest.raises(HTTPException) as caught:
        await agents.copy_agent(
            "source",
            agents.CopyAgentRequest(copy_jobs=True),
        )
    assert caught.value.status_code == 409
    assert "Disable copy_jobs" in caught.value.detail
    source.assert_not_called()


async def test_pg_shadow_checkpoints_do_not_create_file_snapshots(tmp_path):
    cfg = Config(storage={"backend": "postgresql", "deployment_id": "test"})
    session = DatabaseSession(
        cfg.storage,
        tmp_path,
        StorageScope("test", "a", "a"),
    )
    ws = SimpleNamespace(session=session, workspace_dir=tmp_path)
    runtime = CheckpointRuntime()
    with pytest.raises(CheckpointError, match="PostgreSQL"):
        await runtime.get_for_workspace_async(ws)
    with pytest.raises(CheckpointError, match="PostgreSQL"):
        runtime.get_for_workspace(ws)
    await runtime.schedule_auto_snapshot(
        ws,
        session_id="s",
        user_id="u",
        channel="console",
    )
    assert (
        await runtime.delete_session_checkpoints(ws, [("s", "u", "console")])
        == ()
    )
    assert not list(tmp_path.iterdir())
