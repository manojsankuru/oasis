"""Provenance records and the snapshot manifest.

Serialisation only. A dataset without a Provenance record does not enter the
registry, so this module is what makes invariant 6 enforceable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .contracts import DatasetKind, DatasetRecord, Provenance

MANIFEST_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(moment: datetime, dataset: str) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            f"retrieved_at for {dataset!r} is naive; provenance timestamps must "
            "carry a timezone. Use provenance.utc_now()."
        )
    return moment


def _relative(path: Path, root: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _absolute(stored: str, root: Path) -> Path:
    candidate = Path(stored)
    if candidate.is_absolute():
        return candidate.resolve()
    return (Path(root) / candidate).resolve()


def provenance_to_dict(record: Provenance) -> dict[str, Any]:
    _require_aware(record.retrieved_at, record.dataset)
    return {
        "dataset": record.dataset,
        "source_url": record.source_url,
        "retrieved_at": record.retrieved_at.isoformat(),
        "declared_crs": record.declared_crs,
        "working_crs": record.working_crs,
        "vintage": record.vintage,
        "feature_count": record.feature_count,
        "license": record.license,
        "request_params": dict(record.request_params),
        "notes": list(record.notes),
    }


def provenance_from_dict(payload: dict[str, Any]) -> Provenance:
    dataset = payload["dataset"]
    retrieved_at = datetime.fromisoformat(payload["retrieved_at"])
    _require_aware(retrieved_at, dataset)
    return Provenance(
        dataset=dataset,
        source_url=payload["source_url"],
        retrieved_at=retrieved_at,
        declared_crs=payload["declared_crs"],
        working_crs=payload["working_crs"],
        vintage=payload["vintage"],
        feature_count=int(payload["feature_count"]),
        license=payload["license"],
        request_params=dict(payload.get("request_params") or {}),
        notes=tuple(payload.get("notes") or ()),
    )


def record_to_dict(record: DatasetRecord, root: Path) -> dict[str, Any]:
    return {
        "name": record.name,
        "kind": record.kind,
        "path": _relative(record.path, root),
        "provenance": provenance_to_dict(record.provenance),
    }


def record_from_dict(payload: dict[str, Any], root: Path) -> DatasetRecord:
    kind: DatasetKind = payload["kind"]
    if kind not in ("vector", "raster", "table"):
        raise ValueError(f"unknown dataset kind {kind!r} for {payload.get('name')!r}")
    return DatasetRecord(
        name=payload["name"],
        kind=kind,
        path=_absolute(payload["path"], root),
        provenance=provenance_from_dict(payload["provenance"]),
    )


def write_manifest(
    records: list[DatasetRecord],
    path: Path | None = None,
    root: Path | None = None,
) -> Path:
    """Write the snapshot manifest.

    Paths under root are stored relative to it, so a manifest survives being
    moved between machines. A path outside root is stored absolute and will not
    resolve elsewhere.
    """
    target = Path(path or config.MANIFEST_PATH)
    base = Path(root or config.PROJECT_ROOT)
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "written_at": utc_now().isoformat(),
        "datasets": [record_to_dict(record, base) for record in records],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def read_manifest(
    path: Path | None = None,
    root: Path | None = None,
) -> list[DatasetRecord]:
    target = Path(path or config.MANIFEST_PATH)
    base = Path(root or config.PROJECT_ROOT)
    if not target.exists():
        raise FileNotFoundError(
            f"{target} does not exist. Run: python -m src.acquire"
        )
    payload = json.loads(target.read_text(encoding="utf-8"))
    version = payload.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ValueError(
            f"{target} has manifest_version {version!r}, expected {MANIFEST_VERSION}"
        )
    return [record_from_dict(item, base) for item in payload["datasets"]]


def _self_check() -> int:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="manifest_check_"))
    manifest = root / "snapshot" / "manifest.json"

    originals = [
        DatasetRecord(
            name="tracts",
            kind="vector",
            path=root / "snapshot" / "tracts.geojson",
            provenance=Provenance(
                dataset="tracts",
                source_url="https://tigerweb.geo.census.gov/arcgis/rest/services/x/0/query",
                retrieved_at=utc_now(),
                declared_crs="EPSG:4326",
                working_crs="EPSG:5070",
                vintage="TIGERweb current",
                feature_count=123,
                license="public domain",
                request_params={"outSR": "4326", "f": "geojson"},
                notes=("layer 0", "paged"),
            ),
        ),
        DatasetRecord(
            name="elevation",
            kind="raster",
            path=root / "snapshot" / "elevation.tif",
            provenance=Provenance(
                dataset="elevation",
                source_url="https://elevation.nationalmap.gov/arcgis/rest/services/x/ImageServer",
                retrieved_at=utc_now(),
                declared_crs="EPSG:5070",
                working_crs="EPSG:5070",
                vintage="3DEP 2024-06",
                feature_count=1,
                license="public domain",
            ),
        ),
    ]

    write_manifest(originals, path=manifest, root=root)
    restored = read_manifest(path=manifest, root=root)

    checks: list[tuple[str, bool]] = [
        ("round-trips to an equal list", restored == originals),
        ("preserves count", len(restored) == len(originals)),
        ("notes stay a tuple", isinstance(restored[0].provenance.notes, tuple)),
        ("path returns as a Path", isinstance(restored[0].path, Path)),
        ("timestamp keeps its timezone", restored[0].provenance.retrieved_at.tzinfo is not None),
        (
            "timestamp is exact",
            restored[0].provenance.retrieved_at == originals[0].provenance.retrieved_at,
        ),
        ("paths stored relative", '"path": "snapshot/tracts.geojson"' in manifest.read_text(encoding="utf-8")),
    ]

    naive = Provenance(
        dataset="x", source_url="", retrieved_at=datetime.now(),
        declared_crs="", working_crs="", vintage="", feature_count=0, license="",
    )
    try:
        provenance_to_dict(naive)
        checks.append(("rejects a naive timestamp", False))
    except ValueError:
        checks.append(("rejects a naive timestamp", True))

    failed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        failed += 0 if ok else 1

    print(f"\nmanifest: {manifest}")
    print("all checks passed" if failed == 0 else f"{failed} check(s) failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_self_check())
