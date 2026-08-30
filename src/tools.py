"""The LLM-visible tool surface: the eleven names in `contracts.TOOL_NAMES`.

Every tool here **reports** a number that something else computed. None of them
computes one. That is the rule the module is built around and it is worth stating
plainly, because it looks like a restriction and is actually the point.

**Why no tool recomputes.** `pipeline.run()` is the only thing in this system that
computes anything, and one run takes 41 seconds for three scenarios: it derives
three inundation surfaces over an eight-million-cell grid, measures every polygon
of two granularities against each, and builds four components per unit. A tool
that called it per invocation would make a six-iteration agent loop take four
minutes of wall clock, and would do it while producing exactly the same numbers
every time. So one `PipelineResult` is built on first need and every tool reads
from it -- see `analysis()`.

The second reason matters more than the speed. `critic.py` in S11 traces every
number in an answer back to a logged tool result, and a tool that arrived at its
number by its own route would be a second computation path: two answers to one
question, agreeing today and free to disagree after any edit. Recombining a
weighting through `Vulnerability.index` and `Risk.combine` -- the same two
functions `pipeline` itself calls -- is not a second path, and `_agreement_checks`
asserts on the real county that the tool route and the pipeline route return the
same units in the same order.

**What is not in the shared run.** `list_datasets`, `describe_layer` on a vector,
`acquire_dataset` and `ask_user_preferences` read the registry and the manifest
only, so the first thing a model does costs no analysis at all.

**Invariant 3 is checked on the serialised result, not on the intention.**
`coordinate_faults` walks the JSON a tool would send and refuses three shapes: a
key that could name a coordinate, a bare token in a VALUE that could name one --
`describe_layer` reporting `INTPTLAT` in a list of column names is a coordinate
reaching a model message even though no number moved -- and any list holding a
number or a list, which is the shape of a coordinate list itself. The pattern is
`pipeline.COORDINATE_PATTERN`, imported rather than rewritten, because TIGERweb
really does ship `CENTLAT`, `CENTLON`, `INTPTLAT` and `INTPTLON` as ordinary text
attributes on the layer `describe_layer` is most likely to be asked about, and
one regular expression that both the deliverable and the tool surface are held to
is one that cannot drift apart.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from . import acquire, align, config, pipeline, schemas, verify, vulnerability
from .contracts import (
    TOOL_NAMES,
    VULNERABILITY_INDICATORS,
    Col,
    HazardScenario,
    ToolFn,
    ToolResult,
    WeightPreset,
)
from .hazard import HAZARD_SCENARIOS
from .registry import Registry
from .risk import DEFAULT_PRIORITY_UNITS, Risk
from .vulnerability import DEFAULT_PRESET, WEIGHT_PRESETS, PRESETS

TOOL_FUNCTIONS: dict[str, ToolFn] = {}
"""Name to implementation, filled by the `@tool` decorator.

The key is the function's own `__name__`, so the executable name and the source
name are the same string rather than two strings somebody keeps in step. What is
still checked, because it is the thing that breaks, is that this set equals
`contracts.TOOL_NAMES` and equals what `schemas.py` advertises."""

CALLS: list[dict[str, Any]] = []
"""Every tool result this process produced, newest last.

Invariant 8 says every number the agent reports is traceable to a logged tool
result. `agent.py` writes the run log to disk for the transcript; this is the
in-process copy `validate_answer` hands the critic, so the tool that checks
traceability does not have to reach back into the loop that called it."""

MAX_CALLS = 200
"""How many results are kept. A bound rather than a growing list, because this
lives for the life of the process and an agent loop is bounded at a dozen turns."""

MAX_LIST = 25
"""Longest list of identifiers any result carries. Longer lists are cut and the
result says how many it left out -- a truncation nobody counts reads as an answer."""

MAX_NOTES = 6
"""Longest run of provenance notes a detail view carries, for the same reason."""

COORDINATE_PATTERN = pipeline.COORDINATE_PATTERN
"""Imported, never rewritten. `pipeline.reported` refuses to WRITE a column whose
name could carry a coordinate; this refuses to SEND one. Two rules with one
pattern cannot disagree about what a coordinate looks like."""

BARE_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""A string value that could be a column name rather than prose.

The coordinate pattern is written for column names and matches on underscore
boundaries, so applying it to a sentence produces false positives. Applying it to
values that look like identifiers catches the case that matters -- a column name
travelling inside a list -- and leaves prose alone."""

COORDINATE_TEXT = re.compile(r"-?\d{1,3}\.\d{4,}[\s,]+-?\d{1,3}\.\d{4,}")
"""Two decimal degrees side by side anywhere in a sentence.

The third route a coordinate takes, and the one that got through. A `Provenance`
note is free text, written by the retrieval to record what it had to work around,
and `acquire.fetch_osm` records the study extent by printing it -- the bounding
box in both coordinate orders, to fifteen decimal places, in prose. It is neither
a key, nor a bare token, nor a list of numbers, so the two rules above see nothing
and eight coordinates reach the model inside a paragraph about coordinate order.

Written narrowly on purpose: a signed number with at least four decimal places,
next to another one. Populations, depths, percentages and fractions in this
project do not take that shape, and a pair of them is a location in a way that a
single number is not."""

BACKING_MODULES: dict[str, str] = {
    "run_spatial_code": "sandbox",
    "validate_answer": "critic",
}
"""Which module implements the two tools this session does not build.

A mapping from a tool to the module that will run it, which is a fact about the
design. Which of them are PENDING is never written down: `pending_tools()` probes
for the module, so the day `src/sandbox.py` lands the list shortens with no edit
here. A hardcoded pending set is the same defect as a harness hardcoded to one
county's tract count -- correct until the thing it describes changes, and silent
when it does."""

TRACT_KEY = f"{acquire.DATASET_TRACTS}{align.JOINED_SUFFIX}"
GROUP_KEY = f"{acquire.DATASET_BLOCK_GROUPS}{align.JOINED_SUFFIX}"

SCENARIOS: dict[str, HazardScenario] = {item.name: item for item in HAZARD_SCENARIOS}
DEFAULT_SCENARIO: HazardScenario = HAZARD_SCENARIOS[0]
"""The scenario a caller gets by leaving the argument empty: the first the study
area declares, which is the shallowest surge and so the most conservative claim."""


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def tool(fn: ToolFn) -> ToolFn:
    """Register a function as the tool it is named after, and log what it returns.

    Registration by `__name__` rather than by a table means the name the model
    calls and the name in the source are one string. The log is here rather than
    at eleven call sites for the same reason.
    """

    @functools.wraps(fn)
    def logged(**kwargs: Any) -> ToolResult:
        result = fn(**kwargs)
        CALLS.append(
            {"tool": fn.__name__, "arguments": dict(kwargs), "result": result}
        )
        del CALLS[:-MAX_CALLS]
        return result

    TOOL_FUNCTIONS[fn.__name__] = logged
    return logged


def logged_calls() -> list[dict[str, Any]]:
    """Every tool result this process produced, for the critic to trace against."""
    return list(CALLS)


def forget_calls() -> None:
    """Drop the call log. A new question is a new traceability question."""
    CALLS.clear()


# ---------------------------------------------------------------------------
# the shared analysis
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Analysis:
    """One `pipeline.run()`, shared by every tool for the life of the process."""

    result: pipeline.PipelineResult
    engine: Risk
    units: Any
    built_at: str
    seconds: float


_ANALYSIS: Analysis | None = None


def analysis() -> Analysis:
    """The shared run, built on first need.

    `Risk()` is constructed here rather than reached for per call because
    `Risk.combine` and `Vulnerability.index` are pure frame operations -- they
    touch no file and no service -- so a caller recombining a weighting is calling
    the same two functions the pipeline called, on the same frame, and not opening
    a second route to the answer.
    """
    global _ANALYSIS
    if _ANALYSIS is None:
        started = time.time()
        result = pipeline.run()
        _ANALYSIS = Analysis(
            result=result,
            engine=Risk(),
            units=result.snapshot.frames[TRACT_KEY],
            built_at=datetime.now().isoformat(timespec="seconds"),
            seconds=round(time.time() - started, 2),
        )
    return _ANALYSIS


def invalidate() -> None:
    """Forget the shared run. Called after a live retrieval rewrites the snapshot."""
    global _ANALYSIS
    _ANALYSIS = None


def analysis_built() -> bool:
    """Is a shared run currently held?

    Exposed so that "a live retrieval invalidates the analysis" can be checked
    without paying to rebuild it. Without this the only observable of a failed
    invalidation is a stale answer after a retrieval, which no offline check can
    reach and which would show up as a wrong number rather than as a failure.
    """
    return _ANALYSIS is not None


def registry() -> Registry:
    """A registry with the manifest freshly read.

    Read per call rather than cached, so a layer `acquire_dataset` has just
    rewritten is the layer the next `list_datasets` reports.
    """
    found = Registry()
    found.load_manifest()
    return found


# ---------------------------------------------------------------------------
# invariant 3: what a result may not carry
# ---------------------------------------------------------------------------


def coordinate_faults(payload: Any, path: str = "result") -> list[str]:
    """Every way this payload could carry a coordinate into a model message.

    Three shapes, because they arrive by three routes. A KEY that names a
    coordinate is the obvious one. A bare-token VALUE that names one is the route
    `describe_layer` opens by reporting the column names of a layer TIGERweb ships
    with `INTPTLAT` on it. A LIST holding a number or another list is the shape of
    a coordinate list itself, and is refused whatever it is called -- which is why
    every sequence of numbers this module returns is a list of named records
    instead.
    """
    faults: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{path}.{key}"
            if COORDINATE_PATTERN.search(str(key)):
                faults.append(f"{here}: key could name a coordinate")
            faults.extend(coordinate_faults(value, here))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            here = f"{path}[{index}]"
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                faults.append(f"{here}: list holds a number, which is a coordinate shape")
            elif isinstance(value, list):
                faults.append(f"{here}: list holds a list, which is a coordinate shape")
            faults.extend(coordinate_faults(value, here))
    elif isinstance(payload, str):
        if BARE_TOKEN.match(payload) and COORDINATE_PATTERN.search(payload):
            faults.append(f"{path}: value {payload!r} could name a coordinate")
        if COORDINATE_TEXT.search(payload):
            faults.append(
                f"{path}: value carries a coordinate pair in prose: "
                f"{COORDINATE_TEXT.search(payload).group(0)!r}"
            )
    return faults


def as_sent(result: ToolResult) -> Any:
    """The result as the model would receive it: serialised and read back.

    Checked on the round trip rather than on the dict, because what a tool returns
    and what `json.dumps(..., default=str)` makes of it are not the same object --
    a tuple becomes a list, a pandas scalar becomes a string -- and the second one
    is what crosses into the message.
    """
    return json.loads(json.dumps(result, default=str))


def reportable_columns(columns: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Split column names into the ones a result may carry and the ones it may not."""
    kept: list[str] = []
    withheld: list[str] = []
    for column in columns:
        name = str(column)
        (withheld if COORDINATE_PATTERN.search(name) else kept).append(name)
    return kept, withheld


# ---------------------------------------------------------------------------
# small shaping helpers -- formatting only, never arithmetic
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> Any:
    """One frame value as JSON, with a missing value stated rather than dropped."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return bool(value)
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (int, str)):
        return value
    return str(value)


def _capped(items: Iterable[Any], limit: int = MAX_LIST) -> dict[str, Any]:
    """A list cut to a bound, with the number cut named beside it."""
    listed = [str(item) for item in items]
    return {
        "listed": listed[:limit],
        "total": len(listed),
        "not_listed": max(0, len(listed) - limit),
    }


def _numbers(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _scalar(value) for key, value in values.items()}


def _ranking(
    frame: pd.DataFrame, *, by: str, columns: tuple[str, ...], top_n: int
) -> tuple[list[dict[str, Any]], int, int]:
    """The top `top_n` rows by one column, as records, plus what was left out.

    Sorting and slicing read values; nothing here derives one. The two counts
    returned are the reason: a ranking that does not say how many units it dropped
    for having no value, and how many it simply did not show, is a list of ten
    units presented as if it were the county.
    """
    usable = frame.dropna(subset=[by])
    ordered = usable.sort_values(by, ascending=False)
    rows = [
        {column: _scalar(row[column]) for column in columns}
        for _, row in ordered.head(top_n).iterrows()
    ]
    return rows, len(frame) - len(usable), max(0, len(usable) - len(rows))


def _omission(evidence_units: int, unscored: int, unscored_geoids: Iterable[str]) -> dict[str, Any]:
    """Who a ranking left out and why, read from the evidence that recorded it."""
    return {
        "units": evidence_units,
        "units_scored": evidence_units - unscored,
        "units_unscored": unscored,
        "unscored_geoids": _capped(unscored_geoids),
        "why_unscored": (
            "a unit is not scored when any indicator or component is undefined for "
            "it; weighting the ones it does publish would answer a different "
            "question for that unit than for every other"
        ),
    }


def _provenance(record: Any, *, with_notes: bool = True) -> dict[str, Any]:
    """One dataset's provenance as JSON. Invariant 6, made citable.

    The notes are where retrieval records what it had to work around -- a service
    that paged short, a variable resolved by matching a label -- and across seven
    datasets they run to tens of kilobytes, which is most of a model's useful
    context spent before it has asked anything. `list_datasets` is an index and
    takes the count; `describe_layer` is the detail view and takes the notes.
    """
    source = record.provenance
    described = {
        "source_url": source.source_url,
        "retrieved_at": source.retrieved_at.isoformat(),
        "declared_crs": source.declared_crs,
        "working_crs": source.working_crs,
        "vintage": source.vintage,
        "feature_count": source.feature_count,
        "license": source.license,
        "degraded": align.is_degraded(source),
    }
    if with_notes:
        safe = [note for note in source.notes if not COORDINATE_TEXT.search(note)]
        described["notes"] = _capped(safe, MAX_NOTES)
        described["notes_withheld"] = len(source.notes) - len(safe)
        described["why_withheld_notes"] = (
            "a retrieval note that spells out a coordinate is withheld here and "
            "kept in the manifest on disk. Invariant 3 is about what reaches a "
            "model message, not about what the provenance record holds, and the "
            "study extent is derived from the boundary layer at run time rather "
            "than read from a note"
        )
    else:
        described["note_count"] = len(source.notes)
    return described


def _preset(name: str) -> WeightPreset:
    if not name:
        return DEFAULT_PRESET
    return vulnerability.preset_named(name)


def _scenario(name: str) -> HazardScenario:
    if not name:
        return DEFAULT_SCENARIO
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError(
            f"no hazard scenario named {name!r}; this study area carries "
            f"{sorted(SCENARIOS)}"
        ) from None


def _refusal(exc: Exception, kind: str) -> ToolResult:
    """A refusal the model can act on, rather than an exception it has to parse."""
    return {"error": kind, "detail": str(exc).strip("'\"")}


# ---------------------------------------------------------------------------
# recombination -- the same two functions the pipeline called
# ---------------------------------------------------------------------------


def scored(
    scenario: HazardScenario,
    preset: WeightPreset,
    weights: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, Any, Any]:
    """One scenario's table under one weighting.

    A preset carries two halves: weights over the indicators, which decide
    `Col.VULNERABILITY`, and weights over the objective terms, which decide how
    that column trades off against the other three. Recomputing only the index and
    re-ranking is what makes both halves vary; it re-reads no raster, because the
    hazard columns in the frame do not depend on the weighting.

    Run for the pipeline's own preset too, with no branch. A branch here would
    mean the default answer and every other answer came from different code, and
    the check that the two agree would only ever exercise one of them.
    """
    state = analysis()
    table = state.result.tables[scenario.name]
    index, index_evidence = state.engine.index.index(
        state.units, preset=preset, weights=weights, dataset=TRACT_KEY
    )
    frame = table.frame.copy()
    frame[Col.VULNERABILITY] = pd.Series(
        index.to_numpy(), index=table.frame.index, dtype="Float64"
    )
    combined, risk_evidence = state.engine.combine(
        frame, scenario=scenario, preset=preset, dataset=TRACT_KEY
    )
    return combined, risk_evidence, index_evidence


def overridden_weights(preset: WeightPreset, overrides: dict[str, float]) -> tuple[dict[str, float] | None, dict[str, float]]:
    """The preset's weights with the caller's overrides applied, and which applied.

    `schemas.UNSET_WEIGHT` is negative and the index refuses a negative weight, so
    an unset argument cannot be confused with a preference. `None` back means the
    caller overrode nothing and the preset's own weights are used unchanged, which
    keeps the evidence able to say the weighting came from its stated origin.
    """
    applied = {
        name: value
        for name, value in overrides.items()
        if value != schemas.UNSET_WEIGHT
    }
    if not applied:
        return None, {}
    return {**preset.weights, **applied}, applied


# ---------------------------------------------------------------------------
# the eleven tools
# ---------------------------------------------------------------------------


@tool
def list_datasets() -> ToolResult:
    """Every registered dataset with the provenance needed to cite it."""
    found = registry()
    records = found.records()
    return {
        "study_area": found.study_area.name,
        "working_crs": found.working_crs,
        "storage_crs": config.STORAGE_CRS,
        "dataset_count": len(records),
        "datasets": [
            {"name": record.name, "kind": record.kind, **_provenance(record, with_notes=False)}
            for record in records
        ],
        "degraded": [
            record.name for record in records if align.is_degraded(record.provenance)
        ],
        "note": (
            "every layer here was retrieved by this system and carries the URL it "
            "came from, the moment it was retrieved and the licence it is published "
            "under. Nothing was edited by hand. Call describe_layer for a layer's "
            "retrieval notes, which say what the service made this system work around."
        ),
    }


@tool
def acquire_dataset(name: str, timeout_s: float = config.REQUEST_TIMEOUT_S) -> ToolResult:
    """Retrieve one dataset live from the service that publishes it.

    The autonomy showcase: the model names a layer and the system performs the
    real request, re-derives the study extent from the retrieved boundaries when
    the layer needs one, and re-registers it with fresh provenance. Every call
    carries the explicit timeout invariant 7 requires, taken from the argument
    rather than from a constant here, so the bound is visible to the caller.
    """
    groups = _retrievers()
    match = [group for group in groups if name in group.datasets]
    if not match:
        return {
            "error": "unknown dataset",
            "detail": f"nothing retrievable is named {name!r}",
            "retrievable": sorted(item for group in groups for item in group.datasets),
        }
    group = match[0]
    found = registry()
    area = found.study_area
    started = time.time()
    if group.needs_extent:
        boundaries = found.load(acquire.DATASET_TRACTS)
        extent = config.derive_bbox(boundaries)
        produced = group.call(area, found, extent, timeout_s=timeout_s)
    else:
        produced = group.call(area, found, timeout_s=timeout_s)
    records = list(produced) if isinstance(produced, list) else [produced]
    manifest = found.save_manifest()
    invalidate()
    return {
        "requested": name,
        "timeout_s": timeout_s,
        "seconds": round(time.time() - started, 2),
        "retrieved": [
            {"name": record.name, "kind": record.kind, **_provenance(record)}
            for record in records
        ],
        "manifest": manifest.name,
        "analysis_invalidated": True,
        "note": (
            "this was a live request to the publishing service, not a read of the "
            "stored snapshot. The analysis will be rebuilt from the new data on the "
            "next tool call that needs it."
        ),
    }


@tool
def describe_layer(name: str, max_columns: int = 30) -> ToolResult:
    """One layer's shape, columns, null counts, CRS and provenance.

    Columns whose names could carry a coordinate are counted and withheld rather
    than listed. On this study area that is not hypothetical: TIGERweb ships four
    of them on the tract layer as ordinary text attributes, and a list of column
    names is a model message like any other.
    """
    found = registry()
    try:
        record = found.record(name)
    except KeyError as exc:
        return {**_refusal(exc, "unknown dataset"), "datasets": found.names()}

    described: dict[str, Any] = {
        "name": record.name,
        "kind": record.kind,
        **_provenance(record),
    }
    if record.kind == "raster":
        described.update(_raster_facts(record.name))
        return described

    frame = found.load(record.name)
    kept, withheld = reportable_columns(frame.columns)
    described.update(
        {
            "rows": len(frame),
            "column_count": len(frame.columns),
            "columns_withheld": len(withheld),
            "columns_not_listed": max(0, len(kept) - max_columns),
            "columns": [
                {
                    "name": column,
                    "dtype": str(frame[column].dtype),
                    "nulls": int(frame[column].isna().sum()),
                }
                for column in kept[:max_columns]
            ],
            "why_withheld": (
                "a column whose name could carry a coordinate is not reported; "
                "geometry never reaches a model message, and a coordinate wearing a "
                "column name is still a coordinate"
            ),
        }
    )
    if hasattr(frame, "crs") and frame.crs is not None:
        described["crs_on_load"] = frame.crs.to_string()
    return described


def _raster_facts(name: str) -> dict[str, Any]:
    """A raster's grid, read from the evidence the hazard stage already recorded.

    A raster cannot be handed to `Registry.load`, and opening it here would be a
    second reader of a file `align.zonal_stats` already owns. The grid facts below
    were counted once, while the inundation surface was derived from this raster,
    and are quoted from there.
    """
    state = analysis()
    surfaces = [
        surface
        for surface in state.result.hazard.surfaces.values()
        if surface.evidence.source_raster == name
    ]
    if not surfaces:
        return {
            "grid": None,
            "note": (
                f"{name} is a raster and no inundation surface was derived from it "
                "on this run, so no grid statistics were counted for it"
            ),
        }
    grid = surfaces[0].evidence
    return {
        "grid": _numbers(
            {
                "crs": grid.crs,
                "cell_size_m": grid.cell_size_m,
                "cells": grid.cells,
                "usable_cells": grid.usable_cells,
                "nodata_cells": grid.nodata_cells,
                "non_finite_cells": grid.non_finite_cells,
                "elevation_min_m": grid.elevation_min_m,
                "elevation_max_m": grid.elevation_max_m,
            }
        ),
        "note": (
            "counted once while the inundation surface was derived from this raster. "
            "A nodata count of zero means zero cells of the whole grid were missing, "
            "not that nothing was checked -- the denominator is beside it."
        ),
    }


@tool
def describe_alignment() -> ToolResult:
    """What the cleaning stage did, with the denominator beside every count.

    `AlignmentReport` is frozen and carries numerators only. Reporting those alone
    would make "nothing needed repairing" and "repair never ran" the same result,
    so every count here is paired with what it was counted over, from
    `AlignmentEvidence`.
    """
    state = analysis()
    report = state.result.snapshot.report
    evidence = state.result.snapshot.evidence
    return {
        "study_area": state.result.study_area.name,
        "working_crs": state.result.study_area.working_crs,
        "datasets_cleaned": list(evidence.datasets),
        "layers_produced": sorted(state.result.snapshot.frames),
        "reprojected": dict(report.reprojected),
        "crs_observed": dict(evidence.crs_observed),
        "repairs": _numbers(
            {
                "examined": evidence.geometries_examined,
                "invalid_before": evidence.geometries_invalid,
                "repaired": report.geometries_repaired,
                "dropped": report.geometries_dropped,
            }
        ),
        "sentinels": {
            "values_examined": evidence.values_examined,
            "columns_examined": evidence.scrub_columns,
            "non_numeric": evidence.non_numeric,
            "removed": dict(report.sentinels_removed),
            "codes_seen": evidence.codes_seen(),
        },
        "geoid_audit": {
            "compared": evidence.geoids_compared,
            "matched": evidence.geoids_matched,
            "unmatched_boundary_side": _capped(report.unmatched_left),
            "unmatched_attribute_side": _capped(report.unmatched_right),
        },
        "granularity": {
            "units_apportioned": evidence.units_apportioned,
            "method": dict(report.apportioned),
            "error_pct_against_published": _numbers(report.apportionment_error),
        },
        "raster_join": {
            "polygons_measured": evidence.polygons_measured,
            "smallest_polygon_cells": evidence.smallest_polygon_cells,
            "units_below_cell_threshold": report.units_below_cell_threshold,
        },
        "degraded": dict(evidence.degraded),
        "derived_columns": dict(evidence.derived),
        "temporal_span": dict(report.temporal_span),
        "warnings": list(report.warnings),
        "note": (
            "no dataset was edited by hand. Every number here is a count of "
            "something the cleaning stage did at run time, beside the number of "
            "things it looked at to do it."
        ),
    }


@tool
def hazard_exposure(scenario: str = "", top_n: int = schemas.DEFAULT_TOP_N) -> ToolResult:
    """Inundation and exposed population per unit, for one surge scenario."""
    try:
        chosen = _scenario(scenario)
    except KeyError as exc:
        return {**_refusal(exc, "unknown scenario"), "scenarios": sorted(SCENARIOS)}

    state = analysis()
    table = state.result.tables[chosen.name]
    exposure = table.exposure
    measure = [
        item for item in state.result.hazard.measures if item.scenario == chosen.name
    ][0]
    rows, without_value, not_shown = _ranking(
        table.frame,
        by=Col.EXPOSED_POPULATION,
        columns=(
            Col.GEOID,
            Col.POPULATION,
            Col.INUNDATED_FRACTION,
            Col.INUNDATION_MEAN_M,
            Col.INUNDATION_MAX_M,
            Col.EXPOSED_POPULATION,
        ),
        top_n=top_n,
    )
    return {
        "scenario": chosen.name,
        "surge_height_m": chosen.surge_height_m,
        "surge_source": chosen.source,
        "assumption": chosen.assumption_note,
        "model": "bathtub inundation: depth = max(0, surge height - ground elevation)",
        "county_totals": _numbers(
            {
                "population": exposure.population_total,
                "exposed_population": exposure.fine_total,
                "exposed_population_coarse_estimate": exposure.coarse_total,
                "granularity_gap_worst_unit": exposure.max_abs_difference,
                "granularity_error_pct": exposure.relative_error_pct,
            }
        ),
        "granularity": {
            "reported_estimate": exposure.fine,
            "compared_against": exposure.coarse,
            "fine_units": exposure.fine_units,
            "coarse_units": exposure.coarse_units,
            "units_compared": exposure.units_compared,
            "method": exposure.method_note,
            "note": (
                "the reported figure sums block groups; the second multiplies each "
                "tract's population by its own flooded fraction. The gap between "
                "them is a result about granularity, not an error either made."
            ),
        },
        "units": _numbers(
            {
                "measured": measure.units_measured,
                "without_raster_cells": measure.units_without_cells,
                "fully_wet": measure.units_fully_wet,
                "fully_dry": measure.units_fully_dry,
                "fraction_min": measure.fraction_min,
                "fraction_max": measure.fraction_max,
                "deepest_unit": measure.deepest_unit,
                "deepest_m": measure.deepest_m,
            }
        ),
        "ranking": rows,
        "ranked_by": Col.EXPOSED_POPULATION,
        "units_without_a_value": without_value,
        "units_not_shown": not_shown,
        "vector_hazard": dict(state.result.hazard.vector_hazard),
        "warnings": list(measure.warnings) + list(exposure.warnings),
    }


@tool
def vulnerability_index(
    preset: str = "",
    top_n: int = schemas.DEFAULT_TOP_N,
    **overrides: float,
) -> ToolResult:
    """The weighted percentile index, under a named weighting or an overridden one."""
    try:
        chosen = _preset(preset)
    except KeyError as exc:
        return {**_refusal(exc, "unknown preset"), "presets": sorted(PRESETS)}

    weights, applied = overridden_weights(
        chosen,
        {
            schemas.weighted_indicator(argument): value
            for argument, value in overrides.items()
            if argument in schemas.weight_arguments()
        },
    )
    state = analysis()
    try:
        index, evidence = state.engine.index.index(
            state.units, preset=chosen, weights=weights, dataset=TRACT_KEY
        )
    except (KeyError, ValueError) as exc:
        return {**_refusal(exc, "invalid weighting"), "preset": chosen.name}

    frame = state.units.copy()
    frame[Col.VULNERABILITY] = pd.Series(
        index.to_numpy(), index=state.units.index, dtype="Float64"
    )
    rows, without_value, not_shown = _ranking(
        frame,
        by=Col.VULNERABILITY,
        columns=(Col.GEOID, Col.POPULATION, Col.VULNERABILITY, *VULNERABILITY_INDICATORS),
        top_n=top_n,
    )
    return {
        "preset": evidence.preset,
        "weights_origin": evidence.origin,
        "weights_origin_note": chosen.origin_note,
        "weights_origin_url": evidence.origin_url,
        "weights_overridden": _numbers(applied),
        "weights_used": _numbers(evidence.weights),
        "indicators": list(evidence.indicators),
        "directions": dict(evidence.directions),
        "published_per_indicator": dict(evidence.published),
        "missing_per_indicator": dict(evidence.missing),
        "rank_denominator": dict(evidence.rank_denominator),
        "index_min": _scalar(evidence.index_min),
        "index_max": _scalar(evidence.index_max),
        "omitted": _omission(evidence.units, evidence.units_unscored, evidence.unscored_geoids),
        "ranking": rows,
        "ranked_by": Col.VULNERABILITY,
        "units_without_a_value": without_value,
        "units_not_shown": not_shown,
        "note": (
            "each indicator is percentile-ranked against the units that publish it, "
            "the CDC/ATSDR rule, so a unit with no value is not treated as the least "
            "vulnerable -- it is not ranked at all"
        ),
        "warnings": list(evidence.warnings),
    }


@tool
def risk_scenario(
    scenario: str = "", preset: str = "", top_n: int = schemas.DEFAULT_TOP_N
) -> ToolResult:
    """Hazard, exposure, vulnerability and resilience combined into a priority order."""
    try:
        chosen_scenario = _scenario(scenario)
    except KeyError as exc:
        return {**_refusal(exc, "unknown scenario"), "scenarios": sorted(SCENARIOS)}
    try:
        chosen_preset = _preset(preset)
    except KeyError as exc:
        return {**_refusal(exc, "unknown preset"), "presets": sorted(PRESETS)}

    frame, risk, index = scored(chosen_scenario, chosen_preset)
    state = analysis()
    table = state.result.tables[chosen_scenario.name]
    rows, without_value, not_shown = _ranking(
        frame,
        by=Col.RISK_SCORE,
        columns=(
            Col.GEOID,
            Col.PRIORITY_RANK,
            Col.RISK_SCORE,
            Col.POPULATION,
            Col.INUNDATED_FRACTION,
            Col.EXPOSED_POPULATION,
            Col.VULNERABILITY,
            Col.RESILIENCE,
        ),
        top_n=top_n,
    )
    return {
        "scenario": chosen_scenario.name,
        "surge_height_m": chosen_scenario.surge_height_m,
        "preset": risk.preset,
        "weights_origin": risk.origin,
        "weights_origin_note": chosen_preset.origin_note,
        "weights_origin_url": risk.origin_url,
        "components": list(risk.components),
        "component_weights": _numbers(risk.weights),
        "component_directions": dict(risk.directions),
        "component_published": dict(risk.component_published),
        "rank_denominator": dict(risk.rank_denominator),
        "score_min": _scalar(risk.score_min),
        "score_max": _scalar(risk.score_max),
        "omitted": _omission(risk.units, risk.units_unscored, risk.unscored_geoids),
        "vulnerability_omitted": _omission(
            index.units, index.units_unscored, index.unscored_geoids
        ),
        "ranking": rows,
        "ranked_by": Col.RISK_SCORE,
        "units_without_a_value": without_value,
        "units_not_shown": not_shown,
        "resilience": _numbers(
            {
                "facilities": table.resilience.facilities,
                "radius_m": table.resilience.radius_m,
                "tag_key": table.resilience.tag_key,
                "tag_source": table.resilience.tag_source,
                "units_reaching_none": table.resilience.units_with_none,
                "median_count": table.resilience.median_count,
            }
        ),
        "warnings": list(risk.warnings) + list(index.warnings),
    }


@tool
def compare_scenarios(
    scenario: str = "", priority_units: int = DEFAULT_PRIORITY_UNITS
) -> ToolResult:
    """Every weighting against one scenario, including who each one drops.

    `units=` is passed and is load-bearing. Without it the comparison recombines
    only the objective half of each weighting, and two presets that differ only in
    their indicator weights return byte-identical rows while the table presents
    them as compared. That was a real defect in this project, found by reading
    rather than by a check, and `_tradeoff_checks` now asserts the difference it
    should produce.
    """
    try:
        chosen = _scenario(scenario)
    except KeyError as exc:
        return {**_refusal(exc, "unknown scenario"), "scenarios": sorted(SCENARIOS)}

    state = analysis()
    table = state.result.tables[chosen.name]
    try:
        rows = state.engine.compare_presets(
            table.frame,
            scenario=chosen,
            units=state.units,
            presets=WEIGHT_PRESETS,
            priority_units=priority_units,
        )
    except ValueError as exc:
        return _refusal(exc, "invalid comparison")

    return {
        "scenario": chosen.name,
        "surge_height_m": chosen.surge_height_m,
        "priority_units": priority_units,
        "weightings_compared": len(rows),
        "rows": [
            {
                "preset": row.preset,
                "origin": PRESETS[row.preset].origin,
                "origin_url": PRESETS[row.preset].origin_url,
                "top_geoids": _capped(row.top_geoids),
                "population_in_priority": row.population_in_priority,
                "vulnerable_population_in_priority": row.vulnerable_population_in_priority,
                "mean_inundation_m": _scalar(row.mean_inundation_m),
                "displaced_geoids": _capped(row.displaced_geoids),
                "displaced_count": len(row.displaced_geoids),
            }
            for row in rows
        ],
        "who_loses": (
            "displaced_geoids names the units that ANOTHER weighting in this "
            "comparison prioritises and this one does not -- the people whose "
            "priority depends on a value judgement rather than on the data"
        ),
        "note": (
            "both halves of each weighting vary here: the index is recomputed per "
            "weighting from the indicator weights, then re-ranked under the "
            "objective weights. Comparing only the second half would report three "
            "answers where two of them were the same answer."
        ),
    }


@tool
def ask_user_preferences(question: str) -> ToolResult:
    """Put a weighting choice to the human, with where each option came from.

    This run answers with the menu rather than with a person: there is no
    interactive channel in a batch run, and a tool that blocked on `input()` would
    hang any harness that runs the agent without a terminal. What it does NOT do
    is invent an answer and present it as the human's -- `elicited` says plainly
    that nobody was asked, and the default named below is a published weighting
    with a citable origin rather than this system's opinion.
    """
    return {
        "question": question,
        "elicited": False,
        "channel": "none",
        "why": (
            "this run has no interactive channel, so no human answered. The choice "
            "below is the default, not a preference somebody expressed."
        ),
        "default": DEFAULT_PRESET.name,
        "options": [
            {
                "name": item.name,
                "origin": item.origin,
                "origin_note": item.origin_note,
                "origin_url": item.origin_url,
                "weights": _numbers(item.weights),
            }
            for item in WEIGHT_PRESETS
        ],
        "how_to_use": (
            "pass one of these names as the preset argument to risk_scenario or "
            "vulnerability_index, or override an individual indicator weight "
            "directly. Report which weighting you used and where it came from."
        ),
        "note": (
            "two of these come from one published index by two of its own published "
            "rules and differ only in how they weight the indicators; the third is "
            "the authors' judgement and says so. Call compare_scenarios to see who "
            "each choice drops."
        ),
    }


@tool
def run_spatial_code(code: str, timeout_s: float = 60.0) -> ToolResult:
    """Execute model-written Python against the cleaned layers."""
    module = _backing("run_spatial_code")
    if isinstance(module, dict):
        return module
    run = module.Sandbox().run(code, timeout_s=timeout_s)
    return {
        "exit_code": run.exit_code,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "duration_s": run.duration_s,
        "error_type": run.error_type,
    }


@tool
def validate_answer(answer: str) -> ToolResult:
    """Trace every number in a draft answer back to a logged tool result."""
    module = _backing("validate_answer")
    if isinstance(module, dict):
        return module
    report = module.Critic().check(answer, logged_calls(), 1)
    return {
        "cycle": report.cycle,
        "passed": report.passed,
        "numbers_checked": report.numbers_checked,
        "numbers_traceable": report.numbers_traceable,
        "findings": [
            {"kind": item.kind, "detail": item.detail, "evidence": item.evidence}
            for item in report.findings
        ],
    }


# ---------------------------------------------------------------------------
# retrieval dispatch and the pending probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Retriever:
    """One retrieval call and the dataset names it produces."""

    datasets: tuple[str, ...]
    call: Callable[..., Any]
    needs_extent: bool


def _retrievers() -> tuple[Retriever, ...]:
    """Which live retrieval produces which registered dataset.

    The names come from `acquire`'s own constants, so a dataset renamed there is
    renamed here. `needs_extent` marks the calls that take the study extent, which
    is derived from the retrieved boundary layer at run time and never written
    down -- invariant 5.
    """
    return (
        Retriever(
            (acquire.DATASET_TRACTS, acquire.DATASET_BLOCK_GROUPS),
            acquire.acquire_boundaries,
            False,
        ),
        Retriever(
            (acquire.DATASET_ACS, acquire.DATASET_ACS_BLOCK_GROUPS),
            acquire.acquire_acs,
            False,
        ),
        Retriever((acquire.DATASET_ELEVATION,), acquire.acquire_elevation, True),
        Retriever((acquire.DATASET_FACILITIES,), acquire.acquire_facilities, True),
        Retriever((acquire.DATASET_FLOOD_ZONES,), acquire.acquire_flood_zones, True),
    )


def module_present(name: str) -> bool:
    """Is `src/<name>.py` importable in this checkout?"""
    try:
        return importlib.util.find_spec(f"{__package__}.{name}") is not None
    except (ImportError, ValueError):
        return False


def pending_tools() -> tuple[str, ...]:
    """Advertised tools whose backing module has not been built yet.

    Probed, never listed. The day `src/sandbox.py` exists, `run_spatial_code`
    drops off this list with no edit anywhere -- which is the property a written
    list cannot have and the reason the S8 review found five assertions frozen to
    one county's tract count.
    """
    return tuple(
        name
        for name, module in BACKING_MODULES.items()
        if not module_present(module)
    )


def _backing(name: str) -> Any:
    """The module that runs this tool, or the refusal to hand back instead."""
    module = BACKING_MODULES[name]
    if not module_present(module):
        return {
            "error": "tool_unavailable",
            "detail": (
                f"{name} is backed by src/{module}.py, which does not exist in this "
                "checkout, so the capability is not built rather than broken"
            ),
            "backing_module": f"src/{module}.py",
            "advertised_as_pending": name in pending_tools(),
        }
    return importlib.import_module(f"{__package__}.{module}")


def tool_specs() -> list[dict[str, Any]]:
    """The specs the agent sends, with any pending tool marked as such."""
    return schemas.build_tool_specs(pending=pending_tools())


def faults_between(
    advertised: set[str], executable: set[str], frozen: set[str]
) -> list[str]:
    """The three disagreements, taken over sets rather than over module state.

    Separated from `surface_faults` so the guard can be exercised on sets that
    disagree. On a wired-up surface the real sets are equal, so a mutation that
    stops one of these branches firing produces the same empty list and reads as
    caught by nothing -- which is the shape of a guard that cannot fail. The
    fixtures in `_fault_checks` are the only thing that reaches these branches.
    """
    faults: list[str] = []
    unrunnable = sorted(advertised - executable)
    if unrunnable:
        faults.append(
            f"{len(unrunnable)} tool(s) are offered to the model and cannot be executed: "
            f"{unrunnable}. TOOL_FUNCTIONS holds {sorted(executable) or 'nothing'}"
        )
    undeclared = sorted(advertised - frozen)
    if undeclared:
        faults.append(
            f"{len(undeclared)} advertised tool(s) are not in contracts.TOOL_NAMES: "
            f"{undeclared}. The frozen surface is {list(TOOL_NAMES)}"
        )
    unreachable = sorted(executable - advertised)
    if unreachable:
        faults.append(
            f"{len(unreachable)} implemented tool(s) are never advertised, so the model "
            f"cannot call them: {unreachable}"
        )
    return faults


def surface_faults() -> list[str]:
    """Every way the advertised tool surface and the executable one disagree.

    Three separate faults, because they fail differently. A name the model is
    offered but nothing can run wastes a turn and returns an error the model then
    has to reason about. A name outside `contracts.TOOL_NAMES` is drift from the
    frozen contract. A name that is implemented and never advertised is dead code
    the model cannot reach.

    Deliberately blind to `pending_tools()`. A pending tool is advertised,
    executable and frozen -- it runs, and what it returns is a refusal naming the
    module that is missing. Folding "pending" in here would make this guard go
    quiet for a state it was not written to describe, which is how a guard stops
    being able to fail.
    """
    return faults_between(
        set(schemas.TOOL_ARG_MODELS), set(TOOL_FUNCTIONS), set(TOOL_NAMES)
    )


def format_surface() -> str:
    """The tool surface, printed, for the acceptance gate to paste."""
    pending = pending_tools()
    lines = [
        f"TOOL SURFACE -- {len(TOOL_FUNCTIONS)} tool(s), "
        f"{len(pending)} pending a module that is not built yet",
        "",
    ]
    for name in TOOL_NAMES:
        model = schemas.TOOL_ARG_MODELS[name]
        arguments = ", ".join(model.model_fields) or "(none)"
        mark = "PENDING" if name in pending else "ready  "
        lines.append(f"  [{mark}] {name}")
        lines.append(f"            args: {arguments}")
    lines.append("")
    lines.append(f"  faults: {surface_faults() or 'none'}")
    if pending:
        for name in pending:
            lines.append(f"  {name} is backed by src/{BACKING_MODULES[name]}.py, not built")
    return "\n".join(lines)


def main() -> int:
    print(format_surface())
    return 0


# ---------------------------------------------------------------------------
# self check
# ---------------------------------------------------------------------------


SAMPLE_ARGUMENTS: dict[str, dict[str, Any]] = {
    "list_datasets": {},
    "acquire_dataset": {"name": "not-a-dataset"},
    "describe_layer": {"name": acquire.DATASET_TRACTS},
    "describe_alignment": {},
    "hazard_exposure": {},
    "vulnerability_index": {},
    "risk_scenario": {},
    "compare_scenarios": {},
    "ask_user_preferences": {"question": "which weighting should decide priority?"},
    "run_spatial_code": {"code": "print(1)"},
    "validate_answer": {"answer": "a draft"},
}
"""Arguments to call each tool with in the checks.

`acquire_dataset` is called with a name nothing retrieves, so the checks perform
no network request: the real endpoints are exercised by `acquire.py --check`,
which is the module that owns them, and by `python -m src.demo`. A tool added
without an entry here fails the check rather than going unexercised."""


def _every_result() -> dict[str, ToolResult]:
    """Call every registered tool once, enumerated from the registry itself."""
    return {
        name: TOOL_FUNCTIONS[name](**SAMPLE_ARGUMENTS[name])
        for name in sorted(TOOL_FUNCTIONS)
    }


def _surface_checks(results: dict[str, ToolResult]) -> list[tuple[str, bool]]:
    """The three things `surface_faults` watches, plus the pending probe."""
    faults = surface_faults()
    for line in faults:
        print(f"  surface fault: {line}")
    pending = pending_tools()
    print(f"  {len(TOOL_FUNCTIONS)} executable, {len(schemas.TOOL_ARG_MODELS)} advertised, "
          f"{len(TOOL_NAMES)} frozen; pending {list(pending)}")
    print(f"  probe: a module that exists reads present, one that does not reads absent")

    specs = tool_specs()
    marked = {
        spec["function"]["name"]
        for spec in specs
        if schemas.PENDING_SUFFIX in spec["function"]["description"]
    }
    return [
        ("the advertised surface and the executable one agree", not faults),
        ("every frozen tool name is executable", set(TOOL_FUNCTIONS) == set(TOOL_NAMES)),
        ("every tool was covered by the checks, none skipped for lack of arguments",
         set(SAMPLE_ARGUMENTS) == set(TOOL_FUNCTIONS) and set(results) == set(TOOL_NAMES)),
        ("the pending probe finds a module that exists",
         module_present("pipeline") and module_present("schemas")),
        ("the pending probe reports a module that does not exist as absent",
         not module_present("a_module_nobody_wrote")),
        ("pending is derived from the probe, not from a list written down",
         set(pending) == {
             name for name, module in BACKING_MODULES.items() if not module_present(module)
         }),
        ("the specs the agent sends mark exactly the pending tools", marked == set(pending)),
        ("every implemented tool is a callable the loop can reach",
         all(callable(TOOL_FUNCTIONS[name]) for name in TOOL_NAMES)),
    ]


def _serialisation_checks(results: dict[str, ToolResult]) -> list[tuple[str, bool]]:
    """Invariant 3, on every tool's real result, enumerated from the registry."""
    faults: dict[str, list[str]] = {}
    sizes: dict[str, int] = {}
    for name, result in results.items():
        payload = json.dumps(result, default=str)
        sizes[name] = len(payload)
        found = coordinate_faults(as_sent(result))
        if found:
            faults[name] = found
    for name in sorted(sizes):
        print(f"  {name:<22} {sizes[name]:>7,} bytes serialised  "
              f"{len(faults.get(name, [])) or 'no'} coordinate fault(s)")
    for name, found in faults.items():
        for line in found[:3]:
            print(f"    {name}: {line}")

    return [
        ("every tool returns a dict", all(isinstance(item, dict) for item in results.values())),
        ("every result serialises to JSON", len(sizes) == len(results)),
        ("no serialised result carries a coordinate, by key, by value or by shape",
         not faults),
        ("the coordinate scan had results to scan", len(results) == len(TOOL_NAMES)),
        ("no result is large enough to crowd the run's remaining turns",
         max(sizes.values()) < 24_000),
    ]


def _coordinate_guard_checks() -> list[tuple[str, bool]]:
    """That the guard can fail, on each of the three shapes it refuses.

    This study area's tract layer really does carry coordinate columns, so the
    withholding is falsifiable on real data as well -- but the synthetic payloads
    are what prove each branch fires, because a county whose boundary layer
    happened to ship none would leave every branch untested.
    """
    by_key = {"INTPTLAT": "+32.7"}
    by_value = {"columns": ["GEOID", "CENTLON"]}
    by_shape = {"outline": [1.0, 2.0]}
    by_nesting = {"outline": [[1.0, 2.0]]}
    clean = {
        "scenario": "surge_3_0m",
        "rows": [{"geoid": "01001020100", "population": 1}],
        "count": 2,
    }

    by_prose = {
        "note": (
            "bbox converted from (min_lon, min_lat, max_lon, max_lat) "
            "(-80.45355300027266, 32.48256499980578) to south,west order"
        )
    }

    found = registry()
    frame = found.load(acquire.DATASET_TRACTS)
    kept, withheld = reportable_columns(frame.columns)
    described = describe_layer(name=acquire.DATASET_TRACTS)

    # Every registered layer, enumerated from the registry rather than named here.
    # The version of this check that named one layer passed while a second layer
    # was sending the study bounding box to the model in a provenance note: the
    # scan is only as wide as the set it is pointed at.
    every = {name: describe_layer(name=name) for name in found.names()}
    leaking = {
        name: coordinate_faults(as_sent(result))
        for name, result in every.items()
        if coordinate_faults(as_sent(result))
    }
    hidden = {
        name: result.get("notes_withheld", 0)
        for name, result in every.items()
        if result.get("notes_withheld", 0)
    }
    print(f"  {acquire.DATASET_TRACTS}: {len(kept)} column(s) reportable, "
          f"{len(withheld)} withheld -> {withheld}")
    print(f"  described all {len(every)} registered layer(s); "
          f"coordinate faults {leaking or 'none'}")
    print(f"  retrieval notes withheld for carrying a coordinate: {hidden or 'none'}")

    return [
        ("a key that names a coordinate is refused", bool(coordinate_faults(by_key))),
        ("a column name that names a coordinate, inside a value, is refused",
         bool(coordinate_faults(by_value))),
        ("a list of numbers is refused whatever it is called",
         bool(coordinate_faults(by_shape))),
        ("a list of lists is refused whatever it is called",
         bool(coordinate_faults(by_nesting))),
        ("a coordinate spelled out inside a sentence is refused",
         bool(coordinate_faults(by_prose))),
        ("a clean result raises nothing, so the guard is not refusing everything",
         not coordinate_faults(clean)),
        ("prose is not mistaken for a column name",
         not coordinate_faults({"note": "the x and y of the matter, longitude aside"})),
        ("ordinary numbers in prose are not mistaken for a coordinate",
         not coordinate_faults(
             {"note": "99,037 exposed of 420,264 at 3.0 m; error 0.0 pct over 261 units"}
         )),
        ("this study area's boundary layer really does carry coordinate columns",
         len(withheld) > 0),
        ("describe_layer withholds them and says how many",
         described["columns_withheld"] == len(withheld)
         and not any(item["name"] in withheld for item in described["columns"])),
        ("describe_layer still reports the columns that are not coordinates",
         len(described["columns"]) > 0 and Col.GEOID in {item["name"] for item in described["columns"]}),
        ("every registered layer was described, not one chosen by hand",
         set(every) == set(found.names()) and len(every) > 1),
        ("no layer's description carries a coordinate, by key, value, shape or prose",
         not leaking),
        ("a withheld note is counted rather than dropped in silence",
         all(
             every[name]["notes"]["total"] + every[name]["notes_withheld"]
             == len(found.record(name).provenance.notes)
             for name in every
         )),
    ]


def _agreement_checks() -> list[tuple[str, bool]]:
    """That the tool route and the pipeline route are one route.

    Every number a tool reports has to be the number the pipeline computed, or
    there are two answers to one question and the critic's traceability check is
    tracing to the wrong one. These compare the tools' output against
    `PipelineResult` on the real county rather than against a fixture, because the
    thing at risk is agreement on real data.
    """
    state = analysis()
    scenario = DEFAULT_SCENARIO
    table = state.result.tables[scenario.name]

    from_tool = risk_scenario(scenario=scenario.name, preset=DEFAULT_PRESET.name, top_n=10)
    tool_geoids = [row[Col.GEOID] for row in from_tool["ranking"]]
    pipeline_geoids = (
        table.frame.dropna(subset=[Col.RISK_SCORE])
        .sort_values(Col.RISK_SCORE, ascending=False)[Col.GEOID]
        .head(10)
        .tolist()
    )

    exposure = hazard_exposure(scenario=scenario.name)
    totals = exposure["county_totals"]

    compared = compare_scenarios(scenario=scenario.name, priority_units=DEFAULT_PRIORITY_UNITS)
    tool_rows = {row["preset"]: row for row in compared["rows"]}
    pipeline_rows = {
        row.preset: row for row in state.result.tradeoff if row.scenario == scenario.name
    }

    print(f"  shared run: {state.seconds:.1f}s for {len(state.result.tables)} scenario(s), "
          f"built {state.built_at}")
    print(f"  {scenario.name}: tool top-10 == pipeline top-10: {tool_geoids == pipeline_geoids}")
    print(f"  exposed population reported {totals['exposed_population']:,.0f} "
          f"against the tract-uniform estimate {totals['exposed_population_coarse_estimate']:,.0f}")

    return [
        ("the tool's priority order under the pipeline's own weighting is the pipeline's",
         tool_geoids == pipeline_geoids and len(tool_geoids) == 10),
        ("the exposed population reported is the evidence's, not a recomputation",
         totals["exposed_population"] == round(table.exposure.fine_total, 6)),
        ("the coarse estimate reported is the evidence's too",
         totals["exposed_population_coarse_estimate"] == round(table.exposure.coarse_total, 6)),
        ("the population denominator reported is the evidence's",
         totals["population"] == round(table.exposure.population_total, 6)),
        ("the trade-off rows are the pipeline's rows, weighting by weighting",
         set(tool_rows) == set(pipeline_rows)
         and all(
             tool_rows[name]["top_geoids"]["listed"] == list(pipeline_rows[name].top_geoids)
             and tool_rows[name]["displaced_geoids"]["listed"]
             == list(pipeline_rows[name].displaced_geoids)[:MAX_LIST]
             and tool_rows[name]["population_in_priority"]
             == pipeline_rows[name].population_in_priority
             for name in tool_rows
         )
         and len(tool_rows) > 1),
        ("the comparison covered every weighting the project defines",
         compared["weightings_compared"] == len(WEIGHT_PRESETS)),
    ]


def _tradeoff_checks() -> list[tuple[str, bool]]:
    """That both halves of a weighting reach the comparison.

    Asserted as a difference that must EXIST. `svi_equal` and `svi_themes` carry
    identical objective weights and differ only in how they weight the indicators,
    so if the `units=` argument were dropped they would return the same rows and
    this would fail. The same property asserted the other way round -- that the
    presets agree -- would pass without the argument and prove nothing.
    """
    scenario = DEFAULT_SCENARIO
    compared = compare_scenarios(scenario=scenario.name)
    rows = {row["preset"]: row for row in compared["rows"]}
    same_objective = [
        (a.name, b.name)
        for i, a in enumerate(WEIGHT_PRESETS)
        for b in WEIGHT_PRESETS[i + 1:]
        if {k: a.weights[k] for k in vulnerability.OBJECTIVE_TERMS}
        == {k: b.weights[k] for k in vulnerability.OBJECTIVE_TERMS}
        and {k: a.weights[k] for k in VULNERABILITY_INDICATORS}
        != {k: b.weights[k] for k in VULNERABILITY_INDICATORS}
    ]
    differing = [
        (a, b)
        for a, b in same_objective
        if rows[a]["top_geoids"]["listed"] != rows[b]["top_geoids"]["listed"]
    ]
    displaced = sum(row["displaced_count"] for row in rows.values())
    print(f"  preset pairs differing ONLY in indicator weights: {same_objective}")
    print(f"  of those, pairs whose priority lists differ here: {differing}")
    print(f"  displacements recorded across the comparison: {displaced}")

    return [
        ("a preset pair differing only in indicator weights exists to test with",
         len(same_objective) >= 1),
        ("that pair ranks differently, which only happens if the index was recomputed",
         len(differing) == len(same_objective)),
        ("the comparison names units each weighting drops rather than reporting none",
         displaced > 0),
        ("every weighting reports where it came from",
         all(row["origin"] for row in rows.values())),
    ]


def _weighting_checks() -> list[tuple[str, bool]]:
    """That a weight argument is a parameter and not decoration."""
    default = vulnerability_index()
    heavy = vulnerability_index(**{
        schemas.weight_argument(VULNERABILITY_INDICATORS[0]): 10.0
    })
    both = [
        [row[Col.GEOID] for row in default["ranking"]],
        [row[Col.GEOID] for row in heavy["ranking"]],
    ]
    named = vulnerability_index(preset=WEIGHT_PRESETS[1].name)

    # Two presets, chosen for what they hold constant. `indicator_only` differs
    # from the default in its indicator weights alone, so the score can only move
    # if the index was recomputed under it -- the objective weights are identical
    # and would produce the same ranking on their own. `objective_too` also moves
    # the objective terms. Comparing the default against one preset would let
    # either half of a weighting explain any difference, which is how a comparison
    # of three weightings once reported two that had not been varied.
    terms = {name: DEFAULT_PRESET.weights[name] for name in vulnerability.OBJECTIVE_TERMS}
    indicator_only = [
        item
        for item in WEIGHT_PRESETS
        if item.name != DEFAULT_PRESET.name
        and {name: item.weights[name] for name in vulnerability.OBJECTIVE_TERMS} == terms
    ][0]
    objective_too = [
        item
        for item in WEIGHT_PRESETS
        if {name: item.weights[name] for name in vulnerability.OBJECTIVE_TERMS} != terms
    ][0]
    risk_default = risk_scenario(preset=DEFAULT_PRESET.name)
    risk_indicator = risk_scenario(preset=indicator_only.name)
    risk_objective = risk_scenario(preset=objective_too.name)
    print(f"  overriding {VULNERABILITY_INDICATORS[0]} moved "
          f"{len(set(both[0]) ^ set(both[1]))} unit(s) in or out of the top "
          f"{len(both[0])}")
    print(f"  risk under {DEFAULT_PRESET.name} against {indicator_only.name} "
          f"(indicator weights only): rankings differ "
          f"{risk_default['ranking'] != risk_indicator['ranking']}")
    print(f"  against {objective_too.name} (objective weights too): component weights differ "
          f"{risk_default['component_weights'] != risk_objective['component_weights']}")

    return [
        ("an overridden weight changes the ranking", both[0] != both[1]),
        ("a weighting that differs only in its indicator weights still moves the score",
         risk_default["ranking"] != risk_indicator["ranking"]
         and risk_indicator["preset"] == indicator_only.name
         and risk_default["component_weights"] == risk_indicator["component_weights"]),
        ("a weighting that also moves the objective terms reports the moved weights",
         risk_default["component_weights"] != risk_objective["component_weights"]
         and risk_objective["preset"] == objective_too.name),
        ("the override is reported rather than applied silently",
         bool(heavy["weights_overridden"]) and not default["weights_overridden"]),
        ("an untouched weighting keeps its stated origin",
         default["weights_origin"] == DEFAULT_PRESET.origin
         and default["weights_origin_url"] == DEFAULT_PRESET.origin_url),
        ("naming a different preset changes the weights used",
         named["weights_used"] != default["weights_used"]),
        # The tolerance is the rounding this module applies for the message, not
        # slack in the index: five weights rounded to six decimal places can sum
        # to 1.000002, and asserting exact equality would be asserting that the
        # reported number is the unrounded one, which it deliberately is not.
        ("the weights that were used are reported, whatever their source",
         abs(sum(default["weights_used"].values()) - 1.0) < 1e-5
         and abs(sum(heavy["weights_used"].values()) - 1.0) < 1e-5
         and len(heavy["weights_used"]) == len(VULNERABILITY_INDICATORS)),
        ("the unset sentinel is not mistaken for a weight of its own",
         vulnerability_index(**{
             schemas.weight_argument(name): schemas.UNSET_WEIGHT
             for name in VULNERABILITY_INDICATORS
         })["weights_used"] == default["weights_used"]),
    ]


def _omission_checks() -> list[tuple[str, bool]]:
    """That every ranking says how many units it left out, and why.

    The county number is printed and not asserted. One unscored tract here is a
    property of this county's water tract, not of the code, and asserting it would
    stop the harness working on the transfer county with no defect present.
    """
    index = vulnerability_index()
    risk = risk_scenario()
    exposure = hazard_exposure()
    print(f"  vulnerability: {index['omitted']['units_scored']} scored, "
          f"{index['omitted']['units_unscored']} unscored of {index['omitted']['units']}")
    print(f"  risk:          {risk['omitted']['units_scored']} scored, "
          f"{risk['omitted']['units_unscored']} unscored of {risk['omitted']['units']}")
    print(f"  unscored here: {index['omitted']['unscored_geoids']['listed']}")

    return [
        ("the index accounts for every unit, scored plus unscored",
         index["omitted"]["units_scored"] + index["omitted"]["units_unscored"]
         == index["omitted"]["units"] and index["omitted"]["units"] > 0),
        ("the risk table accounts for every unit too",
         risk["omitted"]["units_scored"] + risk["omitted"]["units_unscored"]
         == risk["omitted"]["units"] and risk["omitted"]["units"] > 0),
        ("every ranking says how many units it did not show",
         all("units_not_shown" in item for item in (index, risk, exposure))),
        ("an unscored unit is named rather than merely counted",
         index["omitted"]["unscored_geoids"]["total"]
         == index["omitted"]["units_unscored"]),
        ("the reason a unit is unscored travels with the count",
         bool(index["omitted"]["why_unscored"]) and bool(risk["omitted"]["why_unscored"])),
        ("the risk table reports the vulnerability omissions separately from its own",
         "vulnerability_omitted" in risk),
        ("a unit with no score is dropped from the ranking and counted, not ranked",
         risk["units_without_a_value"] == risk["omitted"]["units_unscored"]
         and index["units_without_a_value"] == index["omitted"]["units_unscored"]),
        ("the ranking, the units not shown and the units with no value account for all",
         len(risk["ranking"]) + risk["units_not_shown"] + risk["units_without_a_value"]
         == risk["omitted"]["units"]),
    ]


def _refusal_checks() -> list[tuple[str, bool]]:
    """What a tool refuses, and whether it refuses recoverably.

    A tool that raises makes `agent.execute_tool` produce `{"error": "KeyError"}`,
    which tells the model the type of the mistake and not the mistake. These
    return the legal values instead, so the next turn can be right.
    """
    unknown_scenario = hazard_exposure(scenario="surge_99m")
    unknown_preset = risk_scenario(preset="nobody_wrote_this")
    unknown_dataset = describe_layer(name="not-a-layer")
    unknown_retrieval = acquire_dataset(name="not-a-layer")
    print(f"  unknown scenario -> {unknown_scenario['error']!r}, "
          f"offering {unknown_scenario['scenarios']}")
    print(f"  unknown preset   -> {unknown_preset['error']!r}, "
          f"offering {unknown_preset['presets']}")

    return [
        ("an unknown scenario is refused with the legal names",
         unknown_scenario.get("error") == "unknown scenario"
         and sorted(SCENARIOS) == unknown_scenario["scenarios"]),
        ("an unknown preset is refused with the legal names",
         unknown_preset.get("error") == "unknown preset"
         and sorted(PRESETS) == unknown_preset["presets"]),
        ("an unknown layer is refused with the registered names",
         unknown_dataset.get("error") == "unknown dataset"
         and acquire.DATASET_TRACTS in unknown_dataset["datasets"]),
        ("an unretrievable name is refused with the retrievable ones",
         unknown_retrieval.get("error") == "unknown dataset"
         and acquire.DATASET_ELEVATION in unknown_retrieval["retrievable"]),
        ("no refusal raised instead of returning, so the loop can continue",
         all(isinstance(item, dict) for item in
             (unknown_scenario, unknown_preset, unknown_dataset, unknown_retrieval))),
        ("a refusal names what to do next rather than only what went wrong",
         all("detail" in item for item in (unknown_dataset, unknown_scenario))),
    ]


def _pending_checks(results: dict[str, ToolResult]) -> list[tuple[str, bool]]:
    """That a tool with no module behind it says so rather than pretending."""
    pending = pending_tools()
    refusals = {
        name: results[name] for name in pending
    }
    ready = [name for name in TOOL_NAMES if name not in pending]
    for name, result in refusals.items():
        print(f"  {name}: {result.get('detail', '')[:96]}")

    return [
        ("every pending tool returns a refusal naming its missing module",
         all(item.get("error") == "tool_unavailable" and item.get("backing_module")
             for item in refusals.values())),
        ("a pending tool is advertised as pending, so no turn is spent discovering it",
         all(item.get("advertised_as_pending") for item in refusals.values())),
        ("no tool that has a module behind it returns that refusal",
         not any(results[name].get("error") == "tool_unavailable" for name in ready)),
        ("the tools with no module are exactly the ones the mapping names",
         set(pending) <= set(BACKING_MODULES)),
        ("at least one tool is backed by a named module, so the probe has work to do",
         len(BACKING_MODULES) > 0),
    ]


def _provenance_checks(results: dict[str, ToolResult]) -> list[tuple[str, bool]]:
    """That what a tool reports about a dataset is what the registry holds.

    Invariant 6 is what makes the answer citable, and the failure mode is not a
    missing record -- it is a plausible one. A source URL written here rather than
    quoted from the retrieval would give the model something to cite that nothing
    retrieved, and every mechanical check about provenance existing would pass.
    So each field is compared against the record, not merely required to be there.
    """
    found = registry()
    listed = {item["name"]: item for item in results["list_datasets"]["datasets"]}
    records = {record.name: record for record in found.records()}
    # Read off the frozen `Provenance` dataclass, NOT back through `_provenance`.
    # Comparing the tool's output to the helper that produced it compares the
    # helper to itself: a source URL invented inside `_provenance` appears
    # identically on both sides and the assertion passes. That is exactly how a
    # mutation replacing the URL with a literal survived the first sweep.
    quoted = all(
        listed[name]["source_url"] == records[name].provenance.source_url
        and listed[name]["vintage"] == records[name].provenance.vintage
        and listed[name]["license"] == records[name].provenance.license
        and listed[name]["feature_count"] == records[name].provenance.feature_count
        and listed[name]["declared_crs"] == records[name].provenance.declared_crs
        and listed[name]["working_crs"] == records[name].provenance.working_crs
        and listed[name]["retrieved_at"]
        == records[name].provenance.retrieved_at.isoformat()
        for name in records
    )
    shaped = all(
        set(listed[name])
        == {"name", "kind", *_provenance(records[name], with_notes=False)}
        for name in records
    )
    detailed = describe_layer(name=acquire.DATASET_TRACTS)
    fields = ("source_url", "retrieved_at", "vintage", "license", "declared_crs")
    populated = {
        field_name: sum(1 for item in listed.values() if item.get(field_name))
        for field_name in fields
    }
    degraded = results["list_datasets"]["degraded"]
    print(f"  {len(listed)} dataset(s) listed; fields populated {populated}")
    print(f"  degraded, read through align.is_degraded: {degraded}")

    return [
        ("every registered dataset is listed", set(listed) == set(records) and len(listed) > 0),
        ("every value is read off the record rather than written here", quoted),
        ("the reported fields are the ones the provenance record carries", shaped),
        ("every dataset carries a source URL, a timestamp, a vintage and a licence",
         all(count == len(listed) for count in populated.values())),
        ("degradation is reported through the provenance flag, not inferred here",
         degraded == [
             name for name, record in records.items()
             if align.is_degraded(record.provenance)
         ]),
        ("the working CRS the layers were put into is reported beside them",
         results["list_datasets"]["working_crs"] == found.working_crs),
        ("the index reports how many retrieval notes a layer has without carrying them",
         all("note_count" in item and "notes" not in item for item in listed.values())),
        ("the detail view carries the notes the index only counted",
         detailed["notes"]["total"] == listed[acquire.DATASET_TRACTS]["note_count"]),
    ]


def _fault_checks() -> list[tuple[str, bool]]:
    """That each of `faults_between`'s three branches can fire.

    On a wired surface the three sets are equal, so every branch returns nothing
    and a deleted branch is indistinguishable from a satisfied one. These feed it
    sets that disagree in one way at a time, which is the only way to observe that
    the guard still works -- the same lesson as the S8 fixtures for rules this
    county cannot violate.
    """
    everything = {"a", "b"}
    unrunnable = faults_between({"a", "b"}, {"a"}, {"a", "b"})
    undeclared = faults_between({"a", "b"}, {"a", "b"}, {"a"})
    unreachable = faults_between({"a"}, {"a", "b"}, {"a", "b"})
    quiet = faults_between(everything, everything, everything)
    print(f"  advertised but not executable -> {len(unrunnable)} fault(s)")
    print(f"  advertised but not frozen     -> {len(undeclared)} fault(s)")
    print(f"  executable but not advertised -> {len(unreachable)} fault(s)")

    return [
        ("a tool offered to the model that nothing can run is reported",
         len(unrunnable) == 1 and "cannot be executed" in unrunnable[0]),
        ("a tool advertised outside the frozen contract is reported",
         len(undeclared) == 1 and "not in contracts.TOOL_NAMES" in undeclared[0]),
        ("a tool implemented and never advertised is reported",
         len(unreachable) == 1 and "never advertised" in unreachable[0]),
        ("three sets that agree produce no fault, so the guard is not always firing",
         quiet == []),
        ("the real surface routes through the same function the fixtures exercised",
         surface_faults() == faults_between(
             set(schemas.TOOL_ARG_MODELS), set(TOOL_FUNCTIONS), set(TOOL_NAMES)
         )),
    ]


def _shaping_checks() -> list[tuple[str, bool]]:
    """That the two shaping helpers state what they dropped.

    Neither can be observed on the real county: no list this study area produces
    is longer than the cap, and a missing value reaches `_scalar` only through the
    one unscored tract. Both are cheap to force and expensive to get wrong -- a
    truncation nobody counts, or a null rendered as a zero, is a wrong number that
    looks like a right one.
    """
    long = _capped([f"unit{index}" for index in range(MAX_LIST + 7)])
    short = _capped(["a", "b"])
    missing = [_scalar(value) for value in (None, float("nan"), pd.NA)]
    real = [_scalar(value) for value in (3, "x", True, 1.5)]
    print(f"  a list of {MAX_LIST + 7} was cut to {len(long['listed'])} "
          f"with {long['not_listed']} named as not listed")

    return [
        ("a list longer than the cap is cut", len(long["listed"]) == MAX_LIST),
        ("the number cut is reported rather than silently dropped",
         long["not_listed"] == 7 and long["total"] == MAX_LIST + 7),
        ("a list under the cap is untouched and reports nothing cut",
         short["listed"] == ["a", "b"] and short["not_listed"] == 0),
        ("a missing value is reported as missing, never as a zero",
         missing == [None, None, None]),
        ("a real value survives shaping unchanged", real == [3, "x", True, 1.5]),
    ]


def _cache_checks() -> list[tuple[str, bool]]:
    """That a live retrieval forgets the shared run, through the real call site.

    Last, because it drops the analysis every earlier check was reading. If the
    invalidation did nothing, a tool called after `acquire_dataset` would answer
    from the snapshot that retrieval replaced -- a stale number presented as a
    fresh one, which is the worst failure shape this module has.

    An earlier version called `invalidate()` directly, which proved the function
    works and never touched the line in `acquire_dataset` that calls it: deleting
    that line left the whole suite green. So this drives the real tool, with the
    retrieval and the registry replaced by stubs. Nothing here reaches the network
    and nothing here writes to `data/`: the scratch registry re-registers the
    records already on disk and saves its manifest into a temporary directory,
    which is what lets the call site be exercised offline without a snapshot being
    rewritten by a check.
    """
    before = analysis_built()
    real = registry()
    record = real.records()[0]
    original_registry, original_retrievers = registry, _retrievers
    snapshot_stamp = config.MANIFEST_PATH.stat().st_mtime_ns

    with tempfile.TemporaryDirectory() as folder:
        scratch = Registry(manifest_path=Path(folder) / config.MANIFEST_PATH.name)
        for existing in real.records():
            scratch.register(
                existing.name, existing.kind, existing.path, existing.provenance
            )

        def again(area: Any, found: Registry, *, timeout_s: float) -> Any:
            return found.register(
                record.name, record.kind, record.path, record.provenance
            )

        globals()["registry"] = lambda: scratch
        globals()["_retrievers"] = lambda: (Retriever((record.name,), again, False),)
        try:
            result = acquire_dataset(name=record.name)
        finally:
            globals()["registry"] = original_registry
            globals()["_retrievers"] = original_retrievers

    after = analysis_built()
    untouched = config.MANIFEST_PATH.stat().st_mtime_ns == snapshot_stamp
    print(f"  a run was held: {before}; after retrieving {record.name!r}: {after}")
    print(f"  the retrieval reported {len(result.get('retrieved', []))} record(s) "
          f"and a manifest written to a temporary directory")

    return [
        ("a shared run was held, so the state under test is not vacuous", before),
        ("a retrieval says it invalidated the analysis",
         result.get("analysis_invalidated") is True),
        ("and the run really is forgotten, through acquire_dataset's own call site",
         not after),
        ("the retrieval reported the provenance of what it re-registered",
         len(result.get("retrieved", [])) == 1
         and result["retrieved"][0]["source_url"] == record.provenance.source_url),
        ("the bound the caller passed is reported back rather than assumed",
         result.get("timeout_s") == config.REQUEST_TIMEOUT_S),
        ("the snapshot manifest was not rewritten by a check",
         untouched),
        ("the stubs were put back, so later callers get the real registry",
         registry is original_registry and _retrievers is original_retrievers),
    ]


def _log_checks() -> list[tuple[str, bool]]:
    """That every tool result is recorded, which is what invariant 8 traces to."""
    before = len(logged_calls())
    result = list_datasets()
    after = logged_calls()
    print(f"  call log holds {len(after)} result(s), bounded at {MAX_CALLS}")

    return [
        ("a tool call is recorded as it returns",
         len(after) == before + 1 and after[-1]["tool"] == "list_datasets"),
        ("the recorded result is the result that was returned",
         after[-1]["result"] == result),
        ("the log records the arguments too, so a number can be traced to a question",
         "arguments" in after[-1]),
        ("the log is bounded rather than growing for the life of the process",
         len(after) <= MAX_CALLS),
        ("every tool is recorded, not only the ones that read the analysis",
         {item["tool"] for item in after} == set(TOOL_NAMES)),
    ]


def _self_check() -> int:
    print("TOOLS -- the eleven names in contracts.TOOL_NAMES\n")
    results = _every_result()

    checks = _surface_checks(results)
    print()
    checks += _fault_checks()
    print()
    checks += _serialisation_checks(results)
    print()
    checks += _coordinate_guard_checks()
    print()
    checks += _provenance_checks(results)
    print()
    checks += _shaping_checks()
    print()
    checks += _agreement_checks()
    print()
    checks += _tradeoff_checks()
    print()
    checks += _weighting_checks()
    print()
    checks += _omission_checks()
    print()
    checks += _refusal_checks()
    print()
    checks += _pending_checks(results)
    print()
    checks += _log_checks()
    print()
    checks += _cache_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])

    # The three modules between a question and these tools carry no `--check` of
    # their own: `agent` is the loop, `llm_client` is the only file that talks to a
    # model, and `trace` renders what the loop logged. They are held to the same
    # scans here rather than nowhere, because an annotation nothing scans is an
    # annotation that rots -- which is how all three came to have none.
    from . import agent, llm_client, trace

    for module in (agent, llm_client, trace):
        print()
        print(f"the loop's collaborators -- {module.__name__}:")
        checks += verify.discipline_checks(module)
    return verify.report(checks)


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
