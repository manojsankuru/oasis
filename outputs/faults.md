# Robustness under injected retrieval faults

Generated 2026-10-26T01:32:30 by `python -m src.experiments.faults`.

`rate` is per network attempt, not per dataset -- see the module docstring in `src/faults.py`. Each fixture cell is 20 seeded runs; each live cell is 6. `correct` means the run returned the same feature count and declared CRS as the clean run; `completed` means only that it returned; `completed but wrong` counts the runs that returned and were not correct -- the silent failures, which should be zero.

| source | fault | rate | runs | completed | correct | completed but wrong | recovery | recovery when faulted | mean extra calls | calls | injected | not applicable | failure |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| fixture | none (baseline) | 0.00 | 20 | 20 | 20 | 0 | 100% | n/a | 0.00 | 60 | 0 | 0 | - |
| fixture | `timeout` | 0.25 | 20 | 20 | 20 | 0 | 100% | 100% | 1.00 | 80 | 20 | 0 | - |
| fixture | `timeout` | 0.50 | 20 | 13 | 13 | 0 | 65% | 59% | 1.65 | 93 | 47 | 0 | TransientError |
| fixture | `server_error` | 0.25 | 20 | 20 | 20 | 0 | 100% | 100% | 1.00 | 80 | 20 | 0 | - |
| fixture | `server_error` | 0.50 | 20 | 13 | 13 | 0 | 65% | 59% | 1.65 | 93 | 47 | 0 | TransientError |
| fixture | `empty` | 0.25 | 20 | 17 | 17 | 0 | 85% | 50% | 1.20 | 84 | 16 | 6 | FeatureCountMismatch |
| fixture | `empty` | 0.50 | 20 | 12 | 12 | 0 | 60% | 33% | 2.30 | 106 | 40 | 12 | FeatureCountMismatch |
| fixture | `wrong_crs` | 0.25 | 20 | 18 | 18 | 0 | 90% | 33% | 0.30 | 66 | 5 | 10 | CRSMismatch |
| fixture | `wrong_crs` | 0.50 | 20 | 14 | 14 | 0 | 70% | 33% | 0.90 | 78 | 15 | 24 | CRSMismatch |
| fixture | `truncated` | 0.25 | 20 | 20 | 20 | 0 | 100% | 100% | 0.50 | 70 | 5 | 11 | - |
| fixture | `truncated` | 0.50 | 20 | 17 | 17 | 0 | 85% | 67% | 1.50 | 90 | 18 | 27 | FeatureCountMismatch |

Rows marked `fixture` ran against a real HTTP server on loopback serving a synthetic FeatureSet; rows marked `live` ran against the service the snapshot came from. Nothing here writes to `data/snapshot/`.

Every row retrieves through `acquire.fetch_arcgis_vector`, so the `completed but wrong` column covers ArcGIS vector layers. It says nothing about the Overpass or raster retrievals, and nothing about a service that refuses `returnCountOnly` on every count request, where a short page still passes with a provenance note saying the total went unchecked. The injector never produces that refusal.
