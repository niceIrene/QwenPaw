# -*- coding: utf-8 -*-
"""File-only operations must not silently omit PostgreSQL state."""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import agents, workspace
from qwenpaw.config.config import Config


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
    assert "storage backup/restore" in caught.value.detail
    resolve.assert_not_awaited()
    upload.read.assert_not_awaited()


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
