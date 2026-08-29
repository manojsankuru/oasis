"""CRS resolution, geometry repair, sentinel scrubbing and the GEOID audit.

The only place data is cleaned. Nothing in this project hand-edits a snapshot;
if a dataset is malformed the fix belongs here, so the agent performs it at run
time on whatever county it is pointed at.

Three things this module is built around.

* **One choke point for the metric CRS.** `to_working_crs` is to this module
  what `_request` is to `acquire`: every area, distance, buffer, centroid and
  zonal operation in the project routes through it. A geographic CRS answers
  those questions in degrees, silently, with no error and no warning, so the
  helper has to be the only way through rather than the polite way through. It
  is idempotent, and it is correct for an object that never came through the
  registry -- the registry reprojects on load, which makes the helper look
  redundant right up until something is built in memory.

* **A reported zero has to prove itself.** On a clean county almost every
  counter in `AlignmentReport` is legitimately zero, and a field that is only
  ever zero cannot be told apart from a field that was never implemented. So
  every operation records what it EXAMINED beside what it CHANGED, in an
  `AlignmentEvidence` record that travels with the report. "0 of 837 geometries
  needed repair" is evidence; "0" is a claim.

* **Nothing is deferred, so every zero has to carry its denominator.** The
  module now implements the whole `Aligner` protocol, and the three fields that
  used to be placeholders -- `apportioned`, `apportionment_error` and
  `units_below_cell_threshold` -- hold measured results. Two of them are
  legitimately zero on this county, which is exactly why `ApportionEvidence` and
  `ZonalEvidence` record what was compared and what was measured beside them.
  `units_below_cell_threshold` says so out loud when it is zero because no
  raster was measured at all: that is the old "not built looks like nothing to
  do" confusion arriving through a different door.

`AlignmentReport.reprojected` and `.temporal_span` are read from each dataset's
`Provenance`, not recomputed here. A CRS this module worked out for itself would
be the same assumption `acquire._received_crs` exists to refuse.
"""

from __future__ import annotations

import inspect
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from pyproj import CRS, Geod
from rasterio import features as rasterio_features
from rasterio import mask as rasterio_mask
from shapely import wkt as shapely_wkt
from shapely.geometry import Point, Polygon

from . import acquire, config
from . import provenance as prov
from .contracts import (
    MIN_RASTER_CELLS,
    VULNERABILITY_INDICATORS,
    AlignmentReport,
    Aligner,
    Col,
    Geoid,
    Provenance,
)
from .registry import Registry

SENTINEL_DIGITS: tuple[int, ...] = tuple(range(1, 10))
SENTINEL_WIDTHS: tuple[int, ...] = (9, 10)


def census_jam_values() -> frozenset[int]:
    """The Census "jam value" family, generated rather than pasted.

    The Bureau encodes an unavailable estimate as a large negative repeated
    digit: -666666666 for an estimate it could not compute, -888888888 for not
    applicable, -999999999 for suppressed, and further siblings whose exact
    membership shifts between releases. Writing that list down makes correctness
    depend on the list being complete, and no published list is load-bearing
    enough to bet a column on.

    So the list is deliberately not load-bearing. `scrub_sentinels` removes every
    negative value, because none of the quantities this project scrubs -- counts
    of people, counts of households, margins of error -- has a negative value
    anywhere in its domain. This family only *labels* what was found, separating
    a known jam code from an unexpected negative worth reading twice. An unlisted
    sentinel is removed either way.
    """
    return frozenset(
        -int(str(digit) * width) for digit in SENTINEL_DIGITS for width in SENTINEL_WIDTHS
    )


JAM_VALUES: frozenset[int] = census_jam_values()

ACS_RESOLVED_PREFIX = "resolved:"
ACS_ESTIMATES_KEY = "estimates"
ACS_MARGINS_KEY = "margins"
DEGRADED_KEY = "degraded"
DEGRADED_NOTE_PREFIX = "DEGRADED"
"""Keys `acquire` writes into `Provenance.request_params`, read here and never
guessed at. A degraded dataset is recognised by `DEGRADED_KEY`, never by having
no rows: "retrieved and found nothing" and "could not be retrieved" are
different facts about the county and only the provenance separates them."""

JOINED_SUFFIX = "_joined"


def is_degraded(record: Provenance) -> bool:
    """Did this dataset fail to retrieve, as opposed to retrieving nothing?

    The only test, and deliberately not `len(gdf) == 0`. A flood-zone layer with
    no rows means the service could not be reached; a flood-zone layer with no
    rows also means an inland county has no mapped hazard. Those are opposite
    facts and the row count cannot tell them apart -- only the flag `acquire`
    wrote when it caught the failure can.
    """
    return record.request_params.get(DEGRADED_KEY) == "true"

ZONAL_STATISTICS: tuple[str, ...] = ("min", "mean", "max", "count")
"""Every statistic `zonal_stats` computes. Anything else is refused by name
rather than returned as a null column, which is how a caller learns it asked for
something this module does not do instead of reading nulls as missing data."""

RASTER_NO_OVERLAP = "do not overlap raster"
"""The one `rasterio.mask` failure that is a fact about the county rather than a
bug: a census unit sitting off the edge of what the service covered.

Matched by message rather than by type, because `ValueError` is also what a
malformed geometry raises, and recording that as "outside the raster" would
attach a confident and false explanation to it -- the warning says "a gap in the
retrieved coverage, not flat ground". The refusal checks in this file are held to
naming the reason they expect; this catch is held to the same standard."""

ALL_TOUCHED = False
"""Which cells a polygon owns: those whose CENTRE falls inside it, not every
cell the boundary clips. Recorded in `ZonalEvidence` and printed, because the
choice changes every number in the result and only one of the two was verified
against an independently computed value. `True` would double-count a cell shared
by two adjacent census units and inflate the total cell count above the raster's.
"""

RASTER_STAT_COLUMNS: dict[str, str] = {
    "min": Col.ELEV_MIN_M,
    "mean": Col.ELEV_MEAN_M,
    "count": Col.RASTER_CELLS,
}
"""How a generic statistic becomes a named elevation column, applied by
`align_snapshot` and by nothing inside `zonal_stats`.

`zonal_stats` is a raster utility that does not know it is looking at elevation,
so it returns "min" and "mean"; the caller that does know the semantics names
them. There is deliberately no entry for "max": `contracts.Col` publishes
ELEV_MIN_M and ELEV_MEAN_M and no ELEV_MAX_M, and inventing one here as a string
literal is exactly what the frozen-contract rule forbids. Session 8 maps the
same generic names onto the inundation columns `Col` does publish."""

CONTROLLED_TO_TRACT = (
    "this zero is guaranteed rather than measured: the Census controls block-group "
    "population to sum to the published tract estimate, so population is the one "
    "variable whose apportionment cannot disagree. An uncontrolled variable would"
)
"""Why the apportionment error for population is exactly zero on real data.

Worth stating wherever the zero is printed. A reader who takes it as evidence
that apportionment is accurate in general has drawn the wrong conclusion from
it: the agreement is a property of how the Bureau publishes this one variable,
not a measurement of this module."""

APPORTION_METHODS: tuple[str, ...] = ("sum", "population_weighted")
"""The two aggregations, and which column each is right for. A count adds up:
the people in a tract are the people in its block groups. A rate does not --
averaging percentages over units of wildly different size is a well-known way to
get a number that is wrong in the direction of the smallest unit -- so a share is
weighted by the population it describes."""

MOE_RULE = "root of the summed squares, the Bureau's published rule for the margin of a derived sum"

METRIC_OPERATIONS: dict[str, str] = {
    "to_crs": r"\.to_crs\(",
    "area": r"\.area\b",
    "buffer": r"\.buffer\(",
    "centroid": r"\.centroid\b",
    "distance": r"\.distance\(",
    "length": r"\.length\b",
}
"""Every call invariant 2 covers. The implementation half of this file may
contain exactly one of these -- the `to_crs` inside `to_working_crs` -- because
each of the others answers in degrees on a geographic frame, silently.

What counts as the implementation half is `Alignment`, `EVIDENCE_RECORDS` and the
module-level helpers named in `_contract_checks`. The self check is outside that
set and may use these calls freely, since a fixture has to measure an area to
compare one. That is an exemption by omission, and it is the scan's weak point:
an implementation helper that nobody adds to the list is exempt without anyone
choosing to exempt it. `EVIDENCE_RECORDS` exists to shrink that surface."""


def sentinel_label(value: float) -> str:
    """Name a removed value: a documented jam code, or something else."""
    if float(value).is_integer() and int(value) in JAM_VALUES:
        return f"{int(value)} (census jam code)"
    return f"{value:g} (negative, not a known jam code)"


def _as_narrow_numeric(values: pd.Series) -> pd.Series:
    """Cast a coerced column to the narrowest nullable numeric type it fits.

    Read from the data rather than assumed: a column whose present values are
    all whole becomes a nullable integer, so a population does not arrive
    downstream as 4200.0, and anything else stays floating point. Nullable
    either way, because a scrubbed sentinel has to be absent rather than zero.
    """
    present = values.dropna()
    if len(present) and bool(np.isfinite(present).all()) and bool((present % 1 == 0).all()):
        if float(present.abs().max()) < 2**53:
            return values.astype("Int64")
    return values.astype("Float64")


CENSUS_GEOID_WIDTHS: frozenset[int] = frozenset({2, 5, 11, 12, 15})
"""The published fixed widths of a Census GEOID: state, county, tract, block
group, block. A property of the identifier's definition, not a value retrieved
from anywhere, and used only to tell one failure apart from another -- a GEOID of
some other width has lost a character rather than describing a coarser geography.
"""


def _geoid_strings(values: pd.Series, side: str) -> pd.Series:
    """Normalise a GEOID column to text, refusing one that arrived as a number.

    An integer GEOID has already lost the leading zero of state codes 01 through
    09, and the loss is invisible on a county whose state code starts with
    something else. Refusing it rather than casting it back is the difference
    between a transfer run that fails loudly and one that joins nothing and
    reports an empty county.

    Both the dtype and the values are checked. An object column holding Python
    ints passes every dtype test there is and has still already lost the zero,
    and that is the shape a frame built inside `run_spatial_code` arrives in.
    The width is then checked against the published forms, because the symptom
    of a lost leading zero -- a width of ten where eleven was meant -- is
    otherwise indistinguishable downstream from a genuine granularity mismatch,
    and the two want opposite responses.
    """
    if pd.api.types.is_numeric_dtype(values.dtype):
        raise ValueError(
            f"{side}: {Col.GEOID} arrived as {values.dtype}, not text. A numeric GEOID has "
            "already dropped the leading zero of state codes 01-09; re-read the source as "
            "text rather than casting it back here"
        )
    present = values.dropna()
    if len(present) and all(isinstance(value, (int, float, np.number)) for value in present):
        raise ValueError(
            f"{side}: {Col.GEOID} is an object column holding {type(present.iloc[0]).__name__} "
            "values. The dtype is text but the values are numbers, so the leading zero of "
            "state codes 01-09 is already gone; re-read the source as text"
        )
    text = values.astype("string").str.strip()
    widths = set(_width_counts(text.dropna()))
    unpublished = sorted(widths - CENSUS_GEOID_WIDTHS)
    if unpublished:
        raise ValueError(
            f"{side}: {Col.GEOID} carries width(s) {unpublished}, and a Census GEOID is one "
            f"of {sorted(CENSUS_GEOID_WIDTHS)} characters. A width one short of a published "
            "form is a dropped leading zero, not a coarser geography"
        )
    return text


AREAL_TYPES: frozenset[str] = frozenset({"Polygon", "MultiPolygon"})
"""Geometry types that enclose area. `make_valid` does not promise to return
one: handed a polygon whose ring doubles back on itself it returns a line, which
is valid, is not empty, and encloses nothing. A layer carrying one of those
reports an area of zero for that unit with nothing anywhere saying why."""


def _make_valid(geometries: gpd.GeoSeries) -> gpd.GeoSeries:
    """`make_valid` over a GeoSeries, surviving a geometry GEOS refuses to touch.

    A coordinate that is not a number reaches GEOS as NaN, and make_valid raises
    rather than returning something invalid -- which would end a run over one bad
    feature out of hundreds, on a rubric criterion that is specifically about
    surviving malformed data. Vectorised first, because that is the normal path
    and the fallback costs a Python loop. The fallback marks the offenders null
    so the caller drops and counts them like any other unrepairable geometry,
    rather than deciding here what a failed repair means.
    """
    try:
        return geometries.make_valid()
    except Exception:
        one_by_one: list[Any] = []
        for geometry in geometries:
            try:
                one_by_one.append(
                    gpd.GeoSeries([geometry], crs=geometries.crs).make_valid().iloc[0]
                )
            except Exception:
                one_by_one.append(None)
        return gpd.GeoSeries(one_by_one, index=geometries.index, crs=geometries.crs)


def _width_counts(values: pd.Series) -> dict[int, int]:
    counts = values.str.len().value_counts().sort_index()
    return {int(width): int(count) for width, count in counts.items()}


# ---------------------------------------------------------------------------
# evidence -- the denominators that make a reported zero readable
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RepairEvidence:
    """What `repair_geometry` looked at, beside what it changed."""

    dataset: str = ""
    examined: int = 0
    invalid_before: int = 0
    repaired: int = 0
    still_invalid: int = 0
    collapsed: int = 0
    missing: int = 0
    empty: int = 0
    type_changed: int = 0

    @property
    def dropped(self) -> int:
        return self.missing + self.empty + self.still_invalid + self.collapsed


@dataclass(slots=True)
class ScrubEvidence:
    """What `scrub_sentinels` looked at, beside what it removed."""

    dataset: str = ""
    columns: tuple[str, ...] = ()
    cells: int = 0
    values: int = 0
    non_numeric: int = 0
    negative: int = 0
    examined: dict[str, int] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)
    codes: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class JoinEvidence:
    """What `join_on_geoid` compared, beside what failed to match."""

    left: str = ""
    right: str = ""
    left_rows: int = 0
    right_rows: int = 0
    matched: int = 0
    unmatched_left: int = 0
    unmatched_right: int = 0
    left_widths: dict[int, int] = field(default_factory=dict)
    right_widths: dict[int, int] = field(default_factory=dict)


@dataclass(slots=True)
class ZonalEvidence:
    """What `zonal_stats` read, beside the statistics it returned.

    `units_below_cell_threshold` is a single integer on the frozen report, and on
    a county whose smallest census unit still covers hundreds of cells it is
    zero. The denominators that make that zero readable -- how many polygons were
    measured, how few cells the smallest one covered, and against what threshold
    -- live here.
    """

    dataset: str = ""
    raster: str = ""
    raster_crs: str = ""
    cell_size_m: float = 0.0
    cell_area_m2: float = 0.0
    polygons: int = 0
    cells: int = 0
    nodata_cells: int = 0
    non_finite_cells: int = 0
    empty_polygons: int = 0
    outside_raster: int = 0
    below_threshold: int = 0
    threshold: int = MIN_RASTER_CELLS
    min_cells: int = 0
    median_cells: float = 0.0
    max_cells: int = 0
    stats: tuple[str, ...] = ()
    all_touched: bool = ALL_TOUCHED

    @property
    def valid_cells(self) -> int:
        """Cells that carried a usable number, as opposed to merely being inside.

        The three categories are counted separately and must add back up to
        `cells`; a check asserts exactly that. Folding nodata into "outside the
        polygon" is how a raster hole becomes an invisibly smaller denominator.
        """
        return self.cells - self.nodata_cells - self.non_finite_cells


@dataclass(slots=True)
class ApportionEvidence:
    """What `apportion` rolled up, beside the error it reported.

    `AlignmentReport.apportionment_error` carries one percentage per column. On
    this county that percentage is zero for population, and the zero is real
    rather than untested -- so what it was computed over has to travel with it:
    how many units were compared, how many of those publish a value a percentage
    can be taken against, and the absolute difference, which stays defined for
    the unit that publishes none.
    """

    fine: str = ""
    coarse: str = ""
    method: str = ""
    method_note: str = ""
    weight_column: str = ""
    fine_rows: int = 0
    coarse_rows: int = 0
    fine_width: int = 0
    coarse_width: int = 0
    parents: int = 0
    orphan_children: int = 0
    childless_parents: int = 0
    columns: tuple[str, ...] = ()
    compared: tuple[str, ...] = ()
    not_published: tuple[str, ...] = ()
    units_compared: dict[str, int] = field(default_factory=dict)
    units_undefined: dict[str, int] = field(default_factory=dict)
    incomplete: dict[str, int] = field(default_factory=dict)
    error: dict[str, float] = field(default_factory=dict)
    max_abs_difference: dict[str, float] = field(default_factory=dict)
    total_fine: dict[str, float] = field(default_factory=dict)
    total_coarse: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class AlignmentEvidence:
    """Everything `AlignmentReport` counts, with the denominator beside it.

    `AlignmentReport` is frozen in `contracts.py` and carries only numerators.
    Rather than overload one of its dicts -- an all-zero `sentinels_removed`
    would read as truthy to any later `if report.sentinels_removed:` and claim
    the opposite of what happened -- the denominators live here and are printed
    alongside by `format_report`.
    """

    repairs: list[RepairEvidence] = field(default_factory=list)
    scrubs: list[ScrubEvidence] = field(default_factory=list)
    joins: list[JoinEvidence] = field(default_factory=list)
    zonals: list[ZonalEvidence] = field(default_factory=list)
    apportionments: list[ApportionEvidence] = field(default_factory=list)
    datasets: tuple[str, ...] = ()
    crs_observed: dict[str, str] = field(default_factory=dict)
    degraded: dict[str, str] = field(default_factory=dict)
    derived: dict[str, str] = field(default_factory=dict)
    undefined: dict[str, int] = field(default_factory=dict)
    columns_dropped: dict[str, int] = field(default_factory=dict)

    @property
    def geometries_examined(self) -> int:
        return sum(item.examined for item in self.repairs)

    @property
    def geometries_invalid(self) -> int:
        return sum(item.invalid_before for item in self.repairs)

    @property
    def cells_examined(self) -> int:
        return sum(item.cells for item in self.scrubs)

    @property
    def values_examined(self) -> int:
        return sum(item.values for item in self.scrubs)

    @property
    def non_numeric(self) -> int:
        return sum(item.non_numeric for item in self.scrubs)

    @property
    def scrub_columns(self) -> int:
        return sum(len(item.columns) for item in self.scrubs)

    @property
    def geoids_compared(self) -> int:
        return sum(item.left_rows + item.right_rows for item in self.joins)

    @property
    def geoids_matched(self) -> int:
        return sum(item.matched for item in self.joins)

    @property
    def polygons_measured(self) -> int:
        return sum(item.polygons for item in self.zonals)

    @property
    def smallest_polygon_cells(self) -> int:
        measured = [item.min_cells for item in self.zonals if item.polygons]
        return min(measured) if measured else 0

    @property
    def units_apportioned(self) -> int:
        return sum(item.fine_rows for item in self.apportionments)

    def codes_seen(self) -> dict[str, int]:
        """Every distinct removed value, labelled, across all scrubbed tables."""
        seen: dict[str, int] = {}
        for item in self.scrubs:
            for label, count in item.codes.items():
                seen[label] = seen.get(label, 0) + count
        return seen


@dataclass(slots=True)
class AlignedSnapshot:
    """The cleaned snapshot: named frames, the frozen report, and the evidence."""

    frames: dict[str, Any]
    report: AlignmentReport
    evidence: AlignmentEvidence


EVIDENCE_RECORDS: tuple[type, ...] = (
    RepairEvidence,
    ScrubEvidence,
    JoinEvidence,
    ZonalEvidence,
    ApportionEvidence,
    AlignmentEvidence,
    AlignedSnapshot,
)
"""Every record the implementation half of this module defines.

Named once and read by both source scans -- the metric-operation scan in
`_contract_checks` and the annotation scan in `_module_functions` -- because they
were previously two hand-maintained lists of the same thing and only one of them
was kept up to date. These carry real bodies: `valid_cells`, `polygons_measured`
and `units_apportioned` all run inside `format_report` and `align_snapshot`, so a
metric operation written into one of them is production code. An inclusion list
fails silently when something is left off it, which is the worst way for a guard
to fail, so there is now one list rather than two."""


# ---------------------------------------------------------------------------
# the aligner
# ---------------------------------------------------------------------------


class Alignment:
    """Implements the frozen `Aligner` protocol for one study area.

    A class rather than a module, unlike `acquire`, because every method here
    needs the working CRS and the working CRS belongs to the study area. Reading
    it from a module-level constant would be the county leaking back in through
    the one door `config.StudyArea` exists to close: the transfer run swaps the
    study area, and a global would keep pointing at the first one.

    The registry is the source of both. It already carries the study area, the
    working CRS and every dataset's `Provenance`, which is what lets
    `align_snapshot` fill `reprojected` and `temporal_span` from what was
    actually retrieved rather than from anything computed here.
    """

    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = registry or Registry()
        self._geod = Geod(ellps="WGS84")

    @property
    def working_crs(self) -> str:
        return self.registry.working_crs

    @property
    def study_area(self) -> config.StudyArea:
        return self.registry.study_area

    # -- invariant 2: the single metric-CRS choke point ---------------------

    def to_working_crs(self, obj: Any) -> Any:
        """Return a spatial object in the metric working CRS.

        Invariant 2 names this helper by position: every area, distance, buffer,
        centroid and zonal operation goes through it. It looks redundant on data
        that came from `Registry.load`, which already reprojects, and that is
        exactly the trap -- a frame built in memory, read from a file directly,
        or handed back by a service mid-run has whatever CRS it has, and a
        geographic one produces degree-based areas with no error to notice.

        Idempotent by identity: an object already in the working CRS is returned
        as it is, so a second call costs nothing and repeated routing through the
        choke point is free. A missing CRS raises rather than defaulting, because
        guessing here is how a degree-based number reaches a paper.
        """
        if isinstance(obj, (str, Path)):
            raise TypeError(
                f"to_working_crs was handed a path ({str(obj)!r}). A raster is reached with "
                "Registry.path_of and reprojected by zonal_stats; only vector objects "
                "carrying a CRS pass through here"
            )
        if not hasattr(obj, "to_crs") or not hasattr(obj, "crs"):
            raise TypeError(
                f"to_working_crs needs a GeoDataFrame or GeoSeries, got {type(obj).__name__}"
            )
        if obj.crs is None:
            raise ValueError(
                f"cannot reproject a {type(obj).__name__} with no CRS to {self.working_crs}. "
                "An area or distance taken from it would be in whatever units the "
                "coordinates happen to be in, and nothing downstream would notice"
            )
        target = CRS.from_user_input(self.working_crs)
        if CRS.from_user_input(obj.crs) == target:
            return obj
        return obj.to_crs(target)

    # -- geometry repair ----------------------------------------------------

    def repair_geometry(self, gdf: Any) -> tuple[Any, int]:
        """Repair invalid geometry, returning the frame and the repair count.

        The frozen signature carries one number. Rows that were dropped rather
        than repaired -- missing, empty, or still invalid after repair -- are
        counted too, and `repair_geometry_detailed` is how the report reaches
        them without changing the contract.
        """
        repaired, evidence = self.repair_geometry_detailed(gdf)
        return repaired, evidence.repaired

    def repair_geometry_detailed(
        self, gdf: Any, *, dataset: str = ""
    ) -> tuple[gpd.GeoDataFrame, RepairEvidence]:
        """`repair_geometry`, keeping the denominator.

        `make_valid` is what does the work, and it can hand back a different
        geometry type: repairing a self-intersecting polygon usually yields a
        multipolygon, occasionally a collection that carries a stray line. That
        change is recorded rather than hidden, since a downstream area is
        unaffected by it and a downstream boundary trace is not.

        A geometry still invalid after repair is dropped, not carried: an
        invalid polygon poisons every overlay it touches, and one that survived
        `make_valid` is not going to be fixed by being passed along.

        So is one that stopped enclosing area. `make_valid` handed a polygon
        whose ring doubles back on itself returns a line -- valid, not empty, and
        worth nothing to a zonal statistic. Kept, it would report an area of zero
        for a real census unit with nothing anywhere saying why, which is worse
        than dropping it and counting it. Repair here has to preserve dimension,
        not just validity.

        An empty layer passes straight through. The degraded flood-zone dataset
        arrives here with zero rows on a normal run, and `total_bounds` on an
        empty frame is nan -- so the early return is not a nicety.
        """
        if not isinstance(gdf, gpd.GeoDataFrame):
            raise TypeError(
                f"repair_geometry needs a GeoDataFrame, got {type(gdf).__name__}"
            )
        frame = self.to_working_crs(gdf).copy()
        evidence = RepairEvidence(dataset=dataset, examined=len(frame))
        if evidence.examined == 0:
            return frame, evidence

        geometry = frame.geometry
        missing = geometry.isna()
        empty = geometry.is_empty.fillna(False) & ~missing
        evidence.missing = int(missing.sum())
        evidence.empty = int(empty.sum())
        frame = frame.loc[~(missing | empty)].copy()
        if len(frame) == 0:
            return frame, evidence

        geometry = frame.geometry
        invalid = ~geometry.is_valid
        evidence.invalid_before = int(invalid.sum())
        if evidence.invalid_before == 0:
            return frame, evidence

        was = geometry.geom_type
        areal = was.isin(AREAL_TYPES)
        fixed = geometry.copy()
        fixed.loc[invalid] = _make_valid(geometry.loc[invalid])
        usable = fixed.notna() & fixed.is_valid & ~fixed.is_empty.fillna(True)
        kept_area = ~areal | fixed.geom_type.isin(AREAL_TYPES)
        recovered = usable & kept_area
        evidence.repaired = int((invalid & recovered).sum())
        evidence.still_invalid = int((invalid & ~usable).sum())
        evidence.collapsed = int((invalid & usable & ~kept_area).sum())
        evidence.type_changed = int((invalid & recovered & (fixed.geom_type != was)).sum())
        frame = frame.set_geometry(fixed)
        return frame.loc[~(invalid & ~recovered)].copy(), evidence

    # -- sentinel scrubbing -------------------------------------------------

    def scrub_sentinels(self, df: Any, value_cols: list[str]) -> tuple[Any, dict[str, int]]:
        """Remove sentinel values from `value_cols`, returning per-column counts.

        Only columns something was actually removed from appear in the returned
        dict. Reporting an examined column with a count of zero would read as
        truthy to any later `if removed:` and claim the opposite of what
        happened; the examined counts live in `ScrubEvidence` instead.
        """
        scrubbed, evidence = self.scrub_sentinels_detailed(df, value_cols)
        return scrubbed, {column: count for column, count in evidence.removed.items() if count}

    def scrub_sentinels_detailed(
        self, df: Any, value_cols: list[str], *, dataset: str = ""
    ) -> tuple[pd.DataFrame, ScrubEvidence]:
        """`scrub_sentinels`, keeping the denominator.

        The test is `value < 0`, not membership of a list of jam codes. Every
        quantity scrubbed here is a count of people or households or a margin of
        error on one, and none of those is ever negative, so the sign is a
        stronger and more durable guard than any enumeration of the Bureau's
        codes. `census_jam_values` then labels what was found, which is what
        separates a documented suppression from a negative nobody expected.

        Values arrive as text -- `acquire.fetch_acs` returns exactly what the
        API sent, deliberately -- so casting is part of the job. A value that is
        not a number becomes null, and is counted separately from a sentinel so
        that a parsing problem cannot hide inside a suppression count.
        """
        absent = [column for column in value_cols if column not in df.columns]
        if absent:
            raise KeyError(
                f"scrub_sentinels was asked for {len(absent)} column(s) the frame does not "
                f"carry: {absent[:8]}"
            )
        frame = df.copy()
        evidence = ScrubEvidence(dataset=dataset, columns=tuple(value_cols))
        for column in value_cols:
            raw = frame[column]
            present = raw.notna()
            found = int(present.sum())
            evidence.cells += int(len(raw))
            evidence.values += found
            evidence.examined[column] = found

            numeric = pd.to_numeric(raw, errors="coerce")
            unparsed = int((present & numeric.isna()).sum())
            evidence.non_numeric += unparsed

            negative = numeric.notna() & (numeric < 0)
            removed = int(negative.sum())
            evidence.removed[column] = removed
            evidence.negative += removed
            for value, times in numeric[negative].value_counts().items():
                label = sentinel_label(float(value))
                evidence.codes[label] = evidence.codes.get(label, 0) + int(times)

            frame[column] = _as_narrow_numeric(numeric.mask(negative))
        return frame, evidence

    # -- the GEOID audit ----------------------------------------------------

    def join_on_geoid(self, geom: Any, attrs: Any) -> tuple[Any, AlignmentReport]:
        """Join attributes onto geometry by GEOID, reporting what did not match.

        The join is inner, so the frame that comes back is the part both sides
        agree on -- but nothing is dropped silently: every GEOID present on one
        side and absent from the other is named in the report, which is the point
        of the audit. A join that quietly returns fewer rows than either input is
        how a county loses tracts without anyone noticing.

        The report this returns covers the join only. `reprojected` and
        `temporal_span` come from `Provenance`, which a pair of frames does not
        carry; `align_snapshot` fills them.
        """
        joined, report, _ = self.join_on_geoid_detailed(geom, attrs)
        return joined, report

    def join_on_geoid_detailed(
        self, geom: Any, attrs: Any, *, left: str = "", right: str = ""
    ) -> tuple[gpd.GeoDataFrame, AlignmentReport, JoinEvidence]:
        """`join_on_geoid`, keeping the denominator.

        Also audits GEOID width. A tract GEOID is eleven characters and a block
        group is twelve, so two sides whose widths disagree are two different
        geographic levels rather than a bad match, and joining them produces
        nothing at all rather than something wrong. That case is named in the
        warnings, because "0 matched" and "0 matched, and here is why" are very
        different things to hand a reader.
        """
        left_name = left or "geometry side"
        right_name = right or "attribute side"
        for label, frame in ((left_name, geom), (right_name, attrs)):
            if Col.GEOID not in frame.columns:
                raise KeyError(
                    f"{label}: no {Col.GEOID} column to join on; it carries "
                    f"{list(frame.columns)[:10]}"
                )

        left_frame = self.to_working_crs(geom).copy()
        right_frame = attrs.copy()
        left_frame[Col.GEOID] = _geoid_strings(left_frame[Col.GEOID], left_name)
        right_frame[Col.GEOID] = _geoid_strings(right_frame[Col.GEOID], right_name)

        for label, frame in ((left_name, left_frame), (right_name, right_frame)):
            repeated = int(frame[Col.GEOID].duplicated().sum())
            if repeated:
                raise ValueError(
                    f"{label}: {repeated} of {len(frame)} rows repeat a {Col.GEOID}. "
                    "A repeated key fans the join out into more rows than either input "
                    "had, which reads downstream as a larger county"
                )

        left_ids = set(left_frame[Col.GEOID])
        right_ids = set(right_frame[Col.GEOID])
        unmatched_left = tuple(sorted(left_ids - right_ids))
        unmatched_right = tuple(sorted(right_ids - left_ids))

        evidence = JoinEvidence(
            left=left_name,
            right=right_name,
            left_rows=len(left_frame),
            right_rows=len(right_frame),
            unmatched_left=len(unmatched_left),
            unmatched_right=len(unmatched_right),
            left_widths=_width_counts(left_frame[Col.GEOID]),
            right_widths=_width_counts(right_frame[Col.GEOID]),
        )

        joined = left_frame.merge(
            right_frame, on=Col.GEOID, how="inner", validate="one_to_one", suffixes=("", "_attr")
        )
        evidence.matched = len(joined)

        report = AlignmentReport(unmatched_left=unmatched_left, unmatched_right=unmatched_right)
        if unmatched_left:
            report.warnings.append(
                f"{left_name}: {len(unmatched_left)} of {len(left_frame)} geometries have no "
                f"attribute row and were not joined, beginning {list(unmatched_left[:5])}"
            )
        if unmatched_right:
            report.warnings.append(
                f"{right_name}: {len(unmatched_right)} of {len(right_frame)} attribute rows "
                f"have no geometry and were not joined, beginning {list(unmatched_right[:5])}"
            )
        if set(evidence.left_widths) != set(evidence.right_widths):
            report.warnings.append(
                f"{left_name} carries {Col.GEOID} widths {sorted(evidence.left_widths)} and "
                f"{right_name} carries {sorted(evidence.right_widths)}: these are different "
                "geographic levels, so this is a granularity mismatch rather than a bad "
                "join. Apportion one to the other instead"
            )
        return joined, report, evidence

    # -- granularity: a finer geography rolled up to a coarser one -----------

    def apportion(
        self,
        fine: Any,
        coarse: Any,
        columns: list[str],
        *,
        method: Literal["sum", "population_weighted"],
    ) -> tuple[Any, dict[str, float]]:
        """Aggregate `fine` units onto `coarse` ones and report the error.

        The frozen signature carries the aggregated frame and one percentage per
        column. Everything that percentage was computed over -- units compared,
        units whose published value a percentage cannot be taken against, the
        absolute difference -- is in `ApportionEvidence`, which is how the report
        reaches them without changing the contract.
        """
        aggregated, evidence = self.apportion_detailed(fine, coarse, columns, method=method)
        return aggregated, evidence.error

    def apportion_detailed(
        self,
        fine: Any,
        coarse: Any,
        columns: list[str],
        *,
        method: Literal["sum", "population_weighted"],
        weight_column: str = Col.POPULATION,
        fine_name: str = "",
        coarse_name: str = "",
    ) -> tuple[pd.DataFrame, ApportionEvidence]:
        """`apportion`, keeping the denominator.

        **The rollup is a string operation, not a spatial one.** A block-group
        GEOID is its tract's GEOID plus one digit, so the identifier already
        carries the nesting exactly, and taking the leading characters is not an
        approximation of centroid containment -- it is the published definition.
        That is why nothing here routes through `to_working_crs`: there is no
        area, distance or overlay to get wrong, and CLAUDE.md puts area-weighted
        apportionment out of scope in as many words. The prefix width is read
        from the coarse frame's own GEOIDs rather than written down, so the same
        call rolls blocks into block groups on a county that has them.

        **Which aggregation is right depends on the column, not on the caller's
        preference.** A count adds: the people in a tract are the people in its
        block groups. A share does not -- averaging percentages over units of
        very different size pulls the answer toward the smallest unit -- so
        `population_weighted` weights each child by the population it describes.
        Asking for a population-weighted population is refused rather than
        answered, because it computes sum of p squared over sum of p, which is a
        real number, is not the population, and looks entirely plausible sitting
        in a table.

        **A suppressed child suppresses its parent.** A tract one of whose block
        groups carries no value gets no value, not the sum of the rest. The
        alternative is an undercount that looks like a smaller tract, which is
        the same failure `derive_acs_columns` refuses when it sums ACS leaves.

        **The error is the worst unit, not the county.** A total can agree while
        individual units are wrong in opposite directions; a maximum cannot
        cancel. It is taken over the units whose published value is non-zero,
        because a relative error against a published zero is undefined -- and
        this county has such a unit, a water tract with no residents. Those units
        are counted in `units_undefined` rather than skipped in silence, and the
        absolute difference, which stays defined for them, is recorded beside it.
        """
        if method not in APPORTION_METHODS:
            raise ValueError(
                f"apportion does not know the method {method!r}; it knows "
                f"{list(APPORTION_METHODS)}"
            )
        if not columns:
            raise ValueError("apportion was asked to roll up no column at all")
        left = fine_name or "fine geography"
        right = coarse_name or "coarse geography"
        for label, frame in ((left, fine), (right, coarse)):
            if Col.GEOID not in frame.columns:
                raise KeyError(
                    f"{label}: no {Col.GEOID} column to apportion on; it carries "
                    f"{list(frame.columns)[:10]}"
                )
        absent = [column for column in columns if column not in fine.columns]
        if absent:
            raise KeyError(
                f"{left}: apportion was asked for {len(absent)} column(s) the frame does "
                f"not carry: {absent[:8]}"
            )
        if method == "population_weighted":
            if weight_column not in fine.columns:
                raise KeyError(
                    f"{left}: population_weighted needs {weight_column!r} to weight by and "
                    "the frame does not carry it"
                )
            if any(column == weight_column for column in columns):
                raise ValueError(
                    f"population_weighted was asked to weight {weight_column!r} by itself, "
                    "which computes the sum of the squares over the sum -- a plausible "
                    f"number that is not the {weight_column}. A count uses method='sum'"
                )

        fine_frame = fine.copy()
        coarse_frame = coarse.copy()
        fine_frame[Col.GEOID] = _geoid_strings(fine_frame[Col.GEOID], left)
        coarse_frame[Col.GEOID] = _geoid_strings(coarse_frame[Col.GEOID], right)
        repeated = int(coarse_frame[Col.GEOID].duplicated().sum())
        if repeated:
            raise ValueError(
                f"{right}: {repeated} of {len(coarse_frame)} rows repeat a {Col.GEOID}, so "
                "there is no single published value to compare an aggregate against"
            )

        for label, frame in ((left, fine_frame), (right, coarse_frame)):
            if not len(frame):
                raise ValueError(
                    f"{label}: carries no rows, so there is nothing to apportion and no "
                    "prefix width to read from it. An empty frame is not a granularity "
                    "mismatch, and join_on_geoid returns one rather than raising"
                )

        fine_widths = _width_counts(fine_frame[Col.GEOID].dropna())
        coarse_widths = _width_counts(coarse_frame[Col.GEOID].dropna())
        for label, widths in ((left, fine_widths), (right, coarse_widths)):
            if len(widths) != 1:
                raise ValueError(
                    f"{label}: {Col.GEOID} carries widths {sorted(widths)}, so this frame "
                    "holds more than one geographic level and no single prefix rolls it up"
                )
        fine_width = next(iter(fine_widths))
        coarse_width = next(iter(coarse_widths))
        if fine_width <= coarse_width:
            raise ValueError(
                f"{left} carries {Col.GEOID} width {fine_width} and {right} carries "
                f"{coarse_width}: the fine geography must be the longer identifier, since "
                "apportionment goes from smaller units to larger ones"
            )

        parent = fine_frame[Col.GEOID].str[:coarse_width]
        published_ids = set(coarse_frame[Col.GEOID])
        evidence = ApportionEvidence(
            fine=left,
            coarse=right,
            method=method,
            weight_column=weight_column if method == "population_weighted" else "",
            fine_rows=len(fine_frame),
            coarse_rows=len(coarse_frame),
            fine_width=fine_width,
            coarse_width=coarse_width,
            parents=int(parent.nunique()),
            orphan_children=int((~parent.isin(published_ids)).sum()),
            childless_parents=len(published_ids - set(parent.dropna())),
            columns=tuple(columns),
        )
        evidence.method_note = (
            f"{method} of {evidence.fine_rows} {left} value(s) into {evidence.parents} "
            f"{right} unit(s) by {Col.GEOID} prefix, width {fine_width} -> {coarse_width}"
        )

        sizes = parent.groupby(parent).size()
        published_frame = coarse_frame.set_index(Col.GEOID)
        aggregated = pd.DataFrame(index=sizes.index.rename(Col.GEOID))
        compared: list[str] = []
        not_published: list[str] = []
        for column in columns:
            value = pd.to_numeric(fine_frame[column], errors="coerce")
            present = value.notna()
            complete = present.groupby(parent).sum() == sizes
            evidence.incomplete[column] = int((~complete).sum())

            if method == "sum":
                rolled = value.groupby(parent).sum(min_count=1)
            else:
                weight = pd.to_numeric(fine_frame[weight_column], errors="coerce")
                usable = present & weight.notna()
                numerator = (value * weight).where(usable).groupby(parent).sum(min_count=1)
                denominator = weight.where(usable).groupby(parent).sum(min_count=1)
                rolled = numerator / denominator.where(denominator > 0)
            aggregated[column] = pd.to_numeric(
                rolled.where(complete), errors="coerce"
            ).astype("Float64")

            if column not in published_frame.columns:
                not_published.append(column)
                continue
            compared.append(column)
            published = pd.to_numeric(
                published_frame[column], errors="coerce"
            ).astype("Float64")
            pair = pd.DataFrame(
                {"published": published, "aggregated": aggregated[column]}
            ).dropna()
            evidence.units_compared[column] = len(pair)
            evidence.total_fine[column] = float(pair["aggregated"].sum())
            evidence.total_coarse[column] = float(pair["published"].sum())
            difference = (pair["aggregated"] - pair["published"]).abs()
            evidence.max_abs_difference[column] = (
                float(difference.max()) if len(pair) else 0.0
            )
            defined = pair["published"] != 0
            evidence.units_undefined[column] = int((~defined).sum())
            if int(defined.sum()):
                relative = 100.0 * difference[defined] / pair.loc[defined, "published"].abs()
                evidence.error[column] = float(relative.max())

        evidence.compared = tuple(compared)
        evidence.not_published = tuple(not_published)
        return aggregated, evidence

    # -- raster to vector: zonal statistics ---------------------------------

    def zonal_stats(
        self,
        raster_path: Path,
        polygons: Any,
        *,
        stats: tuple[str, ...] = ("min", "mean", "max", "count"),
    ) -> Any:
        """Summarise a raster inside each polygon, indexed by GEOID.

        Returns GENERIC statistic names -- "min", "mean", "max", "count" -- and
        not `Col.ELEV_MEAN_M`. This is a raster utility; it does not know it is
        looking at elevation, and the caller that does know maps the names onto
        `Col` where the semantics are known. `RASTER_STAT_COLUMNS` is that map
        for elevation, and session 8 writes the matching one for inundation.

        The denominators are in `ZonalEvidence`.
        """
        frame, _ = self.zonal_stats_detailed(raster_path, polygons, stats=stats)
        return frame

    def zonal_stats_detailed(
        self,
        raster_path: Path,
        polygons: Any,
        *,
        stats: tuple[str, ...] = ("min", "mean", "max", "count"),
        dataset: str = "",
    ) -> tuple[pd.DataFrame, ZonalEvidence]:
        """`zonal_stats`, keeping the denominator.

        **The raster is not warped, and a CRS mismatch raises.** Every other
        spatial operation in this module routes through `to_working_crs`, but
        reprojecting a raster is not reprojecting a frame: it resamples, so it
        would answer a question about elevation by inventing elevations between
        the ones that were measured. `acquire.fetch_arcgis_raster` takes
        `out_sr` and the manifest records what it asked for, so a raster in the
        wrong CRS is a retrieval that needs fixing rather than a frame this
        module can quietly repair. Refusing it by name is also what makes the
        `wrong_crs` fault in `contracts.FaultKind` observable instead of silent.

        **A cell belongs to the polygon its centre falls in.** `ALL_TOUCHED` is
        False, which is the rule the independent check verified; the alternative
        gives every boundary cell to both neighbours at once and totals more
        cells than the raster holds.

        **Three ways to have no value, counted apart.** A cell can be outside the
        polygon, inside it but nodata, or inside it and not a finite number.
        Only the first is not a fact about the data. They are counted separately
        and must add back to the cells examined, because folding a raster hole
        into "outside the polygon" shrinks a denominator invisibly.

        A polygon that misses the raster entirely is recorded and returns no
        value rather than ending the run: the study extent is derived from the
        tract layer, and a unit sitting off the edge of a service's coverage is
        a retrieval fact worth reporting.
        """
        if not stats:
            raise ValueError("zonal_stats was asked for no statistic at all")
        unsupported = [name for name in stats if name not in ZONAL_STATISTICS]
        if unsupported:
            raise ValueError(
                f"zonal_stats does not compute {unsupported}; it computes "
                f"{list(ZONAL_STATISTICS)}"
            )
        if not isinstance(polygons, gpd.GeoDataFrame):
            raise TypeError(
                f"zonal_stats needs a GeoDataFrame of polygons, got "
                f"{type(polygons).__name__}"
            )
        label = dataset or "zonal polygons"
        if Col.GEOID not in polygons.columns:
            raise KeyError(
                f"{label}: no {Col.GEOID} column to index the result by; the frame carries "
                f"{list(polygons.columns)[:10]}"
            )
        path = Path(raster_path)
        if not path.exists():
            raise FileNotFoundError(
                f"zonal_stats was pointed at {path}, which does not exist. A raster is "
                "reached with Registry.path_of, never by building a path here"
            )

        frame = self.to_working_crs(polygons)
        geoids = _geoid_strings(frame[Col.GEOID], label)
        repeated = int(geoids.duplicated().sum())
        if repeated:
            raise ValueError(
                f"{label}: {repeated} of {len(frame)} rows repeat a {Col.GEOID}, so the "
                "result could not be indexed by it"
            )

        evidence = ZonalEvidence(
            dataset=dataset,
            raster=path.name,
            polygons=len(frame),
            threshold=MIN_RASTER_CELLS,
            stats=tuple(stats),
            all_touched=ALL_TOUCHED,
        )
        counts: list[int] = []
        gathered: dict[str, list[float | None]] = {
            name: [] for name in stats if name != "count"
        }
        with rasterio.open(path) as handle:
            if handle.crs is None:
                raise ValueError(
                    f"{path.name} carries no CRS on disk, so which ground its cells cover "
                    "is not knowable. Record the CRS at retrieval time"
                )
            raster_crs = CRS.from_user_input(handle.crs)
            if raster_crs != CRS.from_user_input(self.working_crs):
                raise ValueError(
                    f"{path.name} is stored in {raster_crs.to_string()} but this study area "
                    f"works in {self.working_crs}. Warping it here would resample the very "
                    "values being measured; retrieve it in the working CRS instead -- "
                    "acquire.fetch_arcgis_raster takes out_sr for exactly that"
                )
            evidence.raster_crs = raster_crs.to_string()
            evidence.cell_size_m = float(abs(handle.transform.a))
            evidence.cell_area_m2 = float(abs(handle.transform.a * handle.transform.e))
            for geometry in frame.geometry:
                values = self._cells_under(handle, geometry, evidence)
                counts.append(int(values.size))
                for name in gathered:
                    gathered[name].append(self._statistic(name, values))

        if counts:
            evidence.min_cells = int(min(counts))
            evidence.max_cells = int(max(counts))
            evidence.median_cells = float(np.median(counts))
            evidence.empty_polygons = sum(1 for count in counts if count == 0)
            evidence.below_threshold = sum(
                1 for count in counts if count < MIN_RASTER_CELLS
            )

        built: dict[str, Any] = {}
        for name in stats:
            if name == "count":
                built[name] = pd.array(counts, dtype="Int64")
            else:
                built[name] = pd.array(gathered[name], dtype="Float64")
        return pd.DataFrame(built, index=pd.Index(geoids.to_numpy(), name=Col.GEOID)), evidence

    def _cells_under(self, handle: Any, geometry: Any, evidence: ZonalEvidence) -> np.ndarray:
        """Every usable value whose cell centre falls inside one polygon.

        `rasterio.mask` does the windowing, which is the part that is easy to get
        wrong by hand -- a window rounded inward drops the boundary cells and
        quietly returns a different mean, which is a mistake this session made
        once while building the independent check. The shape mask is then
        recomputed on the window it returned, because the masked array folds
        "outside the polygon" and "nodata" into one mask and this module counts
        them apart.
        """
        try:
            window, window_transform = rasterio_mask.mask(
                handle,
                [geometry],
                crop=True,
                filled=False,
                all_touched=ALL_TOUCHED,
                indexes=[1],
            )
        except ValueError as exc:
            if RASTER_NO_OVERLAP not in str(exc):
                raise
            evidence.outside_raster += 1
            return np.empty(0, dtype="float64")

        band = window[0]
        inside = ~rasterio_features.geometry_mask(
            [geometry],
            band.shape,
            window_transform,
            all_touched=ALL_TOUCHED,
            invert=False,
        )
        nodata = np.ma.getmaskarray(band)
        data = np.asarray(band.data, dtype="float64")
        finite = np.isfinite(data)
        evidence.cells += int(inside.sum())
        evidence.nodata_cells += int((inside & nodata).sum())
        evidence.non_finite_cells += int((inside & ~nodata & ~finite).sum())
        return data[inside & ~nodata & finite]

    def _statistic(self, name: str, values: np.ndarray) -> float | None:
        """One reduction over the cells of one polygon, or None if it had none.

        None rather than zero, and nullable rather than a nan inside a float
        column: a polygon the raster does not cover has no minimum elevation,
        and a zero there would read downstream as sea level.
        """
        if values.size == 0:
            return None
        if name == "min":
            return float(values.min())
        if name == "mean":
            return float(values.mean())
        if name == "max":
            return float(values.max())
        raise ValueError(f"no reduction named {name!r}")

    # -- the whole snapshot --------------------------------------------------

    def acs_value_columns(self, provenance: Any, frame: pd.DataFrame) -> list[str]:
        """Which columns of an ACS table hold numbers that can carry a sentinel.

        Read from `Provenance.request_params`, which `acquire.fetch_acs` filled
        with the estimate and margin ids it actually requested. No variable id is
        written here, and no spec string is split here either: the resolved specs
        are read back through `acquire.acs_variable_ids`, and every id they name
        must appear in the recorded estimate list or this raises. That check is
        what keeps the two modules honest about the same snapshot rather than
        each trusting its own reading of it.
        """
        params = dict(provenance.request_params)
        estimates = [item for item in params.get(ACS_ESTIMATES_KEY, "").split(",") if item]
        margins = [item for item in params.get(ACS_MARGINS_KEY, "").split(",") if item]
        if not estimates:
            raise ValueError(
                f"{provenance.dataset}: provenance records no {ACS_ESTIMATES_KEY!r}, so which "
                "columns hold numbers is not knowable without guessing at the column names"
            )
        if len(margins) != len(estimates):
            raise ValueError(
                f"{provenance.dataset}: {len(estimates)} estimates but {len(margins)} margins; "
                "they are recorded as a positional pairing and no longer pair"
            )

        named = {
            variable_id
            for key, spec in params.items()
            if key.startswith(ACS_RESOLVED_PREFIX)
            for variable_id in acquire.acs_variable_ids(spec)
        }
        unrecorded = sorted(named - set(estimates))
        if unrecorded:
            raise ValueError(
                f"{provenance.dataset}: {len(unrecorded)} id(s) named by a "
                f"{ACS_RESOLVED_PREFIX} spec are absent from {ACS_ESTIMATES_KEY!r}: "
                f"{unrecorded[:8]}"
            )

        wanted = estimates + margins
        absent = [column for column in wanted if column not in frame.columns]
        if absent:
            raise ValueError(
                f"{provenance.dataset}: provenance names {len(absent)} requested column(s) the "
                f"stored table does not carry: {absent[:8]}"
            )
        return wanted

    def derive_acs_columns(
        self, frame: pd.DataFrame, provenance: Any
    ) -> tuple[pd.DataFrame, dict[str, str], dict[str, int]]:
        """Build the canonical `Col` columns from the resolved ACS specs.

        The ACS publishes most of these concepts only as leaves, so a canonical
        column is a sum of estimates over a table total rather than a single
        variable. The arithmetic belongs here rather than in `acquire`, which
        returns what the service sent, and rather than in the analysis modules,
        which would each have to re-derive it.

        Run after scrubbing, never before: a sentinel is far larger in magnitude
        than any real count, and summing one in would not look wrong, it would
        look like a very large tract. Missing values propagate through the sum
        rather than counting as zero, so a suppressed leaf suppresses the
        indicator instead of quietly shrinking it.

        The third return value counts the nulls this step creates by dividing --
        a unit whose universe is zero has no share, and a real county carries
        those. This module counts every null it makes by scrubbing, and a null it
        makes by dividing is the same fact about the same tract; leaving it
        uncounted would be the exact asymmetry this module exists to remove.
        Session 8 percentile-ranks these columns and would otherwise drop a
        census unit without anything saying which or why.

        A share stays floating point even where every value happens to be whole.
        Narrowing on the values would let the same contract column arrive as
        `Int64` on one county and `Float64` on the next, which is a dtype that
        depends on the study area.
        """
        params = dict(provenance.request_params)
        estimates = [item for item in params.get(ACS_ESTIMATES_KEY, "").split(",") if item]
        margins = [item for item in params.get(ACS_MARGINS_KEY, "").split(",") if item]
        margin_of = dict(zip(estimates, margins))

        out = frame.copy()
        derived: dict[str, str] = {}
        undefined: dict[str, int] = {}
        for key, spec in sorted(params.items()):
            if not key.startswith(ACS_RESOLVED_PREFIX):
                continue
            name = key[len(ACS_RESOLVED_PREFIX) :]
            numerators = list(acquire.acs_numerator_ids(spec))
            denominator = acquire.acs_denominator_id(spec)
            total = out[numerators].sum(axis=1, min_count=len(numerators))
            already_null = int(total.isna().sum())

            if denominator is None:
                out[name] = _as_narrow_numeric(pd.to_numeric(total, errors="coerce"))
                derived[name] = f"sum of {len(numerators)} estimate(s) = {spec}"
            else:
                base = out[denominator]
                share = total / base.where(base > 0)
                out[name] = pd.to_numeric(share * 100.0, errors="coerce").astype("Float64")
                derived[name] = (
                    f"100 * sum of {len(numerators)} estimate(s) / {denominator}, "
                    "undefined where the denominator is zero"
                )
            made_null = int(out[name].isna().sum()) - already_null
            if made_null:
                undefined[name] = made_null

            if name == Col.POPULATION:
                paired = [margin_of[item] for item in numerators if item in margin_of]
                if len(paired) == len(numerators):
                    squares = out[paired].apply(pd.to_numeric, errors="coerce") ** 2
                    combined = np.sqrt(squares.sum(axis=1, min_count=len(paired)))
                    out[Col.POP_MOE] = _as_narrow_numeric(pd.to_numeric(combined, errors="coerce"))
                    derived[Col.POP_MOE] = f"{MOE_RULE}: {', '.join(paired)}"
        return out, derived, undefined

    def align_snapshot(self) -> AlignedSnapshot:
        """Clean every dataset in the registry and report what that took.

        The order matters. Reproject, then repair geometry, then scrub, then
        derive, then join: a sentinel summed into an indicator before scrubbing
        is invisible afterwards, and a join performed before the GEOID columns
        are normalised reports a mismatch that is really a dtype.

        `reprojected` and `temporal_span` are copied out of each dataset's
        `Provenance` and cover every manifest entry, the raster included -- the
        raster is never loaded here, and its vintage and CRS are facts about the
        retrieval rather than about anything this module does to it.

        The joined layers are then cut back to GEOID, the canonical `Col`
        columns and the geometry. `acquire.fetch_arcgis_vector` returns every
        field the service publishes and says in as many words that choosing
        columns belongs here, so this is where it happens. Two reasons it is not
        cosmetic. TIGERweb ships `CENTLAT`, `CENTLON`, `INTPTLAT` and `INTPTLON`
        as ordinary text attributes, and invariant 3 says geometry never reaches
        a model message -- a coordinate wearing a column name is still a
        coordinate, and `describe_layer` would hand one over. And a hundred-odd
        raw `B*` estimate columns sitting beside the canonical ones is an
        invitation for a later session to read an ACS id directly and route
        around `contracts.Col`.
        """
        registry = self.registry
        if not registry.names():
            registry.load_manifest()

        report = AlignmentReport()
        evidence = AlignmentEvidence(datasets=tuple(registry.names()))
        frames: dict[str, Any] = {}
        canonical: dict[str, list[str]] = {}

        for record in registry.records():
            record_prov = record.provenance
            report.temporal_span[record.name] = record_prov.vintage
            report.reprojected[record.name] = (
                f"{record_prov.declared_crs} -> {record_prov.working_crs}"
            )
            if is_degraded(record_prov):
                reason = next(
                    (note for note in record_prov.notes if note.startswith(DEGRADED_NOTE_PREFIX)),
                    "degraded, no note recorded",
                )
                detail = next(
                    (note for note in record_prov.notes if ":" in note and "Error" in note),
                    "",
                )
                evidence.degraded[record.name] = f"{reason} {detail}".strip()
                report.warnings.append(
                    f"{record.name}: retrieval degraded, so its {record_prov.feature_count} "
                    "features are an absence of data rather than an absence of hazard. "
                    f"{reason}"
                )
            if record.kind == "raster":
                continue

            frame = registry.load(record.name)
            if record.kind == "vector":
                frame = self.to_working_crs(frame)
                evidence.crs_observed[record.name] = frame.crs.to_string()
                frame, repair = self.repair_geometry_detailed(frame, dataset=record.name)
                report.geometries_repaired += repair.repaired
                report.geometries_dropped += repair.dropped
                evidence.repairs.append(repair)
                if repair.still_invalid:
                    report.warnings.append(
                        f"{record.name}: {repair.still_invalid} geometry(ies) were still "
                        "invalid after make_valid and were dropped rather than carried "
                        "into an overlay"
                    )
                if repair.collapsed:
                    report.warnings.append(
                        f"{record.name}: repairing {repair.collapsed} geometry(ies) left "
                        "them enclosing no area, so they were dropped; kept, each would "
                        "report a zonal statistic of zero for a real census unit"
                    )
                if repair.type_changed:
                    report.warnings.append(
                        f"{record.name}: repair changed the geometry type of "
                        f"{repair.type_changed} feature(s); areas are unaffected, boundary "
                        "traces are not"
                    )
            frames[record.name] = frame

        for name in (acquire.DATASET_ACS, acquire.DATASET_ACS_BLOCK_GROUPS):
            if name not in frames:
                continue
            table_prov = registry.provenance_of(name)
            columns = self.acs_value_columns(table_prov, frames[name])
            scrubbed, scrub = self.scrub_sentinels_detailed(frames[name], columns, dataset=name)
            evidence.scrubs.append(scrub)
            for column, count in scrub.removed.items():
                if count:
                    report.sentinels_removed[column] = (
                        report.sentinels_removed.get(column, 0) + count
                    )
            if scrub.non_numeric:
                report.warnings.append(
                    f"{name}: {scrub.non_numeric} of {scrub.values} values could not be read "
                    "as numbers and became null; that is a parsing problem, not a suppression"
                )
            derived_frame, derived, undefined = self.derive_acs_columns(scrubbed, table_prov)
            evidence.derived.update({f"{name}.{key}": how for key, how in derived.items()})
            canonical[name] = [column for column in derived if column in derived_frame.columns]
            for column, count in undefined.items():
                evidence.undefined[f"{name}.{column}"] = count
                report.warnings.append(
                    f"{name}: {column} is undefined for {count} of {len(derived_frame)} unit(s) "
                    "whose universe for that indicator is zero. Those units carry no value "
                    "rather than a zero, and any ranking over this column omits them"
                )
            frames[name] = derived_frame

        pairs = (
            (acquire.DATASET_TRACTS, acquire.DATASET_ACS),
            (acquire.DATASET_BLOCK_GROUPS, acquire.DATASET_ACS_BLOCK_GROUPS),
        )
        for geometry_name, attribute_name in pairs:
            if geometry_name not in frames or attribute_name not in frames:
                continue
            joined, sub, join = self.join_on_geoid_detailed(
                frames[geometry_name],
                frames[attribute_name],
                left=geometry_name,
                right=attribute_name,
            )
            keep = (
                [Col.GEOID]
                + [column for column in canonical.get(attribute_name, ()) if column in joined]
                + [joined.geometry.name]
            )
            evidence.columns_dropped[f"{geometry_name}{JOINED_SUFFIX}"] = len(joined.columns) - len(keep)
            frames[f"{geometry_name}{JOINED_SUFFIX}"] = joined[keep]
            report.unmatched_left += sub.unmatched_left
            report.unmatched_right += sub.unmatched_right
            report.warnings.extend(sub.warnings)
            evidence.joins.append(join)

        tracts_key = f"{acquire.DATASET_TRACTS}{JOINED_SUFFIX}"
        groups_key = f"{acquire.DATASET_BLOCK_GROUPS}{JOINED_SUFFIX}"
        apportionable = (
            tracts_key in frames
            and groups_key in frames
            and len(frames[tracts_key]) > 0
            and len(frames[groups_key]) > 0
        )
        if apportionable:
            _, apportion = self.apportion_detailed(
                frames[groups_key],
                frames[tracts_key],
                [Col.POPULATION],
                method="sum",
                fine_name=groups_key,
                coarse_name=tracts_key,
            )
            evidence.apportionments.append(apportion)
            for column in apportion.columns:
                report.apportioned[column] = apportion.method_note
            report.apportionment_error.update(apportion.error)
            for column in apportion.not_published:
                report.warnings.append(
                    f"{column} was rolled up from {groups_key} but {tracts_key} publishes "
                    "no value for it, so there is an aggregate here and no error beside it"
                )
            if apportion.orphan_children:
                report.warnings.append(
                    f"{groups_key}: {apportion.orphan_children} of {apportion.fine_rows} "
                    f"unit(s) carry a {Col.GEOID} whose parent is absent from {tracts_key}, "
                    "so their values are in no aggregate"
                )
            if apportion.childless_parents:
                report.warnings.append(
                    f"{tracts_key}: {apportion.childless_parents} of "
                    f"{apportion.coarse_rows} unit(s) have no finer unit nested inside "
                    "them and were not compared"
                )
            for column, count in apportion.incomplete.items():
                if count:
                    report.warnings.append(
                        f"{column}: {count} of {apportion.parents} aggregate(s) carry no "
                        "value because a finer unit inside them carries none; a partial "
                        "sum would read as a smaller unit rather than as a suppression"
                    )

        else:
            report.warnings.append(
                "apportioned and apportionment_error are empty because no pair of "
                "granularities was available to roll up, not because a rollup ran and "
                "found nothing to correct. A join that matched nothing returns an empty "
                "layer rather than raising, and an empty layer cannot be apportioned"
            )

        rasters = [
            record
            for record in registry.records()
            if record.kind == "raster" and not is_degraded(record.provenance)
        ]
        for record in rasters:
            for key in (tracts_key, groups_key):
                if key not in frames:
                    continue
                measured, zonal = self.zonal_stats_detailed(
                    record.path,
                    frames[key],
                    stats=tuple(RASTER_STAT_COLUMNS),
                    dataset=key,
                )
                evidence.zonals.append(zonal)
                report.units_below_cell_threshold += zonal.below_threshold
                if record.name == acquire.DATASET_ELEVATION:
                    frames[key] = frames[key].join(
                        measured.rename(columns=RASTER_STAT_COLUMNS), on=Col.GEOID
                    )
                if zonal.below_threshold:
                    report.warnings.append(
                        f"{key}: {zonal.below_threshold} of {zonal.polygons} unit(s) cover "
                        f"fewer than {zonal.threshold} cells of {zonal.raster}; a zonal "
                        "statistic over that few cells is not trustworthy"
                    )
                if zonal.empty_polygons:
                    report.warnings.append(
                        f"{key}: {zonal.empty_polygons} of {zonal.polygons} unit(s) contain "
                        f"no cell centre of {zonal.raster} at all and carry no value rather "
                        "than a zero"
                    )
                if zonal.outside_raster:
                    report.warnings.append(
                        f"{key}: {zonal.outside_raster} of {zonal.polygons} unit(s) fall "
                        f"outside the extent of {zonal.raster}; that is a gap in the "
                        "retrieved coverage, not flat ground"
                    )
                if zonal.nodata_cells or zonal.non_finite_cells:
                    report.warnings.append(
                        f"{key}: {zonal.nodata_cells} nodata and "
                        f"{zonal.non_finite_cells} non-finite cell(s) of {zonal.cells} "
                        f"inside these units were excluded from every statistic"
                    )
        if not evidence.zonals:
            report.warnings.append(
                f"units_below_cell_threshold is {report.units_below_cell_threshold} because "
                "no raster was measured, not because every unit cleared the threshold; the "
                "registry holds no usable raster for this study area"
            )

        return AlignedSnapshot(frames=frames, report=report, evidence=evidence)


# ---------------------------------------------------------------------------
# reporting -- what was examined, beside what was changed
# ---------------------------------------------------------------------------


def _line(label: str, value: Any, evidence: str) -> str:
    return f"  {label:<27} {str(value):<9} {evidence}"


def format_report(
    report: AlignmentReport, evidence: AlignmentEvidence, study_area: config.StudyArea
) -> str:
    """Render the report so that every zero in it carries its denominator.

    The acceptance gate for this module says every non-zero number should be one
    you can explain. On a county with clean boundaries and no suppressed
    estimates that inverts: almost every number is zero, and the burden is
    proving each zero is real rather than a field nobody filled. So each line
    shows the count, then what was examined to arrive at it.
    """
    out: list[str] = [f"AlignmentReport -- {study_area.name}", ""]

    out.append(
        _line(
            "reprojected",
            len(report.reprojected),
            "dataset(s), each read from its Provenance declared_crs -> working_crs",
        )
    )
    for name, movement in sorted(report.reprojected.items()):
        seen = evidence.crs_observed.get(name)
        confirmation = f"observed on load: {seen}" if seen else "not a vector layer"
        out.append(f"      {name:<21} {movement:<26} {confirmation}")

    repaired_note = (
        f"{evidence.geometries_invalid} of {evidence.geometries_examined} geometries were "
        f"invalid across {len(evidence.repairs)} layer(s)"
    )
    out.append(_line("geometries_repaired", report.geometries_repaired, repaired_note))
    dropped_note = (
        f"of {evidence.geometries_examined} examined: "
        f"{sum(item.missing for item in evidence.repairs)} missing, "
        f"{sum(item.empty for item in evidence.repairs)} empty, "
        f"{sum(item.still_invalid for item in evidence.repairs)} unrepairable, "
        f"{sum(item.collapsed for item in evidence.repairs)} left enclosing no area"
    )
    out.append(_line("geometries_dropped", report.geometries_dropped, dropped_note))
    for item in evidence.repairs:
        out.append(
            f"      {item.dataset:<21} examined {item.examined:<6} invalid {item.invalid_before:<4} "
            f"repaired {item.repaired:<4} dropped {item.dropped}"
        )

    scrub_note = (
        f"{sum(item.negative for item in evidence.scrubs)} negative value(s) among "
        f"{evidence.values_examined} present of {evidence.cells_examined} cells over "
        f"{evidence.scrub_columns} column(s); {evidence.non_numeric} unparseable"
    )
    out.append(_line("sentinels_removed", dict(report.sentinels_removed), scrub_note))
    for item in evidence.scrubs:
        out.append(
            f"      {item.dataset:<21} {len(item.columns)} column(s) x {item.cells // max(len(item.columns), 1)} "
            f"row(s) = {item.cells} cells, {item.values} present, {item.negative} negative"
        )
    if evidence.scrubs and not any(item.negative for item in evidence.scrubs):
        out.append(
            f"      this county carries no negative Census value in any scrubbed column; "
            f"the family checked against is {len(JAM_VALUES)} generated jam codes, and the "
            "removal test is the sign, not the list"
        )
    for label, count in sorted(evidence.codes_seen().items()):
        out.append(f"      {label:<21} {count}")

    out.append(
        _line(
            "unmatched_left",
            len(report.unmatched_left),
            f"geometry GEOIDs with no attribute row, of {evidence.geoids_compared} compared "
            f"across {len(evidence.joins)} join(s)",
        )
    )
    out.append(
        _line(
            "unmatched_right",
            len(report.unmatched_right),
            f"attribute GEOIDs with no geometry, {evidence.geoids_matched} matched",
        )
    )
    for item in evidence.joins:
        out.append(
            f"      {item.left} + {item.right}: {item.left_rows} geometries "
            f"(width {sorted(item.left_widths)}) and {item.right_rows} attribute rows "
            f"(width {sorted(item.right_widths)}) -> {item.matched} joined"
        )

    out.append("")
    out.append(
        _line(
            "apportioned",
            len(report.apportioned),
            f"column(s) rolled up from a finer geography, over "
            f"{evidence.units_apportioned} finer unit(s)",
        )
    )
    for item in evidence.apportionments:
        out.append(f"      {item.method_note}")
        if item.not_published:
            out.append(
                f"      aggregated but not published at the coarse level, so not compared: "
                f"{list(item.not_published)}"
            )
        out.append(
            f"      {item.orphan_children} child unit(s) with no parent, "
            f"{item.childless_parents} parent(s) with no child, "
            f"{sum(item.incomplete.values())} aggregate(s) suppressed by a missing child"
        )

    if report.apportionment_error:
        error_note = (
            "worst unit, not the county total -- a maximum cannot cancel the way a sum can"
        )
    elif not evidence.apportionments:
        error_note = "NOTHING WAS APPORTIONED -- no pair of granularities to roll up"
    else:
        error_note = "no column carried a published coarse value to compare against"

    out.append(_line("apportionment_error", dict(report.apportionment_error), error_note))
    for item in evidence.apportionments:
        for column in item.compared:
            compared = item.units_compared.get(column, 0)
            undefined = item.units_undefined.get(column, 0)
            out.append(
                f"      {column:<21} max |aggregated - published| = "
                f"{item.max_abs_difference.get(column, 0.0):,.6g} over {compared} unit(s); "
                f"{item.total_fine.get(column, 0.0):,.0f} aggregated against "
                f"{item.total_coarse.get(column, 0.0):,.0f} published"
            )
            out.append(
                f"      {'':21} the % is taken over the {compared - undefined} unit(s) "
                f"publishing a non-zero value; {undefined} publish zero, where a relative "
                "error has no meaning and the absolute difference above is the real number"
            )
            if column == Col.POPULATION and not item.max_abs_difference.get(column, 1.0):
                out.append(f"      {'':21} {CONTROLLED_TO_TRACT} not")

    below_note = (
        f"of {evidence.polygons_measured} polygon(s) over {len(evidence.zonals)} layer(s), "
        f"threshold {MIN_RASTER_CELLS} cells"
        if evidence.zonals
        else "NOTHING WAS MEASURED -- the registry holds no usable raster"
    )
    out.append(
        _line("units_below_cell_threshold", report.units_below_cell_threshold, below_note)
    )
    for item in evidence.zonals:
        out.append(
            f"      {item.dataset:<21} {item.polygons} polygon(s) over {item.raster}: "
            f"smallest covers {item.min_cells} cell(s), median {item.median_cells:,.0f}, "
            f"largest {item.max_cells:,}"
        )
        out.append(
            f"      {'':21} {item.cells:,} cell centre(s) inside them = "
            f"{item.valid_cells:,} usable + {item.nodata_cells} nodata + "
            f"{item.non_finite_cells} non-finite; {item.empty_polygons} polygon(s) held "
            f"none, {item.outside_raster} fell outside the raster"
        )
        out.append(
            f"      {'':21} {item.cell_size_m:.4f} m cells of {item.cell_area_m2:,.1f} m2 "
            f"in {item.raster_crs}, all_touched={item.all_touched} -- a cell belongs to "
            "the polygon its centre falls in, which is the rule the check verified"
        )

    out.append("")
    out.append(
        _line("temporal_span", len(report.temporal_span), "dataset vintage(s), read from Provenance")
    )
    for name, vintage in sorted(report.temporal_span.items()):
        out.append(f"      {name:<21} {vintage}")

    if evidence.degraded:
        out.append("")
        out.append("  degraded retrievals -- zero features is an absence of data, not of hazard:")
        for name, why in sorted(evidence.degraded.items()):
            out.append(f"      {name}: {why}")

    if evidence.derived:
        out.append("")
        out.append(f"  canonical columns derived from the resolved specs ({len(evidence.derived)}):")
        for name, how in sorted(evidence.derived.items()):
            out.append(f"      {name:<34} {how}")

    out.append("")
    out.append(
        _line(
            "undefined shares",
            sum(evidence.undefined.values()),
            "value(s) a zero universe left with no share -- counted here because a null "
            "this module makes by dividing is the same fact as one it makes by scrubbing",
        )
    )
    for name, count in sorted(evidence.undefined.items()):
        out.append(f"      {name:<34} {count}")

    out.append(
        _line(
            "columns dropped from joins",
            sum(evidence.columns_dropped.values()),
            "raw and coordinate-bearing column(s) cut so no tool can serialise a "
            "coordinate out of an aligned layer",
        )
    )
    for name, count in sorted(evidence.columns_dropped.items()):
        out.append(f"      {name:<34} {count} dropped")

    out.append("")
    out.append(f"  warnings ({len(report.warnings)}):")
    for message in report.warnings:
        out.append(f"      - {message}")
    return "\n".join(out)


def main(area: config.StudyArea | None = None) -> int:
    registry = Registry(area or config.STUDY_AREA)
    registry.load_manifest()
    aligner = Alignment(registry)
    snapshot = aligner.align_snapshot()

    print(format_report(snapshot.report, snapshot.evidence, aligner.study_area))
    print()
    for name in sorted(snapshot.frames):
        frame = snapshot.frames[name]
        kind = "geo" if isinstance(frame, gpd.GeoDataFrame) else "tab"
        crs = frame.crs.to_string() if isinstance(frame, gpd.GeoDataFrame) else "n/a"
        print(f"  {kind} {name:<24} {len(frame):>5} rows x {len(frame.columns):>3} cols   {crs}")
    return 0


# ---------------------------------------------------------------------------
# self check
#
# The house convention is a `_self_check()` that exits non-zero. This module
# touches no network, so the "exercise the real boundary" rule in CLAUDE.md has
# no boundary to exercise here. Its equivalent, and the rule this file follows,
# is that a spatial result is verified against an independently computed value
# -- a geodesic area from pyproj, a count read back out of the manifest, a
# population totalled at the other granularity -- never against a previous run
# of this same code.
#
# Every check that asserts a zero also asserts its denominator is non-zero, and
# is paired with the same operation run on a deliberately broken frame. A check
# that cannot fail is worth less than no check, because it reads as coverage.
# ---------------------------------------------------------------------------


def _fixture_point(working_crs: str) -> tuple[float, float]:
    """A lon/lat to build test fixtures on, derived from the CRS itself.

    Read out of the working CRS's published area of use rather than written
    down, so this module contains no coordinate literal at all and the fixtures
    follow the working CRS if it ever changes. A test fixture pinned to the
    study area is the same bug as a study extent pinned to it, and `acquire`
    scans this file for exactly that.
    """
    area = CRS.from_user_input(working_crs).area_of_use
    if area is None:
        raise ValueError(f"{working_crs} publishes no area of use to place a fixture in")
    return ((area.west + area.east) / 2.0, (area.south + area.north) / 2.0)


def _square(lon: float, lat: float, side: float = 0.1) -> Polygon:
    return Polygon(
        [(lon, lat), (lon + side, lat), (lon + side, lat + side), (lon, lat + side)]
    )


def _bowtie(lon: float, lat: float, side: float = 0.1) -> Polygon:
    """A self-intersecting polygon on the same four corners as `_square`.

    Invalid, and `make_valid` resolves it into the two triangles that a diagonal
    cuts the square into -- so its repaired area is exactly half the square's,
    which is a value computed from the geometry rather than read off a prior run.
    """
    return Polygon(
        [(lon, lat), (lon + side, lat + side), (lon + side, lat), (lon, lat + side)]
    )


def _sliver(lon: float, lat: float, side: float = 0.1) -> Polygon:
    """A polygon whose ring doubles straight back, enclosing no area.

    `make_valid` resolves it into a line: valid, not empty, and useless to every
    area and zonal operation downstream. It is the case that separates repairing
    a geometry from merely making `is_valid` return True.
    """
    return Polygon([(lon, lat), (lon + side, lat), (lon, lat)])


def _nan_polygon(lon: float, lat: float, side: float = 0.1) -> Any:
    """A polygon carrying a coordinate that is not a number.

    GEOS raises on this rather than repairing it, so it is what proves the
    per-geometry fallback in `_make_valid`: one malformed feature out of hundreds
    must not end a run.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return shapely_wkt.loads(
            f"POLYGON (({lon} {lat}, {lon + side} nan, {lon + side} {lat + side}, {lon} {lat}))"
        )


def _frame(geometries: list[Any], geoids: list[str], crs: str) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({Col.GEOID: geoids}, geometry=geometries, crs=crs)


def _geodesic_area(aligner: Alignment, geometry: Any) -> float:
    area, _ = aligner._geod.geometry_area_perimeter(geometry)
    return abs(area)


def _crs_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    lon, lat = _fixture_point(aligner.working_crs)
    geographic = _frame([_square(lon, lat)], ["A"], config.STORAGE_CRS)
    projected = aligner.to_working_crs(geographic)

    truth = _geodesic_area(aligner, geographic.geometry.iloc[0])
    measured = float(projected.geometry.iloc[0].area)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        degrees = float(geographic.geometry.iloc[0].area)
    error = abs(measured - truth) / truth

    print(
        f"crs: a fixture square placed at {lon:.2f}, {lat:.2f} -- the centre of the "
        f"{aligner.working_crs} area of use, not of any county"
    )
    print(f"  geodesic area (pyproj, independent) {truth:,.1f} m2")
    print(f"  area in {aligner.working_crs:<22} {measured:,.1f} m2   error {error:.4%}")
    print(f"  area in {config.STORAGE_CRS:<22} {degrees:,.6f} (square degrees, not metres)")

    checks: list[tuple[str, bool]] = [
        ("to_working_crs reprojects a frame that never came through the registry",
         projected.crs.to_string() == aligner.working_crs),
        ("the projected area matches an independently computed geodesic area within 1%",
         error < 0.01),
        ("the unprojected area is wrong by orders of magnitude, so the helper is load-bearing",
         measured / degrees > 1e6),
        ("to_working_crs is idempotent and returns the same object when already correct",
         aligner.to_working_crs(projected) is projected),
        ("a GeoSeries goes through too",
         aligner.to_working_crs(geographic.geometry).crs.to_string() == aligner.working_crs),
    ]

    crsless = geographic.copy()
    crsless.crs = None
    try:
        aligner.to_working_crs(crsless)
        checks.append(("a frame with no CRS raises rather than being assumed", False))
    except ValueError:
        checks.append(("a frame with no CRS raises rather than being assumed", True))

    try:
        aligner.to_working_crs(Path("elevation.tif"))
        checks.append(("a raster path is refused and pointed at zonal_stats", False))
    except TypeError:
        checks.append(("a raster path is refused and pointed at zonal_stats", True))

    try:
        aligner.to_working_crs(pd.DataFrame({Col.GEOID: ["A"]}))
        checks.append(("a plain table is refused", False))
    except TypeError:
        checks.append(("a plain table is refused", True))
    return checks


def _repair_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    lon, lat = _fixture_point(aligner.working_crs)
    clean = _frame([_square(lon, lat), _square(lon + 1, lat)], ["A", "B"], config.STORAGE_CRS)
    _, clean_evidence = aligner.repair_geometry_detailed(clean, dataset="clean fixture")

    broken = _frame([_bowtie(lon, lat)], ["A"], config.STORAGE_CRS)
    repaired, broken_evidence = aligner.repair_geometry_detailed(broken, dataset="bowtie fixture")
    square_area = _geodesic_area(aligner, _square(lon, lat))
    repaired_area = float(repaired.geometry.iloc[0].area) if len(repaired) else 0.0
    halves = abs(repaired_area - square_area / 2.0) / (square_area / 2.0) if square_area else 1.0

    holed = _frame(
        [_square(lon, lat), None, Polygon()], ["A", "B", "C"], config.STORAGE_CRS
    )
    kept, holed_evidence = aligner.repair_geometry_detailed(holed, dataset="dropped fixture")

    empty = gpd.GeoDataFrame(
        {Col.GEOID: pd.Series([], dtype="string")},
        geometry=gpd.GeoSeries([], crs=config.STORAGE_CRS),
        crs=config.STORAGE_CRS,
    )
    _, empty_evidence = aligner.repair_geometry_detailed(empty, dataset="empty fixture")

    slivered = _frame(
        [_square(lon, lat), _sliver(lon + 1, lat)], ["A", "B"], config.STORAGE_CRS
    )
    survivors, sliver_evidence = aligner.repair_geometry_detailed(
        slivered, dataset="sliver fixture"
    )
    areal = (
        bool(survivors.geometry.geom_type.isin(AREAL_TYPES).all()) if len(survivors) else False
    )

    hostile = _frame(
        [_square(lon, lat), _nan_polygon(lon + 1, lat)], ["A", "B"], config.STORAGE_CRS
    )
    endured, hostile_evidence = aligner.repair_geometry_detailed(
        hostile, dataset="nan fixture"
    )

    print(
        f"repair: clean fixture {clean_evidence.examined} examined / "
        f"{clean_evidence.repaired} repaired; bowtie fixture "
        f"{broken_evidence.invalid_before} invalid / {broken_evidence.repaired} repaired, "
        f"type changed {broken_evidence.type_changed}"
    )
    print(
        f"  repaired bowtie {repaired_area:,.1f} m2 against half the square's geodesic "
        f"{square_area / 2.0:,.1f} m2   error {halves:.4%}"
    )

    return [
        ("a clean layer reports 0 repaired out of a non-zero examined count",
         clean_evidence.repaired == 0 and clean_evidence.examined == 2),
        ("the same call on an invalid geometry does not report 0, so the zero above is real",
         broken_evidence.invalid_before == 1 and broken_evidence.repaired == 1),
        ("the repaired geometry is valid",
         len(repaired) == 1 and bool(repaired.geometry.is_valid.all())),
        ("the repaired area matches the independently computed half-square",
         halves < 0.01),
        ("repair records that make_valid changed the geometry type",
         broken_evidence.type_changed == 1),
        ("a missing geometry is dropped and counted",
         holed_evidence.missing == 1),
        ("an empty geometry is dropped and counted",
         holed_evidence.empty == 1),
        ("dropped rows leave the frame, and the survivor stays",
         len(kept) == 1 and holed_evidence.dropped == 2),
        ("a zero-row layer passes through without raising, which is the degraded case",
         empty_evidence.examined == 0 and empty_evidence.dropped == 0),
        ("repair routes its input through the metric CRS helper",
         aligner.repair_geometry_detailed(clean)[0].crs.to_string() == aligner.working_crs),
        ("the protocol wrapper returns the repair count",
         aligner.repair_geometry(broken)[1] == 1),
        ("a repair that leaves a polygon enclosing no area is a drop, not a repair",
         sliver_evidence.collapsed == 1 and sliver_evidence.repaired == 0),
        ("nothing that stopped enclosing area survives into the layer",
         len(survivors) == 1 and areal),
        ("a geometry GEOS refuses to repair is dropped, and does not end the run",
         hostile_evidence.still_invalid == 1 and len(endured) == 1),
    ]


def _scrub_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    documented = (-666666666, -999999999, -888888888, -555555555, -333333333, -222222222)
    covered = [value for value in documented if value in JAM_VALUES]

    jam = sorted(JAM_VALUES)[0]
    frame = pd.DataFrame(
        {
            Col.GEOID: ["A", "B", "C", "D"],
            "suppressed": ["10", str(-666666666), "30", "0"],
            "unlisted": ["1", "2", "-42", "3"],
            "clean": ["5", "6", "7", "8"],
            "text": ["1", "not a number", "3", "4"],
            "sparse": ["1", None, "3", "4"],
        }
    )
    value_cols = ["suppressed", "unlisted", "clean", "text", "sparse"]
    scrubbed, evidence = aligner.scrub_sentinels_detailed(frame, value_cols, dataset="fixture")
    wrapped, removed = aligner.scrub_sentinels(frame, value_cols)

    clean_only = pd.DataFrame({"clean": ["5", "6"]})
    _, clean_removed = aligner.scrub_sentinels(clean_only, ["clean"])
    _, clean_evidence = aligner.scrub_sentinels_detailed(clean_only, ["clean"])

    labels = evidence.codes
    print(
        f"scrub: {evidence.values} present values in {evidence.cells} cells over "
        f"{len(evidence.columns)} columns; {evidence.negative} negative, "
        f"{evidence.non_numeric} unparseable"
    )
    for label, count in sorted(labels.items()):
        print(f"  removed {label} x{count}")

    try:
        aligner.scrub_sentinels(frame, ["not_a_column"])
        missing_raises = False
    except KeyError:
        missing_raises = True

    return [
        ("every documented Census code falls inside the generated family, so labels are right",
         len(covered) == len(documented)),
        ("the family is generated, not pasted -- it is closed under its own rule",
         jam == -9999999999 and len(JAM_VALUES) == len(SENTINEL_DIGITS) * len(SENTINEL_WIDTHS)),
        ("a documented jam code is removed",
         evidence.removed["suppressed"] == 1 and pd.isna(scrubbed["suppressed"].iloc[1])),
        ("a negative that is NOT a known jam code is removed too, so the list is not load-bearing",
         evidence.removed["unlisted"] == 1),
        ("the two are labelled apart",
         any("census jam code" in label for label in labels)
         and any("not a known jam code" in label for label in labels)),
        ("a legitimate zero survives scrubbing",
         int(scrubbed["suppressed"].iloc[3]) == 0),
        ("a column with nothing wrong reports 0 removed out of a non-zero examined count",
         evidence.removed["clean"] == 0 and evidence.examined["clean"] == 4),
        ("an unparseable value is counted separately and does not inflate the sentinel count",
         evidence.non_numeric == 1 and evidence.removed["text"] == 0),
        ("a null is not counted as a value examined",
         evidence.examined["sparse"] == 3 and evidence.cells == 20),
        ("the protocol wrapper omits columns nothing was removed from",
         set(removed) == {"suppressed", "unlisted"} and wrapped is not frame),
        ("a clean frame returns an empty dict, so `if removed:` cannot read as a removal",
         clean_removed == {} and clean_evidence.examined["clean"] == 2),
        ("a whole-number column comes back as a nullable integer, not a float",
         str(scrubbed["clean"].dtype) == "Int64"),
        ("asking for a column the frame does not carry raises", missing_raises),
    ]


def _derive_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """Derive canonical columns on a frame built to have what this county lacks.

    Two behaviours here are invisible on the live snapshot because its values
    happen not to exercise them: a share that lands on a whole number, and a unit
    whose universe is zero. Both are ordinary in other counties, and a check that
    only runs where the data is convenient is not a check on the transfer run.
    """
    top, bottom = "num_1E", "den_0E"
    frame = pd.DataFrame(
        {
            Col.GEOID: ["01001000100", "01001000200", "01001000300"],
            bottom: ["100", "200", "0"],
            top: ["50", "200", "0"],
            "den_0M": ["1", "2", "3"],
            "num_1M": ["4", "5", "6"],
        }
    )
    record = Provenance(
        dataset="fixture",
        source_url="https://example.invalid/fixture",
        retrieved_at=prov.utc_now(),
        declared_crs="n/a",
        working_crs=aligner.working_crs,
        vintage="fixture",
        feature_count=len(frame),
        license="fixture",
        request_params={
            ACS_ESTIMATES_KEY: f"{bottom},{top}",
            ACS_MARGINS_KEY: "den_0M,num_1M",
            f"{ACS_RESOLVED_PREFIX}{Col.POPULATION}": bottom,
            f"{ACS_RESOLVED_PREFIX}{Col.PCT_POVERTY}": f"{top}/{bottom}",
        },
    )
    scrubbed, _ = aligner.scrub_sentinels_detailed(frame, [bottom, top, "den_0M", "num_1M"])
    derived_frame, derived, undefined = aligner.derive_acs_columns(scrubbed, record)
    share = derived_frame[Col.PCT_POVERTY]

    print(
        f"derive: shares {[None if pd.isna(v) else float(v) for v in share]} "
        f"dtype {share.dtype}; undefined {undefined}"
    )

    return [
        ("a share that lands on whole numbers still comes back floating point",
         str(share.dtype) == "Float64" and float(share.iloc[1]) == 100.0),
        ("a count does narrow to a nullable integer, so the two are treated differently",
         str(derived_frame[Col.POPULATION].dtype) == "Int64"),
        ("a unit whose universe is zero gets no share rather than a zero",
         bool(pd.isna(share.iloc[2]))),
        ("and that null is counted, not silently produced",
         undefined.get(Col.PCT_POVERTY) == 1),
        ("the margin of a derived count is built and named",
         Col.POP_MOE in derived_frame.columns and Col.POP_MOE in derived),
    ]


def _join_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    lon, lat = _fixture_point(aligner.working_crs)
    geometry = _frame(
        [_square(lon, lat), _square(lon + 1, lat)], ["01001000100", "01001000200"],
        config.STORAGE_CRS,
    )
    matching = pd.DataFrame({Col.GEOID: ["01001000100", "01001000200"], "value": [1, 2]})
    mismatched = pd.DataFrame({Col.GEOID: ["01001000200", "01001000300"], "value": [2, 3]})

    _, clean_report, clean_evidence = aligner.join_on_geoid_detailed(
        geometry, matching, left="fixture geometry", right="fixture attributes"
    )
    joined, report, evidence = aligner.join_on_geoid_detailed(
        geometry, mismatched, left="fixture geometry", right="fixture attributes"
    )

    finer = pd.DataFrame({Col.GEOID: ["010010001001", "010010001002"], "value": [1, 2]})
    _, granularity, _ = aligner.join_on_geoid_detailed(
        geometry, finer, left="fixture geometry", right="finer fixture"
    )

    def refusal(attrs: Any) -> str:
        try:
            aligner.join_on_geoid(geometry, attrs)
        except ValueError as exc:
            return str(exc)
        return ""

    numeric_message = refusal(
        pd.DataFrame({Col.GEOID: [1001000100, 1001000200], "value": [1, 2]})
    )
    boxed_message = refusal(
        pd.DataFrame(
            {Col.GEOID: pd.Series([1001000100, 1001000200], dtype=object), "value": [1, 2]}
        )
    )
    short_message = refusal(
        pd.DataFrame({Col.GEOID: ["0100100010", "0100100020"], "value": [1, 2]})
    )

    repeated = pd.DataFrame({Col.GEOID: ["01001000100", "01001000100"], "value": [1, 2]})
    repeated_message = ""
    try:
        aligner.join_on_geoid(geometry, repeated)
    except ValueError as exc:
        repeated_message = str(exc)
    repeated_raises = "rows repeat a" in repeated_message

    try:
        aligner.join_on_geoid(geometry, pd.DataFrame({"value": [1]}))
        keyless_raises = False
    except KeyError:
        keyless_raises = True

    print(
        f"join: matching fixture {clean_evidence.matched}/{clean_evidence.left_rows} matched; "
        f"mismatched fixture left {report.unmatched_left} right {report.unmatched_right}"
    )

    return [
        ("a matching pair reports no unmatched GEOIDs out of a non-zero comparison",
         clean_report.unmatched_left == () and clean_report.unmatched_right == ()
         and clean_evidence.left_rows == 2 and clean_evidence.matched == 2),
        ("a geometry with no attribute row lands in unmatched_left, and only there",
         report.unmatched_left == ("01001000100",)),
        ("an attribute row with no geometry lands in unmatched_right, and only there",
         report.unmatched_right == ("01001000300",)),
        ("the two sides are not transposed",
         report.unmatched_left != report.unmatched_right),
        ("the join keeps only what both sides agree on",
         len(joined) == 1 and joined[Col.GEOID].iloc[0] == "01001000200"),
        ("nothing is dropped silently: every unmatched GEOID is named in a warning",
         len(report.warnings) == 2
         and all("01001000" in message for message in report.warnings)),
        ("the joined frame is still a GeoDataFrame in the working CRS",
         isinstance(joined, gpd.GeoDataFrame) and joined.crs.to_string() == aligner.working_crs),
        ("a width mismatch is reported as a granularity problem, not as a bad join",
         any("granularity mismatch" in message for message in granularity.warnings)),
        ("a numeric-dtype GEOID is refused by name, not left to a downstream symptom",
         "not text" in numeric_message),
        ("an object column holding numbers is refused too, since the dtype test cannot see it",
         "the values are numbers" in boxed_message),
        ("a width no census geography publishes is called a dropped zero, not a granularity gap",
         "dropped leading zero" in short_message),
        ("a repeated GEOID is named and counted, not left to the merge to notice",
         repeated_raises),
        ("a frame with no GEOID column raises", keyless_raises),
    ]


def _projected_anchor(aligner: Alignment) -> tuple[float, float]:
    """A point in working-CRS metres to hang a synthetic raster on.

    Derived from the working CRS's own area of use, like every other fixture in
    this file, so the module still contains no coordinate literal and a fixture
    raster lands somewhere the CRS is actually defined.
    """
    lon, lat = _fixture_point(aligner.working_crs)
    placed = aligner.to_working_crs(gpd.GeoSeries([Point(lon, lat)], crs=config.STORAGE_CRS))
    return float(placed.iloc[0].x), float(placed.iloc[0].y)


def _write_raster(
    path: Path, values: np.ndarray, x0: float, y0: float, cell: float, crs: str | None
) -> Path:
    """Write a small GeoTIFF whose every cell value this file chose.

    The point of a fixture raster is that the right answer is arithmetic rather
    than another run of the code under test: a polygon over four known cells has
    a mean this file can state in the check itself.
    """
    transform = rasterio.transform.from_origin(x0, y0 + values.shape[0] * cell, cell, cell)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=FIXTURE_NODATA,
    ) as handle:
        handle.write(values.astype("float32"), 1)
    return path


FIXTURE_NODATA = -9999.0
FIXTURE_CELL = 100.0
FIXTURE_SIDE = 10


def _box(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _zonal_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """Verify zonal statistics twice: against arithmetic, and against the county.

    Two independent verifications, because they catch different things. The
    synthetic raster has values this file chose, so the expected minimum, mean,
    maximum and count are stated here as numbers rather than read off a run --
    that is what catches a reduction applied to the wrong cells. The county
    raster is then checked against a cell-centre point-in-polygon average
    computed here with shapely and numpy, never calling `zonal_stats` -- that is
    what catches the windowing, which is the part that is easy to get subtly
    wrong and impossible to notice from a plausible-looking mean.
    """
    import tempfile

    x0, y0 = _projected_anchor(aligner)
    cell, side = FIXTURE_CELL, FIXTURE_SIDE
    values = np.arange(side * side, dtype="float64").reshape(side, side)
    values[5][5] = FIXTURE_NODATA
    values[5][6] = np.nan

    root = Path(tempfile.mkdtemp(prefix="zonal_check_"))
    good = _write_raster(root / "fixture.tif", values, x0, y0, cell, aligner.working_crs)
    top = y0 + side * cell

    quad = _box(x0 + 1, top - 2 * cell + 1, x0 + 2 * cell - 1, top - 1)
    holed = _box(x0 + 5 * cell + 1, top - 6 * cell + 1, x0 + 9 * cell - 1, top - 5 * cell - 1)
    single = _box(x0 + 9 * cell + 1, top - 10 * cell + 1, x0 + 10 * cell - 1, top - 9 * cell - 1)
    away = _box(x0 + 500 * cell, top + 500 * cell, x0 + 501 * cell, top + 501 * cell)
    wide = _box(x0 + 1, top - 3 * cell + 1, x0 + side * cell - 1, top - 1)

    polygons = _frame(
        [quad, holed, single, away, wide],
        ["01001000100", "01001000200", "01001000300", "01001000400", "01001000500"],
        aligner.working_crs,
    )
    measured, evidence = aligner.zonal_stats_detailed(
        good, polygons, stats=("min", "mean", "max", "count"), dataset="fixture"
    )

    quad_row = measured.loc["01001000100"]
    holed_row = measured.loc["01001000200"]
    single_row = measured.loc["01001000300"]
    away_row = measured.loc["01001000400"]
    wide_row = measured.loc["01001000500"]

    print(
        f"zonal fixture: a {side}x{side} raster of known values at "
        f"{x0:,.0f}, {y0:,.0f} in {aligner.working_crs}"
    )
    print(
        f"  four cells holding 0, 1, 10, 11 -> min {quad_row['min']} mean {quad_row['mean']} "
        f"max {quad_row['max']} count {quad_row['count']}   (expected 0, 5.5, 11, 4)"
    )
    print(
        f"  four cells, one nodata and one nan -> count {holed_row['count']} "
        f"mean {holed_row['mean']}   (expected 2, 57.5)"
    )
    print(
        f"  three whole rows, holding 0 to 29 -> mean {wide_row['mean']} "
        f"count {wide_row['count']}   (expected 14.5, 30)"
    )
    print(
        f"  cells inside {evidence.cells}, of which {evidence.nodata_cells} nodata and "
        f"{evidence.non_finite_cells} non-finite; {evidence.outside_raster} polygon(s) "
        f"missed the raster entirely; {evidence.below_threshold} of {evidence.polygons} "
        f"fall under the {evidence.threshold}-cell threshold"
    )

    def refusal(call: Any, kind: Any, phrase: str) -> bool:
        """Did this call refuse for the stated reason, rather than merely raise?

        The phrase is not decoration. Removing the unsupported-statistic guard
        leaves the reduction itself raising ValueError, so a check that asked
        only for the type would pass over a module that had lost the guard.
        """
        try:
            call()
        except kind as exc:
            return phrase in str(exc)
        except Exception:
            return False
        return False

    class _HostileHandle:
        """A dataset handle whose every use fails, and not because of overlap.

        `rasterio.mask` raises ValueError for a polygon that misses the raster,
        which `_cells_under` records as a fact about the county. Nothing reachable
        through a geometry makes it raise ValueError for any OTHER reason -- a
        closed dataset gives RasterioIOError, a degenerate polygon an IndexError --
        so the discipline of matching the message cannot be exercised through the
        public interface. This stub exercises it directly: the policy under test is
        this module's, not rasterio's.
        """

        def __getattr__(self, name: str) -> Any:
            raise ValueError("a failure that is not about overlap")

    hostile_evidence = ZonalEvidence()

    def hostile_propagates() -> bool:
        try:
            aligner._cells_under(_HostileHandle(), quad, hostile_evidence)
        except ValueError as exc:
            return (
                "not about overlap" in str(exc)
                and hostile_evidence.outside_raster == 0
            )
        return False

    crossed = _write_raster(
        root / "wrong_crs.tif", values, x0, y0, cell, config.STORAGE_CRS
    )
    homeless = _write_raster(root / "no_crs.tif", values, x0, y0, cell, None)

    repeated = _frame(
        [quad, holed], ["01001000100", "01001000100"], aligner.working_crs
    )
    keyless = gpd.GeoDataFrame({"value": [1]}, geometry=[quad], crs=aligner.working_crs)

    checks: list[tuple[str, bool]] = [
        ("a statistic over four cells this file chose equals the arithmetic on those cells",
         float(quad_row["min"]) == 0.0
         and float(quad_row["mean"]) == 5.5
         and float(quad_row["max"]) == 11.0
         and int(quad_row["count"]) == 4),
        ("nodata and a nan inside a polygon are excluded from the statistics, not summed",
         int(holed_row["count"]) == 2 and float(holed_row["mean"]) == 57.5),
        ("and they are counted apart, so a raster hole does not shrink a denominator unseen",
         evidence.nodata_cells == 1 and evidence.non_finite_cells == 1),
        ("a second statistic over thirty known cells also equals the arithmetic on them",
         float(wide_row["mean"]) == 14.5
         and int(wide_row["count"]) == 30
         and float(wide_row["max"]) == 29.0),
        ("every cell centre inside a polygon is one of usable, nodata or non-finite",
         evidence.cells == 4 + 4 + 1 + 0 + 30
         and evidence.valid_cells == evidence.cells - 2),
        ("a polygon covering fewer cells than the threshold is counted, so the live zero can move",
         evidence.below_threshold == 4 and int(single_row["count"]) == 1),
        ("and the polygon that clears the threshold is not counted, so the flag discriminates",
         evidence.polygons == 5 and int(wide_row["count"]) >= MIN_RASTER_CELLS),
        ("a polygon that misses the raster is recorded and does not end the run",
         evidence.outside_raster == 1 and int(away_row["count"]) == 0),
        ("a failure that is NOT about overlap is re-raised, not filed under outside the raster",
         hostile_propagates()),
        ("a polygon with no cells carries no value rather than a zero that reads as sea level",
         pd.isna(away_row["min"]) and pd.isna(away_row["mean"])),
        ("counts come back as a nullable integer and statistics as nullable floats",
         str(measured["count"].dtype) == "Int64"
         and {str(measured[name].dtype) for name in ("min", "mean", "max")} == {"Float64"}),
        ("the result is indexed by GEOID and carries exactly the statistics asked for",
         measured.index.name == Col.GEOID
         and list(measured.columns) == ["min", "mean", "max", "count"]),
        ("asking for a subset returns that subset, in the order requested",
         list(aligner.zonal_stats(good, polygons, stats=("count", "min")).columns)
         == ["count", "min"]),
        ("a raster in a different CRS is refused rather than silently warped",
         refusal(lambda: aligner.zonal_stats(crossed, polygons), ValueError,
                 "is stored in")),
        ("a raster with no CRS at all is refused",
         refusal(lambda: aligner.zonal_stats(homeless, polygons), ValueError,
                 "carries no CRS on disk")),
        ("a statistic this module does not compute is refused by name",
         refusal(lambda: aligner.zonal_stats(good, polygons, stats=("median",)), ValueError,
                 "does not compute")),
        ("asking for no statistic at all is refused",
         refusal(lambda: aligner.zonal_stats(good, polygons, stats=()), ValueError,
                 "no statistic at all")),
        ("a raster path that does not exist is refused before anything is read",
         refusal(lambda: aligner.zonal_stats(root / "absent.tif", polygons),
                 FileNotFoundError, "does not exist")),
        ("a repeated GEOID is refused, since the result is indexed by it",
         refusal(lambda: aligner.zonal_stats(good, repeated), ValueError,
                 "rows repeat a")),
        ("a frame with no GEOID column is refused",
         refusal(lambda: aligner.zonal_stats(good, keyless), KeyError,
                 "column to index the result by")),
        ("a plain table is refused",
         refusal(lambda: aligner.zonal_stats(good, pd.DataFrame({Col.GEOID: ["A"]})),
                 TypeError, "needs a GeoDataFrame of polygons")),
    ]

    registry = aligner.registry
    elevation = [record for record in registry.records() if record.kind == "raster"]
    if not elevation:
        return checks

    snapshot_frames = aligner.align_snapshot().frames
    tracts = snapshot_frames.get(f"{acquire.DATASET_TRACTS}{JOINED_SUFFIX}")
    if tracts is None or not len(tracts):
        return checks

    smallest = tracts.loc[tracts.geometry.area.idxmin()]
    geometry = smallest.geometry
    named = str(smallest[Col.GEOID])
    truth = _independent_zonal(elevation[0].path, geometry)
    computed = aligner.zonal_stats(
        elevation[0].path,
        tracts.loc[[smallest.name]],
        stats=("min", "mean", "max", "count"),
    ).iloc[0]

    agrees = (
        int(truth["count"]) == int(computed["count"])
        and abs(float(truth["mean"]) - float(computed["mean"])) < 1e-9
        and abs(float(truth["min"]) - float(computed["min"])) < 1e-9
        and abs(float(truth["max"]) - float(computed["max"])) < 1e-9
    )
    print(
        f"zonal county: GEOID {named}, the smallest tract by area. Selected by rank at run "
        "time, so the identifier is printed from the data and never written into this file"
    )
    print(
        f"  independent centre-in-polygon (shapely + numpy, not zonal_stats): "
        f"n={int(truth['count'])} min={truth['min']:.8f} mean={truth['mean']:.8f} "
        f"max={truth['max']:.8f}"
    )
    print(
        f"  zonal_stats:                                                     "
        f"n={int(computed['count'])} min={float(computed['min']):.8f} "
        f"mean={float(computed['mean']):.8f} max={float(computed['max']):.8f}"
    )

    checks.append(
        ("a real polygon's zonal statistics match a value computed independently of this module",
         agrees)
    )
    checks.append(
        ("that independent check ran over a real number of cells, not an empty polygon",
         int(truth["count"]) > MIN_RASTER_CELLS)
    )
    return checks


def _independent_zonal(raster_path: Path, geometry: Any) -> dict[str, float]:
    """Average a raster inside one polygon without calling `zonal_stats`.

    Deliberately a different route to the same number: a window read generously
    and padded rather than fitted, cell centres built from the transform by hand,
    and shapely asked one point at a time. Slow, and that is fine for one
    polygon. An earlier draft of this function padded nothing and quietly lost
    the boundary cells, which is exactly the mistake it exists to catch -- so the
    padding here is load-bearing, not defensive.
    """
    pad = 3
    with rasterio.open(raster_path) as handle:
        fitted = rasterio.windows.from_bounds(*geometry.bounds, transform=handle.transform)
        window = rasterio.windows.Window(
            int(np.floor(fitted.col_off)) - pad,
            int(np.floor(fitted.row_off)) - pad,
            int(np.ceil(fitted.width)) + 2 * pad,
            int(np.ceil(fitted.height)) + 2 * pad,
        )
        block = handle.read(1, window=window, boundless=True, fill_value=handle.nodata)
        transform = handle.window_transform(window)
        nodata = handle.nodata

    rows, cols = block.shape
    grid_rows, grid_cols = np.mgrid[0:rows, 0:cols]
    xs = transform.c + (grid_cols + 0.5) * transform.a
    ys = transform.f + (grid_rows + 0.5) * transform.e
    inside = np.array(
        [
            [geometry.contains(Point(float(xs[r, c]), float(ys[r, c]))) for c in range(cols)]
            for r in range(rows)
        ]
    )
    picked = block[inside].astype("float64")
    picked = picked[np.isfinite(picked)]
    if nodata is not None:
        picked = picked[picked != nodata]
    if not picked.size:
        return {"count": 0.0, "min": float("nan"), "mean": float("nan"), "max": float("nan")}
    return {
        "count": float(picked.size),
        "min": float(picked.min()),
        "mean": float(picked.mean()),
        "max": float(picked.max()),
    }


def _apportion_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """Roll up frames built so the answer is known before the call is made.

    The live county cannot exercise this. Block-group population is controlled
    to sum to the tract estimate, so the real apportionment error is exactly
    zero, and a machine that returned zero unconditionally would look identical.
    These fixtures disagree on purpose, by a margin stated here as arithmetic.
    """
    children = ["010010001001", "010010001002", "010010002001", "010010002002"]
    parents = ["01001000100", "01001000200"]

    fine = pd.DataFrame(
        {
            Col.GEOID: children,
            Col.POPULATION: [100, 150, 120, 80],
            Col.PCT_POVERTY: [10.0, 50.0, 25.0, 25.0],
        }
    )
    coarse = pd.DataFrame(
        {Col.GEOID: parents, Col.POPULATION: [300, 200], Col.PCT_POVERTY: [34.0, 25.0]}
    )

    rolled, wrong = aligner.apportion_detailed(
        fine, coarse, [Col.POPULATION], method="sum", fine_name="fixture", coarse_name="parent"
    )
    expected_error = 100.0 * 50.0 / 300.0

    agreeing = coarse.copy()
    agreeing[Col.POPULATION] = [250, 200]
    _, right = aligner.apportion_detailed(fine, agreeing, [Col.POPULATION], method="sum")

    weighted, weighted_evidence = aligner.apportion_detailed(
        fine, coarse, [Col.PCT_POVERTY], method="population_weighted"
    )
    summed, _ = aligner.apportion_detailed(fine, coarse, [Col.PCT_POVERTY], method="sum")

    zeroed = coarse.copy()
    zeroed[Col.POPULATION] = [250, 0]
    _, zero_evidence = aligner.apportion_detailed(fine, zeroed, [Col.POPULATION], method="sum")

    suppressed = fine.copy()
    suppressed[Col.POPULATION] = pd.array([100, None, 120, 80], dtype="Int64")
    gapped, gap_evidence = aligner.apportion_detailed(
        suppressed, agreeing, [Col.POPULATION], method="sum"
    )

    orphaned = pd.concat(
        [
            fine,
            pd.DataFrame(
                {Col.GEOID: ["010010009001"], Col.POPULATION: [10], Col.PCT_POVERTY: [1.0]}
            ),
        ],
        ignore_index=True,
    )
    _, orphan_evidence = aligner.apportion_detailed(
        orphaned, coarse, [Col.POPULATION], method="sum"
    )

    unpublished = coarse.drop(columns=[Col.PCT_POVERTY])
    _, unpublished_evidence = aligner.apportion_detailed(
        fine, unpublished, [Col.POPULATION, Col.PCT_POVERTY], method="sum"
    )

    def refusal(call: Any, kind: Any, phrase: str) -> bool:
        """Did this call refuse for the stated reason? See `_zonal_checks`."""
        try:
            call()
        except kind as exc:
            return phrase in str(exc)
        except Exception:
            return False
        return False

    mixed = pd.DataFrame(
        {Col.GEOID: ["010010001001", "01001000200"], Col.POPULATION: [1, 2]}
    )
    repeated = pd.DataFrame({Col.GEOID: parents[:1] * 2, Col.POPULATION: [1, 2]})

    print(
        f"apportion fixture: children summing to 250 and 200 against published 300 and 200 "
        f"-> max per-unit error {wrong.error.get(Col.POPULATION, -1):.4f}% "
        f"(expected {expected_error:.4f}%), max abs "
        f"{wrong.max_abs_difference.get(Col.POPULATION, -1):.0f} (expected 50)"
    )
    print(
        f"  a rate over populations 100 and 150: weighted "
        f"{float(weighted[Col.PCT_POVERTY].iloc[0]):.4f} (expected 34.0), "
        f"summed {float(summed[Col.PCT_POVERTY].iloc[0]):.4f} (expected 60.0) -- "
        "the two methods are not interchangeable and only one is right for a share"
    )

    return [
        ("children that do not sum to the published parent produce the exact error expected",
         abs(wrong.error[Col.POPULATION] - expected_error) < 1e-9
         and wrong.max_abs_difference[Col.POPULATION] == 50.0),
        ("the same call on agreeing frames reports zero, so the live zero is a result",
         right.error[Col.POPULATION] == 0.0
         and right.units_compared[Col.POPULATION] == 2),
        ("the error is the worst unit, not the county total, so opposite errors cannot cancel",
         abs(wrong.error[Col.POPULATION] - expected_error) < 1e-9
         and wrong.total_fine[Col.POPULATION] != wrong.total_coarse[Col.POPULATION]),
        ("a population-weighted share equals the weighted mean computed by hand",
         abs(float(weighted[Col.PCT_POVERTY].iloc[0]) - 34.0) < 1e-9),
        ("and summing that same share gives a different, wrong answer, so the weighting is real",
         abs(float(summed[Col.PCT_POVERTY].iloc[0]) - 60.0) < 1e-9),
        ("the weighted call records which column it weighted by",
         weighted_evidence.weight_column == Col.POPULATION),
        ("a unit publishing zero is excluded from the percentage and counted, not dropped",
         zero_evidence.units_undefined[Col.POPULATION] == 1
         and zero_evidence.units_compared[Col.POPULATION] == 2
         and zero_evidence.error[Col.POPULATION] == 0.0),
        ("a suppressed child suppresses its parent rather than shrinking it to a partial sum",
         bool(pd.isna(gapped[Col.POPULATION].iloc[0]))
         and gap_evidence.incomplete[Col.POPULATION] == 1
         and gap_evidence.units_compared[Col.POPULATION] == 1),
        ("a child whose parent is absent is counted rather than silently left out",
         orphan_evidence.orphan_children == 1
         and orphan_evidence.fine_rows == len(fine) + 1),
        ("a column the coarse frame does not publish is aggregated but named as uncompared",
         unpublished_evidence.not_published == (Col.PCT_POVERTY,)
         and unpublished_evidence.compared == (Col.POPULATION,)),
        ("the rollup width is read from the coarse GEOIDs rather than written down",
         wrong.fine_width == 12 and wrong.coarse_width == 11 and wrong.parents == 2),
        ("the protocol wrapper returns the same error dict the evidence carries",
         aligner.apportion(fine, coarse, [Col.POPULATION], method="sum")[1] == wrong.error),
        ("weighting a population by itself is refused rather than answered",
         refusal(
             lambda: aligner.apportion(fine, coarse, [Col.POPULATION],
                                       method="population_weighted"),
             ValueError,
             "by itself",
         )),
        ("a method this module does not know is refused by name",
         refusal(lambda: aligner.apportion(fine, coarse, [Col.POPULATION], method="mean"),
                 ValueError, "does not know the method")),
        ("rolling up no column at all is refused",
         refusal(lambda: aligner.apportion(fine, coarse, [], method="sum"), ValueError,
                 "no column at all")),
        ("a column the fine frame does not carry is refused",
         refusal(lambda: aligner.apportion(fine, coarse, ["absent"], method="sum"), KeyError,
                 "column(s) the frame does not carry")),
        ("a frame holding two geographic levels at once is refused",
         refusal(lambda: aligner.apportion(mixed, coarse, [Col.POPULATION], method="sum"),
                 ValueError, "more than one geographic level")),
        ("apportioning a coarse frame into a finer one is refused, since it inverts the nesting",
         refusal(lambda: aligner.apportion(coarse, fine, [Col.POPULATION], method="sum"),
                 ValueError, "must be the longer identifier")),
        ("a repeated coarse GEOID is refused, since there is no single published value",
         refusal(lambda: aligner.apportion(fine, repeated, [Col.POPULATION], method="sum"),
                 ValueError, "rows repeat a")),
        ("an empty fine frame is refused as having no rows, not as a granularity mismatch",
         refusal(lambda: aligner.apportion(fine.iloc[0:0], coarse, [Col.POPULATION],
                                           method="sum"),
                 ValueError, "carries no rows")),
        ("and an empty coarse frame is refused the same way",
         refusal(lambda: aligner.apportion(fine, coarse.iloc[0:0], [Col.POPULATION],
                                           method="sum"),
                 ValueError, "carries no rows")),
    ]


def _indicator_checks(
    clean: pd.DataFrame, tracts: Any, table_prov: Any
) -> tuple[list[str], list[str]]:
    """Re-derive every share a second way, and bound it against its own universe.

    Two separate questions. The recomputation restates the definition through a
    different expression -- a Python sum across named columns rather than the
    spec-driven `DataFrame.sum(axis=1)` loop in `derive_acs_columns` -- which is
    what catches the machinery picking up the wrong denominator or the wrong
    leaves. The bound is not a restatement at all: a numerator drawn from inside
    a table can never exceed that table's own total, so a pattern that matched a
    parent row as well as its children shows up here as a share over 100 even
    though every individual number looks plausible.
    """
    recomputed: list[str] = []
    bounded: list[str] = []
    if tracts is None:
        return recomputed, bounded
    for key, spec in sorted(table_prov.request_params.items()):
        if not key.startswith(ACS_RESOLVED_PREFIX):
            continue
        name = key[len(ACS_RESOLVED_PREFIX) :]
        denominator = acquire.acs_denominator_id(spec)
        if denominator is None or name not in tracts.columns:
            continue
        numerators = list(acquire.acs_numerator_ids(spec))
        top = pd.to_numeric(clean[numerators[0]], errors="coerce")
        for extra in numerators[1:]:
            top = top + pd.to_numeric(clean[extra], errors="coerce")
        base = pd.to_numeric(clean[denominator], errors="coerce")
        direct = pd.DataFrame(
            {
                Col.GEOID: clean[Col.GEOID],
                "direct": (top / base.where(base > 0)) * 100.0,
                "within": top <= base,
            }
        )
        merged = tracts[[Col.GEOID, name]].merge(direct, on=Col.GEOID)
        same = np.isclose(
            merged[name].astype("float64"),
            merged["direct"].astype("float64"),
            rtol=1e-9,
            equal_nan=True,
        )
        if len(merged) and bool(same.all()):
            recomputed.append(name)
        if len(merged) and bool(merged["within"].all()):
            bounded.append(name)
    return recomputed, bounded


def _snapshot_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """Run the whole thing on the real snapshot, and prove its zeros.

    The cross-checks here are deliberately not "the same number twice". The
    geometry count is checked against the feature counts the manifest recorded
    at retrieval time, and the population is checked against the same population
    totalled at the other granularity -- block groups nest exactly inside
    tracts, so the two totals are the same number arrived at down two different
    paths.
    """
    snapshot = aligner.align_snapshot()
    report, evidence = snapshot.report, snapshot.evidence
    registry = aligner.registry

    declared = sum(
        record.provenance.feature_count
        for record in registry.records()
        if record.kind == "vector"
    )
    cells = sum(
        len(item.columns) * len(snapshot.frames[item.dataset]) for item in evidence.scrubs
    )

    tracts = snapshot.frames.get(f"{acquire.DATASET_TRACTS}{JOINED_SUFFIX}")
    groups = snapshot.frames.get(f"{acquire.DATASET_BLOCK_GROUPS}{JOINED_SUFFIX}")
    tract_population = int(tracts[Col.POPULATION].sum()) if tracts is not None else -1
    group_population = int(groups[Col.POPULATION].sum()) if groups is not None else -2

    planted = snapshot.frames[acquire.DATASET_ACS].copy()
    target = registry.provenance_of(acquire.DATASET_ACS).request_params[ACS_ESTIMATES_KEY]
    first_estimate = target.split(",")[0]
    planted[first_estimate] = planted[first_estimate].astype("Float64")
    _, before_planting = aligner.scrub_sentinels(planted, [first_estimate])
    baseline_removed = before_planting.get(first_estimate, 0)
    planted.loc[planted.index[0], first_estimate] = float(-666666666)
    _, planted_removed = aligner.scrub_sentinels(planted, [first_estimate])

    indicators = [Col.POPULATION, Col.POP_MOE, *VULNERABILITY_INDICATORS]
    present = [name for name in indicators if tracts is not None and name in tracts.columns]
    shares = [
        name
        for name in VULNERABILITY_INDICATORS
        if tracts is not None
        and name in tracts.columns
        and bool(
            (((tracts[name] >= 0) & (tracts[name] <= 100)) | tracts[name].isna()).all()
        )
    ]
    defined = [
        name
        for name in VULNERABILITY_INDICATORS
        if tracts is not None and name in tracts.columns and int(tracts[name].notna().sum()) > 0
    ]
    reported_nulls = sum(
        count
        for key, count in evidence.undefined.items()
        if key.startswith(f"{acquire.DATASET_ACS}.")
    )
    actual_nulls = (
        sum(int(tracts[name].isna().sum()) for name in VULNERABILITY_INDICATORS)
        if tracts is not None
        else -1
    )

    table_prov = registry.provenance_of(acquire.DATASET_ACS)
    raw = registry.load(acquire.DATASET_ACS)
    value_columns = aligner.acs_value_columns(table_prov, raw)
    clean, _ = aligner.scrub_sentinels_detailed(raw, value_columns)

    recomputed, bounded = _indicator_checks(clean, tracts, table_prov)

    age_spec = table_prov.request_params.get(f"{ACS_RESOLVED_PREFIX}{Col.PCT_AGE_65_PLUS}", "")
    age_total = acquire.acs_denominator_id(age_spec) or ""
    cross_table = pd.DataFrame(
        {Col.GEOID: clean[Col.GEOID], "other": pd.to_numeric(clean[age_total], errors="coerce")}
    ).merge(tracts[[Col.GEOID, Col.POPULATION]], on=Col.GEOID)
    agrees = bool(
        len(cross_table)
        and (cross_table["other"].astype("Float64") == cross_table[Col.POPULATION]).all()
    )

    leaf = acquire.acs_numerator_ids(age_spec)[0]
    poisoned = raw.copy()
    poisoned.loc[poisoned.index[0], leaf] = str(-666666666)
    disinfected, poison = aligner.scrub_sentinels_detailed(poisoned, value_columns)
    propagated, _, _ = aligner.derive_acs_columns(disinfected, table_prov)
    suppresses = (
        poison.removed[leaf] == 1
        and bool(pd.isna(propagated[Col.PCT_AGE_65_PLUS].iloc[0]))
        and not bool(pd.isna(propagated[Col.PCT_AGE_65_PLUS].iloc[1]))
    )

    quoted = all(
        report.reprojected[record.name]
        == f"{record.provenance.declared_crs} -> {record.provenance.working_crs}"
        for record in registry.records()
    )
    distinct = len(set(report.reprojected.values()))
    vintages = len(set(report.temporal_span.values()))

    degraded_by_flag = {
        record.name
        for record in registry.records()
        if record.provenance.request_params.get(DEGRADED_KEY) == "true"
    }
    def fixture_provenance(flagged: bool, features: int) -> Provenance:
        return Provenance(
            dataset="fixture",
            source_url="https://example.invalid/fixture",
            retrieved_at=prov.utc_now(),
            declared_crs=config.STORAGE_CRS,
            working_crs=aligner.working_crs,
            vintage="fixture",
            feature_count=features,
            license="fixture",
            request_params={DEGRADED_KEY: "true"} if flagged else {},
        )

    discriminates = is_degraded(fixture_provenance(True, 5)) and not is_degraded(
        fixture_provenance(False, 0)
    )

    joined_columns = {
        name: set(snapshot.frames[f"{name}{JOINED_SUFFIX}"].columns)
        for name in (acquire.DATASET_TRACTS, acquire.DATASET_BLOCK_GROUPS)
        if f"{name}{JOINED_SUFFIX}" in snapshot.frames
    }
    elevation_columns = set(RASTER_STAT_COLUMNS.values())
    expected_columns = {
        acquire.DATASET_TRACTS: {Col.GEOID, "geometry", Col.POPULATION, Col.POP_MOE}
        | set(VULNERABILITY_INDICATORS)
        | elevation_columns,
        acquire.DATASET_BLOCK_GROUPS: {Col.GEOID, "geometry", Col.POPULATION, Col.POP_MOE}
        | elevation_columns,
    }
    coordinate_named = {
        name: {
            column
            for column in snapshot.frames[name].columns
            if re.fullmatch(r"[A-Z]*(LAT|LON)[A-Z]*", column)
        }
        for name in joined_columns
    }
    coordinates_removed = sorted(
        column for name, columns in coordinate_named.items() for column in columns
    )
    coordinates_leaked = sorted(
        column
        for name, columns in coordinate_named.items()
        for column in columns
        if column in joined_columns[name]
    )
    raw_kept = any(
        column.startswith(first_estimate[0]) and "_" in column
        for column in snapshot.frames[acquire.DATASET_ACS].columns
    )
    share_dtypes = {
        str(tracts[name].dtype) for name in VULNERABILITY_INDICATORS if tracts is not None
    }

    quotes_provenance = all(
        any(note in evidence.degraded[record.name] for note in record.provenance.notes)
        for record in registry.records()
        if record.name in evidence.degraded
    )
    unflagged_empty = {
        record.name
        for record in registry.records()
        if record.kind == "vector"
        and record.provenance.feature_count == 0
        and record.name not in degraded_by_flag
    }
    print(
        f"degradation: {sorted(degraded_by_flag) or 'none'} flagged in provenance; "
        f"{sorted(unflagged_empty) or 'none'} empty without a flag. On this snapshot the "
        "two sets happen to coincide, so neither test is asserted against the other"
    )

    apportion = evidence.apportionments[0] if evidence.apportionments else None
    zonal_by_layer = {item.dataset: item for item in evidence.zonals}
    zero_published = int((tracts[Col.POPULATION] == 0).sum()) if tracts is not None else -1

    prefix_width = apportion.coarse_width if apportion else 0
    nested_units, cell_gap = 0, -1.0
    if (
        tracts is not None
        and groups is not None
        and prefix_width
        and Col.RASTER_CELLS in tracts.columns
        and Col.RASTER_CELLS in groups.columns
    ):
        per_tract = tracts.set_index(Col.GEOID)[Col.RASTER_CELLS].astype("Float64")
        rolled_cells = groups.groupby(groups[Col.GEOID].str[:prefix_width])[
            Col.RASTER_CELLS
        ].sum()
        cell_pair = pd.DataFrame(
            {"tract": per_tract, "rolled": rolled_cells.astype("Float64")}
        ).dropna()
        nested_units = len(cell_pair)
        if nested_units:
            cell_gap = float((cell_pair["tract"] - cell_pair["rolled"]).abs().max())

    measured_rasters = len({item.raster for item in evidence.zonals})
    counts_agree = bool(zonal_by_layer) and all(
        name in snapshot.frames
        and Col.RASTER_CELLS in snapshot.frames[name].columns
        and int(snapshot.frames[name][Col.RASTER_CELLS].sum()) == item.valid_cells
        for name, item in zonal_by_layer.items()
    )
    elevation_named = bool(zonal_by_layer) and all(
        elevation_columns <= set(snapshot.frames[name].columns)
        for name in zonal_by_layer
        if name in snapshot.frames
    )

    print()
    print(format_report(report, evidence, aligner.study_area))
    print()
    print(
        f"cross-check: {evidence.geometries_examined} geometries examined against "
        f"{declared} features the manifest recorded at retrieval"
    )
    print(
        f"cross-check: block group cell counts rolled up to {nested_units} tract(s) by "
        f"GEOID prefix differ from the tract's own count by at most {cell_gap:g} cell(s) -- "
        "the same nesting the population is checked through, applied to the raster"
    )
    print(
        f"cross-check: {tract_population:,} people totalled over tracts against "
        f"{group_population:,} totalled over block groups, two granularities of the same county"
    )
    print(
        f"planted sentinel: one -666666666 injected into {first_estimate} of the real "
        f"table -> {planted_removed} removed"
    )

    return [
        ("align_snapshot reports every manifest entry, raster included",
         len(report.reprojected) == len(registry.names())
         and len(report.temporal_span) == len(registry.names())),
        ("every reprojection is quoted from Provenance, and confirmed against the loaded CRS",
         bool(evidence.crs_observed)
         and all(value == aligner.working_crs for value in evidence.crs_observed.values())),
        ("each entry is that dataset's own declared CRS, not one string repeated",
         quoted and distinct > 1),
        ("each vintage is that dataset's own, not one string repeated",
         vintages > 1 and all(report.temporal_span.values())),
        ("the geometries examined equal the feature counts the manifest recorded",
         evidence.geometries_examined == declared and declared > 0),
        ("the cells scrubbed equal columns x rows recomputed from the frames",
         evidence.cells_examined == cells and cells > 0),
        ("the repair count is zero exactly when nothing was invalid, over a real denominator",
         evidence.geometries_examined > 0
         and (report.geometries_repaired == 0) == (evidence.geometries_invalid == 0)),
        ("the sentinel count is zero exactly when nothing was negative, over a real denominator",
         evidence.values_examined > 0
         and (report.sentinels_removed == {})
         == (sum(item.negative for item in evidence.scrubs) == 0)),
        ("planting one sentinel in the real table raises that same count by exactly one",
         planted_removed.get(first_estimate, 0) == baseline_removed + 1),
        ("the unmatched tuples are empty exactly when the joins matched everything",
         evidence.geoids_compared > 0
         and (report.unmatched_left == ())
         == all(item.unmatched_left == 0 for item in evidence.joins)
         and (report.unmatched_right == ())
         == all(item.unmatched_right == 0 for item in evidence.joins)),
        ("both granularities were joined, and each join accounted for every row it was given",
         len(evidence.joins) == 2
         and all(
             item.matched + item.unmatched_left == item.left_rows
             and item.matched + item.unmatched_right == item.right_rows
             for item in evidence.joins
         )),
        ("the two granularities are separated by GEOID width, read from the data",
         [sorted(item.left_widths) for item in evidence.joins] == [[11], [12]]),
        ("population totals agree between the two granularities",
         tract_population > 0 and tract_population == group_population),
        ("every canonical indicator column was derived onto the joined layer",
         len(present) == len(indicators)),
        ("every derived share is a percentage in range wherever it is defined",
         len(shares) == len(VULNERABILITY_INDICATORS)),
        ("no derived share is null for every unit, which a broken denominator would make it",
         len(defined) == len(VULNERABILITY_INDICATORS)),
        ("every null a division created is counted and warned about, not left silent",
         actual_nulls == reported_nulls
         and (reported_nulls == 0
              or any("universe for that indicator is zero" in item for item in report.warnings))),
        ("the three fields session 7 owns hold measured results, not placeholder warnings",
         report.apportioned != {}
         and report.apportionment_error != {}
         and evidence.units_apportioned > 0
         and evidence.polygons_measured > 0
         and not any("placeholder" in item for item in report.warnings)),
        ("every derived share re-derives to the same value through a different expression",
         len(recomputed) == len(VULNERABILITY_INDICATORS)),
        ("no numerator exceeds its own table total, which an over-matched pattern would",
         len(bounded) == len(VULNERABILITY_INDICATORS)),
        ("the population agrees with the total published in a different ACS table", agrees),
        ("a sentinel planted in one leaf suppresses that indicator instead of shrinking it",
         suppresses),
        ("degradation is recognised from the Provenance flag and from nothing else",
         set(evidence.degraded) == degraded_by_flag),
        ("the degraded test flags a flagged dataset with rows and clears an empty one without",
         discriminates),
        ("a joined layer carries only GEOID, the canonical columns and geometry",
         joined_columns == expected_columns),
        ("the source layers do carry coordinate attributes, so this test has a denominator",
         len(coordinates_removed) > 0),
        ("and none of them survives into a layer a tool could serialise",
         coordinates_leaked == []),
        ("the raw estimate columns still exist on the unjoined table, so this was a selection",
         raw_kept),
        ("a derived share keeps one dtype regardless of what this county's values allow",
         share_dtypes == {"Float64"}),
        ("every degraded dataset quotes its own Provenance notes, not a message written here",
         quotes_provenance),
        ("every tract's apportioned population equals its published one, unit by unit",
         apportion is not None
         and apportion.units_compared.get(Col.POPULATION, 0) == len(tracts)
         and apportion.max_abs_difference.get(Col.POPULATION) == 0.0
         and report.apportionment_error.get(Col.POPULATION) == 0.0),
        ("the apportioned population equals the published total at the coarser granularity",
         apportion is not None
         and apportion.total_fine.get(Col.POPULATION, -1.0)
         == apportion.total_coarse.get(Col.POPULATION, -2.0)
         and apportion.total_fine.get(Col.POPULATION, 0.0) > 0),
        ("the unit publishing a zero population is excluded from the percentage and counted",
         apportion is not None
         and apportion.units_undefined.get(Col.POPULATION, -1) == zero_published),
        ("no child unit was left out of an aggregate and no parent was left uncompared",
         apportion is not None
         and apportion.orphan_children == 0
         and apportion.childless_parents == 0
         and apportion.fine_rows == len(groups)
         and apportion.parents == len(tracts)),
        ("units_below_cell_threshold is zero exactly when the smallest polygon clears it",
         evidence.polygons_measured > 0
         and (report.units_below_cell_threshold == 0)
         == (evidence.smallest_polygon_cells >= MIN_RASTER_CELLS)),
        ("every polygon of both granularities was measured against every raster",
         measured_rasters > 0
         and evidence.polygons_measured
         == (len(tracts) + len(groups)) * measured_rasters),
        ("the cell counts in the layers sum to the usable cells the evidence recorded",
         counts_agree),
        ("block group cell counts roll up to tract cell counts through the same nesting",
         nested_units == len(tracts) and cell_gap == 0.0),
        ("no cell inside any unit was nodata or non-finite, over a real denominator",
         sum(item.cells for item in evidence.zonals) > 0
         and sum(item.nodata_cells + item.non_finite_cells for item in evidence.zonals) == 0),
        ("the elevation statistics reached both joined layers under the names Col publishes",
         elevation_named),
    ]


def _contract_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    source = Path(__file__).read_text(encoding="utf-8")
    checks: list[tuple[str, bool]] = [
        ("every method on the frozen Aligner protocol exists", isinstance(aligner, Aligner))
    ]

    protocol_methods = sorted(
        name
        for name, member in vars(Aligner).items()
        if not name.startswith("_") and inspect.isfunction(member)
    )
    mismatched: list[str] = []
    for name in protocol_methods:
        declared = [
            (item.name, item.kind, item.default)
            for item in inspect.signature(getattr(Aligner, name)).parameters.values()
            if item.name != "self"
        ]
        found = [
            (item.name, item.kind, item.default)
            for item in inspect.signature(getattr(Alignment, name)).parameters.values()
            if item.name != "self"
        ]
        if declared != found:
            mismatched.append(f"{name}: contract {declared} vs module {found}")
    for line in mismatched:
        print(f"  signature drift: {line}")
    print(f"contract: {len(protocol_methods)} signatures compared against contracts.py")
    checks.append(
        ("every signature matches contracts.py argument for argument", not mismatched)
    )

    placeholders = [
        name
        for name in protocol_methods
        if "NotImplementedError" in inspect.getsource(getattr(Alignment, name))
    ]
    for name in placeholders:
        print(f"  still a placeholder: {name}")
    checks.append(
        ("no method on the protocol is still a placeholder that raises instead of answering",
         not placeholders)
    )
    retired = "DEFERRED" "_FIELDS"
    checks.append(
        ("the module names no deferred field anywhere, having filled all three of them",
         retired not in source and not hasattr(sys.modules[__name__], retired))
    )

    implementation = "".join(
        inspect.getsource(part)
        for part in (
            Alignment,
            *EVIDENCE_RECORDS,
            is_degraded,
            census_jam_values,
            sentinel_label,
            _as_narrow_numeric,
            _geoid_strings,
            _width_counts,
            _make_valid,
            format_report,
            _line,
            main,
        )
    )
    metric_calls = {
        name: len(re.findall(pattern, implementation))
        for name, pattern in METRIC_OPERATIONS.items()
    }
    print(f"  metric-sensitive calls outside the self check: {metric_calls}")
    checks.append(
        ("the only reprojection is inside to_working_crs, and no metric operation bypasses it",
         metric_calls["to_crs"] == 1 and not any(
             count for name, count in metric_calls.items() if name != "to_crs"
         ))
    )

    unannotated = [
        f"{name}{inspect.signature(fn)}"
        for name, fn in _module_functions()
        if not _fully_annotated(fn)
    ]
    for line in unannotated:
        print(f"  unannotated: {line}")
    checks.append(("every function and method carries type hints", not unannotated))

    predicate_uses = len(re.findall(r"is_degraded\(", implementation))
    row_count_tests = len(re.findall(r"feature_count\s*==\s*0", implementation))
    print(f"  degradation tested by is_degraded x{predicate_uses}, by row count x{row_count_tests}")
    checks.append(
        (
            "degradation is only ever tested through is_degraded, never through a row count",
            predicate_uses >= 2 and row_count_tests == 0,
        )
    )

    tokens = {
        token
        for area in (config.STUDY_AREA, config.TRANSFER_AREA)
        for token in (
            area.county_geoid,
            area.state_fips + area.county_fips,
            *re.findall(r"[A-Z][a-z]+", area.name.split(",")[0]),
        )
        if token not in acquire._PLACE_WORDS
    }
    found = sorted(token for token in tokens if token in source)
    print(f"  source scan for a hardcoded study area: {found or 'none'}")
    checks.append(
        ("no token of either configured study area appears in this module", not found)
    )
    return checks


def _module_functions() -> list[tuple[str, Any]]:
    module = sys.modules[__name__]
    functions = [
        (name, member)
        for name, member in vars(module).items()
        if inspect.isfunction(member) and member.__module__ == __name__
    ]
    holders = (Alignment, *EVIDENCE_RECORDS)
    for holder in holders:
        for name, member in vars(holder).items():
            if name.startswith("__"):
                continue
            if isinstance(member, property):
                member = member.fget
            elif isinstance(member, (staticmethod, classmethod)):
                member = member.__func__
            if inspect.isfunction(member):
                functions.append((f"{holder.__name__}.{name}", member))
    return functions


def _fully_annotated(fn: Any) -> bool:
    signature = inspect.signature(fn)
    for item in signature.parameters.values():
        if item.name in ("self", "cls"):
            continue
        if item.annotation is inspect.Signature.empty:
            return False
    return signature.return_annotation is not inspect.Signature.empty


def _self_check() -> int:
    registry = Registry()
    registry.load_manifest()
    aligner = Alignment(registry)

    print(f"study area: {aligner.study_area.name}   working crs: {aligner.working_crs}")
    print(f"snapshot:   {len(registry.names())} manifest entries -> {registry.names()}\n")

    checks = _crs_checks(aligner)
    print()
    checks += _repair_checks(aligner)
    print()
    checks += _scrub_checks(aligner)
    print()
    checks += _derive_checks(aligner)
    print()
    checks += _join_checks(aligner)
    print()
    checks += _zonal_checks(aligner)
    print()
    checks += _apportion_checks(aligner)
    checks += _snapshot_checks(aligner)
    print()
    checks += _contract_checks(aligner)

    print()
    failed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        failed += 0 if ok else 1
    print("\nall checks passed" if failed == 0 else f"\n{failed} check(s) failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
