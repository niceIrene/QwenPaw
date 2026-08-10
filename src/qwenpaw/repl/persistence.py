# -*- coding: utf-8 -*-
"""Typed variable persistence for the CodeAct REPL (roadmap §2.6, §2.7).

``persist(name)`` / ``restore_var(name)`` / ``describe(name)`` store kernel
variables under ``workspace/.qwenpaw-repl/vars/`` using the safest format
available for each value:

- pure JSON-serializable objects  -> ``.json``
- numpy arrays                    -> ``.npy``  (when numpy is importable)
- pandas DataFrames               -> ``.parquet`` (pyarrow) else ``.csv``
- everything else                 -> ``.pkl`` with an explicit QwenPaw header

Pickle snapshots are only restored inside the strict sandbox, matching the
roadmap's "controlled fallback" rule.  The same format machinery backs
ExecutionBackend snapshots under ``.qwenpaw-repl/snapshots/<id>/``.
"""

from __future__ import annotations

import json
import pickle
import re
import time
from pathlib import Path
from typing import Any

PICKLE_HEADER = b"QWENPAWPKL1\n"

VARS_SUBDIR = Path(".qwenpaw-repl") / "vars"
SNAPSHOTS_SUBDIR = Path(".qwenpaw-repl") / "snapshots"

_FORMAT_EXTENSIONS = {
    "json": ".json",
    "npy": ".npy",
    "parquet": ".parquet",
    "csv": ".csv",
    "pickle": ".pkl",
}
_EXTENSION_FORMATS = {ext: fmt for fmt, ext in _FORMAT_EXTENSIONS.items()}

# Snapshot budget: keep automatic crash-recovery snapshots bounded.
MAX_SNAPSHOT_VARS = 512
MAX_SNAPSHOT_VAR_BYTES = 64 * 1024 * 1024

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$")


def validate_var_name(name: Any) -> str:
    """Reject names that are not safe, identifier-like file stems."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            "variable name must match [A-Za-z_][A-Za-z0-9_.-]{0,127}: "
            f"{name!r}",
        )
    return name


def vars_dir(workspace: Path) -> Path:
    directory = Path(workspace).resolve() / VARS_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _module_of(value: Any) -> str:
    return getattr(type(value), "__module__", "") or ""


def _is_numpy_array(value: Any) -> bool:
    return _module_of(value).startswith("numpy") and hasattr(value, "shape")


def _is_pandas_dataframe(value: Any) -> bool:
    module = _module_of(value)
    return module.startswith("pandas") and type(value).__name__ == "DataFrame"


def _json_serializable(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def choose_format(value: Any) -> str:
    """Pick the safest durable format for one variable value."""
    if _is_numpy_array(value):
        try:
            import numpy  # noqa: F401

            return "npy"
        except ImportError:
            return "pickle"
    if _is_pandas_dataframe(value):
        try:
            import pyarrow  # noqa: F401

            return "parquet"
        except ImportError:
            return "csv"
    if _json_serializable(value):
        return "json"
    return "pickle"


def _data_path_for(directory: Path, name: str) -> Path | None:
    for extension in _FORMAT_EXTENSIONS.values():
        candidate = directory / f"{name}{extension}"
        if candidate.exists():
            return candidate
    return None


def _write_value(path: Path, value: Any, fmt: str) -> None:
    if fmt == "json":
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return
    if fmt == "npy":
        import numpy

        try:
            numpy.save(str(path), value, allow_pickle=False)
        except ValueError:
            # Object arrays cannot be saved without pickle; degrade safely.
            path.write_bytes(PICKLE_HEADER + pickle.dumps(value))
            path.rename(path.with_suffix(".pkl"))
        return
    if fmt == "parquet":
        value.to_parquet(str(path))
        return
    if fmt == "csv":
        value.to_csv(str(path), index=False)
        return
    if fmt == "pickle":
        path.write_bytes(PICKLE_HEADER + pickle.dumps(value))
        return
    raise ValueError(f"unsupported persistence format: {fmt}")


def _read_value(path: Path, fmt: str) -> Any:
    if fmt == "json":
        return json.loads(path.read_text(encoding="utf-8"))
    if fmt == "npy":
        import numpy

        return numpy.load(str(path), allow_pickle=False)
    if fmt == "parquet":
        import pandas

        return pandas.read_parquet(str(path))
    if fmt == "csv":
        import pandas

        return pandas.read_csv(str(path))
    if fmt == "pickle":
        raw = path.read_bytes()
        if not raw.startswith(PICKLE_HEADER):
            raise ValueError(
                "pickle snapshot is missing the QwenPaw header; refusing to "
                "restore an untrusted payload",
            )
        payload = raw[len(PICKLE_HEADER):]
        return pickle.loads(payload)  # noqa: S301 - trusted sandbox-local file
    raise ValueError(f"unsupported persistence format: {fmt}")


def _meta_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.meta.json"


def persist_variable(name: Any, value: Any, workspace: Path) -> dict[str, Any]:
    """Persist one variable under the workspace and return its metadata."""
    validated = validate_var_name(name)
    directory = vars_dir(workspace)
    fmt = choose_format(value)
    extension = _FORMAT_EXTENSIONS[fmt]

    # One logical variable owns one data file; drop stale format siblings.
    existing = _data_path_for(directory, validated)
    if existing is not None and existing.suffix != extension:
        existing.unlink()
    path = directory / f"{validated}{extension}"
    _write_value(path, value, fmt)

    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    meta = {
        "name": validated,
        "type": type(value).__name__,
        "size": size,
        "format": fmt,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _meta_path(directory, validated).write_text(
        json.dumps(meta, ensure_ascii=False),
        encoding="utf-8",
    )
    return meta


def describe_variable(name: Any, workspace: Path) -> dict[str, Any]:
    """Return persisted-variable metadata without loading the payload."""
    validated = validate_var_name(name)
    directory = vars_dir(workspace)
    meta_path = _meta_path(directory, validated)
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and meta.get("name") == validated:
                return meta
        except (OSError, json.JSONDecodeError):
            pass
    path = _data_path_for(directory, validated)
    if path is None:
        raise FileNotFoundError(f"no persisted variable named {validated!r}")
    fmt = _EXTENSION_FORMATS.get(path.suffix, "pickle")
    return {
        "name": validated,
        "type": "",
        "size": path.stat().st_size,
        "format": fmt,
        "saved_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S%z",
            time.localtime(path.stat().st_mtime),
        ),
    }


def restore_variable(name: Any, workspace: Path) -> Any:
    """Load one persisted variable back into the sandboxed kernel."""
    validated = validate_var_name(name)
    directory = vars_dir(workspace)
    path = _data_path_for(directory, validated)
    if path is None:
        raise FileNotFoundError(f"no persisted variable named {validated!r}")
    fmt = _EXTENSION_FORMATS.get(path.suffix, "pickle")
    return _read_value(path, fmt)


def _visible_names(namespace: dict[str, Any]) -> list[str]:
    internal = set(namespace.get("_paw_internal_names") or ())
    return sorted(
        name
        for name in namespace
        if name not in internal and not name.startswith("__")
    )


def _approx_bytes(value: Any) -> int:
    try:
        import sys

        return sys.getsizeof(value)
    except TypeError:
        return 0


def snapshot_namespace(
    namespace: dict[str, Any],
    destination: Path,
) -> list[str]:
    """Serialize the user namespace for crash recovery (roadmap §2.7).

    Returns the persisted variable names.  Values that cannot be written are
    skipped rather than failing the whole snapshot.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, Any]] = {}
    saved: list[str] = []
    for name in _visible_names(namespace)[:MAX_SNAPSHOT_VARS]:
        value = namespace[name]
        if _approx_bytes(value) > MAX_SNAPSHOT_VAR_BYTES:
            continue
        try:
            fmt = choose_format(value)
            extension = _FORMAT_EXTENSIONS[fmt]
            _write_value(destination / f"{name}{extension}", value, fmt)
        except Exception:  # noqa: BLE001 - best-effort recovery artifact
            continue
        manifest[name] = {"format": fmt, "file": f"{name}{extension}"}
        saved.append(name)
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    # Drop stale artifacts from a previous snapshot so variables deleted in
    # the kernel are not resurrected by a later restore.
    current_files = {entry["file"] for entry in manifest.values()}
    for entry in destination.iterdir():
        if entry.name == "manifest.json" or entry.name in current_files:
            continue
        if entry.is_file():
            entry.unlink(missing_ok=True)
    return saved


def restore_namespace(
    namespace: dict[str, Any],
    source: Path,
) -> list[str]:
    """Restore a previous :func:`snapshot_namespace` into ``namespace``."""
    source = Path(source)
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"snapshot manifest missing: {source}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored: list[str] = []
    if not isinstance(manifest, dict):
        return restored
    for name, entry in manifest.items():
        if not isinstance(entry, dict) or not _NAME_RE.match(str(name)):
            continue
        fmt = str(entry.get("format") or "pickle")
        path = source / str(entry.get("file") or "")
        if not path.is_file() or path.parent != source:
            continue
        try:
            namespace[str(name)] = _read_value(path, fmt)
        except Exception:  # noqa: BLE001 - skip unreadable entries
            continue
        restored.append(str(name))
    return restored


def snapshot_root(workspace: Path, session_tag: str = "default") -> Path:
    return Path(workspace).resolve() / SNAPSHOTS_SUBDIR / session_tag


def latest_snapshot_dir(
    workspace: Path,
    session_tag: str = "default",
) -> Path | None:
    """Return the most recently written snapshot directory for a tag."""
    root = snapshot_root(workspace, session_tag)
    if not root.is_dir():
        return None
    candidates = [
        item
        for item in root.iterdir()
        if item.is_dir() and (item / "manifest.json").is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


__all__ = [
    "PICKLE_HEADER",
    "choose_format",
    "describe_variable",
    "latest_snapshot_dir",
    "persist_variable",
    "restore_namespace",
    "restore_variable",
    "snapshot_namespace",
    "snapshot_root",
    "validate_var_name",
    "vars_dir",
]
