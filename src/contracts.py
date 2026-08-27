"""Frozen interfaces for the OASIS Track A risk agent.

THIS FILE IS THE CONTRACT. Every other module implements against it.

Rules (also stated in CLAUDE.md):
  * Do not change a dataclass field or a Protocol signature here to make an
    implementation compile. Change the implementation.
  * If a signature genuinely has to change, change it HERE FIRST, in its own
    commit, with a one-line note saying why -- then update every caller.
  * This module holds data shapes and call signatures only. No logic, no I/O,
    no imports of project modules. It must stay importable with nothing else
    working.

Why it exists: this system is built across many short sessions. Without a
frozen contract, session 6 quietly renames what session 2 produced and the
pipeline drifts apart. The contract is the thing that makes the sessions
composable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------

ToolResult: TypeAlias = dict[str, Any]
"""What every LLM-visible tool returns. Small, JSON-serialisable, no geometry."""

Geoid: TypeAlias = str
BBox: TypeAlias = tuple[float, float, float, float]
"""(min_lon, min_lat, max_lon, max_lat) in EPSG:4326. Always this order, always 4326."""

DatasetKind: TypeAlias = Literal["vector", "raster", "table"]
FaultKind: TypeAlias = Literal["timeout", "server_error", "empty", "wrong_crs", "truncated"]


# ---------------------------------------------------------------------------
# column names -- the second half of the contract
# ---------------------------------------------------------------------------

class Col:
    """Canonical column names. Never write one of these as a string literal
    anywhere else; a typo in a column name is the most common way a multi-session
    build silently breaks."""

    GEOID = "GEOID"
    NAME = "name"

    # demography (from ACS, resolved at run time -- see acquire.resolve_acs_variables)
    POPULATION = "population"
    POP_MOE = "population_moe"
    PCT_POVERTY = "pct_poverty"
    PCT_AGE_65_PLUS = "pct_age_65_plus"
    PCT_DISABILITY = "pct_disability"
    PCT_NO_VEHICLE = "pct_no_vehicle"
    PCT_LIMITED_ENGLISH = "pct_limited_english"

    # hazard (from the raster + vector hazard layers)
    ELEV_MIN_M = "elev_min_m"
    ELEV_MEAN_M = "elev_mean_m"
    INUNDATED_FRACTION = "inundated_fraction"
    INUNDATION_MEAN_M = "inundation_mean_m"
    INUNDATION_MAX_M = "inundation_max_m"
    RASTER_CELLS = "raster_cells"

    # derived
    VULNERABILITY = "vulnerability_index"
    RESILIENCE = "resilience_index"
    EXPOSED_POPULATION = "exposed_population"
    RISK_SCORE = "risk_score"
    PRIORITY_RANK = "priority_rank"


VULNERABILITY_INDICATORS: tuple[str, ...] = (
    Col.PCT_POVERTY,
    Col.PCT_AGE_65_PLUS,
    Col.PCT_DISABILITY,
    Col.PCT_NO_VEHICLE,
    Col.PCT_LIMITED_ENGLISH,
)
"""The indicator set. Each must be justifiable in one sentence in the paper.
Adding a sixth means writing that sentence; that is the intended friction."""


# ---------------------------------------------------------------------------
# provenance and the dataset registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Provenance:
    """Attached to every dataset. A dataset without one does not enter the registry."""

    dataset: str
    source_url: str
    retrieved_at: datetime
    declared_crs: str          # what the service SAID it was returning
    working_crs: str           # what we reprojected to
    vintage: str               # "ACS 2019-2023", "3DEP 2024-06", "OSM 2026-08-27"
    feature_count: int
    license: str
    request_params: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    name: str
    kind: DatasetKind
    path: Path
    provenance: Provenance


# ---------------------------------------------------------------------------
# alignment
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AlignmentReport:
    """What the cleaning stage actually did. Goes straight into paper section 2.3
    and into the `describe_alignment` tool result, so keep every field reportable."""

    reprojected: dict[str, str] = field(default_factory=dict)      # dataset -> "EPSG:3857 -> EPSG:5070"
    geometries_repaired: int = 0
    geometries_dropped: int = 0
    sentinels_removed: dict[str, int] = field(default_factory=dict)  # column -> count
    unmatched_left: tuple[Geoid, ...] = ()      # geometry side, no attributes
    unmatched_right: tuple[Geoid, ...] = ()     # attribute side, no geometry
    apportioned: dict[str, str] = field(default_factory=dict)      # column -> method used
    apportionment_error: dict[str, float] = field(default_factory=dict)  # column -> % vs published
    units_below_cell_threshold: int = 0         # polygons smaller than MIN_RASTER_CELLS
    temporal_span: dict[str, str] = field(default_factory=dict)    # dataset -> vintage
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# domain values -- the things a human sets, not the agent
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WeightPreset:
    """A named weighting. `origin` is scored: criterion SG asks that community
    perspectives be incorporated, and a preset whose weights came from a published
    planning process is the cheapest honest way to show that."""

    name: str
    weights: dict[str, float]          # keys drawn from VULNERABILITY_INDICATORS + objective terms
    origin: Literal["authors", "published_plan", "published_index", "user"]
    origin_note: str                   # one sentence: where it came from
    origin_url: str = ""


@dataclass(frozen=True, slots=True)
class HazardScenario:
    """A surge scenario. Height is metres above local mean sea level; the mapping
    from hurricane category to height is an assumption and must be cited."""

    name: str
    surge_height_m: float
    source: str
    assumption_note: str


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ScenarioRow:
    """One row of the trade-off table: one weighting under one hazard scenario."""

    preset: str
    scenario: str
    top_geoids: tuple[Geoid, ...]
    population_in_priority: int
    vulnerable_population_in_priority: int
    mean_inundation_m: float
    displaced_geoids: tuple[Geoid, ...] = ()
    """Units that a different preset prioritises and this one does not -- i.e. who loses."""


@dataclass(slots=True)
class CodeRun:
    """One execution of model-written analysis code."""

    attempt: int
    source: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    error_type: str | None = None      # "AttributeError", "CRSError", "Timeout", ...


@dataclass(slots=True)
class CodeSession:
    """All attempts for one code-generation request. The instrumentation behind
    criterion TU: report attempts, first-run failure rate, and repair rate."""

    request: str
    runs: list[CodeRun] = field(default_factory=list)
    succeeded: bool = False


@dataclass(slots=True)
class CriticFinding:
    kind: Literal["untraceable_number", "invariant_violation", "unsupported_claim"]
    detail: str
    evidence: str = ""


@dataclass(slots=True)
class CriticReport:
    cycle: int
    findings: list[CriticFinding] = field(default_factory=list)
    numbers_checked: int = 0
    numbers_traceable: int = 0

    @property
    def passed(self) -> bool:
        return not self.findings


@dataclass(frozen=True, slots=True)
class FaultConfig:
    """Injected failures for the robustness experiment. `rate` is per network call."""

    kind: FaultKind
    rate: float
    seed: int = 0


@dataclass(slots=True)
class FaultEvent:
    kind: FaultKind
    dataset: str
    recovered: bool
    extra_turns: int


# ---------------------------------------------------------------------------
# module protocols -- the call signatures each module must provide
# ---------------------------------------------------------------------------

@runtime_checkable
class Acquirer(Protocol):
    """src/acquire.py. Every method performs a live network call, carries an
    explicit timeout and a bounded retry, and returns data plus provenance."""

    def discover_arcgis_layers(
        self, service_url: str, *, name_contains: str | None = None, timeout_s: float = 30.0
    ) -> list[dict[str, Any]]: ...

    def fetch_arcgis_vector(
        self,
        service_url: str,
        layer_id: int,
        *,
        where: str = "1=1",
        out_sr: int = 4326,
        bbox: BBox | None = None,
        timeout_s: float = 60.0,
    ) -> tuple[Any, Provenance]: ...          # (GeoDataFrame, Provenance)

    def fetch_arcgis_raster(
        self, service_url: str, bbox: BBox, *, out_sr: int, cell_size_m: float, timeout_s: float = 120.0
    ) -> tuple[Path, Provenance]: ...         # (GeoTIFF on disk, Provenance)

    def resolve_acs_variables(
        self, year: int, wanted: dict[str, str], *, timeout_s: float = 30.0
    ) -> dict[str, str]: ...
    """wanted: canonical column name -> a label/concept pattern to match against
    the ACS variables catalogue. Returns canonical name -> resolved variable id.
    Variable ids are NEVER hardcoded; they are discovered at run time."""

    def fetch_acs(
        self,
        year: int,
        variables: dict[str, str],
        state_fips: str,
        county_fips: str,
        *,
        geography: Literal["tract", "block group"] = "tract",
        timeout_s: float = 60.0,
    ) -> tuple[Any, Provenance]: ...          # (DataFrame, Provenance)

    def fetch_osm(
        self, bbox: BBox, tags: dict[str, str], *, timeout_s: float = 120.0
    ) -> tuple[Any, Provenance]: ...          # (GeoDataFrame, Provenance)


@runtime_checkable
class Aligner(Protocol):
    """src/align.py. The only place data is cleaned. Never clean by hand."""

    def to_working_crs(self, obj: Any) -> Any: ...
    def repair_geometry(self, gdf: Any) -> tuple[Any, int]: ...
    def scrub_sentinels(self, df: Any, value_cols: list[str]) -> tuple[Any, dict[str, int]]: ...
    def join_on_geoid(self, geom: Any, attrs: Any) -> tuple[Any, AlignmentReport]: ...
    def apportion(
        self, fine: Any, coarse: Any, columns: list[str], *, method: Literal["sum", "population_weighted"]
    ) -> tuple[Any, dict[str, float]]: ...
    def zonal_stats(
        self, raster_path: Path, polygons: Any, *, stats: tuple[str, ...] = ("min", "mean", "max", "count")
    ) -> Any: ...                              # DataFrame indexed by GEOID


@runtime_checkable
class Sandbox(Protocol):
    """src/sandbox.py. Executes model-written code in a subprocess."""

    def run(self, source: str, *, timeout_s: float = 60.0) -> CodeRun: ...
    def repair_loop(self, request: str, *, max_attempts: int = 3) -> CodeSession: ...


@runtime_checkable
class Critic(Protocol):
    """src/critic.py. Enforces invariant 8: every reported number is traceable."""

    def check(self, answer: str, steps: list[dict[str, Any]], cycle: int) -> CriticReport: ...
    def invariants(self, frame: Any) -> list[CriticFinding]: ...


class ToolFn(Protocol):
    def __call__(self, **kwargs: Any) -> ToolResult: ...


# ---------------------------------------------------------------------------
# the LLM-visible tool surface -- frozen names
# ---------------------------------------------------------------------------

TOOL_NAMES: tuple[str, ...] = (
    "list_datasets",           # registry contents + provenance summary
    "acquire_dataset",         # live retrieval by name; the autonomy showcase
    "describe_layer",          # columns, dtypes, null counts, CRS, feature count
    "describe_alignment",      # AlignmentReport for the current snapshot
    "hazard_exposure",         # zonal inundation per unit for a named scenario
    "vulnerability_index",     # weighted percentile index; weights are arguments
    "risk_scenario",           # combine components under a named preset
    "compare_scenarios",       # the trade-off table, including who loses
    "ask_user_preferences",    # HITL: elicit weighting before deciding
    "run_spatial_code",        # write / execute / repair
    "validate_answer",         # run the critic on a draft answer
)
"""Eleven tools. Keep it at or under twelve. A twelfth idea is usually a
parameter on an existing tool, not a new one."""

MIN_RASTER_CELLS: int = 10
"""Polygons covering fewer raster cells than this get flagged in AlignmentReport;
their zonal statistics are not trustworthy and the paper says so."""
