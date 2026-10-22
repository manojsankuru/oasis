# Robustness under injected retrieval faults

Generated 2026-09-01T19:21:24 by `python -m src.experiments.faults`.

`rate` is per network attempt, not per dataset -- see the module docstring in `src/faults.py`. Each fixture cell is 20 seeded runs; each live cell is 6. `correct` means the run returned the same feature count and declared CRS as the clean run; `completed` means only that it returned.

| source | fault | rate | runs | completed | correct | recovery | recovery when faulted | mean extra calls | calls | injected | not applicable | failure |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| fixture | none (baseline) | 0.00 | 20 | 20 | 20 | 100% | n/a | 0.00 | 40 | 0 | 0 | - |
| fixture | `timeout` | 0.25 | 20 | 20 | 20 | 100% | 100% | 0.60 | 52 | 12 | 0 | - |
| fixture | `timeout` | 0.50 | 20 | 16 | 16 | 80% | 71% | 1.00 | 60 | 27 | 0 | TransientError |
| fixture | `server_error` | 0.25 | 20 | 20 | 20 | 100% | 100% | 0.60 | 52 | 12 | 0 | - |
| fixture | `server_error` | 0.50 | 20 | 16 | 16 | 80% | 71% | 1.00 | 60 | 27 | 0 | TransientError |
| fixture | `empty` | 0.25 | 20 | 20 | 17 | 85% | 0% | 0.00 | 40 | 3 | 6 | - |
| fixture | `empty` | 0.50 | 20 | 20 | 14 | 70% | 0% | 0.00 | 40 | 6 | 12 | - |
| fixture | `wrong_crs` | 0.25 | 20 | 17 | 17 | 85% | 0% | 0.00 | 40 | 3 | 6 | CRSMismatch |
| fixture | `wrong_crs` | 0.50 | 20 | 14 | 14 | 70% | 0% | 0.00 | 40 | 6 | 12 | CRSMismatch |
| fixture | `truncated` | 0.25 | 20 | 20 | 17 | 85% | 0% | 0.00 | 40 | 3 | 6 | - |
| fixture | `truncated` | 0.50 | 20 | 20 | 14 | 70% | 0% | 0.00 | 40 | 6 | 12 | - |

Rows marked `fixture` ran against a real HTTP server on loopback serving a synthetic FeatureSet; rows marked `live` ran against the service the snapshot came from. Nothing here writes to `data/snapshot/`.
