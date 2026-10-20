# -*- coding: utf-8 -*-
"""Verify factory-selected services agree on scope across a PG restart."""

from types import SimpleNamespace

import pytest

from qwenpaw.app.chats.models import ChatSpec
from qwenpaw.app.crons.models import CronJobSpec
from qwenpaw.app.workspace.service_factories import (
    create_chat_service,
    create_job_repository,
    create_session_service,
)
from qwenpaw.config.config import Config
from qwenpaw.runtime._state_utils import StateProxy


async def test_core_workspace_services_reopen_same_pg_scope(
    db,
    tmp_path,
    monkeypatch,
):
    if db.config.backend != "postgresql":
        pytest.skip("PG service wiring; SQLite defaults are tested separately")
    monkeypatch.setattr(
        "qwenpaw.config.utils.load_config",
        lambda: Config(storage=db.config),
    )
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    monkeypatch.setattr(
        "qwenpaw.browser.runtime.links.link_for",
        lambda _: None,
    )
    ws = SimpleNamespace(agent_id="agent", workspace_dir=tmp_path)
    proxy = StateProxy()
    proxy.data = {"turn": "persisted"}
    spec = ChatSpec(session_id="chat", user_id="user", name="Saved chat")
    job = CronJobSpec.model_validate(
        {
            "id": "job",
            "name": "Saved reminder",
            "enabled": False,
            "task_type": "text",
            "text": "hello",
            "schedule": {"type": "cron", "cron": "0 0 * * *"},
            "dispatch": {
                "channel": "console",
                "target": {"user_id": "user", "session_id": "chat"},
            },
        },
    )
    for first_start in (True, False):
        ws.session = await create_session_service(ws, None, lambda _: None)
        try:
            managers = []
            await create_chat_service(ws, None, managers.append)
            chats = managers[0]
            jobs = create_job_repository(ws)
            if first_start:
                await ws.session.save_session_state(
                    "chat",
                    user_id="user",
                    channel="console",
                    agent=proxy,
                )
                await chats.create_chat(spec)
                await jobs.upsert_job(job)
            else:
                restored = await ws.session.get_session_state_dict(
                    "chat",
                    user_id="user",
                    channel="console",
                )
                assert restored == {"agent": proxy.data}
                assert (await chats.get_chat(spec.id)).name == "Saved chat"
                assert await jobs.get_job("job") == job
        finally:
            await ws.session.close()
    assert not (tmp_path / "sessions").exists()
    assert not (tmp_path / "chats.json").exists()
    assert not (tmp_path / "jobs.json").exists()


async def test_session_factory_reads_configuration_off_event_loop(
    tmp_path,
    monkeypatch,
):
    import threading

    loop_thread = threading.get_ident()

    def load():
        assert threading.get_ident() != loop_thread
        return Config()

    monkeypatch.setattr("qwenpaw.config.utils.load_config", load)
    ws = SimpleNamespace(agent_id="agent", workspace_dir=tmp_path)
    session = await create_session_service(ws, None, lambda _: None)
    assert session is not None
