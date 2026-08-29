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

* **Three states, not two.** A field is zero because nothing needed doing, or
  non-zero because something did, or *not yet implemented* -- `apportioned`,
  `apportionment_error` and `units_below_cell_threshold` belong to session 7 and
  are named in `DEFERRED_FIELDS` rather than left looking like clean results.

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
from pyproj import CRS, Geod
from shapely import wkt as shapely_wkt
from shapely.geometry import Polygon

from . import acquire, config
from . import provenance as prov
from .contracts import (
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

DEFERRED_FIELDS: dict[str, str] = {
    "apportioned": "session 7 -- apportion()",
    "apportionment_error": "session 7 -- apportion()",
    "units_below_cell_threshold": "session 7 -- zonal_stats()",
}
"""`AlignmentReport` fields this session does not fill. Printed as their own
category, because an empty dict from a function that never ran looks exactly
like an empty dict from a function that ran and found nothing to do. That
confusion is the whole reason this module reports denominators."""

MOE_RULE = "root of the summed squares, the Bureau's published rule for the margin of a derived sum"

METRIC_OPERATIONS: dict[str, str] = {
    "to_crs": r"\.to_crs\(",
    "area": r"\.area\b",
    "buffer": r"\.buffer\(",
    "centroid": r"\.centroid\b",
    "distance": r"\.distance\(",
    "length": r"\.length\b",
}
"""Every call invariant 2 covers, and the marker that separates the
implementation from its own self check -- the first line of the first fixture. The implementation half of this file may
contain exactly one of these -- the `to_crs` inside `to_working_crs` -- because
each of the others answers in degrees on a geographic frame, silently. The self
check deliberately does the opposite on purpose, which is why it is scanned
separately rather than exempted by name."""


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
    datasets: tuple[str, ...] = ()
    crs_observed: dict[str, str] = field(default_factory=dict)
    degraded: dict[str, str] = field(default_factory=dict)
    derived: dict[str, str] = field(default_factory=dict)
    deferred: dict[str, str] = field(default_factory=dict)
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

    # -- session 7 -----------------------------------------------------------

    def apportion(
        self,
        fine: Any,
        coarse: Any,
        columns: list[str],
        *,
        method: Literal["sum", "population_weighted"],
    ) -> tuple[Any, dict[str, float]]:
        """Not implemented in this session. Session 7 owns it.

        Declared rather than omitted so that `isinstance(Alignment(), Aligner)`
        answers honestly and the frozen signature is checked from today, and
        raising rather than returning an empty result so that no caller can
        mistake "not built" for "nothing to do" -- the distinction this whole
        module is about.
        """
        raise NotImplementedError(f"apportion is {DEFERRED_FIELDS['apportioned']}")

    def zonal_stats(
        self,
        raster_path: Path,
        polygons: Any,
        *,
        stats: tuple[str, ...] = ("min", "mean", "max", "count"),
    ) -> Any:
        """Not implemented in this session. Session 7 owns it. See `apportion`."""
        raise NotImplementedError(
            f"zonal_stats is {DEFERRED_FIELDS['units_below_cell_threshold']}"
        )

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
        evidence = AlignmentEvidence(
            datasets=tuple(registry.names()), deferred=dict(DEFERRED_FIELDS)
        )
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

        for name, owner in sorted(evidence.deferred.items()):
            report.warnings.append(
                f"{name} is not filled by this stage and its value here is a placeholder, "
                f"not a result: {owner}"
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
    out.append("  not filled by this session -- three states, not two:")
    for name, owner in sorted(evidence.deferred.items()):
        current = getattr(report, name)
        out.append(f"      {name:<27} {str(current):<9} {owner}")

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
    expected_columns = {
        acquire.DATASET_TRACTS: {Col.GEOID, "geometry", Col.POPULATION, Col.POP_MOE}
        | set(VULNERABILITY_INDICATORS),
        acquire.DATASET_BLOCK_GROUPS: {Col.GEOID, "geometry", Col.POPULATION, Col.POP_MOE},
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

    print()
    print(format_report(report, evidence, aligner.study_area))
    print()
    print(
        f"cross-check: {evidence.geometries_examined} geometries examined against "
        f"{declared} features the manifest recorded at retrieval"
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
        ("the deferred fields carry their placeholder status inside the frozen report",
         all(
             any(name in item and "placeholder" in item for item in report.warnings)
             for name in DEFERRED_FIELDS
         )),
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
        ("the fields session 7 owns are named as deferred rather than reported as clean",
         set(evidence.deferred) == set(DEFERRED_FIELDS)
         and report.apportioned == {}
         and report.apportionment_error == {}
         and report.units_below_cell_threshold == 0),
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

    deferred_raise = 0
    for call in (
        lambda: aligner.apportion(None, None, [], method="sum"),
        lambda: aligner.zonal_stats(Path("x.tif"), None),
    ):
        try:
            call()
        except NotImplementedError as exc:
            deferred_raise += int("session 7" in str(exc))
        except Exception:
            pass
    checks.append(
        ("the two session-7 methods raise and name their session, rather than returning empty",
         deferred_raise == 2)
    )

    implementation = "".join(
        inspect.getsource(part)
        for part in (
            Alignment,
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
    holders = (
        Alignment,
        RepairEvidence,
        ScrubEvidence,
        JoinEvidence,
        AlignmentEvidence,
        AlignedSnapshot,
    )
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
