# -*- coding: utf-8 -*-
# pylint: disable=unused-import,protected-access,too-many-statements
"""A stopped legacy import survives turns, restart, and paired recovery."""

import asyncio
import json
import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from agentscope.message import Msg, TextBlock

from qwenpaw.agents.context.scroll.manager import ScrollContextManager
from qwenpaw.app.chats.database_session import DatabaseSession
from qwenpaw.hooks.session.session_hook import SessionSaveHook
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.storage.backup import backup_storage, restore_storage
from qwenpaw.storage.backends.sqlite.recall_snapshot import (
    refresh_history_snapshot,
)
from qwenpaw.storage.migration.workspace import import_workspace
from qwenpaw.storage.migration.cron import import_cron
from qwenpaw.storage.migration.usage import import_usage
from qwenpaw.storage.models import StorageScope
from qwenpaw.storage.repositories.sessions import SessionKey
from tests.unit.storage.test_workspace_import import make_legacy_workspace
from tests.unit.storage.test_storage_business_backup import (
    make_target,
    all_rows,
)
from tests.unit.storage.test_cron_usage_import import (
    write_cron_source,
    write_usage_source,
)


async def test_offline_import_turns_restart_and_paired_restore(
    db,
    target,
    legacy_workspace,
    tmp_path,
):
    manifest, _ = legacy_workspace
    (manifest.parent / "session.json").write_text(
        json.dumps(
            {
                "agent": {
                    "scroll": {
                        "persisted_ids": ["legacy"],
                        "seq_by_id": {"legacy": [1, 1]},
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    report = []
    await import_workspace(
        db,
        manifest,
        tenant_id="tenant",
        epoch=1,
        reference_report=report,
    )
    assert not any(row["requires_review"] for row in report)
    await import_cron(
        db,
        write_cron_source(tmp_path),
        tenant_id="tenant",
        epoch=1,
    )
    usage, _ = write_usage_source(tmp_path)
    await import_usage(db, usage, tenant_id="tenant", epoch=1)
    scope = StorageScope("tenant", "agent", "agent")
    key = SessionKey("s", "u", "c").encode()
    session = DatabaseSession(db.config, db.root, scope)
    await session.start()
    manager = ScrollContextManager(
        history=session.repository.history,
        session_id=key,
        agent_id="agent",
    )
    try:
        state = await session.get_session_state_dict("s", "u", "c")
        manager.load_state(state["agent"]["scroll"])
        agent = SimpleNamespace(state=SimpleNamespace(context=[]))

        async def prepare():
            await manager.flush(agent)
            return {"scroll": manager.to_dict()}

        agent.prepare_session_state = prepare
        ctx = SimpleNamespace(
            agent=agent,
            workspace=SimpleNamespace(session=session),
            request=SimpleNamespace(
                request_context={},
                user_id="u",
                channel="c",
            ),
            session_id="s",
            extras={},
            mode_state={},
            error=None,
        )
        for text, cancelled in (
            ("normal turn", False),
            ("cancelled turn", True),
        ):
            agent.state.context.append(
                Msg(
                    name="assistant",
                    role="assistant",
                    content=[TextBlock(text=text)],
                ),
            )
            if cancelled:
                ctx.error = asyncio.CancelledError()
                await Runtime(
                    workspace=ctx.workspace,
                    app_services=None,
                )._try_save_on_cancel(ctx)
            else:
                await SessionSaveHook().run(ctx)
        expected = await session.get_session_state_dict("s", "u", "c")
    finally:
        await manager.aclose()
        await session.close()
    # Pair assets and SQL while all runtime owners are closed.
    files = tmp_path / "files"
    files.mkdir()
    (files / "attachment.txt").write_text("paired asset", encoding="utf-8")
    shutil.copytree(files, tmp_path / "files-backup")
    archive = tmp_path / "storage.jsonl"
    await backup_storage(db, archive)
    await restore_storage(target, archive, epoch=1)
    assert await all_rows(target) == await all_rows(db)
    shutil.rmtree(files)
    shutil.copytree(tmp_path / "files-backup", files)
    assert (files / "attachment.txt").read_text(
        encoding="utf-8",
    ) == "paired asset"
    for database in (db, target):
        reopened = DatabaseSession(database.config, database.root, scope)
        await reopened.start()
        try:
            assert (
                await reopened.get_session_state_dict("s", "u", "c")
                == expected
            )
            snapshot = tmp_path / "recall.db"
            await refresh_history_snapshot(
                reopened.repository.history,
                snapshot,
            )
            with closing(sqlite3.connect(snapshot)) as view:
                rows = view.execute(
                    "SELECT content FROM conversation_history ORDER BY seq",
                ).fetchall()
                text = str(rows)
                assert all(
                    value in text
                    for value in ("original", "normal turn", "cancelled turn")
                )
        finally:
            await reopened.close()
