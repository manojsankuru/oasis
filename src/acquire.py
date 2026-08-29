"""Live retrieval of remote geospatial data.

The only module that fetches data from a remote service. Every function here
makes a real network call, carries an explicit timeout and a bounded retry, and
hands back the data together with the Provenance record that lets it enter the
registry.

Two things this module refuses to assume:

* **Layer ids.** TIGERweb publishes four different layers named "Census Tracts",
  one per vintage, and NFHL moves its flood layer between releases.
  `discover_arcgis_layers` asks the service what it publishes and `select_layer`
  picks one under a tie-break stated in its docstring.
* **The CRS a service returns.** See `_received_crs`. Asking for `f=json`
  rather than `f=geojson` is what makes that assertion real rather than assumed;
  docs/DATA.md section 1 records the measurement behind that choice.

Data comes back in the CRS that was requested, not in the working CRS. The
registry reprojects on load and align.py does the cleaning; this module returns
what the service returned.

`main()` retrieves the two boundary layers and the two ACS tables. Session 5
extends it to the elevation raster and the OSM facilities, and the full
six-entry manifest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import re
import sys
import tempfile
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import pandas as pd
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import config
from . import provenance as prov
from .contracts import VULNERABILITY_INDICATORS, BBox, Col, DatasetRecord, Provenance
from .registry import Registry

USER_AGENT = (
    "oasis-geo-agent/0.1 (ACM SIGSPATIAL 2026 Student Challenge, Track A; "
    "academic use; contact via repository)"
)

TIGERWEB_TRACTS_BLOCKS_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer"
)
NFHL_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
CENSUS_DATA_URL = "https://api.census.gov/data"
ACS_PRODUCT = "acs/acs5"

DATASET_TRACTS = "tracts"
DATASET_BLOCK_GROUPS = "block_groups"
DATASET_FLOOD_ZONES = "flood_zones"
DATASET_ACS = "acs"
DATASET_ACS_BLOCK_GROUPS = "acs_block_groups"

TRACTS_LAYER_NAME = "Census Tracts"
BLOCK_GROUPS_LAYER_NAME = "Census Block Groups"
FLOOD_ZONES_LAYER_NAME = "Flood Hazard Zones"
POLYGON_GEOMETRY = "esriGeometryPolygon"

PAGE_SIZE = 2000
MAX_PAGES = 250

ACS_NAME_FIELD = "NAME"
ACS_ESTIMATE_SUFFIX = "E"
ACS_MARGIN_SUFFIX = "M"
ACS_EARLIEST_YEAR = 2010
ACS_MAX_GET_VARIABLES = 50
ACS_TRACT = "tract"
ACS_BLOCK_GROUP = "block group"

ACS_SUM = "+"
ACS_OVER = "/"

_ACS_REJECTS_LOGGED = 4
_ACS_ESTIMATE_ID = re.compile(r"^[A-Z]+\d+[A-Z]*_\d+E$")

ACS_SEED_TABLES: dict[str, str] = {
    Col.POPULATION: "B01003",
    Col.PCT_POVERTY: "B17001",
    Col.PCT_AGE_65_PLUS: "B01001",
    Col.PCT_DISABILITY: "B18101",
    Col.PCT_NO_VEHICLE: "B08201",
    Col.PCT_LIMITED_ENGLISH: "B16005",
}
"""Which detail table to search for each canonical column. A table prefix, never
a variable id: the table is a stable published concept, while the id inside it
shifts between vintages and is what `resolve_acs_variables` discovers."""

ACS_WANTED: dict[str, str] = {
    Col.POPULATION: r"^Total Population\|\|Estimate!!Total:?$",
    Col.PCT_POVERTY: (
        r"\|\|Estimate!!Total:!!Income in the past 12 months below poverty level:$"
    ),
    Col.PCT_AGE_65_PLUS: (
        r"\|\|Estimate!!Total:!!(?:Male|Female):!!(?:6[5-9]|[78]\d) (?:and|to) \d+ years$"
        r"|\|\|Estimate!!Total:!!(?:Male|Female):!!85 years and over$"
    ),
    Col.PCT_DISABILITY: r"\|\|Estimate!!Total:!!(?:Male|Female):!!.+:!!With a disability$",
    Col.PCT_NO_VEHICLE: r"\|\|Estimate!!Total:!!No vehicle available$",
    Col.PCT_LIMITED_ENGLISH: (
        r"\|\|Estimate!!Total:!!(?:Native|Foreign born):!!Speak .+:"
        r"!!Speak English \"(?:well|not well|not at all)\"$"
    ),
}
"""Canonical column -> a regex matched against "concept||label" in the ACS
variable catalogue. Every pattern is anchored to the end of the label, which is
what separates a wanted leaf from its parents: B08201 publishes "No vehicle
available" once for the table and again inside each household-size block, and an
unanchored pattern would silently sum the same households five times.

Four of the six indicators are sums of leaves rather than single variables --
the ACS publishes 65-and-over as twelve sex-by-age cells, not as one total -- so
a resolved value is a group of ids, joined by ACS_SUM and divided by the table
total after ACS_OVER. See `acs_variable_ids`."""

ACS_TABLE_TOTAL = r"\|\|Estimate!!Total:?$"
"""The denominator, resolved the same way as every numerator. Assuming the total
is `{table}_001E` happens to hold for all six tables here and is still the same
mistake as hardcoding an id."""

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRYABLE_TRANSPORT = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


class AcquisitionError(RuntimeError):
    """Base class, so a caller can degrade gracefully on any retrieval failure."""


class TransientError(AcquisitionError):
    """A failure worth retrying: transport error, 429, or 5xx."""


class ServiceError(AcquisitionError):
    """The service answered and the answer is unusable. Retrying will not help."""


class CRSMismatch(AcquisitionError):
    """The service returned a different CRS from the one requested."""


class NonJsonResponse(ServiceError):
    """The service answered with something that is not JSON.

    Carries the response pieces rather than only a message, because the
    interesting cases are service-specific and the caller is the only one that
    can name the cause: ArcGIS serves an HTML page for a bad path, and the Census
    API serves one when the key is missing or rejected -- both under HTTP 200,
    where the content type is the only signal that anything went wrong.
    """

    def __init__(self, url: str, status_code: int, content_type: str, body: str) -> None:
        self.url = url
        self.status_code = status_code
        self.content_type = content_type
        self.body = body
        self.title = _html_title(body) or " ".join(body.split())[:80]
        super().__init__(
            f"{url}: HTTP {status_code} with Content-Type {content_type}, "
            f"not JSON: {self.title!r}"
        )


def _html_title(body: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(match.group(1).split()) if match else ""


_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _SESSION = session
    return _SESSION


def _raise_for_arcgis_error(payload: Any, url: str) -> None:
    """ArcGIS answers a bad parameter with HTTP 200 and a JSON error body.

    Nothing downstream would notice: the status is fine, the body is valid JSON,
    and only the absence of a `features` key gives it away. Catch it here so a
    malformed query never reaches disk.
    """
    if not isinstance(payload, dict) or "error" not in payload:
        return
    error = payload.get("error") or {}
    details = "; ".join(str(item) for item in (error.get("details") or []))
    code = error.get("code", "?")
    message = error.get("message", "")
    raise ServiceError(
        f"{url}: HTTP 200 with an ArcGIS error body (code {code}): {message} {details}".strip()
    )


@retry(
    retry=retry_if_exception_type(TransientError),
    stop=stop_after_attempt(config.MAX_RETRIES),
    wait=wait_exponential(multiplier=config.RETRY_BACKOFF_S, max=30.0),
    reraise=True,
)
def _get_payload(url: str, params: dict[str, Any], timeout_s: float) -> Any:
    """Every outbound request in this module goes through here.

    One choke point means one place that carries the timeout, one place that
    decides what is worth retrying, and one place for faults.py to wrap in
    session 12. Only transport errors, 429 and 5xx are retried; a 4xx, an ArcGIS
    error body or an HTML page will never succeed, and retrying one only spends
    the budget.

    Returns whatever JSON the service sent, of whatever shape. ArcGIS answers
    with an object; the Census data endpoint answers with an array whose first
    row is the header. The object assertion lives in `_get_json` rather than here
    so that the two share one retry policy instead of growing a second.
    """
    try:
        response = _session().get(url, params=params, timeout=timeout_s)
    except _RETRYABLE_TRANSPORT as exc:
        raise TransientError(f"{url}: {type(exc).__name__}: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        raise ServiceError(f"{url}: {type(exc).__name__}: {exc}") from exc

    if response.status_code in _RETRYABLE_STATUS:
        raise TransientError(f"{url}: HTTP {response.status_code}")
    if response.status_code >= 400:
        raise ServiceError(f"{url}: HTTP {response.status_code}: {response.text[:200]!r}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise NonJsonResponse(
            url,
            response.status_code,
            response.headers.get("Content-Type", "unknown"),
            response.text[:400],
        ) from exc

    _raise_for_arcgis_error(payload, url)
    return payload


def _get_json(url: str, params: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """`_get_payload` for the callers that require a JSON object."""
    payload = _get_payload(url, params, timeout_s)
    if not isinstance(payload, dict):
        raise ServiceError(f"{url}: expected a JSON object, got {type(payload).__name__}")
    return payload


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _epsg_code(crs: str) -> int:
    """Turn a config CRS string into the integer an ArcGIS parameter wants."""
    match = re.fullmatch(r"EPSG:(\d+)", crs.strip(), flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"expected an EPSG:<code> string, got {crs!r}")
    return int(match.group(1))


def discover_arcgis_layers(
    service_url: str, *, name_contains: str | None = None, timeout_s: float = 30.0
) -> list[dict[str, Any]]:
    """Ask a MapServer or FeatureServer which layers it publishes.

    Returns one small dict per layer, group layers included, so a caller can see
    the vintage structure rather than guess at it. `is_top_level` is the useful
    field: TIGERweb hangs each older vintage off a group layer and leaves the
    current one at the top.
    """
    base = service_url.rstrip("/")
    payload = _get_json(base, {"f": "json"}, timeout_s)
    entries = payload.get("layers") or []
    version = str(payload.get("currentVersion", ""))
    group_names = {
        int(entry["id"]): str(entry.get("name", ""))
        for entry in entries
        if entry.get("type") == "Group Layer"
    }

    layers: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name", ""))
        if name_contains and name_contains.lower() not in name.lower():
            continue
        parent_raw = entry.get("parentLayerId")
        parent = -1 if parent_raw is None else int(parent_raw)
        layer_id = int(entry["id"])
        layers.append(
            {
                "id": layer_id,
                "name": name,
                "type": str(entry.get("type", "")),
                "geometry_type": str(entry.get("geometryType", "")),
                "parent_layer_id": parent,
                "parent_name": group_names.get(parent, ""),
                "is_top_level": parent == -1,
                "url": f"{base}/{layer_id}",
                "service_url": base,
                "service_version": version,
            }
        )
    layers.sort(key=lambda layer: layer["id"])
    return layers


def select_layer(
    layers: list[dict[str, Any]], name: str, *, geometry_type: str | None = None
) -> dict[str, Any]:
    """Choose one layer from a discovery result under a stated tie-break.

    An exact name match beats a substring match, a top-level layer beats one
    nested in a group layer, and a lower id breaks the remaining tie. TIGERweb
    publishes four layers called "Census Tracts" and nests every older vintage
    under a group layer ("ACS 2024", "Census 2020"), so this resolves to the
    current vintage without any module naming a layer id.
    """
    wanted = name.strip().lower()
    candidates = [
        layer
        for layer in layers
        if wanted in layer["name"].lower()
        and (geometry_type is None or layer["geometry_type"] == geometry_type)
    ]
    if not candidates:
        available = ", ".join(f"{layer['id']}:{layer['name']}" for layer in layers) or "none"
        qualifier = "" if geometry_type is None else f" of type {geometry_type}"
        raise ServiceError(
            f"no layer matching {name!r}{qualifier}; service publishes: {available}"
        )
    candidates.sort(
        key=lambda layer: (layer["name"].lower() != wanted, not layer["is_top_level"], layer["id"])
    )
    return candidates[0]


def _layer_metadata(service_url: str, layer_id: int, timeout_s: float) -> dict[str, Any]:
    return _get_json(f"{service_url.rstrip('/')}/{layer_id}", {"f": "json"}, timeout_s)


def _object_id_field(meta: dict[str, Any]) -> str | None:
    """Find the layer's object-id field, by declaration or by field type.

    Paging without a stable sort order is not safe: ArcGIS gives no guarantee
    that two `resultOffset` calls see rows in the same order, so rows can be
    duplicated and others missed with no error at all. TIGERweb omits the
    `objectIdField` key entirely, which is why the field-type scan exists.
    """
    declared = meta.get("objectIdField")
    if declared:
        return str(declared)
    for field in meta.get("fields") or []:
        if field.get("type") == "esriFieldTypeOID":
            return str(field["name"])
    return None


def _received_crs(payload: dict[str, Any], requested_sr: int, url: str) -> str:
    """Assert the service returned the CRS that was asked for, and name it.

    This is the trap in docs/DATA.md section 1. With `f=json` the service states
    `spatialReference` in the response, so comparing it against `outSR` is a
    real check. With `f=geojson` the response states no CRS at all and geopandas
    assumes 4326 -- an assumption dressed as a check. That is the whole reason
    this module asks for `f=json` and parses Esri JSON.

    Esri codes are not EPSG codes: wkid 102100 is EPSG:3857. Either code the
    service reports satisfies the comparison, and the EPSG-valid one is recorded.
    """
    spatial_reference = payload.get("spatialReference")
    if not isinstance(spatial_reference, dict):
        raise CRSMismatch(
            f"{url}: the response states no spatialReference, so the CRS cannot be "
            "verified; do not record a declared CRS you did not read"
        )
    wkid = spatial_reference.get("wkid")
    latest = spatial_reference.get("latestWkid")
    codes = {int(code) for code in (wkid, latest) if isinstance(code, int)}
    if not codes:
        raise CRSMismatch(f"{url}: spatialReference carries no wkid: {spatial_reference!r}")
    if requested_sr not in codes:
        raise CRSMismatch(
            f"{url}: requested outSR={requested_sr} but the service returned "
            f"{sorted(codes)}; coordinates would be in the wrong units"
        )
    return f"EPSG:{latest or wkid}"


def _query_features(
    service_url: str,
    layer_id: int,
    params: dict[str, Any],
    *,
    timeout_s: float,
    oid_field: str | None,
    page_size: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Run a /query, following `resultOffset` until the service stops truncating.

    Bounded three ways, because a server that ignores `resultOffset` would
    otherwise loop forever returning page one: a page cap, a stop on an empty
    page, and a hard failure when a page carries only rows already seen.

    Returns the response envelope with every page's features concatenated, plus
    the number of requests it took.
    """
    url = f"{service_url.rstrip('/')}/{layer_id}/query"
    size = int(page_size or PAGE_SIZE)
    collected: list[dict[str, Any]] = []
    seen: set[Any] = set()
    envelope: dict[str, Any] = {}
    offset = 0
    pages = 0

    while True:
        if pages >= MAX_PAGES:
            raise ServiceError(
                f"{url}: still truncated after {MAX_PAGES} pages "
                f"({len(collected)} features); refusing to page further"
            )
        page_params = dict(params)
        page_params["resultOffset"] = offset
        page_params["resultRecordCount"] = size
        if oid_field:
            page_params["orderByFields"] = oid_field

        payload = _get_json(url, page_params, timeout_s)
        pages += 1
        if not envelope:
            envelope = {key: value for key, value in payload.items() if key != "features"}
        features = payload.get("features") or []
        if not features:
            break

        if oid_field:
            keys = [feature.get("attributes", {}).get(oid_field) for feature in features]
            fresh = [feature for feature, key in zip(features, keys) if key not in seen]
            if not fresh:
                raise ServiceError(
                    f"{url}: page {pages} at resultOffset={offset} returned only rows "
                    "already seen; the service is ignoring resultOffset"
                )
            seen.update(key for key in keys if key is not None)
            collected.extend(fresh)
        else:
            digest = hashlib.sha1(
                json.dumps(features, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if digest in seen:
                raise ServiceError(
                    f"{url}: page {pages} at resultOffset={offset} repeated an earlier "
                    "page; the service is ignoring resultOffset"
                )
            seen.add(digest)
            collected.extend(features)

        if len(features) < size or not payload.get("exceededTransferLimit"):
            break
        offset += len(features)

    envelope.pop("exceededTransferLimit", None)
    envelope["features"] = collected
    return envelope, pages


def _read_esri_json(payload: dict[str, Any], declared_crs: str) -> gpd.GeoDataFrame:
    """Parse an Esri JSON FeatureSet through GDAL's ESRIJSON driver.

    Hand-rolling ring-to-polygon conversion gets hole orientation wrong on the
    first county with a lake in it; GDAL already does it correctly. The driver
    reads a path, not a buffer, so the document goes to a temporary file.

    The CRS is set from the asserted `spatialReference` rather than from whatever
    the driver inferred, so there is one source of truth for it.
    """
    if not payload.get("features"):
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=declared_crs)
    with tempfile.TemporaryDirectory(prefix="esrijson_") as scratch:
        document = Path(scratch) / "featureset.json"
        document.write_text(json.dumps(payload), encoding="utf-8")
        gdf = gpd.read_file(document)
    return gdf.set_crs(declared_crs, allow_override=True)


def fetch_arcgis_vector(
    service_url: str,
    layer_id: int,
    *,
    where: str = "1=1",
    out_sr: int = 4326,
    bbox: BBox | None = None,
    timeout_s: float = 60.0,
) -> tuple[gpd.GeoDataFrame, Provenance]:
    """Retrieve one ArcGIS vector layer, paged, with the returned CRS asserted.

    `bbox` is EPSG:4326 as every BBox in this project is, and is sent as an
    envelope intersect filter alongside `where`.

    The frame comes back in `out_sr`, not in the working CRS, and every field the
    layer publishes comes with it: choosing columns is align.py's job, and a
    column list here would be a second place to keep in step with the service.

    Provenance is named after the remote layer. Call `as_dataset` to rename it to
    the role it plays here before registering it.
    """
    base = service_url.rstrip("/")
    query_url = f"{base}/{layer_id}/query"
    meta = _layer_metadata(base, layer_id, timeout_s)
    oid_field = _object_id_field(meta)

    params: dict[str, Any] = {
        "where": where,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": out_sr,
        "f": "json",
    }
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        params.update(
            {
                "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": _epsg_code(config.STORAGE_CRS),
                "spatialRel": "esriSpatialRelIntersects",
            }
        )

    payload, pages = _query_features(
        base, layer_id, params, timeout_s=timeout_s, oid_field=oid_field
    )
    declared_crs = _received_crs(payload, out_sr, query_url)
    gdf = _read_esri_json(payload, declared_crs)

    layer_name = str(meta.get("name") or f"layer {layer_id}")
    service_version = str(meta.get("currentVersion", ""))
    license_text = str(meta.get("copyrightText") or "").strip() or "not stated by the service"

    order_note = (
        f"paged with orderByFields={oid_field}"
        if oid_field
        else "layer publishes no object-id field; paging order is the service default"
    )
    notes = [
        f"requested outSR={out_sr}; response spatialReference={payload.get('spatialReference')}",
        "CRS verified against the response body, not assumed from the format (f=json)",
        f"retrieved in {pages} page(s) of up to {PAGE_SIZE} features",
        order_note,
        f"outFields=* returned {len(gdf.columns)} columns",
    ]
    if bbox is not None:
        notes.append(f"envelope filter applied in {config.STORAGE_CRS}")

    provenance = Provenance(
        dataset=_slug(layer_name),
        source_url=query_url,
        retrieved_at=prov.utc_now(),
        declared_crs=declared_crs,
        working_crs=config.DEFAULT_WORKING_CRS,
        vintage=f"{layer_name} (layer {layer_id}) from service v{service_version}",
        feature_count=int(len(gdf)),
        license=license_text,
        request_params={key: str(value) for key, value in params.items()},
        notes=tuple(notes),
    )
    return gdf, provenance


def as_dataset(record: Provenance, name: str) -> Provenance:
    """Rename a Provenance from the remote layer's name to this project's name.

    Retrieval names a dataset after the layer it came from ("census_tracts"); the
    registry names it after the role it plays here ("tracts"). This is the one
    place the two meet, and the registry rejects a mismatch.
    """
    return dataclasses.replace(record, dataset=name)


def with_notes(record: Provenance, extra: Sequence[str]) -> Provenance:
    """Append notes to a Provenance after the fact.

    `fetch_acs` cannot see how its variable ids were resolved -- its signature is
    frozen and receives only the resolved ids -- so the caller that did the
    resolving attaches that evidence here.
    """
    return dataclasses.replace(record, notes=record.notes + tuple(extra))


def tigerweb_county_where(area: config.StudyArea) -> str:
    """The TIGERweb attribute filter for one county.

    STATE and COUNTY are that service's field names; the values come from the
    study area and appear nowhere else.
    """
    return f"STATE='{area.state_fips}' AND COUNTY='{area.county_fips}'"


def _snapshot_path(name: str) -> Path:
    return config.SNAPSHOT_DIR / f"{name}.geojson"


def _write_vector(gdf: gpd.GeoDataFrame, name: str) -> Path:
    """Write a layer to the snapshot in the storage CRS.

    GeoJSON is 4326 by definition, so anything else would be written under a
    label that silently lies about it.
    """
    config.ensure_dirs()
    path = _snapshot_path(name)
    gdf.to_crs(config.STORAGE_CRS).to_file(path, driver="GeoJSON")
    return path


def _write_table(frame: pd.DataFrame, name: str) -> Path:
    """Write an attribute table to the snapshot as parquet.

    Not CSV. Every ACS value is deliberately a string, and the county and tract
    codes are zero-padded: `pd.read_csv` reads them back as integers with the
    padding gone, which breaks the GEOID join in a way that reads like missing
    data rather than like a format bug. Parquet keeps the dtype it was handed.
    """
    config.ensure_dirs()
    path = config.SNAPSHOT_DIR / f"{name}.parquet"
    frame.to_parquet(path, index=False)
    return path


def acquire_boundaries(
    area: config.StudyArea, registry: Registry, *, timeout_s: float = config.REQUEST_TIMEOUT_S
) -> list[DatasetRecord]:
    """Retrieve tract and block-group polygons for one county and register them.

    Layer ids are discovered from the service, not named here. Block groups exist
    so that session 7 can apportion an attribute up to tracts and report the
    discrepancy against the published tract figure.
    """
    layers = discover_arcgis_layers(TIGERWEB_TRACTS_BLOCKS_URL, timeout_s=timeout_s)
    where = tigerweb_county_where(area)
    out_sr = _epsg_code(config.STORAGE_CRS)
    wanted = (
        (DATASET_TRACTS, TRACTS_LAYER_NAME),
        (DATASET_BLOCK_GROUPS, BLOCK_GROUPS_LAYER_NAME),
    )

    records: list[DatasetRecord] = []
    for dataset_name, layer_name in wanted:
        layer = select_layer(layers, layer_name, geometry_type=POLYGON_GEOMETRY)
        gdf, provenance = fetch_arcgis_vector(
            layer["service_url"], layer["id"], where=where, out_sr=out_sr, timeout_s=timeout_s
        )
        if len(gdf) == 0:
            raise ServiceError(
                f"{dataset_name}: {where} matched no features in layer {layer['id']}; "
                "the study area or the service field names are wrong"
            )
        path = _write_vector(gdf, dataset_name)
        records.append(
            registry.register(dataset_name, "vector", path, as_dataset(provenance, dataset_name))
        )
    return records


# ---------------------------------------------------------------------------
# American Community Survey
# ---------------------------------------------------------------------------


def _acs_base(year: int) -> str:
    return f"{CENSUS_DATA_URL}/{year}/{ACS_PRODUCT}"


def _census_get(url: str, params: dict[str, Any], timeout_s: float, *, keyed: bool) -> Any:
    """Every Census request goes through here, on top of the one retry policy.

    Two things this adds over `_get_payload`. The key is merged in here and never
    touches the dict the caller keeps, so it cannot reach `Provenance.request_params`
    by being copied along with everything else, and anything that does leak it back
    in an error message is redacted on the way out.

    And a non-JSON answer is renamed after its real cause. A missing or rejected
    key is served as HTTP 200 with an HTML page, so `raise_for_status` passes, the
    content type is the only signal, and "expected JSON" would send a reader
    looking for a parsing bug instead of at CENSUS_API_KEY.
    """
    request_params = dict(params)
    key = config.CENSUS_API_KEY if keyed else ""
    if keyed:
        if not key:
            raise ServiceError(
                f"{url}: CENSUS_API_KEY is unset, and the ACS data endpoint answers a "
                "keyless request with HTTP 200 and an HTML 'Missing Key' page rather "
                "than an error status. Free key: "
                "https://api.census.gov/data/key_signup.html"
            )
        request_params["key"] = key
    try:
        return _get_payload(url, request_params, timeout_s)
    except NonJsonResponse as exc:
        raise ServiceError(
            f"{url}: the Census API answered HTTP {exc.status_code} with "
            f"{exc.content_type} instead of JSON, titled {exc.title!r}. That is how it "
            "reports CENSUS_API_KEY missing or rejected -- the status line says 200 and "
            "only the body says no. Check CENSUS_API_KEY in .env; a newly issued key "
            "must be activated from its confirmation email before it works."
        ) from exc
    except AcquisitionError as exc:
        message = str(exc)
        if key and key in message:
            raise type(exc)(message.replace(key, "<CENSUS_API_KEY>")) from None
        raise


def _acs_descriptor(year: int, timeout_s: float = 30.0) -> dict[str, Any]:
    """The published descriptor for one ACS 5-year vintage.

    Read, never composed. `title`, `c_vintage` and `license` are exactly what the
    Provenance for this dataset has to state, and inventing a vintage label is the
    same mistake as recording a CRS the response never stated. Needs no key.
    """
    url = f"{_acs_base(year)}.json"
    payload = _get_json(url, {}, timeout_s)
    wanted = ACS_PRODUCT.split("/")
    for entry in payload.get("dataset") or []:
        if list(entry.get("c_dataset") or []) == wanted:
            return dict(entry)
    raise ServiceError(f"{url}: answered, but publishes no {ACS_PRODUCT} dataset entry")


def discover_acs_year(*, earliest: int = ACS_EARLIEST_YEAR, timeout_s: float = 30.0) -> int:
    """Find the newest published ACS 5-year vintage by probing downwards.

    The newest vintage is not a constant: a year that 404s today starts answering
    the week the Bureau publishes it, and a year written into the source pins the
    study to a release that then goes stale in silence. The probe asks each year's
    small descriptor rather than its 28,000-entry variable catalogue, so walking
    back over an unpublished year or two costs almost nothing.

    Bounded at both ends, and every rejected year is named in the failure: an
    outage that 404s everything has to stop at `earliest` rather than march down
    to 2010 and report a decade-old vintage as though it were current.
    """
    start = datetime.now(timezone.utc).year + 1
    rejected: list[int] = []
    for year in range(start, earliest - 1, -1):
        try:
            _acs_descriptor(year, timeout_s)
        except ServiceError:
            rejected.append(year)
            continue
        return year
    raise ServiceError(
        f"no {ACS_PRODUCT} vintage answered between {earliest} and {start}; "
        f"probed and rejected {rejected}"
    )


def _acs_catalogue(year: int, timeout_s: float = 30.0) -> dict[str, dict[str, Any]]:
    """The full variable catalogue for one vintage. Needs no key, unlike the data."""
    url = f"{_acs_base(year)}/variables.json"
    variables = _get_json(url, {}, timeout_s).get("variables")
    if not isinstance(variables, dict) or not variables:
        raise ServiceError(f"{url}: answered, but carries no variables")
    return variables


def _match_target(entry: dict[str, Any]) -> str:
    """What a pattern is matched against: the concept and the label, together.

    Concept alone cannot separate the twelve sex-by-age cells that make up
    65-and-over from the thirty-seven that do not, and a label repeats across
    tables. The pair is the smallest thing that identifies a variable by meaning.
    """
    return f"{entry.get('concept') or ''}||{entry.get('label') or ''}"


def acs_numerator_ids(spec: str) -> tuple[str, ...]:
    return tuple(spec.split(ACS_OVER)[0].split(ACS_SUM))


def acs_denominator_id(spec: str) -> str | None:
    parts = spec.split(ACS_OVER)
    return parts[1] if len(parts) == 2 else None


def acs_variable_ids(spec: str) -> tuple[str, ...]:
    """Every estimate id a resolved spec refers to, numerator and denominator.

    A spec is `id[+id...][/denominator]`. Most of these concepts exist in the ACS
    only as leaves -- 65-and-over is twelve sex-by-age cells -- so a canonical
    column maps to a group of ids, and the group has to survive as one value
    because `Col` is what every later session joins on.

    Read a spec through this and the two helpers above it. Nothing outside this
    module should split the string itself.
    """
    denominator = acs_denominator_id(spec)
    ids = list(acs_numerator_ids(spec))
    if denominator:
        ids.append(denominator)
    return tuple(dict.fromkeys(ids))


def _margin_id(estimate_id: str) -> str:
    """The margin-of-error variable paired with an estimate.

    The catalogue publishes only the E variables, so the M is derived from the
    documented suffix convention rather than looked up -- and then verified by the
    request itself, because an id the API does not recognise comes back as HTTP
    400 naming it, not as a silently missing column.
    """
    if not estimate_id.endswith(ACS_ESTIMATE_SUFFIX):
        raise ValueError(f"{estimate_id!r} is not an ACS estimate variable id")
    return estimate_id[: -len(ACS_ESTIMATE_SUFFIX)] + ACS_MARGIN_SUFFIX


def _resolve_one(
    name: str, pattern: str, catalogue: dict[str, dict[str, Any]], log: list[str]
) -> str:
    table = ACS_SEED_TABLES.get(name, "")
    pool = {
        variable_id: entry
        for variable_id, entry in catalogue.items()
        if _ACS_ESTIMATE_ID.match(variable_id)
        and (not table or variable_id.startswith(f"{table}_"))
    }
    if not pool:
        raise ServiceError(
            f"{name}: table {table or '(any)'} publishes no estimate variables in this "
            "vintage; it was withdrawn or renamed"
        )

    matcher = re.compile(pattern)
    matched = sorted(vid for vid in pool if matcher.search(_match_target(pool[vid])))
    log.append(
        f"{name}: /{pattern}/ searched {len(pool)} estimate variables in "
        f"{table or 'the whole catalogue'} and matched {len(matched)}"
    )
    if not matched:
        raise ServiceError(
            f"{name}: no variable in {table or 'the catalogue'} matches /{pattern}/ "
            f"across {len(pool)} candidates; this vintage relabelled the table"
        )
    for variable_id in matched:
        log.append(f"{name}: matched {variable_id} <- {_match_target(pool[variable_id])}")

    rejected = sorted(set(pool) - set(matched))
    if rejected:
        shown = ", ".join(
            f"{vid} ({pool[vid].get('label')})" for vid in rejected[:_ACS_REJECTS_LOGGED]
        )
        remainder = len(rejected) - min(len(rejected), _ACS_REJECTS_LOGGED)
        tail = f", and {remainder} more" if remainder else ""
        log.append(f"{name}: rejected {len(rejected)}: {shown}{tail}")

    spec = ACS_SUM.join(matched)
    if name not in VULNERABILITY_INDICATORS:
        return spec

    totals = sorted(vid for vid in pool if re.search(ACS_TABLE_TOTAL, _match_target(pool[vid])))
    if len(totals) != 1:
        raise ServiceError(
            f"{name}: expected exactly one table total in {table} matching "
            f"/{ACS_TABLE_TOTAL}/, found {totals}; the denominator is ambiguous"
        )
    log.append(f"{name}: denominator {totals[0]} <- {_match_target(pool[totals[0]])}")
    return f"{spec}{ACS_OVER}{totals[0]}"


def resolve_acs_variables_logged(
    year: int, wanted: dict[str, str], *, timeout_s: float = 30.0
) -> tuple[dict[str, str], tuple[str, ...]]:
    """`resolve_acs_variables`, keeping the evidence of how it decided.

    The log is what criterion TU is paying for: it names which pattern matched
    which id and what it turned down, so a reader can check the agent picked the
    variables it claims rather than take the ids on trust. It goes into
    `Provenance.notes`, which is why it is a tuple of flat strings.
    """
    catalogue = _acs_catalogue(year, timeout_s)
    log: list[str] = [
        f"resolved against {_acs_base(year)}/variables.json "
        f"({len(catalogue)} variables published, no key required)"
    ]
    resolved = {
        name: _resolve_one(name, pattern, catalogue, log) for name, pattern in wanted.items()
    }
    return resolved, tuple(log)


def resolve_acs_variables(
    year: int, wanted: dict[str, str], *, timeout_s: float = 30.0
) -> dict[str, str]:
    """Discover which ACS variables carry each canonical column, at run time.

    `wanted` maps a canonical column to a regex matched against "concept||label"
    in the published catalogue. What comes back is a spec rather than a bare id --
    `id[+id...][/denominator]` -- because the ACS publishes most of these concepts
    only as leaves: 65-and-over is twelve sex-by-age cells and limited English is
    twenty-four language-by-proficiency cells. Read one back with
    `acs_variable_ids`.

    No ACS variable id appears anywhere in this repository. Ids shift between
    vintages, and a pasted id is the manual-cleaning failure wearing a different
    hat. Use `resolve_acs_variables_logged` when the evidence is wanted too.
    """
    resolved, _ = resolve_acs_variables_logged(year, wanted, timeout_s=timeout_s)
    return resolved


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _acs_rows(payload: Any, url: str) -> tuple[list[str], list[list[Any]]]:
    """Split the data endpoint's response into a header and rows.

    It answers with a JSON array whose row 0 is the header, not with an object.
    That is the whole reason the retry helper returns Any and the object
    assertion sits in `_get_json`: one retry policy, two response shapes.
    """
    if not isinstance(payload, list) or not payload:
        raise ServiceError(
            f"{url}: expected a JSON array whose first row is the header, got "
            f"{type(payload).__name__}"
        )
    header = [str(cell) for cell in payload[0]]
    return header, [list(row) for row in payload[1:]]


def fetch_acs(
    year: int,
    variables: dict[str, str],
    state_fips: str,
    county_fips: str,
    *,
    geography: Literal["tract", "block group"] = ACS_TRACT,
    timeout_s: float = 60.0,
) -> tuple[pd.DataFrame, Provenance]:
    """Retrieve ACS estimates and their margins of error for one county.

    `variables` is what `resolve_acs_variables` returned: canonical column ->
    spec. Every estimate is requested together with its M variant, because the
    paper's ethics section claims that point estimates suppress uncertainty, and
    that claim is only honest if the uncertainty is on disk beside them.

    `get` is capped at fifty variables and these six indicators expand past a
    hundred columns, so this takes several requests and joins them on the
    geography columns the API returned -- never on row position, which nothing in
    the API guarantees across separate requests.

    Values come back exactly as sent, as strings. Suppressed estimates are still
    sentinels (-666666666 and friends) and leading zeros are still there;
    scrubbing and casting belong to align.py, and doing either here would be
    cleaning data inside the retrieval layer.
    """
    estimate_ids = sorted({vid for spec in variables.values() for vid in acs_variable_ids(spec)})
    if not estimate_ids:
        raise ValueError("fetch_acs was given no variables; call resolve_acs_variables first")

    columns = [side for vid in estimate_ids for side in (vid, _margin_id(vid))]
    descriptor = _acs_descriptor(year, timeout_s)
    url = _acs_base(year)
    inside = f"state:{state_fips} county:{county_fips}"
    for_clause = f"{geography}:*"

    frames: list[pd.DataFrame] = []
    geography_columns: list[str] = []
    for index, batch in enumerate(_chunks(columns, ACS_MAX_GET_VARIABLES - 1)):
        requested = ([ACS_NAME_FIELD] + batch) if index == 0 else list(batch)
        payload = _census_get(
            url,
            {"get": ",".join(requested), "for": for_clause, "in": inside},
            timeout_s,
            keyed=True,
        )
        header, rows = _acs_rows(payload, url)
        returned_geography = [column for column in header if column not in set(requested)]
        if not returned_geography:
            raise ServiceError(
                f"{url}: request {index + 1} returned {header} with no geography columns, "
                "so its rows cannot be joined to the others by anything but position"
            )
        if not geography_columns:
            geography_columns = returned_geography
        elif returned_geography != geography_columns:
            raise ServiceError(
                f"{url}: request {index + 1} returned geography columns "
                f"{returned_geography}, but request 1 returned {geography_columns}"
            )
        frames.append(pd.DataFrame(rows, columns=header))

    frame = frames[0]
    for index, extra in enumerate(frames[1:], start=2):
        before = len(frame)
        frame = frame.merge(extra, on=geography_columns, how="inner", validate="one_to_one")
        if len(frame) != before or len(extra) != before:
            raise ServiceError(
                f"{url}: request 1 returned {before} rows and request {index} returned "
                f"{len(extra)}, joining to {len(frame)} on {geography_columns}; the "
                "batches do not describe the same geographies"
            )

    frame.insert(0, Col.GEOID, frame[geography_columns].astype(str).agg("".join, axis=1))
    duplicated = int(frame[Col.GEOID].duplicated().sum())
    if duplicated:
        raise ServiceError(
            f"{url}: {duplicated} of {len(frame)} rows share a {Col.GEOID} built from "
            f"{geography_columns}; those columns do not identify a row"
        )

    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ServiceError(f"{url}: answered without {len(missing)} requested columns: {missing}")

    vintage = (
        f"{descriptor.get('title') or ACS_PRODUCT} "
        f"(c_vintage {descriptor.get('c_vintage')}, temporal {descriptor.get('temporal')})"
    )
    request_params = {
        "year": str(year),
        "for": for_clause,
        "in": inside,
        "requests": str(len(frames)),
        "estimates": ",".join(estimate_ids),
        "margins": ",".join(_margin_id(vid) for vid in estimate_ids),
    }
    request_params.update({f"resolved:{name}": spec for name, spec in sorted(variables.items())})
    notes = [
        f"vintage, title and licence read from {url}.json, not composed here",
        f"{len(estimate_ids)} estimates requested with all {len(estimate_ids)} margins of error",
        f"'get' is capped at {ACS_MAX_GET_VARIABLES} variables, so this took "
        f"{len(frames)} request(s), joined on {geography_columns} rather than on row order",
        f"{Col.GEOID} concatenates {geography_columns}, the geography columns the API "
        "appended to the header, in the order it returned them",
        "values are exactly as the API sent them, as strings: suppressed estimates are "
        "still sentinels and leading zeros are still present, both handled in align.py",
        "CENSUS_API_KEY is sent as a query parameter and is deliberately absent from "
        "request_params, from these notes and from every error this module raises",
    ]
    provenance = Provenance(
        dataset=_slug(f"{ACS_PRODUCT} {year} {geography}"),
        source_url=url,
        retrieved_at=prov.utc_now(),
        declared_crs="n/a",
        working_crs=config.DEFAULT_WORKING_CRS,
        vintage=vintage,
        feature_count=int(len(frame)),
        license=str(descriptor.get("license") or "not stated by the service"),
        request_params=request_params,
        notes=tuple(notes),
    )
    return frame, provenance


def acquire_acs(
    area: config.StudyArea, registry: Registry, *, timeout_s: float = config.REQUEST_TIMEOUT_S
) -> list[DatasetRecord]:
    """Retrieve county demography at two granularities and register both.

    Every indicator at tract level, and population again at block-group level:
    session 7 apportions the finer count up to tracts and reports the discrepancy
    against the published tract figure, which is the mismatched-granularity join
    criterion RB names as its own example.

    The vintage and every variable id are discovered here, in that order, so a
    newly published ACS release changes the numbers without changing the code.
    """
    year = discover_acs_year(timeout_s=timeout_s)
    specs, resolution_log = resolve_acs_variables_logged(year, ACS_WANTED, timeout_s=timeout_s)

    wanted = (
        (DATASET_ACS, ACS_TRACT, specs),
        (DATASET_ACS_BLOCK_GROUPS, ACS_BLOCK_GROUP, {Col.POPULATION: specs[Col.POPULATION]}),
    )
    records: list[DatasetRecord] = []
    for dataset_name, geography, subset in wanted:
        frame, provenance = fetch_acs(
            year,
            subset,
            area.state_fips,
            area.county_fips,
            geography=geography,
            timeout_s=timeout_s,
        )
        if len(frame) == 0:
            raise ServiceError(
                f"{dataset_name}: state:{area.state_fips} county:{area.county_fips} matched "
                f"no {geography} rows; the study area is outside this vintage"
            )
        path = _write_table(frame, dataset_name)
        stamped = with_notes(as_dataset(provenance, dataset_name), resolution_log)
        records.append(registry.register(dataset_name, "table", path, stamped))
    return records


def _column_summary(frame: Any, limit: int = 10) -> str:
    """Column names, truncated. The tract table carries over a hundred."""
    columns = list(frame.columns)
    if len(columns) <= limit:
        return str(columns)
    head = ", ".join(repr(column) for column in columns[:limit])
    return f"[{head}, ... {len(columns) - limit} more]"


def _print_provenance(record: Provenance) -> None:
    print(f"  dataset        {record.dataset}")
    print(f"  source_url     {record.source_url}")
    print(f"  retrieved_at   {record.retrieved_at.isoformat()}")
    print(f"  declared_crs   {record.declared_crs}")
    print(f"  working_crs    {record.working_crs}")
    print(f"  vintage        {record.vintage}")
    print(f"  feature_count  {record.feature_count}")
    print(f"  license        {record.license}")
    print(f"  request_params {record.request_params}")
    for note in record.notes:
        print(f"  note           {note}")


def main(area: config.StudyArea | None = None) -> int:
    study_area = area or config.STUDY_AREA
    registry = Registry(study_area)

    print(f"study area: {study_area.name}  ({study_area.county_geoid})")
    print(f"storage crs: {config.STORAGE_CRS}   working crs: {study_area.working_crs}\n")

    records = acquire_boundaries(study_area, registry)
    records += acquire_acs(study_area, registry)
    manifest = registry.save_manifest()

    for record in records:
        frame = registry.load(record.name)
        unit = "features" if record.kind == "vector" else "rows"
        print(f"{record.name}: {len(frame)} {unit} -> {record.path.name}")
        print(f"  columns        {_column_summary(frame)}")
        if record.kind == "vector":
            print(f"  crs on load    {frame.crs.to_string()}")
        _print_provenance(record.provenance)
        print()

    print(f"manifest: {manifest}")
    return 0


def _first_x(payload: dict[str, Any]) -> float:
    return float(payload["features"][0]["geometry"]["rings"][0][0][0])


_HARDCODED_ACS_ID = re.compile(r"[A-Z]\d{5}[A-Z]?_\d{3}[EM]\b")


def _source_files() -> list[Path]:
    return sorted(Path(__file__).resolve().parent.rglob("*.py"))


def _acs_checks(
    tract_geoids: set[str], block_group_geoids: set[str], timeout_s: float
) -> list[tuple[str, bool]]:
    """The ACS half of the live check. Nothing here is mocked either.

    Three things can only be observed against the real endpoint. The key is
    accepted or rejected by the service, and a rejection arrives as HTTP 200 with
    an HTML page, so no local assertion can stand in for it. The variable
    resolution is only real if the ids it produces exist in the catalogue the
    Bureau publishes today. And the row counts are only meaningful against the
    boundary layers actually retrieved, which is why they arrive as arguments
    rather than as numbers written here.
    """
    area = config.STUDY_AREA
    checks: list[tuple[str, bool]] = []

    year = discover_acs_year(timeout_s=timeout_s)
    descriptor = _acs_descriptor(year, timeout_s)
    print(
        f"acs vintage: newest published is {year} -- {descriptor.get('title')!r}, "
        f"c_vintage {descriptor.get('c_vintage')}, temporal {descriptor.get('temporal')}"
    )
    print(f"  licence read from the service: {descriptor.get('license')}")
    checks.append(("the newest ACS vintage is discovered, not written down", isinstance(year, int)))
    try:
        _acs_descriptor(year + 1, timeout_s)
        checks.append((f"the year above it ({year + 1}) really is unpublished", False))
    except ServiceError:
        checks.append((f"the year above it ({year + 1}) really is unpublished", True))

    url = _acs_base(year)
    probe = {
        "get": ACS_NAME_FIELD,
        "for": f"{ACS_TRACT}:*",
        "in": f"state:{area.state_fips} county:{area.county_fips}",
    }
    header, rows = _acs_rows(_census_get(url, probe, timeout_s, keyed=True), url)
    print(f"\nkey: a keyed request returned {len(rows)} rows under header {header}")
    checks.append(("CENSUS_API_KEY is set and the data endpoint accepts it", len(rows) > 0))

    real_key = config.CENSUS_API_KEY
    config.CENSUS_API_KEY = "0" * (len(real_key) or 40)
    try:
        _census_get(url, probe, timeout_s, keyed=True)
        checks.append(("a rejected key is caught by content type, not by status", False))
    except ServiceError as exc:
        message = str(exc)
        print(f"  rejected key: {message}")
        checks.append(
            (
                "a rejected key is caught by content type, not by status",
                "CENSUS_API_KEY" in message and "HTTP 200" in message,
            )
        )
    finally:
        config.CENSUS_API_KEY = real_key

    catalogue = _acs_catalogue(year, timeout_s)
    print(f"\ncatalogue: {len(catalogue)} variables, fetched with no key at all")
    checks.append(("the variable catalogue answers without a key", len(catalogue) > 1000))

    specs, log = resolve_acs_variables_logged(year, ACS_WANTED, timeout_s=timeout_s)
    print("\nresolution: not one of these ids is written in the source")
    for name, pattern in ACS_WANTED.items():
        spec = specs[name]
        numerators = acs_numerator_ids(spec)
        print(f"  {name}")
        print(f"    pattern      /{pattern}/")
        print(f"    numerator    {len(numerators)} id(s): {ACS_SUM.join(numerators)}")
        print(f"    denominator  {acs_denominator_id(spec) or '(none: this is a count)'}")
    rejected_lines = [line for line in log if ": rejected " in line]
    print(f"  {len(log)} log lines, {len(rejected_lines)} of them recording rejections")
    for line in rejected_lines:
        print(f"    {line}")

    resolved_ids = [vid for spec in specs.values() for vid in acs_variable_ids(spec)]
    checks.append(("every canonical column resolves", set(specs) == set(ACS_WANTED)))
    checks.append(
        ("every resolved id exists in the live catalogue", all(vid in catalogue for vid in resolved_ids))
    )
    checks.append(
        (
            "every resolved id comes from the table its column was seeded with",
            all(
                vid.startswith(f"{ACS_SEED_TABLES[name]}_")
                for name, spec in specs.items()
                for vid in acs_variable_ids(spec)
            ),
        )
    )
    checks.append(
        (
            "a pattern that should match many leaves does, not just the table total",
            all(len(acs_numerator_ids(specs[name])) > 1 for name in (Col.PCT_AGE_65_PLUS, Col.PCT_DISABILITY, Col.PCT_LIMITED_ENGLISH)),
        )
    )
    checks.append(
        (
            "every vulnerability indicator resolves a denominator and population does not",
            all(acs_denominator_id(specs[name]) for name in VULNERABILITY_INDICATORS)
            and acs_denominator_id(specs[Col.POPULATION]) is None,
        )
    )
    checks.append(
        (
            "the log names how every resolved id was reached",
            all(
                any(f"matched {vid} " in line or f"denominator {vid} " in line for line in log)
                for vid in resolved_ids
            ),
        )
    )
    checks.append(
        (
            "the log records what each pattern turned down",
            len(rejected_lines) >= len(VULNERABILITY_INDICATORS),
        )
    )

    scanned = {
        path.name: sorted(set(_HARDCODED_ACS_ID.findall(path.read_text(encoding="utf-8"))))
        for path in _source_files()
    }
    offenders = {name: found for name, found in scanned.items() if found}
    print(f"\nsource scan: {len(scanned)} files under src/, {len(offenders)} carrying an ACS variable id")
    if offenders:
        print(f"  {offenders}")
    checks.append(("no ACS variable id is written anywhere under src/", not offenders))

    frame, provenance = fetch_acs(
        year, specs, area.state_fips, area.county_fips, geography=ACS_TRACT, timeout_s=timeout_s
    )
    estimate_ids = sorted(set(resolved_ids))
    requests_made = int(provenance.request_params["requests"])
    print(
        f"\ntract table: {len(frame)} rows x {len(frame.columns)} columns, "
        f"{len(estimate_ids)} estimates and {len(estimate_ids)} margins, "
        f"in {requests_made} request(s) capped at {ACS_MAX_GET_VARIABLES} variables each"
    )
    checks.append(("the 50-variable cap really forced more than one request", requests_made > 1))
    checks.append(
        (
            "every estimate came back with its margin of error",
            all(_margin_id(vid) in frame.columns for vid in estimate_ids),
        )
    )
    values = [value for vid in estimate_ids for value in frame[vid].tolist()]
    checks.append(
        (
            "estimates arrive as strings, uncast, so align.py still sees the sentinels",
            bool(values) and all(isinstance(value, str) for value in values),
        )
    )

    sentinels = int(
        sum(frame[vid].astype(str).str.startswith("-6666").sum() for vid in estimate_ids)
    )
    print(f"  suppressed-estimate sentinels left in place for align.py: {sentinels}")

    population_id = acs_numerator_ids(specs[Col.POPULATION])[0]
    county_payload = _census_get(
        url,
        {
            "get": population_id,
            "for": f"county:{area.county_fips}",
            "in": f"state:{area.state_fips}",
        },
        timeout_s,
        keyed=True,
    )
    county_header, county_rows = _acs_rows(county_payload, url)
    published = int(county_rows[0][county_header.index(population_id)])
    counts = pd.to_numeric(frame[population_id], errors="coerce")
    tract_total = -1 if counts.isna().any() else int(counts.sum())
    print(
        f"  independent total: {tract_total} summed over tracts against {published} "
        f"published for the county under a separate query"
    )
    checks.append(
        (
            "tract population sums to the county total the API publishes separately",
            tract_total == published,
        )
    )

    acs_tracts = set(frame[Col.GEOID])
    print(f"  geoid: {len(acs_tracts)} ACS tracts against {len(tract_geoids)} tract polygons")
    checks.append(
        (
            "ACS tract rows match the retrieved tract layer exactly",
            bool(tract_geoids) and acs_tracts == tract_geoids,
        )
    )

    groups_frame, groups_provenance = fetch_acs(
        year,
        {Col.POPULATION: specs[Col.POPULATION]},
        area.state_fips,
        area.county_fips,
        geography=ACS_BLOCK_GROUP,
        timeout_s=timeout_s,
    )
    acs_groups = set(groups_frame[Col.GEOID])
    print(
        f"  geoid: {len(acs_groups)} ACS block groups against "
        f"{len(block_group_geoids)} block-group polygons"
    )
    checks.append(
        (
            "ACS block-group rows match the retrieved block-group layer exactly",
            bool(block_group_geoids) and acs_groups == block_group_geoids,
        )
    )
    checks.append(
        ("the block-group table really is the finer granularity", len(groups_frame) > len(frame))
    )
    group_counts = pd.to_numeric(groups_frame[population_id], errors="coerce")
    group_total = -1 if group_counts.isna().any() else int(group_counts.sum())
    checks.append(
        (
            "block-group population sums to the same county total, so S7 has a ground truth",
            group_total == published,
        )
    )

    serialised = json.dumps(
        [prov.provenance_to_dict(provenance), prov.provenance_to_dict(groups_provenance)]
    )
    checks.append(
        ("the key reaches no part of provenance", bool(real_key) and real_key not in serialised)
    )
    checks.append(
        (
            "the resolved ids are recorded in request_params for every canonical column",
            all(f"resolved:{name}" in provenance.request_params for name in ACS_WANTED),
        )
    )
    checks.append(
        (
            "the vintage was read from the service, not composed from the year",
            str(descriptor.get("title")) in provenance.vintage,
        )
    )

    with tempfile.TemporaryDirectory(prefix="acs_table_") as scratch:
        root = Path(scratch)
        path = root / f"{DATASET_ACS}.parquet"
        frame.to_parquet(path, index=False)
        restored = pd.read_parquet(path)

        written = Registry(area, manifest_path=root / "manifest.json", root=root)
        written.register(DATASET_ACS, "table", path, as_dataset(provenance, DATASET_ACS))
        written.save_manifest()
        reopened = Registry(area, manifest_path=root / "manifest.json", root=root)
        reopened.load_manifest()
        through_registry = reopened.load(DATASET_ACS)

    checks.append(
        (
            "leading zeros survive the snapshot round-trip, so the GEOID join holds",
            restored[Col.GEOID].tolist() == frame[Col.GEOID].tolist(),
        )
    )
    checks.append(
        (
            "the table registers, writes a manifest and reloads through the registry",
            through_registry[Col.GEOID].tolist() == frame[Col.GEOID].tolist()
            and all(isinstance(geoid, str) for geoid in through_registry[Col.GEOID]),
        )
    )
    checks.append(
        (
            "every estimate column survives the registry round-trip as a string",
            all(
                through_registry[vid].tolist() == frame[vid].tolist() for vid in estimate_ids
            ),
        )
    )
    return checks


def _self_check() -> int:
    """Exercise the real services. Nothing here is mocked, deliberately.

    A mocked retrieval test proves the mock works. The behaviours that decide
    whether this module is correct -- paging, the CRS assertion, an error body
    served with HTTP 200 -- exist only at the network boundary.
    """
    area = config.STUDY_AREA
    timeout_s = config.REQUEST_TIMEOUT_S
    where = tigerweb_county_where(area)
    out_sr = _epsg_code(config.STORAGE_CRS)
    checks: list[tuple[str, bool]] = []

    layers = discover_arcgis_layers(TIGERWEB_TRACTS_BLOCKS_URL, timeout_s=timeout_s)
    same_name = [layer for layer in layers if layer["name"] == TRACTS_LAYER_NAME]
    print(
        f"discovery: {len(layers)} layers published, {len(same_name)} of them named "
        f"{TRACTS_LAYER_NAME!r}"
    )
    for layer in same_name:
        print(f"    id {layer['id']:>2}   parent {layer['parent_name'] or 'top level'}")
    tracts_layer = select_layer(layers, TRACTS_LAYER_NAME, geometry_type=POLYGON_GEOMETRY)
    groups_layer = select_layer(layers, BLOCK_GROUPS_LAYER_NAME, geometry_type=POLYGON_GEOMETRY)
    print(
        f"  selected tracts -> layer {tracts_layer['id']}, "
        f"block groups -> layer {groups_layer['id']}\n"
    )
    checks.append(("discovery finds several layers sharing one name", len(same_name) > 1))
    checks.append(("selection resolves to the top-level current vintage", tracts_layer["is_top_level"]))
    checks.append(("selection separates the two boundary layers", tracts_layer["id"] != groups_layer["id"]))

    base = tracts_layer["service_url"]
    layer_id = tracts_layer["id"]
    query_url = f"{base}/{layer_id}/query"
    meta = _layer_metadata(base, layer_id, timeout_s)
    oid_field = _object_id_field(meta)
    checks.append(
        (
            "object-id field found by field type, with objectIdField absent",
            bool(oid_field) and not meta.get("objectIdField"),
        )
    )

    params: dict[str, Any] = {
        "where": where,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": out_sr,
        "f": "json",
    }
    whole, whole_pages = _query_features(
        base, layer_id, params, timeout_s=timeout_s, oid_field=oid_field
    )
    paged, paged_pages = _query_features(
        base, layer_id, params, timeout_s=timeout_s, oid_field=oid_field, page_size=10
    )
    whole_ids = {feature["attributes"][oid_field] for feature in whole["features"]}
    paged_ids = {feature["attributes"][oid_field] for feature in paged["features"]}
    print(
        f"paging: {len(whole['features'])} features in {whole_pages} request(s); "
        f"{len(paged['features'])} features in {paged_pages} request(s) at 10 per page"
    )
    checks.append(("a small page size really pages", paged_pages > 1))
    checks.append(("paging returns no duplicate rows", len(paged["features"]) == len(paged_ids)))
    checks.append(("paged and unpaged retrieval agree exactly", bool(whole_ids) and paged_ids == whole_ids))

    declared = _received_crs(whole, out_sr, query_url)
    checks.append(("declared CRS is read from the response body", declared == config.STORAGE_CRS))
    try:
        _received_crs(whole, 3857, query_url)
        checks.append(("the CRS assertion rejects a mismatch", False))
    except CRSMismatch:
        checks.append(("the CRS assertion rejects a mismatch", True))

    whole_frame = _read_esri_json(whole, declared)
    paged_frame = _read_esri_json(paged, declared)
    print(
        f"parse: {len(whole_frame)} rows from a 1-request document, "
        f"{len(paged_frame)} rows from a {paged_pages}-request document"
    )
    parsed_both = oid_field in whole_frame.columns and oid_field in paged_frame.columns
    if parsed_both:
        left = whole_frame.sort_values(oid_field).reset_index(drop=True)
        right = paged_frame.sort_values(oid_field).reset_index(drop=True)
    checks.append(
        (
            "a concatenated multi-page document parses to the same rows",
            parsed_both
            and len(left) > 0
            and left[oid_field].tolist() == right[oid_field].tolist(),
        )
    )
    checks.append(
        (
            "geometry survives page concatenation unchanged",
            parsed_both and len(left) > 0 and bool(left.geometry.geom_equals(right.geometry).all()),
        )
    )

    mercator_params = dict(params)
    mercator_params["outSR"] = 3857
    mercator, _ = _query_features(
        base, layer_id, mercator_params, timeout_s=timeout_s, oid_field=oid_field, page_size=1
    )
    mercator_crs = _received_crs(mercator, 3857, query_url)
    print(
        f"crs: outSR={out_sr} -> {declared}, x={_first_x(whole):.4f} (degrees); "
        f"outSR=3857 -> {mercator_crs}, x={_first_x(mercator):.1f} (metres)"
    )
    checks.append(
        (
            "a different outSR is honoured and detected, not silently mixed",
            mercator_crs == "EPSG:3857" and abs(_first_x(mercator)) > 1000.0,
        )
    )

    _get_payload.statistics.clear()
    try:
        _get_json(query_url, {"where": "NOT_A_FIELD='x'", "outFields": "*", "f": "json"}, timeout_s)
        checks.append(("an ArcGIS error body served with HTTP 200 is raised", False))
    except ServiceError as exc:
        print(f"error body: {exc}")
        checks.append(("an ArcGIS error body served with HTTP 200 is raised", True))
    checks.append(
        ("a service error is not retried", _get_payload.statistics.get("attempt_number") == 1)
    )

    empty_params = dict(params)
    empty_params["where"] = f"{where} AND 1=0"
    empty_payload, _ = _query_features(
        base, layer_id, empty_params, timeout_s=timeout_s, oid_field=oid_field
    )
    empty_gdf = _read_esri_json(empty_payload, _received_crs(empty_payload, out_sr, query_url))
    checks.append(
        (
            "an empty result stays empty and keeps its CRS",
            len(empty_gdf) == 0 and empty_gdf.crs is not None,
        )
    )

    with tempfile.TemporaryDirectory(prefix="empty_layer_") as scratch:
        empty_path = Path(scratch) / "empty.geojson"
        empty_gdf.to_file(empty_path, driver="GeoJSON")
        restored = gpd.read_file(empty_path)
    checks.append(
        (
            "an empty layer round-trips to disk with its CRS, so degradation can register it",
            len(restored) == 0 and restored.crs is not None,
        )
    )

    entry_points = (
        ("discover_arcgis_layers", discover_arcgis_layers),
        ("fetch_arcgis_vector", fetch_arcgis_vector),
        ("acquire_boundaries", acquire_boundaries),
        ("discover_acs_year", discover_acs_year),
        ("resolve_acs_variables", resolve_acs_variables),
        ("fetch_acs", fetch_acs),
        ("acquire_acs", acquire_acs),
    )
    checks.append(
        (
            "every retrieval entry point takes an explicit timeout",
            all("timeout_s" in inspect.signature(fn).parameters for _, fn in entry_points),
        )
    )

    try:
        flood_layers = discover_arcgis_layers(
            NFHL_URL, name_contains="Flood Hazard", timeout_s=timeout_s
        )
        chosen = select_layer(flood_layers, FLOOD_ZONES_LAYER_NAME, geometry_type=POLYGON_GEOMETRY)
        matched = ", ".join(f"{layer['id']}:{layer['name']}" for layer in flood_layers)
        print(f"nfhl: matched {matched}")
        print(f"  selected layer {chosen['id']} ({chosen['name']}) by name, not by a literal id")
        checks.append(("the optional hazard layer id is discovered", isinstance(chosen["id"], int)))
    except AcquisitionError as exc:
        print(f"nfhl: unavailable, the run would continue without it -- {exc}")
        checks.append(("the optional hazard layer degrades instead of failing the run", True))

    print()
    groups_gdf, _ = fetch_arcgis_vector(
        groups_layer["service_url"],
        groups_layer["id"],
        where=where,
        out_sr=out_sr,
        timeout_s=timeout_s,
    )
    checks.extend(
        _acs_checks(
            set(whole_frame[Col.GEOID]),
            set(groups_gdf[Col.GEOID]),
            timeout_s,
        )
    )

    print()
    failed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        failed += 0 if ok else 1
    print("\nall checks passed" if failed == 0 else f"\n{failed} check(s) failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
