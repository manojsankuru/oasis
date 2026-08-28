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
  CENTLAT, CENTLON, INTPTLAT, INTPTLON, OBJECTID, MTFCC, LSADC, FUNCSTAT`.
- `maxRecordCount` is 100000, so one county returns in a single request. Still
  implement `resultOffset` paging — the transfer county is the reason.

```
?where=STATE='{state_fips}' AND COUNTY='{county_fips}'
&outFields=GEOID,NAME,AREALAND,AREAWATER,INTPTLAT,INTPTLON
&outSR=4326
&f=geojson
```

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

## 2. Block groups — `block_groups`

- **VERIFIED 25 Aug 2026** (same service, layer `1`).
- Exists for exactly one reason: criterion RB names *"joining polygons at
  different administrative granularities"* as its example. Pull one attribute at
  block-group level, apportion it to tracts, and report the discrepancy against
  the directly published tract figure.
- Apportion by population weight, not by area. Record the method and the error
  in `AlignmentReport.apportioned` / `apportionment_error`.

## 3. Demography — `acs`

- **VERIFIED 25 Aug 2026 that a key is REQUIRED.** An unkeyed request returns
  an error page, not data. Free, instant: `https://api.census.gov/data/key_signup.html`.
- `https://api.census.gov/data/{year}/acs/acs5`
- Catalogue: `https://api.census.gov/data/{year}/acs/acs5/variables.json`

```
?get=NAME,{comma-separated variable ids}
&for=tract:*
&in=state:{state_fips}%20county:{county_fips}
&key={CENSUS_API_KEY}
```

> **Do not hardcode variable ids.** `resolve_acs_variables()` fetches
> `variables.json` and matches on `concept` and `label` at run time. Two reasons:
> ids shift between vintages, and "the agent discovers which variables it needs"
> is exactly the autonomy criterion TU is paying for. Seed the search with table
> prefixes — `B01003` population, `B17001` poverty, `B01001` age, `B18101`
> disability, `B08201` vehicle availability, `B16005` English proficiency — and
> let the resolver find the specific ids. Log the resolution in provenance.
>
> **Keep the margins of error.** Request the `M` variants alongside the `E`
> estimates. The paper's ethics section claims that point estimates suppress
> uncertainty; having the MOEs on disk is what makes that claim honest.
>
> **Sentinel values.** Suppressed estimates come back as `-666666666`,
> `-999999999`, `-888888888` and friends. They are numerically valid and
> catastrophically wrong if summed. `scrub_sentinels()` handles them and counts
> them; the count is reported.

## 4. Elevation raster — `elevation`

- **VERIFIED 27 Aug 2026** — service reachable, `Image` capability present,
  `maxImageWidth`/`maxImageHeight` both 8000, native `wkid 102100`.
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

- Choose `size` from the bbox extent and a target cell size (10–30 m is plenty).
  A county at 10 m will exceed 8000 px in one dimension — compute the size, and
  if it exceeds the cap either coarsen the cell size or tile. **Handle this in
  code**, because the transfer county will have a different extent and a
  hardcoded size is a hardcoded study area.
- Response is a GeoTIFF body, not JSON. Check `Content-Type` before writing;
  ArcGIS returns a JSON error body with HTTP 200 when a parameter is wrong,
  which is a silent failure worth catching explicitly (and worth a
  `docs/failures.md` entry the first time it happens).

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

- **UNVERIFIED from this session** (standard endpoint, widely used).
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
  fatal.
- `out center` gives ways a representative point, so nodes and ways can be
  treated uniformly as points.

## 6. Hazard polygons — `flood_zones` *(optional, and useful precisely because it might fail)*

- **UNVERIFIED** — `hazards.fema.gov` refused programmatic inspection from this
  session, so the layer id must be discovered, not assumed.
- `https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer`
- Call `discover_arcgis_layers(service_url, name_contains="Flood Hazard")` and
  match by name. **Never hardcode a layer id.** Autonomous discovery of an
  ArcGIS service is a behaviour the rubric rewards; a hardcoded `28` is the
  opposite of it.
- Make this dataset non-load-bearing. If it fails, the run continues on the
  raster hazard alone and the failure is logged. A pipeline that degrades
  gracefully and *says so* is criterion RB evidence; make sure the degradation
  path is exercised at least once on purpose.

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
