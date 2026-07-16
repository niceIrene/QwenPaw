# -*- coding: utf-8 -*-
"""Harbor installed-agent adapter for QwenPaw's BEAM runner.

Build the current checkout as a wheel and point ``QWENPAW_WHEEL`` at it.  The
adapter copies that wheel through Harbor's agent-log mount, installs it in the
task container, initializes an isolated QwenPaw workspace, and runs all BEAM
probes with one durable imported history.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import (  # pylint: disable=no-name-in-module
    BaseInstalledAgent,
)
from harbor.environments.base import (  # pylint: disable=no-name-in-module
    BaseEnvironment,
)
from harbor.models.agent.context import (  # pylint: disable=no-name-in-module
    AgentContext,
)


class QwenPawBeamAgent(BaseInstalledAgent):
    """Run QwenPaw's purpose-built BEAM long-memory evaluation loop."""

    @staticmethod
    def name() -> str:
        return "qwenpaw-beam"

    def get_version_command(self) -> str | None:
        return (
            "python -c 'from importlib.metadata import version; "
            'print(version("qwenpaw"))\''
        )

    async def install(self, environment: BaseEnvironment) -> None:
        wheel_value = (self._get_env("QWENPAW_WHEEL") or "").strip()
        if not wheel_value:
            raise RuntimeError(
                "QWENPAW_WHEEL is required. Build it with "
                "`uv build --wheel --out-dir dist`, then pass its "
                "absolute path through `--ae QWENPAW_WHEEL=...`.",
            )
        wheel = Path(wheel_value).expanduser().resolve()
        if not wheel.is_file() or wheel.suffix != ".whl":
            raise RuntimeError(f"QWENPAW_WHEEL is not a wheel file: {wheel}")

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        # Keep the distribution/version/python/ABI/platform tags intact.
        # pip validates wheel filenames before reading their contents, so a
        # friendly rename such as ``qwenpaw-benchmark.whl`` is rejected even
        # when the copied archive itself is a valid wheel.
        uploaded_name = wheel.name
        shutil.copy2(wheel, self.logs_dir / uploaded_name)
        await self.exec_as_agent(
            environment,
            command=(
                "python -m pip install --no-cache-dir "
                f"{shlex.quote('/logs/agent/' + uploaded_name)}"
            ),
            timeout_sec=1800,
        )
        await self.exec_as_agent(
            environment,
            command=(
                "mkdir -p /tmp/qwenpaw-beam && "
                "touch /tmp/qwenpaw-beam/.telemetry_collected && "
                "qwenpaw init --defaults --accept-security"
            ),
            env={"QWENPAW_WORKING_DIR": "/tmp/qwenpaw-beam"},
            timeout_sec=600,
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # The task's public questions are the actual prompts. Harbor's context
        # is populated from metrics after execution, not during this method.
        del instruction, context
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Harbor --model must use provider/model format")

        recall_tool = (
            self._get_env("QWENPAW_BEAM_RECALL_TOOL") or "structured"
        ).strip()
        if recall_tool not in {"structured", "python"}:
            raise ValueError(
                "QWENPAW_BEAM_RECALL_TOOL must be structured or python",
            )
        command = " ".join(
            [
                "python -m qwenpaw.evals.beam_runner",
                "--chat /app/chat.json",
                "--questions /app/questions.json",
                "--out /app/answers.json",
                "--metrics-out /logs/agent/metrics.json",
                "--trace-dir /logs/agent/traces",
                "--agent-id default",
                f"--model {shlex.quote(self.model_name)}",
                '--conversation-id "${BEAM_CONVERSATION_ID:-1}"',
                f"--recall-tool {shlex.quote(recall_tool)}",
                "--replace-history",
                "2>&1 | tee /logs/agent/qwenpaw-beam.txt",
            ],
        )
        await self.exec_as_agent(
            environment,
            command=command,
            env={"QWENPAW_WORKING_DIR": "/tmp/qwenpaw-beam"},
            timeout_sec=14400,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        metrics_path = self.logs_dir / "metrics.json"
        if not metrics_path.is_file():
            return
        try:
            metrics: dict[str, Any] = json.loads(
                metrics_path.read_text(encoding="utf-8"),
            )
            totals = metrics.get("totals") or {}
            context.n_input_tokens = int(totals.get("input_tokens") or 0)
            context.n_output_tokens = int(totals.get("output_tokens") or 0)
        except (OSError, ValueError, TypeError):
            self.logger.exception("Failed to parse QwenPaw BEAM metrics")
