"""Variable persistence format choice and round-trips (roadmap §2.6/§2.7)."""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from qwenpaw.repl.persistence import (
    PICKLE_HEADER,
    choose_format,
    describe_variable,
    latest_snapshot_dir,
    persist_variable,
    restore_namespace,
    restore_variable,
    snapshot_namespace,
    snapshot_root,
    validate_var_name,
    vars_dir,
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
        expected = "parquet"
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            expected = "csv"
        assert choose_format(frame) == expected


class TestPersistRestore:
    def test_json_round_trip(self, tmp_path: Path) -> None:
        meta = persist_variable("payload", {"x": [1, 2]}, tmp_path)
        assert meta["format"] == "json"
        assert meta["name"] == "payload"
        assert meta["type"] == "dict"
        assert meta["size"] > 0
        assert meta["saved_at"]
        assert restore_variable("payload", tmp_path) == {"x": [1, 2]}

    def test_describe_reports_metadata(self, tmp_path: Path) -> None:
        persist_variable("payload", [1, 2, 3], tmp_path)
        meta = describe_variable("payload", tmp_path)
        assert meta["name"] == "payload"
        assert meta["format"] == "json"

    def test_describe_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            describe_variable("ghost", tmp_path)

    def test_pickle_round_trip_with_header(self, tmp_path: Path) -> None:
        value = {1, 2, 3}
        meta = persist_variable("aset", value, tmp_path)
        assert meta["format"] == "pickle"
        raw = (vars_dir(tmp_path) / "aset.pkl").read_bytes()
        assert raw.startswith(PICKLE_HEADER)
        assert restore_variable("aset", tmp_path) == value

    def test_pickle_without_header_is_refused(self, tmp_path: Path) -> None:
        directory = vars_dir(tmp_path)
        (directory / "evil.pkl").write_bytes(pickle.dumps({"a": 1}))
        with pytest.raises(ValueError, match="header"):
            restore_variable("evil", tmp_path)

    def test_re_persist_switches_format_and_removes_stale_file(
        self,
        tmp_path: Path,
    ) -> None:
        persist_variable("thing", {"a": 1}, tmp_path)
        assert (vars_dir(tmp_path) / "thing.json").exists()
        persist_variable("thing", object(), tmp_path)
        assert not (vars_dir(tmp_path) / "thing.json").exists()
        assert (vars_dir(tmp_path) / "thing.pkl").exists()

    def test_invalid_names_rejected(self, tmp_path: Path) -> None:
        for bad in ("../escape", "a b", "", "x" * 200, "a/b"):
            with pytest.raises(ValueError):
                validate_var_name(bad)

    def test_numpy_round_trip(self, tmp_path: Path) -> None:
        numpy = pytest.importorskip("numpy")
        persist_variable("arr", numpy.arange(5), tmp_path)
        restored = restore_variable("arr", tmp_path)
        assert numpy.array_equal(restored, numpy.arange(5))


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
