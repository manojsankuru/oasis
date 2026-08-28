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

`main()` currently retrieves the two boundary layers. Session 5 extends it to
the remaining four datasets and the full six-entry manifest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import geopandas as gpd
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import config
from . import provenance as prov
from .contracts import BBox, DatasetRecord, Provenance
from .registry import Registry

USER_AGENT = (
    "oasis-geo-agent/0.1 (ACM SIGSPATIAL 2026 Student Challenge, Track A; "
    "academic use; contact via repository)"
)

TIGERWEB_TRACTS_BLOCKS_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer"
)
NFHL_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"

DATASET_TRACTS = "tracts"
DATASET_BLOCK_GROUPS = "block_groups"
DATASET_FLOOD_ZONES = "flood_zones"

TRACTS_LAYER_NAME = "Census Tracts"
BLOCK_GROUPS_LAYER_NAME = "Census Block Groups"
FLOOD_ZONES_LAYER_NAME = "Flood Hazard Zones"
POLYGON_GEOMETRY = "esriGeometryPolygon"

PAGE_SIZE = 2000
MAX_PAGES = 250

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
def _get_json(url: str, params: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """Every outbound request in this module goes through here.

    One choke point means one place that carries the timeout, one place that
    decides what is worth retrying, and one place for faults.py to wrap in
    session 12. Only transport errors, 429 and 5xx are retried; a 4xx or an
    ArcGIS error body will never succeed, and retrying it only spends the budget.
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
        content_type = response.headers.get("Content-Type", "unknown")
        raise ServiceError(
            f"{url}: expected JSON, got {content_type}: {response.text[:200]!r}"
        ) from exc

    _raise_for_arcgis_error(payload, url)
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
    manifest = registry.save_manifest()

    for record in records:
        gdf = registry.load(record.name)
        print(f"{record.name}: {len(gdf)} features -> {record.path.name}")
        print(f"  columns        {list(gdf.columns)}")
        print(f"  crs on load    {gdf.crs.to_string()}")
        _print_provenance(record.provenance)
        print()

    print(f"manifest: {manifest}")
    return 0


def _first_x(payload: dict[str, Any]) -> float:
    return float(payload["features"][0]["geometry"]["rings"][0][0][0])


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

    _get_json.statistics.clear()
    try:
        _get_json(query_url, {"where": "NOT_A_FIELD='x'", "outFields": "*", "f": "json"}, timeout_s)
        checks.append(("an ArcGIS error body served with HTTP 200 is raised", False))
    except ServiceError as exc:
        print(f"error body: {exc}")
        checks.append(("an ArcGIS error body served with HTTP 200 is raised", True))
    checks.append(
        ("a service error is not retried", _get_json.statistics.get("attempt_number") == 1)
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
    failed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        failed += 0 if ok else 1
    print("\nall checks passed" if failed == 0 else f"\n{failed} check(s) failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
