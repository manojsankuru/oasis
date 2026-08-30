"""Hazard, exposure, vulnerability and resilience -- reported apart, then combined.

Criterion SG says a single collapsed number without its components is a failure.
So every table this module produces carries the four objective terms as their own
columns, in the units they were measured in, before any score exists; the score is
one more column beside them, never instead of them.

Five things this module is built around.

* **The four components are incommensurable, and the score says so.** A fraction
  of flooded land, a count of exposed people, and two dimensionless indices
  cannot be added without asserting an exchange rate between a person and a metre
  of water. So each is percentile-ranked before it is weighted, following the
  published rule the presets cite, and the raw value stays in the table beside
  the rank. The cost of that choice is stated where it is made: a percentile rank
  is county-relative, so it answers "who here is worst", never "is here bad". A
  county with no inundation anywhere still produces a full ranking, and
  `Col.INUNDATED_FRACTION` sitting in the next column is what stops that reading
  as danger.

* **Exposure is computed at the finer granularity and rolled up.** Multiplying a
  tract's population by its flooded fraction assumes people are spread evenly
  across the tract, which on a coastal county puts residents in the marsh. Doing
  it per block group and summing to tracts uses the finest population the Census
  publishes. Both numbers are produced, `align.apportion` compares them unit by
  unit, and the disagreement is reported as a result -- it is the granularity
  sensitivity Track A names, not an error to tune away.

* **Resilience is what a unit can reach.** The count of hospitals, schools, fire
  stations and community centres within a stated radius, ranked. Straight-line,
  because network travel time is Track B's named challenge and is cut. The
  facility tag is READ from the retrieval's own provenance rather than written
  here, so a run that asked for different amenities counts the ones it asked for.

* **Resilience enters the score through the protective branch.** More of it means
  less risk, so it is ranked with `MORE_IS_BETTER` and the unit with the most
  reachable facilities contributes least. That is the same helper the
  vulnerability indicators use, with the direction stated rather than smuggled
  into a minus sign.

* **A unit missing any component is unscored.** Not zero, not a partial average
  over the components it has. Counted, named, and excluded from every top-ten
  list with the count printed beside the list.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd

from . import acquire, align, config, verify, vulnerability
from .align import Alignment
from .contracts import Col, HazardScenario, ScenarioRow, WeightPreset
from .hazard import HAZARD_SCENARIOS, Hazard, HazardSurface
from .registry import Registry
from .vulnerability import (
    MORE_IS_BETTER,
    PRESETS,
    MORE_IS_WORSE,
    OBJECTIVE_TERMS,
    WEIGHT_PRESETS,
    Vulnerability,
    normalised_weights,
    percentile_rank,
)

COMPONENT_DIRECTION: dict[str, int] = {
    Col.INUNDATED_FRACTION: MORE_IS_WORSE,
    Col.EXPOSED_POPULATION: MORE_IS_WORSE,
    Col.VULNERABILITY: MORE_IS_WORSE,
    Col.RESILIENCE: MORE_IS_BETTER,
}
"""Which way each objective term pushes the risk score.

Three raise it and one lowers it. Resilience is the reason this map exists as
data rather than as a minus sign somewhere in the arithmetic: a reader can
disagree with the claim that reachable facilities reduce risk, and a claim that
can be disagreed with has to be written down where it can be found."""

COMPONENT_RATIONALE: dict[str, str] = {
    Col.INUNDATED_FRACTION: (
        "the share of the unit's land the modelled surge covers -- the hazard "
        "itself, in the units it was measured in, before anyone is counted"
    ),
    Col.EXPOSED_POPULATION: (
        "how many residents live on that flooded land, summed from block groups "
        "rather than assumed spread evenly across the tract"
    ),
    Col.VULNERABILITY: (
        "how hard it is for those residents to leave and to recover, from the "
        "weighted percentile index over the five indicators"
    ),
    Col.RESILIENCE: (
        "how many hospitals, schools, fire stations and community centres the unit "
        "can reach, which is the only capacity this snapshot can measure"
    ),
}
"""One sentence per component, for the same reason the indicators have them. A
score whose terms cannot each be explained in a sentence is a score nobody can
argue with."""

DEFAULT_FACILITY_RADIUS_M = 5000.0
"""How far a unit is credited with reaching, straight-line, as an ARGUMENT with
this default rather than a constant in a function body.

Five kilometres is roughly a long walk or a short drive on an uncongested road.
It is not a travel time and this module never calls it one: network travel time
is Track B's named challenge and is cut from this build, so the radius is a
stated parameter whose sensitivity a caller can sweep."""

FACILITY_TAG_PARAM = "tags"
"""The key `acquire.fetch_osm` writes its requested tag filter under, in the
facilities `Provenance.request_params`. The tag KEY -- "amenity" on this run --
is read back from there rather than written here, so a snapshot retrieved with a
different filter is counted by the filter it was retrieved with. Hardcoding
"amenity" would be exactly the retrieved-value-written-by-hand that invariant 1
forbids."""

UNIFORM_SUFFIX = "_uniform"
"""How the coarse-granularity exposure estimate is named in the evidence, derived
from `Col.EXPOSED_POPULATION` rather than written as a literal, following the
`align.JOINED_SUFFIX` precedent. It never becomes a column in the reported table;
the table carries the finer estimate under the name `Col` publishes."""

DEFAULT_PRIORITY_UNITS = 10
DEFAULT_VULNERABLE_QUANTILE = 0.5
"""How long a priority list is, and where "vulnerable" starts on it. Both are
arguments with these defaults. The median is a choice, not a threshold anybody
published, and `ScenarioRow.vulnerable_population_in_priority` is only readable
beside the number that produced it."""


@dataclass(slots=True)
class ResilienceEvidence:
    """What the facility count counted, and what it could not."""

    dataset: str = ""
    radius_m: float = 0.0
    tag_key: str = ""
    tag_source: str = ""
    facilities: int = 0
    facilities_placed: int = 0
    units: int = 0
    units_with_none: int = 0
    matches: int = 0
    min_count: int = 0
    median_count: float = 0.0
    max_count: int = 0
    by_tag: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExposureEvidence:
    """The two exposure estimates and how far apart they are.

    `apportionment_error` here is not a defect count. It is how much the answer
    moves when the same question is asked at block-group rather than tract
    granularity, which is a result worth reporting rather than a discrepancy to
    tune away.
    """

    scenario: str = ""
    fine: str = ""
    coarse: str = ""
    fine_units: int = 0
    coarse_units: int = 0
    fine_total: float = 0.0
    coarse_total: float = 0.0
    population_total: float = 0.0
    max_abs_difference: float = 0.0
    relative_error_pct: float = float("nan")
    units_compared: int = 0
    units_undefined: int = 0
    units_suppressed: int = 0
    units_over_population: int = 0
    method_note: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RiskEvidence:
    """What the combined score was built from, beside the score."""

    scenario: str = ""
    preset: str = ""
    origin: str = ""
    origin_url: str = ""
    dataset: str = ""
    units: int = 0
    units_scored: int = 0
    units_unscored: int = 0
    unscored_geoids: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    weights: dict[str, float] = field(default_factory=dict)
    directions: dict[str, int] = field(default_factory=dict)
    component_published: dict[str, int] = field(default_factory=dict)
    rank_denominator: dict[str, int] = field(default_factory=dict)
    score_min: float = float("nan")
    score_max: float = float("nan")
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RiskTable:
    """One scenario's tract table: components, score, rank, and the evidence."""

    scenario: HazardScenario
    frame: pd.DataFrame
    risk: RiskEvidence
    exposure: ExposureEvidence
    resilience: ResilienceEvidence
    vulnerability: vulnerability.IndexEvidence

    @property
    def warnings(self) -> list[str]:
        return [
            *self.resilience.warnings,
            *self.exposure.warnings,
            *self.vulnerability.warnings,
            *self.risk.warnings,
        ]


class Risk:
    """Builds the four components and combines them under a named weighting."""

    def __init__(
        self,
        aligner: Alignment | None = None,
        hazard: Hazard | None = None,
        index: Vulnerability | None = None,
    ) -> None:
        self.aligner = aligner or Alignment()
        self.hazard = hazard or Hazard(self.aligner)
        self.index = index or Vulnerability()
        self.registry: Registry = self.aligner.registry

    # -- resilience: what a unit can reach ----------------------------------

    def facility_tags(self) -> tuple[str, str]:
        """The OSM tag key this snapshot's facilities were retrieved under.

        Read from the retrieval's provenance, never written here. A run that asked
        Overpass for `emergency=*` instead of `amenity=*` gets its facilities
        counted and grouped by the key it asked for, with no edit to this module.
        """
        record = self.registry.record(acquire.DATASET_FACILITIES)
        raw = record.provenance.request_params.get(FACILITY_TAG_PARAM, "")
        try:
            tags = json.loads(raw)
        except (TypeError, ValueError):
            tags = {}
        keys = [key for key in tags if isinstance(key, str)]
        if not keys:
            return "", (
                f"the facilities provenance carries no readable {FACILITY_TAG_PARAM!r}, "
                f"so the count below is of every retrieved feature and is not broken down"
            )
        return keys[0], f"read from {acquire.DATASET_FACILITIES} provenance: {raw}"

    def resilience(
        self,
        units: Any,
        facilities: Any,
        *,
        radius_m: float = DEFAULT_FACILITY_RADIUS_M,
        dataset: str = "",
    ) -> tuple[pd.Series, ResilienceEvidence]:
        """Percentile rank of the critical facilities within `radius_m` of each unit.

        Both frames are routed through `to_working_crs` first. Invariant 2 is not
        theoretical here: buffering a frame in a geographic CRS would buffer by
        five thousand DEGREES, silently, and return a count that looks like a
        count.

        The rank is ascending on the count, so the unit reaching the most
        facilities scores 1 and is the most resilient. The direction is applied
        later, where the score is combined, so this column reads the way its name
        does.
        """
        if radius_m <= 0:
            raise ValueError(
                f"a resilience radius of {radius_m} m credits every unit with reaching "
                "nothing outside itself; the radius is a parameter and has to be positive"
            )
        label = dataset or "units"
        placed = self.aligner.to_working_crs(units)
        points = self.aligner.to_working_crs(facilities)
        if Col.GEOID not in placed.columns:
            raise KeyError(
                f"{label}: no {Col.GEOID} column to count facilities against; the frame "
                f"carries {list(placed.columns)[:10]}"
            )

        tag_key, tag_source = self.facility_tags()
        evidence = ResilienceEvidence(
            dataset=label,
            radius_m=float(radius_m),
            tag_key=tag_key,
            tag_source=tag_source,
            facilities=len(points),
            units=len(placed),
        )

        reach = placed[[Col.GEOID]].copy()
        reach = gpd.GeoDataFrame(
            reach, geometry=placed.geometry.buffer(radius_m), crs=placed.crs
        )
        usable = points[points.geometry.notna() & ~points.geometry.is_empty]
        evidence.facilities_placed = len(usable)
        if evidence.facilities_placed < evidence.facilities:
            evidence.warnings.append(
                f"{label}: {evidence.facilities - evidence.facilities_placed} of "
                f"{evidence.facilities} facility(ies) carry no usable geometry and were "
                "not counted for any unit"
            )

        counts = pd.Series(0, index=placed.index, dtype="int64")
        if evidence.facilities_placed and len(reach):
            joined = gpd.sjoin(
                usable[[usable.geometry.name] + ([tag_key] if tag_key in usable.columns else [])],
                reach,
                how="inner",
                predicate="intersects",
            )
            evidence.matches = len(joined)
            found = joined.groupby("index_right").size()
            counts = counts.add(found.reindex(placed.index).fillna(0), fill_value=0).astype("int64")
            if tag_key and tag_key in joined.columns:
                evidence.by_tag = {
                    str(key): int(value)
                    for key, value in joined[tag_key].value_counts().items()
                }

        evidence.min_count = int(counts.min()) if len(counts) else 0
        evidence.max_count = int(counts.max()) if len(counts) else 0
        evidence.median_count = float(np.median(counts)) if len(counts) else 0.0
        evidence.units_with_none = int((counts == 0).sum())
        if evidence.units_with_none:
            evidence.warnings.append(
                f"{label}: {evidence.units_with_none} of {evidence.units} unit(s) reach no "
                f"facility within {radius_m:,.0f} m and rank lowest on resilience; that is "
                "a measured zero, not a missing value"
            )
        if evidence.max_count == evidence.min_count:
            evidence.warnings.append(
                f"{label}: every unit reaches exactly {evidence.max_count} facility(ies) "
                f"within {radius_m:,.0f} m, so the resilience rank is the same for all of "
                "them and contributes nothing to the ranking at any weight"
            )

        ranked = percentile_rank(counts, direction=MORE_IS_WORSE)
        ranked.index = placed.index
        ranked.name = Col.RESILIENCE
        return ranked, evidence

    # -- exposure: who lives on the flooded land ----------------------------

    def exposure(
        self,
        surface: HazardSurface,
        tracts: Any,
        block_groups: Any,
        *,
        tract_name: str = "",
        group_name: str = "",
    ) -> tuple[pd.Series, ExposureEvidence]:
        """Exposed population per tract, summed from block groups.

        Two estimates of one quantity. The coarse one multiplies each tract's
        population by its own flooded fraction; the fine one does the same per
        block group and sums. They differ whenever population is not spread evenly
        inside a tract, which on a coastal county it never is, and `align.apportion`
        measures that difference unit by unit rather than in total -- a total can
        agree while individual units are wrong in opposite directions.

        The finer estimate is the one returned. A tract whose block-group
        population sums exactly to its published population -- which the Census
        controls it to do -- cannot report more exposed residents than it has,
        because every block group contributes at most its own population.
        """
        fine_label = group_name or "block groups"
        coarse_label = tract_name or "tracts"
        tract_hazard, _ = self.hazard.measure(surface, tracts, dataset=coarse_label)
        group_hazard, _ = self.hazard.measure(surface, block_groups, dataset=fine_label)

        fine = pd.DataFrame(
            {
                Col.GEOID: pd.Series(block_groups[Col.GEOID].to_numpy(), dtype="string"),
                Col.POPULATION: pd.to_numeric(
                    block_groups[Col.POPULATION], errors="coerce"
                ).to_numpy(),
                Col.INUNDATED_FRACTION: group_hazard[Col.INUNDATED_FRACTION].to_numpy(),
            }
        )
        fine[Col.EXPOSED_POPULATION] = (
            fine[Col.POPULATION] * fine[Col.INUNDATED_FRACTION]
        )
        coarse = pd.DataFrame(
            {
                Col.GEOID: pd.Series(tracts[Col.GEOID].to_numpy(), dtype="string"),
                Col.POPULATION: pd.to_numeric(
                    tracts[Col.POPULATION], errors="coerce"
                ).to_numpy(),
                Col.INUNDATED_FRACTION: tract_hazard[Col.INUNDATED_FRACTION].to_numpy(),
            }
        )
        coarse[Col.EXPOSED_POPULATION] = (
            coarse[Col.POPULATION] * coarse[Col.INUNDATED_FRACTION]
        )

        aggregated, apportioned = self.aligner.apportion_detailed(
            fine,
            coarse,
            [Col.EXPOSED_POPULATION],
            method="sum",
            fine_name=fine_label,
            coarse_name=coarse_label,
        )
        exposed = pd.to_numeric(
            aggregated[Col.EXPOSED_POPULATION].reindex(coarse[Col.GEOID].to_numpy()),
            errors="coerce",
        ).astype("Float64")
        exposed.index = tracts.index
        exposed.name = Col.EXPOSED_POPULATION

        population = pd.to_numeric(tracts[Col.POPULATION], errors="coerce")
        population.index = tracts.index
        over = exposed.notna() & population.notna() & (exposed > population + 1e-6)
        evidence = ExposureEvidence(
            scenario=surface.scenario.name,
            fine=fine_label,
            coarse=coarse_label,
            fine_units=len(fine),
            coarse_units=len(coarse),
            fine_total=float(exposed.dropna().sum()),
            coarse_total=float(coarse[Col.EXPOSED_POPULATION].dropna().sum()),
            population_total=float(population.dropna().sum()),
            max_abs_difference=float(
                apportioned.max_abs_difference.get(Col.EXPOSED_POPULATION, 0.0)
            ),
            relative_error_pct=float(
                apportioned.error.get(Col.EXPOSED_POPULATION, float("nan"))
            ),
            units_compared=int(
                apportioned.units_compared.get(Col.EXPOSED_POPULATION, 0)
            ),
            units_undefined=int(
                apportioned.units_undefined.get(Col.EXPOSED_POPULATION, 0)
            ),
            units_suppressed=int(apportioned.incomplete.get(Col.EXPOSED_POPULATION, 0)),
            units_over_population=int(over.sum()),
            method_note=apportioned.method_note,
        )
        if evidence.units_over_population:
            raise ValueError(
                f"{coarse_label}: {evidence.units_over_population} tract(s) report more "
                "exposed residents than residents. Block-group population is controlled "
                "to sum to the tract estimate and every block group contributes at most "
                "its own population, so this cannot happen unless the rollup attached a "
                "child to the wrong parent"
            )
        if evidence.units_suppressed:
            evidence.warnings.append(
                f"{coarse_label}: {evidence.units_suppressed} tract(s) carry no exposed "
                "population because a block group inside them carries none; a partial sum "
                "would read as a smaller tract rather than as a suppression"
            )
        if evidence.units_undefined:
            evidence.warnings.append(
                f"{coarse_label}: {evidence.units_undefined} tract(s) publish a "
                f"{Col.EXPOSED_POPULATION}{UNIFORM_SUFFIX} of zero, so the granularity "
                "difference for them is the absolute one above and not a percentage"
            )
        evidence.warnings.append(
            f"exposure granularity: summing block groups gives "
            f"{evidence.fine_total:,.0f} exposed resident(s) against "
            f"{evidence.coarse_total:,.0f} from the tract-uniform assumption, worst unit "
            f"{evidence.max_abs_difference:,.0f} resident(s). That gap is a result about "
            "granularity, not an error either estimate made"
        )
        return exposed, evidence

    # -- the four components, then the score --------------------------------

    def components(
        self,
        surface: HazardSurface,
        tracts: Any,
        block_groups: Any,
        facilities: Any,
        *,
        preset: WeightPreset = vulnerability.DEFAULT_PRESET,
        radius_m: float = DEFAULT_FACILITY_RADIUS_M,
        dataset: str = "",
    ) -> tuple[pd.DataFrame, ExposureEvidence, ResilienceEvidence, vulnerability.IndexEvidence]:
        """Build the four objective terms as their own columns, before any score."""
        label = dataset or "tracts"
        measured, _ = self.hazard.measure(surface, tracts, dataset=label)
        exposed, exposure = self.exposure(
            surface, tracts, block_groups, tract_name=label, group_name="block groups"
        )
        resilient, resilience = self.resilience(
            tracts, facilities, radius_m=radius_m, dataset=label
        )
        scored, index = self.index.index(tracts, preset=preset, dataset=label)

        frame = pd.DataFrame(index=tracts.index)
        frame[Col.GEOID] = pd.Series(tracts[Col.GEOID].to_numpy(), index=tracts.index).astype("string")
        frame[Col.POPULATION] = pd.to_numeric(
            pd.Series(tracts[Col.POPULATION].to_numpy(), index=tracts.index), errors="coerce"
        )
        for name in vulnerability.VULNERABILITY_INDICATORS:
            frame[name] = pd.to_numeric(
                pd.Series(tracts[name].to_numpy(), index=tracts.index), errors="coerce"
            )
        for column in (Col.INUNDATED_FRACTION, Col.INUNDATION_MEAN_M, Col.INUNDATION_MAX_M):
            frame[column] = pd.Series(measured[column].to_numpy(), index=tracts.index)
        frame[Col.EXPOSED_POPULATION] = exposed
        frame[Col.VULNERABILITY] = scored
        frame[Col.RESILIENCE] = resilient
        return frame, exposure, resilience, index

    def combine(
        self,
        frame: pd.DataFrame,
        *,
        scenario: HazardScenario,
        preset: WeightPreset = vulnerability.DEFAULT_PRESET,
        weights: dict[str, float] | None = None,
        components: tuple[str, ...] = OBJECTIVE_TERMS,
        dataset: str = "",
    ) -> tuple[pd.DataFrame, RiskEvidence]:
        """Rank each component, weight the ranks, and rank the result.

        The components keep their own columns untouched; this adds two more.
        Ranking first is what makes the weights comparable -- on raw values a
        weight on a quantity with tiny variance does nothing, and "the ranking
        changed when I changed a weight" would be a statement about scale rather
        than about preference.

        A unit missing any component is not scored. Averaging three ranks against
        four produces a number on the same scale as the others that answers a
        different question, which is the most plausible-looking way to be wrong
        here.
        """
        absent = [name for name in components if name not in frame.columns]
        if absent:
            raise KeyError(
                f"the frame carries no column for {absent}; the risk score is taken over "
                f"{list(components)} and every one has to be there as its own column first"
            )
        chosen = normalised_weights(
            dict(weights if weights is not None else preset.weights), components
        )
        out = frame.copy()
        ranks = pd.DataFrame(index=frame.index)
        denominators: dict[str, int] = {}
        for name in components:
            values = pd.to_numeric(frame[name], errors="coerce").astype("Float64")
            denominators[name] = int(values.notna().sum())
            ranks[name] = percentile_rank(values, direction=COMPONENT_DIRECTION[name])

        complete = ranks.notna().all(axis=1)
        score = sum(ranks[name] * chosen[name] for name in components)
        out[Col.RISK_SCORE] = pd.Series(score, index=frame.index, dtype="Float64").where(complete)
        out[Col.PRIORITY_RANK] = (
            out[Col.RISK_SCORE].rank(ascending=False, method="min").astype("Int64")
        )

        geoids = out[Col.GEOID].astype("string") if Col.GEOID in out.columns else pd.Series(
            out.index, index=out.index, dtype="string"
        )
        evidence = RiskEvidence(
            scenario=scenario.name,
            preset=preset.name,
            origin=preset.origin,
            origin_url=preset.origin_url,
            dataset=dataset or "tracts",
            units=len(frame),
            units_scored=int(complete.sum()),
            units_unscored=int((~complete).sum()),
            unscored_geoids=tuple(sorted(geoids[~complete].dropna().tolist())),
            components=tuple(components),
            weights=chosen,
            directions={name: COMPONENT_DIRECTION[name] for name in components},
            rank_denominator=denominators,
            component_published={
                name: int(pd.to_numeric(frame[name], errors="coerce").notna().sum())
                for name in components
            },
        )
        if evidence.units_scored:
            evidence.score_min = float(out[Col.RISK_SCORE].min())
            evidence.score_max = float(out[Col.RISK_SCORE].max())
        if evidence.units_unscored:
            evidence.warnings.append(
                f"{evidence.dataset}: {evidence.units_unscored} of {evidence.units} unit(s) "
                "carry no risk score because at least one component is undefined for them, "
                f"and every priority list omits them: {list(evidence.unscored_geoids)[:6]}"
            )
        evidence.warnings.append(
            "every component is percentile-ranked before it is weighted, so this score is "
            "COUNTY-RELATIVE: it says which units here are worst, never whether here is "
            f"bad. The absolute hazard is in {Col.INUNDATED_FRACTION} beside it, and on a "
            "county with no inundation at all this column would still rank every unit"
        )
        return out, evidence

    def table(
        self,
        surface: HazardSurface,
        tracts: Any,
        block_groups: Any,
        facilities: Any,
        *,
        preset: WeightPreset = vulnerability.DEFAULT_PRESET,
        radius_m: float = DEFAULT_FACILITY_RADIUS_M,
        dataset: str = "",
    ) -> RiskTable:
        """Components and score for one scenario under one preset."""
        frame, exposure, resilience, index = self.components(
            surface,
            tracts,
            block_groups,
            facilities,
            preset=preset,
            radius_m=radius_m,
            dataset=dataset,
        )
        combined, risk = self.combine(
            frame, scenario=surface.scenario, preset=preset, dataset=dataset
        )
        return RiskTable(
            scenario=surface.scenario,
            frame=combined,
            risk=risk,
            exposure=exposure,
            resilience=resilience,
            vulnerability=index,
        )

    # -- the trade-off table, including who loses ---------------------------

    def compare_presets(
        self,
        frame: pd.DataFrame,
        *,
        scenario: HazardScenario,
        units: Any = None,
        presets: tuple[WeightPreset, ...] = WEIGHT_PRESETS,
        priority_units: int = DEFAULT_PRIORITY_UNITS,
        vulnerable_quantile: float = DEFAULT_VULNERABLE_QUANTILE,
    ) -> list[ScenarioRow]:
        """One row per weighting: who it prioritises, and who it drops.

        `displaced_geoids` is the criterion-SG question asked directly. For each
        preset it holds the units that some OTHER preset in this comparison puts
        in its priority list and this one does not -- the people whose priority
        depends on a value judgement rather than on the data. A trade-off table
        without that column reports three answers and hides the disagreement
        between them.

        **`units` is what makes the comparison cover a whole preset.** A preset
        carries two halves: weights over the five indicators, which decide
        `Col.VULNERABILITY`, and weights over the four objective terms, which
        decide how that column trades off against the other three. The frame
        arrives with a vulnerability index already computed under ONE preset, so
        without the units that produced it only the objective half can vary --
        and two presets differing only in their indicator weights would return
        byte-identical rows while appearing to have been compared.

        `svi_equal` and `svi_themes` are exactly that pair: same published source,
        same objective weights, different published rule for the indicators. Pass
        `units` -- the joined layer the frame was built from -- and the index is
        recomputed per preset, which is the only way those two ever disagree.
        Omitting it is supported for a frame that carries no indicator columns,
        such as a fixture, and the rows then compare the objective half alone.
        """
        if priority_units <= 0:
            raise ValueError(
                f"a priority list of {priority_units} unit(s) prioritises nobody; the "
                "length is a parameter and has to be positive"
            )
        if not 0.0 <= vulnerable_quantile <= 1.0:
            raise ValueError(
                f"the vulnerable quantile is {vulnerable_quantile}, outside [0, 1]; it is "
                "the point on the county's own vulnerability distribution above which a "
                "resident is counted vulnerable"
            )
        if not presets:
            raise ValueError("compare_presets was given no weighting to compare")

        chosen: dict[str, list[str]] = {}
        scored: dict[str, pd.DataFrame] = {}
        for preset in presets:
            under = frame
            if units is not None:
                index, _ = self.index.index(units, preset=preset)
                under = frame.copy()
                under[Col.VULNERABILITY] = pd.Series(
                    index.to_numpy(), index=frame.index, dtype="Float64"
                )
            combined, _ = self.combine(under, scenario=scenario, preset=preset)
            ordered = combined.dropna(subset=[Col.RISK_SCORE]).sort_values(
                Col.RISK_SCORE, ascending=False
            )
            chosen[preset.name] = ordered[Col.GEOID].head(priority_units).tolist()
            scored[preset.name] = combined

        rows: list[ScenarioRow] = []
        for preset in presets:
            top = chosen[preset.name]
            combined = scored[preset.name]
            picked = combined[combined[Col.GEOID].isin(top)]
            population = pd.to_numeric(picked[Col.POPULATION], errors="coerce")
            vulnerable_cut = pd.to_numeric(
                combined[Col.VULNERABILITY], errors="coerce"
            ).quantile(vulnerable_quantile)
            vulnerable = pd.to_numeric(picked[Col.VULNERABILITY], errors="coerce")
            elsewhere = {
                geoid
                for name, other in chosen.items()
                if name != preset.name
                for geoid in other
            }
            rows.append(
                ScenarioRow(
                    preset=preset.name,
                    scenario=scenario.name,
                    top_geoids=tuple(top),
                    population_in_priority=int(population.fillna(0).sum()),
                    vulnerable_population_in_priority=int(
                        population.where(vulnerable >= vulnerable_cut).fillna(0).sum()
                    ),
                    mean_inundation_m=float(
                        pd.to_numeric(picked[Col.INUNDATION_MEAN_M], errors="coerce").mean()
                    ),
                    displaced_geoids=tuple(sorted(elsewhere - set(top))),
                )
            )
        return rows


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def format_components(table: RiskTable) -> str:
    """The four objective terms, side by side, before any score."""
    lines = [
        f"RISK COMPONENTS -- {table.scenario.name} at {table.scenario.surge_height_m} m, "
        f"preset {table.risk.preset} ({table.risk.origin})",
        "",
    ]
    for name in table.risk.components:
        lines.append(
            f"  {name:<22} weight {table.risk.weights[name]:.4f}  "
            f"direction {table.risk.directions[name]:+d}  "
            f"published {table.risk.component_published[name]} of {table.risk.units}, "
            f"ranked over {table.risk.rank_denominator[name]}"
        )
        lines.append(f"      why  {COMPONENT_RATIONALE[name]}")
    lines.append("")
    lines.append(
        f"  exposure   {table.exposure.fine_total:,.0f} resident(s) summed from "
        f"{table.exposure.fine_units} {table.exposure.fine}, against "
        f"{table.exposure.coarse_total:,.0f} from the tract-uniform assumption over "
        f"{table.exposure.coarse_units} {table.exposure.coarse}"
    )
    lines.append(
        f"             worst unit differs by {table.exposure.max_abs_difference:,.0f} "
        f"resident(s) = {table.exposure.relative_error_pct:.2f}% over "
        f"{table.exposure.units_compared} comparable unit(s), "
        f"{table.exposure.units_undefined} publishing zero; county population "
        f"{table.exposure.population_total:,.0f}"
    )
    lines.append(f"             {table.exposure.method_note}")
    lines.append(
        f"  resilience {table.resilience.facilities_placed} of "
        f"{table.resilience.facilities} facility(ies) within "
        f"{table.resilience.radius_m:,.0f} m; per unit min {table.resilience.min_count}, "
        f"median {table.resilience.median_count:,.1f}, max {table.resilience.max_count}; "
        f"{table.resilience.units_with_none} unit(s) reach none"
    )
    lines.append(f"             tag key {table.resilience.tag_key!r} {table.resilience.tag_source}")
    if table.resilience.by_tag:
        lines.append(f"             matches by tag {table.resilience.by_tag}")
    lines.append(
        f"  vulnerability {table.vulnerability.units_scored} of "
        f"{table.vulnerability.units} unit(s) scored, "
        f"{table.vulnerability.units_unscored} unscored"
    )
    lines.append("")
    lines.append(
        f"  score      {table.risk.units_scored} of {table.risk.units} unit(s) scored, "
        f"{table.risk.units_unscored} unscored; "
        f"{table.risk.score_min:.4f} to {table.risk.score_max:.4f}"
    )
    if table.risk.origin_url:
        lines.append(f"             weights from {table.risk.origin_url}")
    return "\n".join(lines)


def format_table(table: RiskTable, rows: int = 10) -> str:
    """The top of the real table, with the components beside the score."""
    frame = table.frame.dropna(subset=[Col.RISK_SCORE]).sort_values(Col.RISK_SCORE, ascending=False)
    header = (
        f"  {'rank':>4}  {'geoid':<12} {'pop':>7} {'flooded':>8} {'exposed':>8} "
        f"{'meandep':>8} {'vuln':>6} {'resil':>6} {'score':>6}"
    )
    lines = [f"PRIORITY TABLE -- top {rows} of {len(frame)} scored unit(s)", header]
    for _, row in frame.head(rows).iterrows():
        lines.append(
            f"  {int(row[Col.PRIORITY_RANK]):>4}  {row[Col.GEOID]:<12} "
            f"{float(row[Col.POPULATION]):>7,.0f} "
            f"{float(row[Col.INUNDATED_FRACTION]):>8.4f} "
            f"{float(row[Col.EXPOSED_POPULATION]):>8,.0f} "
            f"{float(row[Col.INUNDATION_MEAN_M]):>8.3f} "
            f"{float(row[Col.VULNERABILITY]):>6.3f} "
            f"{float(row[Col.RESILIENCE]):>6.3f} "
            f"{float(row[Col.RISK_SCORE]):>6.3f}"
        )
    if table.risk.unscored_geoids:
        lines.append(
            f"  {len(table.risk.unscored_geoids)} unit(s) are absent from this list "
            f"because a component is undefined for them: {list(table.risk.unscored_geoids)}"
        )
    return "\n".join(lines)


def format_tradeoff(rows: list[ScenarioRow]) -> str:
    """The trade-off table, including who loses under each weighting."""
    lines = [
        "TRADE-OFF -- the same county under three weightings, and who each one drops",
        f"  {'preset':<22} {'scenario':<12} {'pop':>9} {'vuln pop':>9} {'mean m':>7}  displaced",
    ]
    for row in rows:
        lines.append(
            f"  {row.preset:<22} {row.scenario:<12} "
            f"{row.population_in_priority:>9,} "
            f"{row.vulnerable_population_in_priority:>9,} "
            f"{row.mean_inundation_m:>7.3f}  {len(row.displaced_geoids)} unit(s)"
        )
    lines.append("")
    for row in rows:
        lines.append(f"  {row.preset} prioritises {list(row.top_geoids)}")
        lines.append(
            f"      and does NOT prioritise {list(row.displaced_geoids)}, which another "
            "weighting in this table does -- those are the units whose priority depends on "
            "a value judgement rather than on the data"
        )
    return "\n".join(lines)


def _layers(aligner: Alignment) -> tuple[Any, Any, Any, str]:
    """The three cleaned layers every risk table is built from."""
    snapshot = aligner.align_snapshot()
    tract_key = f"{acquire.DATASET_TRACTS}{align.JOINED_SUFFIX}"
    group_key = f"{acquire.DATASET_BLOCK_GROUPS}{align.JOINED_SUFFIX}"
    for key in (tract_key, group_key, acquire.DATASET_FACILITIES):
        if key not in snapshot.frames:
            raise KeyError(
                f"no {key} layer in the snapshot; run `python -m src.acquire` first. "
                f"The snapshot holds {sorted(snapshot.frames)}"
            )
    return (
        snapshot.frames[tract_key],
        snapshot.frames[group_key],
        snapshot.frames[acquire.DATASET_FACILITIES],
        tract_key,
    )


def main(area: config.StudyArea | None = None) -> int:
    """Build the real tract table for the middle scenario and print it."""
    registry = Registry(study_area=area)
    registry.load_manifest()
    aligner = Alignment(registry)
    tracts, groups, facilities, key = _layers(aligner)

    risk = Risk(aligner)
    scenario = HAZARD_SCENARIOS[len(HAZARD_SCENARIOS) // 2]
    surface = risk.hazard.derive_surface(scenario)
    table = risk.table(surface, tracts, groups, facilities, dataset=key)

    print(format_components(table))
    print()
    print(format_table(table))
    print()
    print(
        format_tradeoff(
            risk.compare_presets(table.frame, scenario=scenario, units=tracts)
        )
    )
    if table.warnings:
        print()
        print(f"  warnings ({len(table.warnings)}):")
        for warning in table.warnings:
            print(f"      - {warning}")
    return 0


# ---------------------------------------------------------------------------
# self check
# ---------------------------------------------------------------------------


def _component_fixture() -> pd.DataFrame:
    """Four units whose component ranks can be read off by eye."""
    return pd.DataFrame(
        {
            Col.GEOID: ["01001000100", "01001000200", "01001000300", "01001000400"],
            Col.POPULATION: [1000.0, 2000.0, 3000.0, 4000.0],
            Col.INUNDATION_MEAN_M: [0.1, 0.2, 0.3, 0.4],
            Col.INUNDATED_FRACTION: [0.1, 0.2, 0.3, 0.4],
            Col.EXPOSED_POPULATION: [100.0, 400.0, 900.0, 1600.0],
            Col.VULNERABILITY: [0.9, 0.7, 0.5, 0.3],
            Col.RESILIENCE: [0.25, 0.5, 0.75, 1.0],
        }
    )


def _combine_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """The score is a weighted mean of ranks, and resilience pushes the other way."""
    risk = Risk(aligner)
    frame = _component_fixture()
    scenario = HAZARD_SCENARIOS[0]

    hazard_only = {name: (1.0 if name == Col.INUNDATED_FRACTION else 0.0) for name in OBJECTIVE_TERMS}
    resilience_only = {name: (1.0 if name == Col.RESILIENCE else 0.0) for name in OBJECTIVE_TERMS}
    vulnerable_only = {name: (1.0 if name == Col.VULNERABILITY else 0.0) for name in OBJECTIVE_TERMS}

    by_hazard, _ = risk.combine(frame, scenario=scenario, weights=hazard_only)
    by_resilience, _ = risk.combine(frame, scenario=scenario, weights=resilience_only)
    by_vulnerability, evidence = risk.combine(frame, scenario=scenario, weights=vulnerable_only)
    equal, equal_evidence = risk.combine(frame, scenario=scenario)

    print("combine fixture: four units, hazard and resilience rising, vulnerability falling")
    print(f"  hazard only       -> {[round(float(v), 4) for v in by_hazard[Col.RISK_SCORE]]}   (expected 0.25 0.5 0.75 1.0)")
    print(f"  resilience only   -> {[round(float(v), 4) for v in by_resilience[Col.RISK_SCORE]]}   (expected 1.0 0.75 0.5 0.25: more resilience is LESS risk)")
    print(f"  vulnerability only-> {[round(float(v), 4) for v in by_vulnerability[Col.RISK_SCORE]]}   (expected 1.0 0.75 0.5 0.25)")
    print(f"  equal weights     -> {[round(float(v), 4) for v in equal[Col.RISK_SCORE]]}")
    print(f"  priority rank     -> {[int(v) for v in by_hazard[Col.PRIORITY_RANK]]}   (expected 4 3 2 1)")

    holed = frame.copy()
    holed.loc[2, Col.RESILIENCE] = np.nan
    partial, partial_evidence = risk.combine(holed, scenario=scenario)

    return [
        ("weighting hazard alone reproduces the hazard ranking",
         [round(float(v), 6) for v in by_hazard[Col.RISK_SCORE]] == [0.25, 0.5, 0.75, 1.0]),
        ("weighting vulnerability alone reproduces the vulnerability ranking",
         [round(float(v), 6) for v in by_vulnerability[Col.RISK_SCORE]] == [1.0, 0.75, 0.5, 0.25]),
        ("resilience lowers the score: the unit reaching most facilities ranks least risky",
         [round(float(v), 6) for v in by_resilience[Col.RISK_SCORE]] == [1.0, 0.75, 0.5, 0.25]),
        ("resilience really does enter through the protective direction",
         COMPONENT_DIRECTION[Col.RESILIENCE] == MORE_IS_BETTER
         and all(COMPONENT_DIRECTION[name] == MORE_IS_WORSE
                 for name in OBJECTIVE_TERMS if name != Col.RESILIENCE)),
        ("the hazard and resilience weightings order the county oppositely",
         [round(float(v), 6) for v in by_hazard[Col.RISK_SCORE]]
         == [round(float(v), 6) for v in by_resilience[Col.RISK_SCORE]][::-1]),
        ("priority rank 1 is the highest score, not the lowest",
         int(by_hazard[Col.PRIORITY_RANK].iloc[3]) == 1
         and int(by_hazard[Col.PRIORITY_RANK].iloc[0]) == 4),
        ("every score lies in [0, 1]",
         bool(((equal[Col.RISK_SCORE] >= 0) & (equal[Col.RISK_SCORE] <= 1)).all())),
        ("the four components survive into the table beside the score",
         all(name in equal.columns for name in OBJECTIVE_TERMS)
         and Col.RISK_SCORE in equal.columns
         and len(OBJECTIVE_TERMS) == 4),
        ("a unit missing one component is unscored rather than scored on the other three",
         bool(pd.isna(partial[Col.RISK_SCORE].iloc[2]))
         and partial_evidence.units_scored == 3
         and partial_evidence.units_unscored == 1),
        ("the unscored unit is named",
         partial_evidence.unscored_geoids == ("01001000300",)),
        ("the complete frame leaves nobody unscored, so that branch discriminates",
         evidence.units_unscored == 0 and equal_evidence.units_scored == 4),
        ("a frame missing a component column is refused",
         verify.refuses(
             lambda: risk.combine(
                 frame.drop(columns=[Col.RESILIENCE]), scenario=scenario
             ),
             KeyError,
             "has to be there as its own column first",
         )),
    ]


def _tradeoff_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """The weights move the ranking, and the table says who that costs."""
    risk = Risk(aligner)
    frame = _component_fixture()
    scenario = HAZARD_SCENARIOS[0]
    rows = risk.compare_presets(frame, scenario=scenario, priority_units=2)

    hazard_heavy = WeightPreset(
        name="hazard_heavy",
        weights={
            **{name: 0.2 for name in vulnerability.VULNERABILITY_INDICATORS},
            Col.INUNDATED_FRACTION: 1.0,
            Col.EXPOSED_POPULATION: 0.0,
            Col.VULNERABILITY: 0.0,
            Col.RESILIENCE: 0.0,
        },
        origin="user",
        origin_note="a fixture weighting that puts everything on the hazard",
    )
    resilience_heavy = WeightPreset(
        name="resilience_heavy",
        weights={
            **{name: 0.2 for name in vulnerability.VULNERABILITY_INDICATORS},
            Col.INUNDATED_FRACTION: 0.0,
            Col.EXPOSED_POPULATION: 0.0,
            Col.VULNERABILITY: 0.0,
            Col.RESILIENCE: 1.0,
        },
        origin="user",
        origin_note="a fixture weighting that puts everything on resilience",
    )
    opposed = risk.compare_presets(
        frame, scenario=scenario, presets=(hazard_heavy, resilience_heavy), priority_units=2
    )

    print(f"trade-off fixture: {len(rows)} preset row(s), priority list of 2")
    for row in opposed:
        print(
            f"  {row.preset:<18} top {list(row.top_geoids)} pop "
            f"{row.population_in_priority:,} displaced {list(row.displaced_geoids)}"
        )

    return [
        ("one row per preset comes back",
         len(rows) == len(WEIGHT_PRESETS) and len(opposed) == 2),
        ("two opposed weightings pick different units",
         set(opposed[0].top_geoids) != set(opposed[1].top_geoids)),
        ("each row names the units the other weighting prioritises and it does not",
         set(opposed[0].displaced_geoids) == set(opposed[1].top_geoids) - set(opposed[0].top_geoids)
         and len(opposed[0].displaced_geoids) > 0),
        ("who loses is populated, not an empty tuple standing in for agreement",
         all(len(row.displaced_geoids) > 0 for row in opposed)),
        ("the priority population is the population of the units listed",
         opposed[0].population_in_priority
         == int(
             _component_fixture()
             .set_index(Col.GEOID)
             .loc[list(opposed[0].top_geoids), Col.POPULATION]
             .sum()
         )),
        ("the vulnerable population never exceeds the priority population",
         all(
             row.vulnerable_population_in_priority <= row.population_in_priority
             for row in (*rows, *opposed)
         )),
        ("each row carries the scenario it was computed under",
         all(row.scenario == scenario.name for row in rows) and len(rows) > 0),
        ("a priority list of zero units is refused",
         verify.refuses(
             lambda: risk.compare_presets(frame, scenario=scenario, priority_units=0),
             ValueError,
             "prioritises nobody",
         )),
        ("a vulnerable quantile outside [0, 1] is refused",
         verify.refuses(
             lambda: risk.compare_presets(
                 frame, scenario=scenario, vulnerable_quantile=1.4
             ),
             ValueError,
             "outside [0, 1]",
         )),
        ("comparing no weighting at all is refused",
         verify.refuses(
             lambda: risk.compare_presets(frame, scenario=scenario, presets=()),
             ValueError,
             "no weighting to compare",
         )),
    ]


def _resilience_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """Facility counting, on a layout whose answer is stated here.

    Four units in a row, and facilities placed so that the count per unit is known
    before the call. The radius is the parameter under test: at 1.5 km each unit
    reaches its own neighbourhood, at 100 km every unit reaches everything, and
    the two must not give the same answer.

    **Both frames are defined in degrees and projected here, and both are then
    passed in geographically.** `resilience` reprojects two arguments, and
    `verify.metric_bypasses` cannot tell them apart: it asks whether the function
    mentions `to_working_crs` at all, so either call alone satisfies it. An
    earlier version of this fixture built the facilities pre-projected, which left
    the facilities reprojection unexercised -- dropping it passed all 66 checks.
    `gpd.sjoin` does not raise on a CRS mismatch; it warns and returns no rows, so
    every unit would have read as reaching nothing.
    """
    risk = Risk(aligner)
    bounds = align.CRS.from_user_input(aligner.working_crs).area_of_use
    lat = (bounds.south + bounds.north) / 2
    lon = (bounds.west + bounds.east) / 2
    degrees = gpd.GeoDataFrame(
        {Col.GEOID: [f"0100100010{i}" for i in range(4)]},
        geometry=[align.Point(lon + i * 0.05, lat) for i in range(4)],
        crs=config.STORAGE_CRS,
    )
    units = aligner.to_working_crs(degrees)
    separation = float(
        min(
            units.geometry.iloc[i].distance(units.geometry.iloc[i + 1])
            for i in range(len(units) - 1)
        )
    )
    facilities_degrees = gpd.GeoDataFrame(
        {"amenity": ["hospital", "school", "school", "fire_station"]},
        geometry=[
            align.Point(lon, lat),
            align.Point(lon + 0.05, lat),
            align.Point(lon + 0.051, lat),
            align.Point(lon + 0.10, lat),
        ],
        crs=config.STORAGE_CRS,
    )
    facilities = aligner.to_working_crs(facilities_degrees)

    near, near_evidence = risk.resilience(units, facilities, radius_m=1500.0, dataset="fixture")
    far, far_evidence = risk.resilience(units, facilities, radius_m=100_000.0, dataset="fixture")
    geographic_units, _ = risk.resilience(
        degrees, facilities, radius_m=1500.0, dataset="fixture"
    )
    geographic_facilities, _ = risk.resilience(
        units, facilities_degrees, radius_m=1500.0, dataset="fixture"
    )
    both_geographic, _ = risk.resilience(
        degrees, facilities_degrees, radius_m=1500.0, dataset="fixture"
    )

    print(
        f"resilience fixture: 4 units {separation:,.0f} m apart, one facility at each of "
        "the first three and a second beside the middle one"
    )
    print(f"  radius 1.5 km  -> ranks {[round(float(v), 4) for v in near]}")
    print(
        f"    counts min {near_evidence.min_count} median {near_evidence.median_count} "
        f"max {near_evidence.max_count}; {near_evidence.units_with_none} unit(s) reach none "
        f"(expected counts 1, 2, 1, 0)"
    )
    print(f"  radius 100 km  -> ranks {[round(float(v), 4) for v in far]}")
    print(
        f"    every unit reaches all {far_evidence.max_count} facility(ies), so the rank "
        "is flat and contributes nothing at any weight"
    )
    print(f"  tag key {near_evidence.tag_key!r}, by tag {near_evidence.by_tag}")
    print(
        f"  handed over geographically: units {[round(float(v), 4) for v in geographic_units]}, "
        f"facilities {[round(float(v), 4) for v in geographic_facilities]}, "
        f"both {[round(float(v), 4) for v in both_geographic]} -- all must equal the projected ranks"
    )

    return [
        ("the fixture units are further apart than the radius, so the counts are separable",
         separation > 3000.0),
        ("the unit reaching two facilities ranks above the ones reaching one",
         float(near.iloc[1]) > float(near.iloc[0])
         and float(near.iloc[1]) > float(near.iloc[3])),
        ("the unit reaching none ranks lowest",
         float(near.iloc[3]) == float(near.min()) and near_evidence.units_with_none == 1),
        ("the counts are the stated ones, not merely ordered correctly",
         near_evidence.min_count == 0
         and near_evidence.max_count == 2
         and near_evidence.matches == 4),
        ("a wider radius reaches strictly more, and is a different answer",
         far_evidence.max_count == 4
         and far_evidence.min_count == 4
         and [float(v) for v in far] != [float(v) for v in near]),
        ("a radius large enough to flatten the rank says so rather than ranking noise",
         any("contributes nothing to the ranking" in w for w in far_evidence.warnings)),
        ("the tag key was read from the retrieval, not written into this module",
         near_evidence.tag_key == "amenity"
         and acquire.DATASET_FACILITIES in near_evidence.tag_source),
        ("facilities are broken down by the tag that was retrieved",
         near_evidence.by_tag.get("school") == 2
         and sum(near_evidence.by_tag.values()) == near_evidence.matches),
        ("units arriving in a geographic CRS are reprojected, not buffered in degrees",
         [float(v) for v in geographic_units] == [float(v) for v in near]),
        ("facilities arriving in a geographic CRS are reprojected before the join",
         [float(v) for v in geographic_facilities] == [float(v) for v in near]),
        ("and so are both at once, which is how a sandbox script would hand them over",
         [float(v) for v in both_geographic] == [float(v) for v in near]),
        ("a radius of zero or less is refused",
         verify.refuses(
             lambda: risk.resilience(units, facilities, radius_m=0.0),
             ValueError,
             "has to be positive",
         )),
        ("a frame with no GEOID is refused",
         verify.refuses(
             lambda: risk.resilience(units.drop(columns=[Col.GEOID]), facilities),
             KeyError,
             "no GEOID column to count facilities against",
         )),
    ]


def _exposure_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """Exposure over a synthetic county whose answer is arithmetic stated here.

    The live county can bound this but cannot pin it. "Exposed never exceeds
    population" and "the two estimates disagree" stay true when exposure is
    computed as the population NOT on flooded land, or as the whole population of
    any unit that floods at all -- both are wrong, both are plausible, and both
    survived the first mutation sweep behind those bounds.

    So: a 10x10 raster whose elevation equals the column index, a surge of 3.5 m,
    and block groups drawn on the column boundaries so each one is fully wet, half
    wet or fully dry by construction.

        tract 01001000100, population 5,000, two whole rows
            ...1001  3,000 people, columns 0-1, every cell wet    -> 3,000 exposed
            ...1002  1,000 people, columns 2-5, half the cells    ->   500 exposed
            ...1003  1,000 people, columns 6-9, no cell wet       ->     0 exposed
                                                          summed  -> 3,500

    The tract-uniform estimate for the same tract is 5,000 x 0.4 = 2,000, because
    four of its ten columns are wet. That 1,500-person gap is the granularity
    result, and it is the number the check asserts rather than merely observing
    that a gap exists.

    The populations are deliberately not symmetric: with 2,000 exposed of 5,000,
    "population not on flooded land" would return 3,000 and be caught, but with a
    half-and-half split it would return 2,500 and pass. A fixture can be balanced
    in a way that hides exactly the error it was built to find.
    """
    import tempfile

    from .hazard import _anchor, _fixture_registry, _fixture_scenario, _write_fixture

    root = Path(tempfile.mkdtemp(prefix="exposure_check_"))
    cell, side = 100.0, 10
    x0, y0 = _anchor(aligner)
    top = y0 + side * cell
    elevation = np.tile(np.arange(side, dtype="float64"), (side, 1))
    raster = _write_fixture(root / "elev.tif", elevation, x0, y0, cell, aligner.working_crs)

    def cellbox(first_row: int, last_row: int, first_col: int, last_col: int) -> Any:
        """A polygon covering whole grid cells, rows and columns half-open."""
        return align.Polygon(
            [
                (x0 + first_col * cell + 1, top - last_row * cell + 1),
                (x0 + last_col * cell - 1, top - last_row * cell + 1),
                (x0 + last_col * cell - 1, top - first_row * cell - 1),
                (x0 + first_col * cell + 1, top - first_row * cell - 1),
            ]
        )

    tracts = gpd.GeoDataFrame(
        {
            Col.GEOID: ["01001000100", "01001000200"],
            Col.POPULATION: [5000.0, 4000.0],
        },
        geometry=[cellbox(0, 2, 0, side), cellbox(2, 4, 0, side)],
        crs=aligner.working_crs,
    )
    groups = gpd.GeoDataFrame(
        {
            Col.GEOID: [
                "010010001001", "010010001002", "010010001003",
                "010010002001", "010010002002", "010010002003",
            ],
            Col.POPULATION: [3000.0, 1000.0, 1000.0, 500.0, 1500.0, 2000.0],
        },
        geometry=[
            cellbox(0, 2, 0, 2), cellbox(0, 2, 2, 6), cellbox(0, 2, 6, side),
            cellbox(2, 4, 0, 2), cellbox(2, 4, 2, 6), cellbox(2, 4, 6, side),
        ],
        crs=aligner.working_crs,
    )

    risk = Risk(Alignment(_fixture_registry(root, raster, aligner.working_crs)))
    surface = risk.hazard.derive_surface(_fixture_scenario(3.5, "exposurefixture"))
    exposed, evidence = risk.exposure(
        surface, tracts, groups, tract_name="fixture tracts", group_name="fixture groups"
    )
    fine_fractions, _ = risk.hazard.measure(surface, groups, dataset="fixture groups")
    coarse_fractions, _ = risk.hazard.measure(surface, tracts, dataset="fixture tracts")

    got = [round(float(value), 6) for value in exposed]
    fractions = [round(float(value), 6) for value in fine_fractions[Col.INUNDATED_FRACTION]]

    print("exposure fixture: 10x10 raster, elevation = column index, surge 3.5 m")
    print(
        f"  block-group flooded fractions {fractions}   "
        "(expected 1.0 0.5 0.0 1.0 0.5 0.0 by construction)"
    )
    print(
        f"  tract flooded fractions "
        f"{[round(float(v), 6) for v in coarse_fractions[Col.INUNDATED_FRACTION]]}   "
        "(expected 0.4 0.4: four of ten columns)"
    )
    print(f"  exposed, summed from block groups {got}   (expected 3500.0 1250.0)")
    print(
        f"  exposed, tract-uniform            "
        f"[{evidence.coarse_total - 1600.0:,.1f}, 1600.0] totalling "
        f"{evidence.coarse_total:,.1f}   (expected 2000.0 1600.0, total 3600.0)"
    )
    print(
        f"  worst unit differs by {evidence.max_abs_difference:,.1f} resident(s) = "
        f"{evidence.relative_error_pct:.2f}%   (expected 1500.0 and 75.00%)"
    )

    overstated = tracts.copy()
    overstated[Col.POPULATION] = [1000.0, 4000.0]

    return [
        ("the fixture's block groups really are fully wet, half wet and dry as drawn",
         fractions == [1.0, 0.5, 0.0, 1.0, 0.5, 0.0]),
        ("exposed population is population times flooded fraction, summed per block group",
         got == [3500.0, 1250.0]),
        ("it is not the population NOT on flooded land",
         got != [1500.0, 2750.0]),
        ("it is not the whole population of every unit that floods at all",
         got != [4000.0, 2000.0]),
        ("the tract-uniform estimate is the stated one, and differs from the rollup",
         abs(evidence.coarse_total - 3600.0) < 1e-6
         and abs(evidence.fine_total - 4750.0) < 1e-6),
        ("the granularity gap is the stated number, not merely non-zero",
         abs(evidence.max_abs_difference - 1500.0) < 1e-6
         and abs(evidence.relative_error_pct - 75.0) < 1e-6),
        ("both tracts were compared, neither silently dropped",
         evidence.units_compared == 2 and evidence.fine_units == 6),
        ("a rollup exceeding the published parent population is refused",
         verify.refuses(
             lambda: risk.exposure(
                 surface, overstated, groups,
                 tract_name="fixture tracts", group_name="fixture groups",
             ),
             ValueError,
             "more exposed residents than residents",
         )),
        ("the same call on honest populations does not refuse, so that guard discriminates",
         evidence.units_over_population == 0 and len(got) == 2),
    ]


def _county_checks(aligner: Alignment) -> list[tuple[str, bool]]:
    """The real table, and the three sanity rules the gate names."""
    tracts, groups, facilities, key = _layers(aligner)
    risk = Risk(aligner)
    scenario = HAZARD_SCENARIOS[1]
    surface = risk.hazard.derive_surface(scenario)
    table = risk.table(surface, tracts, groups, facilities, dataset=key)
    frame = table.frame

    population = pd.to_numeric(frame[Col.POPULATION], errors="coerce")
    exposed = pd.to_numeric(frame[Col.EXPOSED_POPULATION], errors="coerce")
    fraction = pd.to_numeric(frame[Col.INUNDATED_FRACTION], errors="coerce")
    score = pd.to_numeric(frame[Col.RISK_SCORE], errors="coerce")

    comparable = population.notna() & exposed.notna()
    within = (exposed <= population + 1e-6)[comparable]
    bounded = ((fraction >= 0.0) & (fraction <= 1.0))[fraction.notna()]

    tops: dict[str, list[str]] = {}
    for preset in WEIGHT_PRESETS:
        combined, _ = risk.combine(frame, scenario=scenario, preset=preset, dataset=key)
        tops[preset.name] = (
            combined.dropna(subset=[Col.RISK_SCORE])
            .sort_values(Col.RISK_SCORE, ascending=False)
            .head(DEFAULT_PRIORITY_UNITS)[Col.GEOID]
            .tolist()
        )
    differing = [
        (a.name, b.name)
        for i, a in enumerate(WEIGHT_PRESETS)
        for b in WEIGHT_PRESETS[i + 1:]
        if set(tops[a.name]) != set(tops[b.name])
    ]
    rows = risk.compare_presets(frame, scenario=scenario, units=tracts)
    # svi_equal and svi_themes carry identical objective weights and differ only in
    # how they weight the five indicators. Comparing them on a frame whose index was
    # already computed under one preset compares nothing -- the rows come back
    # identical. This pair is the check that the whole preset is what varies.
    whole = {
        row.preset: set(row.top_geoids)
        for row in risk.compare_presets(
            frame, scenario=scenario, units=tracts,
            presets=(PRESETS["svi_equal"], PRESETS["svi_themes"]),
        )
    }
    objective_only = {
        row.preset: set(row.top_geoids)
        for row in risk.compare_presets(
            frame, scenario=scenario,
            presets=(PRESETS["svi_equal"], PRESETS["svi_themes"]),
        )
    }

    print(
        f"county: {len(frame)} tract(s), {table.risk.units_scored} scored, scenario "
        f"{scenario.name} at {scenario.surge_height_m} m"
    )
    print(
        f"  exposed population {exposed.sum():,.0f} of {population.sum():,.0f} resident(s); "
        f"{int(comparable.sum())} unit(s) compared, "
        f"{int((~within).sum())} exceed their own population"
    )
    print(
        f"  inundated fraction {fraction.min():.4f} to {fraction.max():.4f} over "
        f"{int(fraction.notna().sum())} unit(s)"
    )
    print(f"  preset pairs with a different top ten: {differing}")
    for name, top in tops.items():
        print(f"    {name:<22} {top[:5]} ...")
    print(
        f"  who loses: "
        + ", ".join(f"{row.preset} drops {len(row.displaced_geoids)}" for row in rows)
    )
    print(
        f"  svi_equal vs svi_themes, whole preset: "
        f"{len(whole['svi_equal'] ^ whole['svi_themes'])} unit(s) differ; "
        f"objective weights alone: "
        f"{len(objective_only['svi_equal'] ^ objective_only['svi_themes'])}"
    )

    return [
        ("exposed population never exceeds the unit's own population",
         bool(within.all()) and int(within.sum()) == len(frame)),
        ("every unit was compared on that rule, not just the ones that happened to pass",
         int(comparable.sum()) == len(frame) and len(frame) > 0),
        ("every inundated fraction lies in [0, 1]",
         bool(bounded.all()) and int(fraction.notna().sum()) == len(frame)),
        ("every risk score lies in [0, 1]",
         bool(((score >= 0) & (score <= 1))[score.notna()].all())
         and int(score.notna().sum()) == table.risk.units_scored),
        ("the ranking changes when the weighting changes",
         len(differing) >= 1),
        ("the four components are all present as their own columns",
         all(name in frame.columns for name in OBJECTIVE_TERMS)),
        ("the raw hazard is in the table beside the county-relative score",
         Col.INUNDATED_FRACTION in frame.columns
         and Col.INUNDATION_MEAN_M in frame.columns
         and Col.INUNDATION_MAX_M in frame.columns),
        ("exactly one tract is unscored, the one with no vulnerability index",
         table.risk.units_unscored == 1
         and table.risk.unscored_geoids == table.vulnerability.unscored_geoids),
        ("the two exposure estimates disagree, which is the granularity result",
         table.exposure.max_abs_difference > 0.0),
        ("the finer estimate is the one in the table",
         abs(float(exposed.sum()) - table.exposure.fine_total) < 1.0
         and abs(table.exposure.fine_total - table.exposure.coarse_total) > 1.0),
        ("every priority list says how many units it omits",
         all(len(row.top_geoids) == DEFAULT_PRIORITY_UNITS for row in rows)
         and len(rows) == len(WEIGHT_PRESETS)),
        ("at least one weighting drops a unit another prioritises",
         any(len(row.displaced_geoids) > 0 for row in rows)),
        ("two presets differing only in indicator weights rank the county differently",
         whole["svi_equal"] != whole["svi_themes"]),
        ("and they agree when only the objective half is varied, so `units` does the work",
         objective_only["svi_equal"] == objective_only["svi_themes"]),
        ("the score is county-relative and the table says so",
         any("COUNTY-RELATIVE" in w for w in table.risk.warnings)),
    ]


def _contract_checks() -> list[tuple[str, bool]]:
    module = sys.modules[__name__]
    print(f"  components: {list(OBJECTIVE_TERMS)}")
    print(f"  directions: {COMPONENT_DIRECTION}")
    return [
        ("every objective term states which way it pushes the score",
         set(COMPONENT_DIRECTION) == set(OBJECTIVE_TERMS) and len(OBJECTIVE_TERMS) == 4),
        ("every objective term explains itself in a sentence",
         set(COMPONENT_RATIONALE) == set(OBJECTIVE_TERMS)
         and all(len(text) > 40 for text in COMPONENT_RATIONALE.values())),
        ("at least one term lowers the score and at least one raises it",
         MORE_IS_BETTER in COMPONENT_DIRECTION.values()
         and MORE_IS_WORSE in COMPONENT_DIRECTION.values()),
    ] + verify.discipline_checks(module)


def _self_check() -> int:
    registry = Registry()
    registry.load_manifest()
    aligner = Alignment(registry)

    print(f"study area: {aligner.study_area.name}   working crs: {aligner.working_crs}\n")

    checks = _combine_checks(aligner)
    print()
    checks += _tradeoff_checks(aligner)
    print()
    checks += _resilience_checks(aligner)
    print()
    checks += _exposure_checks(aligner)
    print()
    checks += _county_checks(aligner)
    print()
    checks += _contract_checks()
    return verify.report(checks)


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
