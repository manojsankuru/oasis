
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


## 2026-08-29 — `make_valid` repairs a polygon into a line (S6)

**What happened.** A geometry probe over four degenerate polygons, run while
building `align.repair_geometry`, showed that `make_valid` does not promise to
hand back the dimension it was given:

```
repeated point     valid=False  -> Point            empty=False valid_after=True
collinear          valid=False  -> MultiLineString  empty=False valid_after=True
zero area sliver   valid=False  -> LineString       empty=False valid_after=True
nan coord          valid=False  make_valid raised GEOSException:
                   IllegalArgumentException: CGAlgorithmsDD::orientationIndex
                   encountered NaN/Inf numbers
```

The first draft of `repair_geometry_detailed` accepted a repair when the result
was valid and not empty. Every row above passes that test, so a census tract
whose ring doubles back would have been "repaired" into a LineString, kept in a
polygon layer, and reported an area — and therefore a zonal inundation depth and
an exposed population — of exactly zero, with nothing anywhere saying why. A
silent zero for a real populated unit is worse than a dropped one.

The fourth row is a second failure in the same call: `make_valid` raises rather
than returning something, so one malformed coordinate out of hundreds of features
ends the whole run. Removing the per-geometry fallback and re-running `--check`
reproduces it as `shapely.errors.GEOSException: Edge direction cannot be
determined because endpoints are equal`.

**Where.** `src/align.py`, `repair_geometry_detailed` and `_make_valid`.
Geopandas 1.1.4 / Shapely 2.1.2.

**Why.** `make_valid` guarantees validity, not dimensionality. Validity was
being used as a proxy for "this is still a usable polygon" and it is not one.

**Did the agent recover?** Yes, in one turn, and only because a mutation sweep
asked whether the check could fail. Repair now requires the result to still be
areal, drops what is not, and counts it in `AlignmentReport.geometries_dropped`
under its own `collapsed` reason. `_make_valid` retries geometry by geometry when
the vectorised call raises, marking the offenders null so they are dropped and
counted like any other unrepairable row.

**Kept as a paper failure case?** Yes — §3.7, and it is the concrete example
behind the claim that a silent zero is the characteristic GeoAI failure. Neither
symptom raises anything and neither is visible in a green test suite.

## 2026-08-29 — two of this module's own checks could not fail (S6)

**What happened.** `python -m src.align --check` reported 66/66 PASS on its first
run. Rather than take that, each function was deliberately broken in turn and the
check re-run. Twelve mutations, ten caught, two survived with exit code 0:

```
[SURVIVED] exit=0  derived percentage divides by the wrong thing
[SURVIVED] exit=0  sentinel summed into the derived indicator instead of propagating
```

The first mutation halved every vulnerability share. The only check covering
those columns asserted the values were percentages in range, and half of a
percentage is still a percentage, so it passed. The second changed the derived
sum's `min_count` so a scrubbed sentinel would be treated as absent rather than
propagating — an indicator would then shrink instead of going null, which is the
suppression bug the scrubbing exists to prevent, and nothing tested it at all.

**Where.** `src/align.py`, `_snapshot_checks`. The same shape as the vacuous
check S5 shipped, found one stage earlier this time.

**Why.** Both checks tested a property the correct answer happens to have —
"is a percentage", "column exists" — rather than the value itself. A property
test passes for a wide class of wrong answers.

**Did the agent recover?** Yes, in one turn. Each share is now re-derived through
a different expression and compared value by value; each numerator is bounded
against its own table total, which an over-matched regex would break; the
population is checked against the total published in a different ACS table and
against the same population summed at block-group granularity; and a sentinel
planted into one leaf of the real table must null that indicator for that tract
and leave the next tract alone. The sweep now stands at 24 mutations, 24 caught.

**Kept as a paper failure case?** Yes — §3.7. The useful number is not 66/66
PASS, it is 24/24 mutations caught, and the two runs differ by exactly the checks
that were worth writing.

## 2026-08-29 — coordinates reached an agent-visible layer as ordinary attributes (S6)

**What happened.** The `invariant-reviewer` subagent, run on `align.py` before
commit, found that the joined tract layer carried 141 columns and four of them
were coordinates:

```
tracts_joined: 99 rows x 141 cols
CENTLAT '+32.8070997'   CENTLON '-079.9522715'
INTPTLAT '+32.8070997'  INTPTLON '-079.9522715'
```

Invariant 3 says geometry never enters a model message. These are geometry
wearing a column name, and they read as text, so every guard that looks for a
geometry object lets them through. `describe_layer` and `run_spatial_code` in
session 9 operate on exactly these frames.

`acquire.fetch_arcgis_vector` had already flagged the gap in its own docstring —
"choosing columns is align.py's job, and a column list here would be a second
place to keep in step with the service" — and align.py had not taken the job.

The same review found four more defects, each of which had a green check sitting
over it: nulls created by dividing were uncounted while nulls created by
scrubbing were counted; the deferral of the session-7 fields lived only in a
side record and not in the `AlignmentReport` that leaves the module; the GEOID
guard tested dtype rather than values, so an object column holding Python ints
passed and was then misreported as a granularity mismatch; and three checks
asserted this county's particular zeros, so `--check` would have failed the
moment the FEMA service recovered.

**Where.** `src/align.py`, `align_snapshot`, `derive_acs_columns`,
`_geoid_strings`, `_snapshot_checks`.

**Why.** All five share one cause: a check written against the snapshot in front
of it rather than against the property it meant to test. A frame that happens to
contain no invalid geometry, a service that happens to be down, and a share that
happens not to land on a whole number all make a wrong implementation look right.

**Did the agent recover?** Yes, in one turn. Joined layers are now cut to GEOID,
the canonical `Col` columns and geometry; every null a division creates is
counted and warned; the deferred fields carry their status inside the frozen
report; the GEOID guard tests values and widths; and the county-specific
assertions became relational ones that hold on any county. The mutation sweep was
extended to 34 mutations covering each fix, and catches all 34.

**Kept as a paper failure case?** Yes — §3.7, and it is the strongest evidence
for the second feedback cycle: a reviewer with the invariants in hand found five
defects in a module whose own 66 checks were green, and the fix for every one of
them was a check that could fail.

## 2026-08-29 — the independent check was wrong, not the code (S7)

**What happened.** The acceptance gate for `zonal_stats` says a spatial result is
verified against an independently computed value. The independent value was a
cell-centre point-in-polygon average, written with shapely and numpy and never
calling `zonal_stats`. On the first run the two disagreed:

```
INDEPENDENT centre-in-polygon: n=285 min=1.462943 mean=3.230679 max=4.205657
rasterio.mask:                 n=286 min=1.462943 mean=3.227710 max=4.205657
```

One cell in 286, and a mean 0.09% apart. The obvious reading was a boundary
rasterisation rule — GDAL including a cell whose centre sits exactly on the edge
where shapely's `contains` excludes it. That reading was wrong. Swapping
`contains` for `covers` and then `intersects` changed nothing, which was the clue:
a boundary-rule difference would have moved with the predicate.

The real cause was the window. The independent path built its own read window
with `rasterio.windows.from_bounds(...).round_offsets().round_lengths()`, which
rounds *inward* and produced a 29x22 array where `rasterio.mask` had used 31x23.
The check was reading a slightly smaller piece of the raster than the polygon
covers, and losing a boundary cell. Padding the window by three cells and reading
`boundless=True` made the two agree to the last digit printed:

```
independent n=286 mean=3.22771025   rio.mask n=286 mean=3.22771025   dmean=0.0
```

**Where.** `_independent_zonal` in `src/align.py`.

**Why.** A verification written to be independent is still code, and it has its
own bugs. This one was in the half nobody thinks of as the hard part: not the
averaging, the windowing. Had the disagreement been accepted as a boundary rule
and papered over with a 1% tolerance, the check would have passed for the rest of
the build while silently permitting a real off-by-one in `zonal_stats`.

**Did the agent recover?** Yes. The padding is now load-bearing rather than
defensive and the docstring of `_independent_zonal` says so, naming this failure,
so a later session cannot tidy it away as belt-and-braces.

**Kept as a paper failure case?** Yes — §3.7. It is the cleanest example in the
build of a check that disagreed with the code and was itself at fault, which is
the failure mode that makes teams stop trusting their checks.

## 2026-08-29 — three mutations survived by raising the right exception for the wrong reason (S7)

**What happened.** The first mutation sweep after implementing `apportion` and
`zonal_stats` ran 64 mutations and caught 60. Four survived:

```
SURVIVOR: a statistic this module does not compute is accepted and returned empty
SURVIVOR: the elevation columns never reach the joined layers
SURVIVOR: a frame holding two geographic levels is accepted and rolled up anyway
SURVIVOR: a repeated coarse GEOID is tolerated, so there is no single published value
```

One was a bad mutation: the replacement expanded to `X if False else Y`, which is
just `Y`, so nothing was actually mutated. The other three were checks that could
not fail, and all three failed the same way. Each asserted only that a call raised
`ValueError`, and with the guard under test removed a *different* guard raised
`ValueError` a few lines later:

- delete the unsupported-statistic guard, and `_statistic` still raises on
  `"median"` with "no reduction named";
- delete the mixed-GEOID-width guard, and the width-ordering guard raises
  "must be the longer identifier" instead, because a frame holding widths 11 and
  12 reports its smallest width as the fine width;
- delete the repeated-coarse-GEOID guard, and pandas raises on constructing a
  frame from a Series with a duplicated index.

Every one of those checks was green, and every one would have stayed green over a
module that had lost the guard it was named after.

**Where.** `_zonal_checks` and `_apportion_checks` in `src/align.py`.

**Why.** `except ValueError: return True` tests that something went wrong, not
that the right thing went wrong. In a module whose guards are stacked — validate
the method, then the columns, then the widths, then the ordering — the type alone
carries almost no information, because every guard raises the same type.

**Did the agent recover?** Yes. The refusal helper now takes the phrase it
expects and returns `phrase in str(exc)`, so each check names the specific refusal
it is asserting. With that change and one rewritten mutation the sweep reports
64 of 64 caught.

## 2026-08-29 — a published population of zero would have made the apportionment error 0/0 (S7)

**What happened.** Not a run-time break; found by measuring the county before
writing the aggregation, and worth recording because the wrong version would have
produced the right number for the wrong reason and passed every check.

`AlignmentReport.apportionment_error` is a percentage per column. The natural
definition is the worst per-unit relative error,
`max(|aggregated - published| / published)`. Charleston County publishes a tract
whose population is zero:

```
tracts with population == 0: 1
      GEOID       population
65    45019990100          0
```

It is a 9900-series water tract, and its block groups sum to zero as well. So
that unit contributes `0/0`. Nothing raises: pandas `.max()` skips the resulting
NaN and returns `0.0` — the correct answer, arrived at over 98 units while
reporting a denominator of 99, with nothing anywhere saying a unit had been
dropped.

**Where.** `apportion_detailed` in `src/align.py`.

**Why.** The same shape as the S6 findings. A silently skipped NaN is a shrinking
denominator, and this module exists to refuse those.

**Did the agent recover?** It never shipped the broken version. The percentage is
taken over the units whose published value is non-zero, `units_undefined` counts
the ones excluded, and the maximum absolute difference — which stays defined at
zero population — is reported beside it. `format_report` prints all three, and a
fixture in `_apportion_checks` asserts that a unit publishing zero is excluded
*and* counted. The live report now reads:

```
population  max |aggregated - published| = 0 over 99 unit(s); 420,264 aggregated against 420,264 published
            the % is taken over the 98 unit(s) publishing a non-zero value; 1 publish zero, where a
            relative error has no meaning and the absolute difference above is the real number
```

**Kept as a paper failure case?** Yes — §3.7, alongside the S6 entries, as
evidence that the reported zeros in §2.3 were interrogated rather than accepted.

## 2026-08-29 — one line of S7 cannot be tested on this county, and the sweep hides it (S7)

**What happened.** Not a break. A limitation found while building the mutation
sweep, recorded because the mutation score would otherwise overstate what is
covered.

`align_snapshot` accumulates the threshold count across the two joined layers:

```python
report.units_below_cell_threshold += zonal.below_threshold
```

On Charleston County the true value is zero — the smallest tract covers 286 cells
and the smallest block group 87, against a `MIN_RASTER_CELLS` of 10. So mutating
that line to `= 0`, deleting the accumulation entirely, changes no observable
number and every one of the 142 checks stays green. The mutation survives, and it
survives because the county has nothing to count, not because the line is
unimportant.

The sweep therefore uses a mutation that *is* observable here —
`+= zonal.polygons`, which makes the report say 360 — and that one is caught. The
64/64 score is honest about the checks it runs and silent about this asymmetry.

**Where.** `align_snapshot` in `src/align.py`; the entry "the threshold count
reports polygons measured instead of polygons flagged" in `mutate.py`.

**Why.** A mutation can only be caught if it changes something the data can show.
A field whose true value is zero on the only county available is untestable at the
wiring level by any amount of mutation, and no amount of check-writing fixes that
— it is a property of the county, not of the code.

**What covers it instead.** Three separate things, none of which is the live
county:

1. The threshold logic itself is proven on a synthetic raster in `_zonal_checks`,
   where four of five fixture polygons fall under the threshold and the fifth
   clears it. The flag discriminates.
2. `format_report` prints the denominator, so a zero always arrives as "0 of 360
   polygon(s) over 2 layer(s), threshold 10 cells" rather than as "0".
3. If no raster is measured at all, `align_snapshot` appends a warning saying the
   zero means nothing was measured. That is the "not built looks like nothing to
   do" confusion the module was built to refuse, and it is the case a second
   county with a failed elevation retrieval will actually hit.

**Kept as a paper failure case?** Yes — §3.7, as the honest footnote to the
mutation numbers. A mutation score reported without naming what it cannot reach is
the same overstatement as a green test suite that mocks its own boundary.

## 2026-08-29 — a green suite and a zero-survivor mutation sweep still hid seven defects (S7)

**What happened.** `src/align.py` reached 142 checks all PASS, exit 0, and a
mutation sweep of 64 for 64 caught. The `invariant-reviewer` subagent, run on the
diff before commit, then found seven defects. Two matter more than the rest.

**The reported zero was not proven.** `apportionment_error` reads
`{'population': 0.0}` and the live check asserted a biconditional:

```python
(report.apportionment_error.get(Col.POPULATION) == 0.0)
== (apportion.max_abs_difference.get(Col.POPULATION) == 0.0)
```

beside `total_fine == total_coarse`. Take one tract aggregating to 100 against a
published 90 and another to 90 against a published 100. Both sides of the
biconditional are then non-zero, so `False == False` passes. The two errors cancel
in the sum, so the total-equality passes. Every one of the five live apportionment
checks passes while the module reports an 11% error as though it were zero. The
check was self-consistency dressed as verification — both sides were computed from
the same `difference` series, in the same loop.

Confirmed by mutation after the fix: attaching each aggregate to the wrong parent,
which leaves the county total exactly right, is caught only by the replacement
assertion "every tract's apportioned population equals its published one, unit by
unit". The old form did not move.

**The CRS scan had a hole this session widened.** `_contract_checks` greps an
explicit tuple of implementation parts for `.area`, `.buffer(`, `.centroid` and
the rest. The tuple never included the evidence dataclasses, and S7 added two more
of them plus four property bodies that run inside `format_report` and
`align_snapshot`. A metric operation written into any of those seven classes was
invisible to invariant 2's only mechanical guard. The failure mode is silent by
construction: an inclusion list exempts whatever nobody remembers to add.

The other five: `apportion` raised on a zero-row frame with a message saying the
frame held *more than one* geographic level, and `align_snapshot` did not catch
it, so a transfer county whose block-group join matched nothing would lose the
whole alignment stage including the tract work; the apportion branch had no
"nothing was apportioned" warning where the zonal branch had one, leaving two of
the three fields able to report `{}` for a reason nobody could distinguish;
`counts_agree` filtered on a column's existence inside its own generator, so it
returned `all([])` — True — precisely when the elevation join stopped running;
`units_below_cell_threshold` sums across granularities and rasters, so its check
silently assumed one raster and would break in S8; and a bare `except ValueError`
attributed every `rasterio.mask` failure to "the polygon is outside the raster",
which would have printed a confident and false explanation.

**Where.** `src/align.py`: `apportion_detailed`, `align_snapshot`, `_cells_under`,
`format_report`, `_snapshot_checks`, `_contract_checks`, `_module_functions`.

**Why.** Every one of the seven is a check or a guard that was written against the
county in front of it. Charleston's block groups nest perfectly, its elevation
raster covers every unit, its population is Census-controlled to agree exactly,
and no frame is ever empty. On that data a biconditional, an `all([])`, and a
message about the wrong failure all look identical to correct code. This is the
same root cause as the five S6 findings, one session later, in code written by
someone who had just read the S6 entry.

**Did the agent recover?** Yes, all seven, in one pass. The zero is now asserted
directly rather than relationally; `EVIDENCE_RECORDS` is named once and read by
both source scans so the two hand-maintained lists became one; empty frames are
refused as empty and `align_snapshot` guards the call; the apportion branch warns
when nothing was rolled up; `counts_agree` requires the column rather than
filtering on it; the polygon count scales by the number of rasters measured; and
the no-overlap catch matches `RASTER_NO_OVERLAP` and re-raises anything else. The
sweep is now 70 mutations, 70 caught, and the suite is 145 checks.

**What the mutation sweep could not have found.** Three of the seven were invisible
to it, and the reason is worth stating because the 64/64 figure implied otherwise:

1. A vacuous check whose blind spot another check happens to cover is reported
   CAUGHT. `counts_agree` returning `all([])` was masked by `elevation_named`.
2. A widened scan cannot be mutated back. Removing the evidence records from the
   scan tuple changes no number until something metric is written into one of
   them, so it survives as a no-op rather than a defect. Two such entries were
   deleted from `mutate.py` rather than left standing as survivors.
3. A field whose true value is zero on the only county available cannot have its
   wiring mutated observably — see the entry above on
   `units_below_cell_threshold`.

**Kept as a paper failure case?** Yes — §3.7, and it is now the primary evidence
for the second feedback cycle. The number worth quoting is not 145 checks or 70
mutations. It is that a reviewer holding the invariants found seven defects behind
both of those, that two of them made a *reported zero* untrustworthy, and that the
fix for every one was a check that could fail.

## 2026-08-29 — a check asserted the wrong tie rule, and the code was right (S8)

**What happened.** `vulnerability --check` failed on its first run:

```
  [FAIL] a column with one repeated value ranks every unit the same, not by row order
```

The check asserted that four identical values percentile-rank to `0.5` each. Pandas
returns `0.625`: the average rank of four ties is 2.5, and 2.5/4 is 0.625. The rule
under test — that a repeated value must not be ordered by row position — was correct
and the module implemented it correctly. The number written into the check was wrong.

**Where.** `_rank_checks` in `src/vulnerability.py`.

**Why.** The expected value was reasoned about rather than computed. "All tied, so
they all sit in the middle" is true of the *rank*, not of *rank over n*, and the two
differ by exactly the off-by-one that `(n+1)/2n` carries.

**Did the agent recover?** Yes, in one turn, and by checking rather than by
re-reasoning: `pd.Series([7.0]*4).rank(pct=True)` was run directly before the
expectation was changed. The assertion now states `(n+1)/2n = 0.625` with the
arithmetic written beside it, so a reader can see where the number comes from.

**Kept as a paper failure case?** Yes — §3.7, beside the S7 entry where the
independent check was wrong rather than the code. That is now twice in two sessions.
The pattern worth reporting is that a hand-written expected value is itself code that
can be wrong, and the only defence is that it fails loudly when it is, which is an
argument for stating arithmetic in a check rather than reading a number off a run.

## 2026-08-29 — the fixture agreed with itself, so a trade-off check could not discriminate (S8)

**What happened.** The same run also failed:

```
  [FAIL] at least one pair of presets ranks the same units differently
```

The four-unit fixture had four of its five indicators rising together, so every
weighting produced the same order. The check is the one that proves weights are
parameters rather than decoration, and on that fixture no weighting could have moved
anything.

**Where.** `_fixture` and `_preset_checks` in `src/vulnerability.py`.

**Why.** The fixture was written to make individual ranks easy to read by eye, and
easy-to-read meant monotone. A trade-off cannot appear in data that has no trade-off
in it. Note the failure direction: the check failed rather than passing vacuously,
because it asserts that a difference EXISTS. The same property asserted the other way
round — "the presets agree" — would have passed and proved nothing.

**Did the agent recover?** Yes. The fixture now moves poverty and no-vehicle against
the other three, which is exactly the axis `svi_themes` weights differently from
`svi_equal`: the two presets order those four units in opposite directions. The real
county was already discriminating — all three preset pairs differ in their top ten —
so the live check would have passed while the fixture check could not fail.

**Kept as a paper failure case?** Yes — §3.7, as the cheapest available illustration
of the difference between a check that can fail and a check that happens to pass.

## 2026-08-29 — the new CRS scan caught the person writing it (S8)

**What happened.** `risk --check` failed on its first run:

```
  metric operation outside the CRS helper: _resilience_checks: ['to_crs', 'buffer'] without to_working_crs
  [FAIL] every metric operation routes through to_working_crs
  [FAIL] the module never reprojects for itself
```

The offending code was in the self check, not the implementation: a fixture built its
unit polygons with `Point(...).buffer(1.0)` and produced a geographic frame with
`units.to_crs(...)` to prove that `resilience` reprojects what it is handed.

**Where.** `_resilience_checks` in `src/risk.py`; the scan is `verify.metric_bypasses`
and `verify.reprojections` in `src/verify.py`.

**Why.** The scan in `align.py` greps a hand-maintained tuple of implementation parts,
which the S7 review showed exempts whatever nobody remembers to add. The replacement
enumerates every function and method from the module object itself, so nothing is
exempt by omission — including check code. That is the intended behaviour and it fired
on its first real use, against its own author.

**Did the agent recover?** Yes, and by fixing the fixture rather than by exempting it.
The unit geometries are now built in EPSG:4326 and routed through `to_working_crs`,
which yields both the geographic frame the check needs and the projected one, with no
`.to_crs(` anywhere in the module. Adding an exemption would have reintroduced exactly
the hole the scan was written to close.

**Kept as a paper failure case?** Yes — §3.7. It is the clearest evidence available
that the guard is mechanical rather than decorative: it caught a violation the author
had just written and did not notice.

## 2026-08-29 — this county's raster has no nodata, so the rule protecting it is untestable on real data (S8)

**What happened.** Not a break. A limitation found while building `hazard.py`, recorded
because the check counts would otherwise overstate what is covered.

The bathtub is `depth = max(0, surge - elevation)`. The elevation nodata sentinel is
-9999.0, so a cell that is nodata and treated as ground reports ten kilometres of
water and becomes the deepest inundation in the county. Every derived raster therefore
carries the source nodata forward.

Measured directly off `3depelevation.tif`: **0 nodata cells and 0 non-finite cells of
7,997,535.** So deleting the entire nodata branch changes no number this county
produces. Every per-unit count, every fraction and every mean is identical with the
rule and without it.

**Where.** `Hazard._bathtub` and `Hazard._write` in `src/hazard.py`.

**Why.** A rule can only be exercised by data that violates the condition it guards.
The retrieved extent has no holes, so no amount of check-writing against this county
reaches that code — the same shape as the S7 entry on `units_below_cell_threshold`,
one session later and in a different module.

**What covers it instead.** A synthetic raster, and only that. `_bathtub_checks` puts a
-9999.0 cell and a NaN cell into a 3x3 grid and asserts that both are carried forward
into the depth raster AND into the wet mask, and `_surface_checks` writes a 10x10
fixture through the real `derive_surface`, reads both rasters back off disk, and
asserts that the holes read back as nodata and that the usable, nodata and non-finite
counts add up to the whole grid. Two mutations target the branch and are caught only
there.

**Kept as a paper failure case?** Yes — §3.7, filed with the S7 entry it repeats. The
honest form of the claim is: the nodata rule is proven on synthetic data and unproven
on Charleston, because Charleston cannot prove it. A transfer county with a coverage
gap would be the first real test.

## 2026-08-29 — eight of fifty-two mutations survived the first sweep (S8)

**What happened.** The five analysis modules reached 145 + 58 + 57 + 53 + 31 checks,
all PASS, exit 0. `python mutate.py` then reported **44 of 52 caught** on the four new
modules. The eight survivors, and what each turned out to be:

1. *"the two rasters' cell counts are compared in total rather than per unit"* — the
   `torn` fixture had ONE polygon, and with one unit a total and a per-unit comparison
   are the same comparison. The check could not express the failure it was written
   for. Fixed with two bands whose counts disagree in opposite directions: 20 vs 19
   and 19 vs 20, totalling 39 = 39.
2. *"the degraded flood layer is recognised by its row count instead of its flag"* — on
   this county `flood_zones` is degraded AND holds zero features, so the two rules give
   the same answer everywhere. Fixed with a fixture registry holding a layer that
   retrieved cleanly and found nothing.
3. *"deriving a surface from a degraded raster is allowed"* — the check passed
   `flood_zones`, which trips the earlier `kind != "raster"` guard, so the refusal it
   observed was not the one it named. Fixed with a degraded *raster* fixture, and the
   two refusals are now asserted separately by message.
4. *"the incomplete unit is scored anyway"* — **not a defect.** A Float64 weighted sum
   already propagates `pd.NA`, so deleting `.where(complete)` returns byte-identical
   answers. The entry was a no-op, which the harness's own rule says does not belong in
   it. Replaced with `.fillna(0.0)`, which is the edit the line actually defends
   against and is caught.
5, 6. *"exposed population is the population NOT on flooded land"* and *"...is the whole
   population wherever any of the unit floods"* — both survived behind bounds. "Exposed
   never exceeds population" and "the two estimates disagree" stay true under both
   errors. Nothing pinned the value. Fixed with a synthetic county whose block groups
   are fully wet, half wet and dry by construction and whose exposed population is
   stated as 3,500 and 1,250 before the call is made.
7. *"a tract reporting more exposed residents than residents is allowed through"* — the
   guard cannot fire on real data, because block-group population is Census-controlled
   to sum to the tract estimate. Fixed by handing the same fixture a tract frame whose
   published population is smaller than its children's.
8. *"the written table carries whatever columns the frame happened to hold"* — the risk
   frame happens to carry exactly the reported columns in exactly the reported order,
   so selecting them and not selecting them are indistinguishable. The guard is real —
   it stops a column added upstream reaching the deliverable — and needed a fixture
   with a spare column to become observable.

**Where.** `_surface_checks`, `_degradation_checks` in `src/hazard.py`;
`_exposure_checks` (new) in `src/risk.py`; `_refusal_checks` in `src/pipeline.py`;
one entry in `mutate.py`.

**Why.** Six of the eight are the same cause in different clothes: a check written
against the county in front of it. Charleston has one degraded layer that is also
empty, block-group populations that are controlled to agree exactly, a raster with no
holes, and a risk frame whose columns already match the deliverable. On that data a
wrong rule and a right one produce the same table. The other two are a fixture too
small to express the failure, and a mutation that was not a mutation.

**Did the agent recover?** Yes, all eight, and the count is now 122 of 122 across five
modules. Every fix was a fixture that makes the rule falsifiable, not a loosened
assertion.

**Kept as a paper failure case?** Yes — §3.7, and it is the strongest single piece of
evidence for the verification argument. The number worth quoting is not that the suite
was green. It is that a green suite of 344 checks hid eight rules that could not fail,
that six of the eight were invisible because the study county cannot express the
error, and that the fix in every case was synthetic data built to disagree.

## 2026-08-29 — the trade-off table compared half of each weighting (S8)

**What happened.** Found by reading, not by a failing check. Each `WeightPreset`
carries two halves: weights over the five indicators, which produce
`Col.VULNERABILITY`, and weights over the four objective terms, which decide how that
column trades off against hazard, exposure and resilience. `compare_presets` took a
frame whose vulnerability index had ALREADY been computed under one preset, so only
the objective half could vary.

`svi_equal` and `svi_themes` carry identical objective weights and differ only in how
they weight the indicators — that is the whole point of the pair, since they are two
published rules from one source. They returned identical rows. Two of the three
presets were indistinguishable in the deliverable, while the report presented them as
compared.

The gate still passed: `evacuation_capacity` moves the objective weights, so "the
ranking changes when a weight changes" was satisfied by one preset out of three.

**Where.** `Risk.compare_presets` in `src/risk.py`, and both its callers.

**Why.** The components are expensive to recompute and the frame is the natural thing
to pass, so the frame was passed. Nothing in the signature said that the frame already
had a weighting baked into it.

**Did the agent recover?** Yes. `compare_presets` now takes the units the frame was
built from and recomputes the index per preset; only the vulnerability column depends
on the indicator weights, so the rasters are not touched again. Measured effect on the
real county: `svi_equal` and `svi_themes` differ in **2 of their top 10 tracts** with
the units passed and **0** without, and the county-wide displacement count rose from
15 to 24. Two checks now assert exactly that contrast, and `pipeline` asserts
generically that a preset pair differing ONLY in indicator weights ranks the county
differently in every scenario.

**Kept as a paper failure case?** Yes — §3.7. It is the clearest example in the build
of a criterion-SG failure that every mechanical check passed: the trade-off table
existed, was populated, and reported three weightings, two of which had not actually
been varied.

## 2026-08-29 — the CRS scan is satisfied by one mention, and one argument went unchecked (S8)

**What happened.** The `invariant-reviewer` run found this behind 366 green checks and
a 125-of-125 mutation sweep. `Risk.resilience` reprojects two arguments:

```python
placed = self.aligner.to_working_crs(units)
points = self.aligner.to_working_crs(facilities)
```

`verify.metric_bypasses` asks whether a function that performs a metric operation
mentions `to_working_crs` **anywhere in its source**. Both calls are in the same
function, so either one alone satisfies the scan. The fixture in `_resilience_checks`
handed a geographic frame to `units` only; `facilities` was always built pre-projected.

The reviewer confirmed it empirically: patching the `facilities` line to `points =
facilities` and running `python -m src.risk --check` exits 0 with everything PASS,
including "every metric operation routes through to_working_crs". A mutation for that
exact line had been written into `mutate.py` earlier in the session with a comment
claiming the geographic-frame fixture would notice. It would not have. It would have
been reported as a survivor on the next sweep.

**Where.** `Risk.resilience` and `_resilience_checks` in `src/risk.py`;
`verify.metric_bypasses` in `src/verify.py`.

**Why.** The scan is a per-function string test, and a function with two frames to
project has two obligations and one observable. This is the same shape as every other
entry in this file: the guard is real, and the thing that would have caught its failure
is a fixture nobody wrote.

The practical consequence is worse than a style violation. `gpd.sjoin` does not raise
on a CRS mismatch — it warns and returns no rows — so every unit would have read as
reaching zero facilities, ranked identically on resilience, and the risk table would
have looked entirely plausible. A sandbox script in S10 calling `resilience` with
facilities straight from `acquire.fetch_osm`, which returns EPSG:4326 per the frozen
`Acquirer` protocol, is exactly how that would happen.

**Did the agent recover?** Yes. The fixture now defines BOTH frames in degrees and
projects them here, then calls `resilience` three more times — geographic units,
geographic facilities, and both at once — asserting each matches the projected result.
The mutation is now caught, and the misleading comment in `mutate.py` is replaced with
what actually happened.

**Kept as a paper failure case?** Yes — §3.7. It is the third session running in which
a reviewer holding the invariants found something a green suite and a zero-survivor
sweep did not, and the first in which the defect was in a guard rather than in the code
the guard watches.

## 2026-08-29 — the verification harness was hardcoded to this county's tract count (S8)

**What happened.** Same review. Five assertions across three modules compared against
Charleston's live tract count rather than deriving it:

```
src/vulnerability.py   evidence.units == 99, set(denominators.values()) == {98},
                       int(scored.notna().sum()) == 98, ranks... == 98
src/risk.py            len(frame) == 99
src/pipeline.py        len(frame) == 99, compared == 99 * len(result.tables)
```

These are not county names or FIPS codes, so `verify.study_area_tokens` does not see
them — it scans for the tokens `config` publishes, and "99" is not one of them.

**Where.** `_county_checks` in `src/vulnerability.py` and `src/risk.py`;
`_written_checks` and `_gate_checks` in `src/pipeline.py`.

**Why.** Every one was written while reading a real report, and 99 was on the screen.
Criterion RB asks the system to run on a second county with no code change; nothing
asked the same of the thing that verifies it.

The consequence is specific and bad: `mutate.py`'s `mutate_module` refuses to run a
single mutation if the baseline `--check` is not green. On Chatham County the baseline
would fail on tract count alone, so the entire mutation harness for three modules would
stop working with no defect present — and the S13 transfer run is the session that
would discover it.

**Did the agent recover?** Yes. Every literal is now derived from the loaded frame:
scored plus unscored must account for every unit, each rank denominator must equal the
count publishing that indicator, and the pipeline's comparison denominator is summed
from the tables it actually built. The county-specific numbers are still PRINTED, so
the report reads the same; they are no longer asserted.

Two checks were deliberately weakened in the process and it is worth saying so. "Exactly
one tract is unscored" became "no unscored tract carries any residents", and "every
index lies in (0, 1] over 98 units" became "...over the units reported as scored". The
discrimination that assertion used to provide has not been lost: `_null_checks` proves
the null policy on a synthetic frame where the answer is stated, and that fixture is
county-independent.

**Kept as a paper failure case?** Yes — §3.7, as the counterexample to the assumption
that "runs on a second county" only needs to hold of the analysis code.

## 2026-08-30 — the coordinate guard refused two keys in the tool it was written to protect (S9)

**What happened.** `tools --check` failed on its first run, on the check that walks
every tool's serialised result looking for a coordinate:

```
  [FAIL] no serialised result carries a coordinate, by key, by value or by shape
    describe_alignment: result.geometry: key could name a coordinate
    describe_alignment: result.geoid_audit.unmatched_geometry_side: key could name a coordinate
```

Neither key carried a coordinate. `result.geometry` held four repair counts, and
`unmatched_geometry_side` held the GEOIDs that have a boundary and no attributes. But
`pipeline.COORDINATE_PATTERN` matches `geometry` on an underscore boundary wherever it
appears, and the rule the guard enforces is about what a key could name, not about what
this particular key happens to hold today. A reviewer reading a model message cannot
tell the two apart, and neither can the next person to put something in that field.

**Where.** `describe_alignment` in `src/tools.py`; the pattern is
`pipeline.COORDINATE_PATTERN`, imported rather than rewritten.

**Why.** The keys were named after the thing they describe, which is the natural way to
name them, and the guard had been written twenty minutes earlier by the same hand. This
is the third session running in which a scan fired on its own author: S8's CRS scan
caught a fixture that reprojected for itself, and the same session's study-area scan is
what would have caught the county name that had been written into a docstring in this
module — found by running that scan's own grep by hand before the check did.

**Did the agent recover?** Yes, by renaming rather than by exempting. `geometry` became
`repairs` and `unmatched_geometry_side` became `unmatched_boundary_side`, which say what
the fields hold more precisely than the originals did. An exemption list would have
reintroduced exactly the hole the guard closes, and would have had to be maintained by
whoever adds the next field.

**Kept as a paper failure case?** Yes — §3.7, filed with the S8 CRS-scan entry. The
pattern worth reporting is that a mechanical guard written from an invariant catches its
author before it catches anybody else, and that this is evidence the guard is real
rather than decorative.

## 2026-08-30 — a tool result was thirty-one kilobytes of provenance notes (S9)

**What happened.** The same run failed a second check:

```
  [FAIL] no result is large enough to crowd the run's remaining turns
  list_datasets           31,427 bytes serialised
```

`list_datasets` is the tool the system prompt tells the model to call first. It returned
every registered dataset with its full `Provenance`, including the free-text `notes`
each retrieval accumulates — which service paged short of its advertised
`maxRecordCount`, which ACS variable was resolved by matching which label, what the
error body said when FEMA answered HTTP 200 with an ArcGIS error. Across seven datasets
that is thirty-one kilobytes, spent before the model has asked anything, out of a loop
bounded at six iterations.

Nothing about it is wrong. Every note is true, retrieved rather than written, and
exactly the provenance invariant 6 exists to preserve. It was still a defect: the first
tool call would have consumed most of the context the run had to reason in.

**Where.** `_provenance` and `list_datasets` in `src/tools.py`.

**Why.** Invariant 6 says every dataset carries a provenance record and criterion TU
says the model must be able to cite its sources, so the obvious implementation returns
the record. What neither says is that a model message has a budget, and that an index
and a detail view are different things. No check in this project had ever measured the
size of a tool result, because until this session there were no tool results.

**Did the agent recover?** Yes. `_provenance` takes `with_notes`; `list_datasets` is an
index and returns a note count, `describe_layer` is the detail view and returns the
notes capped at six with the number withheld named beside them. Measured effect:
`list_datasets` fell from 31,427 bytes to 3,491, and a check now asserts that no tool
result exceeds twenty-four kilobytes, printing every result's size beside it so the
figure is visible rather than merely asserted.

**Kept as a paper failure case?** Yes — §3.7, as the one failure in this build that is
about the interface between the analysis and the model rather than about the analysis.
The honest form of the lesson is that "return the provenance" and "return a citable
summary of the provenance" look like the same requirement and are not.

## 2026-08-30 — a check demanded that two presets differ in the half they are designed to share (S9)

**What happened.** Two more checks failed on the same run, both because the check was
wrong and the code was right:

```
  [FAIL] naming a weighting on risk_scenario reaches the score, not only the label
  [FAIL] the weights that were used are reported, whatever their source
```

The first picked "some preset other than the default" and asserted that its component
weights differed. It picked `svi_themes`, which carries *identical* objective weights to
`svi_equal` on purpose — that is the entire point of the pair, and the property S8's
trade-off fix depends on. The assertion demanded a difference in the one place the two
presets are built to agree.

The second summed the reported weights and required them to equal one within 1e-9. The
weights reported to the model are rounded to six decimal places for the message, and
five rounded weights sum to 1.000002. The check was asserting that the reported number
is the unrounded one, which it deliberately is not.

**Where.** `_weighting_checks` in `src/tools.py`.

**Why.** Both were written by reasoning about what ought to be true rather than by
naming which property was under test. This is the third time in three sessions that a
hand-written expected value has been the thing that was wrong — the S8 tie-rule entry
and the S7 independent-check entry are the other two — and the direction of the failure
is the same each time: the check failed loudly rather than passing vacuously, because
each asserts that something must EXIST or must EQUAL a stated number.

**Did the agent recover?** Yes, and by splitting the first check rather than loosening
it. The presets are now selected by the property under test: the one whose objective
weights match the default proves that a difference in indicator weights alone still
moves the score, and the one whose objective weights differ proves that the moved
weights are reported. Together they pin both halves of a weighting, which one comparison
against one preset could not. The tolerance on the second is now 1e-5 with the rounding
named beside it.

**Kept as a paper failure case?** Yes — §3.7, beside the S8 entry. The count worth
quoting is that of the five checks that failed on this module's first run, three were
defects in the checks and two were defects in the code.

## 2026-08-30 — four mutations survived, and three of them survived the same way (S9)

**What happened.** `src/tools.py` reached 95 checks and `src/schemas.py` 36, all PASS,
exit 0 on both. `python mutate.py tools schemas` then reported **30 of 34 caught**:

```
  SURVIVOR: tools:   a source URL is written here instead of quoted from the retrieval
  SURVIVOR: schemas: a tool description is a placeholder rather than a sentence
  SURVIVOR: schemas: every tool is marked pending, including the ones that work
  SURVIVOR: schemas: the default weighting is no longer named as the default
```

Three of the four are one defect wearing three hats: **the assertion compared a function
against itself**, so the mutation moved both sides and the comparison stayed true.

1. *the source URL.* The check read `list_datasets`'s output and compared it against
   `_provenance(record)` — the very helper that produced that output. Replacing
   `source.source_url` with a literal changes both sides identically. Fixed by reading
   the fields off the frozen `Provenance` dataclass instead, and keeping a second,
   separate assertion for the SHAPE of the reported record so a dropped field still
   fails.
2. *every tool marked pending.* The check compared `build_tool_specs([one])` against
   `TOOL_SPECS`, which is itself `build_tool_specs()`. A build that marks every tool
   marks both, and "no other tool's description changed" passes. Fixed with two absolute
   assertions: nothing carries the pending marker when nothing is pending, and only the
   named tool carries it when one is.
3. *the default weighting.* The check asked whether `DEFAULT_PRESET.name` appeared in the
   argument's help text. It always does — the help lists every legal preset name, and
   the default is one of them. Deleting the sentence that says WHICH one is the default
   left the name in the list and the check passed. Fixed by asserting the phrase
   `"<name> is the default"` against the serialised specs.

The fourth was not a defect in a check. The mutation replaced the first of five
implicitly concatenated string literals in one tool description, leaving a description
still far longer than the length the check tests. **A mutation that does not make the
module wrong is not a mutation**, and this one was too weak rather than uncaught. It now
replaces the whole entry.

**Where.** `_provenance_checks` in `src/tools.py`; `_pending_checks` and
`_derivation_checks` in `src/schemas.py`; one entry in `mutate.py`.

**Why.** Comparing a tool's output to the helper that built it is the natural thing to
write, because the helper is right there and the comparison is exact. It tests that the
two call sites agree, which they always will. What it does not test is whether either is
right. This is the same shape as every other entry in this file — a guard that is real,
and a fixture or a comparison that cannot observe it failing — and it is the fourth
session in a row in which the mutation sweep, not the check suite, is what found it.

**Did the agent recover?** Yes, all four, and re-measured rather than argued: 13 of 13 in
`schemas` on a fresh sweep, and the one surviving `tools` mutation re-run individually
and caught by the new assertion. The counts are now 108 checks in `tools` and 38 in
`schemas`.

**What the sweep still cannot reach, stated rather than implied.** No mutation here can
break the rule that degradation is read through `align.is_degraded` rather than through a
row count, because on this county the one degraded layer is also the one empty layer, so
both rules give the same answer everywhere. `align.py` owns that predicate and proves it
against a fixture registry; `tools.py` only quotes it, and the quote is unmutated. Nor
does any check here perform a live retrieval: `acquire_dataset` is exercised with a name
nothing retrieves, and the real endpoints are covered by `acquire.py --check` and by
`python -m src.demo`.

**Kept as a paper failure case?** Yes — §3.7. The number worth quoting is that a green
suite of 131 checks over two new modules hid four rules that could not fail, that three
of the four failed the same way, and that the failure mode was a comparison against the
implementation rather than against the contract.

## 2026-08-30 — the study bounding box reached a model message inside a sentence (S9)

**What happened.** Found by the `invariant-reviewer` run, behind 108 green checks in
`tools.py`, 38 in `schemas.py`, and a mutation sweep with no survivors.

`describe_layer` forwards a layer's `Provenance.notes` — the free text each retrieval
writes to record what it had to work around. `acquire.fetch_osm` records the Overpass
coordinate-order trap by printing the study extent in both orders:

```
bbox converted from (min_lon, min_lat, max_lon, max_lat)
(-80.45355300027266, 32.48256499980578, -79.2218779995792, 33.215368999850845)
to Overpass south,west,north,east (32.48256499980578, -80.45355300027266, ...)
```

Eight coordinates, to fifteen decimal places, in a tool result. Invariant 3 says geometry
never goes into a model message.

`coordinate_faults` — written this session, for exactly this — returned `[]`. Reproduced
directly:

```
python -c "from src import tools; print(tools.coordinate_faults(tools.as_sent(tools.describe_layer(name='facilities'))))"
[]
```

**Where.** `_provenance` and `coordinate_faults` in `src/tools.py`. The note itself is
`acquire.fetch_osm` and is not a defect: it is honest provenance, it belongs in
`data/snapshot/manifest.json`, and the verification it describes — that all 477 returned
points fall inside the requested extent — is a real check. The defect is S9 deciding to
pipe that text verbatim into a model-visible result.

**Why the guard missed it.** It refused three shapes: a KEY matching
`pipeline.COORDINATE_PATTERN`, a LIST holding a number or a list, and a bare-token string
VALUE matching the pattern. A sentence with floats in it is none of the three. The
bare-token rule was written deliberately narrow, because the pattern matches on
underscore boundaries and applying it to prose produces false positives — and narrowing
it to identifiers left the entire category of *coordinates written as numbers* uncovered.
Three shapes were enumerated and the fourth was never thought of.

There was a second, independent reason no check could have caught it: `SAMPLE_ARGUMENTS`
called `describe_layer` on the tract layer only. `facilities` is the one layer carrying
the leak, and nothing described it. The scan was as wide as the set it was pointed at.

**Did the agent recover?** Yes, and by widening both. `COORDINATE_TEXT` matches a signed
number with four or more decimal places next to another one — narrow enough that this
project's populations, depths, fractions and percentages do not trip it, which is
asserted with a fixture carrying real reported figures. Every string value is now tested
against it, not only bare tokens. `_provenance` withholds a note that carries a
coordinate and reports the count, the same policy `describe_layer` already applied to
column names, and says the full text remains in the manifest on disk — invariant 3 is
about what reaches a model, not about what the record holds. The coordinate check now
describes **every registered layer, enumerated from the registry**, and asserts the
withheld count reconciles against the record for each. Measured after: 7 of 7 layers
described, no faults, 1 note withheld on `facilities`.

**A second finding from the same review.** `acquire_dataset` calls `invalidate()` so the
next tool rebuilds from the snapshot the retrieval just replaced. The check called
`invalidate()` directly, which proved the function works and never reached the call site:
the reviewer replaced that line with `pass` and `python -m src.tools --check` printed
`all checks passed`. A stale answer after a live retrieval is the worst failure this
module can produce, and deleting the line that prevents it was invisible. `_cache_checks`
now drives `acquire_dataset` itself, with the retrieval and the registry replaced by
stubs so nothing reaches the network and nothing writes into `data/` — asserted by
comparing the snapshot manifest's modification time across the check. A mutation for the
call site is now in `mutate.py` beside the one for the function.

**Kept as a paper failure case?** Yes — §3.7, and it is the strongest entry in the file
for the reviewer argument specifically. It is the fourth session running in which an
independent reviewer holding the invariants found something a green suite and a
zero-survivor mutation sweep did not, and the first in which the defect was in a guard
written that same session to enforce the exact invariant it then failed to enforce. The
honest form of the claim is that enumerating the shapes a violation can take is not the
same as covering them, and that nothing inside the suite can tell you which shape you
forgot.
