# Data contract

Six datasets. Every one is retrieved live by `src/acquire.py`, carries a
`Provenance`, and is snapshotted to `data/snapshot/` with `manifest.json`.
Nothing here is downloaded by hand.

**Verification status** is honest: `VERIFIED` means the endpoint was queried and
answered on the stated date. `UNVERIFIED` means the URL pattern is from
documentation or memory and the first thing Session 3–5 must do is call it. Treat
an UNVERIFIED row as a task, not a fact.

---

## Study area parameters

Everything below is driven from `config.py`. No county name, FIPS code, state
code, or bounding box appears anywhere else in the repository.

```python
STUDY_AREA = StudyArea(
    name="Charleston County, South Carolina",
    state_fips="45",
    county_fips="019",
    # bbox is DERIVED from the retrieved tract layer, never typed in
)
TRANSFER_AREA = StudyArea(name="...", state_fips="...", county_fips="...")
```

Pick the transfer county on Day 1 and never touch it again until Day 4. Choose a
coastal county in a *different state* — a different state means different local
GIS conventions, which is the point. Somewhere with a known data gap is better
than somewhere clean.

---

## 1. Census tract geometry — `tracts`

- **VERIFIED 25 Aug 2026.**
- `https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer/0/query`
- Layer `0` is current tracts. Layer `1` is current block groups (see §2).
  Layers 4/5, 7/8, 10/11 are ACS-2025, ACS-2024 and Census-2020 vintages —
  **the vintage you join to matters**; record which layer id you used in
  `Provenance.vintage`.
- Fields: `GEOID, STATE, COUNTY, TRACT, BASENAME, NAME, AREALAND, AREAWATER,
  CENTLAT, CENTLON, INTPTLAT, INTPTLON, OBJECTID, MTFCC, LSADC, FUNCSTAT`, plus
  `OID` (a string, not the object id) and `BLKGRP` on layer 1.
- **Every numeric-looking field is `esriFieldTypeString`.** `AREALAND`,
  `AREAWATER`, `CENTLAT`, `CENTLON`, `INTPTLAT` and `INTPTLON` all arrive as
  text, so they concatenate instead of summing and sort lexically. Cast them in
  `align.py`, at run time — measured 28 Aug 2026 from the layer metadata.
- `maxRecordCount` is 100000, so one county returns in a single request. Still
  implement `resultOffset` paging — the transfer county is the reason.

```
?where=STATE='{state_fips}' AND COUNTY='{county_fips}'
&outFields=*
&returnGeometry=true
&outSR=4326
&f=json
&orderByFields={object id field}
&resultOffset={n}&resultRecordCount={page}
```

`acquire.fetch_arcgis_vector` sends exactly this. Three departures from the
original sketch, each for a reason recorded below: `f=json` so the CRS assertion
is real, `outFields=*` so no column list has to be kept in step with the
service, and `orderByFields` so paging is stable.

The object-id field is **discovered**, not named: this layer omits the
`objectIdField` key from its metadata, so `_object_id_field` falls back to
scanning `fields` for `esriFieldTypeOID` and finds `OBJECTID`.

> **Trap.** The service advertises `wkid 102100` (Web Mercator). Omit `outSR`
> and you get metres where the rest of the pipeline expects degrees, with no
> error. Pass it explicitly, then assert `gdf.crs` matches what you asked for
> and record both in `Provenance.declared_crs` / `working_crs`. This is a real
> ingestion-time CRS mismatch and it belongs in the paper as one.

> **Measured, VERIFIED 28 Aug 2026** against this layer for Charleston County.
> The trap bites only with `f=json`:
>
> ```
> f=json     no outSR     wkid=102100     x = -8900562.9108   metres
> f=json     outSR=4326   wkid=4326       x =      -79.9551   degrees
> f=geojson  no outSR     no CRS stated   x =      -79.9551   degrees
> f=geojson  outSR=4326   no CRS stated   x =      -79.9551   degrees
> ```
>
> All four return HTTP 200. GeoJSON is specified as 4326, so the service ignores
> `outSR` there and the query above is safe as written.
>
> **But this weakens the assertion.** `f=geojson` states no CRS at all in the
> response, so there is nothing to compare the received CRS against and
> geopandas simply assumes 4326. "Assert the CRS received matches the CRS
> requested" is only a real check with `f=json`, where `spatialReference.wkid`
> comes back. Either request `f=json` and assert on `spatialReference.wkid`, or
> keep `f=geojson` and record in `Provenance.notes` that the CRS was assumed
> from the format rather than verified from the response. Do not claim an
> assertion the format cannot support.

> **Resolved 28 Aug 2026 (session 3).** `f=json`, and the assertion is real.
> GDAL's ESRIJSON driver parses the Esri FeatureSet, so nothing hand-rolls
> ring-to-polygon conversion. Confirmed the driver reads the document's
> `spatialReference` rather than assuming one: `outSR=5070` reads back as
> EPSG:5070 and omitting `outSR` reads back as EPSG:3857. The CRS on the frame
> is then set from the asserted wkid, so there is one source of truth for it.
>
> `_received_crs` compares the request against **both** `wkid` and `latestWkid`,
> because Esri codes are not EPSG codes — wkid 102100 is EPSG:3857 and
> `CRS.from_user_input("EPSG:102100")` raises.
>
> Measured live by `python -m src.acquire --check`: `outSR=4326` returns
> x = -79.9551 (degrees), `outSR=3857` returns x = -8900562.9 (metres), and
> asking the 4326 payload to satisfy a 3857 request raises `CRSMismatch`.

> **Paging, VERIFIED 28 Aug 2026.** `supportsPagination` is true and Charleston
> County's 99 tracts arrive in one request, as documented. The check forces a
> page size of 10 and confirms that 10 requests return the same 99 object ids
> with no duplicates — so the transfer county will not be the first time the
> paging path runs. The loop is bounded three ways (page cap, empty page, and a
> page whose rows were all seen before) because a service that ignores
> `resultOffset` returns page one forever with `exceededTransferLimit` stuck
> true.

> **Error bodies.** A bad `where` returns **HTTP 200** with
> `{"error":{"code":400,...}}` and no `features` key. Measured. `_get_json`
> raises on it rather than letting it reach disk, and does not retry it: only
> transport errors, 429 and 5xx are retried.

## 2. Block groups — `block_groups`

- **VERIFIED 25 Aug 2026** (same service, layer `1`).
- Exists for exactly one reason: criterion RB names *"joining polygons at
  different administrative granularities"* as its example. Pull one attribute at
  block-group level, apportion it to tracts, and report the discrepancy against
  the directly published tract figure.
- Apportion by population weight, not by area. Record the method and the error
  in `AlignmentReport.apportioned` / `apportionment_error`.

## 3. Demography — `acs`

- **VERIFIED 28 Aug 2026.** A key is REQUIRED for the data endpoint and is NOT
  required for the variable catalogue. Free, instant, but a new key must be
  activated from its confirmation email before it works:
  `https://api.census.gov/data/key_signup.html`.
- `https://api.census.gov/data/{year}/acs/acs5` — the year is discovered, below.
- Catalogue: `https://api.census.gov/data/{year}/acs/acs5/variables.json`
- Descriptor: `https://api.census.gov/data/{year}/acs/acs5.json` — vintage and
  licence come from here.

```
?get=NAME,{comma-separated variable ids and their M variants}
&for=tract:*
&in=state:{state_fips}%20county:{county_fips}
&key={CENSUS_API_KEY}
```

> **Verified live 28 Aug 2026 (session 4)**, by `python -m src.acquire --check`.
> The key was rejected earlier the same day and works once activated from its
> confirmation email; nothing below is from memory.
>
> **Vintage is discovered, not written down.** `acs5` answered for 2024 and
> 404'd for 2025 and 2026. `discover_acs_year()` probes downwards from next year
> and stops at the first year that answers, bounded at 2010. It probes
> `.../acs/acs5.json` (19 KB) rather than `variables.json` (several MB), and that
> descriptor is also where the vintage and the licence come from:
> `title` "ACS 5-Year Detailed Tables", `c_vintage` 2024, `temporal` 2024/2024,
> `license` CC0. Recorded as read, never composed.
>
> **The catalogue needs no key.** `variables.json` returned 28,475 variables
> unkeyed. Only the data endpoint is keyed.
>
> **The data endpoint returns a JSON ARRAY, not an object.** Row 0 is the
> header. This is why the retry helper returns `Any` and the object assertion
> sits in `_get_json` — one retry policy, two response shapes, because
> `faults.py` in S12 depends on there being exactly one choke point.
>
> **A missing or wrong key is HTTP 200 with `text/html`**, titled "Missing Key"
> or "Invalid Key". `raise_for_status()` passes and only the content type gives
> it away, so the content-type guard is the check and its message names
> `CENSUS_API_KEY` explicitly. Measured: the key is *not* echoed in that body,
> and every error this module raises is redacted anyway.
>
> **An unknown variable id is HTTP 400 `text/plain`** — `error: unknown variable
> 'B99999_001E'`. A different path from the key failure, which is why both are
> checked separately.
>
> **`get` is capped at 50 variables, `NAME` included.** 51 returns
> `error: 'get' is limited to 50 variables`. The six indicators expand to 56
> estimates plus 56 margins, so retrieval takes three requests. They are joined
> on the geography columns, never on row position: separate requests carry no
> ordering guarantee, and a positional concatenation would corrupt silently.
>
> **The geography columns are appended to the header** in hierarchy order —
> `state, county, tract` and then `block group` — after whatever was asked for.
> `fetch_acs` derives them by difference from the requested list rather than
> naming them, and builds `Col.GEOID` by concatenating them in that order.

**Do not hardcode variable ids.** `resolve_acs_variables()` fetches
`variables.json` and matches a regex against `"{concept}||{label}"` at run time.
Two reasons: ids shift between vintages, and "the agent discovers which variables
it needs" is exactly the autonomy criterion TU is paying for. The search is
seeded with table prefixes — `B01003` population, `B17001` poverty, `B01001` age,
`B18101` disability, `B08201` vehicle availability, `B16005` English proficiency
— and the resolver finds the ids inside them. `--check` scans every file under
`src/` for an ACS id and fails if it finds one.

> **A resolved value is a group of ids, not one id.** The ACS publishes most of
> these concepts only as leaves: 65-and-over is twelve sex-by-age cells and
> limited English is twenty-four language-by-proficiency cells. So
> `resolve_acs_variables` returns `id[+id...][/denominator]`, keyed by the exact
> `contracts.Col` name every later session joins on. Read one back with
> `acs_variable_ids()`; nothing outside `acquire.py` splits the string itself.
>
> **Patterns are anchored to the end of the label.** B08201 publishes "No vehicle
> available" once for the table and again inside each household-size block, so an
> unanchored pattern sums the same households five times. Depth is not uniform
> across tables, so a global depth rule does not work — each pattern is anchored
> instead.
>
> **The denominator is resolved too**, by matching `Estimate!!Total:` in the same
> table. Assuming `{table}_001E` happens to hold for all six and is still the
> same mistake as hardcoding an id.

**What it resolved on 28 Aug 2026** — this is *output*, recorded as the evidence
for criterion TU. It is not configuration, and pasting any of it back into the
code would be the failure this section exists to prevent.

| canonical column | numerator | denominator | universe |
| --- | --- | --- | --- |
| `population` | `B01003_001E` | — (a count) | total population |
| `pct_poverty` | `B17001_002E` | `B17001_001E` | poverty status determined |
| `pct_age_65_plus` | 12 ids, `B01001_020E`–`_025E` and `_044E`–`_049E` | `B01001_001E` | total population |
| `pct_disability` | 12 ids, the `With a disability` leaves of `B18101` | `B18101_001E` | civilian noninstitutionalised |
| `pct_no_vehicle` | `B08201_002E` | `B08201_001E` | households, not people |
| `pct_limited_english` | 24 ids, `"well"` / `"not well"` / `"not at all"` | `B16005_001E` | population 5 years and over |

Two of those universes are worth a sentence in the paper: the no-vehicle rate is
a **household** rate while the others are person rates, and limited English is
defined as speaking English less than "very well", which is the standard
definition and excludes the `"very well"` cells.

> **Verified against a figure this code did not compute.** Charleston County's
> 99 tracts sum to 420,264 population; its 261 block groups sum to 420,264; and a
> separate `for=county:` query returns 420,264. `--check` asserts all three.
> Chatham County, Georgia runs the same code with no change: 88 tracts, 246 block
> groups, 300,879 population.
>
> **Keep the margins of error.** Every `E` is requested with its `M`. The
> catalogue lists only the `E` variables, so the `M` id is derived by suffix and
> then verified by the request itself — an id the API does not recognise comes
> back as HTTP 400 naming it, not as a silently missing column. The paper's
> ethics section claims point estimates suppress uncertainty; having the MOEs on
> disk is what makes that claim honest.
>
> **Sentinel values.** Suppressed estimates come back as `-666666666`,
> `-999999999`, `-888888888` and friends. They are numerically valid and
> catastrophically wrong if summed. `fetch_acs` deliberately leaves them alone
> and returns every value as the string the API sent; `scrub_sentinels()` in
> `align.py` handles them and counts them, and the count is reported. Charleston
> had none in these 56 variables, so the transfer county is where that path first
> runs on real data.
>
> **The snapshot is parquet, not CSV.** County and tract codes are zero-padded
> and `pd.read_csv` reads them back as integers with the padding gone, which
> breaks the GEOID join in a way that looks like missing data rather than a
> format bug. `Registry` already accepted `.parquet`; `pyarrow` was the missing
> dependency and is now in `requirements.txt`.


## 4. Elevation raster — `elevation`

- **VERIFIED 28 Aug 2026** — `exportImage` returned real GeoTIFF bytes at the
  requested `imageSR`, not merely a reachable service. A 64×56 probe came back
  as 66,748 bytes beginning `II*\x00`, `EPSG:5070`, `float32`, `nodata -9999`,
  and the full study raster is on disk. `Image` capability present,
  `maxImageWidth`/`maxImageHeight` both 8000, native `wkid 102100`.
- **The advertised 8000 × 8000 cap is not the operative one.** Measured the same
  day: 3000×2366 (7.1 Mpx) succeeded, 4000×3154 (12.6 Mpx) returned HTTP 500
  "Error exporting image", and 8000×6309 (50.5 Mpx) returned HTTP 504 after 90 s.
  `acquire.MAX_EXPORT_PIXELS` therefore applies a second, lower budget before the
  request, and `_raster_grid` coarsens against whichever cap binds and records
  which one in Provenance. See `docs/failures.md`.
- The study extent is **126,789 m × 99,978 m** in EPSG:5070, computed at run time
  from the retrieved tract layer. An earlier planning note said 106,785 × 93,524;
  four independent methods (pyproj corners, `transform_bounds` densified and not,
  `rasterio.warp`) agree on the larger figure. The conclusion is unchanged — 10 m
  needs 12,679 px and blows the cap either way — but do not reuse the old number.
- `https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage`

```
?bbox={minx},{miny},{maxx},{maxy}
&bboxSR=4326
&size={width},{height}          # must be <= 8000 in each dimension
&imageSR=5070                   # ask for the metric CRS directly
&format=tiff
&pixelType=F32
&noData=-9999
&interpolation=RSP_BilinearInterpolation
&f=image
```

- Choose `size` from the bbox extent **reprojected into `imageSR` and measured
  in metres**. A size computed from the degree span is invariant 2 broken
  silently: the request succeeds, the raster is a handful of pixels, and only a
  strange zonal statistic ever hints at it. `acquire._metric_bounds` refuses a
  geographic or non-metre `out_sr` rather than computing anything.
- A county at 10 m exceeds 8000 px in one dimension — compute the size, and if it
  exceeds either cap, coarsen. **Handle this in code**, because the transfer
  county will have a different extent and a hardcoded size is a hardcoded study
  area. Coarsening shrinks both axes proportionally; clamping each axis to its
  own cap would square off a rectangular county. Record the requested *and* the
  effective cell size — a raster quietly coarser than asked for is a number the
  paper would get wrong.
- Response is a GeoTIFF body, not JSON. **Checking `Content-Type` is not enough,
  and measurement is why:** an over-cap `size`, an invalid `imageSR` and a
  three-number `bbox` each returned HTTP 200 with `Content-Type: image/tiff` and
  a ~150-byte JSON error document. Both the status line and the header report
  success. The TIFF magic bytes (`II*\x00` / `MM\x00*`, and the BigTIFF pair) are
  the only discriminator, so `acquire._expect_image` tests those and hands a JSON
  body to `_raise_for_arcgis_error` for the service's own reason. See
  `docs/failures.md`.
- Verify the written file, do not trust the request: open it with rasterio and
  assert the CRS is what `imageSR` asked for and the transform's pixel size
  matches the effective cell size. Same discipline as `_received_crs`.

### Inundation model

Bathtub: `depth = max(0, surge_height_m - elevation_m)`, computed per cell, then
summarised per polygon by `zonal_stats`. Scenarios come from `HazardScenario`
records; state the category-to-height mapping as an assumption and cite it.

This is a deliberate simplification — no hydraulic connectivity, no attenuation,
no timing. Say so in the limitations. It is defensible for a four-day build and
it delivers the raster-to-vector multiscale join that Track A actually names.

> If NOAA SLOSH MOM inundation rasters download cleanly on Day 1, use them
> instead and keep 3DEP for the elevation covariate. If they resist for more
> than thirty minutes, stop — the bathtub model is the plan, not the fallback.

## 5. Critical facilities — `facilities`

- **VERIFIED 28 Aug 2026** — the POST returned HTTP 200,
  `Content-Type: application/json`, 477 elements over the study bbox (253 nodes
  and 224 ways, every way carrying a `center`), across hospitals, schools,
  community centres and fire stations. Vintage and licence are read out of
  `osm3s.timestamp_osm_base` and `osm3s.copyright`, not composed.
- `https://overpass-api.de/api/interpreter`, POST, body is Overpass QL.

```
[out:json][timeout:120];
(
  node["amenity"~"^(hospital|school|community_centre|fire_station)$"]({s},{w},{n},{e});
  way ["amenity"~"^(hospital|school|community_centre|fire_station)$"]({s},{w},{n},{e});
);
out center tags;
```

- Bbox order in Overpass QL is `south,west,north,east` — **not** the
  `(minx,miny,maxx,maxy)` order used everywhere else in this repo. Convert in
  one place, in `acquire.fetch_osm`, and nowhere else.
- Send a descriptive `User-Agent`. The public instance rate-limits; a 429 is a
  normal outcome and the retry path must handle it rather than treating it as
  fatal. 429 is already in `acquire._RETRYABLE_STATUS`, so the single retry
  policy covers it — but the backoff budget is a few seconds and `/api/status`
  reports slots freeing in tens of seconds, so what actually carries this dataset
  under load is the caller's degradation path, not the retry.
- `out center` gives ways a representative point, so nodes and ways can be
  treated uniformly as points.
- **A `remark` in the body means the answer is truncated, and it arrives under
  HTTP 200.** Measured 28 Aug: a memory-starved query returned 200, valid JSON,
  zero elements and `remark: runtime error: Query ran out of memory`; a
  time-limited one returned 133,069 elements *and* a remark. Nothing but the
  remark separates a complete answer from a partial one, and a silently truncated
  facilities layer reads as a county with fewer schools.
  `acquire._raise_for_overpass_remark` refuses any response carrying one.
- The tag filter is a **parameter** (`fetch_osm(bbox, tags)`), not a literal in
  the retrieval layer; `acquire.FACILITY_TAGS` at the call site is the policy
  choice. Values are Overpass regexes and are refused, not escaped, if they carry
  a quote, a backslash or a control character — a tag value can reach the query
  builder from a dataset attribute rather than from a person.

## 6. Hazard polygons — `flood_zones` *(optional, and useful precisely because it might fail)*

- **VERIFIED 28 Aug 2026** — the service answered `?f=json` (v11.1). An earlier
  session could not reach this host at all, which is exactly why the dataset
  stays non-load-bearing.
- Discovery on `name_contains="Flood Hazard"` matches two layers:
  `27 Flood Hazard Boundaries` (polyline) and `28 Flood Hazard Zones` (polygon).
  `select_layer(..., geometry_type="esriGeometryPolygon")` resolves to 28
  without that id appearing anywhere in the code.
- `https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer`
- Call `discover_arcgis_layers(service_url, name_contains="Flood Hazard")` and
  match by name. **Never hardcode a layer id.** Autonomous discovery of an
  ArcGIS service is a behaviour the rubric rewards; a hardcoded `28` is the
  opposite of it.
- Make this dataset non-load-bearing. If it fails, the run continues on the
  raster hazard alone and the failure is logged. A pipeline that degrades
  gracefully and *says so* is criterion RB evidence; make sure the degradation
  path is exercised at least once on purpose.
- **It failed for real on 28 Aug, and the run continued.** The bbox query matches
  13,963 polygons, and the service answered
  `HTTP 200 ... code 500: Error performing query operation`. `acquire` registered
  an empty layer whose Provenance carries that sentence verbatim, and the other
  six datasets landed. `--check` also forces the same path against an unresolvable
  host so the recovery is tested rather than merely observed.
- The cause is the same shape as section 4's: layer 28 publishes
  `maxRecordCount: 2000` and cannot serve it. Measured at the same bbox —
  2000 per page fails in 16 s, 500 fails in 14 s, **100 succeeds in 4 s**. A later
  session that wants this layer should walk it at 100 per page (about 140 pages);
  it is not worth nine minutes of every acquisition run for a supporting layer.

---

## Deliberately not retrieved

| Dataset | Why not |
| --- | --- |
| Road network (OSMnx) | Network travel time is **Track B's** named challenge. It is also the single largest time sink available. Out. |
| FEMA National Risk Index | Download URL could not be verified from here. Try it for thirty minutes on Day 1; if it resists, derive a resilience proxy from ACS (tenure, insurance coverage, employment) and label it a proxy. |
| CDC/ATSDR SVI | Same. If it comes easily it is the better external benchmark; do not spend a morning on it. |
| Storm events, wind, wildfire, earthquake | One hazard. Out. |

---

## CRS policy

- **Storage / transport:** EPSG:4326. Every `BBox` in the codebase is 4326.
- **Working / metric:** EPSG:5070 (NAD83 CONUS Albers, equal area). Every area,
  distance, buffer, centroid and zonal operation happens here.
- Ask remote services for the CRS you want (`outSR`, `imageSR`) rather than
  reprojecting after the fact where the service supports it — then assert what
  you actually received. Record both in `Provenance`.
- EPSG:5070 covers CONUS. If the transfer county is outside CONUS, the working
  CRS becomes a `StudyArea` field rather than a global constant. Decide this on
  Day 1 when you pick the transfer county, not on Day 4.
