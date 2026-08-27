---
description: Run the acceptance gate before committing a module
argument-hint: [module name, e.g. align]
---

Run the acceptance gate for `$ARGUMENTS`. Do not commit until every line below
is checked. Report each as PASS or FAIL with the evidence, and stop at the
first FAIL.

1. **Invariant sweep.** Re-read the hard invariants in `CLAUDE.md`. For each
   one, point at the specific code that upholds it or state that it does not
   apply to this module. Pay particular attention to:
   - any area/distance/buffer/centroid/zonal call that does not go through the
     metric-CRS helper
   - any literal county name, FIPS code, state code, or bbox outside `config.py`
   - any dataset reachable without a `Provenance` record
   - any network call without an explicit timeout
   - any tool result that could contain coordinates
2. **No manual cleaning.** Search the diff for hardcoded values that should
   have been retrieved or derived, and for any edit to a file under `data/`.
   A snapshot file changed by hand is an automatic FAIL.
3. **Real boundary exercised.** If this module touches the network, a model, or
   a subprocess: name the check that ran the real thing. A test that mocks the
   thing under test does not count. If no such check exists, write one now.
4. **Type hints.** Every new function annotated.
5. **Run it.** Execute the module's entry point and paste the actual output —
   not a description of what it should produce.
6. **Failures logged.** If anything broke during this work, confirm it is in
   `docs/failures.md` with the date.

Then propose a one-line commit message and wait for approval before committing.
