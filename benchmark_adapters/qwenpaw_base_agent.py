# -*- coding: utf-8 -*-
"""Shared Harbor installed-agent foundation for QwenPaw benchmarks."""

from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from harbor.agents.installed.base import (  # pylint: disable=no-name-in-module
    BaseInstalledAgent,
)
from harbor.environments.base import (  # pylint: disable=no-name-in-module
    BaseEnvironment,
)

if TYPE_CHECKING:
    from harbor.models.trajectories.trajectory import Trajectory


class QwenPawBaseHarborAgent(BaseInstalledAgent):
    """Install and initialize an isolated QwenPaw workspace for Harbor.

    Benchmark subclasses remain responsible for implementing ``name()`` and
    ``run()`` as well as any benchmark-specific result or trajectory mapping.
    """

    QWENPAW_WORKING_DIR: ClassVar[str] = "/tmp/qwenpaw-harbor"
    QWENPAW_INSTALL_TIMEOUT_SEC: ClassVar[int] = 1800
    QWENPAW_INIT_TIMEOUT_SEC: ClassVar[int] = 600

    def get_version_command(self) -> str | None:
        return (
            "python -c 'from importlib.metadata import version; "
            'print(version("qwenpaw"))\''
        )

    def require_model_name(self) -> str:
        """Return Harbor's provider/model selection or fail clearly."""

        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Harbor --model must use provider/model format")
        provider, model = self.model_name.split("/", 1)
        if not provider or not model:
            raise ValueError("Harbor --model must use provider/model format")
        return self.model_name

    def qwenpaw_env(
        self,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the per-command environment for the isolated workspace."""

        result = {"QWENPAW_WORKING_DIR": self.QWENPAW_WORKING_DIR}
        if extra:
            result.update(extra)
        return result

    def resolve_wheel(self) -> Path:
        """Resolve and validate the host wheel selected for this trial."""

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
        return wheel

    async def install(self, environment: BaseEnvironment) -> None:
        """Copy the selected wheel into Harbor and initialize QwenPaw."""

        wheel = self.resolve_wheel()
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Preserve distribution/version/python/ABI/platform tags: pip validates
        # the filename before inspecting the archive contents.
        uploaded_name = wheel.name
        shutil.copy2(wheel, self.logs_dir / uploaded_name)
        await self.exec_as_agent(
            environment,
            command=(
                "python -m pip install --no-cache-dir "
                f"{shlex.quote('/logs/agent/' + uploaded_name)}"
            ),
            timeout_sec=self.QWENPAW_INSTALL_TIMEOUT_SEC,
        )

        working_dir = shlex.quote(self.QWENPAW_WORKING_DIR)
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {working_dir} && "
                f"touch {working_dir}/.telemetry_collected && "
                "qwenpaw init --defaults --accept-security"
            ),
            env=self.qwenpaw_env(),
            timeout_sec=self.QWENPAW_INIT_TIMEOUT_SEC,
        )

    def load_log_json(self, relative_path: str | Path) -> dict[str, Any]:
        """Read a JSON object from this trial's downloaded agent logs."""

        path = self.logs_dir / relative_path
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object in {path}")
        return data

    def write_atif_trajectory(
        self,
        trajectory_data: Mapping[str, Any],
    ) -> "Trajectory":
        """Validate and write Harbor's canonical ``agent/trajectory.json``."""

        trajectory_class = import_module(
            "harbor.models.trajectories.trajectory",
        ).Trajectory
        trajectory = trajectory_class.model_validate(dict(trajectory_data))
        path = self.logs_dir / "trajectory.json"
        path.write_text(
            json.dumps(
                trajectory.to_json_dict(),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return trajectory
