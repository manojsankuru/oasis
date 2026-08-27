# Session Report — geo-agent-demo

**Date:** 2026-08-25
**Project:** `C:\Projects\oasis\geo-agent-demo`
**Goal:** a minimal, experimental spatial AI agent — an LLM picks from a few predefined
tools, the tools run deterministically in GeoPandas, results go back to the LLM, and the
loop repeats until a final answer. Every step logged.

Explicitly **not** a production architecture. No LangChain, LangGraph, CrewAI, MCP,
vector DB, RAG, Streamlit, Docker, or ArcPy.

---

## 1. Outcome

Working end to end against the live Clemson LLM proxy.

| Check | Result |
| --- | --- |
| API reachable | pass |
| Plain chat completion | pass |
| **Tool calling** | pass |
| Agent answers demo Q1 | correct |
| Agent answers demo Q2 | correct |
| Deterministic tool math | verified against hand calculations |
| Step logging (JSONL + transcript) | working |
| Terminal trace | working |
| Project venv | built, both entry points pass through it |

Typical run: 3 LLM calls, 2–3 GIS tool calls, 11–16 seconds.

---

## 2. Chronology

### 2.1 Orientation

- Target directory existed but was empty. Python 3.11.9. Git root is `C:\Projects`
  (a multi-project repo), and `oasis/` was untracked in it.
- `C:\Projects\oasis` held two PDFs and a docx. Direct PDF read failed — `pdftoppm`
  is not installed. Converted `OASIS GeoAI Disaster Response Agent.pdf` via the
  markitdown MCP tool instead.
- That PDF is a **paper outline** for a competition submission: disaster/public-health
  scenario, shelter siting, hazard exposure, vulnerability, equity, accessibility,
  location-allocation, provenance, sensitivity analysis, failure cases.
- Decision: use it for **domain naming only** (shelters, tracts, flood zone,
  vulnerable population). Do not let it pull provenance tracking, a critic agent,
  sensitivity analysis, or a data-acquisition layer into a task specified as minimal.

### 2.2 Write enforcement and the worktree

- First write to `C:\Projects\oasis\geo-agent-demo\requirements.txt` was **rejected**:
  background sessions cannot edit the shared checkout until isolated.
- `EnterWorktree` → `C:\Projects\.claude\worktrees\geo-agent-demo`, branch
  `worktree-geo-agent-demo`.
- All 16 files were written there. This is why the files did not appear where they
  were expected — see §6.1.

### 2.3 Build

Sixteen files: four at the root, three `.gitkeep` placeholders keeping `data/`,
`outputs/` and `logs/` in git, and nine modules under `src/`. The table in §3 lists the
thirteen that carry content; the `.gitkeep` files are empty by design.

### 2.4 Dependency situation

The global Python 3.11 had `openai`, `pydantic`, and `python-dotenv` but **not**
`geopandas`, `shapely`, or `pyproj`. Rather than install a heavy geo stack into the
user's system Python unprompted, a throwaway venv was created in the job temp
directory for verification. A permanent project venv came later (§2.12).

Resolved versions: geopandas 1.1.4, shapely 2.1.2, pyproj 3.7.2, pydantic 2.13.4,
openai 3.3.1.

### 2.5 Verifying the deterministic half

Each tool was called directly with fixed arguments. Results matched hand calculations.
The decisive check: a 0.05° latitude offset returned **5.58 km** (true value ≈ 5.55 km),
proving distances are real ground metres and not degrees.

Also exercised: unknown shelter name, missing required argument, unknown tool name.
All three return an error dict rather than raising.

### 2.6 Verifying the agent loop offline

The loop was tested with a stub LLM — no API, no credentials. Confirmed:

- three turns, ending on a final answer
- **two tool calls in a single assistant message**, both answered
- every `tool_call_id` matched by exactly one `role: "tool"` reply, in order
- assistant message appended to history *before* its tool replies
- a pydantic `ValidationError` surfaced back to the model as a tool result
- `MAX_ITERATIONS` guard tripping cleanly with `stop_reason: "max_iterations"`

### 2.7 The SDK-surface gap

A review pass caught that **no actual openai SDK code had executed**: the loop test
stubbed both `make_client` and `chat`, and `demo`/`test_api` returned at the
missing-settings guard before constructing a client. Since pip resolved **openai
3.3.1** (which pulls `httpx2`, signalling a different SDK generation from the 1.x line),
the assumed call shape was unverified.

Verified offline, no network:

- `chat.completions.create` accepts `model`, `messages`, `tools`, `tool_choice`
- `ChatCompletion.choices` → `Choice.message` / `.finish_reason`
- `ChatCompletionMessage.content` / `.tool_calls`
- tool call → `.id`, `.function` → `.name`, `.arguments`

One fix: `client.models.list()` was being read as `.data`. Changed to iterate the
return directly, which works whether it is a page object or an iterator.

### 2.8 Delivery correction

Files were reported as living in the worktree with a copy command supplied, rather than
being copied. They were then copied to `C:\Projects\oasis\geo-agent-demo` and re-verified
from that path. The destination previously held only `.claude\settings.local.json`,
which was left untouched.

### 2.9 Endpoint and model discovery

The `.env.example` was updated with two URLs and a model. Investigation found:

1. **The two URLs serve different model families.** `.../openai/v1` (the proxy) serves
   OpenAI models only — no GLM anywhere in its catalog. `glm-5.1-fp8` therefore belongs
   to the other endpoint, `.../v1`.
2. **`/models` lists far more than the proxy will actually serve.** The catalog returns
   124 entries including `gpt-5.1`, but calling it 404s with
   `model "gpt-5.1" is not available from provider "OpenAI"`.

Probed individually:

| Works | Listed but refused |
| --- | --- |
| `gpt-5`, `gpt-5-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o`, `gpt-4o-mini`, `gpt-3.5-turbo` | `gpt-5.1`, `gpt-5.1-chat-latest`, `gpt-5-chat-latest` |

Settled on **`gpt-5`**, the closest working model to what was asked for. Tool calling
confirmed working on it.

`config.py` gained `OPENAI_PROXY_URL` support: if set it wins over
`CLEMSON_API_BASE_URL`, and `ENDPOINT_SOURCE` records which one was used so the trace
and `test_api` can print it.

### 2.10 Credential handling

The updated `.env.example` contained a **real 32-character API key**. That file is the
one env file deliberately *not* git-ignored — it is the template meant to be committed.

Actions: key moved to `.env` (git-ignored, verified against the project `.gitignore`),
`.env.example` restored to `REPLACE-ME`.

Verified the key never reached git: `git log --all -S<key>` returned no commits, and
`oasis/` is untracked in `C:\Projects`. **No rotation was necessary.**

Also handled: the key had been written as `CLEMSON_API_KEY = "value"` with spaces and
quotes. `config.py` now strips whitespace and surrounding quotes from every setting.

### 2.11 Terminal trace

Added `src/trace.py` — all formatting lives there so `agent.py` stays the loop.
Output format:

```
USER / STEP n — LLM / STEP n — TOOL RESULT / STEP n — TOOL ERROR /
FINAL ANSWER / TOTAL LLM CALLS / TOTAL GIS TOOL CALLS / TOTAL DURATION
```

Long lists collapse to `tracts: 9 rows` for readability; the JSONL keeps every value.
`demo.py` prints a combined RUN TOTALS block for multi-question runs.

A first version printed the final answer twice — once as `Says:` and again under
`FINAL ANSWER`. Fixed: `Says:` now appears only when the model emits text *and* calls a
tool in the same turn.

Rendering of `TOOL ERROR`, the retry step, and `STOPPED` was verified separately.

### 2.12 Project venv

Created `.venv` in the project root (268 MB, git-ignored) with all six dependencies.
Both `test_api` and `demo` pass through it. Activation itself was verified, not just
the direct-interpreter path.

---

## 3. What was built

| File | Purpose |
| --- | --- |
| `requirements.txt` | the six dependencies, unpinned |
| `.env.example` | template for the four settings; committed, no secrets |
| `.gitignore` | excludes `.env`, `.venv`, logs, outputs, generated GeoJSON |
| `README.md` | setup, run order, file table, trace guide, verified numbers |
| `src/config.py` | loads `.env` from project root, paths, CRS constants, endpoint choice |
| `src/test_api.py` | three ordered API checks, ending with tool calling |
| `src/create_sample_data.py` | writes the three synthetic layers |
| `src/spatial_tools.py` | the four tools over GeoPandas |
| `src/schemas.py` | pydantic arg models → OpenAI tool specs |
| `src/llm_client.py` | the only file that talks to the API or parses tool calls |
| `src/agent.py` | the loop, logging, counters |
| `src/trace.py` | terminal formatting |
| `src/demo.py` | entry point |

### 3.1 Sample data

Synthetic, fixed coordinates, no randomness. Coastal South Carolina.

- **tracts** — 9 polygons, 3×3 grid, with `population`, `pct_vulnerable`,
  `vulnerable_population`
- **shelters** — 5 points with `name`, `capacity`
- **flood_zone** — 1 polygon covering the eastern third

Layout (north up, `*` = in flood zone):

```
T07 (North Church Hall)   T08                     T09 *
T04                       T05 (Central Middle)    T06 * (Harbor Center)
T01 (Riverside High)      T02                     T03 * (Eastside Rec)
```

T02, T04, T08 and T09 contain no shelter — which is what makes the underserved-tract
question produce a meaningful answer.

### 3.2 The four tools

| Tool | Spatial operation |
| --- | --- |
| `list_layers` | metadata, so the model need not guess attribute names |
| `shelters_in_hazard_zone` | point-in-polygon against the flood zone |
| `population_within_distance` | buffer, then centroid-in-buffer selection |
| `nearest_shelter_distances` | centroid to nearest point distance |

Each returns a small JSON-serialisable dict. **Geometry is never sent to the model.**

### 3.3 Verified numbers

```
shelters_in_hazard_zone
  exposed: Eastside Recreation Center, Harbor Community Center  (capacity 1050)
  safe:    Central Middle School, North Church Hall, Riverside High School  (capacity 2300)

nearest_shelter_distances(max_travel_km=5.0)
  mean distance 3.48 km
  underserved tracts: T02 (5.58), T04 (5.66), T09 (8.03)
  underserved population 14,800, of which vulnerable 4,892

population_within_distance("Central Middle School", 8)
  7 of 9 tracts, population 33,800, vulnerable 11,224, 79.2% of the study area
```

The agent's own answers matched these exactly on every live run.

---

## 4. Key findings

### 4.1 The CRS trap — the most important one

Data is stored in EPSG:4326 (degrees). **Buffer, centroid, and distance all return
silently wrong values in a geographic CRS** — degrees, not metres — with no error and
no warning. A demo whose whole point is deterministic execution would have been quietly
reporting distances in degrees.

Mitigation: one `METRIC_CRS` constant (EPSG:5070, CONUS Albers) and a single reproject
helper that every buffer/centroid/distance call routes through. If anyone extends
`spatial_tools.py`, this is the single thing most likely to break silently.

### 4.2 Model catalog ≠ model access

`GET /models` mirrors the upstream catalog. The proxy grants a subset. A model can be
listed and still 404. Never assume availability from the listing — call it.

### 4.3 Tool calling is the load-bearing capability

Many OpenAI-compatible servers serve `/v1/chat/completions` correctly and ignore the
`tools` parameter entirely. This is why `test_api.py` check 3 exists and why it is
ordered last: checks 1 and 2 passing tells you nothing about whether the agent design
is viable. Parsing is confined to `llm_client.py` so that a different tool-call format
would require changes in exactly one file.

### 4.4 Flat schemas

`model_json_schema()` emits `$defs`/`$ref` for nested models, and non-OpenAI servers
frequently reject those. All arg models use only `str`/`float` scalars — confirmed no
`$ref` or `$defs` appears in the emitted specs.

### 4.5 Error recovery is deliberately thin

A bad argument, unknown tool, or exception inside a tool becomes an error dict handed
back as the tool result. The model corrects itself on the next turn. Nothing retries
automatically. This was a scope decision, not an oversight.

---

## 5. Decisions and rationale

| Decision | Why |
| --- | --- |
| Domain from the OASIS PDF, scope from the brief | keeps naming meaningful without importing the full paper's architecture |
| Four tools, not more | covers overlay, buffer, spatial join, and distance — the four basic GeoPandas operations — with nothing redundant |
| `list_layers` as a tool | lets the model discover schema instead of hallucinating attribute names; the model does call it first unprompted |
| Small dicts, never geometry | keeps token cost bounded and results legible |
| Synthetic data with fixed coordinates | reproducible; the same run gives the same numbers every time |
| Verify with a stub LLM before spending API calls | tests the loop mechanics that break on the second iteration (id matching, message ordering) without credentials |
| venv rather than global install | the geo stack is heavy; the user's system Python stays clean |
| Did not push | the only git remote is `rl-football-debugger`, an unrelated repository |

### 5.1 Where these decisions came from

Two structured review passes shaped the work, and most of §4 traces to them.

**Before any code was written**, the first pass set: probe the write guard with a single
file rather than discovering it on file nine; make `test_api` prove *tool calling* and
not merely connectivity; keep the pydantic schemas flat to avoid `$ref`; route every
buffer, centroid and distance through one metric-CRS helper; and cap tool results to
small dicts.

**After both test suites were green**, the second pass caught the gap in §2.7: no real
openai SDK code had executed. The loop test stubbed `make_client` and `chat`; `demo` and
`test_api` both returned at the missing-settings guard before constructing a client.

That second finding is the transferable lesson from this session:

> Two passing test suites, and the load-bearing integration was still completely
> untested — because both suites mocked it. Green tests measure what they cover, not
> what matters.

The fix cost one offline introspection call and caught a genuine bug (`models.list().data`
versus iterating the return) on an SDK generation that had silently changed underneath
the code.

---

## 6. Incidents

### 6.1 Files delivered to the wrong place

Background sessions cannot write to the shared checkout, so the work landed in a
worktree. The first report gave a copy command instead of running it, and the files were
not where they were expected. Corrected by copying them and re-verifying from the real
path.

Follow-up: `worktree.bgIsolation` was set to `"none"` (§7) so future background jobs
write directly.

### 6.2 Real API key in a committed-by-design file

Covered in §2.10. Caught before any commit; no exposure.

### 6.3 Shelter placed exactly on a tract centroid

`Riverside High School` was initially at `(-80.02, 32.75)` — precisely tract T01's
centroid — producing a `0.0 km` nearest-shelter distance that reads like a broken
calculation. Moved to `(-80.03, 32.74)`; now reports 1.45 km.

### 6.4 `.env.example` disappeared

Present at one point in the session and absent later. **Cause not determined** — several
`Copy-Item -Force` syncs and `Remove-Item` sweeps ran in between and none was positively
ruled out. Restored from the worktree copy with the placeholder key. If its removal was
deliberate, delete it again — but `README.md` references it and a fresh clone needs it.

---

## 7. Changes made outside the project

`C:\Users\manoj\.claude\settings.json` (user-global), by request:

```json
"worktree": { "baseRef": "head", "bgIsolation": "none" },
"permissions": { "allow": ["PowerShell(python *)", "PowerShell(pip *)"] }
```

`bgIsolation: "none"` lets background jobs edit the working copy directly. Verified live
by writing to a path that had been blocked earlier in the session. **Trade-off:** two
background jobs running at once in the same repo can now overwrite each other's edits
and any uncommitted work. Set globally, so it applies to every repository on the
machine. Revert with `"bgIsolation": "worktree"`.

---

## 8. Git state

Branch `worktree-geo-agent-demo`, worktree at `C:\Projects\.claude\worktrees\geo-agent-demo`:

```
07c087e3  proxy url and model checks
265cb355  models list shape
4931baed  readme windows note
df36db23  geo agent demo
```

**The live copy has diverged from this branch and is ahead of it.** Uncommitted in
`C:\Projects\oasis\geo-agent-demo`:

- `src/trace.py` — new, absent from the branch
- `src/agent.py`, `src/demo.py`, `README.md` — modified

Nothing was pushed. `oasis/` remains untracked in the `C:\Projects` repository, which
contains a dozen unrelated projects. **Open decision:** whether `geo-agent-demo` should
become its own git repository (`git init` in the project folder) rather than living
untracked inside the grab-bag repo. Recommended, but not done without instruction.

---

## 9. Limitations

Deliberate, all out of scope for this step:

- **Data is synthetic.** Real work needs Census/ACS, FEMA, NOAA sources.
- **Distances are straight-line**, not road network travel time. A river or a closed
  bridge would invalidate the accessibility numbers entirely.
- No data acquisition, no provenance or metadata tracking, no CRS auto-detection for
  incoming datasets.
- No optimization or location-allocation — the agent reports, it does not recommend
  where to put a new shelter.
- No critic or validation agent, no sensitivity analysis, no scenario comparison.
- Tract-level results use centroid containment, not area-weighted apportionment.
- `MAX_ITERATIONS` is 6; complex multi-part questions may hit it.

The OASIS paper outline calls for most of the above. This project deliberately proves
one thing only: **that the LLM can reliably drive deterministic spatial tools.**

---

## 10. How to run

```powershell
cd C:\Projects\oasis\geo-agent-demo
.\.venv\Scripts\Activate.ps1
python -m src.test_api
python -m src.demo
python -m src.demo "your question here"
```

Without activating, substitute `.\.venv\Scripts\python.exe` for `python`.
Run as a module from the project root — `python src/demo.py` fails on imports.

Every run writes `logs/run_<id>.jsonl` (one JSON object per step, full untruncated
results) and `outputs/run_<id>.json` (full message list plus steps). The trace is for
watching; the JSONL is for auditing.

---

## 11. Suggested next steps

1. Decide the git question in §8 — `git init` the project, or commit the divergence to
   the worktree branch.
2. Swap one synthetic layer for a real one (Census tract boundaries with population) and
   see what breaks — CRS mismatch and missing attributes are the likely failures.
3. Add a road network and replace straight-line distance with travel time. This is the
   single change that most improves the credibility of the accessibility results.
4. Add a validation step that re-checks the agent's final numbers against the tool
   outputs, catching any number the model introduced on its own.
5. Decide whether the direct endpoint (`.../v1`, GLM models) is needed, and if so test
   whether those models support tool calling — this was never checked.
