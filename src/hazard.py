"""Bathtub inundation over the 3DEP elevation raster.

`depth = max(0, surge_height_m - elevation_m)`, per cell, for a named
`HazardScenario`. A deliberate simplification: it fills every cell below the
surge height whether or not water could reach it, ignores wave run-up, and
ignores the time the water takes to arrive. CLAUDE.md puts that in the
limitations rather than in the model, and what it buys is the raster-to-vector
multiscale join Track A actually names.

Four things this module is built around.

* **The fraction is measured, not estimated.** `zonal_stats` computes min, mean,
  max and count and refuses anything else by name, and an inundated *fraction*
  is none of them. So this module writes a second raster holding 1 where the
  cell is wet and 0 where it is dry, and the zonal MEAN of that raster is the
  fraction by definition. The alternative -- pulling a depth array per polygon
  and reducing it here -- would be a second windowing path to verify, and the
  windowing is the part that is easy to get subtly wrong and impossible to
  notice from a plausible-looking number. Reusing `zonal_stats` makes the
  fraction inherit the same cell-centre rule and the same nodata accounting as
  every other statistic in the table.

* **Nodata is not sea level.** The elevation nodata sentinel is -9999.0, and
  `surge - (-9999)` is ten kilometres of water. Every derived raster carries the
  source raster's nodata forward, so a hole stays a hole and never becomes the
  deepest inundation in the county. This county's raster has no nodata cell
  anywhere -- zero of 7,997,535 -- so that rule is not observable on live data
  and is proven on a synthetic raster instead. `format_report` says so where the
  zero is printed.

* **A degraded flood layer is not an absence of flooding.** The NFHL retrieval
  failed on this county and `acquire` registered an empty layer with
  `degraded=true`. This module reads that flag through `align.is_degraded`,
  never through a row count, and reports the hazard as elevation-only rather
  than quietly summing zero regulatory flood polygons into "no flood hazard".

* **Depth is checked against a measurement it did not make.** The joined layers
  already carry `Col.ELEV_MIN_M` from a separate zonal pass in `align.py`. The
  deepest cell in a unit is `max(0, surge - elev_min)` exactly, by the definition
  of the bathtub, so the check compares this module's answer to that one unit by
  unit. A number verified against a previous run of the same code is not
  verified.
"""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio

from . import acquire, align, config, verify
from . import provenance as prov
from .align import Alignment
from .contracts import Col, HazardScenario, Provenance
from .registry import Registry

SURGE_SOURCE = (
    "NOAA National Hurricane Center, National Storm Surge Hazard Maps (SLOSH MOM "
    "category bands)"
)
SURGE_URL = "https://www.nhc.noaa.gov/nationalsurge/"

BATHTUB_NOTE = (
    "surge height is an INPUT to this model, not a prediction of it: the bathtub "
    "fills every cell below the height whether or not water could reach it, with no "
    "wave run-up, no attenuation over land and no arrival time. The category-to-height "
    "mapping is a coarse reading of the published SLOSH maximum-of-maxima bands and is "
    "not specific to any one coastline, so the height should be read as a scenario "
    "label rather than as a forecast for a named storm"
)

HAZARD_SCENARIOS: tuple[HazardScenario, ...] = (
    HazardScenario(
        name="surge_1_5m",
        surge_height_m=1.5,
        source=f"{SURGE_SOURCE}; {SURGE_URL}",
        assumption_note=(
            "roughly a Category 1 to 2 surge on the US Atlantic and Gulf coast. "
            f"{BATHTUB_NOTE}"
        ),
    ),
    HazardScenario(
        name="surge_3_0m",
        surge_height_m=3.0,
        source=f"{SURGE_SOURCE}; {SURGE_URL}",
        assumption_note=(
            "roughly a Category 3 surge on the US Atlantic and Gulf coast. "
            f"{BATHTUB_NOTE}"
        ),
    ),
    HazardScenario(
        name="surge_5_0m",
        surge_height_m=5.0,
        source=f"{SURGE_SOURCE}; {SURGE_URL}",
        assumption_note=(
            "roughly a Category 4 to 5 surge on the US Atlantic and Gulf coast. "
            f"{BATHTUB_NOTE}"
        ),
    ),
)
"""The three scenarios, defined here rather than in `config.py`.

BUILD-PLAN says config; `config.py` says of itself that it holds settings, paths,
CRS constants and the study-area parameter, and that importing it touches no
filesystem. A surge height is a domain value consumed by exactly one module, and
it sits beside the model it parameterises for the same reason the weight presets
sit beside the index they weight. The heights are not county-specific, so
invariant 5 is untouched either way."""

DEPTH_STAT_COLUMNS: dict[str, str] = {
    "mean": Col.INUNDATION_MEAN_M,
    "max": Col.INUNDATION_MAX_M,
}
"""How a generic statistic off the DEPTH raster becomes a named column.

The analogue of `align.RASTER_STAT_COLUMNS`, and deliberately without a "count"
entry. `Col.RASTER_CELLS` is already filled from the elevation grid by
`align_snapshot`, and the derived rasters share that grid exactly, so remapping
"count" here would have two passes writing one column and the second would
silently win. The counts are compared instead -- see `_agree_on_cells`."""

WET_STAT_COLUMNS: dict[str, str] = {"mean": Col.INUNDATED_FRACTION}
"""The mean of a raster holding 1 for wet and 0 for dry IS the wet fraction.

That identity is the whole reason the mask raster exists: it turns a statistic
`zonal_stats` refuses to compute into one it already computes and has already
been verified against an independently calculated value."""

DEPTH_SUFFIX = "_depth"
WET_SUFFIX = "_wet"
"""Derived raster filenames are built from the scenario name and these, never
written out as literals, so adding a scenario cannot collide with an existing
file by a typo."""

DERIVED_NODATA = -9999.0
"""Carried forward from the elevation raster rather than chosen here. The source
value is read off the file at derive time and this is only the fallback for a
source that declares none -- a raster with no nodata has no hole to preserve,
and a sentinel still has to exist for cells this module marks unusable."""

DRY = 0.0
WET = 1.0
"""A cell is wet when its depth is strictly greater than zero, so a cell exactly
at the surge height is dry. Stated because the boundary is a choice and the
count of cells sitting exactly on it is reported."""

RASTER_PROFILE: dict[str, Any] = {
    "driver": "GTiff",
    "dtype": "float32",
    "count": 1,
    "compress": "deflate",
}
"""What every derived raster is written as. float32 and deflate because a depth
field is mostly zeros and compresses to a fraction of the source; the CRS,
transform and shape are copied from the source raster and never chosen here,
because `zonal_stats` refuses a raster whose CRS differs from the working one and
warping it would resample the values being measured. No predictor and no tiling:
a floating-point predictor is a different code for a different dtype, and getting
that wrong writes a file that reads back subtly changed."""


@dataclass(slots=True)
class SurfaceEvidence:
    """What deriving one inundation surface read and wrote, county-wide.

    The per-unit numbers come later, from `zonal_stats`. These are the whole-grid
    denominators, and they exist because "0 nodata cells were carried forward" is
    indistinguishable from "nodata was never handled" without the count of cells
    that were examined to say so.
    """

    scenario: str = ""
    surge_height_m: float = 0.0
    source_raster: str = ""
    crs: str = ""
    cell_size_m: float = 0.0
    cell_area_m2: float = 0.0
    cells: int = 0
    nodata_cells: int = 0
    non_finite_cells: int = 0
    usable_cells: int = 0
    wet_cells: int = 0
    at_threshold_cells: int = 0
    elevation_min_m: float = float("nan")
    elevation_max_m: float = float("nan")
    depth_max_m: float = float("nan")
    depth_path: Path = Path()
    wet_path: Path = Path()

    @property
    def wet_fraction(self) -> float:
        """Wet share of the usable grid, which is NOT the county's wet share.

        The raster covers the study-area bounding box, and this county's bounding
        box is largely ocean. The per-unit fractions are the county numbers; this
        one is a property of the retrieved extent and is printed as such.
        """
        return self.wet_cells / self.usable_cells if self.usable_cells else float("nan")


@dataclass(slots=True)
class HazardSurface:
    """One derived inundation surface: two rasters, a scenario and a provenance.

    Not registered in the `Registry`. Every dataset in the registry is something
    that was retrieved, and `align_snapshot` walks the registry measuring every
    raster it finds -- registering a computed layer there would make the
    alignment stage report zonal statistics over this module's own output as
    though a service had served it. Invariant 6 is upheld the other way: the
    `Provenance` travels on this record, names the raster it was derived from,
    and records the surge height that produced it.
    """

    scenario: HazardScenario
    depth_path: Path
    wet_path: Path
    provenance: Provenance
    evidence: SurfaceEvidence


@dataclass(slots=True)
class MeasureEvidence:
    """What measuring one surface over one set of polygons compared.

    `cells_matched` is the cross-check that earns its place: the depth raster,
    the wet raster and the elevation raster share a grid and a nodata pattern
    exactly, so every polygon must cover the same number of usable cells in all
    three. A per-unit disagreement is a windowing or nodata bug. The totals are
    recorded too but are not what is asserted -- two errors of opposite sign
    cancel in a sum.
    """

    scenario: str = ""
    dataset: str = ""
    polygons: int = 0
    cells_compared: int = 0
    cells_matched: int = 0
    elevation_cells_compared: int = 0
    elevation_cells_matched: int = 0
    units_measured: int = 0
    units_without_cells: int = 0
    units_fully_wet: int = 0
    units_fully_dry: int = 0
    fraction_min: float = float("nan")
    fraction_max: float = float("nan")
    deepest_unit: str = ""
    deepest_m: float = float("nan")
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HazardReport:
    """Every scenario measured over one set of polygons, with the evidence."""

    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    surfaces: dict[str, HazardSurface] = field(default_factory=dict)
    measures: list[MeasureEvidence] = field(default_factory=list)
    vector_hazard: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Hazard:
    """Derives and measures bathtub inundation surfaces for one study area.

    Holds an `Alignment` rather than a `Registry` because every raster read here
    goes through `zonal_stats`, which is the aligner's, and because the working
    CRS belongs to the study area the aligner already carries.
    """

    def __init__(self, aligner: Alignment | None = None) -> None:
        self.aligner = aligner or Alignment()
        self.registry: Registry = self.aligner.registry

    @property
    def working_crs(self) -> str:
        return self.aligner.working_crs

    @property
    def derived_dir(self) -> Path:
        return config.DERIVED_DIR

    # -- deriving the surface ------------------------------------------------

    def derive_surface(
        self, scenario: HazardScenario, *, raster_name: str = acquire.DATASET_ELEVATION
    ) -> HazardSurface:
        """Write the depth and wet-mask rasters for one scenario.

        Always recomputed, never reused from disk. A cached raster from a
        previous study area is indistinguishable on disk from a fresh one for
        this county, and the whole grid is eight million float32 cells -- cheap
        enough that caching would trade a real correctness risk for nothing.

        The output carries the source raster's CRS, transform, shape and nodata
        unchanged. Nothing here reprojects: `zonal_stats` refuses a raster whose
        CRS differs from the working one precisely so that a wrong CRS is a
        retrieval to fix rather than a resampling this module performs quietly.
        """
        if scenario.surge_height_m < 0:
            raise ValueError(
                f"scenario {scenario.name!r} carries a surge height of "
                f"{scenario.surge_height_m} m; a negative surge is not a lower flood, "
                "it is a sign error, and the bathtub would report every cell dry"
            )
        record = self.registry.record(raster_name)
        if record.kind != "raster":
            raise ValueError(
                f"{raster_name!r} is a {record.kind} dataset; the bathtub model needs the "
                "elevation raster and reaches it with Registry.path_of"
            )
        if align.is_degraded(record.provenance):
            raise ValueError(
                f"{raster_name!r} is flagged degraded in its provenance, so there is no "
                "elevation to subtract a surge height from. An inundation surface derived "
                "from a failed retrieval would be a picture of nothing"
            )

        config.ensure_dirs()
        source = record.path
        with rasterio.open(source) as handle:
            if handle.crs is None:
                raise ValueError(
                    f"{source.name} carries no CRS on disk, so which ground its cells "
                    "cover is not knowable and neither is where the water is"
                )
            elevation = handle.read(1).astype("float64")
            profile = handle.profile
            source_nodata = handle.nodata
            crs = handle.crs
            transform = handle.transform

        nodata = float(source_nodata) if source_nodata is not None else DERIVED_NODATA
        depth, wet, usable = self._bathtub(elevation, scenario.surge_height_m, source_nodata)

        evidence = SurfaceEvidence(
            scenario=scenario.name,
            surge_height_m=scenario.surge_height_m,
            source_raster=source.name,
            crs=crs.to_string(),
            cell_size_m=float(abs(transform.a)),
            cell_area_m2=float(abs(transform.a * transform.e)),
            cells=int(elevation.size),
            usable_cells=int(usable.sum()),
            wet_cells=int((wet == WET).sum()),
        )
        evidence.nodata_cells = (
            0 if source_nodata is None else int((elevation == source_nodata).sum())
        )
        evidence.non_finite_cells = int((~np.isfinite(elevation)).sum())
        evidence.at_threshold_cells = int(
            (usable & (elevation == scenario.surge_height_m)).sum()
        )
        if evidence.usable_cells:
            evidence.elevation_min_m = float(elevation[usable].min())
            evidence.elevation_max_m = float(elevation[usable].max())
            evidence.depth_max_m = float(depth[usable].max())

        stem = self._stem(scenario)
        evidence.depth_path = self._write(
            self.derived_dir / f"{stem}{DEPTH_SUFFIX}.tif", depth, profile, nodata
        )
        evidence.wet_path = self._write(
            self.derived_dir / f"{stem}{WET_SUFFIX}.tif", wet, profile, nodata
        )
        return HazardSurface(
            scenario=scenario,
            depth_path=evidence.depth_path,
            wet_path=evidence.wet_path,
            provenance=self._provenance(scenario, record.provenance, evidence),
            evidence=evidence,
        )

    def _stem(self, scenario: HazardScenario) -> str:
        """Filename stem for a scenario, with anything path-shaped removed."""
        cleaned = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in scenario.name
        )
        if not cleaned.strip("_"):
            raise ValueError(
                f"scenario name {scenario.name!r} leaves no usable filename stem; a "
                "scenario is written to disk under its own name"
            )
        return cleaned

    def _bathtub(
        self, elevation: np.ndarray, surge_height_m: float, source_nodata: float | None
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Depth, wet mask and usable mask for one surge height.

        Depth can exceed the surge height. Ground below the vertical datum has a
        negative elevation -- this county's raster reaches -16.42 m -- and
        `surge - (-16.42)` is deeper than the surge. That is the model behaving
        correctly, and it is why the depth bound asserted downstream is
        `surge - elev_min` and not `surge`.

        A cell that is nodata or not finite in the source is nodata in both
        outputs. It is not zero depth and it is not dry: the wet mask has to
        carry the hole too, or the fraction would be counted over a larger
        denominator than the depth mean and the two would disagree by exactly the
        number of holes.
        """
        usable = np.isfinite(elevation)
        if source_nodata is not None:
            usable &= elevation != source_nodata

        depth = np.where(usable, np.maximum(0.0, surge_height_m - elevation), np.nan)
        wet = np.where(usable, np.where(depth > 0.0, WET, DRY), np.nan)
        return depth, wet, usable

    def _write(
        self, path: Path, values: np.ndarray, profile: dict[str, Any], nodata: float
    ) -> Path:
        """Write one derived band, filling the holes with the nodata sentinel."""
        out = dict(profile)
        out.update(RASTER_PROFILE)
        out["nodata"] = nodata
        filled = np.where(np.isfinite(values), values, nodata).astype("float32")
        path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(path, "w", **out) as handle:
            handle.write(filled, 1)
        return path

    def _provenance(
        self, scenario: HazardScenario, source: Provenance, evidence: SurfaceEvidence
    ) -> Provenance:
        """Provenance for a computed layer, quoting the retrieval it came from.

        `source_url` is the elevation service's, because that is where the numbers
        in this raster came from; the transformation applied to them is in
        `request_params` and `notes`. Inventing a URL for a file this module wrote
        would be worse than quoting the one that is true.
        """
        return Provenance(
            dataset=f"inundation_{scenario.name}",
            source_url=source.source_url,
            retrieved_at=prov.utc_now(),
            declared_crs=evidence.crs,
            working_crs=self.working_crs,
            vintage=f"derived from {source.vintage}",
            feature_count=1,
            license=source.license,
            request_params={
                "model": "bathtub",
                "scenario": scenario.name,
                "surge_height_m": f"{scenario.surge_height_m:.4f}",
                "source_dataset": source.dataset,
                "source_raster": evidence.source_raster,
                "nodata": f"{DERIVED_NODATA:.1f}",
                "cells": str(evidence.cells),
                "usable_cells": str(evidence.usable_cells),
                "wet_cells": str(evidence.wet_cells),
                "cell_size_m": f"{evidence.cell_size_m:.4f}",
            },
            notes=(
                f"depth = max(0, {scenario.surge_height_m} - elevation) per cell",
                BATHTUB_NOTE,
                scenario.assumption_note,
                (
                    "nodata carried forward from the source raster: a cell with no "
                    "elevation has no depth, and is neither wet nor dry"
                ),
                (
                    "not registered in the snapshot registry: this layer was computed, "
                    "not retrieved, and align_snapshot measures every raster the registry "
                    "holds"
                ),
            ),
        )

    # -- measuring it per unit ----------------------------------------------

    def measure(
        self, surface: HazardSurface, polygons: Any, *, dataset: str = ""
    ) -> tuple[pd.DataFrame, MeasureEvidence]:
        """Inundated fraction, mean depth and max depth per polygon.

        Two zonal passes over two rasters that share one grid. The depth raster
        answers "how deep" and the wet raster answers "how much of it", and the
        cell counts they return must agree with each other unit by unit -- and,
        where the frame carries `Col.RASTER_CELLS` from the elevation pass in
        `align_snapshot`, with that too. Three passes over one grid returning
        three different denominators would mean the fraction and the mean were
        taken over different sets of cells.
        """
        if not isinstance(polygons, gpd.GeoDataFrame):
            raise TypeError(
                f"hazard.measure needs a GeoDataFrame of polygons, got "
                f"{type(polygons).__name__}"
            )
        label = dataset or "hazard polygons"
        frame = self.aligner.to_working_crs(polygons)

        depth, depth_zonal = self.aligner.zonal_stats_detailed(
            surface.depth_path,
            frame,
            stats=(*sorted(DEPTH_STAT_COLUMNS), "count"),
            dataset=f"{label}/{surface.scenario.name}{DEPTH_SUFFIX}",
        )
        wet, wet_zonal = self.aligner.zonal_stats_detailed(
            surface.wet_path,
            frame,
            stats=(*sorted(WET_STAT_COLUMNS), "count"),
            dataset=f"{label}/{surface.scenario.name}{WET_SUFFIX}",
        )

        evidence = MeasureEvidence(
            scenario=surface.scenario.name,
            dataset=label,
            polygons=len(frame),
            units_measured=int(depth["count"].gt(0).sum()),
            units_without_cells=int(depth["count"].eq(0).sum()),
        )
        self._agree_on_cells(depth, wet, frame, evidence, label)

        measured = pd.DataFrame(index=depth.index)
        measured[Col.INUNDATED_FRACTION] = wet["mean"]
        for statistic, column in DEPTH_STAT_COLUMNS.items():
            measured[column] = depth[statistic]

        fraction = measured[Col.INUNDATED_FRACTION]
        outside = fraction.notna() & ((fraction < 0.0) | (fraction > 1.0))
        if int(outside.sum()):
            raise ValueError(
                f"{label}: {int(outside.sum())} of {len(fraction)} unit(s) carry an "
                f"inundated fraction outside [0, 1], worst {float(fraction.abs().max())}. "
                "The mean of a raster holding only 0 and 1 cannot leave that interval, so "
                "the wet mask is not a mask"
            )
        depths = measured[Col.INUNDATION_MEAN_M]
        negative = depths.notna() & (depths < 0.0)
        if int(negative.sum()):
            raise ValueError(
                f"{label}: {int(negative.sum())} unit(s) carry a negative mean inundation "
                "depth; depth is clipped at zero per cell, so a negative mean means the "
                "clip did not run"
            )

        present = fraction.notna()
        evidence.units_fully_wet = int((present & (fraction >= 1.0)).sum())
        evidence.units_fully_dry = int((present & (fraction <= 0.0)).sum())
        if int(present.sum()):
            evidence.fraction_min = float(fraction[present].min())
            evidence.fraction_max = float(fraction[present].max())
        deepest = measured[Col.INUNDATION_MAX_M]
        if int(deepest.notna().sum()):
            evidence.deepest_unit = str(deepest.idxmax())
            evidence.deepest_m = float(deepest.max())
        if evidence.units_without_cells:
            evidence.warnings.append(
                f"{label}: {evidence.units_without_cells} of {evidence.polygons} unit(s) "
                f"contain no cell centre of {surface.depth_path.name} and carry no "
                "inundation value rather than a zero"
            )
        if depth_zonal.nodata_cells or wet_zonal.nodata_cells:
            evidence.warnings.append(
                f"{label}: {depth_zonal.nodata_cells} depth and "
                f"{wet_zonal.nodata_cells} wet-mask cell(s) inside these units were "
                "nodata and left out of every statistic; the elevation they came from "
                "was missing, which is not the same as flat ground"
            )
        return measured, evidence

    def _agree_on_cells(
        self,
        depth: pd.DataFrame,
        wet: pd.DataFrame,
        frame: gpd.GeoDataFrame,
        evidence: MeasureEvidence,
        label: str,
    ) -> None:
        """Refuse a measurement whose three passes disagree about the denominator.

        Per unit, not in total. Two units wrong in opposite directions leave a
        total that agrees exactly, which is the shape that got through the S7
        apportionment check.
        """
        evidence.cells_compared = len(depth)
        depth_counts = pd.to_numeric(depth["count"], errors="coerce")
        wet_counts = pd.to_numeric(wet["count"], errors="coerce")
        matched = depth_counts.eq(wet_counts).fillna(False)
        evidence.cells_matched = int(matched.sum())
        if evidence.cells_matched != evidence.cells_compared:
            disagreeing = int(evidence.cells_compared - evidence.cells_matched)
            raise ValueError(
                f"{label}: the depth and wet-mask rasters report different usable cell "
                f"counts for {disagreeing} of {evidence.cells_compared} unit(s). They are "
                "written from one elevation grid with one nodata mask, so the fraction and "
                "the mean depth would be taken over different cells"
            )

        if Col.RASTER_CELLS not in frame.columns:
            return
        published = pd.to_numeric(
            pd.Series(frame[Col.RASTER_CELLS].to_numpy(), index=depth.index),
            errors="coerce",
        )
        comparable = published.notna()
        evidence.elevation_cells_compared = int(comparable.sum())
        agree = published.eq(depth_counts).fillna(False) & comparable
        evidence.elevation_cells_matched = int(agree.sum())
        if evidence.elevation_cells_matched != evidence.elevation_cells_compared:
            disagreeing = int(
                evidence.elevation_cells_compared - evidence.elevation_cells_matched
            )
            raise ValueError(
                f"{label}: {disagreeing} of {evidence.elevation_cells_compared} unit(s) "
                f"cover a different number of derived cells than the {Col.RASTER_CELLS} "
                "the elevation pass recorded. The derived rasters copy the elevation "
                "grid, so a per-unit disagreement is a windowing or nodata fault"
            )

    def measure_all(
        self,
        polygons: Any,
        *,
        scenarios: tuple[HazardScenario, ...] = HAZARD_SCENARIOS,
        dataset: str = "",
        raster_name: str = acquire.DATASET_ELEVATION,
    ) -> HazardReport:
        """Derive and measure every scenario over one set of polygons."""
        if not scenarios:
            raise ValueError("hazard was asked to measure no scenario at all")
        names = [scenario.name for scenario in scenarios]
        if len(set(names)) != len(names):
            raise ValueError(
                f"two scenarios share a name in {names}; they would write the same "
                "raster and the second would silently overwrite the first"
            )

        report = HazardReport()
        for scenario in scenarios:
            surface = self.derive_surface(scenario, raster_name=raster_name)
            measured, evidence = self.measure(surface, polygons, dataset=dataset)
            report.surfaces[scenario.name] = surface
            report.frames[scenario.name] = measured
            report.measures.append(evidence)
            report.warnings.extend(evidence.warnings)
        report.vector_hazard = self.vector_hazard_status()
        report.warnings.extend(report.vector_hazard.values())
        return report

    def vector_hazard_status(self) -> dict[str, str]:
        """What the vector hazard layers contribute, and what they do not.

        Read through `align.is_degraded`, never through a row count. A flood-zone
        layer with no rows on an inland county means there is no mapped hazard
        there; the same empty layer on this county means FEMA's service answered
        HTTP 200 with an error body. Treating the second as the first would report
        a coastal county as having no regulatory flood risk.
        """
        status: dict[str, str] = {}
        for name in (acquire.DATASET_FLOOD_ZONES,):
            try:
                record = self.registry.record(name)
            except KeyError:
                status[name] = (
                    f"{name} is not in the registry at all, so this run's hazard is "
                    "elevation-only and nothing was checked for a mapped flood zone"
                )
                continue
            if align.is_degraded(record.provenance):
                status[name] = (
                    f"{name}: retrieval degraded, so the hazard in this table is the "
                    "bathtub over elevation ALONE. Its zero features are an absence of "
                    "data, not an absence of regulatory flood risk, and every inundated "
                    "fraction here is therefore unconstrained by the mapped floodplain"
                )
            elif record.provenance.feature_count == 0:
                status[name] = (
                    f"{name}: retrieved successfully and returned no feature, which for "
                    "this study area is a real absence of mapped flood zones rather than "
                    "a failed request"
                )
        return status


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def format_report(report: HazardReport, study_area: config.StudyArea) -> str:
    """The inundation table's denominators, printed beside its numerators."""
    lines = [
        f"HAZARD -- bathtub inundation for {study_area.name}",
        f"  working crs {study_area.working_crs}   model depth = max(0, surge - elevation)",
        "",
    ]
    for evidence in report.measures:
        surface = report.surfaces[evidence.scenario]
        grid = surface.evidence
        lines.append(
            f"  {evidence.scenario}  surge {grid.surge_height_m:.2f} m over "
            f"{grid.source_raster}"
        )
        lines.append(
            f"      grid          {grid.cells:,} cell(s) of {grid.cell_area_m2:,.1f} m2 = "
            f"{grid.usable_cells:,} usable + {grid.nodata_cells:,} nodata + "
            f"{grid.non_finite_cells:,} non-finite"
        )
        lines.append(
            f"                    elevation {grid.elevation_min_m:.2f} to "
            f"{grid.elevation_max_m:.2f} m; deepest cell {grid.depth_max_m:.2f} m, which "
            f"exceeds the surge wherever the ground sits below the datum"
        )
        lines.append(
            f"                    {grid.wet_cells:,} wet of {grid.usable_cells:,} usable "
            f"= {grid.wet_fraction:.4f} of the RETRIEVED EXTENT, most of which is ocean "
            "outside every census unit -- the county numbers are the per-unit ones below"
        )
        lines.append(
            f"                    {grid.at_threshold_cells:,} cell(s) sit exactly at the "
            "surge height and are counted dry"
        )
        lines.append(
            f"      per unit      {evidence.units_measured} of {evidence.polygons} unit(s) "
            f"measured, {evidence.units_without_cells} held no cell centre; fraction "
            f"{evidence.fraction_min:.4f} to {evidence.fraction_max:.4f}"
        )
        lines.append(
            f"                    {evidence.units_fully_wet} fully wet, "
            f"{evidence.units_fully_dry} fully dry; deepest unit "
            f"{evidence.deepest_unit or 'n/a'} at {evidence.deepest_m:.2f} m"
        )
        lines.append(
            f"      cell counts   depth vs wet agreed on {evidence.cells_matched} of "
            f"{evidence.cells_compared} unit(s); vs the elevation pass "
            f"{evidence.elevation_cells_matched} of {evidence.elevation_cells_compared}"
        )
        lines.append("")

    lines.append("  vector hazard layers:")
    for name, note in report.vector_hazard.items():
        lines.append(f"      {note}")
    if not report.vector_hazard:
        lines.append("      every configured vector hazard layer retrieved normally")

    lines.append("")
    lines.append(
        "  nodata handling is NOT exercised by this county: its elevation raster carries "
        "zero nodata and zero non-finite cells, so every count above is a real zero over a "
        "real denominator and none of them can discriminate. The rule that a hole stays a "
        "hole is proven on a synthetic raster in --check, and only there"
    )
    if report.warnings:
        lines.append("")
        lines.append(f"  warnings ({len(report.warnings)}):")
        for warning in report.warnings:
            lines.append(f"      - {warning}")
    return "\n".join(lines)


def main(area: config.StudyArea | None = None) -> int:
    """Derive every scenario and measure it over the joined tract layer."""
    registry = Registry(study_area=area)
    registry.load_manifest()
    aligner = Alignment(registry)
    snapshot = aligner.align_snapshot()
    key = f"{acquire.DATASET_TRACTS}{align.JOINED_SUFFIX}"
    if key not in snapshot.frames:
        print(f"no {key} layer in the snapshot; run `python -m src.acquire` first")
        return 1
    hazard = Hazard(aligner)
    report = hazard.measure_all(snapshot.frames[key], dataset=key)
    print(format_report(report, aligner.study_area))
    print()
    for scenario, frame in report.frames.items():
        print(f"  {scenario}: {len(frame)} rows x {len(frame.columns)} cols")
    return 0


# ---------------------------------------------------------------------------
# self check
# ---------------------------------------------------------------------------


FIXTURE_NODATA = -9999.0
FIXTURE_CELL = 100.0
FIXTURE_SIDE = 10


def _fixture_scenario(height: float, name: str = "fixture") -> HazardScenario:
    return HazardScenario(
        name=name,
        surge_height_m=height,
        source="synthetic fixture",
        assumption_note="a raster of values this file chose, so the answer is arithmetic",
    )


def _anchor(aligner: Alignment) -> tuple[float, float]:
    """A projected origin inside the working CRS's area of use."""
    bounds = align.CRS.from_user_input(aligner.working_crs).area_of_use
    point = gpd.GeoSeries(
        [align.Point((bounds.west + bounds.east) / 2, (bounds.south + bounds.north) / 2)],
        crs=config.STORAGE_CRS,
    )
    projected = aligner.to_working_crs(point).iloc[0]
    return float(projected.x), float(projected.y)


def _write_fixture(
    path: Path, values: np.ndarray, x0: float, y0: float, cell: float, crs: str
) -> Path:
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


def _bathtub_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """The depth arithmetic, on values chosen here so the answer is stated here.

    Every expected number below is arithmetic written into this function, not a
    number read off a run. That is what catches a clip applied in the wrong
    direction or a sentinel treated as ground.
    """
    hazard = Hazard(aligner)
    elevation = np.array(
        [[-2.0, 0.0, 1.0], [1.5, 3.0, 10.0], [FIXTURE_NODATA, np.nan, 4.0]],
        dtype="float64",
    )
    depth, wet, usable = hazard._bathtub(elevation, 1.5, FIXTURE_NODATA)

    expected_depth = np.array([[3.5, 1.5, 0.5], [0.0, 0.0, 0.0], [np.nan, np.nan, np.nan]])
    expected_wet = np.array([[1.0, 1.0, 1.0], [0.0, 0.0, 0.0], [np.nan, np.nan, np.nan]])

    print("bathtub fixture: a 3x3 elevation grid, surge 1.5 m")
    print(f"  elevation      {elevation.tolist()}")
    print(f"  depth          {np.round(depth, 4).tolist()}   (expected {expected_depth.tolist()})")
    print(f"  wet mask       {wet.tolist()}   (expected {expected_wet.tolist()})")
    print(
        f"  usable {int(usable.sum())} of {usable.size}; the -9999 sentinel and the nan "
        "are neither wet nor dry"
    )

    finite = np.isfinite(expected_depth)
    deeper = hazard._bathtub(elevation, 5.0, FIXTURE_NODATA)[0]
    zero_surge = hazard._bathtub(elevation, 0.0, FIXTURE_NODATA)

    return [
        ("depth is max(0, surge - elevation) cell by cell",
         bool(np.allclose(depth[finite], expected_depth[finite]))),
        ("the wet mask holds exactly 1 where depth is positive and 0 where it is not",
         bool(np.array_equal(wet[finite], expected_wet[finite]))),
        ("ground above the surge is dry rather than negatively deep",
         bool(depth[1][2] == 0.0 and wet[1][2] == DRY)),
        ("a cell exactly at the surge height is dry, not wet",
         bool(depth[1][0] == 0.0 and wet[1][0] == DRY)),
        ("ground below the datum is deeper than the surge, not clipped to it",
         bool(depth[0][0] == 3.5 and depth[0][0] > 1.5)),
        ("the nodata sentinel is carried forward, not treated as ground at -9999 m",
         bool(np.isnan(depth[2][0]) and np.isnan(wet[2][0]) and not usable[2][0])),
        ("a non-finite cell is carried forward the same way",
         bool(np.isnan(depth[2][1]) and np.isnan(wet[2][1]) and not usable[2][1])),
        ("the hole is a hole in BOTH outputs, so the two share one denominator",
         int(np.isfinite(depth).sum()) == int(np.isfinite(wet).sum()) == int(usable.sum())),
        ("a deeper surge floods at least every cell a shallower one did",
         bool(np.all(deeper[finite] >= depth[finite]))),
        ("a zero surge still floods ground below the datum, and nothing above it",
         bool(zero_surge[0][0][0] == 2.0 and zero_surge[1][0][1] == DRY)),
        ("a negative surge height is refused rather than reported as a dry county",
         verify.refuses(
             lambda: hazard.derive_surface(_fixture_scenario(-1.0)),
             ValueError,
             "not a lower flood",
         )),
    ]


def _fixture_registry(root: Path, raster: Path, working_crs: str) -> Registry:
    """A registry holding one synthetic elevation raster and nothing else.

    `derive_surface` reaches its raster through `Registry.path_of`, so exercising
    it end to end needs a registry rather than a path. Building one here is what
    makes the on-disk round trip -- nodata written, nodata read back -- a test of
    the real code path instead of of a hand-assembled `HazardSurface`.
    """
    registry = Registry(manifest_path=root / "manifest.json", root=root)
    registry.register(
        acquire.DATASET_ELEVATION,
        "raster",
        raster,
        Provenance(
            dataset=acquire.DATASET_ELEVATION,
            source_url="https://example.invalid/fixture",
            retrieved_at=prov.utc_now(),
            declared_crs=working_crs,
            working_crs=working_crs,
            vintage="synthetic fixture",
            feature_count=1,
            license="synthetic fixture",
        ),
    )
    return registry


def _surface_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """Derive a surface end to end from a synthetic raster, and measure it.

    Every expected number is arithmetic stated here. The fixture is a 10x10 grid
    whose elevation equals its column index, so at a surge of 3.5 m columns 0 to
    3 are wet and columns 4 to 9 are dry, and a polygon covering the top three
    whole rows must return 12/30 wet, a mean depth of 24/30 and a deepest cell of
    3.5 m.

    This is also the ONLY place nodata reaches the raster path. The county's
    elevation raster carries zero nodata and zero non-finite cells, so the rule
    that a hole stays a hole cannot be exercised on real data by any amount of
    checking -- it is a property of the county, not of the code.
    """
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="hazard_check_"))
    x0, y0 = _anchor(aligner)
    cell, side = FIXTURE_CELL, FIXTURE_SIDE
    elevation = np.tile(np.arange(side, dtype="float64"), (side, 1))
    elevation[5][5] = FIXTURE_NODATA
    elevation[5][6] = np.nan
    raster = _write_fixture(root / "elev.tif", elevation, x0, y0, cell, aligner.working_crs)

    registry = _fixture_registry(root, raster, aligner.working_crs)
    hazard = Hazard(Alignment(registry))
    surge = 3.5
    scenario = _fixture_scenario(surge, "fixturesurface")
    surface = hazard.derive_surface(scenario)

    depth, wet, usable = hazard._bathtub(elevation, surge, FIXTURE_NODATA)
    with rasterio.open(surface.depth_path) as handle:
        depth_back = handle.read(1).astype("float64")
        depth_nodata = handle.nodata
        depth_crs = handle.crs
    with rasterio.open(surface.wet_path) as handle:
        wet_back = handle.read(1).astype("float64")

    holes = ~np.isfinite(depth)
    kept = np.isfinite(depth)

    top = y0 + side * cell
    wide = align.Polygon(
        [
            (x0 + 1, top - 3 * cell + 1),
            (x0 + side * cell - 1, top - 3 * cell + 1),
            (x0 + side * cell - 1, top - 1),
            (x0 + 1, top - 1),
        ]
    )
    polygons = gpd.GeoDataFrame(
        {Col.GEOID: ["01001000100"]}, geometry=[wide], crs=aligner.working_crs
    )
    measured, evidence = hazard.measure(surface, polygons, dataset="fixture")
    row = measured.loc["01001000100"]

    print(
        f"surface fixture: {side}x{side}, elevation = column index, surge {surge} m, "
        f"one nodata cell and one nan cell"
    )
    print(
        f"  derived to {surface.depth_path.name} and {surface.wet_path.name}; "
        f"{int(holes.sum())} hole(s) written as {depth_nodata}"
    )
    print(
        f"  grid: {surface.evidence.cells} cell(s) = {surface.evidence.usable_cells} usable "
        f"+ {surface.evidence.nodata_cells} nodata + {surface.evidence.non_finite_cells} "
        f"non-finite; {surface.evidence.wet_cells} wet   (expected 100 = 98 + 1 + 1, 39 wet)"
    )
    print(
        f"  three whole rows -> fraction {float(row[Col.INUNDATED_FRACTION]):.6f} "
        f"mean {float(row[Col.INUNDATION_MEAN_M]):.6f} "
        f"max {float(row[Col.INUNDATION_MAX_M]):.6f}   "
        f"(expected 12/30 = 0.400000, 24/30 = 0.800000, 3.500000)"
    )
    print(
        f"  cell counts: depth vs wet agreed on {evidence.cells_matched} of "
        f"{evidence.cells_compared} unit(s)"
    )

    def rewritten(name: str, values: np.ndarray, crs: str | None = None) -> HazardSurface:
        """A surface with one raster deliberately replaced by a wrong one."""
        replaced = _write_fixture(
            root / name, values, x0, y0, cell, crs or aligner.working_crs
        )
        return HazardSurface(
            scenario=scenario,
            depth_path=replaced if name.startswith("d") else surface.depth_path,
            wet_path=replaced if name.startswith("w") else surface.wet_path,
            provenance=surface.provenance,
            evidence=SurfaceEvidence(),
        )

    torn_wet = wet.copy()
    torn_wet[0][0] = FIXTURE_NODATA
    torn = rewritten("w_torn.tif", torn_wet)
    unmasked = rewritten("w_big.tif", np.where(np.isfinite(wet), 7.0, np.nan))
    mismatched = rewritten("d_crs.tif", depth, crs=config.STORAGE_CRS)
    labelled = polygons.copy()
    labelled[Col.RASTER_CELLS] = [29]

    def band(first: int, last: int) -> Any:
        """A polygon covering whole grid rows `first` up to but not including `last`."""
        return align.Polygon(
            [
                (x0 + 1, top - last * cell + 1),
                (x0 + side * cell - 1, top - last * cell + 1),
                (x0 + side * cell - 1, top - first * cell - 1),
                (x0 + 1, top - first * cell - 1),
            ]
        )

    # Two units whose cell counts disagree in OPPOSITE directions: the depth
    # raster loses a cell in the second band and the wet raster loses one in the
    # first, so the county totals agree exactly while neither unit does. This is
    # the shape the S7 review showed a total-based comparison cannot see, and the
    # single-polygon `torn` fixture above cannot express it -- with one unit a
    # total and a per-unit check are the same check.
    cancelling_depth = depth.copy()
    cancelling_depth[2][0] = np.nan
    cancelling_wet = wet.copy()
    cancelling_wet[0][0] = np.nan
    cancelling = HazardSurface(
        scenario=scenario,
        depth_path=_write_fixture(
            root / "d_cancel.tif", cancelling_depth, x0, y0, cell, aligner.working_crs
        ),
        wet_path=_write_fixture(
            root / "w_cancel.tif", cancelling_wet, x0, y0, cell, aligner.working_crs
        ),
        provenance=surface.provenance,
        evidence=SurfaceEvidence(),
    )
    two_bands = gpd.GeoDataFrame(
        {Col.GEOID: ["01001000101", "01001000102"]},
        geometry=[band(0, 2), band(2, 4)],
        crs=aligner.working_crs,
    )
    intact, intact_evidence = hazard.measure(surface, two_bands, dataset="fixture")
    print(
        f"  cancelling fixture: two 20-cell bands, one cell lost from the depth raster "
        f"in the second and one from the wet raster in the first -- 39 = 39 in total, "
        f"20 != 19 and 19 != 20 per unit"
    )

    wet_holes = np.isfinite(wet_back) & (wet_back != DERIVED_NODATA)
    expected_wet_cells = int((elevation < surge).sum() - 1)

    return [
        ("the whole grid is accounted for as usable, nodata or non-finite",
         surface.evidence.cells
         == surface.evidence.usable_cells
         + surface.evidence.nodata_cells
         + surface.evidence.non_finite_cells
         == side * side),
        ("the fixture really does contain a hole, so the nodata rule was exercised",
         surface.evidence.nodata_cells == 1
         and surface.evidence.non_finite_cells == 1
         and surface.evidence.usable_cells == side * side - 2),
        ("the county-wide wet count is the stated arithmetic, holes excluded",
         surface.evidence.wet_cells == expected_wet_cells),
        ("a hole written to disk reads back as nodata in the depth raster",
         bool(depth_nodata == DERIVED_NODATA
              and np.all(depth_back[holes] == DERIVED_NODATA)
              and int(holes.sum()) == 2)),
        ("a hole is a hole in the wet mask too, not a dry cell",
         bool(np.all(wet_back[holes] == DERIVED_NODATA))),
        ("every cell that was not a hole survives the round trip unchanged",
         bool(np.allclose(depth_back[kept], depth[kept]))),
        ("the wet mask on disk holds only zero, one, or the nodata sentinel",
         bool(np.all(np.isin(wet_back[wet_holes], (DRY, WET))))),
        ("the derived raster keeps the source CRS, which zonal_stats requires",
         depth_crs is not None
         and align.CRS.from_user_input(depth_crs)
         == align.CRS.from_user_input(aligner.working_crs)),
        ("the derived provenance names the raster and the surge it came from",
         surface.provenance.request_params.get("surge_height_m") == f"{surge:.4f}"
         and surface.provenance.request_params.get("source_raster") == raster.name
         and surface.provenance.source_url.endswith("fixture")),
        ("the inundated fraction is the wet mask's mean, to the cell",
         bool(abs(float(row[Col.INUNDATED_FRACTION]) - 12 / 30) < 1e-9)),
        ("the mean depth is the stated arithmetic over all cells, dry ones included",
         bool(abs(float(row[Col.INUNDATION_MEAN_M]) - 24 / 30) < 1e-9)),
        ("the deepest cell is the surge less the lowest ground in the unit",
         bool(abs(float(row[Col.INUNDATION_MAX_M]) - 3.5) < 1e-9)),
        ("both rasters reported the same usable cell count for every unit",
         evidence.cells_matched == evidence.cells_compared == 1),
        ("a raster in the wrong CRS is refused rather than warped",
         verify.refuses(
             lambda: hazard.measure(mismatched, polygons, dataset="fixture"),
             ValueError,
             "Warping it here would resample",
         )),
        ("two rasters that disagree on one unit's cells are refused, not averaged over",
         verify.refuses(
             lambda: hazard.measure(torn, polygons, dataset="fixture"),
             ValueError,
             "different usable cell counts",
         )),
        ("two units disagreeing in opposite directions are refused, though the totals agree",
         verify.refuses(
             lambda: hazard.measure(cancelling, two_bands, dataset="fixture"),
             ValueError,
             "different usable cell counts",
         )),
        ("the same two units on intact rasters agree, so that refusal discriminates",
         intact_evidence.cells_matched == intact_evidence.cells_compared == 2
         and len(intact) == 2),
        ("a wet mask holding values above one is caught by the [0, 1] bound",
         verify.refuses(
             lambda: hazard.measure(unmasked, polygons, dataset="fixture"),
             ValueError,
             "inundated fraction outside [0, 1]",
         )),
        ("a frame whose recorded elevation cell count disagrees is refused",
         verify.refuses(
             lambda: hazard.measure(surface, labelled, dataset="fixture"),
             ValueError,
             f"different number of derived cells than the {Col.RASTER_CELLS}",
         )),
        ("a scenario name that leaves no filename is refused",
         verify.refuses(
             lambda: hazard._stem(_fixture_scenario(1.0, "///")),
             ValueError,
             "no usable filename stem",
         )),
    ]


def _independent_fraction(
    raster_path: Path, geometry: Any, surge_height_m: float
) -> dict[str, float]:
    """Bathtub one polygon straight off the ELEVATION raster, by hand.

    Never touches `Hazard`. A window read generously and padded rather than
    fitted, cell centres built from the transform, shapely asked one point at a
    time, and the depth arithmetic applied here. Slow, and that is fine for one
    polygon. This is the only number in the module that was not produced by the
    code under test.
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
            [geometry.contains(align.Point(float(xs[r, c]), float(ys[r, c]))) for c in range(cols)]
            for r in range(rows)
        ]
    )
    picked = block[inside].astype("float64")
    picked = picked[np.isfinite(picked)]
    if nodata is not None:
        picked = picked[picked != nodata]
    if not picked.size:
        return {"count": 0.0, "fraction": float("nan"), "mean": float("nan"), "max": float("nan")}
    depths = np.maximum(0.0, surge_height_m - picked)
    return {
        "count": float(picked.size),
        "fraction": float((depths > 0.0).mean()),
        "mean": float(depths.mean()),
        "max": float(depths.max()),
    }


def _county_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """Measure the real county, and check it against things it did not compute.

    Three independent anchors, none of which is a previous run:

      * one tract bathtubbed by hand off the elevation raster, cell centre by
        cell centre, never calling `Hazard`;
      * the deepest cell per unit against `Col.ELEV_MIN_M`, which `align.py`
        measured in a separate zonal pass -- the bathtub makes the deepest cell
        `max(0, surge - elev_min)` by definition, so the two must agree exactly;
      * the fully DRY units against the same column, since a unit is fully dry
        exactly when its lowest ground clears the surge. The fully WET side needs
        the highest ground, which `Col` does not publish, so it is checked
        against `Col.ELEV_MEAN_M` instead: a unit with every cell wet has a mean
        depth of exactly `surge - elev_mean`.

    Two of those comparisons can be vacuous on a given county -- a set of dry
    units can be empty, and so can the set of fully wet ones. Both counts are
    printed, and a zero is called out where it is printed rather than left to
    read as agreement.
    """
    snapshot = aligner.align_snapshot()
    key = f"{acquire.DATASET_TRACTS}{align.JOINED_SUFFIX}"
    tracts = snapshot.frames[key]
    hazard = Hazard(aligner)
    scenario = HAZARD_SCENARIOS[1]
    surface = hazard.derive_surface(scenario)
    measured, evidence = hazard.measure(surface, tracts, dataset=key)

    elevation_path = aligner.registry.path_of(acquire.DATASET_ELEVATION)
    sample = tracts.iloc[0]
    expected = _independent_fraction(
        elevation_path, sample.geometry, scenario.surge_height_m
    )
    got = measured.loc[str(sample[Col.GEOID])]

    print(
        f"county check: {len(tracts)} tract(s), scenario {scenario.name} at "
        f"{scenario.surge_height_m} m over {elevation_path.name}"
    )
    print(
        f"  one tract, bathtubbed by hand off the elevation raster: "
        f"count {expected['count']:.0f} fraction {expected['fraction']:.6f} "
        f"mean {expected['mean']:.6f} max {expected['max']:.6f}"
    )
    print(
        f"  the same tract through hazard.measure:                  "
        f"count {float(sample[Col.RASTER_CELLS]):.0f} "
        f"fraction {float(got[Col.INUNDATED_FRACTION]):.6f} "
        f"mean {float(got[Col.INUNDATION_MEAN_M]):.6f} "
        f"max {float(got[Col.INUNDATION_MAX_M]):.6f}"
    )

    elev_min = pd.to_numeric(
        pd.Series(tracts[Col.ELEV_MIN_M].to_numpy(), index=measured.index), errors="coerce"
    )
    deepest = pd.to_numeric(measured[Col.INUNDATION_MAX_M], errors="coerce")
    predicted = (scenario.surge_height_m - elev_min).clip(lower=0.0)
    comparable = elev_min.notna() & deepest.notna()
    depth_agrees = int((np.abs(deepest - predicted) < 1e-4)[comparable].sum())

    fraction = pd.to_numeric(measured[Col.INUNDATED_FRACTION], errors="coerce")
    dry = comparable & (fraction <= 0.0)
    above = comparable & (elev_min >= scenario.surge_height_m)
    dry_agrees = bool((dry == above)[comparable].all())

    fully_wet = comparable & (fraction >= 1.0)
    wet_mean = pd.to_numeric(measured[Col.INUNDATION_MEAN_M], errors="coerce")
    elev_mean = pd.to_numeric(
        pd.Series(tracts[Col.ELEV_MEAN_M].to_numpy(), index=measured.index), errors="coerce"
    )
    mean_predicted = scenario.surge_height_m - elev_mean
    mean_agrees = int(
        (np.abs(wet_mean - mean_predicted) < 1e-3)[fully_wet].sum()
    )

    print(
        f"  deepest cell vs {Col.ELEV_MIN_M} from the separate elevation pass: "
        f"{depth_agrees} of {int(comparable.sum())} unit(s) agree to 1e-4 m"
    )
    print(
        f"  fully dry units vs {Col.ELEV_MIN_M} >= surge: {int(dry.sum())} and "
        f"{int(above.sum())} unit(s); the sets are "
        f"{'identical' if dry_agrees else 'DIFFERENT'}"
    )
    print(
        f"  fully wet units where mean depth must be surge - {Col.ELEV_MEAN_M}: "
        f"{mean_agrees} of {int(fully_wet.sum())} agree to 1e-3 m"
    )
    if int(fully_wet.sum()) == 0:
        print(
            "    no unit is fully wet at this surge height, so that identity was not "
            "exercised on this county and the check below only asserts that"
        )
    if int(dry.sum()) == 0:
        print(
            "    no unit is fully dry at this surge height, so the dry-set comparison "
            "above is two empty sets agreeing and discriminates nothing"
        )

    return [
        ("one tract's inundated fraction matches a hand computation off the elevation raster",
         bool(abs(float(got[Col.INUNDATED_FRACTION]) - expected["fraction"]) < 1e-9)),
        ("its mean depth matches that hand computation",
         bool(abs(float(got[Col.INUNDATION_MEAN_M]) - expected["mean"]) < 1e-6)),
        ("its deepest cell matches that hand computation",
         bool(abs(float(got[Col.INUNDATION_MAX_M]) - expected["max"]) < 1e-6)),
        ("the hand computation and the elevation pass counted the same cells",
         int(expected["count"]) == int(sample[Col.RASTER_CELLS])),
        (f"every unit's deepest cell equals surge - {Col.ELEV_MIN_M}, unit by unit",
         depth_agrees == int(comparable.sum()) and int(comparable.sum()) == len(tracts)),
        ("the fully dry units are exactly those whose lowest ground clears the surge",
         dry_agrees),
        ("every fully wet unit's mean depth is surge minus its mean elevation",
         mean_agrees == int(fully_wet.sum())),
        ("every unit was measured, none silently dropped",
         len(measured) == len(tracts) and int(comparable.sum()) == len(tracts)),
        ("the cell counts agreed with the elevation pass for every unit",
         evidence.elevation_cells_matched == evidence.elevation_cells_compared == len(tracts)),
        ("every inundated fraction lies in [0, 1]",
         bool(((fraction >= 0.0) & (fraction <= 1.0))[fraction.notna()].all()
              and int(fraction.notna().sum()) == len(tracts))),
    ]


def _degradation_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """A failed flood-zone retrieval must not read as an absence of flooding."""
    hazard = Hazard(aligner)
    status = hazard.vector_hazard_status()
    record = aligner.registry.record(acquire.DATASET_FLOOD_ZONES)
    degraded = align.is_degraded(record.provenance)

    print(f"flood zones: {record.provenance.feature_count} feature(s), degraded={degraded}")
    for name, note in status.items():
        print(f"  {name} -> {note[:100]}...")

    # On this county the flag and the row count agree -- flood_zones is degraded
    # AND holds zero features -- so nothing here can tell the two rules apart. The
    # fixtures below prise them apart: a layer that retrieved cleanly and found
    # nothing, and a raster that failed to retrieve at all. Neither exists in this
    # snapshot, and without them "read the flag, not the count" is unfalsifiable.
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="hazard_degraded_"))
    honest = Registry(manifest_path=root / "manifest.json", root=root)
    honest.register(
        acquire.DATASET_FLOOD_ZONES,
        "vector",
        record.path,
        dataclasses.replace(record.provenance, request_params={}, feature_count=0),
    )
    absent = Hazard(Alignment(honest)).vector_hazard_status()

    elevation = aligner.registry.record(acquire.DATASET_ELEVATION)
    broken = Registry(manifest_path=root / "broken.json", root=root)
    broken.register(
        acquire.DATASET_ELEVATION,
        "raster",
        elevation.path,
        dataclasses.replace(
            elevation.provenance,
            request_params={**elevation.provenance.request_params, align.DEGRADED_KEY: "true"},
        ),
    )
    print(
        f"  fixture, retrieved cleanly with 0 features -> "
        f"{absent[acquire.DATASET_FLOOD_ZONES][:78]}..."
    )

    return [
        ("the degraded flood layer is reported as missing data, not as missing hazard",
         acquire.DATASET_FLOOD_ZONES in status
         and "absence of data, not an absence of regulatory flood risk"
         in status[acquire.DATASET_FLOOD_ZONES]),
        ("this run really is on a degraded flood layer, so that branch was taken",
         degraded and record.provenance.feature_count == 0),
        ("degradation is read from the provenance flag, never from the row count",
         align.is_degraded(record.provenance)
         != align.is_degraded(
             dataclasses.replace(record.provenance, request_params={})
         )),
        ("a layer that retrieved cleanly and found nothing is a real absence, not a failure",
         "real absence of mapped flood zones" in absent.get(acquire.DATASET_FLOOD_ZONES, "")
         and "absence of data" not in absent.get(acquire.DATASET_FLOOD_ZONES, "")),
        ("that fixture has the same zero row count as the degraded one, so only the flag differs",
         honest.provenance_of(acquire.DATASET_FLOOD_ZONES).feature_count
         == record.provenance.feature_count == 0
         and not align.is_degraded(honest.provenance_of(acquire.DATASET_FLOOD_ZONES))),
        ("deriving a surface from a non-raster dataset is refused",
         verify.refuses(
             lambda: Hazard(aligner).derive_surface(
                 HAZARD_SCENARIOS[0], raster_name=acquire.DATASET_FLOOD_ZONES
             ),
             ValueError,
             "is a vector dataset",
         )),
        ("deriving a surface from a raster whose retrieval failed is refused as degraded",
         verify.refuses(
             lambda: Hazard(Alignment(broken)).derive_surface(HAZARD_SCENARIOS[0]),
             ValueError,
             "flagged degraded in its provenance",
         )),
    ]


def _contract_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """The column map, the scenario set and the module discipline scans."""
    module = sys.modules[__name__]
    published = {
        Col.INUNDATED_FRACTION,
        Col.INUNDATION_MEAN_M,
        Col.INUNDATION_MAX_M,
    }
    mapped = set(DEPTH_STAT_COLUMNS.values()) | set(WET_STAT_COLUMNS.values())
    unsupported = [
        name
        for name in (*DEPTH_STAT_COLUMNS, *WET_STAT_COLUMNS)
        if name not in align.ZONAL_STATISTICS
    ]
    heights = [scenario.surge_height_m for scenario in HAZARD_SCENARIOS]

    print(f"  inundation columns mapped: {sorted(mapped)}")
    print(f"  statistics requested: {sorted({*DEPTH_STAT_COLUMNS, *WET_STAT_COLUMNS})}")
    print(f"  scenarios: {[s.name for s in HAZARD_SCENARIOS]} at {heights} m")

    checks = [
        ("every inundation column contracts.Col publishes is filled by this module",
         mapped == published),
        (f"nothing here remaps a statistic onto {Col.RASTER_CELLS}, which align already fills",
         Col.RASTER_CELLS not in mapped),
        ("every statistic asked of zonal_stats is one it computes",
         not unsupported),
        ("at least three scenarios are defined",
         len(HAZARD_SCENARIOS) >= 3),
        ("every scenario names a source and a stated assumption",
         all(s.source and s.assumption_note for s in HAZARD_SCENARIOS)
         and len(HAZARD_SCENARIOS) > 0),
        ("every scenario's assumption says the height is an input rather than a forecast",
         all("input" in s.assumption_note.lower() for s in HAZARD_SCENARIOS)
         and len(HAZARD_SCENARIOS) > 0),
        ("the scenario heights are distinct and increasing",
         heights == sorted(set(heights)) and len(heights) == len(HAZARD_SCENARIOS)),
        ("scenario names are unique, so no surface overwrites another",
         len({s.name for s in HAZARD_SCENARIOS}) == len(HAZARD_SCENARIOS)),
        ("measuring no scenario at all is refused",
         verify.refuses(
             lambda: Hazard(aligner).measure_all(
                 gpd.GeoDataFrame(
                     {Col.GEOID: []}, geometry=[], crs=aligner.working_crs
                 ),
                 scenarios=(),
             ),
             ValueError,
             "no scenario at all",
         )),
        ("two scenarios sharing a name are refused before either is written",
         verify.refuses(
             lambda: Hazard(aligner).measure_all(
                 gpd.GeoDataFrame(
                     {Col.GEOID: []}, geometry=[], crs=aligner.working_crs
                 ),
                 scenarios=(_fixture_scenario(1.0, "twin"), _fixture_scenario(2.0, "twin")),
             ),
             ValueError,
             "share a name",
         )),
        ("a non-GeoDataFrame is refused rather than measured",
         verify.refuses(
             lambda: Hazard(aligner).measure(
                 HazardSurface(
                     scenario=_fixture_scenario(1.0),
                     depth_path=Path(),
                     wet_path=Path(),
                     provenance=aligner.registry.provenance_of(acquire.DATASET_ELEVATION),
                     evidence=SurfaceEvidence(),
                 ),
                 pd.DataFrame({Col.GEOID: ["1"]}),
             ),
             TypeError,
             "needs a GeoDataFrame",
         )),
    ]
    return checks + verify.discipline_checks(module)


def _self_check() -> int:
    registry = Registry()
    registry.load_manifest()
    aligner = Alignment(registry)

    print(f"study area: {aligner.study_area.name}   working crs: {aligner.working_crs}")
    print(f"snapshot:   {len(registry.names())} manifest entries\n")

    checks = _bathtub_checks(aligner)
    print()
    checks += _surface_checks(aligner)
    print()
    checks += _county_checks(aligner)
    print()
    checks += _degradation_checks(aligner)
    print()
    checks += _contract_checks(aligner)
    return verify.report(checks)


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
