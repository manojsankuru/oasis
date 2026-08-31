"""Break every check on purpose, and report any that did not notice.

A check that cannot fail is worth less than no check, because it reads as
coverage. This harness applies one wrong edit at a time to a module under src/,
runs that module's own `--check`, and reports any mutation that still exits 0.

Each module's checks are run against mutations of that module and no other. A
defect in `hazard.py` that only `pipeline.py` notices is a hole in hazard's own
suite, and running the downstream check would hide it rather than report it.

Self-contained on purpose: it takes its own backup of the live file and restores
it in a `finally`, so an interrupted run cannot leave a mutated module on disk.
An earlier version kept its backup inside a scratch job directory, which meant
the harness stopped working the moment that directory was cleaned up.

    python mutate.py

Every entry is an edit that makes the module WRONG, not merely different. A
rewrite that produces the same answers is not a mutation and does not belong
here -- it would be reported as a survivor and read as a missing check.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = pathlib.Path(sys.executable)


def source_of(module: str) -> pathlib.Path:
    return ROOT / "src" / f"{module}.py"


def backup_of(module: str) -> pathlib.Path:
    return ROOT / "src" / f"{module}.py.mutation-backup"


ALIGN_MUTATIONS: list[tuple[str, str, str]] = [
    # -- the metric CRS choke point ----------------------------------------
    ("to_working_crs never reprojects",
     "        return obj.to_crs(target)",
     "        return obj"),
    ("to_working_crs defaults a missing CRS instead of raising",
     "        if obj.crs is None:",
     "        if obj.crs is None and False:"),
    ("a metric operation added outside the CRS helper",
     "        target = CRS.from_user_input(self.working_crs)",
     "        _bypass = obj.buffer(0)\n        target = CRS.from_user_input(self.working_crs)"),

    # -- geometry repair ----------------------------------------------------
    ("repair_geometry always reports zero repaired",
     "        evidence.repaired = int((invalid & recovered).sum())",
     "        evidence.repaired = 0"),
    ("repair_geometry never drops a missing geometry",
     "        evidence.missing = int(missing.sum())",
     "        evidence.missing = 0"),
    ("repair_geometry keeps a geometry that make_valid could not fix",
     "        return frame.loc[~(invalid & ~recovered)].copy(), evidence",
     "        return frame.copy(), evidence"),
    ("collapsed geometries counted as repairs",
     "        evidence.collapsed = int((invalid & usable & ~kept_area).sum())",
     "        evidence.collapsed = 0"),
    ("repair keeps a geometry that stopped enclosing area",
     "        recovered = usable & kept_area",
     "        recovered = usable"),
    ("make_valid failure ends the run instead of dropping one feature",
     "        fixed.loc[invalid] = _make_valid(geometry.loc[invalid])",
     "        fixed.loc[invalid] = geometry.loc[invalid].make_valid()"),

    # -- sentinel scrubbing -------------------------------------------------
    ("scrub only removes values on the pasted jam list",
     "            negative = numeric.notna() & (numeric < 0)",
     "            negative = numeric.isin(list(JAM_VALUES))"),
    ("scrub removes nothing",
     "            negative = numeric.notna() & (numeric < 0)",
     "            negative = numeric.notna() & (numeric < -1e30)"),
    ("scrub counts an unparseable value as a sentinel",
     "            unparsed = int((present & numeric.isna()).sum())",
     "            unparsed = 0"),

    # -- the GEOID audit ----------------------------------------------------
    ("join reports the two unmatched sides transposed",
     "        report = AlignmentReport(unmatched_left=unmatched_left, unmatched_right=unmatched_right)",
     "        report = AlignmentReport(unmatched_left=unmatched_right, unmatched_right=unmatched_left)"),
    ("join reports nothing unmatched",
     "        unmatched_left = tuple(sorted(left_ids - right_ids))",
     "        unmatched_left = ()"),
    ("join tolerates a repeated GEOID and fans out",
     "            if repeated:",
     "            if repeated and False:"),
    ("numeric-dtype GEOID silently cast back",
     "    if pd.api.types.is_numeric_dtype(values.dtype):",
     "    if pd.api.types.is_numeric_dtype(values.dtype) and False:"),
    ("object-dtype numeric GEOID accepted",
     "    if len(present) and all(isinstance(value, (int, float, np.number)) for value in present):",
     "    if len(present) and False:"),
    ("a GEOID width no census geography uses is accepted",
     "    if unpublished:",
     "    if unpublished and False:"),

    # -- provenance is quoted, never invented -------------------------------
    ("temporal_span invented instead of read from provenance",
     "            report.temporal_span[record.name] = record_prov.vintage",
     "            report.temporal_span[record.name] = 'current'"),
    ("reprojected invented here instead of quoted from provenance",
     '                f"{record_prov.declared_crs} -> {record_prov.working_crs}"',
     '                "EPSG:4326 -> EPSG:5070"'),
    ("the degraded predicate itself reads the row count",
     '    return record.request_params.get(DEGRADED_KEY) == "true"',
     "    return record.feature_count == 0"),
    ("align_snapshot bypasses the predicate and reads the row count",
     "            if is_degraded(record_prov):",
     '            if record_prov.feature_count == 0 and record.kind == "vector":'),
    ("degraded reason written here instead of quoted from provenance",
     '                evidence.degraded[record.name] = f"{reason} {detail}".strip()',
     '                evidence.degraded[record.name] = "the service was unavailable"'),

    # -- derived ACS columns ------------------------------------------------
    ("derived percentage divides by the wrong thing",
     "                share = total / base.where(base > 0)",
     "                share = total / (base.where(base > 0) * 2.0)"),
    ("derived percentage never defined, whole column null",
     "                share = total / base.where(base > 0)",
     "                share = total / base.where(base < 0)"),
    ("sentinel summed into the derived indicator instead of propagating",
     "            total = out[numerators].sum(axis=1, min_count=len(numerators))",
     "            total = out[numerators].sum(axis=1, min_count=1)"),
    ("numerator over-matched, summing every leaf twice",
     "            numerators = list(acquire.acs_numerator_ids(spec))",
     "            numerators = list(acquire.acs_numerator_ids(spec)) * 2"),
    ("nulls made by dividing go uncounted",
     "            if made_null:",
     "            if made_null and False:"),
    ("a share narrowed to Int64 when the county happens to allow it",
     '                out[name] = pd.to_numeric(share * 100.0, errors="coerce").astype("Float64")',
     '                out[name] = _as_narrow_numeric(pd.to_numeric(share * 100.0, errors="coerce"))'),
    ("population derived off by one",
     '                derived[name] = f"sum of {len(numerators)} estimate(s) = {spec}"',
     '                out[name] = out[name] + 1\n                derived[name] = f"sum of {len(numerators)} estimate(s) = {spec}"'),
    ("acs value columns guessed from the frame instead of read from provenance",
     "        wanted = estimates + margins",
     "        wanted = [column for column in frame.columns if column.endswith('E')][:1]"),
    ("coordinate columns left in the joined layer",
     '            frames[f"{geometry_name}{JOINED_SUFFIX}"] = joined[keep]',
     '            frames[f"{geometry_name}{JOINED_SUFFIX}"] = joined'),

    # -- session 7: zonal statistics ---------------------------------------
    ("a cell is claimed by every polygon its boundary clips, not the one it centres in",
     "ALL_TOUCHED = False",
     "ALL_TOUCHED = True"),
    ("nodata cells are averaged in as if they were elevations",
     "        return data[inside & ~nodata & finite]",
     "        return data[inside]"),
    ("a non-finite cell is kept and poisons the mean",
     "        return data[inside & ~nodata & finite]",
     "        return data[inside & ~nodata]"),
    ("nodata inside a polygon is not counted apart from ground outside it",
     "        evidence.nodata_cells += int((inside & nodata).sum())",
     "        evidence.nodata_cells += 0"),
    ("the shape mask is inverted, so every statistic describes the wrong ground",
     "            invert=False,\n        )\n        nodata = np.ma.getmaskarray(band)",
     "            invert=True,\n        )\n        nodata = np.ma.getmaskarray(band)"),
    ("the mean is computed over the minimum",
     '        if name == "mean":\n            return float(values.mean())',
     '        if name == "mean":\n            return float(values.min())'),
    ("the maximum is computed over the minimum",
     '        if name == "max":\n            return float(values.max())',
     '        if name == "max":\n            return float(values.min())'),
    ("a polygon the raster does not cover reports zero rather than nothing",
     "        if values.size == 0:\n            return None",
     "        if values.size == 0:\n            return 0.0"),
    ("nothing is ever flagged as below the cell threshold",
     "                1 for count in counts if count < MIN_RASTER_CELLS",
     "                1 for count in counts if count < 0"),
    ("every polygon is flagged as below the cell threshold",
     "                1 for count in counts if count < MIN_RASTER_CELLS",
     "                1 for count in counts if count < 10 ** 9"),
    ("a polygon that misses the raster ends the run instead of being recorded",
     "        except ValueError as exc:\n            if RASTER_NO_OVERLAP not in str(exc):",
     "        except ZeroDivisionError as exc:\n            if RASTER_NO_OVERLAP not in str(exc):"),
    ("a raster in the wrong CRS is used as if it were in the working one",
     "            if raster_crs != CRS.from_user_input(self.working_crs):",
     "            if raster_crs != CRS.from_user_input(self.working_crs) and False:"),
    ("a statistic this module does not compute is accepted and returned empty",
     "        if unsupported:",
     "        if unsupported and False:"),
    ("the result is indexed by position rather than by GEOID",
     "        return pd.DataFrame(built, index=pd.Index(geoids.to_numpy(), name=Col.GEOID)), evidence",
     "        return pd.DataFrame(built), evidence"),
    ("a repeated GEOID is tolerated, so a unit silently overwrites another",
     "        if repeated:\n            raise ValueError(\n                f\"{label}: {repeated} of {len(frame)} rows repeat a {Col.GEOID}, so the \"",
     "        if repeated and False:\n            raise ValueError(\n                f\"{label}: {repeated} of {len(frame)} rows repeat a {Col.GEOID}, so the \""),
    ("the threshold count reports polygons measured instead of polygons flagged",
     "                report.units_below_cell_threshold += zonal.below_threshold",
     "                report.units_below_cell_threshold += zonal.polygons"),
    ("the elevation columns never reach the joined layers",
     "                if record.name == acquire.DATASET_ELEVATION:",
     "                if record.name == acquire.DATASET_ELEVATION and False:"),

    # -- session 7: apportionment ------------------------------------------
    ("the rollup takes the wrong number of GEOID characters",
     "        parent = fine_frame[Col.GEOID].str[:coarse_width]",
     "        parent = fine_frame[Col.GEOID].str[: coarse_width - 1]"),
    ("population_weighted ignores the weights and just sums",
     "                rolled = numerator / denominator.where(denominator > 0)",
     "                rolled = value.groupby(parent).sum(min_count=1)"),
    ("weighting a population by itself is answered instead of refused",
     "            if any(column == weight_column for column in columns):",
     "            if any(column == weight_column for column in columns) and False:"),
    ("the error is the county total, which lets opposite errors cancel",
     '                relative = 100.0 * difference[defined] / pair.loc[defined, "published"].abs()\n                evidence.error[column] = float(relative.max())',
     '                evidence.error[column] = float(\n                    100.0\n                    * abs(pair["aggregated"].sum() - pair["published"].sum())\n                    / abs(pair["published"].sum())\n                )'),
    ("the error is the best unit rather than the worst",
     "                evidence.error[column] = float(relative.max())",
     "                evidence.error[column] = float(relative.min())"),
    ("the error divides by the aggregate instead of the published value",
     '                relative = 100.0 * difference[defined] / pair.loc[defined, "published"].abs()',
     '                relative = 100.0 * difference[defined] / pair.loc[defined, "aggregated"].abs()'),
    ("a unit publishing zero is fed into the relative error as a division by zero",
     '            defined = pair["published"] != 0',
     '            defined = pair["published"].notna()'),
    ("a suppressed child is dropped, so its parent reports a partial sum",
     "            aggregated[column] = pd.to_numeric(\n                rolled.where(complete), errors=\"coerce\"\n            ).astype(\"Float64\")",
     "            aggregated[column] = pd.to_numeric(rolled, errors=\"coerce\").astype(\"Float64\")"),
    ("a child whose parent is absent goes uncounted",
     "            orphan_children=int((~parent.isin(published_ids)).sum()),",
     "            orphan_children=0,"),
    ("a coarse frame apportioned into a finer one is accepted",
     "        if fine_width <= coarse_width:",
     "        if fine_width <= coarse_width and False:"),
    ("a frame holding two geographic levels is accepted and rolled up anyway",
     "            if len(widths) != 1:",
     "            if len(widths) != 1 and False:"),
    ("a repeated coarse GEOID is tolerated, so there is no single published value",
     "        repeated = int(coarse_frame[Col.GEOID].duplicated().sum())\n        if repeated:",
     "        repeated = int(coarse_frame[Col.GEOID].duplicated().sum())\n        if repeated and False:"),
    ("the apportionment error never reaches the frozen report",
     "            report.apportionment_error.update(apportion.error)",
     "            report.apportionment_error.update({})"),
    ("the apportioned columns never reach the frozen report",
     "            for column in apportion.columns:\n                report.apportioned[column] = apportion.method_note",
     "            for column in ():\n                report.apportioned[column] = apportion.method_note"),
    ("the units compared are counted before the unmatched ones are dropped",
     '            evidence.units_compared[column] = len(pair)',
     '            evidence.units_compared[column] = len(published_frame)'),

    # -- the invariant reviewer's findings, each now load-bearing ------------
    ("an empty frame is called a granularity mismatch again",
     "            if not len(frame):",
     "            if not len(frame) and False:"),
    ("the no-overlap message no longer matches, so a real gap in coverage crashes",
     'RASTER_NO_OVERLAP = "do not overlap raster"',
     'RASTER_NO_OVERLAP = "a message rasterio never emits"'),
    ("any rasterio failure is attributed to the polygon being outside the raster",
     "            if RASTER_NO_OVERLAP not in str(exc):\n                raise",
     "            if False:\n                raise"),
    ("apportionment is skipped and nothing says so",
     "        if apportionable:",
     "        if apportionable and False:"),
    # Two entries were removed here rather than left to survive. One deleted
    # *EVIDENCE_RECORDS from the scan tuple; the other reverted counts_agree to its
    # vacuous form. Neither changes any number this county produces, so neither can
    # be caught, and neither is a defect -- a mutation has to make the module wrong.
    # The scan widening is proven instead by the entry immediately below, which
    # writes a metric operation into a record and is caught only because the record
    # is now scanned. See docs/failures.md for what suite-level mutation cannot see.
    ("a metric operation hidden inside an evidence record goes unseen",
     "    @property\n    def valid_cells(self) -> int:",
     '    @property\n    def valid_cells(self) -> int:\n        _bypass = "".centroid if False else 0'),
    # Units disagree, the county total does not. This is the exact shape the
    # invariant reviewer showed the old biconditional-plus-total check could not
    # see: two errors of opposite sign cancel in the sum, and both sides of the
    # biconditional move together. Only a per-unit assertion catches it.
    ("aggregates are attached to the wrong parents, leaving the county total correct",
     "                rolled = value.groupby(parent).sum(min_count=1)",
     "                _r = value.groupby(parent).sum(min_count=1)\n                rolled = pd.Series(_r.to_numpy()[::-1], index=_r.index)"),
]

HAZARD_MUTATIONS: list[tuple[str, str, str]] = [
    # -- the bathtub arithmetic ---------------------------------------------
    ("depth is not clipped at zero, so dry ground reports negative water",
     "        depth = np.where(usable, np.maximum(0.0, surge_height_m - elevation), np.nan)",
     "        depth = np.where(usable, surge_height_m - elevation, np.nan)"),
    ("the surge is subtracted the wrong way round",
     "        depth = np.where(usable, np.maximum(0.0, surge_height_m - elevation), np.nan)",
     "        depth = np.where(usable, np.maximum(0.0, elevation - surge_height_m), np.nan)"),
    ("a cell exactly at the surge height counts as wet",
     "        wet = np.where(usable, np.where(depth > 0.0, WET, DRY), np.nan)",
     "        wet = np.where(usable, np.where(depth >= 0.0, WET, DRY), np.nan)"),
    ("the wet mask is the depth itself, so the mean is not a fraction",
     "        wet = np.where(usable, np.where(depth > 0.0, WET, DRY), np.nan)",
     "        wet = np.where(usable, depth, np.nan)"),
    ("a hole in the wet mask is filled with dry instead of carried forward",
     "        wet = np.where(usable, np.where(depth > 0.0, WET, DRY), np.nan)",
     "        wet = np.where(usable, np.where(depth > 0.0, WET, DRY), DRY)"),
    # -- nodata is not sea level --------------------------------------------
    ("the nodata sentinel is treated as ground at -9999 m",
     "            usable &= elevation != source_nodata",
     "            usable &= elevation == elevation"),
    ("a non-finite elevation is treated as usable",
     "        usable = np.isfinite(elevation)",
     "        usable = np.ones(elevation.shape, dtype=bool)"),
    ("the derived raster is written with no nodata, so a hole reads as -9999 m of water",
     '        out["nodata"] = nodata',
     '        out["nodata"] = None'),
    # -- the column map -----------------------------------------------------
    ("mean and max depth are mapped onto each other's columns",
     '    "mean": Col.INUNDATION_MEAN_M,\n    "max": Col.INUNDATION_MAX_M,',
     '    "mean": Col.INUNDATION_MAX_M,\n    "max": Col.INUNDATION_MEAN_M,'),
    ("the inundation pass remaps count onto the column align already fills",
     'WET_STAT_COLUMNS: dict[str, str] = {"mean": Col.INUNDATED_FRACTION}',
     'WET_STAT_COLUMNS: dict[str, str] = {"mean": Col.INUNDATED_FRACTION, "count": Col.RASTER_CELLS}'),
    # -- the denominators have to agree -------------------------------------
    ("the two rasters' cell counts are compared in total rather than per unit",
     "        matched = depth_counts.eq(wet_counts).fillna(False)",
     "        matched = pd.Series(\n            depth_counts.sum() == wet_counts.sum(), index=depth_counts.index\n        )"),
    ("the derived cell counts are never compared against the elevation pass",
     "        if Col.RASTER_CELLS not in frame.columns:",
     "        if True:"),
    ("the inundated fraction is never bounds-checked",
     "        outside = fraction.notna() & ((fraction < 0.0) | (fraction > 1.0))",
     "        outside = fraction.notna() & (fraction < -1e30)"),
    # -- a degraded layer is not an absent hazard ---------------------------
    ("a failed flood-zone retrieval is reported as an absence of flood risk",
     "            if align.is_degraded(record.provenance):\n                status[name] = (",
     "            if False:\n                status[name] = ("),
    ("the degraded flood layer is recognised by its row count instead of its flag",
     "            if align.is_degraded(record.provenance):\n                status[name] = (\n                    f\"{name}: retrieval degraded, so the hazard in this table is the \"",
     "            if record.provenance.feature_count == 0:\n                status[name] = (\n                    f\"{name}: retrieval degraded, so the hazard in this table is the \""),
    # -- refusals -----------------------------------------------------------
    ("a negative surge height is accepted and reports a dry county",
     "        if scenario.surge_height_m < 0:",
     "        if scenario.surge_height_m < -1e30:"),
    ("deriving a surface from a degraded or non-raster dataset is allowed",
     "        if align.is_degraded(record.provenance):\n            raise ValueError(",
     "        if False:\n            raise ValueError("),
]

VULNERABILITY_MUTATIONS: list[tuple[str, str, str]] = [
    # -- the percentile rule ------------------------------------------------
    ("the stated direction is ignored and every indicator ranks ascending",
     '    return numeric.rank(pct=True, ascending=direction == MORE_IS_WORSE).astype("Float64")',
     '    return numeric.rank(pct=True, ascending=True).astype("Float64")'),
    ("a missing value is ranked as a zero, making the unit the least vulnerable",
     '    return numeric.rank(pct=True, ascending=direction == MORE_IS_WORSE).astype("Float64")',
     '    return numeric.fillna(0).rank(pct=True, ascending=direction == MORE_IS_WORSE).astype("Float64")'),
    ("the rank denominator is the row count rather than the published count",
     '    return numeric.rank(pct=True, ascending=direction == MORE_IS_WORSE).astype("Float64")',
     '    return (numeric.rank(ascending=direction == MORE_IS_WORSE) / len(numeric)).astype("Float64")'),
    ("ties are broken by row order instead of sharing the average rank",
     '    return numeric.rank(pct=True, ascending=direction == MORE_IS_WORSE).astype("Float64")',
     '    return numeric.rank(pct=True, ascending=direction == MORE_IS_WORSE, method="first").astype("Float64")'),
    # -- the null policy ----------------------------------------------------
    ("a unit missing an indicator is scored on the ones it does publish",
     "        complete = ranked.notna().all(axis=1)",
     "        complete = ranked.notna().any(axis=1)"),
    # Deleting `.where(complete)` outright is NOT a mutation: a Float64 weighted sum
    # already propagates pd.NA, so removing it produces byte-identical answers and
    # survives as a no-op rather than as a defect. What the line actually defends
    # against is somebody deciding a null should score zero, so that is the edit.
    ("a unit missing an indicator is scored zero instead of left unscored",
     '        scored = pd.Series(weighted, index=frame.index, dtype="Float64").where(complete)',
     '        scored = pd.Series(weighted, index=frame.index, dtype="Float64").fillna(0.0)'),
    # -- weights are arguments, and are validated ---------------------------
    ("weights are not normalised, so their units change the ranking",
     "    return {key: value / total for key, value in used.items()}",
     "    return dict(used)"),
    ("a weighting missing an indicator silently drops it",
     "    missing = [key for key in keys if key not in weights]",
     "    missing = []"),
    ("a misspelled weight key is ignored instead of refused",
     "    unknown = [key for key in weights if key not in WEIGHT_KEYS]",
     "    unknown = []"),
    ("a negative weight smuggles a direction past INDICATOR_DIRECTION",
     "    negative = {key: value for key, value in used.items() if value < 0}",
     "    negative = {}"),
    ("the preset's weights are ignored and every call uses the default",
     "            dict(weights if weights is not None else preset.weights), self.indicators",
     "            dict(DEFAULT_PRESET.weights), self.indicators"),
    # -- the rationale friction ---------------------------------------------
    ("an indicator can join the index without a sentence saying why",
     "        unexplained = [name for name in indicators if name not in INDICATOR_RATIONALE]",
     "        unexplained = []"),
    ("a preset loses its published origin url",
     '        origin_url=SVI_URL,\n    ),\n    WeightPreset(\n        name="svi_themes",',
     '        origin_url="",\n    ),\n    WeightPreset(\n        name="svi_themes",'),
    ("the two published presets are given identical indicator weights",
     "            Col.PCT_POVERTY: 1 / 3,\n            Col.PCT_AGE_65_PLUS: 1 / 9,\n            Col.PCT_DISABILITY: 1 / 9,\n            Col.PCT_LIMITED_ENGLISH: 1 / 9,\n            Col.PCT_NO_VEHICLE: 1 / 3,",
     "            Col.PCT_POVERTY: 0.2,\n            Col.PCT_AGE_65_PLUS: 0.2,\n            Col.PCT_DISABILITY: 0.2,\n            Col.PCT_LIMITED_ENGLISH: 0.2,\n            Col.PCT_NO_VEHICLE: 0.2,"),
]

RISK_MUTATIONS: list[tuple[str, str, str]] = [
    # -- the four components ------------------------------------------------
    ("resilience raises the risk score instead of lowering it",
     "    Col.RESILIENCE: MORE_IS_BETTER,",
     "    Col.RESILIENCE: MORE_IS_WORSE,"),
    ("exposed population is the population NOT on flooded land",
     "        fine[Col.EXPOSED_POPULATION] = (\n            fine[Col.POPULATION] * fine[Col.INUNDATED_FRACTION]\n        )",
     "        fine[Col.EXPOSED_POPULATION] = (\n            fine[Col.POPULATION] * (1.0 - fine[Col.INUNDATED_FRACTION])\n        )"),
    ("exposure is the whole population wherever any of the unit floods",
     "        fine[Col.EXPOSED_POPULATION] = (\n            fine[Col.POPULATION] * fine[Col.INUNDATED_FRACTION]\n        )",
     "        fine[Col.EXPOSED_POPULATION] = fine[Col.POPULATION] * (\n            fine[Col.INUNDATED_FRACTION] > 0\n        ).astype(float)"),
    ("the coarse tract-uniform estimate is reported instead of the block-group rollup",
     "        exposed = pd.to_numeric(\n            aggregated[Col.EXPOSED_POPULATION].reindex(coarse[Col.GEOID].to_numpy()),\n            errors=\"coerce\",\n        ).astype(\"Float64\")",
     "        exposed = pd.to_numeric(\n            coarse[Col.EXPOSED_POPULATION], errors=\"coerce\"\n        ).astype(\"Float64\")"),
    ("a tract reporting more exposed residents than residents is allowed through",
     "        if evidence.units_over_population:",
     "        if False:"),
    # -- resilience ---------------------------------------------------------
    # The metric-bypass scan cannot see either of these: `resilience` mentions
    # to_working_crs twice, so removing ONE call leaves the name in the function and
    # the scan satisfied. Only a fixture that hands the frame over in degrees
    # notices, and the review found there was one for `units` and none for
    # `facilities` -- so this second entry survived until _resilience_checks grew
    # the matching case. gpd.sjoin does not raise on a CRS mismatch; it warns and
    # returns nothing, which reads as every unit reaching no facility.
    ("the units are buffered in whatever CRS they arrive in, not the metric one",
     "        placed = self.aligner.to_working_crs(units)",
     "        placed = units"),
    ("the facilities are joined in whatever CRS they arrive in",
     "        points = self.aligner.to_working_crs(facilities)",
     "        points = facilities"),
    ("the reach radius is applied in kilometres, so every unit reaches almost nothing",
     "            reach, geometry=placed.geometry.buffer(radius_m), crs=placed.crs",
     "            reach, geometry=placed.geometry.buffer(radius_m / 1000.0), crs=placed.crs"),
    ("the facility count is ranked as though more facilities were worse",
     "        ranked = percentile_rank(counts, direction=MORE_IS_WORSE)",
     "        ranked = percentile_rank(counts, direction=MORE_IS_BETTER)"),
    ("the facility tag is written here instead of read from the retrieval",
     "        keys = [key for key in tags if isinstance(key, str)]",
     '        keys = ["shop"]'),
    ("a zero or negative reach radius is accepted",
     "        if radius_m <= 0:",
     "        if radius_m < -1e30:"),
    ("a metric operation is written into combine, which never routes through the helper",
     "        absent = [name for name in components if name not in frame.columns]",
     '        _bypass = "".buffer(1) if False else 0\n        absent = [name for name in components if name not in frame.columns]'),
    # -- the score ----------------------------------------------------------
    # The sibling of the vulnerability entry above, and for the same reason:
    # deleting `.where(complete)` is a no-op because a Float64 weighted sum already
    # propagates pd.NA. Scoring the null as zero is the edit the line defends against.
    ("a unit missing a component is scored zero instead of left unscored",
     '        out[Col.RISK_SCORE] = pd.Series(score, index=frame.index, dtype="Float64").where(complete)',
     '        out[Col.RISK_SCORE] = pd.Series(score, index=frame.index, dtype="Float64").fillna(0.0)'),
    ("a unit missing a component is scored on the components it has",
     "        complete = ranks.notna().all(axis=1)",
     "        complete = ranks.notna().any(axis=1)"),
    ("priority rank 1 goes to the lowest risk score",
     '            out[Col.RISK_SCORE].rank(ascending=False, method="min").astype("Int64")',
     '            out[Col.RISK_SCORE].rank(ascending=True, method="min").astype("Int64")'),
    ("every preset is scored under the default weighting, so the ranking cannot move",
     "            dict(weights if weights is not None else preset.weights), components",
     "            dict(vulnerability.DEFAULT_PRESET.weights), components"),
    ("the index is not recomputed per preset, so indicator weights cannot move anything",
     "            if units is not None:",
     "            if False:"),
    ("every preset is compared under the first preset's index",
     "                index, _ = self.index.index(units, preset=preset)",
     "                index, _ = self.index.index(units, preset=vulnerability.DEFAULT_PRESET)"),

    # -- who loses ----------------------------------------------------------
    ("the trade-off table reports that no weighting displaces anybody",
     "                    displaced_geoids=tuple(sorted(elsewhere - set(top))),",
     "                    displaced_geoids=(),"),
    ("who loses is the units this preset picked rather than the ones it dropped",
     "                    displaced_geoids=tuple(sorted(elsewhere - set(top))),",
     "                    displaced_geoids=tuple(sorted(set(top))),"),
]

PIPELINE_MUTATIONS: list[tuple[str, str, str]] = [
    ("the written table carries whatever columns the frame happened to hold",
     "    return frame[list(REPORTED_COLUMNS)]",
     "    return frame"),
    ("a promised column missing upstream writes a shorter file instead of failing",
     "    if absent:",
     "    if False:"),
    ("a coordinate-bearing column is written into the deliverable",
     "    if leaking:",
     "    if False:"),
    ("every scenario is scored against the shallowest surface",
     "        surface = hazard_report.surfaces[scenario.name]",
     "        surface = list(hazard_report.surfaces.values())[0]"),
    ("the trade-off comparison uses one preset rather than every one",
     "                presets=presets,\n                priority_units=priority_units,",
     "                presets=presets[:1],\n                priority_units=priority_units,"),
    ("the trade-off comparison forgets the layer the index has to be rebuilt from",
     "                units=tracts,",
     "                units=None,"),
    ("running with no scenario at all is allowed and writes nothing",
     '        raise ValueError("the pipeline was given no hazard scenario to run")',
     "        scenarios = HAZARD_SCENARIOS"),
]

SCHEMAS_MUTATIONS: list[tuple[str, str, str]] = [
    # -- the frozen surface --------------------------------------------------
    ("a tool is advertised under a name the contract does not carry",
     '    "validate_answer": ValidateAnswerArgs,',
     '    "validate_answer_typo": ValidateAnswerArgs,'),
    # Replaces the WHOLE entry. An earlier version replaced only the first of the
    # five implicitly concatenated strings, which left a description still well
    # over the length the check tests -- a mutation too weak to be wrong, reported
    # as a survivor.
    ("a tool description is a placeholder rather than a sentence",
     '    "list_datasets": (\n'
     '        "List every retrieved dataset with its source URL, retrieval timestamp, "\n'
     '        "vintage, feature count, licence, declared and working CRS, and whether "\n'
     '        "its retrieval was degraded. Call this first: it is the only way to learn "\n'
     '        "which layer names the other tools accept, and it is where the citations "\n'
     '        "for your answer come from."\n'
     "    ),",
     '    "list_datasets": "tbd",'),

    # -- invariant 4 ---------------------------------------------------------
    ("the forbidden-keyword list is empty, so the flatness scan looks for nothing",
     'FORBIDDEN_KEYWORDS: tuple[str, ...] = ("$ref", "$defs", "anyOf", "allOf", "oneOf")',
     "FORBIDDEN_KEYWORDS: tuple[str, ...] = ()"),
    ("the emitted spec drops the list of required arguments",
     '    schema.setdefault("properties", {})',
     '    schema.pop("required", None)\n    schema.setdefault("properties", {})'),

    # -- the unset sentinel --------------------------------------------------
    ("the unset sentinel is a weight the index would accept",
     "UNSET_WEIGHT = -1.0",
     "UNSET_WEIGHT = 0.5"),
    ("a weight argument is named the same thing for every indicator",
     '    return f"{WEIGHT_ARG_PREFIX}{indicator}"',
     '    return f"{WEIGHT_ARG_PREFIX}weight"'),
    ("an argument name reads back as the wrong indicator",
     "    return argument[len(WEIGHT_ARG_PREFIX):]",
     "    return argument[len(WEIGHT_ARG_PREFIX) + 1:]"),

    # -- bounds and defaults -------------------------------------------------
    ("a default is outside the bound declared beside it",
     "DEFAULT_TOP_N = 10",
     "DEFAULT_TOP_N = 100"),
    ("the upper bound on a ranking length is dropped",
     "class HazardExposureArgs(BaseModel):\n"
     "    scenario: str = Field(default=UNSET_NAME, description=_SCENARIO_HELP)\n"
     "    top_n: int = Field(default=DEFAULT_TOP_N, ge=1, le=MAX_TOP_N, description=_TOP_N_HELP)",
     "class HazardExposureArgs(BaseModel):\n"
     "    scenario: str = Field(default=UNSET_NAME, description=_SCENARIO_HELP)\n"
     "    top_n: int = Field(default=DEFAULT_TOP_N, ge=1, description=_TOP_N_HELP)"),

    # -- what the model is told ----------------------------------------------
    ("a pending tool is not marked, so the model learns it by wasting a turn",
     "    unavailable = set(pending)",
     "    unavailable = set()"),
    ("every tool is marked pending, including the ones that work",
     '+ (PENDING_SUFFIX if name in unavailable else "")',
     "+ PENDING_SUFFIX"),
    ("the legal scenario names are written down instead of read from the module",
     "SCENARIO_NAMES: tuple[str, ...] = tuple(item.name for item in HAZARD_SCENARIOS)",
     'SCENARIO_NAMES: tuple[str, ...] = ("surge_1_5m",)'),
    ("the default weighting is no longer named as the default",
     'f"{DEFAULT_PRESET.name} is the default and is the one with a published origin. "',
     '"one of them is the default. "'),
]


TOOLS_MUTATIONS: list[tuple[str, str, str]] = [
    # -- invariant 3: no coordinate reaches a model message ------------------
    ("a key that names a coordinate passes the guard",
     "            if COORDINATE_PATTERN.search(str(key)):",
     "            if COORDINATE_PATTERN.search(str(key)) and False:"),
    ("a column name that names a coordinate passes inside a value",
     "        if BARE_TOKEN.match(payload) and COORDINATE_PATTERN.search(payload):",
     "        if BARE_TOKEN.match(payload) and COORDINATE_PATTERN.search(payload) and False:"),
    ("a list of numbers is allowed, which is the shape of a coordinate list",
     '                faults.append(f"{here}: list holds a number, which is a coordinate shape")',
     "                pass"),
    ("describe_layer lists the coordinate columns TIGERweb ships instead of withholding them",
     "        (withheld if COORDINATE_PATTERN.search(name) else kept).append(name)",
     "        kept.append(name)"),
    # Found by the invariant reviewer, not by a check: a retrieval note spells the
    # study bounding box out in prose, which is neither a key, a bare token, nor a
    # list of numbers. These two break the rule that closed it.
    ("a coordinate spelled out inside a sentence passes the guard",
     "        if COORDINATE_TEXT.search(payload):",
     "        if COORDINATE_TEXT.search(payload) and False:"),
    ("a retrieval note carrying a coordinate is forwarded into a model message",
     "        safe = [note for note in source.notes if not COORDINATE_TEXT.search(note)]",
     "        safe = list(source.notes)"),
    ("only one layer is scanned for a coordinate instead of every registered one",
     "    every = {name: describe_layer(name=name) for name in found.names()}",
     "    every = {acquire.DATASET_TRACTS: describe_layer(name=acquire.DATASET_TRACTS)}"),

    # -- the surface guard itself --------------------------------------------
    ("a tool is registered under a name nothing advertises",
     "    TOOL_FUNCTIONS[fn.__name__] = logged",
     '    TOOL_FUNCTIONS[fn.__name__ + "_"] = logged'),
    ("the guard stops reporting a tool the model is offered and nothing can run",
     "    unrunnable = sorted(advertised - executable)",
     "    unrunnable = []"),
    ("the probe reports every module as present, so nothing is ever pending",
     '        return importlib.util.find_spec(f"{__package__}.{name}") is not None',
     "        return True"),
    ("pending is reported as empty while a backing module is still missing",
     "        if not module_present(module)",
     "        if False"),

    # -- one weighting, both halves ------------------------------------------
    ("the trade-off table drops the units argument and compares half of each weighting",
     "            units=state.units,",
     "            units=None,"),
    ("the score is combined under the default weighting whatever was asked for",
     "        frame, scenario=scenario, preset=preset, dataset=TRACT_KEY",
     "        frame, scenario=scenario, preset=DEFAULT_PRESET, dataset=TRACT_KEY"),
    ("the index is taken under the default weighting whatever was asked for",
     "        state.units, preset=preset, weights=weights, dataset=TRACT_KEY",
     "        state.units, preset=DEFAULT_PRESET, weights=weights, dataset=TRACT_KEY"),
    ("the unset sentinel is applied as though it were a weight somebody chose",
     "        if value != schemas.UNSET_WEIGHT",
     "        if True"),

    # -- reporting the pipeline's number, not another one --------------------
    ("the headline exposed population is the coarse estimate, not the reported one",
     '                "exposed_population": exposure.fine_total,',
     '                "exposed_population": exposure.coarse_total,'),
    ("the ranking is ordered worst-last",
     "    ordered = usable.sort_values(by, ascending=False)",
     "    ordered = usable.sort_values(by, ascending=True)"),
    ("a source URL is written here instead of quoted from the retrieval",
     '        "source_url": source.source_url,',
     '        "source_url": "https://example.invalid/service",'),

    # -- saying what was left out --------------------------------------------
    ("a unit with no value is ranked rather than counted as unscored",
     "    usable = frame.dropna(subset=[by])",
     "    usable = frame"),
    ("the scored and unscored counts are transposed",
     '        "units_scored": evidence_units - unscored,',
     '        "units_scored": unscored,'),
    ("a truncated list reports nothing truncated",
     '        "not_listed": max(0, len(listed) - limit),',
     '        "not_listed": 0,'),
    ("a missing value is rendered as a zero",
     "    if value is None:\n        return None",
     "    if value is None:\n        return 0"),

    # -- the shared run ------------------------------------------------------
    ("a live retrieval leaves the previous analysis in place, so answers go stale",
     "    _ANALYSIS = None",
     "    pass"),
    # The call site as well as the function. Deleting this one line left the whole
    # suite green until the check was rewritten to drive acquire_dataset itself.
    ("acquire_dataset stops invalidating the run it just made stale",
     "    invalidate()",
     "    pass"),
    ("a tool result is not recorded, so no number can be traced back to it",
     "        CALLS.append(",
     "        [].append("),
]


SANDBOX_MUTATIONS: list[tuple[str, str, str]] = [
    # -- the deadline, and what is left running behind it --------------------
    # Every entry here breaks the KILL or the VERDICT, never the WAIT. `run_check`
    # above calls subprocess.run with no timeout of its own, so a mutation that
    # lengthened a deadline would hang this harness silently rather than being
    # reported. These all fail inside the fixture's own settle window.
    ("a run that outlives its deadline is abandoned rather than killed",
     "            if timed_out:\n                kill_tree(process)",
     "            if timed_out:\n                pass"),
    ("the kill stops at the child, so a grandchild survives it",
     '                ["taskkill", "/F", "/T", "/PID", str(process.pid)],',
     '                ["taskkill", "/F", "/PID", str(process.pid)],'),
    ("a run that ran out of time is not recognised as one",
     "            timed_out = process.poll() is None",
     "            timed_out = False"),
    ("a killed run reports exit zero, so a timeout reads as a success",
     "            exit_code = process.returncode if process.returncode is not None else TIMEOUT_EXIT_CODE",
     "            exit_code = 0"),

    # -- invariant 3 on the child's stdout -----------------------------------
    ("the output guard passes every line through untouched",
     "        faults = output_faults(line)",
     "        faults = []"),
    # The three rules the invariant reviewer added. Each was invisible to the
    # first rule set and each has one fixture that only it can catch.
    ("the rule that sees a bare coordinate pair is dropped",
     '    ("two decimal numbers side by side", BARE_PAIR),',
     '    ("two decimal numbers side by side", GEOMETRY_TEXT),'),
    ("the rule that sees a labelled projected pair is dropped",
     '    ("two large decimal numbers with a label between them", LABELLED_PAIR),',
     '    ("two large decimal numbers with a label between them", BARE_PAIR),'),
    ("a name that labels a coordinate stops being tested",
     '            faults.append("a name that could label a coordinate, in front of a number")',
     "            pass"),
    ("a quoted coordinate column name stops being tested",
     '            faults.append("a quoted name that could be a coordinate column")',
     "            pass"),
    ("the child stops guarding what it prints",
     "builtins.print = guarded_print",
     "pass"),
    ("a stream longer than the bound is sent whole, crowding the run's turns",
     "    if len(joined) <= STREAM_LIMIT:",
     "    if True:"),

    # -- the traceback the model repairs from --------------------------------
    ("the traceback is reduced to its last line, so no frame reaches the model",
     '        sys.stderr.write("".join(traceback.format_exception(kind, value, tb.tb_next or tb)))',
     '        sys.stderr.write("".join(traceback.format_exception(kind, value, None)))'),
    ("the sandbox's own frame is left on top of the model's traceback",
     '        sys.stderr.write("".join(traceback.format_exception(kind, value, tb.tb_next or tb)))',
     '        sys.stderr.write("".join(traceback.format_exception(kind, value, tb)))'),

    # -- what a failure tells the model ---------------------------------------
    # The S10 demo failed here: the tool ran, the model did not know what the
    # layers were called, and a true and complete NameError was useless.
    ("a failed run stops saying what names it had bound",
     '            raw_err = raw_err + "\\n" + available_names(space) + "\\n"',
     "            pass"),
    ("the failure is classified after the inventory, so the last line is not the error",
     '        failed_as = classify(int(exit_code), raw_err, timed_out)\n'
     '        if exit_code != 0:\n'
     '            raw_err = raw_err + "\\n" + available_names(space) + "\\n"',
     '        if exit_code != 0:\n'
     '            raw_err = raw_err + "\\n" + available_names(space) + "\\n"\n'
     '        failed_as = classify(int(exit_code), raw_err, timed_out)'),

    # -- the error taxonomy, which is the half of criterion IR worth reporting -
    ("every failure is classified as the same thing",
     "    return short",
     '    return "Error"'),
    ("a shape mismatch is reported as a bare ValueError",
     '    if short == "ValueError" and SHAPE_MISMATCH.search(stderr):',
     "    if False:"),

    # -- the dump the child reads --------------------------------------------
    ("the dump survives a retrieval that replaced the snapshot it came from",
     "    if _WORKSPACE is None or _WORKSPACE.built_from is not state:",
     "    if _WORKSPACE is None:"),
    ("only the head of each layer is dumped, so the child measures a sample",
     "    frame.to_parquet(path)",
     "    frame.head(5).to_parquet(path)"),
    ("a degraded layer is offered to the model as an ordinary one",
     '        layers[name]["degraded"] = base in degraded',
     '        layers[name]["degraded"] = False'),
    ("the child runs outside the scratch directory the sandbox owns",
     "                        cwd=run_directory,",
     "                        cwd=tempfile.gettempdir(),"),

    # -- the repair loop, at its call sites -----------------------------------
    ("the loop asks again without showing the model the traceback",
     '            messages.append({"role": "user", "content": repair_message(run)})',
     '            messages.append({"role": "user", "content": "try again"})'),
    ("every attempt is reported as the first one",
     "            run.attempt = attempt",
     "            run.attempt = 1"),
    ("the loop keeps going after the code works",
     "            if run.exit_code == 0:",
     "            if False:"),

    # -- the instrumentation --------------------------------------------------
    ("a run is not recorded, so nothing can be measured over it",
     "    RUNS.append(run)",
     "    [].append(run)"),
    ("the repair rate is taken over every session, not the ones that failed first",
     "            round(len(repaired) / len(first_failed), 3) if first_failed else 0.0",
     "            round(len(repaired) / len(sessions), 3) if sessions else 0.0"),
]


TARGETS: dict[str, list[tuple[str, str, str]]] = {
    "align": ALIGN_MUTATIONS,
    "hazard": HAZARD_MUTATIONS,
    "vulnerability": VULNERABILITY_MUTATIONS,
    "risk": RISK_MUTATIONS,
    "pipeline": PIPELINE_MUTATIONS,
    "schemas": SCHEMAS_MUTATIONS,
    "tools": TOOLS_MUTATIONS,
    "sandbox": SANDBOX_MUTATIONS,
}
"""Which module each mutation edits, and therefore which `--check` runs.

A module listed here with no mutations is a module whose checks have never been
broken on purpose, which is the state this harness exists to make visible. The
run prints a per-module count so an empty list reads as `0/0 caught` rather than
disappearing into a healthy-looking total."""


def run_check(module: str) -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(PYTHON), "-m", f"src.{module}", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    failing = [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("[FAIL]")
    ]
    if not failing and proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()
        failing = ["(crashed: " + (tail[-1][:110] if tail else "no stderr") + ")"]
    return proc.returncode, failing


def mutate_module(module: str) -> list[tuple[str, object, list[str]]]:
    """Apply every mutation for one module, restoring the file in a finally."""
    source = source_of(module)
    backup = backup_of(module)
    original = source.read_text(encoding="utf-8")
    backup.write_text(original, encoding="utf-8")

    baseline_code, baseline_failing = run_check(module)
    if baseline_code != 0:
        backup.unlink(missing_ok=True)
        raise SystemExit(
            f"BASELINE IS NOT GREEN for src/{module}.py -- fix that before mutating\n"
            + "\n".join(f"  {line}" for line in baseline_failing)
        )

    results: list[tuple[str, object, list[str]]] = []
    try:
        for label, needle, replacement in TARGETS[module]:
            if needle not in original:
                results.append((label, "NEEDLE NOT FOUND", []))
                continue
            source.write_text(original.replace(needle, replacement, 1), encoding="utf-8")
            code, failing = run_check(module)
            results.append((label, code, failing))
    finally:
        source.write_text(original, encoding="utf-8")
        backup.unlink(missing_ok=True)
    return results


def main() -> int:
    wanted = [arg for arg in sys.argv[1:] if not arg.startswith("-")] or list(TARGETS)
    unknown = [module for module in wanted if module not in TARGETS]
    if unknown:
        print(f"no mutations defined for {unknown}; known modules: {list(TARGETS)}")
        return 2

    print("MUTATION RESULTS -- a mutation that exits 0 is a check that cannot fail\n")
    survivors: list[str] = []
    total = 0
    for module in wanted:
        results = mutate_module(module)
        total += len(results)
        module_survivors = 0
        print(f"-- src/{module}.py, checked by `python -m src.{module} --check`")
        for label, code, failing in results:
            caught = code == 1 and bool(failing)
            print(f"[{'CAUGHT  ' if caught else 'SURVIVED'}] exit={code}  {label}")
            for line in failing[:2]:
                print(f"           {line}")
            if not caught:
                survivors.append(f"{module}: {label}")
                module_survivors += 1
        print(
            f"   {len(results) - module_survivors}/{len(results)} caught in {module}\n"
        )

    print(f"{total - len(survivors)}/{total} mutations caught across {len(wanted)} module(s)")
    for label in survivors:
        print(f"  SURVIVOR: {label}")
    return 0 if not survivors else 1


if __name__ == "__main__":
    raise SystemExit(main())
