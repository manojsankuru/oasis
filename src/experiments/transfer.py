"""Second-area acquisition and pipeline run, isolated from the primary snapshot.

This is an experiment runner, not a model-visible tool and not a second scenario
engine.  It reaches the configured transfer area through ``config.TRANSFER_AREA``,
calls ``acquire.main`` once, and calls ``pipeline.run`` once.  The existing
pipeline remains the only route that computes risk tables or ``ScenarioRow``
trade-offs.

Transfer data lives under a fresh, generic ``outputs/transfer-work/<run-id>``
root.  Five path globals move together for the duration of the attempt because
the acquisition writers consult ``SNAPSHOT_DIR`` dynamically, hazard surfaces
consult ``DERIVED_DIR`` dynamically, and each ``Registry`` captures both the
manifest path and project root when it is constructed.  The five values are
restored in a ``finally``.  ``OUTPUTS_DIR``, ``PAPER_DIR`` and ``LOGS_DIR`` never
move; the transfer pipeline receives its paper-facing run directory explicitly.

The normal entry point first copies the already-authoritative primary trade-off
CSV byte for byte into the paper namespace.  It then fingerprints every primary
snapshot file before and after the isolated attempt, serialises only frozen
records and scalar evidence, and always tries to write a strict structured JSON
report after configuration has been restored.  No network request is implemented
here: the one live boundary remains ``src.acquire`` with its existing timeout and
bounded retry policy.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import rasterio

from .. import acquire, align, config, pipeline, provenance, verify
from ..contracts import AlignmentReport, Col, DatasetRecord, Provenance
from ..registry import Registry


SCHEMA_VERSION = 1
REPORT_NAME = "transfer_report.json"
WORK_NAMESPACE = "transfer-work"
PAPER_NAMESPACE = "transfer"
MANIFEST_NAME = "manifest.json"

CONFIG_PATHS: tuple[str, ...] = (
    "PROJECT_ROOT",
    "DATA_DIR",
    "SNAPSHOT_DIR",
    "DERIVED_DIR",
    "MANIFEST_PATH",
)

REQUIRED_REPORT_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "run_id",
        "status",
        "started_at",
        "finished_at",
        "stage",
        "last_observable_action",
        "study_area",
        "working_crs",
        "work_root",
        "snapshot_path",
        "manifest_path",
        "pipeline_output_path",
        "primary_snapshot_before",
        "primary_snapshot_after",
        "primary_snapshot_unchanged",
        "config_paths_restored",
        "error",
        "retry_observability",
        "network_attempts",
        "retries_by_stage",
        "datasets",
        "unregistered_partial_files",
        "partial_pipeline_files",
        "alignment",
        "pipeline",
        "written_files",
        "warnings",
    }
)

RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")
WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[/\\][^\s'\";,)\]}]+"
)

AcquireCall = Callable[[config.StudyArea], int]
PipelineCall = Callable[..., pipeline.PipelineResult]


class TransferSafetyError(RuntimeError):
    """A safety invariant failed after an otherwise ordinary transfer action."""


@dataclass(frozen=True, slots=True)
class TransferPaths:
    """Every path owned by one fresh transfer attempt."""

    run_id: str
    work_root: Path
    data_dir: Path
    snapshot_dir: Path
    derived_dir: Path
    manifest_path: Path
    pipeline_output_dir: Path


@dataclass(slots=True)
class RegistryEvidence:
    """Manifest-derived facts plus private GEOID sets used for cross-checks."""

    datasets: list[dict[str, Any]]
    geoids: dict[str, tuple[str, ...]]
    validation: dict[str, Any]


def utc_now() -> datetime:
    """A timezone-aware report timestamp."""

    return datetime.now(timezone.utc)


def new_run_id() -> str:
    """A generic, filename-safe identifier with collision-resistant suffix."""

    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def transfer_area() -> config.StudyArea:
    """The one configured area both production control-flow boundaries receive."""

    area = config.TRANSFER_AREA
    return area


def capture_config_paths() -> dict[str, Path]:
    """The five independently stored configuration paths."""

    return {name: Path(getattr(config, name)) for name in CONFIG_PATHS}


def build_paths(
    run_id: str,
    *,
    outputs_dir: Path,
    paper_dir: Path,
) -> TransferPaths:
    """Build the county-neutral layout for one attempt without creating it."""

    if not RUN_ID.fullmatch(run_id):
        raise ValueError(
            f"run_id {run_id!r} is not filename-safe; use letters, digits, '-' or '_'"
        )
    work_root = (Path(outputs_dir) / WORK_NAMESPACE / run_id).resolve()
    data_dir = work_root / "data"
    snapshot_dir = data_dir / "snapshot"
    return TransferPaths(
        run_id=run_id,
        work_root=work_root,
        data_dir=data_dir,
        snapshot_dir=snapshot_dir,
        derived_dir=data_dir / "derived",
        manifest_path=snapshot_dir / MANIFEST_NAME,
        pipeline_output_dir=(Path(paper_dir) / PAPER_NAMESPACE / run_id).resolve(),
    )


def paths_overlap(left: Path, right: Path) -> bool:
    """Whether either resolved path equals or contains the other."""

    first = Path(left).resolve()
    second = Path(right).resolve()
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def rebound_values(paths: TransferPaths) -> dict[str, Path]:
    """The exact values the isolation context will assign."""

    return {
        "PROJECT_ROOT": paths.work_root,
        "DATA_DIR": paths.data_dir,
        "SNAPSHOT_DIR": paths.snapshot_dir,
        "DERIVED_DIR": paths.derived_dir,
        "MANIFEST_PATH": paths.manifest_path,
    }


def validate_rebound_paths(
    values: dict[str, Path],
    originals: dict[str, Path],
    work_root: Path,
) -> None:
    """Refuse a rebound layout that can touch primary data or escape its root."""

    missing = sorted(set(CONFIG_PATHS) - set(values))
    if missing:
        raise TransferSafetyError(f"rebound configuration is missing {missing}")

    root = Path(work_root).resolve()
    resolved = {name: Path(values[name]).resolve() for name in CONFIG_PATHS}
    expected = {
        "PROJECT_ROOT": root,
        "DATA_DIR": root / "data",
        "SNAPSHOT_DIR": root / "data" / "snapshot",
        "DERIVED_DIR": root / "data" / "derived",
        "MANIFEST_PATH": root / "data" / "snapshot" / MANIFEST_NAME,
    }
    wrong_layout = {
        name: {"actual": value, "expected": expected[name]}
        for name, value in resolved.items()
        if value != expected[name]
    }
    if wrong_layout:
        raise TransferSafetyError(
            f"rebound path layout does not match the isolated root: {wrong_layout}"
        )

    outside = {
        name: value
        for name, value in resolved.items()
        if not value.is_relative_to(root)
    }
    if outside:
        raise TransferSafetyError(
            f"rebound paths escape the isolated work root: {outside}"
        )

    if resolved["MANIFEST_PATH"] != resolved["SNAPSHOT_DIR"] / MANIFEST_NAME:
        raise TransferSafetyError(
            "the isolated manifest is not exactly SNAPSHOT_DIR/manifest.json"
        )

    primary_names = ("DATA_DIR", "SNAPSHOT_DIR", "DERIVED_DIR", "MANIFEST_PATH")
    overlaps: list[str] = []
    for rebound_name in primary_names:
        for primary_name in primary_names:
            if paths_overlap(resolved[rebound_name], originals[primary_name]):
                overlaps.append(f"{rebound_name} overlaps primary {primary_name}")
    if overlaps:
        raise TransferSafetyError(
            "unsafe transfer target: " + "; ".join(overlaps)
        )


def validate_paper_destinations(
    paths: TransferPaths,
    originals: dict[str, Path],
    paper_dir: Path,
) -> None:
    """Refuse every paper-facing write if it could reach primary storage."""

    project_root = originals["PROJECT_ROOT"].resolve()
    paper_root = Path(paper_dir).resolve()
    if not paper_root.is_relative_to(project_root):
        raise TransferSafetyError(
            f"paper directory is outside the saved project root: {paper_root}"
        )
    if not paths.pipeline_output_dir.resolve().is_relative_to(paper_root):
        raise TransferSafetyError(
            "pipeline output escapes the saved paper directory: "
            f"{paths.pipeline_output_dir.resolve()}"
        )
    candidates = {
        "paper_directory": paper_root,
        "primary_tradeoff": paper_root / pipeline.TRADEOFF_NAME,
        "transfer_report": paper_root / REPORT_NAME,
        "pipeline_output": paths.pipeline_output_dir.resolve(),
    }
    final_files = {
        name: candidate
        for name, candidate in candidates.items()
        if name in ("primary_tradeoff", "transfer_report")
    }
    unsafe_final_files = {
        name: {
            "path": candidate,
            "is_symlink": candidate.is_symlink(),
            "resolved": candidate.resolve(),
        }
        for name, candidate in final_files.items()
        if candidate.is_symlink()
        or (candidate.exists() and not candidate.is_file())
        or not candidate.resolve().is_relative_to(paper_root)
    }
    if unsafe_final_files:
        raise TransferSafetyError(
            "paper final-file target is a symlink or escapes its directory: "
            f"{unsafe_final_files}"
        )
    primary_names = ("DATA_DIR", "SNAPSHOT_DIR", "DERIVED_DIR", "MANIFEST_PATH")
    overlaps = [
        f"{candidate_name} overlaps primary {primary_name}"
        for candidate_name, candidate in candidates.items()
        for primary_name in primary_names
        if paths_overlap(candidate, originals[primary_name])
    ]
    if overlaps:
        raise TransferSafetyError(
            "unsafe paper write destination: " + "; ".join(overlaps)
        )


def validate_canonical_report_destination(
    originals: dict[str, Path],
    paper_dir: Path,
) -> None:
    """Prove only the canonical report target safe before replacing stale evidence."""

    project_root = originals["PROJECT_ROOT"].resolve()
    expected_paper = (project_root / "outputs" / "paper").absolute()
    configured_paper = Path(config.PAPER_DIR).absolute()
    paper_root = Path(paper_dir).resolve()
    target = paper_root / REPORT_NAME
    if configured_paper != expected_paper or paper_root != expected_paper:
        raise TransferSafetyError(
            "the canonical report directory is not the exact outputs/paper namespace"
        )
    if (
        target.is_symlink()
        or (target.exists() and not target.is_file())
        or not target.resolve().is_relative_to(paper_root)
    ):
        raise TransferSafetyError(
            "the canonical transfer report target is a symlink, non-file, or escape"
        )
    primary_names = ("DATA_DIR", "SNAPSHOT_DIR", "DERIVED_DIR", "MANIFEST_PATH")
    if any(paths_overlap(target, originals[name]) for name in primary_names):
        raise TransferSafetyError(
            "the canonical transfer report target overlaps primary storage"
        )


def validate_namespace_layout(
    paths: TransferPaths,
    originals: dict[str, Path],
    outputs_dir: Path,
    paper_dir: Path,
) -> None:
    """Require the exact lexical output namespaces, refusing reparse redirection."""

    project_root = originals["PROJECT_ROOT"].resolve()
    expected_outputs = (project_root / "outputs").absolute()
    expected_paper = (expected_outputs / "paper").absolute()
    configured_outputs = Path(config.OUTPUTS_DIR).absolute()
    configured_paper = Path(config.PAPER_DIR).absolute()
    resolved_outputs = Path(outputs_dir).resolve()
    resolved_paper = Path(paper_dir).resolve()
    expected_work_root = (
        expected_outputs / WORK_NAMESPACE / paths.run_id
    ).absolute()
    expected_pipeline_output = (
        expected_paper / PAPER_NAMESPACE / paths.run_id
    ).absolute()

    mismatches = {
        "OUTPUTS_DIR_lexical": (configured_outputs, expected_outputs),
        "OUTPUTS_DIR_resolved": (resolved_outputs, expected_outputs),
        "PAPER_DIR_lexical": (configured_paper, expected_paper),
        "PAPER_DIR_resolved": (resolved_paper, expected_paper),
        "work_root": (paths.work_root.resolve(), expected_work_root),
        "pipeline_output": (
            paths.pipeline_output_dir.resolve(),
            expected_pipeline_output,
        ),
    }
    wrong = {
        name: {"actual": actual, "expected": expected}
        for name, (actual, expected) in mismatches.items()
        if actual != expected
    }
    if wrong:
        raise TransferSafetyError(
            f"transfer paths do not remain in the exact output namespaces: {wrong}"
        )


@contextmanager
def isolated_config(
    paths: TransferPaths,
    originals: dict[str, Path],
) -> Iterator[None]:
    """Move all five config paths into a fresh root and restore them on exit."""

    if paths.work_root.exists():
        raise FileExistsError(
            f"transfer work root already exists and will not be reused: {paths.work_root}"
        )
    if paths.pipeline_output_dir.exists():
        raise FileExistsError(
            "transfer paper run directory already exists and will not be reused: "
            f"{paths.pipeline_output_dir}"
        )

    validate_rebound_paths(rebound_values(paths), originals, paths.work_root)
    paths.work_root.parent.mkdir(parents=True, exist_ok=True)
    paths.work_root.mkdir(exist_ok=False)
    try:
        config.PROJECT_ROOT = paths.work_root
        config.DATA_DIR = paths.data_dir
        config.SNAPSHOT_DIR = paths.snapshot_dir
        config.DERIVED_DIR = paths.derived_dir
        config.MANIFEST_PATH = paths.manifest_path

        # This second validation deliberately inspects the assigned globals.  It
        # must run before ensure_dirs(), fixture writes, acquisition, or yield.
        validate_rebound_paths(capture_config_paths(), originals, paths.work_root)
        config.ensure_dirs()
        yield
    finally:
        for name in CONFIG_PATHS:
            setattr(config, name, originals[name])


def sha256_file(path: Path) -> str:
    """SHA-256 of one file, streamed rather than loaded wholesale."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_bytes(destination: Path, payload: bytes) -> Path:
    """Durably replace one file without following an existing final symlink."""

    target = Path(destination)
    if target.is_symlink():
        raise TransferSafetyError(
            f"refusing to replace a symbolic-link artifact target: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def file_fingerprint(path: Path) -> dict[str, Any]:
    """Size, nanosecond mtime and hash, refusing a file that moves mid-read."""

    target = Path(path).resolve()
    before = target.stat()
    digest = sha256_file(target)
    after = target.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"file changed while it was fingerprinted: {target}")
    return {
        "size": after.st_size,
        "st_mtime_ns": after.st_mtime_ns,
        "sha256": digest,
    }


def file_inventory(root: Path) -> dict[str, dict[str, Any]]:
    """Deterministic fingerprint of every file below a directory."""

    base = Path(root).resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"snapshot directory does not exist: {base}")
    return {
        path.relative_to(base).as_posix(): file_fingerprint(path)
        for path in sorted(base.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    }


def fingerprint_primary(
    *,
    snapshot_dir: Path,
    manifest_path: Path,
    project_root: Path,
    area: config.StudyArea,
) -> dict[str, Any]:
    """Complete primary snapshot inventory plus a registry-derived tract count."""

    manifest = Path(manifest_path).resolve()
    registry = Registry(
        study_area=area,
        manifest_path=manifest,
        root=Path(project_root).resolve(),
    )
    registry.load_manifest()
    tract_count = len(registry.load(acquire.DATASET_TRACTS))
    return {
        "manifest_path": manifest,
        "manifest": file_fingerprint(manifest),
        "tract_count": tract_count,
        "inventory": file_inventory(snapshot_dir),
    }


def csv_contents(payload: bytes) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """A CSV header and rows, preserving column order and cell text."""

    text = payload.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise ValueError("the authoritative trade-off CSV is empty")
    return tuple(rows[0]), tuple(tuple(row) for row in rows[1:])


def csv_geoid_tuple(value: Any) -> tuple[str, ...]:
    """Parse the pipeline writer's space-separated GEOID tuple without cleaning."""

    if value is None or value is pd.NA or value is pd.NaT or pd.isna(value):
        return ()
    return tuple(str(value).split(" "))


def copy_primary_tradeoff(source: Path, destination: Path) -> dict[str, Any]:
    """Copy authoritative trade-off bytes and independently verify their shape."""

    origin = Path(source)
    target = Path(destination)
    payload = origin.read_bytes()
    source_columns, source_rows = csv_contents(payload)
    atomic_write_bytes(target, payload)

    copied = target.read_bytes()
    destination_columns, destination_rows = csv_contents(copied)
    source_hash = hashlib.sha256(payload).hexdigest()
    destination_hash = hashlib.sha256(copied).hexdigest()
    evidence = {
        "source": origin.resolve(),
        "destination": target.resolve(),
        "byte_count": len(payload),
        "source_sha256": source_hash,
        "destination_sha256": destination_hash,
        "bytes_equal": copied == payload,
        "columns_equal": destination_columns == source_columns,
        "rows_equal": destination_rows == source_rows,
        "columns": source_columns,
        "row_count": len(source_rows),
    }
    if not all(
        evidence[name]
        for name in ("bytes_equal", "columns_equal", "rows_equal")
    ) or source_hash != destination_hash:
        raise RuntimeError("the paper trade-off copy does not match its source")
    return evidence


def portable_path(path: Path, project_root: Path, work_root: Path) -> str:
    """A POSIX path relative to the saved project root or current work root."""

    target = Path(path).resolve()
    for base in (Path(project_root).resolve(), Path(work_root).resolve()):
        if target == base:
            return "."
        if target.is_relative_to(base):
            return target.relative_to(base).as_posix()
    raise ValueError(
        f"path is outside both portable roots and cannot enter the report: {target}"
    )


def portable_text(
    value: str,
    project_root: Path,
    work_root: Path,
) -> tuple[str, bool]:
    """Normalize filesystem paths embedded inside exact diagnostic strings."""

    normalized = value
    for root in (Path(project_root).resolve(), Path(work_root).resolve()):
        spellings = (str(root), root.as_posix())
        for spelling in spellings:
            normalized = re.sub(
                re.escape(spelling),
                ".",
                normalized,
                flags=re.IGNORECASE,
            )
    normalized = WINDOWS_ABSOLUTE_PATH.sub("$ABSOLUTE_PATH", normalized)
    changed = normalized != value
    if changed:
        normalized = normalized.replace("\\", "/")
    return normalized, changed


def json_safe(
    value: Any,
    *,
    project_root: Path,
    work_root: Path,
    warnings: list[str],
    location: str = "report",
) -> Any:
    """Convert scalar evidence to strict JSON and refuse spatial/bulk objects."""

    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Path):
        return portable_path(value, project_root, work_root)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{location} contains a naive datetime")
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_safe(
            dataclasses.asdict(value),
            project_root=project_root,
            work_root=work_root,
            warnings=warnings,
            location=location,
        )
    if isinstance(value, dict):
        return {
            str(key): json_safe(
                item,
                project_root=project_root,
                work_root=work_root,
                warnings=warnings,
                location=f"{location}.{key}",
            )
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            json_safe(
                item,
                project_root=project_root,
                work_root=work_root,
                warnings=warnings,
                location=f"{location}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        return json_safe(
            sorted(value, key=str),
            project_root=project_root,
            work_root=work_root,
            warnings=warnings,
            location=location,
        )
    if isinstance(value, (pd.DataFrame, pd.Series, pd.Index)):
        raise TypeError(f"{location} contains a pandas frame/array, which is not reportable")
    if type(value).__module__.startswith("shapely"):
        raise TypeError(f"{location} contains a geometry, which is not reportable")
    if type(value).__module__.startswith("numpy"):
        if getattr(value, "ndim", 0) != 0:
            raise TypeError(f"{location} contains a NumPy array, which is not reportable")
        value = value.item()
    elif hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            warnings.append(f"{location}: non-finite value serialised as null")
            return None
        return float(value)
    if isinstance(value, str):
        portable, changed = portable_text(value, project_root, work_root)
        if changed:
            warnings.append(
                f"{location}: filesystem path text normalized for portability"
            )
        return portable
    raise TypeError(f"{location} contains unsupported {type(value).__name__}")


def write_strict_report(
    report: dict[str, Any],
    destination: Path,
    *,
    project_root: Path,
    work_root: Path,
) -> Path:
    """Write portable JSON with no NaN/Infinity tokens."""

    conversion_warnings: list[str] = []
    safe = json_safe(
        report,
        project_root=project_root,
        work_root=work_root,
        warnings=conversion_warnings,
    )
    if conversion_warnings:
        listed = safe.setdefault("warnings", [])
        listed.extend(conversion_warnings)
    payload = (
        json.dumps(safe, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    return atomic_write_bytes(Path(destination), payload)


def provenance_payload(record: Provenance) -> dict[str, Any]:
    """Every field of the frozen provenance record, with no inferred values."""

    payload = provenance.provenance_to_dict(record)
    required = {field.name for field in dataclasses.fields(Provenance)}
    if set(payload) != required:
        raise ValueError(
            f"provenance serialisation fields {sorted(payload)} do not match {sorted(required)}"
        )
    return payload


def dataset_payload(record: DatasetRecord, validation: dict[str, Any]) -> dict[str, Any]:
    """One frozen dataset record plus separately measured validation evidence."""

    return {
        "name": record.name,
        "kind": record.kind,
        "path": record.path,
        "provenance": provenance_payload(record.provenance),
        "validation": validation,
    }


def request_area_match(record: DatasetRecord, area: config.StudyArea) -> bool | None:
    """Whether request parameters that name an area identify the configured one."""

    params = record.provenance.request_params
    if record.name in (acquire.DATASET_TRACTS, acquire.DATASET_BLOCK_GROUPS):
        expected = f"STATE='{area.state_fips}' AND COUNTY='{area.county_fips}'"
        return params.get("where") == expected
    if record.name in (acquire.DATASET_ACS, acquire.DATASET_ACS_BLOCK_GROUPS):
        expected = f"state:{area.state_fips} county:{area.county_fips}"
        return params.get("in") == expected
    return None


def collect_registry_evidence(
    registry: Registry,
    records: list[DatasetRecord],
    *,
    area: config.StudyArea,
    work_root: Path,
) -> RegistryEvidence:
    """Validate manifest paths/counts and measure cross-dataset identity facts."""

    if not records:
        raise ValueError("the transfer manifest registered no datasets")

    required_datasets = {
        acquire.DATASET_TRACTS,
        acquire.DATASET_BLOCK_GROUPS,
        acquire.DATASET_ACS,
        acquire.DATASET_ACS_BLOCK_GROUPS,
        acquire.DATASET_ELEVATION,
        acquire.DATASET_FACILITIES,
        acquire.DATASET_FLOOD_ZONES,
    }
    registered_names = {record.name for record in records}
    missing_datasets = tuple(sorted(required_datasets - registered_names))
    if missing_datasets:
        raise ValueError(
            f"the transfer manifest is missing required dataset(s): {missing_datasets}"
        )

    root = Path(work_root).resolve()
    datasets: list[dict[str, Any]] = []
    geoids: dict[str, tuple[str, ...]] = {}
    request_checks: dict[str, bool] = {}
    wrong_prefixes: dict[str, tuple[str, ...]] = {}

    for record in records:
        path = record.path.resolve()
        record_provenance = record.provenance
        aware = (
            record_provenance.retrieved_at.tzinfo is not None
            and record_provenance.retrieved_at.utcoffset() is not None
        )
        required_text = (
            record_provenance.source_url,
            record_provenance.declared_crs,
            record_provenance.working_crs,
            record_provenance.vintage,
            record_provenance.license,
        )
        validation: dict[str, Any] = {
            "path_inside_work_root": path.is_relative_to(root),
            "path_exists": path.is_file(),
            "provenance_name_matches": record_provenance.dataset == record.name,
            "working_crs_matches_area": (
                record_provenance.working_crs == area.working_crs
            ),
            "retrieved_at_timezone_aware": aware,
            "complete_text_fields": all(bool(value) for value in required_text),
            "request_params_present": (
                isinstance(record_provenance.request_params, dict)
                and bool(record_provenance.request_params)
            ),
            "notes_present": (
                isinstance(record_provenance.notes, tuple)
                and bool(record_provenance.notes)
            ),
            "request_area_matches": request_area_match(record, area),
        }
        if not all(
            validation[name]
            for name in (
                "path_inside_work_root",
                "path_exists",
                "provenance_name_matches",
                "working_crs_matches_area",
                "retrieved_at_timezone_aware",
                "complete_text_fields",
                "request_params_present",
                "notes_present",
            )
        ):
            raise ValueError(f"registered dataset {record.name!r} failed {validation}")

        if record.kind == "raster":
            with rasterio.open(path) as raster:
                actual_count = int(raster.count)
                validation["raster_width"] = int(raster.width)
                validation["raster_height"] = int(raster.height)
        else:
            frame = registry.load(record.name)
            actual_count = len(frame)
            if Col.GEOID in frame.columns:
                raw_geoids = frame[Col.GEOID]
                if raw_geoids.isna().any():
                    raise ValueError(
                        f"{record.name!r} contains null {Col.GEOID!r} values; "
                        "validation will not drop or repair them locally"
                    )
                identifiers = tuple(sorted(str(value) for value in raw_geoids.tolist()))
                geoids[record.name] = identifiers
                validation["geoid_count"] = len(identifiers)
                validation["geoid_count_matches_rows"] = len(identifiers) == actual_count
                validation["geoids_unique"] = len(set(identifiers)) == len(identifiers)
                geoid_width = (
                    11
                    if record.name in (acquire.DATASET_TRACTS, acquire.DATASET_ACS)
                    else 12
                    if record.name in (
                        acquire.DATASET_BLOCK_GROUPS,
                        acquire.DATASET_ACS_BLOCK_GROUPS,
                    )
                    else None
                )
                malformed = tuple(
                    identifier
                    for identifier in identifiers
                    if not identifier.isdigit()
                    or (geoid_width is not None and len(identifier) != geoid_width)
                )
                validation["malformed_geoids"] = malformed
                if not validation["geoid_count_matches_rows"]:
                    raise ValueError(
                        f"{record.name!r} exposes {len(identifiers)} GEOIDs for "
                        f"{actual_count} row(s)"
                    )
                if not validation["geoids_unique"]:
                    raise ValueError(
                        f"{record.name!r} contains duplicate {Col.GEOID!r} values"
                    )
                if malformed:
                    raise ValueError(
                        f"{record.name!r} contains malformed {Col.GEOID!r} values: "
                        f"{malformed[:10]}"
                    )
                wrong = tuple(
                    identifier
                    for identifier in identifiers
                    if not identifier.startswith(area.county_geoid)
                )
                wrong_prefixes[record.name] = wrong

        validation["actual_feature_count"] = actual_count
        validation["feature_count_matches"] = (
            actual_count == record_provenance.feature_count
        )
        validation["degraded"] = align.is_degraded(record_provenance)
        validation["degraded_reasons"] = tuple(
            note for note in record_provenance.notes if note.startswith(align.DEGRADED_NOTE_PREFIX)
        )
        if not validation["feature_count_matches"]:
            raise ValueError(
                f"{record.name!r} loads {actual_count} feature(s), provenance records "
                f"{record_provenance.feature_count}"
            )
        if validation["request_area_matches"] is False:
            raise ValueError(
                f"{record.name!r} request parameters do not identify the configured transfer area"
            )
        if validation["request_area_matches"] is not None:
            request_checks[record.name] = bool(validation["request_area_matches"])
        datasets.append(dataset_payload(record, validation))

    wrong = {
        name: values for name, values in wrong_prefixes.items() if values
    }
    if wrong:
        raise ValueError(
            f"registered GEOIDs do not begin with the configured county prefix: {wrong}"
        )

    tract_boundary = set(geoids.get(acquire.DATASET_TRACTS, ()))
    tract_attributes = set(geoids.get(acquire.DATASET_ACS, ()))
    group_boundary = set(geoids.get(acquire.DATASET_BLOCK_GROUPS, ()))
    group_attributes = set(geoids.get(acquire.DATASET_ACS_BLOCK_GROUPS, ()))
    validation = {
        "required_datasets": tuple(sorted(required_datasets)),
        "registered_datasets": tuple(sorted(registered_names)),
        "all_required_datasets_registered": not missing_datasets,
        "request_area_matches": request_checks,
        "all_request_area_checks_pass": bool(request_checks) and all(request_checks.values()),
        "all_geoids_match_prefix": not wrong,
        "geoid_prefix": area.county_geoid,
        "tracts": {
            "boundary_count": len(tract_boundary),
            "attribute_count": len(tract_attributes),
            "sets_equal": tract_boundary == tract_attributes,
            "boundary_without_attributes": tuple(sorted(tract_boundary - tract_attributes)),
            "attributes_without_boundary": tuple(sorted(tract_attributes - tract_boundary)),
        },
        "block_groups": {
            "boundary_count": len(group_boundary),
            "attribute_count": len(group_attributes),
            "sets_equal": group_boundary == group_attributes,
            "boundary_without_attributes": tuple(sorted(group_boundary - group_attributes)),
            "attributes_without_boundary": tuple(sorted(group_attributes - group_boundary)),
        },
        "population_total": None,
        "population_source": (
            "populated from RiskTable.exposure.population_total and verified "
            "against aligned PipelineResult tables after pipeline"
        ),
    }
    expected_request_checks = {
        acquire.DATASET_TRACTS,
        acquire.DATASET_BLOCK_GROUPS,
        acquire.DATASET_ACS,
        acquire.DATASET_ACS_BLOCK_GROUPS,
    }
    if set(request_checks) != expected_request_checks:
        raise ValueError(
            "the transfer manifest did not expose every area-bearing request "
            f"parameter check: observed {tuple(sorted(request_checks))}"
        )
    if not validation["tracts"]["sets_equal"]:
        raise ValueError(
            "tract boundary and ACS GEOID sets differ: "
            f"{validation['tracts']}"
        )
    if not validation["block_groups"]["sets_equal"]:
        raise ValueError(
            "block-group boundary and ACS GEOID sets differ: "
            f"{validation['block_groups']}"
        )
    return RegistryEvidence(
        datasets=datasets,
        geoids=geoids,
        validation=validation,
    )


def call_acquisition(area: config.StudyArea, acquire_call: AcquireCall) -> int:
    """Invoke the configured acquisition boundary exactly once and require zero."""

    return_code = acquire_call(area)
    if return_code != 0:
        raise RuntimeError(f"acquire.main returned nonzero status {return_code}")
    return return_code


def call_pipeline(
    area: config.StudyArea,
    outputs: Path,
    pipeline_call: PipelineCall,
    invocation_log: list[bool] | None = None,
) -> pipeline.PipelineResult:
    """Invoke the existing pipeline once, with its run-specific output path."""

    if Path(outputs).exists():
        raise FileExistsError(
            f"transfer paper run directory appeared before pipeline execution: {outputs}"
        )
    if invocation_log is not None:
        invocation_log.append(True)
    return pipeline_call(area=area, outputs=Path(outputs))


def pipeline_payload(
    result: pipeline.PipelineResult,
    *,
    area: config.StudyArea,
    tract_count: int,
    output_dir: Path,
) -> dict[str, Any]:
    """Selective pipeline evidence: no frames, geometry, coordinates or rasters."""

    if result.study_area != area:
        raise ValueError("PipelineResult.study_area is not the configured transfer area")

    expected_scenarios = tuple(item.name for item in pipeline.HAZARD_SCENARIOS)
    observed_scenarios = tuple(result.tables)
    if observed_scenarios != expected_scenarios:
        raise ValueError(
            "PipelineResult did not complete the default scenario sequence: "
            f"expected {expected_scenarios}, observed {observed_scenarios}"
        )

    expected_presets = tuple(item.name for item in pipeline.WEIGHT_PRESETS)
    default_preset = pipeline.vulnerability.DEFAULT_PRESET
    expected_tradeoff_pairs = {
        (scenario, preset)
        for scenario in expected_scenarios
        for preset in expected_presets
    }
    observed_tradeoff_pairs = [
        (row.scenario, row.preset) for row in result.tradeoff
    ]
    if (
        len(observed_tradeoff_pairs) != len(expected_tradeoff_pairs)
        or set(observed_tradeoff_pairs) != expected_tradeoff_pairs
    ):
        raise ValueError(
            "PipelineResult trade-off rows do not cover every default "
            f"scenario/preset pair: observed {observed_tradeoff_pairs}"
        )
    wrong_tradeoff_geoids = tuple(
        geoid
        for row in result.tradeoff
        for geoid in (*row.top_geoids, *row.displaced_geoids)
        if not str(geoid).startswith(area.county_geoid)
    )
    if wrong_tradeoff_geoids:
        raise ValueError(
            "PipelineResult trade-off rows contain GEOIDs outside the configured "
            f"area: {wrong_tradeoff_geoids[:10]}"
        )

    tables: dict[str, Any] = {}
    population_totals: dict[str, int | float | None] = {}
    population_missing: dict[str, int] = {}
    for scenario, table in result.tables.items():
        frame = table.frame
        required_columns = (Col.GEOID, Col.POPULATION, Col.RISK_SCORE, Col.PRIORITY_RANK)
        absent = tuple(column for column in required_columns if column not in frame.columns)
        if absent:
            raise KeyError(
                f"pipeline table {scenario!r} lacks columns from contracts.Col: {absent}"
            )
        if table.scenario.name != scenario:
            raise ValueError(
                f"pipeline table key {scenario!r} names scenario "
                f"{table.scenario.name!r}"
            )
        if table.risk.preset != default_preset.name:
            raise ValueError(
                f"pipeline table {scenario!r} used preset {table.risk.preset!r}, "
                f"expected the production default {default_preset.name!r}"
            )
        score_present = frame[Col.RISK_SCORE].notna()
        rank_present = frame[Col.PRIORITY_RANK].notna()
        if not score_present.equals(rank_present):
            raise ValueError(
                f"pipeline table {scenario!r} disagrees on score/rank nullness"
            )
        scored = int(score_present.sum())
        unscored = int((~score_present).sum())
        if len(frame) != tract_count or scored + unscored != tract_count:
            raise ValueError(
                f"pipeline table {scenario!r} accounts for {len(frame)} row(s), "
                f"{scored} scored + {unscored} unscored, but the transfer registry "
                f"loaded {tract_count} tract(s)"
            )

        table_geoids = frame[Col.GEOID]
        if table_geoids.isna().any():
            raise ValueError(
                f"pipeline table {scenario!r} contains null {Col.GEOID!r} values"
            )
        wrong_table_geoids = tuple(
            str(value)
            for value in table_geoids.tolist()
            if not str(value).startswith(area.county_geoid)
        )
        if wrong_table_geoids:
            raise ValueError(
                f"pipeline table {scenario!r} contains GEOIDs outside the "
                f"configured area: {wrong_table_geoids[:10]}"
            )

        population = frame[Col.POPULATION]
        if not pd.api.types.is_numeric_dtype(population.dtype):
            raise TypeError(
                f"aligned pipeline table {scenario!r} has nonnumeric "
                f"{Col.POPULATION!r}; the transfer validator will not coerce it"
            )
        measured_population = population.sum(min_count=1)
        if pd.isna(measured_population):
            observed_population: int | float | None = None
        elif hasattr(measured_population, "item"):
            observed_population = measured_population.item()
        else:
            observed_population = measured_population
        evidence_population = float(table.exposure.population_total)
        if (
            observed_population is None
            or not math.isfinite(evidence_population)
            or not math.isclose(
                float(observed_population),
                evidence_population,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            raise ValueError(
                f"pipeline table {scenario!r} population {observed_population!r} "
                "does not match its existing ExposureEvidence total "
                f"{evidence_population!r}"
            )
        population_total: int | float = table.exposure.population_total
        population_totals[scenario] = population_total
        population_missing[scenario] = int(population.isna().sum())
        tables[scenario] = {
            "row_count": len(frame),
            "units_scored": scored,
            "units_unscored": unscored,
            "population_total": population_total,
            "population_missing": population_missing[scenario],
            "score_rank_nullness_agrees": True,
            "risk_evidence": dataclasses.asdict(table.risk),
            "exposure_evidence": dataclasses.asdict(table.exposure),
            "resilience_evidence": dataclasses.asdict(table.resilience),
            "vulnerability_evidence": dataclasses.asdict(table.vulnerability),
        }

    if not population_totals or len(set(population_totals.values())) != 1:
        raise ValueError(
            "population totals are absent or disagree across pipeline scenarios: "
            f"{population_totals}"
        )

    root = Path(output_dir).resolve()
    written: list[Path] = []
    for path in result.written:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(
                f"PipelineResult.written contains a missing or out-of-run path: {resolved}"
            )
        written.append(resolved)

    if len(written) != len(set(written)):
        raise ValueError("PipelineResult.written contains duplicate paths")
    expected_tables_by_name: dict[str, Any] = {}
    for table in result.tables.values():
        preset_name = table.risk.preset
        if preset_name != default_preset.name:
            raise ValueError(
                f"pipeline table names nondefault scoring preset {preset_name!r}"
            )
        filename = pipeline.table_name(table.scenario, default_preset)
        if filename in expected_tables_by_name:
            raise ValueError(f"multiple pipeline tables map to {filename!r}")
        expected_tables_by_name[filename] = table
    expected_table_names = set(expected_tables_by_name)
    expected_written_names = {
        *expected_table_names,
        pipeline.TRADEOFF_NAME,
        pipeline.REPORT_NAME,
    }
    observed_written_names = {path.name for path in written}
    if (
        len(written) != len(expected_written_names)
        or observed_written_names != expected_written_names
    ):
        raise ValueError(
            "PipelineResult.written does not contain exactly the current run's "
            f"required artifacts: expected {tuple(sorted(expected_written_names))}, "
            f"observed {tuple(sorted(observed_written_names))}"
        )

    table_file_rows: dict[str, int] = {}
    for path in written:
        if path.name not in expected_table_names:
            continue
        saved = pd.read_csv(path, dtype={Col.GEOID: str})
        if tuple(saved.columns) != pipeline.REPORTED_COLUMNS:
            raise ValueError(
                f"written pipeline table {path.name!r} has unexpected columns: "
                f"{tuple(saved.columns)}"
            )
        if len(saved) != tract_count:
            raise ValueError(
                f"written pipeline table {path.name!r} has {len(saved)} row(s), "
                f"expected {tract_count}"
            )
        expected_buffer = io.StringIO()
        pipeline.reported(expected_tables_by_name[path.name].frame).to_csv(
            expected_buffer,
            index=False,
        )
        expected_saved = pd.read_csv(
            io.StringIO(expected_buffer.getvalue()),
            dtype={Col.GEOID: str},
        )
        if not saved.equals(expected_saved):
            raise ValueError(
                f"written pipeline table {path.name!r} does not round-trip to "
                "the corresponding PipelineResult frame"
            )
        table_file_rows[path.name] = len(saved)

    tradeoff_path = next(path for path in written if path.name == pipeline.TRADEOFF_NAME)
    saved_tradeoff = pd.read_csv(tradeoff_path)
    saved_pairs = list(zip(saved_tradeoff["scenario"], saved_tradeoff["preset"]))
    scenario_row_fields = {
        field.name for field in dataclasses.fields(result.tradeoff[0])
    }
    if (
        len(saved_pairs) != len(expected_tradeoff_pairs)
        or saved_pairs != observed_tradeoff_pairs
        or set(saved_tradeoff.columns) != scenario_row_fields
    ):
        raise ValueError(
            "written transfer trade-off does not preserve every scenario/preset "
            "row and every ScenarioRow field"
        )
    saved_tradeoff_rows = {
        (record["scenario"], record["preset"]): record
        for record in saved_tradeoff.to_dict(orient="records")
    }
    tradeoff_mismatches: list[str] = []
    for expected_row in result.tradeoff:
        pair = (expected_row.scenario, expected_row.preset)
        saved_row = saved_tradeoff_rows[pair]
        saved_mean = saved_row["mean_inundation_m"]
        expected_mean = expected_row.mean_inundation_m
        mean_matches = (
            bool(pd.isna(saved_mean)) and math.isnan(expected_mean)
        ) or math.isclose(
            float(saved_mean),
            float(expected_mean),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        if not (
            csv_geoid_tuple(saved_row["top_geoids"]) == expected_row.top_geoids
            and csv_geoid_tuple(saved_row["displaced_geoids"])
            == expected_row.displaced_geoids
            and int(saved_row["population_in_priority"])
            == expected_row.population_in_priority
            and int(saved_row["vulnerable_population_in_priority"])
            == expected_row.vulnerable_population_in_priority
            and mean_matches
        ):
            tradeoff_mismatches.append(f"{pair[0]}/{pair[1]}")
    if tradeoff_mismatches:
        raise ValueError(
            "written transfer trade-off rows differ from PipelineResult: "
            f"{tuple(tradeoff_mismatches)}"
        )
    pipeline_report_path = next(
        path for path in written if path.name == pipeline.REPORT_NAME
    )
    if not pipeline_report_path.read_text(encoding="utf-8").strip():
        raise ValueError("the written pipeline report is empty")

    return {
        "study_area": dataclasses.asdict(result.study_area),
        "study_area_matches_requested": result.study_area == area,
        "scenarios_completed": observed_scenarios,
        "expected_scenarios": expected_scenarios,
        "expected_presets": expected_presets,
        "default_table_preset": default_preset.name,
        "row_counts": {name: evidence["row_count"] for name, evidence in tables.items()},
        "population_total": next(iter(population_totals.values())),
        "population_missing_by_scenario": population_missing,
        "tables": tables,
        "warnings": tuple(result.warnings),
        "scenario_rows": tuple(dataclasses.asdict(row) for row in result.tradeoff),
        "written_files": tuple(written),
        "artifact_validation": {
            "expected_names": tuple(sorted(expected_written_names)),
            "observed_names": tuple(sorted(observed_written_names)),
            "table_file_rows": table_file_rows,
            "table_files_match_pipeline_result": True,
            "tradeoff_row_count": len(saved_tradeoff),
            "tradeoff_matches_pipeline_result": True,
            "pipeline_report_nonempty": True,
        },
    }


def list_attempt_files(root: Path) -> list[Path]:
    """Files under exactly one current attempt, never its shared parent."""

    target = Path(root)
    if not target.exists():
        return []
    return sorted(
        (path.resolve() for path in target.rglob("*") if path.is_file()),
        key=lambda path: path.as_posix(),
    )


def git_data_state(project_root: Path) -> dict[str, Any]:
    """Tracked/staged/unignored-untracked Git state under data only."""

    root = Path(project_root)
    commands = {
        "unstaged_diff": ["git", "diff", "--quiet", "--", "data"],
        "staged_diff": ["git", "diff", "--cached", "--quiet", "--", "data"],
        "status": ["git", "status", "--short", "--untracked-files=all", "--", "data"],
    }
    evidence: dict[str, Any] = {}
    clean = True
    for name, command in commands.items():
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        evidence[name] = {
            "return_code": completed.returncode,
            "output": completed.stdout.strip(),
            "error": completed.stderr.strip(),
        }
        if name == "status":
            clean = clean and completed.returncode == 0 and not completed.stdout.strip()
        else:
            clean = clean and completed.returncode == 0
    evidence["clean"] = clean
    return evidence


def base_report(
    paths: TransferPaths,
    *,
    area: config.StudyArea,
    started_at: datetime,
) -> dict[str, Any]:
    """The complete stable report shape, populated progressively by measured work."""

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": paths.run_id,
        "status": "unattempted",
        "started_at": started_at,
        "finished_at": None,
        "stage": "initialization",
        "last_observable_action": "unknown",
        "study_area": dataclasses.asdict(area),
        "working_crs": area.working_crs,
        "work_root": paths.work_root,
        "snapshot_path": paths.snapshot_dir,
        "manifest_path": paths.manifest_path,
        "pipeline_output_path": paths.pipeline_output_dir,
        "primary_snapshot_before": None,
        "primary_snapshot_after": None,
        "primary_snapshot_unchanged": None,
        "config_paths_restored": False,
        "error": None,
        "retry_observability": "not_applicable",
        "network_attempts": None,
        "retries_by_stage": None,
        "datasets": [],
        "unregistered_partial_files": [],
        "partial_pipeline_files": [],
        "alignment": None,
        "pipeline": None,
        "written_files": [],
        "warnings": [],
        "primary_tradeoff": None,
        "boundary_verification": None,
        "safety_verification": None,
    }


def exception_payload(exc: BaseException) -> dict[str, str]:
    """Exact local error identity plus a hash that survives path normalization."""

    message = str(exc)
    return {
        "type": type(exc).__name__,
        "message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
    }


def report_write_fallback(
    report: dict[str, Any],
    paths: TransferPaths,
    *,
    area: config.StudyArea,
    exc: Exception,
) -> dict[str, Any]:
    """A controlled current-attempt report if full evidence cannot serialize."""

    started_at = report.get("started_at")
    if not isinstance(started_at, datetime):
        started_at = utc_now()
    fallback = base_report(paths, area=area, started_at=started_at)
    for name in ("work_root", "snapshot_path", "manifest_path", "pipeline_output_path"):
        fallback[name] = None
    fallback.update(
        {
            "status": "failed",
            "finished_at": utc_now(),
            "stage": "artifacts",
            "last_observable_action": (
                f"{type(exc).__name__} raised while writing the full transfer "
                f"report: {exc}"
            ),
            "config_paths_restored": bool(report.get("config_paths_restored")),
            "primary_snapshot_unchanged": report.get(
                "primary_snapshot_unchanged"
            ),
            "error": exception_payload(exc),
            "retry_observability": report.get(
                "retry_observability", "not_applicable"
            ),
            "network_attempts": report.get("network_attempts"),
            "retries_by_stage": report.get("retries_by_stage"),
            "warnings": [
                "the full current-attempt report could not be serialized; "
                "bulk evidence was omitted rather than inferred",
                f"underlying transfer state was status={report.get('status')!r}, "
                f"stage={report.get('stage')!r}",
            ],
        }
    )
    underlying = report.get("error")
    if isinstance(underlying, dict):
        fallback["warnings"].append(
            "underlying transfer error was "
            f"{underlying.get('type')}: {underlying.get('message')}"
        )
    return fallback


def record_failure(
    report: dict[str, Any],
    *,
    stage: str,
    exc: BaseException,
    usable_manifest: bool,
    interrupted: bool,
    network_attempted: bool,
) -> None:
    """Preserve the exact failing stage/type/text and assign an honest status."""

    if interrupted:
        report["status"] = "interrupted"
    elif usable_manifest:
        report["status"] = "partial"
    elif not network_attempted:
        report["status"] = "unattempted"
    else:
        report["status"] = "failed"
    report["stage"] = stage
    report["error"] = exception_payload(exc)
    report["last_observable_action"] = (
        f"{type(exc).__name__} raised during {stage}: {exc}"
    )
    report["retry_observability"] = (
        "not_exposed" if network_attempted else "not_applicable"
    )
    report["network_attempts"] = None
    report["retries_by_stage"] = None


def safety_error(report: dict[str, Any], failures: list[str]) -> None:
    """Override any computational status when primary/config safety is unproven."""

    prior = report.get("error")
    if prior is not None:
        report["warnings"].append(
            f"the transfer action also failed before safety verification: "
            f"{prior['type']}: {prior['message']}"
        )
    exc = TransferSafetyError("; ".join(failures))
    report["status"] = "failed"
    report["stage"] = "safety_verification"
    report["error"] = exception_payload(exc)
    report["last_observable_action"] = (
        f"{type(exc).__name__} raised during safety_verification: {exc}"
    )


def run_transfer(
    *,
    run_id: str | None = None,
    acquire_call: AcquireCall = acquire.main,
    pipeline_call: PipelineCall = pipeline.run,
) -> int:
    """Run one live transfer attempt and always try to leave structured evidence."""

    originals = capture_config_paths()
    saved_project_root = originals["PROJECT_ROOT"].resolve()
    saved_outputs_dir = Path(config.OUTPUTS_DIR).resolve()
    saved_paper_dir = Path(config.PAPER_DIR).resolve()
    selected_run_id = run_id or new_run_id()
    paths = build_paths(
        selected_run_id,
        outputs_dir=saved_outputs_dir,
        paper_dir=saved_paper_dir,
    )
    area = transfer_area()
    try:
        validate_canonical_report_destination(originals, saved_paper_dir)
    except TransferSafetyError as exc:
        print(f"REFUSED UNSAFE TRANSFER CONFIGURATION: {exc}", file=sys.stderr)
        return 1
    report = base_report(paths, area=area, started_at=utc_now())
    report_path = saved_paper_dir / REPORT_NAME
    report["config_paths_restored"] = capture_config_paths() == originals
    report["last_observable_action"] = (
        "current transfer attempt initialized; acquisition boundary not yet invoked"
    )
    marker = dict(report)
    for name in ("work_root", "snapshot_path", "manifest_path", "pipeline_output_path"):
        marker[name] = None
    marker["warnings"] = [
        "noncanonical destinations are omitted until the full write-layout "
        "preflight passes"
    ]
    try:
        write_strict_report(
            marker,
            report_path,
            project_root=saved_project_root,
            work_root=paths.work_root,
        )
    except Exception as exc:
        print(
            f"FAILED TO INITIALIZE {report_path}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    report["last_observable_action"] = (
        "current-attempt marker atomically replaced the prior canonical report"
    )
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    usable_manifest = False
    network_attempted = False
    isolation_entered = False
    pipeline_invoked = False
    pipeline_validated = False
    pipeline_result: pipeline.PipelineResult | None = None
    failed_stage = "initialization"

    try:
        validate_paper_destinations(paths, originals, saved_paper_dir)
        validate_namespace_layout(
            paths,
            originals,
            saved_outputs_dir,
            saved_paper_dir,
        )
        validate_rebound_paths(rebound_values(paths), originals, paths.work_root)
        if not paths.work_root.is_relative_to(saved_project_root):
            raise TransferSafetyError(
                f"transfer work root is outside the saved project root: {paths.work_root}"
            )
        report["last_observable_action"] = "all transfer write destinations validated"

        failed_stage = "artifacts"
        report["stage"] = failed_stage
        report["primary_tradeoff"] = copy_primary_tradeoff(
            saved_outputs_dir / pipeline.TRADEOFF_NAME,
            saved_paper_dir / pipeline.TRADEOFF_NAME,
        )
        report["last_observable_action"] = "primary trade-off copied and verified"

        failed_stage = "initialization"
        report["stage"] = failed_stage
        before = fingerprint_primary(
            snapshot_dir=originals["SNAPSHOT_DIR"],
            manifest_path=originals["MANIFEST_PATH"],
            project_root=saved_project_root,
            area=config.STUDY_AREA,
        )
        report["primary_snapshot_before"] = before
        report["last_observable_action"] = "primary snapshot fingerprinted before transfer"

        with isolated_config(paths, originals):
            isolation_entered = True
            failed_stage = "acquisition"
            report["stage"] = failed_stage
            report["retry_observability"] = "not_exposed"
            network_attempted = True
            call_acquisition(area, acquire_call)
            report["last_observable_action"] = "acquire.main returned zero"

            failed_stage = "manifest"
            report["stage"] = failed_stage
            registry = Registry(area)
            records = registry.load_manifest()
            report["datasets"] = [
                dataset_payload(record, {"validation_completed": False})
                for record in records
            ]
            report["last_observable_action"] = (
                f"transfer manifest loaded with {len(records)} registered dataset(s)"
            )
            registry_evidence = collect_registry_evidence(
                registry,
                records,
                area=area,
                work_root=paths.work_root,
            )
            usable_manifest = True
            report["datasets"] = registry_evidence.datasets
            report["boundary_verification"] = registry_evidence.validation
            report["last_observable_action"] = (
                f"validated {len(records)} registered transfer dataset(s)"
            )

            failed_stage = "pipeline"
            report["stage"] = failed_stage
            pipeline_invocation_log: list[bool] = []
            try:
                pipeline_result = call_pipeline(
                    area,
                    paths.pipeline_output_dir,
                    pipeline_call,
                    pipeline_invocation_log,
                )
            finally:
                pipeline_invoked = bool(pipeline_invocation_log)
            report["last_observable_action"] = "pipeline.run returned a PipelineResult"

            failed_stage = "artifacts"
            report["stage"] = failed_stage
            tract_count = len(registry_evidence.geoids[acquire.DATASET_TRACTS])
            report["alignment"] = dataclasses.asdict(
                pipeline_result.snapshot.report
            )
            pipeline_evidence = pipeline_payload(
                pipeline_result,
                area=area,
                tract_count=tract_count,
                output_dir=paths.pipeline_output_dir,
            )
            pipeline_validated = True
            report["pipeline"] = pipeline_evidence
            report["boundary_verification"]["population_total"] = (
                pipeline_evidence["population_total"]
            )
            report["boundary_verification"]["population_source"] = (
                "RiskTable.exposure.population_total, verified against aligned "
                "PipelineResult tables"
            )
            report["written_files"] = tuple(pipeline_result.written)
            report["warnings"].extend(pipeline_result.warnings)
            report["last_observable_action"] = (
                "pipeline artifacts validated from PipelineResult.written"
            )
            report["status"] = "completed"
    except KeyboardInterrupt as exc:
        record_failure(
            report,
            stage=failed_stage,
            exc=exc,
            usable_manifest=usable_manifest,
            interrupted=True,
            network_attempted=network_attempted,
        )
    except Exception as exc:
        record_failure(
            report,
            stage=failed_stage,
            exc=exc,
            usable_manifest=usable_manifest,
            interrupted=False,
            network_attempted=network_attempted,
        )

    post_attempt_errors: list[tuple[str, BaseException]] = []
    if not usable_manifest and isolation_entered:
        try:
            report["unregistered_partial_files"] = list_attempt_files(
                paths.snapshot_dir
            )
        except (Exception, KeyboardInterrupt) as exc:
            post_attempt_errors.append(("unverified_snapshot_inventory", exc))
    if pipeline_invoked and not pipeline_validated:
        try:
            report["partial_pipeline_files"] = list_attempt_files(
                paths.pipeline_output_dir
            )
        except (Exception, KeyboardInterrupt) as exc:
            post_attempt_errors.append(("partial_pipeline_inventory", exc))

    restored = capture_config_paths() == originals
    report["config_paths_restored"] = restored
    after_error: dict[str, str] | None = None
    try:
        after = fingerprint_primary(
            snapshot_dir=originals["SNAPSHOT_DIR"],
            manifest_path=originals["MANIFEST_PATH"],
            project_root=saved_project_root,
            area=config.STUDY_AREA,
        )
        report["primary_snapshot_after"] = after
    except (Exception, KeyboardInterrupt) as exc:
        after_error = exception_payload(exc)

    unchanged = before is not None and after is not None and before == after
    report["primary_snapshot_unchanged"] = unchanged if before is not None else None
    try:
        git_state = git_data_state(saved_project_root)
    except (Exception, KeyboardInterrupt) as exc:
        post_attempt_errors.append(("git_data_state", exc))
        git_state = {
            "clean": False,
            "collection_error": exception_payload(exc),
        }
    safety = {
        "config_paths_before": originals,
        "config_paths_after": capture_config_paths(),
        "config_paths_restored": restored,
        "primary_registry_reload": after is not None,
        "primary_snapshot_unchanged": unchanged,
        "primary_fingerprint_error": after_error,
        "git_data_state": git_state,
        "post_attempt_errors": [
            {"stage": stage, **exception_payload(exc)}
            for stage, exc in post_attempt_errors
        ],
        "isolated_layout": rebound_values(paths),
    }
    report["safety_verification"] = safety

    safety_failures: list[str] = []
    if not restored:
        safety_failures.append("not all five config paths were restored")
    if before is None and network_attempted:
        safety_failures.append("the primary before-fingerprint is unavailable")
    if after is None:
        safety_failures.append(
            "the primary registry/fingerprint could not be reloaded after transfer"
        )
    if before is not None and after is not None and not unchanged:
        safety_failures.append("the complete primary snapshot fingerprint changed")
    if not git_state["clean"]:
        safety_failures.append("Git reports a change under data/")
    safety_failures.extend(
        f"{stage} failed with {type(exc).__name__}: {exc}"
        for stage, exc in post_attempt_errors
    )
    if safety_failures:
        safety_error(report, safety_failures)
    elif report["status"] == "completed":
        report["stage"] = "complete"
        report["last_observable_action"] = (
            "all five paths restored and the complete primary snapshot matched"
        )

    report["finished_at"] = utc_now()
    try:
        write_strict_report(
            report,
            report_path,
            project_root=saved_project_root,
            work_root=paths.work_root,
        )
    except Exception as exc:
        print(
            f"FAILED TO WRITE {report_path}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        fallback = report_write_fallback(report, paths, area=area, exc=exc)
        try:
            write_strict_report(
                fallback,
                report_path,
                project_root=saved_project_root,
                work_root=paths.work_root,
            )
        except Exception as fallback_exc:
            print(
                "FAILED TO WRITE CURRENT-ATTEMPT FALLBACK "
                f"{report_path}: {type(fallback_exc).__name__}: {fallback_exc}",
                file=sys.stderr,
            )
        return 1

    print(f"transfer status: {report['status']}")
    print(f"stage: {report['stage']}")
    print(f"run id: {paths.run_id}")
    print(f"work root: {paths.work_root}")
    print(f"pipeline output: {paths.pipeline_output_dir}")
    print(f"report: {report_path}")
    print(f"config paths restored: {restored}")
    print(f"primary snapshot unchanged: {report['primary_snapshot_unchanged']}")
    if report["error"]:
        print(f"error: {report['error']['type']}: {report['error']['message']}")
    return 0 if report["status"] == "completed" else 130 if report["status"] == "interrupted" else 1


def enter_and_exit(paths: TransferPaths, originals: dict[str, Path]) -> None:
    """Drive a context entry for refusal checks."""

    with isolated_config(paths, originals):
        pass


def _isolation_checks() -> list[tuple[str, bool]]:
    """Fresh-root, overlap, manifest round-trip, restoration and fingerprint checks."""

    originals = capture_config_paths()
    before = fingerprint_primary(
        snapshot_dir=originals["SNAPSHOT_DIR"],
        manifest_path=originals["MANIFEST_PATH"],
        project_root=originals["PROJECT_ROOT"],
        area=config.STUDY_AREA,
    )
    moved: dict[str, bool] = {}
    manifest_relation = False
    fixture_inside = False
    fixture_round_trip = False
    fixture_provenance = False
    normal_error: Exception | None = None
    exception_restored = False

    with tempfile.TemporaryDirectory(prefix="transfer-offline-check-") as temporary:
        base = Path(temporary)
        paths = build_paths(
            "fresh-isolation",
            outputs_dir=base / "outputs",
            paper_dir=base / "paper",
        )
        try:
            with isolated_config(paths, originals):
                actual = capture_config_paths()
                moved = {
                    name: actual[name].resolve().is_relative_to(paths.work_root)
                    for name in CONFIG_PATHS
                }
                manifest_relation = (
                    actual["MANIFEST_PATH"].resolve()
                    == actual["SNAPSHOT_DIR"].resolve() / MANIFEST_NAME
                )
                fixture = actual["SNAPSHOT_DIR"] / "fixture.csv"
                geoid = f"{transfer_area().county_geoid}001000"
                pd.DataFrame({Col.GEOID: [geoid], Col.POPULATION: [1]}).to_csv(
                    fixture, index=False
                )
                fixture_record = Provenance(
                    dataset="fixture",
                    source_url="https://example.invalid/fixture",
                    retrieved_at=utc_now(),
                    declared_crs="not_applicable",
                    working_crs=transfer_area().working_crs,
                    vintage="offline fixture",
                    feature_count=1,
                    license="test-only",
                    request_params={"mode": "offline"},
                    notes=("fixture manifest round-trip",),
                )
                registry = Registry(transfer_area())
                registered = registry.register("fixture", "table", fixture, fixture_record)
                registry.save_manifest()
                reloaded = Registry(transfer_area())
                records = reloaded.load_manifest()
                fixture_inside = all(
                    record.path.resolve().is_relative_to(paths.work_root)
                    for record in records
                )
                fixture_round_trip = (
                    records == [registered] and len(reloaded.load("fixture")) == 1
                )
                fixture_provenance = (
                    set(provenance_payload(records[0].provenance))
                    == {field.name for field in dataclasses.fields(Provenance)}
                )
        except Exception as exc:
            normal_error = exc

        exception_paths = build_paths(
            "exception-isolation",
            outputs_dir=base / "exception-outputs",
            paper_dir=base / "exception-paper",
        )
        try:
            with isolated_config(exception_paths, originals):
                raise RuntimeError("offline restoration probe")
        except RuntimeError:
            exception_restored = capture_config_paths() == originals
        finally:
            # Test cleanup after a restore mutation: compare first, then protect
            # the remaining checks and the user's process from mutated globals.
            for name in CONFIG_PATHS:
                setattr(config, name, originals[name])

        existing_paths = build_paths(
            "existing-root",
            outputs_dir=base / "existing-outputs",
            paper_dir=base / "existing-paper",
        )
        existing_paths.work_root.mkdir(parents=True)
        existing_refused = verify.refuses(
            lambda: enter_and_exit(existing_paths, originals),
            FileExistsError,
            "will not be reused",
        )

        paper_collision = build_paths(
            "existing-paper",
            outputs_dir=base / "paper-collision-outputs",
            paper_dir=base / "paper-collision-paper",
        )
        paper_collision.pipeline_output_dir.mkdir(parents=True)
        paper_refused = verify.refuses(
            lambda: enter_and_exit(paper_collision, originals),
            FileExistsError,
            "paper run directory already exists",
        )

        unsafe = rebound_values(
            build_paths(
                "unsafe-layout",
                outputs_dir=base / "unsafe-outputs",
                paper_dir=base / "unsafe-paper",
            )
        )
        unsafe["SNAPSHOT_DIR"] = originals["SNAPSHOT_DIR"]
        unsafe_refused = verify.refuses(
            lambda: validate_rebound_paths(
                unsafe,
                originals,
                Path(unsafe["PROJECT_ROOT"]),
            ),
            TransferSafetyError,
            "rebound path layout",
        )

        unsafe_paper_paths = build_paths(
            "unsafe-paper",
            outputs_dir=base / "unsafe-paper-outputs",
            paper_dir=originals["SNAPSHOT_DIR"],
        )
        unsafe_paper_refused = verify.refuses(
            lambda: validate_paper_destinations(
                unsafe_paper_paths,
                originals,
                originals["SNAPSHOT_DIR"],
            ),
            TransferSafetyError,
            "unsafe paper write destination",
        )

        namespace_paths = build_paths(
            "namespace-layout",
            outputs_dir=config.OUTPUTS_DIR,
            paper_dir=config.PAPER_DIR,
        )
        escaped_namespace = dataclasses.replace(
            namespace_paths,
            pipeline_output_dir=(
                originals["PROJECT_ROOT"] / "outputs" / "outside-paper" / "namespace-layout"
            ).resolve(),
        )
        namespace_escape_refused = verify.refuses(
            lambda: validate_namespace_layout(
                escaped_namespace,
                originals,
                config.OUTPUTS_DIR,
                config.PAPER_DIR,
            ),
            TransferSafetyError,
            "exact output namespaces",
        )

    after = fingerprint_primary(
        snapshot_dir=originals["SNAPSHOT_DIR"],
        manifest_path=originals["MANIFEST_PATH"],
        project_root=originals["PROJECT_ROOT"],
        area=config.STUDY_AREA,
    )
    print(f"  five rebound paths under the fresh root: {moved}")
    print(f"  primary files fingerprinted before/after: {len(before['inventory'])}")
    if normal_error is not None:
        print(f"  normal isolation raised unexpectedly: {type(normal_error).__name__}: {normal_error}")
    return [
        ("all five paths move under a fresh isolated work root",
         normal_error is None and len(moved) == len(CONFIG_PATHS) and all(moved.values())),
        ("the manifest is exactly the isolated snapshot plus manifest.json",
         manifest_relation),
        ("a fixture manifest resolves only files inside the isolated root",
         fixture_inside),
        ("the fixture manifest round-trips through Registry",
         fixture_round_trip),
        ("the fixture preserves every frozen provenance field",
         fixture_provenance),
        ("an exception restores all five original config values",
         exception_restored),
        ("an existing work root is refused rather than reused",
         existing_refused),
        ("an existing paper run directory is refused rather than reused",
         paper_refused),
        ("a target equal to the primary snapshot is specifically refused",
         unsafe_refused),
        ("a paper destination inside primary storage is specifically refused",
         unsafe_paper_refused),
        ("a run-specific output escaping the exact paper namespace is refused",
         namespace_escape_refused),
        ("the complete primary snapshot fingerprint survives an isolated exception",
         before == after),
    ]


def _copy_checks() -> list[tuple[str, bool]]:
    """The production copier preserves bytes, hashes, columns and rows."""

    payload = b"preset,scenario,top_geoids,displaced_geoids\r\na,b,one,two\r\n"
    with tempfile.TemporaryDirectory(prefix="transfer-copy-check-") as temporary:
        root = Path(temporary)
        source = root / "source.csv"
        destination = root / "paper" / "tradeoff.csv"
        source.write_bytes(payload)
        evidence = copy_primary_tradeoff(source, destination)
        copied = destination.read_bytes()
    print(
        f"  copied {evidence['row_count']} row(s), {evidence['byte_count']} byte(s), "
        f"sha256 {evidence['source_sha256']}"
    )
    return [
        ("the primary trade-off copier preserves exact bytes", copied == payload),
        ("source and paper copy have the same SHA-256",
         evidence["source_sha256"] == evidence["destination_sha256"]),
        ("the copier verifies column order",
         evidence["columns"] == ("preset", "scenario", "top_geoids", "displaced_geoids")
         and evidence["columns_equal"]),
        ("the copier verifies every parsed row", evidence["rows_equal"]),
    ]


def _serialisation_checks() -> list[tuple[str, bool]]:
    """Frozen provenance and non-finite/path conversions in strict JSON."""

    with tempfile.TemporaryDirectory(prefix="transfer-json-check-") as temporary:
        root = Path(temporary).resolve()
        dataset = root / "work" / "fixture.csv"
        dataset.parent.mkdir(parents=True)
        dataset.write_text(f"{Col.GEOID}\n", encoding="utf-8")
        record = DatasetRecord(
            name="fixture",
            kind="table",
            path=dataset,
            provenance=Provenance(
                dataset="fixture",
                source_url="https://example.invalid/source",
                retrieved_at=utc_now(),
                declared_crs="not_applicable",
                working_crs=transfer_area().working_crs,
                vintage="offline fixture",
                feature_count=0,
                license="test-only",
                request_params={"requested": "yes"},
                notes=("one", "two"),
            ),
        )
        report = {
            "dataset": dataset_payload(record, {"feature_count_matches": True}),
            "alignment": dataclasses.asdict(AlignmentReport()),
            "missing": float("nan"),
            "infinite": float("inf"),
            "pandas_missing": pd.NA,
            "pandas_time_missing": pd.NaT,
            "tuple": (1, 2),
            "scalar": pd.Series([3], dtype="Int64").iloc[0],
            "embedded_path_error": f"failed at {dataset.resolve()}",
            "warnings": [],
        }
        destination = root / "report.json"
        destination.write_text('{"run_id": "stale"}\n', encoding="utf-8")
        write_strict_report(
            report,
            destination,
            project_root=root,
            work_root=root / "work",
        )
        text = destination.read_text(encoding="utf-8")
        restored = json.loads(text)
        temporary_files = tuple(root.glob(".report.json.*.tmp"))

    provenance_fields = {field.name for field in dataclasses.fields(Provenance)}
    serialized_fields = set(restored["dataset"]["provenance"])
    print(f"  strict JSON provenance fields: {sorted(serialized_fields)}")
    return [
        ("every frozen Provenance field is present", serialized_fields == provenance_fields),
        ("timezone-aware datetimes are ISO strings",
         "+00:00" in restored["dataset"]["provenance"]["retrieved_at"]),
        ("tuples become JSON arrays", restored["tuple"] == [1, 2]),
        ("pandas scalars become ordinary JSON scalars", restored["scalar"] == 3),
        ("Pandas missing scalars become JSON null",
         restored["pandas_missing"] is None
         and restored["pandas_time_missing"] is None),
        ("non-finite values become null",
         restored["missing"] is None and restored["infinite"] is None),
        ("every non-finite conversion adds a warning",
         len([item for item in restored["warnings"] if "non-finite" in item]) == 2),
        ("strict JSON contains no NaN or Infinity token",
         "NaN" not in text and "Infinity" not in text),
        ("the report atomically replaces stale canonical content",
         "stale" not in text and not temporary_files),
        ("absolute paths embedded in diagnostic strings become portable",
         restored["embedded_path_error"] == "failed at ./work/fixture.csv"
         and any(
             "filesystem path text normalized" in warning
             for warning in restored["warnings"]
         )),
        ("portable JSON contains no Windows absolute path",
         re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[/\\\\]", text) is None),
    ]


def _control_flow_checks() -> list[tuple[str, bool]]:
    """Configured-area dispatch and exact failure-report semantics, offline."""

    seen_acquisition: list[config.StudyArea] = []
    seen_pipeline: list[config.StudyArea] = []

    def acquisition_recorder(area: config.StudyArea) -> int:
        seen_acquisition.append(area)
        return 0

    def pipeline_recorder(
        *, area: config.StudyArea, outputs: Path
    ) -> pipeline.PipelineResult:
        seen_pipeline.append(area)
        raise RuntimeError(f"offline pipeline recorder at {outputs.name}")

    selected = transfer_area()
    call_acquisition(selected, acquisition_recorder)
    try:
        call_pipeline(selected, Path(tempfile.gettempdir()) / uuid.uuid4().hex, pipeline_recorder)
    except RuntimeError:
        pass

    paths = build_paths(
        "failure-report",
        outputs_dir=Path(tempfile.gettempdir()) / uuid.uuid4().hex,
        paper_dir=Path(tempfile.gettempdir()) / uuid.uuid4().hex,
    )
    failure = base_report(paths, area=selected, started_at=utc_now())
    expected = RuntimeError("exact offline failure text")
    record_failure(
        failure,
        stage="acquisition",
        exc=expected,
        usable_manifest=False,
        interrupted=False,
        network_attempted=True,
    )
    unattempted = base_report(paths, area=selected, started_at=utc_now())
    record_failure(
        unattempted,
        stage="initialization",
        exc=RuntimeError("offline preflight refusal"),
        usable_manifest=False,
        interrupted=False,
        network_attempted=False,
    )
    print(
        f"  dispatch identities: acquire={seen_acquisition}, pipeline={seen_pipeline}; "
        f"failure={failure['error']}"
    )
    return [
        ("the production area selector returns config.TRANSFER_AREA",
         selected is config.TRANSFER_AREA),
        ("the acquisition boundary receives the configured transfer object once",
         seen_acquisition == [config.TRANSFER_AREA]),
        ("the pipeline boundary receives the same configured transfer object once",
         seen_pipeline == [config.TRANSFER_AREA]),
        ("a failure without a usable manifest is not declared completed",
         failure["status"] == "failed"),
        ("a failure report preserves the exact stage",
         failure["stage"] == "acquisition"),
        ("a failure report preserves exact exception type and text",
         failure["error"]["type"] == "RuntimeError"
         and failure["error"]["message"] == "exact offline failure text"
         and failure["error"]["message_sha256"]
         == hashlib.sha256(b"exact offline failure text").hexdigest()),
        ("unknown attempts and retries stay null rather than becoming zero",
         failure["retry_observability"] == "not_exposed"
         and failure["network_attempts"] is None
         and failure["retries_by_stage"] is None),
        ("a preflight refusal before the acquisition boundary is unattempted",
         unattempted["status"] == "unattempted"
         and unattempted["retry_observability"] == "not_applicable"),
        ("the base report exposes every field S14 requires",
         REQUIRED_REPORT_FIELDS <= set(failure)),
    ]


def _self_check() -> int:
    """Fully offline isolation, copier, serializer and dispatch checks."""

    print("TRANSFER -- offline isolation and artifact checks\n")
    checks = _isolation_checks()
    print()
    checks += _copy_checks()
    print()
    checks += _serialisation_checks()
    print()
    checks += _control_flow_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])
    return verify.report(checks)


def main() -> int:
    """Run the one real configured transfer attempt."""

    return run_transfer()


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
