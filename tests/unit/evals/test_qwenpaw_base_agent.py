# -*- coding: utf-8 -*-
"""Contract tests for the shared QwenPaw Harbor adapter foundation."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from benchmark_adapters.qwenpaw_base_agent import QwenPawBaseHarborAgent
    from benchmark_adapters.qwenpaw_beam_agent import QwenPawBeamAgent
except (ImportError, ModuleNotFoundError):
    # The core QwenPaw test venv intentionally does not depend on Harbor.
    # These tests run in Harbor's tool environment and skip in the core venv.
    QwenPawBaseHarborAgent = None  # type: ignore[assignment,misc]
    QwenPawBeamAgent = None  # type: ignore[assignment,misc]


class _ConcreteAgent(QwenPawBaseHarborAgent or object):  # type: ignore[misc]
    @staticmethod
    def name() -> str:
        return "qwenpaw-test"

    async def run(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeEnvironment:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def exec(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(return_code=0, stdout="", stderr="")


@unittest.skipIf(
    QwenPawBaseHarborAgent is None,
    "Harbor is not installed in the core QwenPaw test environment",
)
class QwenPawBaseHarborAgentTest(unittest.TestCase):
    def test_install_copies_wheel_and_initializes_isolated_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "qwenpaw-2.0.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            logs = root / "logs"
            agent = _ConcreteAgent(
                logs_dir=logs,
                model_name="dashscope/qwen3.7-max",
                extra_env={"QWENPAW_WHEEL": str(wheel)},
            )
            environment = _FakeEnvironment()

            asyncio.run(agent.install(environment))

            self.assertEqual((logs / wheel.name).read_bytes(), b"wheel")
            self.assertEqual(len(environment.calls), 2)
            self.assertIn("pip install", environment.calls[0]["command"])
            self.assertIn("qwenpaw init", environment.calls[1]["command"])
            self.assertEqual(
                environment.calls[1]["env"]["QWENPAW_WORKING_DIR"],
                "/tmp/qwenpaw-harbor",
            )

    def test_requires_provider_model_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            valid = _ConcreteAgent(
                logs_dir=Path(temporary),
                model_name="dashscope/qwen3.7-max",
            )
            invalid = _ConcreteAgent(
                logs_dir=Path(temporary),
                model_name="qwen3.7-max",
            )

            self.assertEqual(
                valid.require_model_name(),
                "dashscope/qwen3.7-max",
            )
            with self.assertRaisesRegex(ValueError, "provider/model"):
                invalid.require_model_name()

    def test_validates_and_writes_atif_trajectory(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            agent = _ConcreteAgent(
                logs_dir=logs,
                model_name="dashscope/qwen3.7-max",
            )

            trajectory = agent.write_atif_trajectory(
                {
                    "schema_version": "ATIF-v1.7",
                    "session_id": "test-session",
                    "agent": {
                        "name": "qwenpaw-test",
                        "version": "2.0.0",
                    },
                    "steps": [
                        {
                            "step_id": 1,
                            "source": "user",
                            "message": "Test instruction",
                        },
                    ],
                },
            )

            saved = json.loads((logs / "trajectory.json").read_text())
            self.assertEqual(trajectory.schema_version, "ATIF-v1.7")
            self.assertEqual(saved["steps"][0]["message"], "Test instruction")

    def test_beam_subclass_retains_its_specialized_run_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            agent = QwenPawBeamAgent(
                logs_dir=Path(temporary),
                model_name="dashscope/qwen3.7-max",
                extra_env={"QWENPAW_BEAM_RECALL_TOOL": "python"},
            )
            environment = _FakeEnvironment()

            asyncio.run(
                agent.run(
                    "Harbor instruction is not the BEAM probe source",
                    environment,
                    SimpleNamespace(),
                ),
            )

            self.assertEqual(len(environment.calls), 1)
            command = environment.calls[0]["command"]
            self.assertIn("qwenpaw.evals.beam_runner", command)
            self.assertIn("--chat /app/chat.json", command)
            self.assertIn("--recall-tool python", command)
            self.assertIn("--model dashscope/qwen3.7-max", command)
            self.assertNotIn("Harbor instruction", command)
            self.assertEqual(
                environment.calls[0]["env"]["QWENPAW_WORKING_DIR"],
                "/tmp/qwenpaw-beam",
            )


if __name__ == "__main__":
    unittest.main()
