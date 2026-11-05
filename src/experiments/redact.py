"""Take out of a run transcript what a public repository must not hold.

    python -m src.experiments.redact SOURCE DESTINATION   # redact one transcript
    python -m src.experiments.redact --check              # offline checks

A Vertex transcript logs the endpoint the loop called, and that URL names the
Google Cloud project the runs were billed to. Every transcript goes through
`redact_file` before it is committed: the critic's pinned real-run fixture did,
and the Session 7 cohort transcripts will.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .. import config, critic, verify

PROJECT_SEGMENT = re.compile(r"aiplatform\.googleapis\.com/[^/\s\"']+/projects/([^/\s\"'?#]+)")
"""The project id in a Vertex AI endpoint URL. Anchored to the host, so a local path
that happens to hold a `projects/` folder is never read as a project."""

PROJECT_PLACEHOLDER = "PROJECT_ID"

SECRET_SETTINGS: dict[str, str] = {
    "GOOGLE_CLOUD_PROJECT": PROJECT_PLACEHOLDER,
    "CENSUS_API_KEY": "CENSUS_API_KEY",
}
"""Environment settings whose values never reach a commit, and what replaces each."""

MIN_SECRET_LENGTH = 6

FAKE_PROJECT = "demo-project-424242"
FAKE_KEY = "0123456789abcdef0123456789abcdef01234567"


@dataclass(frozen=True, slots=True)
class Redaction:
    payload: Any
    replaced: dict[str, int]


def strings(payload: Any) -> Iterator[str]:
    """Every string in a JSON payload, dictionary keys included."""
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key)
            yield from strings(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from strings(item)


def project_ids(payload: Any) -> set[str]:
    """Every project id a `projects/<id>/` segment names, the placeholder excluded."""
    return {
        found
        for text in strings(payload)
        for found in PROJECT_SEGMENT.findall(text)
        if found != PROJECT_PLACEHOLDER
    }


def secrets_from(environ: Mapping[str, str]) -> dict[str, str]:
    """The secret values the environment holds, mapped to the placeholder for each."""
    found: dict[str, str] = {}
    for setting, placeholder in SECRET_SETTINGS.items():
        value = environ.get(setting, "").strip().strip('"').strip("'").strip()
        if len(value) >= MIN_SECRET_LENGTH:
            found[value] = placeholder
    return found


def redact(payload: Any, secrets: Mapping[str, str] | None = None) -> Redaction:
    """The payload with every project id and every named secret replaced, everywhere."""
    replacements = {identifier: PROJECT_PLACEHOLDER for identifier in project_ids(payload)}
    replacements.update(secrets or {})
    ordered = sorted(replacements.items(), key=lambda item: -len(item[0]))
    counts: dict[str, int] = {}

    def clean(text: str) -> str:
        for value, placeholder in ordered:
            hits = text.count(value)
            if hits:
                counts[placeholder] = counts.get(placeholder, 0) + hits
                text = text.replace(value, placeholder)
        return text

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return clean(item)
        if isinstance(item, dict):
            return {clean(key) if isinstance(key, str) else key: walk(value) for key, value in item.items()}
        if isinstance(item, list):
            return [walk(value) for value in item]
        return item

    return Redaction(payload=walk(payload), replaced=counts)


def leaks(payload: Any, secrets: Mapping[str, str] | None = None) -> list[str]:
    """What redaction should have removed and did not, named by placeholder, never by value."""
    found: set[str] = set()
    if project_ids(payload):
        found.add(f"{PROJECT_PLACEHOLDER} (a projects/<id>/ segment)")
    texts = list(strings(payload))
    for value, placeholder in (secrets or {}).items():
        if any(value in text for text in texts):
            found.add(placeholder)
    return sorted(found)


def serialised(payload: Any) -> bytes:
    """The transcript as `agent.run_agent` writes it, with LF line endings on every platform."""
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def redact_file(source: Path, destination: Path, secrets: Mapping[str, str] | None = None) -> dict[str, int]:
    """Write the redacted transcript, or nothing if anything would remain."""
    result = redact(json.loads(source.read_text(encoding="utf-8")), secrets)
    remaining = leaks(result.payload, secrets)
    if remaining:
        raise ValueError(f"redaction left {', '.join(remaining)} in {source.name}; nothing was written")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(serialised(result.payload))
    return result.replaced


def _fixture_transcript() -> dict[str, Any]:
    base_url = (
        "https://us-central1-aiplatform.googleapis.com/v1beta1/"
        f"projects/{FAKE_PROJECT}/locations/us-central1/endpoints/openapi"
    )
    return {
        "run_id": "fixture",
        "model": "google/gemini-2.5-pro",
        "final_answer": "Tract 99001000100 holds 4,200 people.",
        "messages": [{"role": "user", "content": f"billed to {FAKE_PROJECT}, again {FAKE_PROJECT}"}],
        "steps": [
            {"step": "llm_request", "payload": {"base_url": base_url, "model": "google/gemini-2.5-pro"}},
            {"step": "tool_result", "payload": {"result": {
                "source_url": f"https://api.census.gov/data/2023/acs/acs5?get=B01003_001E&key={FAKE_KEY}",
                "population": 4200,
            }}},
            {"step": "note", "payload": {FAKE_PROJECT: "a key that is the id"}},
        ],
    }


def _synthetic_checks() -> list[tuple[str, bool]]:
    original = _fixture_transcript()
    secrets = {FAKE_KEY: "CENSUS_API_KEY"}
    result = redact(original, secrets)
    cleaned = result.payload
    texts = list(strings(cleaned))
    again = redact(cleaned, secrets)
    plain = {"final_answer": "4,200 people", "steps": [{"payload": {
        "base_url": "https://example.org/v1/models",
        "path": "/home/someone/projects/geo-agent/logs/run.jsonl",
        "note": "the geo-agent folder",
    }}]}
    untouched = redact(plain, secrets)
    bare = redact({"url": f"https://us-central1-aiplatform.googleapis.com/v1/projects/{FAKE_PROJECT}"}, {})
    print(f"  replaced in the synthetic transcript: {result.replaced}")
    print(f"  leaks before redaction: {leaks(original, secrets)}; after: {leaks(cleaned, secrets)}")

    with tempfile.TemporaryDirectory(prefix="redact-check-") as folder:
        source = Path(folder) / "run_fixture.json"
        destination = Path(folder) / "fixtures" / "run_fixture.json"
        source.write_text(json.dumps(original, indent=2), encoding="utf-8")
        written_counts = redact_file(source, destination, secrets)
        written = destination.read_bytes()

    return [
        ("the project id in base_url is replaced by the placeholder",
         cleaned["steps"][0]["payload"]["base_url"].count(f"projects/{PROJECT_PLACEHOLDER}/") == 1),
        ("the id is gone from every string in the transcript, keys included",
         not any(FAKE_PROJECT in text for text in texts) and PROJECT_PLACEHOLDER in cleaned["steps"][2]["payload"]),
        ("a secret the environment names is replaced wherever it appears",
         not any(FAKE_KEY in text for text in texts) and "key=CENSUS_API_KEY" in cleaned["steps"][1]["payload"]["result"]["source_url"]),
        ("every occurrence is counted, by placeholder",
         result.replaced == {PROJECT_PLACEHOLDER: 4, "CENSUS_API_KEY": 1}),
        ("nothing else in the transcript changes",
         cleaned["final_answer"] == original["final_answer"]
         and cleaned["steps"][1]["payload"]["result"]["population"] == 4200
         and cleaned["model"] == original["model"] and cleaned["run_id"] == original["run_id"]),
        ("redacting a redacted transcript changes nothing", again.payload == cleaned and again.replaced == {}),
        ("a transcript with nothing to hide comes back unchanged, a local projects/ folder included",
         untouched.payload == plain and untouched.replaced == {}),
        ("a Vertex URL that ends at the project id is still redacted",
         FAKE_PROJECT not in bare.payload["url"] and bare.replaced == {PROJECT_PLACEHOLDER: 1}),
        ("an unredacted transcript is reported by placeholder, never by value",
         leaks(original, secrets) == ["CENSUS_API_KEY", f"{PROJECT_PLACEHOLDER} (a projects/<id>/ segment)"]
         and not any(FAKE_PROJECT in item or FAKE_KEY in item for item in leaks(original, secrets))),
        ("the redacted transcript leaks nothing", leaks(cleaned, secrets) == []),
        ("a redacted file is written in the agent's own format with LF line endings",
         written == serialised(cleaned) and b"\r\n" not in written and json.loads(written) == cleaned),
        ("the file writer reports the same counts", written_counts == result.replaced),
    ]


def _fixture_checks() -> list[tuple[str, bool]]:
    fixture = critic.REAL_RUN_FIXTURE
    source = config.OUTPUTS_DIR / fixture.name
    environment = secrets_from(os.environ)
    if not fixture.is_file():
        print(f"  the pinned transcript is missing: {fixture.name}")
        return [("the critic's pinned transcript is on disk and not git-ignored", False)]
    ignored = verify.git_ignores(config.PROJECT_ROOT, fixture)
    content = fixture.read_bytes()
    payload = json.loads(content)
    bases = [
        step.get("payload", {}).get("base_url", "")
        for step in payload.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("payload"), dict) and "base_url" in step["payload"]
    ]
    print(f"  {fixture.name}: {len(content)} bytes, {len(payload.get('steps', []))} steps, "
          f"{len(bases)} logged base_url(s); the environment names {len(environment)} secret(s)")
    checks = [
        ("the critic's pinned transcript is on disk and not git-ignored, so it is committed with the code",
         ignored is False),
        ("its base_url carries the placeholder where the project id was",
         bool(bases) and all(f"projects/{PROJECT_PLACEHOLDER}/" in base for base in bases)),
        ("it names no project id anywhere", project_ids(payload) == set()),
        ("no secret this environment holds appears in it", leaks(payload, environment) == []),
        ("it is stored with LF line endings, as redact_file writes", b"\r\n" not in content),
    ]
    if source.is_file():
        rebuilt = serialised(redact(json.loads(source.read_text(encoding="utf-8")), environment).payload)
        print(f"  the source transcript is on disk, so the fixture is compared with a fresh redaction of it")
        checks.append(("the fixture is exactly the redaction of its source transcript", rebuilt == content))
    else:
        print(f"  the source transcript is not on disk in this checkout, so it is not compared")
    return checks


def _self_check() -> int:
    print("REDACT -- what a committed transcript must not hold\n")
    checks = _synthetic_checks()
    print()
    checks += _fixture_checks()
    print()
    checks += verify.discipline_checks(sys.modules[__name__])
    return verify.report(checks)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m src.experiments.redact SOURCE DESTINATION")
        return 2
    source, destination = Path(argv[0]), Path(argv[1])
    replaced = redact_file(source, destination, secrets_from(os.environ))
    summary = ", ".join(f"{name} x{count}" for name, count in sorted(replaced.items())) or "nothing to replace"
    print(f"redacted {source.name} into {destination.as_posix()}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check() if "--check" in sys.argv[1:] else main(sys.argv[1:]))
