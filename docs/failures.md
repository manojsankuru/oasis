
## 2026-08-27 — intermittent tool-call miss on gemini-2.5-pro (S1)

`python -m src.test_api` check 3 returned FAIL once, immediately after eight
consecutive passes of the same check with no code change in between:

```
3. tool calling
  no tool_calls returned; this server may not support function calling
  FAIL
```

Not reproducible. A direct 8-run probe of the same prompt and the same single-tool
spec against `google/gemini-2.5-pro` on the Vertex OpenAI-compatible endpoint
returned a tool call 8/8 times. Observed rate is therefore roughly 1 miss in 10,
not a capability gap — the model occasionally answers from parametric knowledge
instead of calling the offered tool.

Consequence for the build: a single-shot check of "does this backend support tool
calling" can report a false negative. Any future gate that turns on one tool-call
observation should run it more than once before concluding. It also sets a floor
on agent reliability that the S10 sandbox instrumentation and the S12 fault runs
should measure rather than assume: an occasional silent no-call is a different
failure shape from an error, because nothing raises.

Not fixed. Recorded so the number is honest when the paper reports repair rates.

## 2026-08-28 — keyless Census API answers HTTP 200 with an HTML page (S3)

`config.setting_warnings()` claimed the Census API "allows keyless use up to a
daily cap". Measured, it does not:

```
GET https://api.census.gov/data/2023/acs/acs5?get=NAME,B01003_001E&for=tract:*&in=state:45 county:019
200  Content-Type: text/html
<html ...><title>Missing Key</title>
```

The status is 200 and the content type is HTML, so `raise_for_status()` passes
and `.json()` is where it finally fails, several frames away from the cause.
`docs/DATA.md` section 3 had it right; the warning string in `config.py` was
wrong and has been corrected.

This is the second member of the same family found today, after the ArcGIS error
body served with HTTP 200 in section 1. Both services signal failure in the body
and success in the status line. The retrieval layer therefore treats content type
and body shape as part of the success condition, not the status code alone --
which is a paper point, not just a bug fix.

Consequence for the build: S4 cannot start until CENSUS_API_KEY is set.

**Resolved later on 2026-08-28 (S4).** A key was set and still returned the same
HTML page, this time titled `Invalid Key` rather than `Missing Key` — a newly
issued key does nothing until it is activated from its confirmation email, and
the two states are indistinguishable from the status line. Once activated the
same request returned 99 rows of JSON. The guard stayed: `_census_get` raises a
message naming `CENSUS_API_KEY` for either title, and `--check` deliberately
sends a mangled key to prove that path still fires. The wasted time was spent
deciding whether the key or the code was wrong, which is the argument for the
error message naming the setting rather than saying "expected JSON".

## 2026-08-28 — exportImage serves a JSON error body as `Content-Type: image/tiff` (S5)

`docs/DATA.md` section 4 said "Check `Content-Type` before writing". Measured, that
guard catches nothing. Three separate bad parameters against
`3DEPElevation/ImageServer/exportImage`:

```
size=9000,9000   -> 200  Content-Type: image/tiff  156 bytes
  {"error":{"code":400,"extendedCode":-2147024809,"message":"Invalid or missing input
   parameters.","details":["The requested image exceeds the size limit."]}}

imageSR=999999   -> 200  Content-Type: image/tiff  144 bytes
  {"error":{...,"details":["'imageSR' parameter is invalid."]}}

bbox=1,2,3       -> 200  Content-Type: image/tiff  190 bytes
```

The status line says 200, the content type says TIFF, and the body is a JSON
refusal. Written to disk that is a 156-byte `.tif` that rasterio cannot open,
several sessions from the parameter that caused it. A fourth variant,
`format=not-a-format`, returned `Content-Type: application/octet-stream` carrying
a JPEG — so the header is not merely stale, it is unrelated to the payload.

Fixed in `acquire._expect_image`: the TIFF magic bytes are the test, and a body
that parses as JSON goes to the existing `_raise_for_arcgis_error` so the caller
reads the service's own reason rather than "that was not a TIFF". `--check`
exercises it against the live endpoint and asserts no file was written.

This is the third member of the family after the ArcGIS error body in section 1
and the keyless Census HTML page above — and the sharpest, because here the
`Content-Type` header itself is the thing that lies.

## 2026-08-28 — 3DEPElevation advertises an 8000 × 8000 cap it cannot render (S5)

The service publishes `maxImageWidth: 8000` and `maxImageHeight: 8000`. Sizing the
study county at 10 m and coarsening to fit exactly that cap gives 8000 × 6309.
That request does not fail fast; it fails slowly:

```
3000 x 2366  ( 7.1 Mpx)  200  29.9 MB   7 s
4000 x 3154  (12.6 Mpx)  500  "Error: Error exporting image"   after 23 s
5000 x 3943  (19.7 Mpx)  500  same                             after 29 s
8000 x 6309  (50.5 Mpx)  504  text/html gateway timeout        after 90 s
```

Both 500 and 504 are in `_RETRYABLE_STATUS`, correctly — they are transient
statuses in general. Here they are not transient at all, so the retry policy
spends its entire budget and then fails, three times slower than a single attempt.

Not fixed by retrying, and deliberately not fixed by tiling: mosaicking is beyond
the stated raster scope and this is the last session of day 1. `MAX_EXPORT_PIXELS`
applies a second budget *before* the request, sized inside the largest observed
success, and `_raster_grid` reports in Provenance which cap bound. The study raster
lands at 3185 × 2511 and 39.82 m against a requested 10 m, and both numbers are on
the record rather than one of them.

Worth stating plainly for the paper: a published service limit is a claim, not a
capability, and a client that trusts it degrades worse than one that measures.

Also corrected here: a planning note recorded the study extent as 106,785 × 93,524 m
in EPSG:5070. It is 126,789 × 99,978 m — pyproj corner transform, `transform_bounds`
with and without densification, and `rasterio.warp.transform_bounds` all agree. The
cap conclusion survives either way, but the old figure should not be reused.

## 2026-08-28 — Overpass reports truncation in the body, under HTTP 200 (S5)

A query given a one-second budget over the study bbox:

```
POST https://overpass-api.de/api/interpreter
200  Content-Type: application/json   25,828,061 bytes
  keys: version, generator, osm3s, elements, remark
  elements: 133069
  remark: runtime error: Query timed out in "print" at line 1 after 3 seconds.
```

133,069 elements and a partial answer, served as success. A memory-starved query
behaves the same way with zero elements. Nothing in the status, the headers or the
JSON shape distinguishes a complete result from a truncated one — only `remark`
does, and a facilities layer silently missing half its schools would have been
read as a county with fewer schools rather than as a failed retrieval. This is
`FaultKind = "truncated"` in `contracts.py`, arriving unprompted from the real
service before session 12 ever injects it.

Fixed in `acquire._raise_for_overpass_remark`. `--check` forces a real remark by
adding `[maxsize:1]` to the actual facilities query, so the guard is tested against
the service rather than a fixture.

## 2026-08-28 — NFHL publishes `maxRecordCount: 2000` and serves 100 (S5)

The optional flood-zone layer degraded during the first full six-dataset run. The
service's own words, carried into the empty layer's Provenance:

```
ServiceError: https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query:
HTTP 200 with an ArcGIS error body (code 500): Error performing query operation
```

The run continued and the other six datasets landed, which is the behaviour
section 6 of `docs/DATA.md` asks for. Chased far enough to know it is not our bug:
layer 28 (`Flood Hazard Zones`) publishes `maxRecordCount: 2000`, `_query_features`
was already paging at exactly 2000, and the same bbox query behaves like this:

```
resultRecordCount=2000  ->  error, 16 s
resultRecordCount=500   ->  error, 14 s
resultRecordCount=100   ->  200, 100 features, exceededTransferLimit=true, 4 s
```

So this is the second service in one session advertising a limit it cannot meet —
3DEPElevation with `maxImageWidth: 8000`, NFHL with `maxRecordCount: 2000`. The
pattern is worth a sentence in the paper: reading a limit from a service is
better than hardcoding it, and still not the same as knowing what the service can
do. Only a request finds that out.

Deliberately not fixed. Walking 13,963 polygons at 100 per page is about 140
requests and nine minutes on every acquisition run, for the one dataset the design
declares non-load-bearing. The page number is recorded in `docs/DATA.md` section 6
so a later session can spend that time on purpose if the layer is ever wanted.

