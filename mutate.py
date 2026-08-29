"""Break every check on purpose, and report any that did not notice.

A check that cannot fail is worth less than no check, because it reads as
coverage. This harness applies one wrong edit at a time to src/align.py, runs
`python -m src.align --check`, and reports any mutation that still exits 0.

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
SOURCE = ROOT / "src" / "align.py"
BACKUP = ROOT / "src" / "align.py.mutation-backup"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():
    PYTHON = pathlib.Path(sys.executable)

MUTATIONS: list[tuple[str, str, str]] = [
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


def run_check() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [str(PYTHON), "-m", "src.align", "--check"],
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


def main() -> int:
    original = SOURCE.read_text(encoding="utf-8")
    BACKUP.write_text(original, encoding="utf-8")

    baseline_code, baseline_failing = run_check()
    if baseline_code != 0:
        print("BASELINE IS NOT GREEN -- fix that before mutating")
        for line in baseline_failing:
            print(f"  {line}")
        BACKUP.unlink(missing_ok=True)
        return 2

    results: list[tuple[str, object, list[str]]] = []
    try:
        for label, needle, replacement in MUTATIONS:
            if needle not in original:
                results.append((label, "NEEDLE NOT FOUND", []))
                continue
            SOURCE.write_text(original.replace(needle, replacement, 1), encoding="utf-8")
            code, failing = run_check()
            results.append((label, code, failing))
    finally:
        SOURCE.write_text(original, encoding="utf-8")
        BACKUP.unlink(missing_ok=True)

    print("MUTATION RESULTS -- a mutation that exits 0 is a check that cannot fail\n")
    survivors: list[str] = []
    for label, code, failing in results:
        caught = code == 1 and bool(failing)
        print(f"[{'CAUGHT  ' if caught else 'SURVIVED'}] exit={code}  {label}")
        for line in failing[:2]:
            print(f"           {line}")
        if not caught:
            survivors.append(label)

    print(f"\n{len(results) - len(survivors)}/{len(results)} mutations caught")
    for label in survivors:
        print(f"  SURVIVOR: {label}")
    return 0 if not survivors else 1


if __name__ == "__main__":
    raise SystemExit(main())
