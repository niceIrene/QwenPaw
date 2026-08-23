# -*- coding: utf-8 -*-
"""Snapshot persistence format choice and round-trips (roadmap §2.7)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from qwenpaw.repl.persistence import (
    choose_format,
    latest_snapshot_dir,
    restore_namespace,
    snapshot_namespace,
    snapshot_root,
)


class TestChooseFormat:
    def test_json_for_plain_data(self) -> None:
        assert choose_format({"a": [1, 2]}) == "json"
        assert choose_format("text") == "json"
        assert choose_format(3.5) == "json"

    def test_pickle_for_non_json(self) -> None:
        assert choose_format(object()) == "pickle"
        assert choose_format({1, 2, 3}) == "pickle"

    def test_numpy_prefers_npy(self) -> None:
        pytest.importorskip("numpy")
        import numpy

        assert choose_format(numpy.arange(3)) == "npy"

    def test_pandas_prefers_parquet_else_csv(self) -> None:
        pandas = pytest.importorskip("pandas")
        frame = pandas.DataFrame({"a": [1, 2]})
        expected = "parquet" if importlib.util.find_spec("pyarrow") else "csv"
        assert choose_format(frame) == expected


class TestNamespaceSnapshots:
    def _namespace(self) -> dict:
        return {
            "_paw_internal_names": ("paw", "_paw_internal_names"),
            "paw": object(),
            "__builtins__": {},
            "alpha": [1, 2],
            "beta": "text",
        }

    def test_snapshot_restore_round_trip(self, tmp_path: Path) -> None:
        destination = snapshot_root(tmp_path, "tag") / "latest"
        saved = snapshot_namespace(self._namespace(), destination)
        assert sorted(saved) == ["alpha", "beta"]

        target: dict = {}
        restored = restore_namespace(target, destination)
        assert sorted(restored) == ["alpha", "beta"]
        assert target["alpha"] == [1, 2]
        assert target["beta"] == "text"
        assert "paw" not in target

    def test_snapshot_drops_stale_files(self, tmp_path: Path) -> None:
        destination = snapshot_root(tmp_path, "tag") / "latest"
        namespace = self._namespace()
        snapshot_namespace(namespace, destination)
        del namespace["beta"]
        snapshot_namespace(namespace, destination)

        target: dict = {}
        restored = restore_namespace(target, destination)
        assert restored == ["alpha"]

    def test_latest_snapshot_dir(self, tmp_path: Path) -> None:
        assert latest_snapshot_dir(tmp_path, "tag") is None
        destination = snapshot_root(tmp_path, "tag") / "latest"
        snapshot_namespace(self._namespace(), destination)
        assert latest_snapshot_dir(tmp_path, "tag") == destination

    def test_restore_missing_manifest_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            restore_namespace({}, tmp_path / "nowhere")
