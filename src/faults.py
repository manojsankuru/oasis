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

A content fault needs something to corrupt. Against a layer-metadata reply or a
raster body there is nothing to make empty or to mis-declare, so the attempt is
recorded as not applicable and passed through untouched rather than counted as
an injection that did not happen. A `returnCountOnly` reply is faulted like any
other call: by the two raised kinds, and by `empty`, which takes the count out
of it. The count is what `acquire` checks every page against, which is how
`empty` and `truncated` stop passing as success, so a bad count reply is the
fault that check most needs to survive.

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
import socket
import sys
import tempfile
import threading
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, get_args

import geopandas as gpd
import numpy as np
import requests
from pyproj import Transformer
from shapely.geometry import Point, box

from . import acquire, config, verify
from .contracts import FaultConfig, FaultEvent, FaultKind, Provenance
from .registry import Registry

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
COUNT_KEY = "count"
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

    One reply that is not a FeatureSet is corruptible: a `returnCountOnly`
    count. `empty` takes the count out of it, which is the bad count reply a
    real service sends, and the one that must not let an attempt's pages through
    unchecked. `truncated` and `wrong_crs` leave it alone. A halved count would be
    a second wrong number that could agree with a halved page by chance, and a
    count carries no reference to mis-declare.
    """
    if kind not in SUBSTITUTED_KINDS:
        raise FaultNotApplicable(f"{kind} is raised, not substituted")
    try:
        payload = response.json()
    except ValueError as exc:
        raise FaultNotApplicable(f"body is not JSON: {type(exc).__name__}") from exc
    if isinstance(payload, dict) and COUNT_KEY in payload and FEATURE_KEY not in payload:
        if kind != "empty":
            raise FaultNotApplicable(f"{kind} has nothing to corrupt in a count reply")
        payload.pop(COUNT_KEY)
        response._content = json.dumps(payload).encode("utf-8")
        response._content_consumed = True
        return response
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


METRE_ORIGIN = (1_613_477.0, 1_234_567.0)
"""Where `FixtureSettings.metres` puts its points: projected-metre magnitudes, far
outside any degree's range, served under a degree label. Synthetic, near no
study area."""


@dataclass(frozen=True, slots=True)
class FixtureSettings:
    """How the local service misbehaves, beyond what the injector does per call.

    The injector corrupts one call at a time at a seeded rate. These are the
    standing faults a real service can have on every call: a count that disagrees
    with its own pages, no `returnCountOnly` support at all, or coordinates in
    metres under a degree label. Each one is a path in `acquire` that no per-call
    corruption reaches.

    `count_replies` scripts the count request by request instead: an integer is
    that count, a token names a bad reply, and once the script runs out the
    standing settings answer. `count_log` records every count request's
    parameters, which is how a check sees that the count carried the query's
    filter -- the fixture serves the same features whatever the filter says. It is what reaches the order of outcomes -- a count
    that disagrees and then a count that is refused -- which no rate can aim at.
    """

    count_reported: int | None = None
    refuse_count: bool = False
    metres: bool = False
    count_replies: tuple[int | str, ...] = ()
    count_log: list[dict[str, str]] = field(default_factory=list)


REFUSE = "refuse"
"""A scripted count reply: an ArcGIS error body with a 4xx code, the service
saying it will not count. A standing refusal, accepted and named."""

FAIL = "fail"
"""A scripted count reply: an ArcGIS error body with a 5xx code under HTTP 200,
which is how NFHL reports a failure. Asked again, never trusted."""

BLANK = "blank"
"""A scripted count reply with no `count` in it at all."""

TEXT = "text"
"""A scripted count reply whose count is a string, not an integer."""

BUSY = "busy"
"""A scripted count reply: an ArcGIS error body with code 429. A 4xx code, and
still transient -- `acquire._RETRYABLE_STATUS` says so -- so never a refusal."""


def sample_featureset(
    count: int, out_sr: int, offset: int, page: int, *, metres: bool = False
) -> dict[str, Any]:
    """One page of an Esri JSON FeatureSet, shaped the way TIGERweb ships one.

    Synthetic coordinates and synthetic identifiers. The data is not the thing
    under test; the fault path is, and it needs a real FeatureSet to travel
    through `_query_features`, `_received_crs` and the ESRIJSON driver.
    """
    x0, y0, step = (*METRE_ORIGIN, 10.0) if metres else (-100.0, 40.0, 0.01)
    rows = [
        {
            "attributes": {
                OID_FIELD: index + 1,
                "GEOID": f"{99_000_000_000 + index:011d}",
                "NAME": f"unit {index + 1}",
            },
            "geometry": {"x": x0 + index * step, "y": y0 + index * step},
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
    """Answers the three requests `fetch_arcgis_vector` makes, and nothing else:
    the layer description, the `returnCountOnly` count, and a page of features."""

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        settings: FixtureSettings = getattr(self.server, "settings", FixtureSettings())
        if path.rstrip("/").endswith("/query") and params.get("returnCountOnly") == "true":
            settings.count_log.append(dict(params))
            asked = getattr(self.server, "counts_asked", 0)
            self.server.counts_asked = asked + 1  # type: ignore[attr-defined]
            scripted = (
                settings.count_replies[asked] if asked < len(settings.count_replies) else None
            )
            if scripted == REFUSE or (scripted is None and settings.refuse_count):
                body: dict[str, Any] = {
                    "error": {"code": 400, "message": "returnCountOnly is not supported"}
                }
            elif scripted == FAIL:
                body = {"error": {"code": 500, "message": "Error performing query operation"}}
            elif scripted == BUSY:
                body = {"error": {"code": 429, "message": "Too many requests"}}
            elif scripted == BLANK:
                body = {}
            elif scripted == TEXT:
                body = {"count": str(SAMPLE_FEATURE_COUNT)}
            elif scripted is not None:
                body = {"count": scripted}
            elif settings.count_reported is not None:
                body = {"count": settings.count_reported}
            else:
                body = {"count": SAMPLE_FEATURE_COUNT}
        elif path.rstrip("/").endswith("/query"):
            out_sr = int(params.get("outSR", 4326))
            offset = int(params.get("resultOffset", 0))
            page = int(params.get("resultRecordCount", acquire.PAGE_SIZE))
            body = sample_featureset(
                SAMPLE_FEATURE_COUNT, out_sr, offset, page, metres=settings.metres
            )
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
def local_service(settings: FixtureSettings | None = None) -> Iterator[str]:
    """A real HTTP server on a loopback port, yielded as a service URL.

    A real socket, real headers and a real status line, so `_request`,
    `_query_features`, `_received_crs` and the ESRIJSON driver all run for real
    against it. Stubbing `_request` instead would have tested this module's idea
    of `acquire`, which CLAUDE.md names as the thing two green suites here have
    already done. `settings` names a standing fault the service has on every call.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.settings = settings or FixtureSettings()  # type: ignore[attr-defined]
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
    one metadata call, one count call and one page, and a harness that hardcoded
    three would stop being right the moment a layer needed a second page.

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
    def count_reply() -> requests.Response:
        return synthetic_response(
            "GET", "http://x/0/query", 200, b'{"count": 10}', "application/json"
        )

    count_emptied = corrupt("empty", count_reply(), 4326).json()
    count_untouched = all(
        verify.refuses(
            lambda kind=kind: corrupt(kind, count_reply(), 4326),
            FaultNotApplicable,
            "nothing to corrupt in a count reply",
        )
        for kind in ("truncated", "wrong_crs")
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
        ("empty takes the count out of a count reply, the bad reply acquire must survive",
         COUNT_KEY not in count_emptied),
        ("truncated and wrong_crs leave a count reply alone, so no second wrong number "
         "can agree with a short page by chance",
         count_untouched),
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
        "number is the metadata call, the count call and at least one page",
        counter.attempts == repeat.attempts and counter.attempts >= 3,
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

    # The two content faults that used to complete silently. The page is checked
    # against the service's own count, so a page faulted on every attempt is
    # asked again to the bound and then refused by name -- never returned short.
    counted_attempts = 1 + config.MAX_RETRIES * (clean_attempts - 1)
    empty = outcomes["empty"]
    results.append(("empty", (
        "an empty page on every attempt is refused by name, not returned as zero features",
        not empty.completed and "FeatureCountMismatch" in empty.error,
    )))
    results.append(("empty", (
        "the query was asked again to exactly config.MAX_RETRIES counted attempts",
        empty.attempts == counted_attempts,
    )))
    results.append(("empty", (
        "with the count stripped too, the refusal says the count was unusable, and "
        "the emptied page was not returned",
        "no usable count" in empty.error and "not returned" in empty.error,
    )))

    wrong = outcomes["wrong_crs"]
    results.append(("wrong_crs", (
        "an injected wrong CRS is caught by acquire's own CRS assertion",
        not wrong.completed and "CRSMismatch" in wrong.error,
    )))
    results.append(("wrong_crs", (
        "a wrong CRS is re-requested exactly once, then refused rather than retried again",
        wrong.attempts == clean_attempts + (clean_attempts - 1),
    )))
    results.append(("wrong_crs", (
        "the refusal names both the requested and the returned reference",
        "outSR=4326" in wrong.error and "3857" in wrong.error,
    )))

    short = outcomes["truncated"]
    results.append(("truncated", (
        "a short page on every attempt is refused by name, not returned as half the layer",
        not short.completed and "FeatureCountMismatch" in short.error,
    )))
    results.append(("truncated", (
        "the refusal names the half the page held against the count the service gave",
        f"counted {SAMPLE_FEATURE_COUNT}" in short.error
        and f"returned {SAMPLE_FEATURE_COUNT // 2}" in short.error,
    )))
    results.append(("truncated", (
        "a truncation the count caught is not a recovery and returns no features",
        not short.correct and short.features == 0,
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

    counted = acquire._counted_attempt.retry
    print(f"  policy: stop_after_attempt={policy.stop.max_attempt_number}, "
          f"reraise={policy.reraise}")
    print(f"  count policy: stop_after_attempt={counted.stop.max_attempt_number}, "
          f"reraise={counted.reraise}, retries {counted.retry.exception_types}")
    print(f"  attempts spent at rate 1.0: timeout {attempts}, "
          f"server_error {server_attempts}, bound {config.MAX_RETRIES}")
    print(f"  timeouts carried by those calls: {timeouts}")
    return [
        ("the retry policy still stops at config.MAX_RETRIES",
         policy.stop.max_attempt_number == config.MAX_RETRIES),
        ("the retry policy still reraises rather than returning a failure object",
         policy.reraise is True),
        ("the count policy stops at the same bound and reraises too, so it cannot loop",
         counted.stop.max_attempt_number == config.MAX_RETRIES and counted.reraise is True),
        ("the count policy re-asks a count mismatch only, never a transport failure",
         counted.retry.exception_types
         in (acquire.FeatureCountMismatch, (acquire.FeatureCountMismatch,))),
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
    # The one silent path left. A service that refuses returnCountOnly -- every
    # count reply in the retrieval a 4xx refusal, on every request in the bound --
    # has no count to check a page against, so an emptied page still completes. That is the gap `acquire._feature_count` names in the
    # provenance note. A count reply that is merely bad, or a refusal after a
    # count, is never trusted; only this refusal is let through. Kept as a check
    # rather than hidden, and it is the run that proves `correct` is more than
    # `completed`.
    with local_service(FixtureSettings(refuse_count=True)) as url:
        silent = run_retrieval(
            url, FaultConfig(kind="empty", rate=1.0, seed=1), dataset="tracts",
            expect_features=SAMPLE_FEATURE_COUNT, expect_crs="EPSG:4326",
            clean_attempts=clean_attempts, timeout_s=10.0,
        )
    survived = good.event()
    failed = bad.event()
    print(f"  recovered event: {survived}")
    print(f"  refused-run event: {failed}")
    print(f"  where the service cannot count, an emptied page: completed={silent.completed} "
          f"features={silent.features} correct={silent.correct}")
    return [
        ("an event carries the kind that was configured", survived.kind == "timeout"),
        ("an event names the dataset it was measured on", survived.dataset == "tracts"),
        ("a run with the right numbers is recorded as recovered", survived.recovered),
        ("a run that ended in a named failure is NOT recorded as recovered",
         not bad.completed and "FeatureCountMismatch" in bad.error and not failed.recovered),
        ("where the service refuses to count, an emptied page still completes -- the named gap",
         silent.completed and silent.features == 0),
        ("a run that completed with the wrong numbers is NOT recorded as recovered",
         silent.completed and not silent.correct and not silent.event().recovered),
        ("extra_turns is the attempts beyond the clean run", survived.extra_turns == 0),
        ("the event kind is one of the five", failed.kind in ALL_KINDS),
    ]


@dataclass(slots=True)
class Fetched:
    """One `fetch_arcgis_vector` call's outcome, with a failure kept as data.

    The checks below assert on what a call did, including how it failed. A call
    left to raise would end the whole suite at that line instead of failing the
    one check it belongs to, and a mutant would read as a crash rather than as
    the named check that caught it.
    """

    frame: Any = None
    record: Any = None
    error: str = ""

    @property
    def features(self) -> int:
        return 0 if self.frame is None else int(len(self.frame))

    def noted(self, phrase: str) -> bool:
        return self.record is not None and any(phrase in note for note in self.record.notes)


def fetch(url: str) -> Fetched:
    """`acquire.fetch_arcgis_vector` on the fixture layer, never raising."""
    try:
        frame, record = acquire.fetch_arcgis_vector(url, 0, out_sr=4326, timeout_s=10.0)
    except acquire.AcquisitionError as exc:
        return Fetched(error=f"{type(exc).__name__}: {exc}")
    return Fetched(frame=frame, record=record)


FILTER_WHERE = "GEOID LIKE '99%'"
"""A filter for the count-filter check, with characters a URL has to encode."""


def seed_for(pattern: tuple[bool, ...], rate: float, limit: int = 5000) -> int:
    """The first seed whose opening decisions at `rate` are exactly `pattern`.

    `FaultPlan` draws once per call, in call order, so a seed can be chosen to
    fault one named call -- the count, or the first page -- and leave the rest
    alone. Found by replaying the same generator the plan seeds, not guessed.
    """
    for seed in range(limit):
        draws = random.Random(seed)
        if tuple(draws.random() < rate for _ in pattern) == pattern:
            return seed
    raise ValueError(f"no seed below {limit} opens with {pattern} at rate {rate}")


def _count_checks() -> list[tuple[str, bool]]:
    """That a page is checked against the service's own count, both ways round.

    The injector corrupts one call at a seeded rate; `FixtureSettings` gives the
    service a standing fault on every call. Between them every branch of
    `acquire._counted_query` and `acquire._feature_count` runs against a real
    socket: agreement, a count that never agrees, a service that cannot count,
    a page faulted once and recovered, and a fault on the count request itself.
    """
    meta, count, page = False, False, False
    # metadata clean, count timed out, count again clean, page clean
    count_timeout = seed_for((meta, True, count, page), 0.5)
    # metadata clean, count clean, page emptied; then count and page clean
    page_emptied = seed_for((meta, count, True, count, page), 0.5)

    with local_service() as url:
        with injecting(FaultConfig(kind="timeout", rate=0.0, seed=0)) as counter:
            clean = fetch(url)
        with injecting(FaultConfig(kind="timeout", rate=0.5, seed=count_timeout)) as timed:
            timed_fetch = fetch(url)
        with injecting(FaultConfig(kind="empty", rate=0.5, seed=page_emptied)) as emptied:
            recovered = fetch(url)
    with local_service(FixtureSettings(count_reported=SAMPLE_FEATURE_COUNT + 1)) as url:
        with injecting(FaultConfig(kind="timeout", rate=0.0, seed=0)) as disagreeing:
            never = fetch(url)
    with local_service(FixtureSettings(refuse_count=True)) as url:
        with injecting(FaultConfig(kind="timeout", rate=0.0, seed=0)) as refusing:
            uncounted = fetch(url)
    bad_once: dict[str, Fetched] = {}
    for token in (FAIL, BUSY, BLANK, TEXT):
        with local_service(FixtureSettings(count_replies=(token,))) as url:
            bad_once[token] = fetch(url)
    with local_service(FixtureSettings(count_replies=(BLANK,) * config.MAX_RETRIES)) as url:
        never_usable = fetch(url)
    # Refusals after a reply that was not one: two 400s and a 5xx, then 400s for
    # good. The 5xx means this is not a service that never counts, so the later
    # run of refusals is not believed and the layer is refused.
    with local_service(
        FixtureSettings(count_replies=(REFUSE, REFUSE, FAIL), refuse_count=True)
    ) as url:
        refused_after_failure = fetch(url)
    # A count of 10, then 5 for the same query, with both of those attempts'
    # pages truncated. A count that changes vouches for neither page set.
    count_changed = seed_for((meta, count, True, count, True, count, page), 0.5)
    with local_service(FixtureSettings(count_replies=(SAMPLE_FEATURE_COUNT, SAMPLE_FEATURE_COUNT // 2))) as url:
        with injecting(FaultConfig(kind="truncated", rate=0.5, seed=count_changed)):
            changed = fetch(url)
    # A count of 11 first, then a refusal on every request after it: each later
    # attempt's pages are checked against the 11 the service already gave, so
    # the layer is refused rather than let through as uncountable.
    with local_service(
        FixtureSettings(count_replies=(SAMPLE_FEATURE_COUNT + 1,), refuse_count=True)
    ) as url:
        disagreed_then_refused = fetch(url)
    # One 400 refusal from a service that otherwise counts, with that attempt's
    # page truncated by the injector. Draws: metadata, count (refused), count
    # (answered), page (truncated); then count and page clean.
    short_after_refusal = seed_for((meta, count, count, True, count, page), 0.5)
    with local_service(FixtureSettings(count_replies=(REFUSE,))) as url:
        with injecting(
            FaultConfig(kind="truncated", rate=0.5, seed=short_after_refusal)
        ) as refused_short:
            refused_once = fetch(url)
    filtered_settings = FixtureSettings()
    with local_service(filtered_settings) as url:
        acquire.fetch_arcgis_vector(
            url, 0, where=FILTER_WHERE, out_sr=4326, bbox=(-100.5, 39.5, -99.5, 40.5),
            timeout_s=10.0,
        )
    filtered = filtered_settings
    real_parser = acquire._read_esri_json
    acquire._read_esri_json = lambda payload, crs: real_parser(payload, crs).iloc[:-1]
    try:
        with local_service() as url:
            dropped = fetch(url)
    finally:
        acquire._read_esri_json = real_parser
    # metadata clean, count stripped, page emptied in the same attempt; then clean
    both_faulted = seed_for((meta, True, True, count, page), 0.5)
    with local_service() as url:
        with injecting(FaultConfig(kind="empty", rate=0.5, seed=both_faulted)) as stripped:
            survived = fetch(url)

    print(f"  clean: {counter.attempts} call(s); count timed out on seed {count_timeout}: "
          f"{timed.attempts} call(s), {timed_fetch.features} feature(s)")
    print(f"  first page emptied on seed {page_emptied}: {emptied.attempts} call(s), "
          f"{recovered.features} feature(s) after the re-query {recovered.error[:60]}")
    print(f"  a count that never agrees: {disagreeing.attempts} call(s), {never.error[:70]}")
    print(f"  a service that refuses to count: {uncounted.features} feature(s) after "
          f"{refusing.attempts} call(s) {uncounted.error[:60]}")
    print(f"  one bad count reply each: "
          f"{ {token: (item.features, item.error[:40]) for token, item in bad_once.items()} }")
    print(f"  a count stripped and its page emptied on seed {both_faulted}: "
          f"{survived.features} feature(s), {stripped.injected} injected")
    return [
        ("a clean retrieval asks the count and records that the pages agreed with it",
         clean.noted(f"counted {SAMPLE_FEATURE_COUNT} and paging returned {SAMPLE_FEATURE_COUNT}")),
        ("a timeout on the count request is retried like any other call, and recovers",
         timed.decisions[1] is True and timed.injected == 1
         and timed_fetch.features == SAMPLE_FEATURE_COUNT
         and timed.attempts == counter.attempts + 1),
        ("a page emptied once is asked again and the whole layer comes back",
         emptied.injected == 1 and recovered.features == SAMPLE_FEATURE_COUNT),
        ("the recovered layer's provenance says the count agreed on the second attempt",
         recovered.noted("after 2 counted attempt(s)")),
        ("a count that never agrees is refused by name once the bound is spent",
         never.error.startswith("FeatureCountMismatch")
         and f"counted {SAMPLE_FEATURE_COUNT + 1}" in never.error
         and disagreeing.attempts == 1 + config.MAX_RETRIES * (counter.attempts - 1)),
        ("a service that refuses to count still returns the layer, and says it went unchecked",
         uncounted.features == SAMPLE_FEATURE_COUNT and uncounted.noted("unchecked")),
        ("a refusal is asked again as a count only, to the bound, before it is believed",
         refusing.attempts == counter.attempts + config.MAX_RETRIES - 1),
        ("one bad count reply -- a 5xx body, a 429 body, a blank one, a text count -- "
         "is asked again, and the layer is checked on the attempt that answered",
         all(
             item.features == SAMPLE_FEATURE_COUNT
             and item.noted("agreeing after 2 counted attempt(s)")
             for item in bad_once.values()
         )
         and len(bad_once) == 4),
        ("refusals after a count reply that was not one are not believed",
         refused_after_failure.error.startswith("FeatureCountMismatch")
         and "not a refusal" in refused_after_failure.error),
        ("a count that changes between attempts vouches for neither page set",
         changed.features == SAMPLE_FEATURE_COUNT
         and not changed.noted(f"counted {SAMPLE_FEATURE_COUNT // 2}")),
        ("one refusal from a service that counts does not let a short page through",
         refused_once.features == SAMPLE_FEATURE_COUNT and refused_short.injected == 1),
        ("the count request carries the query's filter, not the whole layer's",
         bool(filtered.count_log)
         and filtered.count_log[0].get("where") == FILTER_WHERE
         and "geometry" in filtered.count_log[0]
         and filtered.count_log[0].get("returnCountOnly") == "true"),
        ("a feature the parser drops is refused, not returned one short",
         dropped.error.startswith("ServiceError") and "parser kept" in dropped.error),
        ("a count that is never usable is refused by name, never let through unchecked",
         never_usable.error.startswith("FeatureCountMismatch")
         and "no usable count" in never_usable.error),
        ("a refusal after a count the service already gave is checked against that count, "
         "not taken as a service that cannot count",
         disagreed_then_refused.error.startswith("FeatureCountMismatch")
         and f"counted {SAMPLE_FEATURE_COUNT + 1}" in disagreed_then_refused.error),
        ("a count stripped by the injector with its page emptied is asked again and recovers",
         stripped.injected == 2 and survived.features == SAMPLE_FEATURE_COUNT),
    ]


def _crs_recovery_checks() -> list[tuple[str, bool]]:
    """That a mislabelled reference is asked for once more, and coordinates are read.

    Two separate defences. The re-request answers a label that is wrong once.
    The range check answers a label that is right with numbers that are not:
    metres under a degree label pass every label check there is, so the numbers
    themselves are held to the declared reference's area of use.
    """
    meta, count, page = False, False, False
    # metadata clean, count clean, page mislabelled; the re-request clean
    mislabelled_once = seed_for((meta, count, True, count, page), 0.5)
    with local_service() as url:
        with injecting(FaultConfig(kind="wrong_crs", rate=0.5, seed=mislabelled_once)) as plan:
            relabelled = fetch(url)
    with local_service(FixtureSettings(metres=True)) as url:
        in_metres = fetch(url)
    # The first page agrees with a count of 10 and is mislabelled; the re-request
    # is refused a count on every request. The 10 already given still checks it.
    with local_service(
        FixtureSettings(count_replies=(SAMPLE_FEATURE_COUNT,), refuse_count=True)
    ) as url:
        with injecting(FaultConfig(kind="wrong_crs", rate=0.5, seed=mislabelled_once)):
            known_kept = fetch(url)
    metres_error = in_metres.error if in_metres.error.startswith("CRSMismatch") else ""

    def points(x0: float, y0: float, crs: str) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(
            geometry=[Point(x0 + step, y0 + step) for step in (0.0, 1.0)], crs=crs
        )

    degrees = points(-100.0, 40.0, "EPSG:4326")
    nad83 = points(-100.0, 40.0, "EPSG:4269")
    nad83_off = points(-100.0, 200.0, "EPSG:4269")
    nad83_wrong_side = points(20.0, 40.0, "EPSG:4269")
    alaska_metres = points(0.0, 1_000_000.0, "EPSG:3338")
    alaska_degrees = points(-150.0, 60.0, "EPSG:3338")
    no_geometry = gpd.GeoDataFrame(geometry=[None, None], crs="EPSG:4326")
    mercator = points(*METRE_ORIGIN, "EPSG:3857")
    mislabelled = points(*METRE_ORIGIN, "EPSG:4326")
    # One frame past each edge of the degree range and inside the other three,
    # so each of the four comparisons is the only one that can refuse it.
    past_one_edge = {
        "west": points(-400.0, 40.0, "EPSG:4326"),
        "east": points(400.0, 40.0, "EPSG:4326"),
        "south": points(-100.0, -200.0, "EPSG:4326"),
        "north": points(-100.0, 200.0, "EPSG:4326"),
    }
    edges = {side: _range_note(frame, "EPSG:4326") for side, frame in past_one_edge.items()}
    declared = relabelled.record.declared_crs if relabelled.record is not None else "-"
    print(f"  mislabelled once on seed {mislabelled_once}: {relabelled.features} feature(s) in "
          f"{declared} after {plan.attempts} call(s) {relabelled.error[:60]}")
    print(f"  metres under a degree label -> {metres_error[-120:]!r}")
    return [
        ("a reference mislabelled once is re-requested and the layer comes back right",
         plan.injected == 1 and relabelled.features == SAMPLE_FEATURE_COUNT
         and declared == "EPSG:4326"),
        ("the provenance says the first response was mislabelled and re-requested",
         relabelled.noted("re-requested once")),
        ("metres under a degree label are refused, though the label matches the request",
         "outside that reference's valid range" in metres_error),
        ("the refusal names no coordinate, because it can reach a provenance note",
         str(int(METRE_ORIGIN[0])) not in metres_error),
        ("degrees under a degree label pass the range check",
         _range_note(degrees, "EPSG:4326").startswith(INSIDE_RANGE)),
        ("metres under a metre label pass too, so the check reads units, not size",
         _range_note(mercator, "EPSG:3857").startswith(INSIDE_RANGE)),
        ("the range check refuses metres under a degree label when called directly",
         verify.refuses(
             lambda: acquire._assert_plausible_extent(mislabelled, "EPSG:4326", "x"),
             acquire.CRSMismatch,
             "valid range",
         )),
        ("an empty layer has no range to check and says so",
         "no features" in _range_note(degrees.iloc[0:0], "EPSG:4326")),
        ("a layer whose features carry no geometry is noted unchecked, not as inside the range",
         "not checked" in _range_note(no_geometry, "EPSG:4326")),
        ("a reference whose area of use crosses the antimeridian still accepts its own "
         "coordinates, and still refuses an impossible latitude",
         _range_note(nad83, "EPSG:4269").startswith(INSIDE_RANGE)
         and _range_note(nad83_off, "EPSG:4269").startswith("refused")),
        ("a longitude outside the wrapped area of use is still refused",
         _range_note(nad83_wrong_side, "EPSG:4269").startswith("refused")),
        ("a projected reference crossing the antimeridian keeps its range check",
         _range_note(alaska_metres, "EPSG:3338").startswith(INSIDE_RANGE)
         and _range_note(alaska_degrees, "EPSG:3338").startswith("refused")),
        ("the re-request checks its pages against the count the first attempt gave",
         known_kept.features == SAMPLE_FEATURE_COUNT
         and known_kept.noted(f"counted {SAMPLE_FEATURE_COUNT}")
         and not known_kept.noted("unchecked")),
        ("a coordinate past any one edge of the range is refused, each edge on its own",
         all(note.startswith("refused") for note in edges.values()) and len(edges) == 4),
    ]


INSIDE_RANGE = "every coordinate falls inside the valid range"
"""How `acquire._assert_plausible_extent` says a frame passed. Matched as a prefix:
the refusal's text also says "valid range", so a check that only looked for
those words passed on a refusal -- which is how a broken antimeridian case first
went unnoticed here."""


def _range_note(frame: Any, declared: str) -> str:
    """`acquire._assert_plausible_extent`'s note, or the refusal's text as data."""
    try:
        return acquire._assert_plausible_extent(frame, declared, "fixture")
    except acquire.CRSMismatch as exc:
        return f"refused: {exc}"


def _extent_checks() -> list[tuple[str, bool]]:
    """That every layer after tracts has to reach the tract layer's extent.

    Run against a real `Registry` in a temporary directory, with a real tract
    file registered under a real `Provenance`, so `_within_study_area` loads the
    tracts the way `acquire.main` does. The comparison runs in the working CRS
    through `align`: the far-off layer below is refused in metres, and the
    nearby one is accepted although it arrives in another reference.
    """
    from . import align
    from . import provenance as prov

    def square(x0: float, y0: float, crs: str = "EPSG:4326") -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(
            geometry=[box(x0, y0, x0 + 0.1, y0 + 0.1)], crs=crs
        )

    tracts = square(-100.0, 40.0)
    overlapping = square(-99.95, 40.05)
    far_away = square(-80.0, 10.0)
    # The same overlapping square, its corners put into web mercator by pyproj
    # directly: this module may not reproject a frame itself, and the point is
    # that `align` does it for the comparison.
    to_mercator = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    west, south = to_mercator.transform(-99.95, 40.05)
    east, north = to_mercator.transform(-99.85, 40.15)
    in_mercator = gpd.GeoDataFrame(geometry=[box(west, south, east, north)], crs="EPSG:3857")
    with tempfile.TemporaryDirectory(prefix="extent_") as folder:
        root = Path(folder)
        path = root / "tracts.geojson"
        tracts.to_file(path, driver="GeoJSON")
        record = Provenance(
            dataset=acquire.DATASET_TRACTS,
            source_url="http://127.0.0.1/fixture",
            retrieved_at=prov.utc_now(),
            declared_crs="EPSG:4326",
            working_crs=config.DEFAULT_WORKING_CRS,
            vintage="synthetic fixture",
            feature_count=1,
            license="synthetic fixture, no license",
        )
        empty_registry = Registry(manifest_path=root / "manifest.json", root=root)
        registry = Registry(manifest_path=root / "manifest.json", root=root)
        registry.register(acquire.DATASET_TRACTS, "vector", path, record)
        aligner = align.Alignment(registry)
        accepted = _extent_note(overlapping, registry, "block_groups")
        refused = _extent_note(far_away, registry, "flood_zones")
        unregistered = _extent_note(overlapping, empty_registry, "facilities")
        empty = _extent_note(overlapping.iloc[0:0], registry, "flood_zones")
        compared = {
            "overlapping": acquire.meets_extent(overlapping, tracts, aligner),
            "overlapping, in another reference": acquire.meets_extent(in_mercator, tracts, aligner),
            "far away": acquire.meets_extent(far_away, tracts, aligner),
            "empty": acquire.meets_extent(overlapping.iloc[0:0], tracts, aligner),
        }
    print(f"  extent against the tracts: {compared}")
    print(f"  accepted: {accepted}")
    return [
        ("a layer that overlaps the tracts is accepted, and the check is noted",
         "meets the tract layer" in accepted and registry.working_crs in accepted),
        ("a layer that lands somewhere else is refused as a CRS mismatch",
         refused.startswith("refused") and "does not reach the tract layer" in refused),
        ("the comparison says which extents meet, whatever reference each arrives in",
         compared == {
             "overlapping": True,
             "overlapping, in another reference": True,
             "far away": False,
             "empty": False,
         }),
        ("with no tract layer registered the check is noted as not made, not passed",
         "not compared" in unregistered),
        ("an empty layer has no extent to compare and says so",
         "no features" in empty),
    ]


def _raster_and_error_checks() -> list[tuple[str, bool]]:
    """Two refusals that leave something behind if they are wrong.

    A refused raster has already been written by the time its extent is compared,
    so the refusal has to delete it -- checked on a real GeoTIFF against a real
    registry. And a transport error names the URL it failed on, query string and
    all, which for an envelope filter is the study extent URL-encoded -- checked
    by sending a real request to a loopback port nothing listens on, through the
    real `_request`.
    """
    import rasterio
    from rasterio.transform import from_origin

    from . import provenance as prov

    with tempfile.TemporaryDirectory(prefix="raster_") as folder:
        root = Path(folder)
        tracts_path = root / "tracts.geojson"
        gpd.GeoDataFrame(geometry=[box(-100.0, 40.0, -99.9, 40.1)], crs="EPSG:4326").to_file(
            tracts_path, driver="GeoJSON"
        )
        registry = Registry(manifest_path=root / "manifest.json", root=root)
        registry.register(
            acquire.DATASET_TRACTS,
            "vector",
            tracts_path,
            Provenance(
                dataset=acquire.DATASET_TRACTS,
                source_url="http://127.0.0.1/fixture",
                retrieved_at=prov.utc_now(),
                declared_crs="EPSG:4326",
                working_crs=config.DEFAULT_WORKING_CRS,
                vintage="synthetic fixture",
                feature_count=1,
                license="synthetic fixture, no license",
            ),
        )

        def raster(name: str, west: float, north: float) -> Path:
            path = root / name
            with rasterio.open(
                path, "w", driver="GTiff", width=4, height=4, count=1, dtype="float32",
                crs="EPSG:4326", transform=from_origin(west, north, 0.025, 0.025),
            ) as dataset:
                dataset.write(np.zeros((1, 4, 4), dtype="float32"))
            return path

        inside = raster("inside.tif", -100.0, 40.1)
        elsewhere = raster("elsewhere.tif", -80.0, 10.1)
        unreadable = root / "unreadable.tif"
        unreadable.write_bytes(b"not a raster")
        kept_note = _raster_note(inside, registry)
        refused_note = _raster_note(elsewhere, registry)
        try:
            acquire._checked_raster(unreadable, registry)
            unreadable_refused = False
        except Exception:
            unreadable_refused = True
        kept, removed = inside.exists(), not elsewhere.exists()
        unreadable_removed = not unreadable.exists()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    hidden = "geometry"
    transport_error = ""
    try:
        acquire._request(
            "GET",
            f"http://127.0.0.1:{closed_port}/0/query",
            params={"where": "1=1", hidden: "-10.123456,20.654321,-10.0,20.7"},
            timeout_s=2.0,
        )
    except acquire.AcquisitionError as exc:
        transport_error = str(exc)
    print(f"  a raster inside the tracts: {kept_note[:70]}")
    print(f"  a raster elsewhere: {refused_note[:70]}; file removed: {removed}")
    print(f"  transport error text: {transport_error[:110]!r}")
    return [
        ("a raster that reaches the tracts is kept and its check noted",
         kept and "meets the tract layer" in kept_note),
        ("a raster that lands elsewhere is refused and removed, not left for the old record",
         refused_note.startswith("refused") and removed),
        ("a raster that cannot even be compared is removed too, whatever the error",
         unreadable_refused and unreadable_removed),
        ("a transport error reached the real request path",
         "ConnectionError" in transport_error),
        ("a transport error carries no query string, so no encoded extent reaches a note",
         hidden not in transport_error and "query withheld" in transport_error),
    ]


def _raster_note(path: Path, registry: Registry) -> str:
    """`acquire._checked_raster`'s note, or the refusal's text as data."""
    try:
        return acquire._checked_raster(path, registry)
    except acquire.CRSMismatch as exc:
        return f"refused: {exc}"


def _extent_note(frame: Any, registry: Registry, dataset: str) -> str:
    """`acquire._within_study_area`'s note, or the refusal's text as data."""
    try:
        return acquire._within_study_area(frame, registry, dataset)
    except acquire.CRSMismatch as exc:
        return f"refused: {exc}"


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
    checks += _count_checks()
    print()
    checks += _crs_recovery_checks()
    print()
    checks += _extent_checks()
    print()
    checks += _raster_and_error_checks()
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
