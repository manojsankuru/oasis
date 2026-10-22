"""Generate the two repository-owned data figures for the paper from existing evidence.

This module is deliberately downstream of the analysis.  It reads the primary
snapshot, the already-written risk and trade-off tables, and the canonical
transfer report.  It never retrieves data and never rebuilds a pipeline result.
Geometry is used only to draw the static risk map; no coordinate is serialized
to a model-facing artifact.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import math
import os
import re
import socket
import sys
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, Sequence
from unittest import mock

# Matplotlib otherwise tries to lock the interactive user's cache even though
# this module is intentionally headless.  The system temporary directory is the
# appropriate writable home for its disposable font index.
os.environ.setdefault(
    "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "geo-agent-matplotlib-cache")
)

import matplotlib

matplotlib.use("Agg")

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from matplotlib.axes import Axes
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize, PowerNorm
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator
from pyproj import CRS

from . import acquire, config, hazard, pipeline, verify, vulnerability
from .align import Alignment
from .contracts import Col, HazardScenario, WeightPreset
from .registry import Registry
from .risk import DEFAULT_PRIORITY_UNITS, DEFAULT_VULNERABLE_QUANTILE


FIGURE_DIRECTORY = config.PROJECT_ROOT / "paper" / "figs"
EXPECTED_FIGURES: tuple[str, ...] = (
    "risk_surface.pdf",
    "tradeoff.pdf",
)
HAND_MADE_FIGURES: tuple[str, ...] = ("architecture.pdf",)

TRACT_GEOID_SUFFIX_WIDTH = 6
PDF_PAGE_PATTERN = re.compile(rb"/Type\s*/Page(?!s)\b")
TRANSFER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

TRADEOFF_COLUMNS: tuple[str, ...] = (
    "preset",
    "scenario",
    "population_in_priority",
    "vulnerable_population_in_priority",
    "mean_inundation_m",
    "top_geoids",
    "displaced_geoids",
)
TRADEOFF_NUMERIC_COLUMNS: tuple[str, ...] = (
    "population_in_priority",
    "vulnerable_population_in_priority",
    "mean_inundation_m",
)

# Okabe-Ito hues remain separable under the common red/green deficiencies.
SCENARIO_COLOURS: tuple[str, ...] = ("#0072B2", "#E69F00", "#D55E00")
PRESET_MARKERS: tuple[str, ...] = ("o", "s", "^")


@dataclass(frozen=True, slots=True)
class TransferSource:
    """Validated location and label of the canonical transfer evidence."""

    run_id: str
    area_name: str
    directory: Path
    tradeoff_path: Path
    report_path: Path


@dataclass(frozen=True, slots=True)
class RiskSurface:
    """Local arrays and joined attributes used by the risk figure."""

    joined: gpd.GeoDataFrame
    depth: np.ma.MaskedArray
    raster_bounds: tuple[float, float, float, float]
    scenario: HazardScenario
    preset: WeightPreset
    top_geoids: tuple[str, ...]
    raster_crs: str
    raster_masked_cells: int


@dataclass(frozen=True, slots=True)
class FigureInputs:
    """All validated evidence needed by the two renderers."""

    primary_name: str
    transfer_name: str
    risk: RiskSurface
    primary_tradeoff: pd.DataFrame
    transfer_tradeoff: pd.DataFrame
    source_paths: tuple[Path, ...]
    transfer_run_id: str


class NetworkBlocker:
    """Count and refuse any accidental socket opened during generation."""

    def __init__(self) -> None:
        self.attempts = 0

    def refuse(self, *args: Any, **kwargs: Any) -> None:
        self.attempts += 1
        raise RuntimeError("network disabled while generating paper figures")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _read_json(path: Path) -> dict[str, Any]:
    _require(path.is_file(), f"required JSON evidence is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read strict JSON evidence {path}: {exc}") from exc
    _require(isinstance(payload, dict), f"{path} must contain a JSON object")
    return payload


def _repo_path(value: str, *, label: str) -> Path:
    candidate = Path(value)
    _require(not candidate.is_absolute(), f"{label} must be repository-relative")
    _require(".." not in candidate.parts, f"{label} may not escape the repository")
    resolved = (config.PROJECT_ROOT / candidate).resolve()
    try:
        resolved.relative_to(config.PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside the repository") from exc
    return resolved


def _resolve_transfer_source(payload: dict[str, Any], report_path: Path) -> TransferSource:
    _require(payload.get("schema_version") == 1, "transfer report schema_version must be 1")
    _require(
        payload.get("status") == "completed" and payload.get("stage") == "complete",
        "canonical transfer report must be completed/complete",
    )
    run_id = payload.get("run_id")
    _require(
        isinstance(run_id, str) and bool(TRANSFER_ID_PATTERN.fullmatch(run_id)),
        "canonical transfer report has an unsafe or missing run_id",
    )

    area = payload.get("study_area")
    _require(isinstance(area, dict), "canonical transfer report has no study_area object")
    expected_area = config.TRANSFER_AREA
    for key, expected in (
        ("name", expected_area.name),
        ("state_fips", expected_area.state_fips),
        ("county_fips", expected_area.county_fips),
        ("working_crs", expected_area.working_crs),
    ):
        _require(area.get(key) == expected, f"transfer study_area.{key} does not match configuration")

    raw_directory = payload.get("pipeline_output_path")
    _require(isinstance(raw_directory, str), "transfer report has no pipeline_output_path")
    directory = _repo_path(raw_directory, label="pipeline_output_path")
    transfer_root = (config.PAPER_DIR / "transfer").resolve()
    try:
        directory.relative_to(transfer_root)
    except ValueError as exc:
        raise ValueError("pipeline_output_path is outside outputs/paper/transfer") from exc
    _require(directory.name == run_id, "pipeline_output_path does not end in the canonical run_id")
    _require(directory.is_dir(), f"canonical transfer output directory is missing: {directory}")

    pipeline_evidence = payload.get("pipeline")
    _require(isinstance(pipeline_evidence, dict), "completed transfer has no pipeline evidence")
    scenarios = tuple(item.name for item in hazard.HAZARD_SCENARIOS)
    presets = tuple(item.name for item in vulnerability.WEIGHT_PRESETS)
    _require(
        tuple(pipeline_evidence.get("expected_scenarios") or ()) == scenarios,
        "transfer report scenario set differs from production configuration",
    )
    _require(
        tuple(pipeline_evidence.get("expected_presets") or ()) == presets,
        "transfer report preset set differs from production configuration",
    )
    validation = pipeline_evidence.get("artifact_validation")
    _require(isinstance(validation, dict), "completed transfer has no artifact validation")
    _require(
        validation.get("tradeoff_row_count") == len(scenarios) * len(presets),
        "canonical transfer report does not validate the complete trade-off table",
    )

    tradeoff_path = directory / pipeline.TRADEOFF_NAME
    _require(tradeoff_path.is_file(), f"canonical transfer trade-off is missing: {tradeoff_path}")
    written = payload.get("written_files")
    _require(isinstance(written, list), "completed transfer report has no written_files list")
    written_paths = {
        _repo_path(item, label="written_files entry")
        for item in written
        if isinstance(item, str)
    }
    _require(tradeoff_path.resolve() in written_paths, "canonical trade-off is absent from written_files")
    return TransferSource(
        run_id=run_id,
        area_name=str(area["name"]),
        directory=directory,
        tradeoff_path=tradeoff_path,
        report_path=report_path,
    )


def _canonical_transfer_source() -> TransferSource:
    report_path = config.PAPER_DIR / "transfer_report.json"
    return _resolve_transfer_source(_read_json(report_path), report_path)


def _split_geoids(value: Any) -> tuple[str, ...]:
    text = str(value).strip()
    return tuple(text.split()) if text else ()


def _validate_tradeoff_frame(
    frame: pd.DataFrame,
    *,
    geoid_prefix: str,
    label: str,
) -> pd.DataFrame:
    scenarios = tuple(item.name for item in hazard.HAZARD_SCENARIOS)
    presets = tuple(item.name for item in vulnerability.WEIGHT_PRESETS)
    expected_rows = len(scenarios) * len(presets)
    _require(tuple(frame.columns) == TRADEOFF_COLUMNS, f"{label} has the wrong columns")
    _require(len(frame) == expected_rows, f"{label} must have exactly {expected_rows} rows")

    cleaned = frame.copy()
    for column in TRADEOFF_NUMERIC_COLUMNS:
        try:
            cleaned[column] = pd.to_numeric(cleaned[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}.{column} is not numeric") from exc
        values = cleaned[column].to_numpy(dtype="float64")
        _require(bool(np.isfinite(values).all()), f"{label}.{column} contains NaN or Infinity")
        _require(bool((values >= 0.0).all()), f"{label}.{column} contains a negative value")

    observed_pairs = list(cleaned[["scenario", "preset"]].itertuples(index=False, name=None))
    expected_pairs = [(scenario, preset) for scenario in scenarios for preset in presets]
    _require(len(set(observed_pairs)) == expected_rows, f"{label} repeats a scenario/preset pair")
    _require(set(observed_pairs) == set(expected_pairs), f"{label} is missing a scenario or preset")

    geoid_width = len(geoid_prefix) + TRACT_GEOID_SUFFIX_WIDTH
    for index, row in cleaned.iterrows():
        top = _split_geoids(row["top_geoids"])
        displaced = _split_geoids(row["displaced_geoids"])
        _require(
            len(top) == DEFAULT_PRIORITY_UNITS and len(set(top)) == len(top),
            f"{label} row {index} does not name {DEFAULT_PRIORITY_UNITS} unique priority GEOIDs",
        )
        _require(len(set(displaced)) == len(displaced), f"{label} row {index} repeats a displaced GEOID")
        _require(not set(top) & set(displaced), f"{label} row {index} overlaps priority and displaced GEOIDs")
        for geoid in (*top, *displaced):
            _require(
                len(geoid) == geoid_width and geoid.startswith(geoid_prefix),
                f"{label} row {index} contains a GEOID outside its configured study area",
            )
    return cleaned


def _read_tradeoff(path: Path, *, geoid_prefix: str, label: str) -> pd.DataFrame:
    _require(path.is_file(), f"required trade-off evidence is missing: {path}")
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, UnicodeError, pd.errors.ParserError) as exc:
        raise ValueError(f"cannot read trade-off evidence {path}: {exc}") from exc
    return _validate_tradeoff_frame(frame, geoid_prefix=geoid_prefix, label=label)


def _middle_scenario() -> HazardScenario:
    ordered = sorted(hazard.HAZARD_SCENARIOS, key=lambda item: item.surge_height_m)
    _require(len(ordered) == 3, "the paper risk figure requires exactly three configured scenarios")
    return ordered[len(ordered) // 2]


def _validate_risk_frame(frame: pd.DataFrame, tract_geoids: set[str]) -> pd.DataFrame:
    _require(tuple(frame.columns) == pipeline.REPORTED_COLUMNS, "primary risk table has the wrong columns")
    _require(len(frame) == len(tract_geoids), f"primary risk table must have exactly {len(tract_geoids)} rows, one per tract")
    _require(not frame[Col.GEOID].isna().any(), "primary risk table has a null GEOID")
    _require(frame[Col.GEOID].is_unique, "primary risk GEOIDs must be unique")

    cleaned = frame.copy()
    cleaned[Col.GEOID] = cleaned[Col.GEOID].astype("string")
    _require(set(cleaned[Col.GEOID]) == tract_geoids, "primary tract/risk GEOID join has unmatched rows")
    for column in pipeline.REPORTED_COLUMNS:
        if column == Col.GEOID:
            continue
        try:
            cleaned[column] = pd.to_numeric(cleaned[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"primary risk column {column!r} is not numeric") from exc
        present = cleaned[column].dropna().to_numpy(dtype="float64")
        _require(bool(np.isfinite(present).all()), f"primary risk column {column!r} contains Infinity")

    score_missing = cleaned[Col.RISK_SCORE].isna()
    rank_missing = cleaned[Col.PRIORITY_RANK].isna()
    _require(score_missing.equals(rank_missing), "risk score and priority rank nullness disagree")
    _require(
        score_missing.equals(cleaned[Col.VULNERABILITY].isna()),
        "risk and vulnerability missingness disagree",
    )
    ranks = cleaned.loc[~rank_missing, Col.PRIORITY_RANK]
    expected_ranks = list(range(1, len(ranks) + 1))
    observed_ranks = sorted(int(value) for value in ranks)
    _require(observed_ranks == expected_ranks, "priority ranks are not a complete one-based sequence")
    ordered = cleaned.dropna(subset=[Col.PRIORITY_RANK]).sort_values(Col.PRIORITY_RANK)
    _require(
        ordered[Col.RISK_SCORE].is_monotonic_decreasing,
        "priority rank does not follow descending risk score",
    )
    return cleaned


def _selected_tradeoff_row(
    frame: pd.DataFrame, scenario: HazardScenario, preset: WeightPreset
) -> pd.Series:
    selected = frame.loc[
        (frame["scenario"] == scenario.name) & (frame["preset"] == preset.name)
    ]
    _require(len(selected) == 1, "primary trade-off does not identify the selected scenario/preset")
    return selected.iloc[0]


def _recorded_tract_count() -> int:
    registry = Registry(study_area=config.STUDY_AREA)
    registry.load_manifest()
    return int(registry.provenance_of(acquire.DATASET_TRACTS).feature_count)


def _load_risk_surface(primary_tradeoff: pd.DataFrame) -> tuple[RiskSurface, tuple[Path, ...]]:
    scenario = _middle_scenario()
    preset = vulnerability.DEFAULT_PRESET
    _require(preset in vulnerability.WEIGHT_PRESETS, "default preset is outside the configured preset set")

    registry = Registry(study_area=config.STUDY_AREA)
    registry.load_manifest()
    aligner = Alignment(registry)
    tracts = aligner.to_working_crs(registry.load(acquire.DATASET_TRACTS))
    _require(isinstance(tracts, gpd.GeoDataFrame), "registered tract evidence is not spatial")
    expected_tracts = _recorded_tract_count()
    _require(len(tracts) == expected_tracts, f"primary tract layer must have the {expected_tracts} rows its provenance records")
    _require(tracts.crs is not None, "primary tract layer has no CRS")
    _require(
        CRS.from_user_input(tracts.crs) == CRS.from_user_input(config.STUDY_AREA.working_crs),
        "tract geometry did not pass through the configured working CRS",
    )
    _require(Col.GEOID in tracts.columns, "primary tract layer has no GEOID")
    tract_geoids = tracts[Col.GEOID].astype("string")
    _require(tract_geoids.is_unique, "primary tract GEOIDs must be unique")
    expected_width = len(config.STUDY_AREA.county_geoid) + TRACT_GEOID_SUFFIX_WIDTH
    _require(
        bool(tract_geoids.str.startswith(config.STUDY_AREA.county_geoid).all())
        and bool((tract_geoids.str.len() == expected_width).all()),
        "primary tract GEOIDs do not match the configured study area",
    )
    geometry_name = tracts.geometry.name
    geometry = gpd.GeoDataFrame(
        {Col.GEOID: tract_geoids, geometry_name: tracts.geometry},
        geometry=geometry_name,
        crs=tracts.crs,
    )

    risk_path = config.OUTPUTS_DIR / pipeline.table_name(scenario, preset)
    _require(risk_path.is_file(), f"selected primary risk table is missing: {risk_path}")
    try:
        raw_risk = pd.read_csv(risk_path, dtype={Col.GEOID: "string"})
    except (OSError, UnicodeError, pd.errors.ParserError) as exc:
        raise ValueError(f"cannot read primary risk evidence {risk_path}: {exc}") from exc
    risk_frame = _validate_risk_frame(raw_risk, set(tract_geoids))
    joined = geometry.merge(risk_frame, on=Col.GEOID, how="outer", validate="one_to_one", indicator=True)
    _require(len(joined) == len(tracts), "primary tract/risk join changed the row count")
    _require(bool((joined["_merge"] == "both").all()), "primary tract/risk join left unmatched GEOIDs")
    joined = joined.drop(columns=["_merge"])

    selected = _selected_tradeoff_row(primary_tradeoff, scenario, preset)
    top_geoids = _split_geoids(selected["top_geoids"])
    ranked_top = tuple(
        risk_frame.dropna(subset=[Col.PRIORITY_RANK])
        .sort_values(Col.PRIORITY_RANK)
        .head(DEFAULT_PRIORITY_UNITS)[Col.GEOID]
        .astype(str)
    )
    _require(top_geoids == ranked_top, "selected risk table top ten disagrees with trade-off evidence")
    top = risk_frame.loc[risk_frame[Col.GEOID].isin(top_geoids)]
    _require(
        int(top[Col.POPULATION].sum()) == int(selected["population_in_priority"]),
        "selected risk table priority population disagrees with trade-off evidence",
    )
    vulnerable_threshold = float(risk_frame[Col.VULNERABILITY].quantile(DEFAULT_VULNERABLE_QUANTILE))
    vulnerable_population = int(
        top.loc[top[Col.VULNERABILITY] >= vulnerable_threshold, Col.POPULATION].sum()
    )
    _require(
        vulnerable_population == int(selected["vulnerable_population_in_priority"]),
        "selected risk table vulnerable priority population disagrees with trade-off evidence",
    )
    _require(
        math.isclose(
            float(top[Col.INUNDATION_MEAN_M].mean()),
            float(selected["mean_inundation_m"]),
            rel_tol=1e-12,
            abs_tol=1e-12,
        ),
        "selected risk table mean depth disagrees with trade-off evidence",
    )

    depth_path = config.DERIVED_DIR / f"{scenario.name}{hazard.DEPTH_SUFFIX}.tif"
    _require(depth_path.is_file(), f"selected inundation-depth raster is missing: {depth_path}")
    with rasterio.open(depth_path) as source:
        _require(source.count == 1 and source.width > 0 and source.height > 0, "depth raster has an invalid shape")
        _require(source.crs is not None, "depth raster has no CRS")
        raster_crs = CRS.from_user_input(source.crs)
        working_crs = CRS.from_user_input(config.DEFAULT_WORKING_CRS)
        _require(raster_crs == working_crs, "depth raster is not in the required working CRS")
        _require(raster_crs == CRS.from_user_input(tracts.crs), "depth raster and tracts use different CRSs")
        _require(
            source.transform.a > 0.0
            and source.transform.e < 0.0
            and source.transform.b == 0.0
            and source.transform.d == 0.0,
            "depth raster transform is rotated or vertically mirrored",
        )
        depth = source.read(1, masked=True)
        bounds = (
            float(source.bounds.left),
            float(source.bounds.bottom),
            float(source.bounds.right),
            float(source.bounds.top),
        )
        raster_crs_text = source.crs.to_string()

    compressed = depth.compressed()
    _require(compressed.size > 0, "depth raster contains no usable cells")
    _require(bool(np.isfinite(compressed).all()), "depth raster contains NaN or Infinity")
    _require(bool((compressed >= 0.0).all()), "depth raster contains a negative depth")
    _require(bool((compressed > 0.0).any()), "depth raster contains no inundated cell")
    tract_bounds = tuple(float(value) for value in joined.total_bounds)
    _require(
        bounds[0] <= tract_bounds[0]
        and bounds[1] <= tract_bounds[1]
        and bounds[2] >= tract_bounds[2]
        and bounds[3] >= tract_bounds[3],
        "depth raster does not cover the primary tract extent",
    )

    surface = RiskSurface(
        joined=joined,
        depth=depth,
        raster_bounds=bounds,
        scenario=scenario,
        preset=preset,
        top_geoids=top_geoids,
        raster_crs=raster_crs_text,
        raster_masked_cells=int(np.ma.getmaskarray(depth).sum()),
    )
    return surface, (registry.manifest_path, registry.path_of(acquire.DATASET_TRACTS), risk_path, depth_path)


def _load_inputs() -> FigureInputs:
    transfer = _canonical_transfer_source()
    primary_tradeoff_path = config.PAPER_DIR / pipeline.TRADEOFF_NAME
    pipeline_tradeoff_path = config.OUTPUTS_DIR / pipeline.TRADEOFF_NAME
    _require(pipeline_tradeoff_path.is_file(), f"primary pipeline trade-off is missing: {pipeline_tradeoff_path}")
    _require(primary_tradeoff_path.is_file(), f"primary paper trade-off is missing: {primary_tradeoff_path}")
    _require(
        primary_tradeoff_path.read_bytes() == pipeline_tradeoff_path.read_bytes(),
        "primary paper trade-off is not byte-identical to the pipeline artifact",
    )
    primary_tradeoff = _read_tradeoff(
        primary_tradeoff_path,
        geoid_prefix=config.STUDY_AREA.county_geoid,
        label="primary trade-off",
    )
    transfer_tradeoff = _read_tradeoff(
        transfer.tradeoff_path,
        geoid_prefix=config.TRANSFER_AREA.county_geoid,
        label="transfer trade-off",
    )
    risk_surface, risk_paths = _load_risk_surface(primary_tradeoff)
    sources = (
        *risk_paths,
        pipeline_tradeoff_path,
        primary_tradeoff_path,
        transfer.report_path,
        transfer.tradeoff_path,
    )
    _require(all(path.is_file() for path in sources), "a validated figure source disappeared")
    return FigureInputs(
        primary_name=config.STUDY_AREA.name,
        transfer_name=transfer.area_name,
        risk=risk_surface,
        primary_tradeoff=primary_tradeoff,
        transfer_tradeoff=transfer_tradeoff,
        source_paths=tuple(dict.fromkeys(path.resolve() for path in sources)),
        transfer_run_id=transfer.run_id,
    )


@contextmanager
def _offline() -> Iterator[NetworkBlocker]:
    blocker = NetworkBlocker()
    with (
        mock.patch.object(socket.socket, "connect", blocker.refuse),
        mock.patch.object(socket, "create_connection", blocker.refuse),
    ):
        yield blocker


def _save_pdf(figure: Figure, path: Path, *, title: str) -> None:
    metadata: dict[str, Any] = {
        "Title": title,
        "Author": "geo-agent",
        "Creator": "src.figures",
        "CreationDate": None,
        "ModDate": None,
    }
    figure.savefig(path, format="pdf", bbox_inches="tight", metadata=metadata)


def _map_axis(axis: Axes) -> None:
    axis.set_aspect("equal")
    axis.set_xticks([])
    axis.set_yticks([])
    for spine in axis.spines.values():
        spine.set_visible(False)


def _risk_surface_figure(path: Path, evidence: RiskSurface) -> None:
    figure, axes = plt.subplots(3, 1, figsize=(3.35, 8.30))
    try:
        joined = evidence.joined
        depth_cmap = matplotlib.colormaps["Blues"].copy()
        depth_cmap.set_bad("#BDBDBD")
        positive = evidence.depth.compressed()
        depth_norm = PowerNorm(gamma=0.62, vmin=0.0, vmax=float(positive.max()))
        image = axes[0].imshow(
            evidence.depth,
            extent=(
                evidence.raster_bounds[0],
                evidence.raster_bounds[2],
                evidence.raster_bounds[1],
                evidence.raster_bounds[3],
            ),
            origin="upper",
            interpolation="nearest",
            cmap=depth_cmap,
            norm=depth_norm,
            rasterized=True,
        )
        joined.plot(ax=axes[0], facecolor="none", edgecolor="#333333", linewidth=0.20)
        axes[0].set_title(f"(a) Depth raster - {evidence.scenario.surge_height_m:g} m scenario", fontsize=8.2)
        depth_bar = figure.colorbar(image, ax=axes[0], orientation="horizontal", fraction=0.050, pad=0.025)
        depth_bar.set_label("Inundation depth (m)", fontsize=7.2)
        depth_bar.ax.tick_params(labelsize=6.4)
        _map_axis(axes[0])

        vulnerability_cmap = matplotlib.colormaps["viridis"].copy()
        joined.plot(
            column=Col.VULNERABILITY,
            ax=axes[1],
            cmap=vulnerability_cmap,
            vmin=0.0,
            vmax=1.0,
            edgecolor="white",
            linewidth=0.18,
            missing_kwds={"color": "#BDBDBD", "edgecolor": "#555555", "hatch": "///"},
        )
        vulnerability_map = ScalarMappable(norm=Normalize(0.0, 1.0), cmap=vulnerability_cmap)
        vulnerability_bar = figure.colorbar(
            vulnerability_map, ax=axes[1], orientation="horizontal", fraction=0.050, pad=0.025
        )
        vulnerability_bar.set_label("Vulnerability index (0-1)", fontsize=7.2)
        vulnerability_bar.ax.tick_params(labelsize=6.4)
        axes[1].set_title("(b) Tract vulnerability", fontsize=8.2)
        axes[1].legend(
            handles=[Patch(facecolor="#BDBDBD", edgecolor="#555555", hatch="///", label="Unscored")],
            loc="lower left",
            fontsize=6.2,
            frameon=True,
        )
        _map_axis(axes[1])

        risk_cmap = matplotlib.colormaps["cividis"].copy()
        joined.plot(
            column=Col.RISK_SCORE,
            ax=axes[2],
            cmap=risk_cmap,
            vmin=0.0,
            vmax=1.0,
            edgecolor="white",
            linewidth=0.18,
            missing_kwds={"color": "#BDBDBD", "edgecolor": "#555555", "hatch": "///"},
        )
        priority = joined.loc[joined[Col.GEOID].astype(str).isin(evidence.top_geoids)]
        priority.plot(ax=axes[2], facecolor="none", edgecolor="#000000", linewidth=1.45)
        priority.plot(ax=axes[2], facecolor="none", edgecolor="#D55E00", linewidth=0.75)
        risk_map = ScalarMappable(norm=Normalize(0.0, 1.0), cmap=risk_cmap)
        risk_bar = figure.colorbar(risk_map, ax=axes[2], orientation="horizontal", fraction=0.050, pad=0.025)
        risk_bar.set_label("Risk score (area-relative, 0-1)", fontsize=7.2)
        risk_bar.ax.tick_params(labelsize=6.4)
        axes[2].set_title(
            f"(c) Combined risk - top {DEFAULT_PRIORITY_UNITS} outlined",
            fontsize=8.2,
        )
        axes[2].legend(
            handles=[
                Line2D([0], [0], color="#D55E00", linewidth=1.5, label="Priority tract"),
                Patch(facecolor="#BDBDBD", edgecolor="#555555", hatch="///", label="Unscored"),
            ],
            loc="lower left",
            fontsize=6.2,
            frameon=True,
        )
        _map_axis(axes[2])

        figure.subplots_adjust(left=0.02, right=0.98, top=0.975, bottom=0.095, hspace=0.25)
        figure.text(
            0.5,
            0.012,
            "Zonal statistics join raster hazard to tract-level\ndemographic and risk attributes.",
            ha="center",
            va="bottom",
            fontsize=6.4,
        )
        _save_pdf(figure, path, title="Raster-to-tract risk surface")
    finally:
        plt.close(figure)


def _scenario_style() -> dict[str, tuple[str, str]]:
    _require(
        len(hazard.HAZARD_SCENARIOS) == len(SCENARIO_COLOURS),
        "scenario colour set no longer covers every configured scenario",
    )
    return {
        scenario.name: (colour, f"{scenario.surge_height_m:g} m")
        for scenario, colour in zip(hazard.HAZARD_SCENARIOS, SCENARIO_COLOURS, strict=True)
    }


def _preset_style() -> dict[str, tuple[str, str]]:
    _require(
        len(vulnerability.WEIGHT_PRESETS) == len(PRESET_MARKERS),
        "preset marker set no longer covers every configured preset",
    )
    return {
        preset.name: (marker, preset.name.replace("_", " "))
        for preset, marker in zip(vulnerability.WEIGHT_PRESETS, PRESET_MARKERS, strict=True)
    }


def _thousands_tick(value: float, position: float | None = None) -> str:
    """Compact population ticks so a single-column panel stays legible."""
    del position
    return f"{value / 1000.0:g}k"


def _tradeoff_panel(
    axis: Axes,
    frame: pd.DataFrame,
    title: str,
    scenario_style: dict[str, tuple[str, str]],
    preset_style: dict[str, tuple[str, str]],
) -> None:
    preset_order = [preset.name for preset in vulnerability.WEIGHT_PRESETS]
    for scenario in hazard.HAZARD_SCENARIOS:
        subset = frame.loc[frame["scenario"] == scenario.name].copy()
        subset["_order"] = subset["preset"].map({name: index for index, name in enumerate(preset_order)})
        subset = subset.sort_values("_order")
        colour, _scenario_label = scenario_style[scenario.name]
        axis.plot(
            subset["population_in_priority"],
            subset["vulnerable_population_in_priority"],
            color=colour,
            linewidth=0.8,
            alpha=0.38,
            zorder=1,
        )
        for row in subset.itertuples(index=False):
            marker, _preset_label = preset_style[str(row.preset)]
            axis.scatter(
                float(row.population_in_priority),
                float(row.vulnerable_population_in_priority),
                color=colour,
                edgecolor="#1A1A1A",
                linewidth=0.55,
                marker=marker,
                s=34.0,
                zorder=2,
            )
    axis.set_title(title, fontsize=8.4, fontweight="bold")
    axis.set_xlabel("Population in priority set", fontsize=7.2)
    axis.set_ylabel("Vulnerable population in priority set", fontsize=7.2)
    axis.xaxis.set_major_locator(MaxNLocator(nbins=5))
    axis.yaxis.set_major_locator(MaxNLocator(nbins=5))
    axis.xaxis.set_major_formatter(FuncFormatter(_thousands_tick))
    axis.yaxis.set_major_formatter(FuncFormatter(_thousands_tick))
    axis.tick_params(labelsize=6.4)
    axis.grid(color="#D9D9D9", linewidth=0.45, alpha=0.8)
    axis.set_axisbelow(True)


def _tradeoff_figure(path: Path, inputs: FigureInputs) -> None:
    scenario_style = _scenario_style()
    preset_style = _preset_style()
    figure, axes = plt.subplots(2, 1, figsize=(3.35, 5.25), sharex=True, sharey=True)
    try:
        _tradeoff_panel(
            axes[0], inputs.primary_tradeoff, inputs.primary_name, scenario_style, preset_style
        )
        _tradeoff_panel(
            axes[1], inputs.transfer_tradeoff, inputs.transfer_name, scenario_style, preset_style
        )
        scenario_handles = [
            Line2D(
                [0],
                [0],
                color=scenario_style[item.name][0],
                marker="o",
                markersize=4.5,
                linewidth=1.0,
                label=scenario_style[item.name][1],
            )
            for item in hazard.HAZARD_SCENARIOS
        ]
        preset_handles = [
            Line2D(
                [0],
                [0],
                color="#444444",
                marker=preset_style[item.name][0],
                linestyle="None",
                markersize=5.0,
                label=preset_style[item.name][1],
            )
            for item in vulnerability.WEIGHT_PRESETS
        ]
        figure.legend(
            handles=[*scenario_handles, *preset_handles],
            loc="lower center",
            ncol=3,
            fontsize=6.1,
            frameon=False,
            bbox_to_anchor=(0.5, 0.058),
        )
        figure.subplots_adjust(left=0.20, right=0.97, top=0.96, bottom=0.19, hspace=0.30)
        figure.text(
            0.5,
            0.015,
            "Lines guide the eye across discrete presets; they are not continuous optimization frontiers.",
            ha="center",
            va="bottom",
            fontsize=5.9,
        )
        _save_pdf(figure, path, title="Priority population trade-offs")
    finally:
        plt.close(figure)


def _render_bundle(directory: Path, inputs: FigureInputs) -> tuple[Path, ...]:
    directory.mkdir(parents=True, exist_ok=True)
    _require(not any(directory.iterdir()), f"figure staging directory is not empty: {directory}")
    paths = tuple(directory / name for name in EXPECTED_FIGURES)
    _risk_surface_figure(paths[0], inputs.risk)
    _tradeoff_figure(paths[1], inputs)
    _validate_pdf_bundle(directory)
    return paths


def _pdf_page_count(path: Path) -> int:
    return len(PDF_PAGE_PATTERN.findall(path.read_bytes()))


def _validate_pdf_bundle(directory: Path) -> None:
    _require(directory.is_dir(), f"figure directory is missing: {directory}")
    observed = {path.name for path in directory.iterdir() if path.is_file()}
    _require(
        observed - set(HAND_MADE_FIGURES) == set(EXPECTED_FIGURES),
        "figure directory does not contain exactly the expected generated files",
    )
    for name in sorted(observed):
        path = directory / name
        _require(path.stat().st_size > 0, f"{name} is empty")
        with path.open("rb") as handle:
            _require(handle.read(4) == b"%PDF", f"{name} does not begin with PDF magic")
        _require(_pdf_page_count(path) >= 1, f"{name} contains no PDF page")


def _directory_fingerprints(directory: Path) -> dict[str, str]:
    if not directory.exists():
        return {}
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _file_fingerprints(paths: Sequence[Path]) -> dict[str, str]:
    return {
        path.resolve().relative_to(config.PROJECT_ROOT.resolve()).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


def _destination_is_safe(directory: Path) -> None:
    if not directory.exists():
        return
    observed = {path.name for path in directory.iterdir()}
    unexpected = observed - set(EXPECTED_FIGURES) - set(HAND_MADE_FIGURES)
    _require(not unexpected, f"refusing to overwrite a figure directory with unexpected entries: {sorted(unexpected)}")


def generate(directory: Path = FIGURE_DIRECTORY) -> tuple[Path, ...]:
    """Validate local evidence, render in a staging directory, then replace PDFs."""
    destination = Path(directory)
    _destination_is_safe(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _offline() as blocker:
        inputs = _load_inputs()
        with tempfile.TemporaryDirectory(prefix="figures-stage-", dir=destination.parent) as temporary:
            staging = Path(temporary)
            staged = _render_bundle(staging, inputs)
            destination.mkdir(parents=True, exist_ok=True)
            for source in staged:
                os.replace(source, destination / source.name)
        _validate_pdf_bundle(destination)
    _require(blocker.attempts == 0, "figure generation attempted a network connection")
    return tuple(destination / name for name in EXPECTED_FIGURES)


def _forbidden_call_checks(module: ModuleType) -> list[tuple[str, bool]]:
    source = inspect.getsource(module)
    tree = ast.parse(source)
    pipeline_runs = 0
    acquisition_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "pipeline" and node.func.attr == "run":
            pipeline_runs += 1
        if isinstance(owner, ast.Name) and owner.id == "acquire":
            acquisition_calls += 1
    return [
        ("the figure module never calls the pipeline runner", pipeline_runs == 0),
        ("the figure module never calls acquisition", acquisition_calls == 0),
    ]


def _negative_guard_checks(inputs: FigureInputs) -> list[tuple[str, bool]]:
    risk = inputs.risk.joined.drop(columns=[inputs.risk.joined.geometry.name]).copy()
    duplicate = risk.copy()
    duplicate.loc[duplicate.index[1], Col.GEOID] = duplicate.loc[duplicate.index[0], Col.GEOID]
    short = risk.iloc[:-1].copy()
    null_mismatch = risk.copy()
    scored_index = null_mismatch[Col.RISK_SCORE].first_valid_index()
    _require(scored_index is not None, "risk fixture has no scored row")
    null_mismatch.loc[scored_index, Col.PRIORITY_RANK] = np.nan

    transfer_payload = _read_json(config.PAPER_DIR / "transfer_report.json")
    failed_transfer = deepcopy(transfer_payload)
    failed_transfer["status"] = "failed"
    escaped_transfer = deepcopy(transfer_payload)
    escaped_transfer["pipeline_output_path"] = "../outside"

    primary_prefix = config.STUDY_AREA.county_geoid
    missing_tradeoff = inputs.primary_tradeoff.iloc[:-1].copy()
    return [
        (
            "duplicate risk GEOIDs are refused",
            verify.refuses(
                lambda: _validate_risk_frame(duplicate, set(risk[Col.GEOID].astype(str))),
                ValueError,
                "unique",
            ),
        ),
        (
            "a short risk table is refused",
            verify.refuses(
                lambda: _validate_risk_frame(short, set(risk[Col.GEOID].astype(str))),
                ValueError,
                "exactly",
            ),
        ),
        (
            "risk/rank nullness disagreement is refused",
            verify.refuses(
                lambda: _validate_risk_frame(null_mismatch, set(risk[Col.GEOID].astype(str))),
                ValueError,
                "nullness",
            ),
        ),
        (
            "an incomplete trade-off table is refused",
            verify.refuses(
                lambda: _validate_tradeoff_frame(
                    missing_tradeoff,
                    geoid_prefix=primary_prefix,
                    label="mutated trade-off",
                ),
                ValueError,
                "exactly",
            ),
        ),
        (
            "a non-completed canonical transfer is refused",
            verify.refuses(
                lambda: _resolve_transfer_source(
                    failed_transfer, config.PAPER_DIR / "transfer_report.json"
                ),
                ValueError,
                "completed/complete",
            ),
        ),
        (
            "a transfer path escaping the repository is refused",
            verify.refuses(
                lambda: _resolve_transfer_source(
                    escaped_transfer, config.PAPER_DIR / "transfer_report.json"
                ),
                ValueError,
                "escape",
            ),
        ),
    ]


def _self_check() -> int:
    try:
        with _offline() as load_blocker:
            inputs = _load_inputs()
        if load_blocker.attempts:
            raise ValueError("loading figure evidence attempted a network connection")
    except Exception as exc:
        print(f"figure evidence validation failed: {type(exc).__name__}: {exc}")
        return 1

    source_before = _file_fingerprints(inputs.source_paths)
    production_before = _directory_fingerprints(FIGURE_DIRECTORY)
    checks: list[tuple[str, bool]] = [
        ("the middle configured scenario drives the risk figure", inputs.risk.scenario == _middle_scenario()),
        ("the default configured preset drives the risk figure", inputs.risk.preset == vulnerability.DEFAULT_PRESET),
        ("the primary risk table has one row per recorded tract", len(inputs.risk.joined) == _recorded_tract_count()),
        ("the tract/risk join is one-to-one and complete", inputs.risk.joined[Col.GEOID].is_unique),
        (
            "risk score and priority rank nullness agree",
            inputs.risk.joined[Col.RISK_SCORE].isna().equals(
                inputs.risk.joined[Col.PRIORITY_RANK].isna()
            ),
        ),
        ("the depth raster uses the configured working CRS", inputs.risk.raster_crs == config.DEFAULT_WORKING_CRS),
        ("the risk figure outlines exactly the priority set", len(inputs.risk.top_geoids) == DEFAULT_PRIORITY_UNITS),
        ("the primary trade-off has nine validated rows", len(inputs.primary_tradeoff) == 9),
        ("the transfer trade-off has nine validated rows", len(inputs.transfer_tradeoff) == 9),
        ("the canonical transfer run identifier is present", bool(inputs.transfer_run_id)),
    ]

    with tempfile.TemporaryDirectory(prefix="figures-check-") as temporary:
        root = Path(temporary)
        first = root / "first"
        second = root / "second"
        try:
            first_paths = generate(first)
            second_paths = generate(second)
            first_hashes = _directory_fingerprints(first)
            second_hashes = _directory_fingerprints(second)
            _validate_pdf_bundle(first)
            _validate_pdf_bundle(second)
            checks.extend(
                [
                    ("check generation writes exactly the generated filenames", {item.name for item in first_paths} == set(EXPECTED_FIGURES)),
                    ("check generation never writes a hand-made figure", not any((first / name).exists() for name in HAND_MADE_FIGURES)),
                    ("every generated artifact begins with PDF magic", all(path.read_bytes()[:4] == b"%PDF" for path in first_paths)),
                    ("every generated PDF is nonempty", all(path.stat().st_size > 0 for path in first_paths)),
                    ("every generated PDF contains at least one page", all(_pdf_page_count(path) >= 1 for path in first_paths)),
                    ("repeated generation is byte-deterministic", first_hashes == second_hashes),
                    ("check directories are separate from production", first.resolve() != FIGURE_DIRECTORY.resolve() and second.resolve() != FIGURE_DIRECTORY.resolve()),
                ]
            )
        except Exception as exc:
            print(f"temporary figure generation failed: {type(exc).__name__}: {exc}")
            checks.append(("temporary figure generation succeeds", False))

    checks.extend(
        [
            ("the check did not change production figures", production_before == _directory_fingerprints(FIGURE_DIRECTORY)),
            ("the hand-made figures are present in production", all((FIGURE_DIRECTORY / name).is_file() for name in HAND_MADE_FIGURES)),
            ("figure generation did not mutate source artifacts", source_before == _file_fingerprints(inputs.source_paths)),
        ]
    )
    checks.extend(_negative_guard_checks(inputs))
    checks.extend(_forbidden_call_checks(sys.modules[__name__]))
    checks.extend(verify.discipline_checks(sys.modules[__name__]))
    return verify.report(checks)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate sources and generate twice in temporary directories only",
    )
    arguments = parser.parse_args(argv)
    if arguments.check:
        return _self_check()
    paths = generate()
    for path in paths:
        print(path.relative_to(config.PROJECT_ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
