"""Build the paper's traceable numbers file from completed artifacts only.

This module is deliberately downstream of every analysis.  It reads manifests,
tables, experiment reports, and source constants; it never acquires data, calls
the model, or invokes the pipeline.  Every paper-facing value carries the file,
selector, and derivation that produced it, while ``sources`` fingerprints every
file used so a stale numbers file is detectable rather than merely plausible.

The production command writes ``outputs/paper/numbers.json`` atomically.  The
``--check`` command constructs a complete offline fixture in a temporary
directory, exercises both the successful path and the refusal paths, and never
touches the production artifact.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeAlias

import rasterio

from .. import agent, config, contracts, hazard, risk, sandbox, verify, vulnerability

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
Metric: TypeAlias = dict[str, JsonValue]

SCHEMA_VERSION = 1
OUTPUT_RELATIVE = Path("outputs/paper/numbers.json")
PAPER_RELATIVE = Path("paper/main.tex")
MANIFEST_RELATIVE = Path("data/snapshot/manifest.json")
ELEVATION_RELATIVE = Path("data/snapshot/3depelevation.tif")
PIPELINE_REPORT_RELATIVE = Path("outputs/pipeline_report.txt")
PRIMARY_TRADEOFF_RELATIVE = Path("outputs/tradeoff.csv")
PAPER_TRADEOFF_RELATIVE = Path("outputs/paper/tradeoff.csv")
TRANSFER_REPORT_RELATIVE = Path("outputs/paper/transfer_report.json")
FAULTS_RELATIVE = Path("outputs/faults.json")
ATTEMPTS_RELATIVE = Path("outputs/paper/transfer_attempts")
TRANSFER_RELATIVE = Path("outputs/paper/transfer")

RISK_COLUMNS = (
    contracts.Col.GEOID,
    contracts.Col.POPULATION,
    contracts.Col.PCT_POVERTY,
    contracts.Col.PCT_AGE_65_PLUS,
    contracts.Col.PCT_DISABILITY,
    contracts.Col.PCT_NO_VEHICLE,
    contracts.Col.PCT_LIMITED_ENGLISH,
    contracts.Col.INUNDATED_FRACTION,
    contracts.Col.INUNDATION_MEAN_M,
    contracts.Col.INUNDATION_MAX_M,
    contracts.Col.EXPOSED_POPULATION,
    contracts.Col.VULNERABILITY,
    contracts.Col.RESILIENCE,
    contracts.Col.RISK_SCORE,
    contracts.Col.PRIORITY_RANK,
)
TRADEOFF_COLUMNS = (
    "preset",
    "scenario",
    "population_in_priority",
    "vulnerable_population_in_priority",
    "mean_inundation_m",
    "top_geoids",
    "displaced_geoids",
)
REQUIRED_DATASETS = (
    "acs",
    "acs_block_groups",
    "block_groups",
    "elevation",
    "facilities",
    "flood_zones",
    "tracts",
)
METRIC_FIELDS = frozenset({"value", "unit", "source", "selector", "derivation"})
SEMANTIC_KEY = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
PLACEHOLDER = re.compile(r"\\TD\{([^{}]+)\}")
RUN_ID = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")
WKT_TEXT = re.compile(
    r"\b(?:POINT|LINESTRING|LINEARRING|POLYGON|MULTIPOINT|MULTILINESTRING"
    r"|MULTIPOLYGON|GEOMETRYCOLLECTION)\s*[ZM]{0,2}\s*\(",
    re.IGNORECASE,
)
ABSOLUTE_WINDOWS = re.compile(r"^[A-Za-z]:[\\/]")
FORBIDDEN_SPATIAL_KEYS = frozenset(
    {
        "geometry",
        "geometries",
        "coordinate",
        "coordinates",
        "latitude",
        "longitude",
        "lat",
        "lon",
        "lng",
        "bbox",
        "bounds",
        "centroid",
        "x",
        "y",
    }
)
PROSE_TODO = re.compile(
    r"\b(?:TODO|TBD)\b|\[(?:[^\]]*(?:insert|name|email|institution|department|"
    r"city|state|country|conference|date|location|acknowledg|placeholder)[^\]]*)\]"
    r"|\bConference Name\b|\\acm(?:DOI|ISBN)\{\}"
    r"|^(?:Describe|Report|Compare)\b.*\bhere\.",
    re.IGNORECASE,
)

MODULE_SOURCES: dict[str, str] = {
    "contracts": "src/contracts.py",
    "config": "src/config.py",
    "hazard": "src/hazard.py",
    "vulnerability": "src/vulnerability.py",
    "risk": "src/risk.py",
    "agent": "src/agent.py",
    "sandbox": "src/sandbox.py",
}

ALIGNMENT_REPORT_LABELS: dict[str, str] = {
    "sentinel_count": "sentinels_removed",
    "apportionment_error_pct": "apportionment_error",
}

AGENT_METRICS = (
    "question_count",
    "llm_calls",
    "gis_tool_calls",
    "generated_code_calls",
    "successful_code_runs",
    "failed_code_runs",
    "error_taxonomy",
    "repair_attempts",
    "repair_successes",
    "critic_cycles",
    "critic_firings",
    "revisions_requested",
    "first_draft_pass_rate",
    "final_draft_pass_rate",
    "numbers_checked",
    "numbers_traceable",
    "mean_turns",
    "mean_duration",
)

EXPECTED_PAPER_KEYS = (
    "abstract.primary_tract_count",
    "abstract.scenario_count",
    "abstract.preset_count",
    "abstract.transfer_tract_count",
    "abstract.transfer_scored_count",
    "methods.tool_count",
    "methods.agent_iteration_bound",
    "methods.critic_revision_bound",
    "methods.sandbox_timeout_s",
    "methods.retry_bound",
    "alignment.working_epsg",
    "alignment.primary_raster_resolution_m",
    "alignment.primary_sentinel_count",
    "results.primary_population",
    "results.primary_tract_count",
    "results.primary_scored_count",
    "results.transfer_population",
    "results.transfer_tract_count",
    "results.transfer_scored_count",
    "results.transfer_unscored_count",
    "results.transfer_facility_count",
    "results.transfer_raster_width",
    "results.transfer_raster_height",
    "results.transfer_raster_resolution_m",
    "robustness.fixture_runs_per_cell",
    "robustness.rate_count",
    "robustness.fault_kind_count",
    "limitations.county_count",
)


class EvidenceError(ValueError):
    """An input artifact is absent, malformed, stale, or mutually inconsistent."""


def _utc_now() -> str:
    """Return a timezone-aware generation timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    """Hash a file without interpreting its contents."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EvidenceError(f"missing source file: {path}") from exc
    return digest.hexdigest()


def _fingerprint(path: Path) -> tuple[str, int]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"missing source file: {path}") from exc
    if b"\0" not in content[:8000]:
        content = content.replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest(), len(content)


def _portable(path: Path, root: Path) -> str:
    """Return a forward-slash repository path and refuse paths outside the root."""
    base = root.resolve()
    target = path.resolve()
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise EvidenceError(f"absolute path leakage outside repository: {target}") from exc
    return relative.as_posix()


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook that rejects duplicate keys instead of keeping the last."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate key in JSON: {key}")
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    """Reject the non-JSON NaN and Infinity tokens accepted by Python's parser."""
    raise EvidenceError(f"NaN/Infinity is not valid evidence: {token}")


def _load_json(path: Path) -> dict[str, Any]:
    """Load a strict JSON object with useful evidence errors."""
    if not path.is_file():
        raise EvidenceError(f"missing source file: {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except EvidenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"malformed JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise EvidenceError(f"malformed JSON object: {path}")
    return payload


def _source(
    sources: dict[str, JsonValue], path: Path, root: Path, role: str
) -> str:
    """Register one authoritative source and return its portable path."""
    relative = _portable(path, root)
    if relative not in sources:
        if not path.is_file():
            raise EvidenceError(f"missing source file: {relative}")
        digest, size = _fingerprint(path)
        sources[relative] = {
            "sha256": digest,
            "bytes": size,
            "role": role,
        }
    return relative


def _metric(
    value: JsonValue,
    unit: str,
    source: str,
    selector: str,
    derivation: str,
) -> Metric:
    """Construct one fully traceable metric record."""
    _finite(value, "metric value")
    if not all((unit, source, selector, derivation)):
        raise EvidenceError("paper key with no evidence")
    return {
        "value": value,
        "unit": unit,
        "source": source,
        "selector": selector,
        "derivation": derivation,
    }


def _finite(value: JsonValue, where: str) -> None:
    """Recursively reject non-finite floating-point values."""
    if isinstance(value, float) and not math.isfinite(value):
        raise EvidenceError(f"NaN/Infinity in {where}")
    if isinstance(value, list):
        for item in value:
            _finite(item, where)
    elif isinstance(value, dict):
        for item in value.values():
            _finite(item, where)


def _number(value: Any, label: str) -> float:
    """Parse one required finite number without turning absence into zero."""
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise EvidenceError(f"missing numeric evidence: {label}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"malformed numeric evidence: {label}") from exc
    if not math.isfinite(result):
        raise EvidenceError(f"NaN/Infinity in {label}")
    return result


def _integer(value: Any, label: str) -> int:
    """Parse one non-negative integer represented either as text or a number."""
    number = _number(value, label)
    if number < 0 or not number.is_integer():
        raise EvidenceError(f"expected non-negative integer: {label}")
    return int(number)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    """Require a mapping at an evidence selector."""
    if not isinstance(value, dict):
        raise EvidenceError(f"malformed mapping: {label}")
    return value


def _sequence(value: Any, label: str) -> list[Any]:
    """Require a list at an evidence selector."""
    if not isinstance(value, list):
        raise EvidenceError(f"malformed list: {label}")
    return value


def _dataset_map(manifest: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    """Index a manifest's datasets while validating complete provenance."""
    rows = _sequence(manifest.get("datasets"), f"{label}.datasets")
    indexed: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        row = _mapping(raw, f"{label}.datasets[{index}]")
        name = str(row.get("name", ""))
        if not name or name in indexed:
            raise EvidenceError(f"missing or duplicate dataset: {name!r}")
        provenance = _mapping(row.get("provenance"), f"{label}.{name}.provenance")
        required = (
            "dataset",
            "source_url",
            "retrieved_at",
            "declared_crs",
            "working_crs",
            "vintage",
            "feature_count",
            "license",
            "request_params",
            "notes",
        )
        missing = [key for key in required if key not in provenance]
        if missing:
            raise EvidenceError(f"missing dataset provenance for {name}: {missing}")
        if provenance.get("dataset") != name:
            raise EvidenceError(f"dataset provenance name mismatch: {name}")
        _integer(provenance.get("feature_count"), f"{label}.{name}.feature_count")
        _mapping(provenance.get("request_params"), f"{label}.{name}.request_params")
        _sequence(provenance.get("notes"), f"{label}.{name}.notes")
        indexed[name] = row
    if set(indexed) != set(REQUIRED_DATASETS):
        raise EvidenceError(
            f"missing dataset provenance: expected {list(REQUIRED_DATASETS)}, "
            f"observed {sorted(indexed)}"
        )
    return indexed


def _request_area_matches(dataset: dict[str, Any], area: config.StudyArea) -> bool:
    """Validate the requested administrative area without using a literal county."""
    name = str(dataset.get("name", ""))
    provenance = _mapping(dataset.get("provenance"), f"{name}.provenance")
    params = _mapping(provenance.get("request_params"), f"{name}.request_params")
    if name in {"acs", "acs_block_groups"}:
        request = str(params.get("in", ""))
        return f"state:{area.state_fips}" in request and f"county:{area.county_fips}" in request
    if name in {"tracts", "block_groups"}:
        request = str(params.get("where", ""))
        return (
            f"STATE='{area.state_fips}'" in request
            and f"COUNTY='{area.county_fips}'" in request
        )
    return True


def _csv_rows(path: Path, expected: tuple[str, ...]) -> list[dict[str, str]]:
    """Read a UTF-8 CSV and require its exact, non-duplicated schema."""
    if not path.is_file():
        raise EvidenceError(f"missing source file: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = tuple(reader.fieldnames or ())
            if len(fields) != len(set(fields)):
                raise EvidenceError(f"duplicate key in CSV header: {path}")
            if fields != expected:
                raise EvidenceError(
                    f"wrong CSV columns for {path.name}: expected {expected}, observed {fields}"
                )
            rows = list(reader)
    except EvidenceError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise EvidenceError(f"malformed CSV: {path}") from exc
    return rows


def _geoid(value: Any, prefix: str, width: int, label: str) -> str:
    """Validate a GEOID as an identifier, never as a numeric coordinate."""
    result = str(value or "")
    if len(result) != width or not result.isdigit() or not result.startswith(prefix):
        raise EvidenceError(f"wrong GEOID prefix or width in {label}: {result!r}")
    return result


def _risk_table(
    path: Path,
    area: config.StudyArea,
    expected_rows: int,
    scenario: str,
    preset: str,
) -> dict[str, Any]:
    """Validate and summarize one already-produced risk table."""
    expected_name = f"risk_{scenario}_{preset}.csv"
    if path.name != expected_name:
        raise EvidenceError(
            f"risk filename does not match scenario/preset: {path.name} != {expected_name}"
        )
    rows = _csv_rows(path, RISK_COLUMNS)
    if len(rows) != expected_rows:
        raise EvidenceError(
            f"wrong pipeline row count for {path.name}: {len(rows)} != {expected_rows}"
        )
    geoids: list[str] = []
    population_total = 0
    exposed_total = 0.0
    scored: list[str] = []
    unscored: list[str] = []
    ranks: list[int] = []
    for index, row in enumerate(rows):
        geoid = _geoid(row.get(contracts.Col.GEOID), area.county_geoid, 11, path.name)
        geoids.append(geoid)
        population = _integer(row.get(contracts.Col.POPULATION), f"{path.name}[{index}].population")
        exposed = _number(
            row.get(contracts.Col.EXPOSED_POPULATION),
            f"{path.name}[{index}].exposed_population",
        )
        if exposed < 0 or exposed > population + 1e-6:
            raise EvidenceError(f"exposed population outside tract population in {path.name}")
        population_total += population
        exposed_total += exposed
        score = str(row.get(contracts.Col.RISK_SCORE, "")).strip()
        rank = str(row.get(contracts.Col.PRIORITY_RANK, "")).strip()
        if bool(score) != bool(rank):
            raise EvidenceError(f"risk score and priority rank nullness disagree in {path.name}")
        if score:
            _number(score, f"{path.name}[{index}].risk_score")
            ranks.append(_integer(rank, f"{path.name}[{index}].priority_rank"))
            scored.append(geoid)
        else:
            unscored.append(geoid)
    if len(set(geoids)) != len(geoids):
        raise EvidenceError(f"duplicate GEOID in risk table: {path.name}")
    if ranks and (min(ranks) != 1 or max(ranks) > len(scored)):
        raise EvidenceError(f"priority ranks are outside the scored population in {path.name}")
    return {
        "row_count": len(rows),
        "geoids": geoids,
        "population_total": population_total,
        "exposed_total": exposed_total,
        "scored_count": len(scored),
        "unscored_count": len(unscored),
        "unscored_geoids": unscored,
    }


def _geoid_words(value: Any, area: config.StudyArea, label: str) -> list[str]:
    """Parse the space-delimited GEOID cells written by the pipeline."""
    text = str(value or "").strip()
    if not text:
        return []
    return [_geoid(item, area.county_geoid, 11, label) for item in text.split()]


def _tradeoff_table(
    path: Path,
    area: config.StudyArea,
    scenarios: tuple[str, ...],
    presets: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Validate the discrete scenario-by-preset trade-off table."""
    rows = _csv_rows(path, TRADEOFF_COLUMNS)
    expected_pairs = {(scenario, preset) for scenario in scenarios for preset in presets}
    observed_pairs: set[tuple[str, str]] = set()
    parsed: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        scenario = str(row.get("scenario", ""))
        preset = str(row.get("preset", ""))
        pair = (scenario, preset)
        if pair not in expected_pairs or pair in observed_pairs:
            raise EvidenceError(f"missing scenario/preset or duplicate trade-off pair: {pair}")
        observed_pairs.add(pair)
        top = _geoid_words(row.get("top_geoids"), area, f"{path.name}[{index}].top_geoids")
        displaced = _geoid_words(
            row.get("displaced_geoids"), area, f"{path.name}[{index}].displaced_geoids"
        )
        if len(top) != risk.DEFAULT_PRIORITY_UNITS or len(set(top)) != len(top):
            raise EvidenceError(f"malformed priority GEOID set in {path.name}")
        if set(top) & set(displaced):
            raise EvidenceError(f"displaced GEOIDs overlap the selected set in {path.name}")
        parsed.append(
            {
                "scenario": scenario,
                "preset": preset,
                "population_in_priority": _integer(
                    row.get("population_in_priority"), f"{path.name}[{index}].population"
                ),
                "vulnerable_population_in_priority": _integer(
                    row.get("vulnerable_population_in_priority"),
                    f"{path.name}[{index}].vulnerable_population",
                ),
                "mean_inundation_m": _number(
                    row.get("mean_inundation_m"), f"{path.name}[{index}].mean_inundation"
                ),
                "top_geoids": top,
                "displaced_geoids": displaced,
            }
        )
    if observed_pairs != expected_pairs:
        raise EvidenceError(
            f"incorrect trade-off row count or missing combination: "
            f"{len(rows)} != {len(expected_pairs)}"
        )
    for scenario in scenarios:
        group = [row for row in parsed if row["scenario"] == scenario]
        union = set().union(*(set(row["top_geoids"]) for row in group))
        for row in group:
            if set(row["displaced_geoids"]) != union - set(row["top_geoids"]):
                raise EvidenceError(f"displaced GEOIDs do not match discrete presets in {path.name}")
    return parsed


def _extract_int(text: str, label: str) -> int:
    """Extract one labeled integer from the primary pipeline report."""
    match = re.search(rf"^\s{{2}}{re.escape(label)}\s+(\d+)\b", text, re.MULTILINE)
    if not match:
        raise EvidenceError(f"missing pipeline report selector: {label}")
    return int(match.group(1))


def _alignment_values(text: str) -> dict[str, JsonValue]:
    """Select alignment counters from the existing human-readable report."""
    sentinel = re.search(
        r"^\s{2}sentinels_removed\s+\{[^\n]*\}\s+(\d+)\s+negative value",
        text,
        re.MULTILINE,
    )
    error = re.search(
        r"^\s{2}apportionment_error\s+\{'population':\s*([-+0-9.eE]+)\}",
        text,
        re.MULTILINE,
    )
    crs = re.search(r"^\s{2}working crs\s+(EPSG:\d+)\b", text, re.MULTILINE)
    if not sentinel:
        raise EvidenceError("missing pipeline report selector: sentinels_removed")
    if not error:
        raise EvidenceError("missing pipeline report selector: apportionment_error")
    if not crs:
        raise EvidenceError("missing pipeline report selector: working crs")
    return {
        "working_crs": crs.group(1),
        "geometries_repaired": _extract_int(text, "geometries_repaired"),
        "geometries_dropped": _extract_int(text, "geometries_dropped"),
        "sentinel_count": int(sentinel.group(1)),
        "unmatched_left": _extract_int(text, "unmatched_left"),
        "unmatched_right": _extract_int(text, "unmatched_right"),
        "units_below_cell_threshold": _extract_int(text, "units_below_cell_threshold"),
        "apportionment_error_pct": _number(error.group(1), "apportionment_error"),
    }


def _raster_values(path: Path, manifest_dataset: dict[str, Any]) -> dict[str, JsonValue]:
    """Read only raster metadata and cross-check it against recorded provenance."""
    provenance = _mapping(manifest_dataset.get("provenance"), "elevation.provenance")
    params = _mapping(provenance.get("request_params"), "elevation.request_params")
    try:
        requested = _number(params.get("cell_size_m_requested"), "requested raster resolution")
        recorded = _number(params.get("cell_size_m_effective"), "effective raster resolution")
        size = tuple(int(item) for item in str(params.get("size", "")).split(","))
    except ValueError as exc:
        raise EvidenceError("malformed raster size provenance") from exc
    if len(size) != 2:
        raise EvidenceError("malformed raster size provenance")
    try:
        with rasterio.open(path) as dataset:
            actual = float(abs(dataset.res[0]))
            if str(dataset.crs) != str(provenance.get("working_crs")):
                raise EvidenceError("raster CRS disagrees with provenance")
            if dataset.width != size[0] or dataset.height != size[1]:
                raise EvidenceError("raster dimensions disagree with provenance")
            if not math.isclose(abs(dataset.res[0]), abs(dataset.res[1]), rel_tol=0, abs_tol=1e-6):
                raise EvidenceError("raster cells are unexpectedly non-square")
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError(f"cannot read raster metadata: {path}") from exc
    if not math.isclose(actual, recorded, rel_tol=0, abs_tol=1e-3):
        raise EvidenceError("effective raster resolution disagrees with raster")
    return {
        "crs": str(provenance.get("working_crs")),
        "requested_resolution_m": requested,
        "effective_resolution_m": actual,
        "width": size[0],
        "height": size[1],
    }


def _area_payload(
    area: config.StudyArea,
    source: str,
    population: int,
    tract_count: int,
    block_group_count: int,
    facility_count: int,
    population_selector: str,
    count_selector: str,
) -> dict[str, JsonValue]:
    """Create traceable study-area identity and count metrics."""
    return {
        "name": _metric(
            area.name,
            "name",
            source,
            "StudyArea.name",
            "read from the configured study-area value",
        ),
        "state_fips": _metric(
            area.state_fips,
            "FIPS code",
            source,
            "StudyArea.state_fips",
            "read as an identifier from the configured study-area value",
        ),
        "county_fips": _metric(
            area.county_fips,
            "FIPS code",
            source,
            "StudyArea.county_fips",
            "read as an identifier from the configured study-area value",
        ),
        "population": _metric(
            population,
            "people",
            source if population_selector.startswith("StudyArea") else population_selector.split(" | ", 1)[0],
            population_selector.split(" | ", 1)[-1],
            "sum of the validated tract population column; repeated scenario tables agree",
        ),
        "tract_count": _metric(
            tract_count,
            "tracts",
            count_selector.split(" | ", 1)[0],
            count_selector.split(" | ", 1)[-1],
            "validated boundary/attribute count, equal to every risk-table row count",
        ),
        "block_group_count": _metric(
            block_group_count,
            "block groups",
            count_selector.split(" | ", 1)[0],
            count_selector.split(" | ", 1)[-1].replace("tracts", "block_groups"),
            "validated boundary/attribute count",
        ),
        "facility_count": _metric(
            facility_count,
            "facilities",
            count_selector.split(" | ", 1)[0],
            count_selector.split(" | ", 1)[-1].replace("tracts", "facilities"),
            "registered facility feature count",
        ),
    }


def _safe_transfer_directory(root: Path, report: dict[str, Any]) -> tuple[str, Path]:
    """Resolve the canonical transfer output without trusting an arbitrary path."""
    run_id = str(report.get("run_id", ""))
    if not RUN_ID.fullmatch(run_id):
        raise EvidenceError(f"wrong transfer run ID: {run_id!r}")
    expected_relative = (TRANSFER_RELATIVE / run_id).as_posix()
    reported = str(report.get("pipeline_output_path", "")).replace("\\", "/")
    if reported != expected_relative:
        raise EvidenceError(
            f"wrong transfer run ID or output path: {reported!r} != {expected_relative!r}"
        )
    directory = (root / TRANSFER_RELATIVE / run_id).resolve()
    try:
        directory.relative_to((root / TRANSFER_RELATIVE).resolve())
    except ValueError as exc:
        raise EvidenceError("transfer output escapes its run namespace") from exc
    return run_id, directory


def _failed_attempt(root: Path, area: config.StudyArea) -> tuple[Path, dict[str, Any]]:
    """Select the earliest preserved failed attempt for the configured transfer area."""
    directory = root / ATTEMPTS_RELATIVE
    if not directory.is_dir():
        raise EvidenceError(f"missing source file: {directory}")
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        payload = _load_json(path)
        study = payload.get("study_area")
        if not isinstance(study, dict):
            continue
        if (
            payload.get("status") == "failed"
            and study.get("state_fips") == area.state_fips
            and study.get("county_fips") == area.county_fips
        ):
            candidates.append((str(payload.get("started_at", "")), path, payload))
    if not candidates:
        raise EvidenceError("missing preserved failed transfer attempt")
    _, path, payload = min(candidates, key=lambda item: (item[0], item[1].name))
    run_id = str(payload.get("run_id", ""))
    if not RUN_ID.fullmatch(run_id):
        raise EvidenceError(f"wrong transfer run ID: {run_id!r}")
    if payload.get("stage") in (None, "complete") or not isinstance(payload.get("error"), dict):
        raise EvidenceError("malformed preserved failed transfer attempt")
    return path, payload


def _validate_transfer_report(
    report: dict[str, Any],
    area: config.StudyArea,
    scenarios: tuple[str, ...],
    presets: tuple[str, ...],
) -> dict[str, Any]:
    """Validate the completed transfer's safety, boundaries, and analysis summary."""
    if report.get("status") != "completed" or report.get("stage") != "complete":
        raise EvidenceError("transfer not completed")
    study = _mapping(report.get("study_area"), "transfer.study_area")
    expected_study = {
        "name": area.name,
        "state_fips": area.state_fips,
        "county_fips": area.county_fips,
        "working_crs": area.working_crs,
    }
    if study != expected_study:
        raise EvidenceError("wrong transfer study area")
    safety = _mapping(report.get("safety_verification"), "transfer.safety_verification")
    if safety.get("primary_snapshot_unchanged") is not True:
        raise EvidenceError("primary snapshot not unchanged")
    if safety.get("config_paths_restored") is not True:
        raise EvidenceError("configuration not restored")
    if report.get("primary_snapshot_unchanged") is not True:
        raise EvidenceError("primary snapshot not unchanged")
    if report.get("config_paths_restored") is not True:
        raise EvidenceError("configuration not restored")
    boundary = _mapping(report.get("boundary_verification"), "transfer.boundary_verification")
    if boundary.get("geoid_prefix") != area.county_geoid:
        raise EvidenceError("wrong GEOID prefix in transfer boundary evidence")
    if not all(
        boundary.get(key) is True
        for key in (
            "all_geoids_match_prefix",
            "all_request_area_checks_pass",
            "all_required_datasets_registered",
        )
    ):
        raise EvidenceError("transfer boundary validation failed")
    datasets_list = _sequence(report.get("datasets"), "transfer.datasets")
    transfer_manifest = {"datasets": datasets_list}
    datasets = _dataset_map(transfer_manifest, "transfer")
    for name in ("acs", "acs_block_groups", "block_groups", "tracts"):
        validation = _mapping(datasets[name].get("validation"), f"transfer.{name}.validation")
        if validation.get("request_area_matches") is not True:
            raise EvidenceError("wrong transfer request area")
    pipeline = _mapping(report.get("pipeline"), "transfer.pipeline")
    if pipeline.get("study_area_matches_requested") is not True:
        raise EvidenceError("wrong transfer study area in pipeline")
    if tuple(pipeline.get("expected_scenarios", ())) != scenarios:
        raise EvidenceError("missing scenario in transfer report")
    if tuple(pipeline.get("scenarios_completed", ())) != scenarios:
        raise EvidenceError("missing scenario in completed transfer")
    if tuple(pipeline.get("expected_presets", ())) != presets:
        raise EvidenceError("missing preset in transfer report")
    artifacts = _mapping(pipeline.get("artifact_validation"), "transfer.artifact_validation")
    expected_names = {
        "pipeline_report.txt",
        *(f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv" for scenario in scenarios),
        "tradeoff.csv",
    }
    if set(artifacts.get("expected_names", ())) != expected_names:
        raise EvidenceError("transfer artifact expectation mismatch")
    if set(artifacts.get("observed_names", ())) != expected_names:
        raise EvidenceError("transfer artifact validation failed")
    if artifacts.get("pipeline_report_nonempty") is not True:
        raise EvidenceError("transfer pipeline report is empty")
    if artifacts.get("table_files_match_pipeline_result") is not True:
        raise EvidenceError("transfer risk-table artifact validation failed")
    if artifacts.get("tradeoff_matches_pipeline_result") is not True:
        raise EvidenceError("transfer trade-off artifact validation failed")
    if _integer(artifacts.get("tradeoff_row_count"), "transfer trade-off row count") != len(
        scenarios
    ) * len(presets):
        raise EvidenceError("incorrect trade-off row count in transfer report")
    return {
        "datasets": datasets,
        "boundary": boundary,
        "pipeline": pipeline,
        "safety": safety,
    }


def _transfer_counts(
    report: dict[str, Any], validated: dict[str, Any], scenarios: tuple[str, ...]
) -> dict[str, Any]:
    """Select mutually checked transfer counts from the canonical report."""
    datasets = _mapping(validated.get("datasets"), "validated transfer datasets")
    pipeline = _mapping(validated.get("pipeline"), "validated transfer pipeline")

    def actual(name: str) -> int:
        row = _mapping(datasets[name], f"transfer.{name}")
        validation = _mapping(row.get("validation"), f"transfer.{name}.validation")
        if validation.get("feature_count_matches") is not True:
            raise EvidenceError(f"transfer feature count validation failed: {name}")
        return _integer(validation.get("actual_feature_count"), f"transfer.{name}.actual_count")

    tract_count = actual("tracts")
    if actual("acs") != tract_count:
        raise EvidenceError("inconsistent tract/ACS counts in transfer report")
    block_groups = actual("block_groups")
    if actual("acs_block_groups") != block_groups:
        raise EvidenceError("inconsistent block-group/ACS counts in transfer report")
    row_counts = _mapping(pipeline.get("row_counts"), "transfer.pipeline.row_counts")
    if any(_integer(row_counts.get(name), f"transfer row count {name}") != tract_count for name in scenarios):
        raise EvidenceError("wrong pipeline row count in transfer report")
    tables = _mapping(pipeline.get("tables"), "transfer.pipeline.tables")
    scored_counts: set[int] = set()
    unscored_counts: set[int] = set()
    unscored_geoids: set[tuple[str, ...]] = set()
    for scenario in scenarios:
        table = _mapping(tables.get(scenario), f"transfer.pipeline.tables.{scenario}")
        scored_counts.add(_integer(table.get("units_scored"), f"{scenario}.units_scored"))
        unscored_counts.add(_integer(table.get("units_unscored"), f"{scenario}.units_unscored"))
        risk_evidence = _mapping(table.get("risk_evidence"), f"{scenario}.risk_evidence")
        geoids = tuple(
            _geoid(item, config.TRANSFER_AREA.county_geoid, 11, f"{scenario}.unscored")
            for item in _sequence(risk_evidence.get("unscored_geoids"), f"{scenario}.unscored")
        )
        unscored_geoids.add(geoids)
    if len(scored_counts) != 1 or len(unscored_counts) != 1 or len(unscored_geoids) != 1:
        raise EvidenceError("transfer scored/unscored counts disagree across scenarios")
    scored_count = next(iter(scored_counts))
    unscored_count = next(iter(unscored_counts))
    if scored_count + unscored_count != tract_count:
        raise EvidenceError("transfer scored/unscored counts do not sum to total tracts")
    elevation = _mapping(datasets["elevation"], "transfer.elevation")
    elevation_validation = _mapping(elevation.get("validation"), "transfer.elevation.validation")
    elevation_provenance = _mapping(elevation.get("provenance"), "transfer.elevation.provenance")
    elevation_params = _mapping(
        elevation_provenance.get("request_params"), "transfer.elevation.request_params"
    )
    flood = _mapping(datasets["flood_zones"], "transfer.flood_zones")
    flood_validation = _mapping(flood.get("validation"), "transfer.flood_zones.validation")
    reasons = [str(item) for item in flood_validation.get("degraded_reasons", ())]
    if flood_validation.get("degraded") is not True or not reasons:
        raise EvidenceError("transfer NFHL degradation is not recorded")
    return {
        "population": _integer(pipeline.get("population_total"), "transfer population"),
        "tract_count": tract_count,
        "block_group_count": block_groups,
        "facility_count": actual("facilities"),
        "scored_count": scored_count,
        "unscored_count": unscored_count,
        "unscored_geoids": list(next(iter(unscored_geoids))),
        "raster_width": _integer(elevation_validation.get("raster_width"), "transfer raster width"),
        "raster_height": _integer(
            elevation_validation.get("raster_height"), "transfer raster height"
        ),
        "requested_resolution_m": _number(
            elevation_params.get("cell_size_m_requested"), "transfer requested raster resolution"
        ),
        "effective_resolution_m": _number(
            elevation_params.get("cell_size_m_effective"), "transfer effective raster resolution"
        ),
        "nfhl_degraded": True,
        "nfhl_reason": reasons,
    }


def _fault_values(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and group the clean and faulted robustness measurements."""
    rates_raw = _sequence(payload.get("rates"), "faults.rates")
    rates = [_number(value, "fault rate") for value in rates_raw]
    if not rates or len(set(rates)) != len(rates) or any(not 0 < rate < 1 for rate in rates):
        raise EvidenceError("malformed fault rates")
    seeds = _integer(payload.get("seeds"), "fault seeds")
    if seeds == 0:
        raise EvidenceError("fault seeds must be positive")
    if _integer(payload.get("max_retries"), "fault max_retries") != config.MAX_RETRIES:
        raise EvidenceError("fault retry bound disagrees with configuration")
    if not math.isclose(
        _number(payload.get("request_timeout_s"), "fault request timeout"),
        config.REQUEST_TIMEOUT_S,
    ):
        raise EvidenceError("fault timeout disagrees with configuration")
    clean = _mapping(payload.get("clean"), "faults.clean")
    rows_raw = _sequence(payload.get("rows"), "faults.rows")
    rows: list[dict[str, JsonValue]] = []
    pairs: set[tuple[str, str, float]] = set()
    for index, raw in enumerate(rows_raw):
        row = _mapping(raw, f"faults.rows[{index}]")
        source = str(row.get("source", ""))
        kind = str(row.get("kind", ""))
        rate = _number(row.get("rate"), f"faults.rows[{index}].rate")
        pair = (source, kind, rate)
        if not source or not kind or rate not in rates or pair in pairs:
            raise EvidenceError(f"duplicate or malformed fault row: {pair}")
        pairs.add(pair)
        runs = _integer(row.get("runs"), f"faults.rows[{index}].runs")
        completed = _integer(row.get("completed"), f"faults.rows[{index}].completed")
        correct = _integer(row.get("correct"), f"faults.rows[{index}].correct")
        faulted = _integer(
            row.get("runs_with_an_injected_fault"), f"faults.rows[{index}].faulted"
        )
        if runs == 0 or correct > completed or completed > runs or faulted > runs:
            raise EvidenceError("inconsistent robustness counts")
        recovery_rate = _number(
            row.get("recovery_rate"), f"faults.rows[{index}].recovery_rate"
        )
        if not 0.0 <= recovery_rate <= 1.0 or not math.isclose(
            recovery_rate,
            correct / runs,
            rel_tol=0.0,
            abs_tol=5e-4,
        ):
            raise EvidenceError("inconsistent robustness recovery rate")
        mean_extra_calls = _number(
            row.get("mean_extra_calls"), f"faults.rows[{index}].mean_extra_calls"
        )
        if mean_extra_calls < 0.0:
            raise EvidenceError("negative robustness mean extra calls")
        injected_faults = _integer(
            row.get("injected_faults"), f"faults.rows[{index}].injected_faults"
        )
        not_applicable = _integer(
            row.get("not_applicable"), f"faults.rows[{index}].not_applicable"
        )
        attempts = _integer(row.get("attempts"), f"faults.rows[{index}].attempts")
        if injected_faults < faulted or not_applicable > runs or attempts < runs:
            raise EvidenceError("inconsistent robustness attempt counts")
        parsed: dict[str, JsonValue] = {
            "source": source,
            "kind": kind,
            "rate": rate,
            "runs": runs,
            "completed": completed,
            "correct": correct,
            "recovery_rate": recovery_rate,
            "recovery_rate_given_faulted": None,
            "runs_with_an_injected_fault": faulted,
            "injected_faults": injected_faults,
            "not_applicable": not_applicable,
            "attempts": attempts,
            "mean_extra_calls": mean_extra_calls,
            "failure_kinds": [
                str(item)
                for item in _sequence(
                    row.get("failure_kinds"), f"faults.rows[{index}].failure_kinds"
                )
            ],
        }
        conditional = row.get("recovery_rate_given_faulted")
        if faulted == 0 and conditional is not None:
            raise EvidenceError("conditional robustness rate has no faulted runs")
        if faulted > 0 and conditional is None:
            raise EvidenceError("conditional robustness rate is missing")
        if conditional is not None:
            conditional_rate = _number(
                conditional, f"faults.rows[{index}].recovery_rate_given_faulted"
            )
            if not 0.0 <= conditional_rate <= 1.0:
                raise EvidenceError("inconsistent conditional robustness recovery rate")
            parsed["recovery_rate_given_faulted"] = conditional_rate
        rows.append(parsed)
    fixture_rows = [row for row in rows if row["source"] == "fixture"]
    kinds = sorted({str(row["kind"]) for row in fixture_rows})
    expected = {(kind, rate) for kind in kinds for rate in rates}
    observed = {(str(row["kind"]), float(row["rate"])) for row in fixture_rows}
    if not kinds or observed != expected:
        raise EvidenceError("fixture robustness grid is incomplete")
    runs_per_cell = {int(row["runs"]) for row in fixture_rows}
    if runs_per_cell != {seeds}:
        raise EvidenceError("fixture runs per cell disagree with seeds")
    clean_runs = _integer(clean.get("runs"), "faults.clean.runs")
    clean_completed = _integer(clean.get("completed"), "faults.clean.completed")
    clean_correct = _integer(clean.get("correct"), "faults.clean.correct")
    clean_recovery = _number(clean.get("recovery_rate"), "faults.clean.recovery_rate")
    clean_mean_extra = _number(
        clean.get("mean_extra_calls"), "faults.clean.mean_extra_calls"
    )
    if clean.get("source") != "fixture" or not (
        clean_runs == clean_completed == clean_correct == seeds
    ) or not math.isclose(clean_recovery, 1.0) or clean_mean_extra < 0.0:
        raise EvidenceError("clean robustness baseline is inconsistent")
    if (
        clean.get("recovery_rate_given_faulted") is not None
        or _integer(clean.get("runs_with_an_injected_fault"), "faults.clean.faulted") != 0
        or _integer(clean.get("injected_faults"), "faults.clean.injected_faults") != 0
        or _integer(clean.get("not_applicable"), "faults.clean.not_applicable") > clean_runs
        or _integer(clean.get("attempts"), "faults.clean.attempts") < clean_runs
    ):
        raise EvidenceError("clean robustness baseline is inconsistent")
    grouped = {
        source: [row for row in rows if row["source"] == source]
        for source in sorted({str(row["source"]) for row in rows})
    }
    grouped.setdefault("live", [])
    return {
        "rates": rates,
        "seeds": seeds,
        "fault_kinds": kinds,
        "clean": clean,
        "rows_by_source": grouped,
        "fixture_runs_per_cell": seeds,
    }


def _paper_placeholders(path: Path) -> tuple[list[str], list[dict[str, JsonValue]]]:
    """Collect unique semantic number keys and separately locate prose TODOs."""
    if not path.is_file():
        raise EvidenceError(f"missing source file: {path}")
    text = path.read_text(encoding="utf-8")
    keys = PLACEHOLDER.findall(text)
    generic = [key for key in keys if not SEMANTIC_KEY.fullmatch(key)]
    if generic:
        raise EvidenceError(f"numeric paper placeholder with no semantic key: {generic}")
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise EvidenceError(f"duplicate key in paper placeholders: {duplicates}")
    todos: list[dict[str, JsonValue]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if PROSE_TODO.search(line.strip()):
            todos.append({"line": line_number, "text": line.strip()})
    return keys, todos


def _compare_transfer_rows(
    rows: list[dict[str, Any]], report_rows: Any
) -> None:
    """Require the transfer CSV and the canonical report's scenario rows to agree."""
    reported = _sequence(report_rows, "transfer.pipeline.scenario_rows")
    if len(reported) != len(rows):
        raise EvidenceError("incorrect trade-off row count in transfer scenario rows")
    csv_by_pair = {(row["scenario"], row["preset"]): row for row in rows}
    for index, raw in enumerate(reported):
        row = _mapping(raw, f"transfer.pipeline.scenario_rows[{index}]")
        pair = (str(row.get("scenario", "")), str(row.get("preset", "")))
        if pair not in csv_by_pair:
            raise EvidenceError(f"missing scenario/preset in transfer scenario rows: {pair}")
        observed = csv_by_pair[pair]
        for key in ("population_in_priority", "vulnerable_population_in_priority"):
            if _integer(row.get(key), f"transfer scenario row {key}") != observed[key]:
                raise EvidenceError("transfer trade-off CSV disagrees with canonical report")
        if not math.isclose(
            _number(row.get("mean_inundation_m"), "transfer scenario mean"),
            observed["mean_inundation_m"],
            rel_tol=0,
            abs_tol=1e-12,
        ):
            raise EvidenceError("transfer trade-off CSV disagrees with canonical report")
        if list(row.get("top_geoids", ())) != observed["top_geoids"]:
            raise EvidenceError("transfer top GEOIDs disagree with canonical report")
        if list(row.get("displaced_geoids", ())) != observed["displaced_geoids"]:
            raise EvidenceError("transfer displaced GEOIDs disagree with canonical report")


def _tradeoff_payload(rows: list[dict[str, Any]], source: str) -> list[dict[str, JsonValue]]:
    """Turn validated trade-off rows into individually traceable records."""
    payload: list[dict[str, JsonValue]] = []
    for row in rows:
        selector = f"scenario={row['scenario']}; preset={row['preset']}"
        payload.append(
            {
                "scenario": _metric(
                    str(row["scenario"]),
                    "scenario name",
                    source,
                    f"row where {selector}; column=scenario",
                    "read from the validated scenario/preset Cartesian table",
                ),
                "preset": _metric(
                    str(row["preset"]),
                    "preset name",
                    source,
                    f"row where {selector}; column=preset",
                    "read from the validated scenario/preset Cartesian table",
                ),
                "population_in_priority": _metric(
                    int(row["population_in_priority"]),
                    "people",
                    source,
                    f"row where {selector}; column=population_in_priority",
                    "pipeline-produced sum over the ten selected GEOIDs",
                ),
                "vulnerable_population_in_priority": _metric(
                    int(row["vulnerable_population_in_priority"]),
                    "people",
                    source,
                    f"row where {selector}; column=vulnerable_population_in_priority",
                    "pipeline-produced population sum for selected units above its stated vulnerability quantile",
                ),
                "mean_inundation_m": _metric(
                    float(row["mean_inundation_m"]),
                    "metres",
                    source,
                    f"row where {selector}; column=mean_inundation_m",
                    "pipeline-produced mean over the discrete priority set",
                ),
                "top_geoids": _metric(
                    list(row["top_geoids"]),
                    "GEOIDs",
                    source,
                    f"row where {selector}; column=top_geoids",
                    "validated space-delimited identifiers, preserving leading zeros",
                ),
                "displaced_geoids": _metric(
                    list(row["displaced_geoids"]),
                    "GEOIDs",
                    source,
                    f"row where {selector}; column=displaced_geoids",
                    "set difference between the union selected by any preset and this preset's selected set",
                ),
                "displaced_geoid_count": _metric(
                    len(row["displaced_geoids"]),
                    "tracts",
                    source,
                    f"row where {selector}; column=displaced_geoids",
                    "count of validated space-delimited displaced identifiers",
                ),
            }
        )
    return payload


def _robustness_payload(values: dict[str, Any], source: str) -> dict[str, JsonValue]:
    """Attach traceability to every robustness count and rate."""
    clean = _mapping(values.get("clean"), "validated faults clean")
    clean_payload: dict[str, JsonValue] = {}
    for key in (
        "source",
        "kind",
        "rate",
        "runs",
        "completed",
        "correct",
        "recovery_rate",
        "recovery_rate_given_faulted",
        "runs_with_an_injected_fault",
        "injected_faults",
        "not_applicable",
        "attempts",
        "mean_extra_calls",
        "features_seen",
        "every_call_had_a_timeout",
        "failure_kinds",
    ):
        if key in clean:
            clean_payload[key] = _metric(
                clean[key],
                "value",
                source,
                f"$.clean.{key}",
                "read from the separately measured clean fixture baseline",
            )
    grouped = _mapping(values.get("rows_by_source"), "validated faults rows_by_source")
    rows_by_source: dict[str, JsonValue] = {}
    for group, raw_rows in grouped.items():
        rows = _sequence(raw_rows, f"validated faults rows_by_source.{group}")
        wrapped: list[JsonValue] = []
        for row in rows:
            parsed = _mapping(row, f"validated fault row {group}")
            selector = f"$.rows[source={parsed['source']},kind={parsed['kind']},rate={parsed['rate']}]"
            wrapped.append(
                {
                    key: _metric(
                        value,
                        "value",
                        source,
                        f"{selector}.{key}",
                        "read from one labeled experiment cell; fixture and live cells remain separate",
                    )
                    for key, value in parsed.items()
                }
            )
        rows_by_source[group] = wrapped
    rates = list(values["rates"])
    kinds = list(values["fault_kinds"])
    return {
        "rates": _metric(
            rates,
            "probability per network attempt",
            source,
            "$.rates",
            "validated unique injected-fault rates",
        ),
        "rate_count": _metric(
            len(rates),
            "rates",
            source,
            "$.rates",
            "length of the validated rate list",
        ),
        "seeds": _metric(
            int(values["seeds"]),
            "seeded runs",
            source,
            "$.seeds",
            "declared fixture seed count, cross-checked against every fixture cell",
        ),
        "fixture_runs_per_cell": _metric(
            int(values["fixture_runs_per_cell"]),
            "runs per cell",
            source,
            "$.rows[source=fixture].runs",
            "the single run count shared by every validated fixture cell",
        ),
        "fault_kinds": _metric(
            kinds,
            "fault-kind names",
            source,
            "distinct $.rows[source=fixture].kind",
            "sorted unique kind names in the complete fixture grid",
        ),
        "fault_kind_count": _metric(
            len(kinds),
            "fault kinds",
            source,
            "distinct $.rows[source=fixture].kind",
            "count of unique kind names in the complete fixture grid",
        ),
        "clean_baseline": clean_payload,
        "rows_by_source": rows_by_source,
    }


def _paper_catalog(report: dict[str, JsonValue]) -> dict[str, Metric]:
    """Map semantic paper placeholders to already-traceable metric records."""
    study = _mapping(report["study_areas"], "report.study_areas")
    primary = _mapping(study["primary"], "report.study_areas.primary")
    transfer = _mapping(study["transfer"], "report.study_areas.transfer")
    configuration = _mapping(report["configuration"], "report.configuration")
    alignment = _mapping(report["alignment"], "report.alignment")
    primary_alignment = _mapping(alignment["primary"], "report.alignment.primary")
    transfer_section = _mapping(report["transfer"], "report.transfer")
    robustness = _mapping(report["robustness"], "report.robustness")
    mappings: dict[str, Any] = {
        "abstract.primary_tract_count": primary["tract_count"],
        "abstract.scenario_count": configuration["scenario_count"],
        "abstract.preset_count": configuration["preset_count"],
        "abstract.transfer_tract_count": transfer["tract_count"],
        "abstract.transfer_scored_count": transfer_section["scored_count"],
        "methods.tool_count": configuration["tool_count"],
        "methods.agent_iteration_bound": configuration["agent_iteration_bound"],
        "methods.critic_revision_bound": configuration["critic_revision_bound"],
        "methods.sandbox_timeout_s": configuration["sandbox_timeout_s"],
        "methods.retry_bound": configuration["network_retry_bound"],
        "alignment.working_epsg": alignment["working_epsg"],
        "alignment.primary_raster_resolution_m": primary_alignment[
            "effective_raster_resolution_m"
        ],
        "alignment.primary_sentinel_count": primary_alignment["sentinel_count"],
        "results.primary_population": primary["population"],
        "results.primary_tract_count": primary["tract_count"],
        "results.primary_scored_count": primary["scored_count"],
        "results.transfer_population": transfer["population"],
        "results.transfer_tract_count": transfer["tract_count"],
        "results.transfer_scored_count": transfer_section["scored_count"],
        "results.transfer_unscored_count": transfer_section["unscored_count"],
        "results.transfer_facility_count": transfer["facility_count"],
        "results.transfer_raster_width": transfer_section["raster_width"],
        "results.transfer_raster_height": transfer_section["raster_height"],
        "results.transfer_raster_resolution_m": transfer_section[
            "effective_raster_resolution_m"
        ],
        "robustness.fixture_runs_per_cell": robustness["fixture_runs_per_cell"],
        "robustness.rate_count": robustness["rate_count"],
        "robustness.fault_kind_count": robustness["fault_kind_count"],
        "limitations.county_count": configuration["configured_county_count"],
    }
    catalog: dict[str, Metric] = {}
    for key, raw in mappings.items():
        metric = _mapping(raw, f"paper catalog {key}")
        if set(metric) != METRIC_FIELDS:
            raise EvidenceError(f"paper key with no evidence: {key}")
        catalog[key] = dict(metric)
    return catalog


def build_report(root: Path, generated_at: str | None = None) -> dict[str, JsonValue]:
    """Aggregate and validate all authoritative evidence beneath ``root``."""
    repository = Path(root).resolve()
    sources: dict[str, JsonValue] = {}
    module_source = {
        name: _source(sources, repository / relative, repository, sources_role)
        for name, relative, sources_role in (
            (name, relative, f"configuration constant source: {name}")
            for name, relative in MODULE_SOURCES.items()
        )
    }

    scenarios = tuple(item.name for item in hazard.HAZARD_SCENARIOS)
    presets = tuple(item.name for item in vulnerability.WEIGHT_PRESETS)
    if len(set(scenarios)) != len(scenarios) or not scenarios:
        raise EvidenceError("missing scenario in source configuration")
    if len(set(presets)) != len(presets) or not presets:
        raise EvidenceError("missing preset in source configuration")

    manifest_path = repository / MANIFEST_RELATIVE
    manifest_source = _source(sources, manifest_path, repository, "primary snapshot manifest")
    manifest = _load_json(manifest_path)
    datasets = _dataset_map(manifest, "primary")
    for name in ("acs", "acs_block_groups", "block_groups", "tracts"):
        if not _request_area_matches(datasets[name], config.STUDY_AREA):
            raise EvidenceError(f"wrong requested study area in primary dataset: {name}")
    primary_tracts = _integer(
        datasets["tracts"]["provenance"]["feature_count"], "primary tract count"
    )
    if _integer(datasets["acs"]["provenance"]["feature_count"], "primary ACS count") != primary_tracts:
        raise EvidenceError("inconsistent tract/ACS counts")
    primary_block_groups = _integer(
        datasets["block_groups"]["provenance"]["feature_count"],
        "primary block-group count",
    )
    if _integer(
        datasets["acs_block_groups"]["provenance"]["feature_count"],
        "primary block-group ACS count",
    ) != primary_block_groups:
        raise EvidenceError("inconsistent block-group/ACS counts")
    primary_facilities = _integer(
        datasets["facilities"]["provenance"]["feature_count"],
        "primary facility count",
    )
    primary_flood_params = _mapping(
        datasets["flood_zones"]["provenance"]["request_params"],
        "primary flood_zones request params",
    )
    primary_flood_notes = [
        str(item) for item in datasets["flood_zones"]["provenance"].get("notes", ())
    ]
    if primary_flood_params.get("degraded") != "true" or not primary_flood_notes:
        raise EvidenceError("primary NFHL degradation is not recorded")

    primary_risk: dict[str, dict[str, Any]] = {}
    primary_risk_sources: dict[str, str] = {}
    for scenario in scenarios:
        path = repository / "outputs" / f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv"
        primary_risk_sources[scenario] = _source(
            sources, path, repository, f"primary risk table: {scenario}"
        )
        primary_risk[scenario] = _risk_table(
            path,
            config.STUDY_AREA,
            primary_tracts,
            scenario,
            vulnerability.DEFAULT_PRESET.name,
        )
    geoid_sets = {tuple(row["geoids"]) for row in primary_risk.values()}
    population_totals = {int(row["population_total"]) for row in primary_risk.values()}
    scored_counts = {int(row["scored_count"]) for row in primary_risk.values()}
    unscored_counts = {int(row["unscored_count"]) for row in primary_risk.values()}
    unscored_geoids = {tuple(row["unscored_geoids"]) for row in primary_risk.values()}
    if any(len(values) != 1 for values in (geoid_sets, population_totals, scored_counts, unscored_counts, unscored_geoids)):
        raise EvidenceError("primary risk tables disagree across scenarios")
    primary_population = next(iter(population_totals))
    primary_scored = next(iter(scored_counts))
    primary_unscored = next(iter(unscored_counts))
    if primary_scored + primary_unscored != primary_tracts:
        raise EvidenceError("primary scored/unscored counts do not sum to total tracts")

    pipeline_report_path = repository / PIPELINE_REPORT_RELATIVE
    pipeline_source = _source(
        sources, pipeline_report_path, repository, "primary pipeline report"
    )
    try:
        alignment_values = _alignment_values(pipeline_report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise EvidenceError("missing primary pipeline report") from exc
    if alignment_values["working_crs"] != config.STUDY_AREA.working_crs:
        raise EvidenceError("primary working CRS disagrees with configuration")

    elevation_path = repository / ELEVATION_RELATIVE
    elevation_source = _source(
        sources, elevation_path, repository, "primary elevation raster metadata"
    )
    primary_raster = _raster_values(elevation_path, datasets["elevation"])
    if primary_raster["crs"] != config.STUDY_AREA.working_crs:
        raise EvidenceError("primary elevation raster has wrong working CRS")

    tradeoff_path = repository / PRIMARY_TRADEOFF_RELATIVE
    paper_tradeoff_path = repository / PAPER_TRADEOFF_RELATIVE
    primary_tradeoff_source = _source(
        sources, tradeoff_path, repository, "primary trade-off table"
    )
    paper_tradeoff_source = _source(
        sources, paper_tradeoff_path, repository, "verified primary paper trade-off copy"
    )
    if tradeoff_path.read_bytes() != paper_tradeoff_path.read_bytes():
        raise EvidenceError("mismatched primary trade-off copy")
    primary_tradeoffs = _tradeoff_table(
        paper_tradeoff_path, config.STUDY_AREA, scenarios, presets
    )

    transfer_report_path = repository / TRANSFER_REPORT_RELATIVE
    transfer_report_source = _source(
        sources, transfer_report_path, repository, "canonical transfer report"
    )
    transfer_report = _load_json(transfer_report_path)
    run_id, transfer_directory = _safe_transfer_directory(repository, transfer_report)
    validated_transfer = _validate_transfer_report(
        transfer_report, config.TRANSFER_AREA, scenarios, presets
    )
    transfer_counts = _transfer_counts(transfer_report, validated_transfer, scenarios)
    transfer_pipeline = _mapping(validated_transfer["pipeline"], "validated transfer pipeline")

    transfer_risk: dict[str, dict[str, Any]] = {}
    transfer_risk_sources: dict[str, str] = {}
    for scenario in scenarios:
        path = transfer_directory / f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv"
        transfer_risk_sources[scenario] = _source(
            sources, path, repository, f"transfer risk table: {scenario}"
        )
        transfer_risk[scenario] = _risk_table(
            path,
            config.TRANSFER_AREA,
            int(transfer_counts["tract_count"]),
            scenario,
            vulnerability.DEFAULT_PRESET.name,
        )
        row = transfer_risk[scenario]
        if (
            row["population_total"] != transfer_counts["population"]
            or row["scored_count"] != transfer_counts["scored_count"]
            or row["unscored_count"] != transfer_counts["unscored_count"]
            or set(row["unscored_geoids"]) != set(transfer_counts["unscored_geoids"])
        ):
            raise EvidenceError("transfer risk table disagrees with canonical report")
    transfer_tradeoff_path = transfer_directory / "tradeoff.csv"
    transfer_tradeoff_source = _source(
        sources, transfer_tradeoff_path, repository, "run-scoped transfer trade-off table"
    )
    transfer_tradeoffs = _tradeoff_table(
        transfer_tradeoff_path, config.TRANSFER_AREA, scenarios, presets
    )
    _compare_transfer_rows(transfer_tradeoffs, transfer_pipeline.get("scenario_rows"))
    transfer_pipeline_report_path = transfer_directory / "pipeline_report.txt"
    _source(
        sources,
        transfer_pipeline_report_path,
        repository,
        "run-scoped transfer pipeline report",
    )
    if transfer_pipeline_report_path.stat().st_size == 0:
        raise EvidenceError("transfer pipeline report is empty")

    expected_written = {
        _portable(transfer_pipeline_report_path, repository),
        _portable(transfer_tradeoff_path, repository),
        *(_portable(transfer_directory / f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv", repository) for scenario in scenarios),
    }
    if set(transfer_report.get("written_files", ())) != expected_written:
        raise EvidenceError("canonical transfer written_files do not match run artifacts")
    primary_copy = _mapping(transfer_report.get("primary_tradeoff"), "transfer.primary_tradeoff")
    if not all(
        primary_copy.get(key) is True for key in ("bytes_equal", "columns_equal", "rows_equal")
    ):
        raise EvidenceError("mismatched primary trade-off copy in transfer report")
    if (
        primary_copy.get("source_sha256") != _sha256(tradeoff_path)
        or primary_copy.get("destination_sha256") != _sha256(paper_tradeoff_path)
        or _integer(primary_copy.get("row_count"), "primary trade-off copy row count")
        != len(scenarios) * len(presets)
    ):
        raise EvidenceError("mismatched primary trade-off copy fingerprint")

    attempt_path, failed_attempt = _failed_attempt(repository, config.TRANSFER_AREA)
    attempt_source = _source(
        sources, attempt_path, repository, "preserved initial failed transfer attempt"
    )

    faults_path = repository / FAULTS_RELATIVE
    faults_source = _source(sources, faults_path, repository, "fault-injection measurements")
    fault_values = _fault_values(_load_json(faults_path))

    paper_path = repository / PAPER_RELATIVE
    paper_source = _source(sources, paper_path, repository, "paper placeholder source")
    placeholders, prose_todos = _paper_placeholders(paper_path)

    config_source = module_source["config"]
    first_primary_risk_source = primary_risk_sources[scenarios[0]]
    primary_count_selector = f"{manifest_source} | $.datasets[name=tracts].provenance.feature_count"
    transfer_count_selector = (
        f"{transfer_report_source} | $.datasets[name=tracts].validation.actual_feature_count"
    )
    primary_area = _area_payload(
        config.STUDY_AREA,
        config_source,
        primary_population,
        primary_tracts,
        primary_block_groups,
        primary_facilities,
        f"{first_primary_risk_source} | column=population; aggregation=sum; all rows",
        primary_count_selector,
    )
    primary_area["scored_count"] = _metric(
        primary_scored,
        "tracts",
        first_primary_risk_source,
        "columns=risk_score,priority_rank; rows where both are non-null",
        "count after validating that score/rank nullness agrees and ranks stay within the scored population",
    )
    primary_area["unscored_count"] = _metric(
        primary_unscored,
        "tracts",
        first_primary_risk_source,
        "columns=risk_score,priority_rank; rows where both are null",
        "count of explicitly unscored rows, never coerced to zero score",
    )
    primary_area["unscored_geoids"] = _metric(
        list(next(iter(unscored_geoids))),
        "GEOIDs",
        first_primary_risk_source,
        "GEOID where risk_score and priority_rank are both null",
        "validated identifiers of explicitly unscored rows",
    )
    transfer_area = _area_payload(
        config.TRANSFER_AREA,
        config_source,
        int(transfer_counts["population"]),
        int(transfer_counts["tract_count"]),
        int(transfer_counts["block_group_count"]),
        int(transfer_counts["facility_count"]),
        f"{transfer_report_source} | $.pipeline.population_total",
        transfer_count_selector,
    )

    robustness_payload = _robustness_payload(fault_values, faults_source)
    report: dict[str, JsonValue] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "sources": sources,
        "study_areas": {"primary": primary_area, "transfer": transfer_area},
        "data_acquisition": {
            "primary": {
                "dataset_count": _metric(
                    len(datasets),
                    "registered datasets",
                    manifest_source,
                    "$.datasets",
                    "length after requiring the complete registered dataset set",
                ),
                "nfhl_degraded": _metric(
                    True,
                    "boolean",
                    manifest_source,
                    "$.datasets[name=flood_zones].provenance.request_params.degraded",
                    "the recorded degraded flag, not an inference from zero features",
                ),
                "nfhl_reason": _metric(
                    primary_flood_notes,
                    "provenance notes",
                    manifest_source,
                    "$.datasets[name=flood_zones].provenance.notes",
                    "recorded retrieval reason retained verbatim as structured strings",
                ),
            },
            "transfer": {
                "dataset_count": _metric(
                    len(REQUIRED_DATASETS),
                    "registered datasets",
                    transfer_report_source,
                    "$.datasets",
                    "length after requiring the complete registered dataset set",
                ),
                "nfhl_degraded": _metric(
                    bool(transfer_counts["nfhl_degraded"]),
                    "boolean",
                    transfer_report_source,
                    "$.datasets[name=flood_zones].validation.degraded",
                    "canonical validation flag, not an inference from zero features",
                ),
                "nfhl_reason": _metric(
                    list(transfer_counts["nfhl_reason"]),
                    "degradation reasons",
                    transfer_report_source,
                    "$.datasets[name=flood_zones].validation.degraded_reasons",
                    "recorded reasons from the completed transfer validation",
                ),
            },
        },
        "alignment": {
            "working_crs": _metric(
                config.DEFAULT_WORKING_CRS,
                "CRS identifier",
                config_source,
                "DEFAULT_WORKING_CRS",
                "configured projected working CRS",
            ),
            "working_epsg": _metric(
                int(config.DEFAULT_WORKING_CRS.split(":", 1)[1]),
                "EPSG code",
                config_source,
                "DEFAULT_WORKING_CRS",
                "integer suffix of the validated EPSG identifier",
            ),
            "primary": {
                "requested_raster_resolution_m": _metric(
                    primary_raster["requested_resolution_m"],
                    "metres",
                    manifest_source,
                    "$.datasets[name=elevation].provenance.request_params.cell_size_m_requested",
                    "parsed finite requested cell size",
                ),
                "effective_raster_resolution_m": _metric(
                    primary_raster["effective_resolution_m"],
                    "metres",
                    elevation_source,
                    "raster header transform pixel width",
                    "absolute pixel width read from the raster header and checked against manifest provenance",
                ),
                "raster_width": _metric(
                    primary_raster["width"],
                    "pixels",
                    elevation_source,
                    "raster header width",
                    "read from the raster header and checked against manifest provenance",
                ),
                "raster_height": _metric(
                    primary_raster["height"],
                    "pixels",
                    elevation_source,
                    "raster header height",
                    "read from the raster header and checked against manifest provenance",
                ),
                **{
                    key: _metric(
                        value,
                        "count" if key != "apportionment_error_pct" else "percent",
                        pipeline_source,
                        f"AlignmentReport line labeled {ALIGNMENT_REPORT_LABELS.get(key, key)}",
                        "parsed from the uniquely labeled alignment counter in the completed pipeline report",
                    )
                    for key, value in alignment_values.items()
                    if key != "working_crs"
                },
            },
        },
        "hazard": {
            "scenario_count": _metric(
                len(scenarios),
                "scenarios",
                module_source["hazard"],
                "HAZARD_SCENARIOS",
                "length of the source tuple after validating unique names",
            ),
            "scenario_names": _metric(
                list(scenarios),
                "scenario names",
                module_source["hazard"],
                "HazardScenario.name for HAZARD_SCENARIOS",
                "ordered names from the source tuple",
            ),
            "scenario_heights_m": _metric(
                {item.name: item.surge_height_m for item in hazard.HAZARD_SCENARIOS},
                "metres",
                module_source["hazard"],
                "HazardScenario.surge_height_m for HAZARD_SCENARIOS",
                "name-to-height mapping from the configured source tuple",
            ),
            "population_exposed_by_scenario": {
                scenario: _metric(
                    int(round(float(primary_risk[scenario]["exposed_total"]))),
                    "people",
                    primary_risk_sources[scenario],
                    "column=exposed_population; aggregation=sum; all rows",
                    "sum of the pipeline-produced tract values, rounded to the nearest resident",
                )
                for scenario in scenarios
            },
        },
        "risk": {
            "primary": {
                "row_counts": {
                    scenario: _metric(
                        int(primary_risk[scenario]["row_count"]),
                        "rows",
                        primary_risk_sources[scenario],
                        "all data rows",
                        "CSV row count after exact schema and unique-GEOID validation",
                    )
                    for scenario in scenarios
                },
                "scored_count": primary_area["scored_count"],
                "unscored_count": primary_area["unscored_count"],
                "unscored_geoids": primary_area["unscored_geoids"],
            },
            "transfer": {
                "row_counts": {
                    scenario: _metric(
                        int(transfer_risk[scenario]["row_count"]),
                        "rows",
                        transfer_risk_sources[scenario],
                        "all data rows",
                        "CSV row count checked against canonical transfer validation",
                    )
                    for scenario in scenarios
                }
            },
        },
        "tradeoffs": {
            "primary": {
                "row_count": _metric(
                    len(primary_tradeoffs),
                    "rows",
                    paper_tradeoff_source,
                    "all data rows",
                    "validated scenario-by-preset Cartesian product; copy is byte-identical to outputs/tradeoff.csv",
                ),
                "rows": _tradeoff_payload(primary_tradeoffs, paper_tradeoff_source),
            },
            "transfer": {
                "row_count": _metric(
                    len(transfer_tradeoffs),
                    "rows",
                    transfer_tradeoff_source,
                    "all data rows",
                    "validated scenario-by-preset Cartesian product and matched to canonical scenario_rows",
                ),
                "rows": _tradeoff_payload(transfer_tradeoffs, transfer_tradeoff_source),
            },
        },
        "robustness": robustness_payload,
        "configuration": {
            "tool_count": _metric(
                len(contracts.TOOL_NAMES),
                "model-visible tools",
                module_source["contracts"],
                "TOOL_NAMES",
                "length of the frozen tool-name tuple",
            ),
            "network_retry_bound": _metric(
                config.MAX_RETRIES,
                "attempts",
                config_source,
                "MAX_RETRIES",
                "configured bound on network attempts",
            ),
            "network_timeout_s": _metric(
                config.REQUEST_TIMEOUT_S,
                "seconds",
                config_source,
                "REQUEST_TIMEOUT_S",
                "configured timeout on each network request",
            ),
            "agent_iteration_bound": _metric(
                config.MAX_ITERATIONS,
                "iterations",
                config_source,
                "MAX_ITERATIONS",
                "configured maximum turns for one question",
            ),
            "critic_revision_bound": _metric(
                agent.MAX_REVISIONS,
                "revisions",
                module_source["agent"],
                "MAX_REVISIONS",
                "source constant bounding critic-requested revisions",
            ),
            "sandbox_timeout_s": _metric(
                sandbox.DEFAULT_TIMEOUT_S,
                "seconds",
                module_source["sandbox"],
                "DEFAULT_TIMEOUT_S",
                "source constant bounding one generated-code run",
            ),
            "vulnerability_indicator_count": _metric(
                len(contracts.VULNERABILITY_INDICATORS),
                "indicators",
                module_source["contracts"],
                "VULNERABILITY_INDICATORS",
                "length of the frozen indicator tuple",
            ),
            "vulnerability_indicator_names": _metric(
                list(contracts.VULNERABILITY_INDICATORS),
                "indicator names",
                module_source["contracts"],
                "VULNERABILITY_INDICATORS",
                "ordered names from the frozen indicator tuple",
            ),
            "scenario_count": _metric(
                len(scenarios),
                "scenarios",
                module_source["hazard"],
                "HAZARD_SCENARIOS",
                "length after validating unique scenario names",
            ),
            "scenario_names": _metric(
                list(scenarios),
                "scenario names",
                module_source["hazard"],
                "HazardScenario.name for HAZARD_SCENARIOS",
                "ordered names from the source tuple",
            ),
            "preset_count": _metric(
                len(presets),
                "weight presets",
                module_source["vulnerability"],
                "WEIGHT_PRESETS",
                "length after validating unique preset names",
            ),
            "preset_names": _metric(
                list(presets),
                "preset names",
                module_source["vulnerability"],
                "WeightPreset.name for WEIGHT_PRESETS",
                "ordered names from the source tuple",
            ),
            "preset_origins": _metric(
                {item.name: item.origin for item in vulnerability.WEIGHT_PRESETS},
                "origin labels",
                module_source["vulnerability"],
                "WeightPreset.origin for WEIGHT_PRESETS",
                "name-to-origin mapping from source values",
            ),
            "facility_radius_m": _metric(
                risk.DEFAULT_FACILITY_RADIUS_M,
                "metres",
                module_source["risk"],
                "DEFAULT_FACILITY_RADIUS_M",
                "source default for straight-line reachable-facility measurement",
            ),
            "priority_units": _metric(
                risk.DEFAULT_PRIORITY_UNITS,
                "tracts",
                module_source["risk"],
                "DEFAULT_PRIORITY_UNITS",
                "source default used by the risk and trade-off priority sets",
            ),
            "configured_county_count": _metric(
                len((config.STUDY_AREA, config.TRANSFER_AREA)),
                "configured counties",
                config_source,
                "STUDY_AREA and TRANSFER_AREA",
                "count of the two distinct configured study-area values",
            ),
        },
        "agent_behaviour": {
            "status": "missing_evidence",
            "evaluation_cohort": None,
            "reason": "no declared paper evaluation manifest selects exact run IDs and questions",
        },
        "transfer": {
            "run_id": _metric(
                run_id,
                "run identifier",
                transfer_report_source,
                "$.run_id",
                "canonical completed run identifier, resolved dynamically",
            ),
            "initial_failed_run_id": _metric(
                str(failed_attempt["run_id"]),
                "run identifier",
                attempt_source,
                "$.run_id where $.status=failed and transfer study area matches",
                "earliest preserved failed attempt by started_at",
            ),
            "initial_failed_stage": _metric(
                str(failed_attempt["stage"]),
                "stage name",
                attempt_source,
                "$.stage",
                "recorded terminal stage of the preserved failed attempt",
            ),
            "final_status": _metric(
                str(transfer_report["status"]),
                "status",
                transfer_report_source,
                "$.status",
                "canonical report status, required to equal completed",
            ),
            "final_stage": _metric(
                str(transfer_report["stage"]),
                "stage",
                transfer_report_source,
                "$.stage",
                "canonical report stage, required to equal complete",
            ),
            "scenario_count": _metric(
                len(scenarios),
                "scenarios",
                transfer_report_source,
                "$.pipeline.expected_scenarios",
                "length after matching the canonical expected and completed scenario lists to source configuration",
            ),
            "preset_count": _metric(
                len(presets),
                "weight presets",
                transfer_report_source,
                "$.pipeline.expected_presets",
                "length after matching the canonical preset list to source configuration",
            ),
            "tradeoff_row_count": _metric(
                len(transfer_tradeoffs),
                "rows",
                transfer_tradeoff_source,
                "all data rows",
                "validated scenario-by-preset Cartesian product and matched to the canonical scenario rows",
            ),
            "scored_count": _metric(
                int(transfer_counts["scored_count"]),
                "tracts",
                transfer_report_source,
                "$.pipeline.tables[*].units_scored",
                "single count shared by all completed scenarios and cross-checked against run-scoped risk tables",
            ),
            "unscored_count": _metric(
                int(transfer_counts["unscored_count"]),
                "tracts",
                transfer_report_source,
                "$.pipeline.tables[*].units_unscored",
                "single count shared by all completed scenarios; not coerced to scored zeroes",
            ),
            "unscored_geoids": _metric(
                list(transfer_counts["unscored_geoids"]),
                "GEOIDs",
                transfer_report_source,
                "$.pipeline.tables[*].risk_evidence.unscored_geoids",
                "single validated identifier set shared by all completed scenarios",
            ),
            "raster_width": _metric(
                int(transfer_counts["raster_width"]),
                "pixels",
                transfer_report_source,
                "$.datasets[name=elevation].validation.raster_width",
                "raster dimension read back and recorded by transfer validation",
            ),
            "raster_height": _metric(
                int(transfer_counts["raster_height"]),
                "pixels",
                transfer_report_source,
                "$.datasets[name=elevation].validation.raster_height",
                "raster dimension read back and recorded by transfer validation",
            ),
            "requested_raster_resolution_m": _metric(
                float(transfer_counts["requested_resolution_m"]),
                "metres",
                transfer_report_source,
                "$.datasets[name=elevation].provenance.request_params.cell_size_m_requested",
                "finite requested cell size recorded by acquisition",
            ),
            "effective_raster_resolution_m": _metric(
                float(transfer_counts["effective_resolution_m"]),
                "metres",
                transfer_report_source,
                "$.datasets[name=elevation].provenance.request_params.cell_size_m_effective",
                "finite effective cell size recorded after raster read-back",
            ),
            "primary_snapshot_unchanged": _metric(
                True,
                "boolean",
                transfer_report_source,
                "$.safety_verification.primary_snapshot_unchanged",
                "required true after before/after fingerprint validation",
            ),
            "configuration_restored": _metric(
                True,
                "boolean",
                transfer_report_source,
                "$.safety_verification.config_paths_restored",
                "required true after all rebound configuration paths are compared",
            ),
            "artifact_validation": _metric(
                True,
                "boolean",
                transfer_report_source,
                "$.pipeline.artifact_validation",
                "all expected names, table rows, report content, and trade-off equality were required",
            ),
        },
        "verification": {},
        "paper_keys": {},
        "missing_evidence": [
            {
                "key": f"agent_behaviour.{name}",
                "reason": "no declared paper evaluation cohort; arbitrary development runs were excluded",
                "expected_source": "outputs/paper/agent_evaluation_manifest.json",
            }
            for name in AGENT_METRICS
        ],
        "remaining_paper_todos": prose_todos,
    }
    if not fault_values["rows_by_source"].get("live"):
        missing = _sequence(report["missing_evidence"], "report.missing_evidence")
        missing.append(
            {
                "key": "robustness.live_rows",
                "reason": "the current fault artifact contains no live rows; fixture results remain separately labeled",
                "expected_source": faults_source,
            }
        )

    catalog = _paper_catalog(report)
    unknown = sorted(set(placeholders) - set(catalog))
    if unknown:
        raise EvidenceError(f"numeric paper placeholder has no mapped evidence: {unknown}")
    report["paper_keys"] = {key: catalog[key] for key in placeholders}
    report["verification"] = {
        "quantitative_placeholder_count": _metric(
            len(placeholders),
            "placeholders",
            paper_source,
            r"all semantic \\TD{key} occurrences",
            "count after rejecting generic and duplicate placeholders",
        ),
        "mapped_placeholder_count": _metric(
            len(placeholders),
            "mapped placeholders",
            paper_source,
            r"all semantic \\TD{key} occurrences",
            "count whose key exists in paper_keys with complete evidence",
        ),
        "unused_paper_keys": sorted(set(catalog) - set(placeholders)),
        "remaining_prose_todo_count": _metric(
            len(prose_todos),
            "prose TODO lines",
            paper_source,
            "line scan for explicit TODO/TBD, bracketed author placeholders, and template instructions",
            "counted separately from quantitative placeholders so mapping cannot imply the paper is complete",
        ),
        "all_paper_todos_resolved": len(prose_todos) == 0 and len(placeholders) == 0,
    }
    _validate_payload(report, repository, paper_path)
    return report


def _walk_payload(value: JsonValue, path: str = "$", key: str = "") -> None:
    """Reject non-finite values, spatial payloads, and machine-specific paths."""
    _finite(value, path)
    if key.lower() in FORBIDDEN_SPATIAL_KEYS:
        raise EvidenceError(f"accidental geometry/coordinate serialization at {path}")
    if isinstance(value, str):
        if WKT_TEXT.search(value):
            raise EvidenceError(f"accidental geometry/coordinate serialization at {path}")
        if ABSOLUTE_WINDOWS.match(value) or value.startswith("/"):
            raise EvidenceError(f"absolute path leakage at {path}: {value}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_payload(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for child_key, item in value.items():
            _walk_payload(item, f"{path}.{child_key}", child_key)


def _metrics(value: JsonValue, path: str = "$") -> list[tuple[str, dict[str, JsonValue]]]:
    """Find every metric-shaped mapping and reject partial metric records."""
    found: list[tuple[str, dict[str, JsonValue]]] = []
    if isinstance(value, dict):
        keys = set(value)
        if "value" in keys:
            if keys != METRIC_FIELDS:
                raise EvidenceError(f"paper key with no evidence at {path}: fields={sorted(keys)}")
            found.append((path, value))
        else:
            for key, item in value.items():
                found.extend(_metrics(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_metrics(item, f"{path}[{index}]"))
    return found


def _validate_payload(payload: dict[str, JsonValue], root: Path, paper_path: Path) -> None:
    """Validate strictness, source hashes, traceability, portability, and paper keys."""
    required = {
        "schema_version",
        "generated_at",
        "sources",
        "study_areas",
        "data_acquisition",
        "alignment",
        "hazard",
        "risk",
        "tradeoffs",
        "robustness",
        "configuration",
        "agent_behaviour",
        "transfer",
        "verification",
        "paper_keys",
        "missing_evidence",
        "remaining_paper_todos",
    }
    if set(payload) != required or payload.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("numbers schema is incomplete or has the wrong version")
    _walk_payload(payload)
    sources = _mapping(payload.get("sources"), "$.sources")
    for relative, raw in sources.items():
        if (
            not relative
            or "\\" in relative
            or Path(relative).is_absolute()
            or ABSOLUTE_WINDOWS.match(relative)
        ):
            raise EvidenceError(f"absolute path leakage in source registry: {relative}")
        record = _mapping(raw, f"$.sources.{relative}")
        if set(record) != {"sha256", "bytes", "role"}:
            raise EvidenceError(f"malformed source fingerprint: {relative}")
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise EvidenceError(f"absolute path leakage in source registry: {relative}") from exc
        if not path.is_file():
            raise EvidenceError(f"missing source file: {relative}")
        digest, size = _fingerprint(path)
        if digest != record.get("sha256"):
            raise EvidenceError(f"source hash change: {relative}")
        if size != _integer(record.get("bytes"), f"source bytes {relative}"):
            raise EvidenceError(f"source hash change: {relative}")
    for metric_path, metric in _metrics(payload):
        source = metric.get("source")
        if not isinstance(source, str) or source not in sources:
            raise EvidenceError(f"paper key with no evidence at {metric_path}")
        if not all(str(metric.get(field, "")).strip() for field in ("unit", "selector", "derivation")):
            raise EvidenceError(f"paper key with no evidence at {metric_path}")
    placeholders, prose_todos = _paper_placeholders(paper_path)
    paper_keys = _mapping(payload.get("paper_keys"), "$.paper_keys")
    if set(paper_keys) != set(placeholders):
        raise EvidenceError("paper placeholder mapping does not match paper/main.tex")
    for key, raw in paper_keys.items():
        if not SEMANTIC_KEY.fullmatch(key):
            raise EvidenceError(f"numeric paper placeholder with no semantic key: {key}")
        metric = _mapping(raw, f"$.paper_keys.{key}")
        if set(metric) != METRIC_FIELDS or metric.get("source") not in sources:
            raise EvidenceError(f"paper key with no evidence: {key}")
    remaining = _sequence(payload.get("remaining_paper_todos"), "$.remaining_paper_todos")
    if remaining != prose_todos:
        raise EvidenceError("remaining prose TODO inventory is stale")
    try:
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        decoded = json.loads(
            encoded,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceError("final JSON is not strict") from exc
    if not isinstance(decoded, dict):
        raise EvidenceError("final JSON is not an object")


def _atomic_write(destination: Path, payload: dict[str, JsonValue]) -> Path:
    """Write stable UTF-8 strict JSON and atomically replace the destination."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target


def generate(
    root: Path = config.PROJECT_ROOT,
    destination: Path | None = None,
    generated_at: str | None = None,
) -> Path:
    """Build, validate, and write the production numbers artifact."""
    repository = Path(root).resolve()
    target = Path(destination) if destination is not None else repository / OUTPUT_RELATIVE
    payload = build_report(repository, generated_at=generated_at)
    _validate_payload(payload, repository, repository / PAPER_RELATIVE)
    return _atomic_write(target, payload)


def _write_fixture_json(path: Path, payload: dict[str, Any]) -> None:
    """Write strict JSON inside the temporary self-check fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_fixture_csv(
    path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]
) -> None:
    """Write one deterministic UTF-8 CSV inside the temporary fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _fixture_provenance(
    name: str,
    kind: str,
    feature_count: int,
    area: config.StudyArea,
    raster_size: tuple[int, int],
    raster_resolution: float,
) -> dict[str, Any]:
    """Build complete synthetic provenance with the same shape as production."""
    params: dict[str, str] = {}
    if name == "acs":
        params = {"for": "tract:*", "in": f"state:{area.state_fips} county:{area.county_fips}"}
    elif name == "acs_block_groups":
        params = {
            "for": "block group:*",
            "in": f"state:{area.state_fips} county:{area.county_fips}",
        }
    elif name in {"tracts", "block_groups"}:
        params = {"where": f"STATE='{area.state_fips}' AND COUNTY='{area.county_fips}'"}
    elif name == "elevation":
        params = {
            "size": f"{raster_size[0]},{raster_size[1]}",
            "cell_size_m_requested": "30.000000",
            "cell_size_m_effective": f"{raster_resolution:.6f}",
        }
    elif name == "flood_zones":
        params = {"degraded": "true"}
    return {
        "dataset": name,
        "source_url": f"https://example.invalid/{name}",
        "retrieved_at": "2099-01-01T00:00:00+00:00",
        "declared_crs": area.working_crs if kind == "raster" else "EPSG:4326",
        "working_crs": area.working_crs,
        "vintage": "fixture vintage",
        "feature_count": feature_count,
        "license": "fixture license",
        "request_params": params,
        "notes": (
            ["DEGRADED: optional source unavailable; elevation-only fixture"]
            if name == "flood_zones"
            else ["complete fixture provenance"]
        ),
    }


def _fixture_manifest(
    area: config.StudyArea,
    tracts: int,
    block_groups: int,
    facilities: int,
    raster_size: tuple[int, int],
    raster_resolution: float,
) -> dict[str, Any]:
    """Build a seven-dataset manifest fixture."""
    counts = {
        "acs": tracts,
        "acs_block_groups": block_groups,
        "block_groups": block_groups,
        "elevation": 1,
        "facilities": facilities,
        "flood_zones": 0,
        "tracts": tracts,
    }
    kinds = {
        "acs": "table",
        "acs_block_groups": "table",
        "block_groups": "vector",
        "elevation": "raster",
        "facilities": "vector",
        "flood_zones": "vector",
        "tracts": "vector",
    }
    return {
        "manifest_version": 1,
        "written_at": "2099-01-01T00:00:00+00:00",
        "datasets": [
            {
                "name": name,
                "kind": kinds[name],
                "path": f"data/snapshot/{name}.fixture",
                "provenance": _fixture_provenance(
                    name,
                    kinds[name],
                    counts[name],
                    area,
                    raster_size,
                    raster_resolution,
                ),
            }
            for name in REQUIRED_DATASETS
        ],
    }


def _fixture_geoids(area: config.StudyArea, count: int) -> list[str]:
    """Create tract-width identifiers from the configured county prefix."""
    return [f"{area.county_geoid}{index:06d}" for index in range(1, count + 1)]


def _fixture_risk_rows(
    area: config.StudyArea,
    count: int,
    unscored: int,
    scenario_index: int,
) -> list[dict[str, Any]]:
    """Create a small, internally consistent risk-table fixture."""
    rows: list[dict[str, Any]] = []
    scored = count - unscored
    for index, geoid in enumerate(_fixture_geoids(area, count), start=1):
        population = 1000 + index
        is_scored = index <= scored
        fraction = min(0.95, 0.1 * scenario_index + index / (count * 20))
        rows.append(
            {
                contracts.Col.GEOID: geoid,
                contracts.Col.POPULATION: population,
                contracts.Col.PCT_POVERTY: 10 + index / 10,
                contracts.Col.PCT_AGE_65_PLUS: 12 + index / 10,
                contracts.Col.PCT_DISABILITY: 8 + index / 10,
                contracts.Col.PCT_NO_VEHICLE: 6 + index / 10,
                contracts.Col.PCT_LIMITED_ENGLISH: 4 + index / 10,
                contracts.Col.INUNDATED_FRACTION: fraction,
                contracts.Col.INUNDATION_MEAN_M: scenario_index + index / 100,
                contracts.Col.INUNDATION_MAX_M: scenario_index + index / 50,
                contracts.Col.EXPOSED_POPULATION: population * fraction,
                contracts.Col.VULNERABILITY: 0.2 + index / (count * 2) if is_scored else "",
                contracts.Col.RESILIENCE: 0.1 + index / (count * 3),
                contracts.Col.RISK_SCORE: 1 - index / (count + 1) if is_scored else "",
                contracts.Col.PRIORITY_RANK: index if is_scored else "",
            }
        )
    return rows


def _fixture_tradeoffs(
    area: config.StudyArea,
    scenarios: tuple[str, ...],
    presets: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Create three discrete, overlapping priority sets per scenario."""
    geoids = _fixture_geoids(area, 12)
    starts = tuple(range(len(presets)))
    rows: list[dict[str, Any]] = []
    for scenario_index, scenario in enumerate(scenarios, start=1):
        selected = {
            preset: geoids[start : start + risk.DEFAULT_PRIORITY_UNITS]
            for preset, start in zip(presets, starts)
        }
        union = set().union(*(set(items) for items in selected.values()))
        for preset_index, preset in enumerate(presets, start=1):
            top = selected[preset]
            rows.append(
                {
                    "preset": preset,
                    "scenario": scenario,
                    "population_in_priority": sum(1000 + geoids.index(item) + 1 for item in top),
                    "vulnerable_population_in_priority": sum(
                        1000 + geoids.index(item) + 1 for item in top[preset_index:]
                    ),
                    "mean_inundation_m": scenario_index + preset_index / 10,
                    "top_geoids": " ".join(top),
                    "displaced_geoids": " ".join(sorted(union - set(top))),
                }
            )
    return rows


def _fixture_transfer_datasets(
    manifest: dict[str, Any], raster_size: tuple[int, int]
) -> list[dict[str, Any]]:
    """Add canonical transfer validation records to manifest-shaped datasets."""
    rows: list[dict[str, Any]] = []
    for raw in manifest["datasets"]:
        row = dict(raw)
        provenance = _mapping(row["provenance"], "fixture provenance")
        validation: dict[str, Any] = {
            "actual_feature_count": provenance["feature_count"],
            "complete_text_fields": True,
            "degraded": row["name"] == "flood_zones",
            "degraded_reasons": (
                ["DEGRADED: optional source unavailable; elevation-only fixture"]
                if row["name"] == "flood_zones"
                else []
            ),
            "feature_count_matches": True,
            "notes_present": True,
            "path_exists": True,
            "path_inside_work_root": True,
            "provenance_name_matches": True,
            "request_area_matches": (
                True if row["name"] in {"acs", "acs_block_groups", "block_groups", "tracts"} else None
            ),
            "request_params_present": True,
            "retrieved_at_timezone_aware": True,
            "working_crs_matches_area": True,
        }
        if row["name"] == "elevation":
            validation["raster_width"] = raster_size[0]
            validation["raster_height"] = raster_size[1]
        row["validation"] = validation
        rows.append(row)
    return rows


def _fixture_faults() -> dict[str, Any]:
    """Create a complete two-rate, five-kind fixture robustness grid."""
    rates = [0.25, 0.5]
    seeds = 3
    kinds = ["timeout", "server_error", "empty", "wrong_crs", "truncated"]
    rows: list[dict[str, Any]] = []
    for kind in kinds:
        for rate in rates:
            faulted = 1 if rate < 0.5 else 2
            recoverable = kind in {"timeout", "server_error"}
            correct = seeds if recoverable else seeds - faulted
            rows.append(
                {
                    "source": "fixture",
                    "kind": kind,
                    "rate": rate,
                    "runs": seeds,
                    "completed": seeds,
                    "correct": correct,
                    "recovery_rate": correct / seeds,
                    "recovery_rate_given_faulted": 1.0 if recoverable else 0.0,
                    "runs_with_an_injected_fault": faulted,
                    "injected_faults": faulted,
                    "not_applicable": 0,
                    "attempts": seeds + faulted,
                    "mean_extra_calls": faulted / seeds,
                    "features_seen": [10],
                    "every_call_had_a_timeout": True,
                    "failure_kinds": [],
                }
            )
    clean = {
        "source": "fixture",
        "kind": "none",
        "rate": 0.0,
        "runs": seeds,
        "completed": seeds,
        "correct": seeds,
        "recovery_rate": 1.0,
        "recovery_rate_given_faulted": None,
        "runs_with_an_injected_fault": 0,
        "injected_faults": 0,
        "not_applicable": 0,
        "attempts": seeds,
        "mean_extra_calls": 0.0,
        "features_seen": [10],
        "every_call_had_a_timeout": True,
        "failure_kinds": [],
    }
    return {
        "generated": "2099-01-01T00:00:00",
        "rates": rates,
        "seeds": seeds,
        "max_retries": config.MAX_RETRIES,
        "request_timeout_s": config.REQUEST_TIMEOUT_S,
        "clean": clean,
        "rows": rows,
        "events": [],
    }


def _write_fixture(root: Path) -> None:
    """Write a complete miniature repository evidence tree for ``--check``."""
    scenarios = tuple(item.name for item in hazard.HAZARD_SCENARIOS)
    presets = tuple(item.name for item in vulnerability.WEIGHT_PRESETS)
    for relative in MODULE_SOURCES.values():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# offline source fixture for {relative}\n", encoding="utf-8")

    primary_tracts = 12
    primary_block_groups = 24
    primary_facilities = 14
    primary_raster_size = (6, 5)
    primary_resolution = 30.0
    primary_manifest = _fixture_manifest(
        config.STUDY_AREA,
        primary_tracts,
        primary_block_groups,
        primary_facilities,
        primary_raster_size,
        primary_resolution,
    )
    _write_fixture_json(root / MANIFEST_RELATIVE, primary_manifest)
    elevation = root / ELEVATION_RELATIVE
    elevation.parent.mkdir(parents=True, exist_ok=True)
    from rasterio.transform import from_origin

    with rasterio.open(
        elevation,
        "w",
        driver="GTiff",
        width=primary_raster_size[0],
        height=primary_raster_size[1],
        count=1,
        dtype="float32",
        crs=config.STUDY_AREA.working_crs,
        transform=from_origin(0, 150, primary_resolution, primary_resolution),
        nodata=-9999.0,
    ):
        pass
    pipeline_text = "\n".join(
        (
            "PIPELINE -- offline fixture",
            f"  working crs {config.STUDY_AREA.working_crs}   fixture",
            "  geometries_repaired         1         fixture",
            "  geometries_dropped          0         fixture",
            "  sentinels_removed           {}        2 negative value(s)",
            "  unmatched_left              0         fixture",
            "  unmatched_right             0         fixture",
            "  apportionment_error         {'population': 0.0} fixture",
            "  units_below_cell_threshold  3         fixture",
            "",
        )
    )
    pipeline_path = root / PIPELINE_REPORT_RELATIVE
    pipeline_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline_path.write_text(pipeline_text, encoding="utf-8")

    primary_risk_rows: dict[str, list[dict[str, Any]]] = {}
    for scenario_index, scenario in enumerate(scenarios, start=1):
        rows = _fixture_risk_rows(config.STUDY_AREA, primary_tracts, 0, scenario_index)
        primary_risk_rows[scenario] = rows
        _write_fixture_csv(
            root / "outputs" / f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv",
            RISK_COLUMNS,
            rows,
        )
    primary_tradeoffs = _fixture_tradeoffs(config.STUDY_AREA, scenarios, presets)
    primary_tradeoff_path = root / PRIMARY_TRADEOFF_RELATIVE
    paper_tradeoff_path = root / PAPER_TRADEOFF_RELATIVE
    _write_fixture_csv(primary_tradeoff_path, TRADEOFF_COLUMNS, primary_tradeoffs)
    paper_tradeoff_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(primary_tradeoff_path, paper_tradeoff_path)

    transfer_run_id = "20990101T010203123456Z-deadbeef"
    failed_run_id = "20990101T000000123456Z-bad0c0de"
    transfer_directory = root / TRANSFER_RELATIVE / transfer_run_id
    transfer_directory.mkdir(parents=True, exist_ok=True)
    transfer_tracts = 13
    transfer_block_groups = 26
    transfer_facilities = 9
    transfer_raster_size = (7, 6)
    transfer_resolution = 30.5
    transfer_manifest = _fixture_manifest(
        config.TRANSFER_AREA,
        transfer_tracts,
        transfer_block_groups,
        transfer_facilities,
        transfer_raster_size,
        transfer_resolution,
    )
    transfer_datasets = _fixture_transfer_datasets(transfer_manifest, transfer_raster_size)
    transfer_risk_rows: dict[str, list[dict[str, Any]]] = {}
    for scenario_index, scenario in enumerate(scenarios, start=1):
        rows = _fixture_risk_rows(config.TRANSFER_AREA, transfer_tracts, 1, scenario_index)
        transfer_risk_rows[scenario] = rows
        _write_fixture_csv(
            transfer_directory / f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv",
            RISK_COLUMNS,
            rows,
        )
    transfer_tradeoffs = _fixture_tradeoffs(config.TRANSFER_AREA, scenarios, presets)
    _write_fixture_csv(
        transfer_directory / "tradeoff.csv", TRADEOFF_COLUMNS, transfer_tradeoffs
    )
    (transfer_directory / "pipeline_report.txt").write_text(
        "completed offline transfer fixture\n", encoding="utf-8"
    )
    transfer_geoids = _fixture_geoids(config.TRANSFER_AREA, transfer_tracts)
    unscored = [transfer_geoids[-1]]
    population_total = sum(
        _integer(row[contracts.Col.POPULATION], "fixture transfer population")
        for row in transfer_risk_rows[scenarios[0]]
    )
    expected_names = [
        "pipeline_report.txt",
        *(f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv" for scenario in scenarios),
        "tradeoff.csv",
    ]
    scenario_rows = [
        {
            "scenario": row["scenario"],
            "preset": row["preset"],
            "population_in_priority": row["population_in_priority"],
            "vulnerable_population_in_priority": row["vulnerable_population_in_priority"],
            "mean_inundation_m": row["mean_inundation_m"],
            "top_geoids": row["top_geoids"].split(),
            "displaced_geoids": row["displaced_geoids"].split(),
        }
        for row in transfer_tradeoffs
    ]
    transfer_report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": transfer_run_id,
        "status": "completed",
        "stage": "complete",
        "started_at": "2099-01-01T01:02:03+00:00",
        "finished_at": "2099-01-01T01:03:03+00:00",
        "study_area": {
            "name": config.TRANSFER_AREA.name,
            "state_fips": config.TRANSFER_AREA.state_fips,
            "county_fips": config.TRANSFER_AREA.county_fips,
            "working_crs": config.TRANSFER_AREA.working_crs,
        },
        "working_crs": config.TRANSFER_AREA.working_crs,
        "pipeline_output_path": (TRANSFER_RELATIVE / transfer_run_id).as_posix(),
        "primary_snapshot_unchanged": True,
        "config_paths_restored": True,
        "safety_verification": {
            "primary_snapshot_unchanged": True,
            "config_paths_restored": True,
        },
        "boundary_verification": {
            "geoid_prefix": config.TRANSFER_AREA.county_geoid,
            "all_geoids_match_prefix": True,
            "all_request_area_checks_pass": True,
            "all_required_datasets_registered": True,
            "population_total": population_total,
        },
        "datasets": transfer_datasets,
        "pipeline": {
            "study_area": {
                "name": config.TRANSFER_AREA.name,
                "state_fips": config.TRANSFER_AREA.state_fips,
                "county_fips": config.TRANSFER_AREA.county_fips,
                "working_crs": config.TRANSFER_AREA.working_crs,
            },
            "study_area_matches_requested": True,
            "expected_scenarios": list(scenarios),
            "scenarios_completed": list(scenarios),
            "expected_presets": list(presets),
            "population_total": population_total,
            "population_missing_by_scenario": {scenario: 0 for scenario in scenarios},
            "row_counts": {scenario: transfer_tracts for scenario in scenarios},
            "tables": {
                scenario: {
                    "row_count": transfer_tracts,
                    "units_scored": transfer_tracts - 1,
                    "units_unscored": 1,
                    "risk_evidence": {
                        "units_scored": transfer_tracts - 1,
                        "units_unscored": 1,
                        "unscored_geoids": unscored,
                    },
                }
                for scenario in scenarios
            },
            "scenario_rows": scenario_rows,
            "artifact_validation": {
                "expected_names": expected_names,
                "observed_names": expected_names,
                "pipeline_report_nonempty": True,
                "table_file_rows": {
                    f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv": transfer_tracts
                    for scenario in scenarios
                },
                "table_files_match_pipeline_result": True,
                "tradeoff_matches_pipeline_result": True,
                "tradeoff_row_count": len(transfer_tradeoffs),
            },
        },
        "primary_tradeoff": {
            "source": PRIMARY_TRADEOFF_RELATIVE.as_posix(),
            "destination": PAPER_TRADEOFF_RELATIVE.as_posix(),
            "source_sha256": _sha256(primary_tradeoff_path),
            "destination_sha256": _sha256(paper_tradeoff_path),
            "byte_count": primary_tradeoff_path.stat().st_size,
            "row_count": len(primary_tradeoffs),
            "bytes_equal": True,
            "columns_equal": True,
            "rows_equal": True,
        },
        "written_files": [
            (TRANSFER_RELATIVE / transfer_run_id / name).as_posix() for name in expected_names
        ],
    }
    _write_fixture_json(root / TRANSFER_REPORT_RELATIVE, transfer_report)
    failed_attempt = {
        "schema_version": 1,
        "run_id": failed_run_id,
        "status": "failed",
        "stage": "acquisition",
        "started_at": "2099-01-01T00:00:00+00:00",
        "finished_at": "2099-01-01T00:01:00+00:00",
        "study_area": {
            "name": config.TRANSFER_AREA.name,
            "state_fips": config.TRANSFER_AREA.state_fips,
            "county_fips": config.TRANSFER_AREA.county_fips,
            "working_crs": config.TRANSFER_AREA.working_crs,
        },
        "error": {"type": "TransientError", "message": "offline fixture failure"},
    }
    _write_fixture_json(root / ATTEMPTS_RELATIVE / f"{failed_run_id}.json", failed_attempt)
    _write_fixture_json(root / FAULTS_RELATIVE, _fixture_faults())
    paper = root / PAPER_RELATIVE
    paper.parent.mkdir(parents=True, exist_ok=True)
    paper.write_text(
        "\\documentclass{article}\n"
        "\\newcommand{\\TD}[1]{\\textbf{[#1]}}\n"
        + "\n".join(f"\\TD{{{key}}}" for key in EXPECTED_PAPER_KEYS)
        + "\nAuthor: [email address]\n",
        encoding="utf-8",
    )


def _edit_fixture_json(
    path: Path, editor: Callable[[dict[str, Any]], None]
) -> None:
    """Apply one intentional self-check mutation to a JSON fixture."""
    payload = _load_json(path)
    editor(payload)
    _write_fixture_json(path, payload)


def _fixture_failure(
    base: Path,
    cases: Path,
    name: str,
    mutate: Callable[[Path], None],
    phrase: str,
) -> bool:
    """Copy the good fixture, apply one mutation, and require a specific refusal."""
    root = cases / name
    shutil.copytree(base, root)
    mutate(root)
    return verify.refuses(lambda: build_report(root, "2099-01-02T00:00:00+00:00"), EvidenceError, phrase)


def _tree_hashes(root: Path) -> dict[str, str]:
    """Fingerprint every fixture source to prove aggregation is read-only."""
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _delete_mutation(relative: Path) -> Callable[[Path], None]:
    """Return a fixture mutation that removes one exact file."""
    def mutate(root: Path) -> None:
        (root / relative).unlink()

    return mutate


def _json_mutation(
    relative: Path, editor: Callable[[dict[str, Any]], None]
) -> Callable[[Path], None]:
    """Return a fixture mutation that edits one strict JSON object."""
    def mutate(root: Path) -> None:
        _edit_fixture_json(root / relative, editor)

    return mutate


def _malformed_json_mutation(relative: Path) -> Callable[[Path], None]:
    """Return a mutation that makes one fixture JSON syntactically invalid."""
    def mutate(root: Path) -> None:
        (root / relative).write_text("{\n", encoding="utf-8")

    return mutate


def _nan_json_mutation(relative: Path) -> Callable[[Path], None]:
    """Return a mutation using Python's non-standard NaN JSON token."""
    def mutate(root: Path) -> None:
        (root / relative).write_text('{"bad": NaN}\n', encoding="utf-8")

    return mutate


def _paper_append_mutation(text: str) -> Callable[[Path], None]:
    """Return a mutation that appends one paper placeholder line."""
    def mutate(root: Path) -> None:
        path = root / PAPER_RELATIVE
        path.write_text(path.read_text(encoding="utf-8") + text + "\n", encoding="utf-8")

    return mutate


def _tradeoff_row_mutation(root: Path) -> None:
    """Remove the last primary trade-off row from both byte-identical copies."""
    source = root / PRIMARY_TRADEOFF_RELATIVE
    lines = source.read_text(encoding="utf-8").splitlines()
    source.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    shutil.copyfile(source, root / PAPER_TRADEOFF_RELATIVE)
    report_path = root / TRANSFER_REPORT_RELATIVE

    def update(payload: dict[str, Any]) -> None:
        copy = payload["primary_tradeoff"]
        copy["source_sha256"] = _sha256(source)
        copy["destination_sha256"] = _sha256(root / PAPER_TRADEOFF_RELATIVE)
        copy["byte_count"] = source.stat().st_size
        copy["row_count"] = len(lines) - 2

    _edit_fixture_json(report_path, update)


def _mismatched_tradeoff_mutation(root: Path) -> None:
    """Change only the paper copy of the primary trade-off table."""
    path = root / PAPER_TRADEOFF_RELATIVE
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")


def _wrong_geoid_mutation(root: Path) -> None:
    """Replace one primary GEOID prefix while retaining a valid-width identifier."""
    scenario = hazard.HAZARD_SCENARIOS[0].name
    path = root / "outputs" / f"risk_{scenario}_{vulnerability.DEFAULT_PRESET.name}.csv"
    rows = _csv_rows(path, RISK_COLUMNS)
    original = rows[0][contracts.Col.GEOID]
    replacement = ("9" if original[0] != "9" else "8") + original[1:]
    rows[0][contracts.Col.GEOID] = replacement
    _write_fixture_csv(path, RISK_COLUMNS, rows)


def _payload_copy(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Clone a strict payload through JSON for isolated negative checks."""
    cloned = json.loads(
        json.dumps(payload, allow_nan=False),
        object_pairs_hook=_strict_pairs,
        parse_constant=_reject_constant,
    )
    if not isinstance(cloned, dict):
        raise EvidenceError("payload clone is not an object")
    return cloned


def _payload_refuses(
    payload: dict[str, JsonValue],
    root: Path,
    mutate: Callable[[dict[str, JsonValue]], None],
    phrase: str,
) -> bool:
    """Require one post-build payload mutation to fail validation specifically."""
    broken = _payload_copy(payload)
    mutate(broken)
    return verify.refuses(
        lambda: _validate_payload(broken, root, root / PAPER_RELATIVE),
        EvidenceError,
        phrase,
    )


def _hash_change_refusal(base: Path, cases: Path, delete: bool) -> bool:
    """Prove an already-built payload detects a changed or missing source file."""
    root = cases / ("missing-after-build" if delete else "hash-change")
    shutil.copytree(base, root)
    payload = build_report(root, "2099-01-02T00:00:00+00:00")
    source = root / MODULE_SOURCES["config"]
    if delete:
        source.unlink()
        phrase = "missing source file"
    else:
        source.write_text(source.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
        phrase = "source hash change"
    return verify.refuses(
        lambda: _validate_payload(payload, root, root / PAPER_RELATIVE),
        EvidenceError,
        phrase,
    )


def _self_check() -> int:
    """Run realistic offline positive and negative report gates."""
    from unittest.mock import patch

    with tempfile.TemporaryDirectory(prefix="paper-report-check-") as temporary:
        outer = Path(temporary)
        base = outer / "good"
        cases = outer / "cases"
        cases.mkdir()
        _write_fixture(base)
        fixed_time = "2099-01-02T00:00:00+00:00"
        good = build_report(base, fixed_time)
        encoded = json.dumps(good, sort_keys=True, allow_nan=False)
        repeated = json.dumps(build_report(base, fixed_time), sort_keys=True, allow_nan=False)
        before = _tree_hashes(base)

        from .. import pipeline as pipeline_module

        destination = outer / "generated" / "numbers.json"
        with (
            patch.object(
                pipeline_module,
                "run",
                side_effect=AssertionError("report attempted pipeline execution"),
            ),
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("report attempted a network connection"),
            ),
            patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("report attempted a network connection"),
            ),
        ):
            generated = generate(base, destination, fixed_time)
        after = _tree_hashes(base)
        parsed = _load_json(generated)
        crlf_text = outer / "eol-crlf.txt"
        lf_text = outer / "eol-lf.txt"
        binary = outer / "eol-binary.bin"
        crlf_text.write_bytes(b"a,b\r\n1,2\r\n")
        lf_text.write_bytes(b"a,b\n1,2\n")
        binary.write_bytes(b"\0\r\n\0")

        checks: list[tuple[str, bool]] = [
            ("text source fingerprints match across CRLF and LF checkouts", _fingerprint(crlf_text) == _fingerprint(lf_text)),
            ("binary source fingerprints keep every byte", _fingerprint(binary) == (_sha256(binary), binary.stat().st_size)),
            ("a complete realistic offline fixture builds", bool(good)),
            ("the schema is stable and versioned", good.get("schema_version") == SCHEMA_VERSION),
            ("all agreed semantic paper keys are mapped", set(good["paper_keys"]) == set(EXPECTED_PAPER_KEYS)),
            ("the paper key catalogue contains exactly the agreed 28 mappings", len(EXPECTED_PAPER_KEYS) == 28),
            ("fixed evidence and timestamp produce deterministic JSON", encoded == repeated),
            ("production generation never called the pipeline or network", generated.is_file()),
            ("production generation did not mutate a source artifact", before == after),
            ("the generated JSON is strict and round-trips", parsed == good),
            ("the generated artifact stays outside the production path during check", generated != base / OUTPUT_RELATIVE),
            ("the generated artifact contains only portable registered source paths", all("\\" not in key and not Path(key).is_absolute() for key in good["sources"])),
            ("fixture and live robustness rows remain separately labeled", set(good["robustness"]["rows_by_source"]) == {"fixture", "live"}),
            ("missing agent behaviour is explicit rather than zero-filled", good["agent_behaviour"]["status"] == "missing_evidence"),
        ]

        checks.extend(
            [
                (
                    "missing manifest is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "missing-manifest",
                        _delete_mutation(MANIFEST_RELATIVE),
                        "missing source file",
                    ),
                ),
                (
                    "malformed JSON is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "malformed-json",
                        _malformed_json_mutation(MANIFEST_RELATIVE),
                        "malformed JSON",
                    ),
                ),
                (
                    "a mismatched primary trade-off copy is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "tradeoff-copy",
                        _mismatched_tradeoff_mutation,
                        "mismatched primary trade-off copy",
                    ),
                ),
                (
                    "a wrong transfer run ID is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "run-id",
                        _json_mutation(
                            TRANSFER_REPORT_RELATIVE,
                            lambda payload: payload.__setitem__("run_id", "wrong"),
                        ),
                        "wrong transfer run ID",
                    ),
                ),
                (
                    "a non-completed transfer is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "transfer-status",
                        _json_mutation(
                            TRANSFER_REPORT_RELATIVE,
                            lambda payload: payload.__setitem__("status", "failed"),
                        ),
                        "transfer not completed",
                    ),
                ),
                (
                    "a changed primary snapshot is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "primary-changed",
                        _json_mutation(
                            TRANSFER_REPORT_RELATIVE,
                            lambda payload: payload["safety_verification"].__setitem__(
                                "primary_snapshot_unchanged", False
                            ),
                        ),
                        "primary snapshot not unchanged",
                    ),
                ),
                (
                    "unrestored configuration is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "config-not-restored",
                        _json_mutation(
                            TRANSFER_REPORT_RELATIVE,
                            lambda payload: payload["safety_verification"].__setitem__(
                                "config_paths_restored", False
                            ),
                        ),
                        "configuration not restored",
                    ),
                ),
                (
                    "missing dataset provenance is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "missing-provenance",
                        _json_mutation(
                            MANIFEST_RELATIVE,
                            lambda payload: payload["datasets"][0]["provenance"].pop(
                                "source_url"
                            ),
                        ),
                        "missing dataset provenance",
                    ),
                ),
                (
                    "a wrong GEOID prefix is refused",
                    _fixture_failure(
                        base, cases, "wrong-geoid", _wrong_geoid_mutation, "wrong GEOID prefix"
                    ),
                ),
                (
                    "inconsistent tract and ACS counts are refused",
                    _fixture_failure(
                        base,
                        cases,
                        "acs-count",
                        _json_mutation(
                            MANIFEST_RELATIVE,
                            lambda payload: payload["datasets"][0]["provenance"].__setitem__(
                                "feature_count", 11
                            ),
                        ),
                        "inconsistent tract/ACS counts",
                    ),
                ),
                (
                    "a wrong pipeline row count is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "pipeline-rows",
                        _json_mutation(
                            TRANSFER_REPORT_RELATIVE,
                            lambda payload: payload["pipeline"]["row_counts"].__setitem__(
                                hazard.HAZARD_SCENARIOS[0].name, 1
                            ),
                        ),
                        "wrong pipeline row count",
                    ),
                ),
                (
                    "a missing scenario is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "missing-scenario",
                        _json_mutation(
                            TRANSFER_REPORT_RELATIVE,
                            lambda payload: payload["pipeline"].__setitem__(
                                "expected_scenarios", payload["pipeline"]["expected_scenarios"][:-1]
                            ),
                        ),
                        "missing scenario",
                    ),
                ),
                (
                    "a missing preset is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "missing-preset",
                        _json_mutation(
                            TRANSFER_REPORT_RELATIVE,
                            lambda payload: payload["pipeline"].__setitem__(
                                "expected_presets", payload["pipeline"]["expected_presets"][:-1]
                            ),
                        ),
                        "missing preset",
                    ),
                ),
                (
                    "an incorrect trade-off row count is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "tradeoff-rows",
                        _tradeoff_row_mutation,
                        "incorrect trade-off row count",
                    ),
                ),
                (
                    "NaN and Infinity are refused",
                    _fixture_failure(
                        base,
                        cases,
                        "nan",
                        _nan_json_mutation(FAULTS_RELATIVE),
                        "NaN/Infinity",
                    ),
                ),
                (
                    "an impossible robustness recovery rate is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "impossible-recovery-rate",
                        _json_mutation(
                            FAULTS_RELATIVE,
                            lambda payload: payload["rows"][0].__setitem__(
                                "recovery_rate", 2.0
                            ),
                        ),
                        "recovery rate",
                    ),
                ),
                (
                    "a generic numeric paper placeholder is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "generic-paper-key",
                        _paper_append_mutation(r"\TD{n}"),
                        "numeric paper placeholder with no semantic key",
                    ),
                ),
                (
                    "a duplicate paper key is refused",
                    _fixture_failure(
                        base,
                        cases,
                        "duplicate-paper-key",
                        _paper_append_mutation(r"\TD{abstract.primary_tract_count}"),
                        "duplicate key in paper placeholders",
                    ),
                ),
                (
                    "an absent evidence file is refused during aggregation",
                    _fixture_failure(
                        base,
                        cases,
                        "missing-faults",
                        _delete_mutation(FAULTS_RELATIVE),
                        "missing source file",
                    ),
                ),
                (
                    "a source removed after aggregation is detected",
                    _hash_change_refusal(base, cases, True),
                ),
                (
                    "a source hash change after aggregation is detected",
                    _hash_change_refusal(base, cases, False),
                ),
                (
                    "absolute path leakage is refused",
                    _payload_refuses(
                        good,
                        base,
                        lambda payload: payload["agent_behaviour"].__setitem__(
                            "reason", "C:\\private\\evidence.json"
                        ),
                        "absolute path leakage",
                    ),
                ),
                (
                    "a paper key without evidence is refused",
                    _payload_refuses(
                        good,
                        base,
                        lambda payload: payload["paper_keys"][EXPECTED_PAPER_KEYS[0]].__setitem__(
                            "derivation", ""
                        ),
                        "paper key with no evidence",
                    ),
                ),
                (
                    "geometry serialization is refused",
                    _payload_refuses(
                        good,
                        base,
                        lambda payload: payload["agent_behaviour"].__setitem__(
                            "geometry", "POINT (1 2)"
                        ),
                        "accidental geometry/coordinate serialization",
                    ),
                ),
                (
                    "coordinate-array serialization is refused",
                    _payload_refuses(
                        good,
                        base,
                        lambda payload: payload["agent_behaviour"].__setitem__(
                            "coordinates", [1.0, 2.0]
                        ),
                        "accidental geometry/coordinate serialization",
                    ),
                ),
            ]
        )
        checks += verify.discipline_checks(sys.modules[__name__])
        print(f"report check exercised {len(checks)} assertions")
        return verify.report(checks)


def main() -> int:
    """Write the production report and print a compact evidence summary."""
    destination = generate()
    payload = _load_json(destination)
    paper_keys = _mapping(payload.get("paper_keys"), "numbers.paper_keys")
    missing = _sequence(payload.get("missing_evidence"), "numbers.missing_evidence")
    todos = _sequence(payload.get("remaining_paper_todos"), "numbers.remaining_paper_todos")
    print(f"wrote {_portable(destination, config.PROJECT_ROOT)}")
    print(f"  {len(paper_keys)} quantitative paper placeholder(s) mapped")
    print(f"  {len(missing)} missing-evidence item(s), {len(todos)} prose TODO line(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
