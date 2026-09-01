"""Injectable retrieval failures, for the robustness experiment.

Five kinds, taken from `contracts.FaultKind` and not extended here: `timeout`,
`server_error`, `empty`, `wrong_crs`, `truncated`.

WHERE THE FAULT GOES, and why
-----------------------------
Faults are injected at `acquire._SESSION`, the session object that
`acquire._request` calls at acquire.py's single outbound call site, inside the
body the tenacity decorator retries.

Three placements were considered and this is the third reading of the first one:

* Replacing the decorated `acquire._request` was the obvious choice -- its own
  docstring names this module as the thing that would wrap it. It was rejected
  on one detail. `_request` IS the retry: replacing it means rebuilding
  `stop_after_attempt(config.MAX_RETRIES)` and `wait_exponential` by hand, and a
  hand-rebuilt retry policy is exactly the way invariant 7 gets weakened by a
  harness whose whole purpose is to measure it. Injecting one layer down leaves
  the decorator, its policy and its exception filter untouched: the function
  object that runs under fault is the same object that runs in production.
* Wrapping each `acquire_*` retrieval means five wrappers that drift, and a
  sixth retrieval added later is silently un-faulted. That is the shape of a
  hand-maintained inventory, which this project has been bitten by twice.
* Patching `requests` itself catches calls this project does not make, and lands
  OUTSIDE the retry, so the measured system would not be the shipped one.

The choke point is real and was checked rather than assumed: `_session()` is
referenced exactly twice in `acquire.py` -- its own definition and the one call
inside `_request`. `_choke_point_checks` asserts that count, so a second
outbound call added to `acquire` fails this module rather than escaping it.

Injecting at the transport buys a second thing beside safety. Every fault
travels through `acquire`'s own error handling rather than around it: an
injected `timeout` is a real `requests.exceptions.Timeout` that `_request`
converts through `_RETRYABLE_TRANSPORT`, and an injected `server_error` is a
real HTTP 500 that it converts through `_RETRYABLE_STATUS`. Raising
`TransientError` directly would have skipped both conversions and tested this
module's idea of `acquire` instead of `acquire`.

WHAT `rate` MEANS
-----------------
Per attempt, which is what the frozen contract's "per network call" says: a
retried call is a second network call. It is NOT per dataset.

The distinction is not cosmetic. `_request` retries a `TransientError` up to
`config.MAX_RETRIES` times, so at rate 0.5 a single request survives unless all
three attempts are hit -- about one time in eight. A recovery rate computed
against a per-dataset denominator would be a wrong number that looks right.
`FaultPlan` counts attempts and injections, the table prints both, and
`extra_turns` is measured against the attempt count of the clean run rather than
assumed.

RAISED VERSUS SUBSTITUTED
-------------------------
`timeout` and `server_error` are failures the transport reports. The other
three are successful responses carrying wrong content, and they are injected as
substituted response bodies rather than as raised errors -- otherwise all five
kinds would exercise the retry path and none would exercise the data-handling
path, while the report said five kinds were tested.

* `empty`      -- HTTP 200 with zero features. `flood_zones` is the real one.
* `wrong_crs`  -- HTTP 200 declaring a spatial reference that was not requested.
                  `acquire._received_crs` raises `CRSMismatch`, which is not
                  retryable, so this one fails on the first attempt by design.
* `truncated`  -- a short page with `exceededTransferLimit` absent, which is the
                  ArcGIS trap docs/DATA.md section 1 documents. Nothing raises;
                  the caller gets fewer features than exist and no signal.

A content fault needs a FeatureSet to corrupt. Against a layer-metadata reply or
a raster body there is nothing to make empty or to mis-declare, so the attempt is
recorded as not applicable and passed through untouched rather than counted as an
injection that did not happen.

OFF BY DEFAULT
--------------
`_ACTIVE` is `None` until `injecting()` opens a session and is `None` again in
its `finally`, including when the body raises. Nothing in this module fires
without a `FaultConfig`. `armed()` reports the state and `_guard_checks` asserts
it in both directions -- the `tools.ELICIT` pattern, for the same reason.
"""

from __future__ import annotations

import inspect
import json
import random
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, get_args

import requests

from . import acquire, config, verify
from .contracts import FaultConfig, FaultEvent, FaultKind

RAISED_KINDS: tuple[FaultKind, ...] = ("timeout", "server_error")
"""Injected as an exception the transport would have raised, inside the retry."""

SUBSTITUTED_KINDS: tuple[FaultKind, ...] = ("empty", "wrong_crs", "truncated")
"""Injected as a successful response whose CONTENT is wrong."""

ALL_KINDS: tuple[FaultKind, ...] = RAISED_KINDS + SUBSTITUTED_KINDS
"""Every kind this module can inject. A check asserts this equals the frozen
`FaultKind` alias, so a sixth kind added to the contract cannot go untested and
a kind dropped from here cannot be reported as covered."""

INJECTED_STATUS = 500
"""What `server_error` answers with. In `acquire._RETRYABLE_STATUS`, so the
existing policy retries it; a check asserts that membership rather than trusting
this comment."""

FEATURE_KEY = "features"
LIMIT_KEY = "exceededTransferLimit"
SR_KEY = "spatialReference"

OID_FIELD = "OBJECTID"
SAMPLE_FEATURE_COUNT = 10
"""How many features the local service publishes. Even, and above one, so
`truncated` has a shorter page to return that is not the `empty` case."""


class FaultNotApplicable(RuntimeError):
    """This response has nothing the requested content fault could corrupt."""


def other_sr(requested: int) -> int:
    """A spatial reference that is definitely not the one asked for.

    Not a constant, because `acquire._received_crs` accepts either the wkid or
    the latestWkid the service reports, and Esri 102100 IS EPSG:3857 -- a fixed
    substitute would be silently accepted whenever it happened to match the
    request, and the fault would report as fired without firing.
    """
    return 4326 if requested != 4326 else 3857


def synthetic_response(
    method: str, url: str, status: int, body: bytes, content_type: str
) -> requests.Response:
    """A real `requests.Response` carrying a body this module wrote.

    Built rather than faked so that everything downstream -- the status check in
    `_request`, `_parse_json`, the Content-Type diagnosis in `NonJsonResponse` --
    reads it the way it reads a response off the wire.
    """
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.request = requests.Request(method=method, url=url).prepare()
    response.headers["Content-Type"] = content_type
    response._content = body
    response._content_consumed = True
    return response


def requested_sr(params: Any) -> int:
    """The `outSR` this call asked for, defaulting to the storage CRS code."""
    if isinstance(params, dict):
        value = params.get("outSR")
        if value is not None:
            return int(value)
    return 4326


def corrupt(kind: FaultKind, response: requests.Response, out_sr: int) -> requests.Response:
    """Rewrite a successful response so its content is wrong in one stated way.

    Mutates and returns the response it was given, so status, headers and URL
    stay exactly what the service sent and only the body differs. Raises
    `FaultNotApplicable` when the body is not a FeatureSet, or when it is one the
    named corruption cannot be performed on -- a single feature cannot be
    shortened into a shorter page without becoming the `empty` case, and
    reporting one kind while injecting another is the failure this separation
    exists to prevent.
    """
    if kind not in SUBSTITUTED_KINDS:
        raise FaultNotApplicable(f"{kind} is raised, not substituted")
    try:
        payload = response.json()
    except ValueError as exc:
        raise FaultNotApplicable(f"body is not JSON: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or FEATURE_KEY not in payload:
        raise FaultNotApplicable("body is not a FeatureSet, so it carries no features")

    features = list(payload.get(FEATURE_KEY) or [])
    if kind == "empty":
        payload[FEATURE_KEY] = []
        payload.pop(LIMIT_KEY, None)
    elif kind == "wrong_crs":
        substitute = other_sr(out_sr)
        payload[SR_KEY] = {"wkid": substitute, "latestWkid": substitute}
    else:
        if len(features) < 2:
            raise FaultNotApplicable(
                f"a page of {len(features)} feature(s) cannot be shortened "
                "without emptying it"
            )
        payload[FEATURE_KEY] = features[: len(features) // 2]
        payload.pop(LIMIT_KEY, None)

    response._content = json.dumps(payload).encode("utf-8")
    response._content_consumed = True
    return response


@dataclass(slots=True)
class FaultPlan:
    """The seeded decision sequence and the tally for one faulted session.

    `rng` is seeded from `FaultConfig.seed` and consumed once per intercepted
    call, so two sessions at the same seed and rate make the same decisions in
    the same order. That is the difference between an experiment and an anecdote,
    and `_seed_checks` proves it rather than assuming it.
    """

    settings: FaultConfig
    dataset: str = ""
    attempts: int = 0
    injected: int = 0
    not_applicable: int = 0
    decisions: list[bool] = field(default_factory=list)
    timeouts_seen: list[Any] = field(default_factory=list)
    rng: random.Random = field(default_factory=random.Random, repr=False)

    def __post_init__(self) -> None:
        self.rng.seed(self.settings.seed)

    def decide(self, timeout: Any) -> bool:
        """Draw one decision for one outbound call, and record what it carried.

        The timeout is recorded on every intercepted call, whether or not a fault
        fires, because invariant 7 is about the calls the harness lets through as
        much as the ones it stops.
        """
        self.attempts += 1
        self.timeouts_seen.append(timeout)
        fires = self.rng.random() < self.settings.rate
        self.decisions.append(fires)
        return fires

    def every_call_carried_a_timeout(self) -> bool:
        """Did every intercepted call name a deadline? Invariant 7, observed."""
        return bool(self.timeouts_seen) and all(
            value is not None for value in self.timeouts_seen
        )


class FaultedSession(requests.Session):
    """A session that fails a seeded fraction of the calls made through it.

    Delegates every call it does not fault to the session `acquire` was already
    using, so headers, connection pool and adapters are the shipped ones rather
    than replacements.
    """

    def __init__(self, inner: requests.Session, plan: FaultPlan) -> None:
        super().__init__()
        self.headers.update(inner.headers)
        self.inner = inner
        self.plan = plan

    def request(  # type: ignore[override]
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        """Draw once, then either fault this call or hand it to the real session."""
        plan = self.plan
        if not plan.decide(kwargs.get("timeout")):
            return self.inner.request(method, url, **kwargs)

        kind = plan.settings.kind
        if kind == "timeout":
            plan.injected += 1
            raise requests.exceptions.Timeout(f"injected {kind} for {url}")
        if kind == "server_error":
            plan.injected += 1
            return synthetic_response(
                method,
                url,
                INJECTED_STATUS,
                b'{"error": "injected server error"}',
                "application/json",
            )

        response = self.inner.request(method, url, **kwargs)
        try:
            faulted = corrupt(kind, response, requested_sr(kwargs.get("params")))
        except FaultNotApplicable:
            plan.not_applicable += 1
            return response
        plan.injected += 1
        return faulted


_ACTIVE: FaultPlan | None = None
"""The open session, or None. None is the state of this module at import and the
state it returns to in `injecting`'s finally."""


def armed() -> bool:
    """Is a fault session open? False unless `injecting` is inside its block."""
    return _ACTIVE is not None


def active() -> FaultPlan | None:
    """The open plan, or None. The experiment reads its tally through this."""
    return _ACTIVE


@contextmanager
def injecting(settings: FaultConfig, dataset: str = "") -> Iterator[FaultPlan]:
    """Install the injector for the duration of the block, and remove it after.

    Refuses to nest. Two open plans would draw from two seeded sequences against
    one call stream, and the resulting fault order would depend on which wrapper
    happened to be inside the other -- reproducible in neither.
    """
    global _ACTIVE
    if _ACTIVE is not None:
        raise RuntimeError(
            "a fault session is already open; nesting would make the fault "
            "sequence depend on nesting order rather than on the seed"
        )
    inner = acquire._session()
    plan = FaultPlan(settings=settings, dataset=dataset)
    acquire._SESSION = FaultedSession(inner, plan)
    _ACTIVE = plan
    try:
        yield plan
    finally:
        acquire._SESSION = inner
        _ACTIVE = None


# ---------------------------------------------------------------------------
# a real service on localhost, so the five kinds can be observed offline
# ---------------------------------------------------------------------------


def sample_featureset(count: int, out_sr: int, offset: int, page: int) -> dict[str, Any]:
    """One page of an Esri JSON FeatureSet, shaped the way TIGERweb ships one.

    Synthetic coordinates and synthetic identifiers. The data is not the thing
    under test; the fault path is, and it needs a real FeatureSet to travel
    through `_query_features`, `_received_crs` and the ESRIJSON driver.
    """
    rows = [
        {
            "attributes": {
                OID_FIELD: index + 1,
                "GEOID": f"{99_000_000_000 + index:011d}",
                "NAME": f"unit {index + 1}",
            },
            "geometry": {"x": -100.0 + index * 0.01, "y": 40.0 + index * 0.01},
        }
        for index in range(offset, min(offset + page, count))
    ]
    return {
        "objectIdFieldName": OID_FIELD,
        "geometryType": "esriGeometryPoint",
        SR_KEY: {"wkid": out_sr, "latestWkid": out_sr},
        "fields": [
            {"name": OID_FIELD, "type": "esriFieldTypeOID", "alias": OID_FIELD},
            {"name": "GEOID", "type": "esriFieldTypeString", "alias": "GEOID", "length": 11},
            {"name": "NAME", "type": "esriFieldTypeString", "alias": "NAME", "length": 40},
        ],
        LIMIT_KEY: offset + page < count,
        FEATURE_KEY: rows,
    }


def sample_metadata() -> dict[str, Any]:
    """The layer description `fetch_arcgis_vector` reads before it queries."""
    return {
        "id": 0,
        "name": "local fixture layer",
        "type": "Feature Layer",
        "currentVersion": 11.2,
        "copyrightText": "synthetic fixture, no license",
        "objectIdField": OID_FIELD,
        "geometryType": "esriGeometryPoint",
        "fields": sample_featureset(0, 4326, 0, 0)["fields"],
    }


class _FixtureHandler(BaseHTTPRequestHandler):
    """Answers the two requests `fetch_arcgis_vector` makes, and nothing else."""

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        params = {
            key: value[0]
            for key, value in [
                (item.split("=", 1) + [""])[:2] for item in query.split("&") if item
            ]
            for value in [[value]]
        }
        if path.rstrip("/").endswith("/query"):
            out_sr = int(params.get("outSR", 4326))
            offset = int(params.get("resultOffset", 0))
            page = int(params.get("resultRecordCount", acquire.PAGE_SIZE))
            body = sample_featureset(SAMPLE_FEATURE_COUNT, out_sr, offset, page)
        else:
            body = sample_metadata()
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the per-request line; the checks print their own."""
        return


@contextmanager
def local_service() -> Iterator[str]:
    """A real HTTP server on a loopback port, yielded as a service URL.

    A real socket, real headers and a real status line, so `_request`,
    `_query_features`, `_received_crs` and the ESRIJSON driver all run for real
    against it. Stubbing `_request` instead would have tested this module's idea
    of `acquire`, which CLAUDE.md names as the thing two green suites here have
    already done.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# one run of one retrieval, clean or faulted
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunOutcome:
    """What one retrieval did, and what it cost.

    `correct` is not `completed`. A run that finishes and returns the wrong
    number of features has not recovered -- it has failed silently, which is the
    outcome `empty` and `truncated` exist to make visible. The table reports the
    two as separate columns for that reason.
    """

    kind: str
    rate: float
    seed: int
    dataset: str
    completed: bool
    correct: bool
    features: int
    crs: str
    attempts: int
    injected: int
    not_applicable: int
    extra_turns: int
    error: str
    timeouts_ok: bool

    def event(self) -> FaultEvent:
        """The frozen record for this run."""
        return FaultEvent(
            kind=self.kind,  # type: ignore[arg-type]
            dataset=self.dataset,
            recovered=self.correct,
            extra_turns=self.extra_turns,
        )


def run_retrieval(
    service_url: str,
    settings: FaultConfig | None,
    *,
    dataset: str,
    expect_features: int,
    expect_crs: str,
    clean_attempts: int,
    layer_id: int = 0,
    out_sr: int = 4326,
    where: str = "1=1",
    timeout_s: float = config.REQUEST_TIMEOUT_S,
) -> RunOutcome:
    """Retrieve one vector layer, optionally under an injected fault.

    With `settings` None nothing is installed and the call is the one `acquire`
    would have made on its own -- that is the clean row of the table, and it is
    produced by this same function so the comparison is between two runs of one
    code path rather than between a run and a description of one.

    `extra_turns` is the attempts this run spent beyond the attempts the clean
    run needed. Measured, not assumed: a clean run of `fetch_arcgis_vector` costs
    one metadata call plus one page, and a harness that hardcoded two would stop
    being right the moment a layer needed a second page.

    One honest limit on the `settings is None` branch. Nothing is installed
    there, which is the whole point of it, so nothing is counted either: the
    `attempts` it reports is the count `clean_attempt_count()` measured against
    the same URL through a rate-0.0 plan, not a count of this call. It is a
    measurement, taken elsewhere -- do not read it as this run instrumenting
    itself, because a run with no injector cannot.
    """
    plan: FaultPlan | None = None
    completed = False
    features = 0
    crs = ""
    error = ""

    def attempt() -> None:
        nonlocal completed, features, crs, error
        try:
            frame, provenance = acquire.fetch_arcgis_vector(
                service_url, layer_id, where=where, out_sr=out_sr, timeout_s=timeout_s
            )
            completed = True
            features = int(len(frame))
            crs = provenance.declared_crs
        except acquire.AcquisitionError as exc:
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            error = f"{type(exc).__name__}: {exc}"

    if settings is None:
        attempt()
        attempts = clean_attempts
        injected = 0
        not_applicable = 0
        timeouts_ok = True
    else:
        with injecting(settings, dataset=dataset) as opened:
            plan = opened
            attempt()
        attempts = plan.attempts
        injected = plan.injected
        not_applicable = plan.not_applicable
        timeouts_ok = plan.every_call_carried_a_timeout()

    return RunOutcome(
        kind=settings.kind if settings else "none",
        rate=settings.rate if settings else 0.0,
        seed=settings.seed if settings else 0,
        dataset=dataset,
        completed=completed,
        correct=completed and features == expect_features and crs == expect_crs,
        features=features,
        crs=crs,
        attempts=attempts,
        injected=injected,
        not_applicable=not_applicable,
        extra_turns=max(0, attempts - clean_attempts),
        error=error,
        timeouts_ok=timeouts_ok,
    )


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _kind_checks() -> list[tuple[str, bool]]:
    """That the five kinds this module injects are the five the contract names."""
    declared = tuple(get_args(FaultKind))
    overlap = set(RAISED_KINDS) & set(SUBSTITUTED_KINDS)
    print(f"  contract kinds: {list(declared)}")
    print(f"  raised: {list(RAISED_KINDS)}   substituted: {list(SUBSTITUTED_KINDS)}")
    return [
        ("this module injects exactly the kinds the frozen contract declares",
         sorted(ALL_KINDS) == sorted(declared)),
        ("the contract still names five kinds", len(declared) == 5),
        ("no kind is both raised and substituted", not overlap),
        ("every kind is one or the other", len(ALL_KINDS) == len(declared)),
    ]


def _choke_point_checks() -> list[tuple[str, bool]]:
    """That the injection point is still the only outbound call in `acquire`.

    Counted from `acquire`'s source rather than trusted, because the whole
    argument for injecting here is that there is exactly one of it. A second
    call site added tomorrow would be silently un-faulted, and the table would go
    on reporting a rate it was no longer applying to every request.
    """
    source = inspect.getsource(acquire)
    call_sites = len(re.findall(r"_session\(\)\.request\(", source))
    definitions = len(re.findall(r"\ndef _session\(", source))
    mentions = source.count("_session()")
    retry_target = acquire._request.__wrapped__
    print(f"  _session().request( call sites in acquire.py: {call_sites}, "
          f"total mentions {mentions}")
    print(f"  the retry target is {retry_target.__name__} in {retry_target.__module__}")
    return [
        ("acquire makes its outbound calls at exactly one site", call_sites == 1),
        ("the scan reads the source rather than reporting a constant",
         mentions == call_sites + definitions),
        ("there is exactly one session factory to install into", definitions == 1),
        ("the one call site is inside the function tenacity retries",
         "_session().request(" in inspect.getsource(retry_target)),
        ("the injected server status is one acquire already retries",
         INJECTED_STATUS in acquire._RETRYABLE_STATUS),
        ("an injected timeout is a transport error acquire already converts",
         issubclass(requests.exceptions.Timeout, acquire._RETRYABLE_TRANSPORT)),
    ]


def _guard_checks() -> list[tuple[str, bool]]:
    """That the injector is unreachable unless somebody asked for it.

    Asserted in both directions, the way `tools.ELICIT` is: closed, open, closed
    again. A guard only ever checked in its closed state cannot tell a working
    injector from one that never fires, and a guard only ever checked open cannot
    tell an installed one from one that was never removed.
    """
    before_armed = armed()
    before_session = acquire._SESSION
    inside_armed = False
    inside_session: Any = None
    with injecting(FaultConfig(kind="timeout", rate=0.0, seed=1)) as plan:
        inside_armed = armed()
        inside_session = acquire._SESSION
        nested = False
        try:
            with injecting(FaultConfig(kind="timeout", rate=1.0, seed=2)):
                nested = True
        except RuntimeError:
            nested = False
    after_armed = armed()
    after_session = acquire._SESSION

    raised = False
    try:
        with injecting(FaultConfig(kind="timeout", rate=1.0, seed=3)):
            raise ValueError("the block failed")
    except ValueError:
        raised = True

    print(f"  armed before / inside / after: {before_armed} / {inside_armed} / {after_armed}")
    print(f"  session class before / inside / after: {type(before_session).__name__} / "
          f"{type(inside_session).__name__} / {type(after_session).__name__}")
    return [
        ("the injector is off at import and after use",
         not before_armed and not after_armed),
        ("the injector is on only inside the block", inside_armed),
        ("acquire's session is replaced inside the block",
         isinstance(inside_session, FaultedSession)),
        ("acquire's session is the plain one outside the block",
         not isinstance(before_session, FaultedSession)
         and not isinstance(after_session, FaultedSession)),
        ("the session object restored is the one that was there",
         after_session is inside_session.inner),
        ("a second session cannot open inside the first", not nested),
        ("a block that raises still removes the injector",
         raised and not armed() and not isinstance(acquire._SESSION, FaultedSession)),
        ("a plan with rate 0.0 injected nothing", plan.injected == 0),
    ]


def _seed_checks() -> list[tuple[str, bool]]:
    """That the seed makes a run reproducible, and that it does something.

    Both halves are needed. A harness whose seed is ignored produces the same
    sequence for every seed and passes the first assertion; one whose sequence is
    freshly random each time passes neither. `mutate.py` carries a mutation for
    each.
    """
    def sequence(seed: int, rate: float, draws: int) -> list[bool]:
        plan = FaultPlan(settings=FaultConfig(kind="timeout", rate=rate, seed=seed))
        return [plan.decide(config.REQUEST_TIMEOUT_S) for _ in range(draws)]

    first = sequence(7, 0.5, 40)
    again = sequence(7, 0.5, 40)
    other = sequence(8, 0.5, 40)
    never = sequence(7, 0.0, 40)
    always = sequence(7, 1.0, 40)
    print(f"  seed 7, rate 0.5, first 12 draws: {[int(item) for item in first[:12]]}")
    print(f"  seed 8, rate 0.5, first 12 draws: {[int(item) for item in other[:12]]}")
    print(f"  fired: seed 7 {sum(first)}/40, seed 8 {sum(other)}/40")
    return [
        ("two plans at the same seed make the same decisions", first == again),
        ("two plans at different seeds do not", first != other),
        ("the same seed at rate 0.0 fires never", not any(never)),
        ("the same seed at rate 1.0 fires always", all(always)),
        ("rate 0.5 fires on some draws and not others",
         0 < sum(first) < len(first)),
    ]


def _corruption_checks() -> list[tuple[str, bool]]:
    """That each substituted kind changes the body in its own stated way.

    Asserted on what survives, not only on what is gone. The S11 lesson: a check
    that something was removed says nothing about what was left behind, so each
    of these names the value it expects to read back.
    """
    def body(kind: FaultKind, count: int, out_sr: int = 4326) -> dict[str, Any]:
        payload = sample_featureset(count, out_sr, 0, count)
        payload[LIMIT_KEY] = True
        response = synthetic_response(
            "GET", "http://x/0/query", 200, json.dumps(payload).encode(), "application/json"
        )
        return corrupt(kind, response, out_sr).json()

    emptied = body("empty", 10)
    shortened = body("truncated", 10)
    mis_declared = body("wrong_crs", 10)
    unchanged = sample_featureset(10, 4326, 0, 10)

    single = verify.refuses(
        lambda: body("truncated", 1), FaultNotApplicable, "cannot be shortened"
    )
    not_a_featureset = verify.refuses(
        lambda: corrupt(
            "empty",
            synthetic_response("GET", "http://x/0", 200, b'{"name": "layer"}', "application/json"),
            4326,
        ),
        FaultNotApplicable,
        "carries no features",
    )
    not_json = verify.refuses(
        lambda: corrupt(
            "empty",
            synthetic_response("GET", "http://x/0", 200, b"<html></html>", "text/html"),
            4326,
        ),
        FaultNotApplicable,
        "not JSON",
    )
    raised_kind = verify.refuses(
        lambda: corrupt(
            "timeout",
            synthetic_response("GET", "http://x/0/query", 200, b"{}", "application/json"),
            4326,
        ),
        FaultNotApplicable,
        "raised, not substituted",
    )

    print(f"  empty:     {len(emptied[FEATURE_KEY])} feature(s), "
          f"{SR_KEY} {emptied[SR_KEY]}, {LIMIT_KEY} present: {LIMIT_KEY in emptied}")
    print(f"  truncated: {len(shortened[FEATURE_KEY])} of "
          f"{len(unchanged[FEATURE_KEY])} feature(s), "
          f"{LIMIT_KEY} present: {LIMIT_KEY in shortened}")
    print(f"  wrong_crs: {mis_declared[SR_KEY]} for a request that asked for 4326, "
          f"{len(mis_declared[FEATURE_KEY])} feature(s) kept")

    kept_names = [row["attributes"]["NAME"] for row in shortened[FEATURE_KEY]]
    return [
        ("empty returns a FeatureSet with no features",
         emptied[FEATURE_KEY] == []),
        ("empty leaves the declared CRS alone",
         emptied[SR_KEY] == unchanged[SR_KEY]),
        ("empty drops the transfer-limit flag, so paging stops rather than loops",
         LIMIT_KEY not in emptied),
        ("truncated returns the first half of the page, as itself",
         kept_names == ["unit 1", "unit 2", "unit 3", "unit 4", "unit 5"]),
        ("truncated drops the transfer-limit flag, which is what makes it silent",
         LIMIT_KEY not in shortened),
        ("truncated leaves the declared CRS alone",
         shortened[SR_KEY] == unchanged[SR_KEY]),
        ("wrong_crs declares a reference the request did not ask for",
         mis_declared[SR_KEY] == {"wkid": 3857, "latestWkid": 3857}),
        ("wrong_crs keeps every feature, so only the declaration is wrong",
         len(mis_declared[FEATURE_KEY]) == len(unchanged[FEATURE_KEY])),
        ("a substitute CRS is never the one requested",
         all(other_sr(code) != code for code in (4326, 3857, 5070, 102100))),
        ("a page of one is refused rather than emptied and called truncated", single),
        ("a reply with no features is refused rather than corrupted", not_a_featureset),
        ("a non-JSON body is refused rather than corrupted", not_json),
        ("a raised kind is refused by the substituting path", raised_kind),
    ]


def _live_kind_checks() -> list[tuple[str, tuple[str, bool]]]:
    """Every kind, fired at rate 1.0 through the real request path.

    One fixture per kind, because a harness that can only raise exceptions
    reports "all five kinds tested" while three of them never ran, and that reads
    exactly like five working kinds. Each fixture asserts what the faulted call
    RETURNED, not merely that something went wrong.
    """
    results: list[tuple[str, tuple[str, bool]]] = []
    with local_service() as url:
        clean = run_retrieval(
            url, None, dataset="fixture", expect_features=SAMPLE_FEATURE_COUNT,
            expect_crs="EPSG:4326", clean_attempts=0, timeout_s=10.0,
        )
        with injecting(FaultConfig(kind="empty", rate=0.0, seed=0)) as counter:
            acquire.fetch_arcgis_vector(url, 0, out_sr=4326, timeout_s=10.0)
        clean_attempts = counter.attempts
        with injecting(FaultConfig(kind="timeout", rate=0.0, seed=99)) as repeat:
            acquire.fetch_arcgis_vector(url, 0, out_sr=4326, timeout_s=10.0)

        outcomes: dict[str, RunOutcome] = {}
        for kind in ALL_KINDS:
            outcomes[kind] = run_retrieval(
                url,
                FaultConfig(kind=kind, rate=1.0, seed=11),
                dataset="fixture",
                expect_features=SAMPLE_FEATURE_COUNT,
                expect_crs="EPSG:4326",
                clean_attempts=clean_attempts,
                timeout_s=10.0,
            )

    print(f"  clean run: {clean.features} feature(s) in {clean.crs}, "
          f"{clean_attempts} network call(s)")
    for kind, outcome in outcomes.items():
        print(
            f"  {kind:<13} completed={outcome.completed!s:<5} "
            f"features={outcome.features:<3} crs={outcome.crs or '-':<10} "
            f"attempts={outcome.attempts} injected={outcome.injected} "
            f"error={outcome.error[:56] or '-'}"
        )

    results.append(("clean", (
        "with no FaultConfig the fixture layer returns every feature in the requested CRS",
        clean.completed and clean.features == SAMPLE_FEATURE_COUNT
        and clean.crs == "EPSG:4326",
    )))
    results.append(("clean", (
        "a plan at rate 0.0 injects nothing, whatever kind and seed it names",
        counter.injected == 0 and repeat.injected == 0,
    )))
    results.append(("clean", (
        "two independent rate-0.0 runs cost the same number of calls, and that "
        "number is the metadata call plus at least one page",
        counter.attempts == repeat.attempts and counter.attempts >= 2,
    )))

    timeout = outcomes["timeout"]
    results.append(("timeout", (
        "an injected timeout ends the call as a TransientError",
        not timeout.completed and "TransientError" in timeout.error,
    )))
    results.append(("timeout", (
        "an injected timeout is retried to exactly config.MAX_RETRIES attempts",
        timeout.attempts == config.MAX_RETRIES,
    )))
    results.append(("timeout", (
        "the retried call names the injected timeout rather than a generic failure",
        "injected timeout" in timeout.error,
    )))

    server = outcomes["server_error"]
    results.append(("server_error", (
        "an injected 500 ends the call as a TransientError",
        not server.completed and "TransientError" in server.error,
    )))
    results.append(("server_error", (
        "an injected 500 is retried to exactly config.MAX_RETRIES attempts",
        server.attempts == config.MAX_RETRIES,
    )))
    results.append(("server_error", (
        f"the failure names HTTP {INJECTED_STATUS}",
        f"HTTP {INJECTED_STATUS}" in server.error,
    )))

    empty = outcomes["empty"]
    results.append(("empty", (
        "an injected empty response raises nothing and returns zero features",
        empty.completed and empty.features == 0 and not empty.error,
    )))
    results.append(("empty", (
        "an injected empty response still declares the CRS that was requested",
        empty.crs == "EPSG:4326",
    )))
    results.append(("empty", (
        "an empty response is not a recovery, because the numbers are wrong",
        not empty.correct,
    )))

    wrong = outcomes["wrong_crs"]
    results.append(("wrong_crs", (
        "an injected wrong CRS is caught by acquire's own CRS assertion",
        not wrong.completed and "CRSMismatch" in wrong.error,
    )))
    results.append(("wrong_crs", (
        "a wrong CRS is not retried, because retrying a wrong body cannot help",
        wrong.attempts == clean_attempts,
    )))
    results.append(("wrong_crs", (
        "the refusal names both the requested and the returned reference",
        "outSR=4326" in wrong.error and "3857" in wrong.error,
    )))

    short = outcomes["truncated"]
    results.append(("truncated", (
        "an injected truncation raises nothing at all",
        short.completed and not short.error,
    )))
    results.append(("truncated", (
        "a truncated page returns exactly half the features and no signal",
        short.features == SAMPLE_FEATURE_COUNT // 2,
    )))
    results.append(("truncated", (
        "a truncated page is not a recovery, because the count is silently short",
        not short.correct,
    )))

    results.append(("all", (
        "every kind fired on the run it was configured for",
        all(outcome.injected > 0 for outcome in outcomes.values()),
    )))
    results.append(("all", (
        "the substituted kinds returned a response rather than raising a transport error",
        all("TransientError" not in outcomes[kind].error for kind in SUBSTITUTED_KINDS),
    )))
    results.append(("all", (
        "the raised kinds never reached the data-handling path",
        all(outcomes[kind].features == 0 for kind in RAISED_KINDS),
    )))
    results.append(("all", (
        "every intercepted call carried an explicit timeout",
        all(outcome.timeouts_ok for outcome in outcomes.values()),
    )))
    results.append(("all", (
        "an attempt is counted once, as injected or as not applicable, never both",
        all(
            outcome.injected + outcome.not_applicable <= outcome.attempts
            for outcome in outcomes.values()
        ),
    )))
    results.append(("all", (
        "a content fault at rate 1.0 records the layer-description call it could "
        "not corrupt, rather than counting it as an injection",
        all(outcomes[kind].not_applicable >= 1 for kind in SUBSTITUTED_KINDS)
        and all(outcomes[kind].not_applicable == 0 for kind in RAISED_KINDS),
    )))
    return results


def _invariant_seven_checks() -> list[tuple[str, bool]]:
    """That the harness weakened neither the timeout nor the retry bound.

    Structural, then behavioural. The structural half reads the live policy off
    the decorated function; the behavioural half counts the attempts a rate-1.0
    run really spent. A comparison of policy objects alone can be walked past by
    an injector that never lets the retry run, which is why the count is here.
    """
    policy = acquire._request.retry
    with local_service() as url:
        with injecting(FaultConfig(kind="timeout", rate=1.0, seed=5)) as plan:
            failed = verify.refuses(
                lambda: acquire.fetch_arcgis_vector(url, 0, out_sr=4326, timeout_s=10.0),
                acquire.TransientError,
                "injected timeout",
            )
        attempts = plan.attempts
        timeouts = list(plan.timeouts_seen)

        with injecting(FaultConfig(kind="server_error", rate=1.0, seed=5)) as second:
            verify.refuses(
                lambda: acquire.fetch_arcgis_vector(url, 0, out_sr=4326, timeout_s=10.0),
                acquire.TransientError,
                f"HTTP {INJECTED_STATUS}",
            )
        server_attempts = second.attempts

    print(f"  policy: stop_after_attempt={policy.stop.max_attempt_number}, "
          f"reraise={policy.reraise}")
    print(f"  attempts spent at rate 1.0: timeout {attempts}, "
          f"server_error {server_attempts}, bound {config.MAX_RETRIES}")
    print(f"  timeouts carried by those calls: {timeouts}")
    return [
        ("the retry policy still stops at config.MAX_RETRIES",
         policy.stop.max_attempt_number == config.MAX_RETRIES),
        ("the retry policy still reraises rather than returning a failure object",
         policy.reraise is True),
        ("a call faulted on every attempt stops at the bound, it does not loop",
         attempts == config.MAX_RETRIES),
        ("a 500 faulted on every attempt stops at the same bound",
         server_attempts == config.MAX_RETRIES),
        ("the bound is spent rather than skipped, so recovery is measurable",
         attempts > 1),
        ("every call the harness saw carried an explicit timeout",
         all(value is not None for value in timeouts)),
        ("no call carried an unbounded timeout",
         all(isinstance(value, (int, float)) and value > 0 for value in timeouts)),
        ("the faulted call refused for the injected reason", failed),
    ]


def _recovery_checks() -> list[tuple[str, bool]]:
    """That a fault below rate 1.0 is something the retry can survive.

    Recovery is the number criterion RB is asking for, so it has to be observed
    rather than argued: the same seed at a partial rate, run against the fixture
    service, either came back with every feature or did not.
    """
    with local_service() as url:
        with injecting(FaultConfig(kind="timeout", rate=0.0, seed=1)) as counter:
            acquire.fetch_arcgis_vector(url, 0, out_sr=4326, timeout_s=10.0)
        clean_attempts = counter.attempts

        recovered: list[RunOutcome] = []
        for seed in range(12):
            recovered.append(
                run_retrieval(
                    url,
                    FaultConfig(kind="timeout", rate=0.5, seed=seed),
                    dataset="fixture",
                    expect_features=SAMPLE_FEATURE_COUNT,
                    expect_crs="EPSG:4326",
                    clean_attempts=clean_attempts,
                    timeout_s=10.0,
                )
            )
    survived = [item for item in recovered if item.correct]
    lost = [item for item in recovered if not item.correct]
    faulted = [item for item in recovered if item.injected > 0]
    print(f"  timeout at rate 0.5 over 12 seeds: {len(survived)} correct, "
          f"{len(lost)} failed, {len(faulted)} saw at least one injected fault")
    print(f"  extra attempts spent: {[item.extra_turns for item in recovered]}")
    return [
        ("a partial rate injects a fault on some runs", len(faulted) > 0),
        ("a partial rate does not fault every run", len(faulted) < len(recovered)),
        ("the retry recovers some runs that were faulted",
         any(item.injected > 0 and item.correct for item in survived)),
        ("a run the retry could not save is reported as failed, not as recovered",
         all(item.correct is False for item in lost)),
        ("a recovered run spent more attempts than a clean one",
         any(item.extra_turns > 0 for item in survived)),
        ("a run with no injected fault spent no extra attempts",
         all(item.extra_turns == 0 for item in recovered if item.injected == 0)),
        ("recovery is measured against the clean run's attempt count",
         clean_attempts > 0),
    ]


def _event_checks() -> list[tuple[str, bool]]:
    """That the frozen `FaultEvent` is filled from measurements, not from hope."""
    with local_service() as url:
        with injecting(FaultConfig(kind="timeout", rate=0.0, seed=1)) as counter:
            acquire.fetch_arcgis_vector(url, 0, out_sr=4326, timeout_s=10.0)
        clean_attempts = counter.attempts
        good = run_retrieval(
            url, FaultConfig(kind="timeout", rate=0.0, seed=1), dataset="tracts",
            expect_features=SAMPLE_FEATURE_COUNT, expect_crs="EPSG:4326",
            clean_attempts=clean_attempts, timeout_s=10.0,
        )
        bad = run_retrieval(
            url, FaultConfig(kind="empty", rate=1.0, seed=1), dataset="tracts",
            expect_features=SAMPLE_FEATURE_COUNT, expect_crs="EPSG:4326",
            clean_attempts=clean_attempts, timeout_s=10.0,
        )
    survived = good.event()
    failed = bad.event()
    print(f"  recovered event: {survived}")
    print(f"  silent-failure event: {failed}")
    return [
        ("an event carries the kind that was configured", survived.kind == "timeout"),
        ("an event names the dataset it was measured on", survived.dataset == "tracts"),
        ("a run with the right numbers is recorded as recovered", survived.recovered),
        ("a run that completed with the wrong numbers is NOT recorded as recovered",
         bad.completed and not failed.recovered),
        ("extra_turns is the attempts beyond the clean run", survived.extra_turns == 0),
        ("the event kind is one of the five", failed.kind in ALL_KINDS),
    ]


def _surface_checks() -> list[tuple[str, bool]]:
    """That fault injection did not become something the model can reach.

    An experiment harness is not a tool. If a fault kind ever appears in
    `TOOL_NAMES` the model can turn the injector on mid-answer, and every number
    after that point is untraceable for a reason nothing in the transcript
    records.
    """
    from . import tools

    names = tuple(tools.TOOL_NAMES)
    faulty = tools.surface_faults()
    reachable = [name for name in names if "fault" in name or "inject" in name]
    print(f"  {len(names)} tool(s) advertised; surface faults: {faulty or 'none'}")
    return [
        ("the tool surface is still eleven names", len(names) == 11),
        ("no tool exposes fault injection", not reachable),
        ("the tool surface reports no faults of its own", faulty == []),
        ("importing this module did not arm the injector", not armed()),
        ("importing this module did not replace acquire's session",
         not isinstance(acquire._SESSION, FaultedSession)),
    ]


def _self_check() -> int:
    print("faults: injected at acquire._SESSION, inside the retry tenacity drives\n")
    checks = _kind_checks()
    print()
    checks += _choke_point_checks()
    print()
    checks += _guard_checks()
    print()
    checks += _seed_checks()
    print()
    checks += _corruption_checks()
    print()
    checks += [item for _, item in _live_kind_checks()]
    print()
    checks += _invariant_seven_checks()
    print()
    checks += _recovery_checks()
    print()
    checks += _event_checks()
    print()
    checks += _surface_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])
    return verify.report(checks)


def main() -> int:
    """Describe the harness. The runs live in `src/experiments/faults.py`."""
    print(__doc__)
    print(f"kinds: {list(ALL_KINDS)}")
    print(f"retry bound: {config.MAX_RETRIES} attempts, timeout {config.REQUEST_TIMEOUT_S}s")
    print(f"armed: {armed()}")
    print("\nrun the experiment with: python -m src.experiments.faults")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main())
