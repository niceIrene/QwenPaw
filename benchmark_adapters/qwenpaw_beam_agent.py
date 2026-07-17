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
from typing import Any

from harbor.environments.base import (  # pylint: disable=no-name-in-module
    BaseEnvironment,
)
from harbor.models.agent.context import (  # pylint: disable=no-name-in-module
    AgentContext,
)

from benchmark_adapters.qwenpaw_base_agent import QwenPawBaseHarborAgent
from benchmark_adapters.qwenpaw_beam_trajectory import (
    build_beam_trajectory,
    load_ordered_traces,
)


class QwenPawBeamAgent(QwenPawBaseHarborAgent):
    """Run QwenPaw's purpose-built BEAM long-memory evaluation loop."""

    SUPPORTS_ATIF = True
    QWENPAW_WORKING_DIR = "/tmp/qwenpaw-beam"

    @staticmethod
    def name() -> str:
        return "qwenpaw-beam"

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # The task's public questions are the actual prompts. Harbor's context
        # is populated from metrics after execution, not during this method.
        del instruction, context
        model_name = self.require_model_name()

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
                f"--model {shlex.quote(model_name)}",
                '--conversation-id "${BEAM_CONVERSATION_ID:-1}"',
                f"--recall-tool {shlex.quote(recall_tool)}",
                "--replace-history",
                "2>&1 | tee /logs/agent/qwenpaw-beam.txt",
            ],
        )
        await self.exec_as_agent(
            environment,
            command=command,
            env=self.qwenpaw_env(),
            timeout_sec=14400,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        metrics_path = self.logs_dir / "metrics.json"
        if not metrics_path.is_file():
            return
        try:
            metrics: dict[str, Any] = self.load_log_json("metrics.json")
            totals = metrics.get("totals") or {}
            context.n_input_tokens = int(totals.get("input_tokens") or 0)
            context.n_output_tokens = int(totals.get("output_tokens") or 0)
        except (OSError, ValueError, TypeError):
            self.logger.exception("Failed to parse QwenPaw BEAM metrics")
            return

        try:
            traces = load_ordered_traces(self.logs_dir, metrics)
            session_id = str(
                self.session_id
                or f"beam-{metrics.get('conversation_id') or 'conversation'}",
            )
            trajectory_data = build_beam_trajectory(
                traces,
                session_id=session_id,
                agent_version=self.version() or "unknown",
                model_name=self.model_name,
                totals=totals,
                conversation_id=str(metrics.get("conversation_id") or ""),
            )
            trajectory = self.write_atif_trajectory(trajectory_data)
            self.logger.info(
                "Wrote Harbor ATIF trajectory with %s steps",
                len(trajectory.steps),
            )
        except (
            ImportError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            self.logger.exception("Failed to build QwenPaw BEAM trajectory")
