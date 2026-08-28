"""The named dataset registry.

Datasets are reachable only by name through a Registry. Nothing else in the
project opens a file under data/ directly: that is what keeps provenance
attached and the working CRS applied.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from . import config, provenance as prov
from .contracts import Col, DatasetKind, DatasetRecord, Provenance

VECTOR_SUFFIXES = frozenset({".geojson", ".json", ".gpkg", ".shp", ".parquet"})
TABLE_SUFFIXES = frozenset({".csv", ".parquet"})


class DatasetNotRegistered(KeyError):
    pass


class MissingProvenance(ValueError):
    pass


class Registry:
    """Holds DatasetRecords for one study area and hands back loaded data."""

    def __init__(
        self,
        study_area: config.StudyArea | None = None,
        manifest_path: Path | None = None,
        root: Path | None = None,
    ) -> None:
        self.study_area = study_area or config.STUDY_AREA
        self.manifest_path = Path(manifest_path or config.MANIFEST_PATH)
        self.root = Path(root or config.PROJECT_ROOT)
        self._records: dict[str, DatasetRecord] = {}
        self._cache: dict[str, Any] = {}

    @property
    def working_crs(self) -> str:
        return self.study_area.working_crs

    def register(
        self,
        name: str,
        kind: DatasetKind,
        path: Path,
        provenance: Provenance,
    ) -> DatasetRecord:
        """Register a dataset. Re-registering a name replaces it, which is how a
        re-acquisition lands; a provenance naming a different dataset raises."""
        if not isinstance(provenance, Provenance):
            raise MissingProvenance(
                f"dataset {name!r} cannot be registered without a Provenance record"
            )
        if provenance.dataset != name:
            raise ValueError(
                f"provenance is for {provenance.dataset!r} but was registered as {name!r}"
            )
        if kind not in ("vector", "raster", "table"):
            raise ValueError(f"unknown dataset kind {kind!r} for {name!r}")
        resolved = Path(path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(
                f"dataset {name!r} points at {resolved}, which does not exist"
            )
        stamped = dataclasses.replace(provenance, working_crs=self.working_crs)
        record = DatasetRecord(name=name, kind=kind, path=resolved, provenance=stamped)
        self._records[name] = record
        self._cache.pop(name, None)
        return record

    def names(self) -> list[str]:
        return sorted(self._records)

    def records(self) -> list[DatasetRecord]:
        return [self._records[name] for name in self.names()]

    def record(self, name: str) -> DatasetRecord:
        try:
            return self._records[name]
        except KeyError:
            raise DatasetNotRegistered(
                f"no dataset named {name!r}; registered: {self.names()}"
            ) from None

    def provenance_of(self, name: str) -> Provenance:
        return self.record(name).provenance

    def path_of(self, name: str) -> Path:
        return self.record(name).path

    def load(self, name: str) -> Any:
        """Return the dataset already in the working CRS.

        The caller gets a copy. The cache holds the projected master, so code
        that mutates what it was handed cannot poison later readers. Rasters are
        opened by path with rasterio, not here.
        """
        record = self.record(name)
        if record.kind == "raster":
            raise ValueError(
                f"{name!r} is a raster; use path_of({name!r}) and open it with rasterio"
            )
        if name not in self._cache:
            self._cache[name] = self._read(record)
        return self._cache[name].copy()

    def _read(self, record: DatasetRecord) -> Any:
        suffix = record.path.suffix.lower()
        if record.kind == "vector":
            if suffix not in VECTOR_SUFFIXES:
                raise ValueError(f"{record.name!r}: unsupported vector file {suffix!r}")
            gdf = gpd.read_parquet(record.path) if suffix == ".parquet" else gpd.read_file(record.path)
            if gdf.crs is None:
                raise ValueError(
                    f"{record.name!r} has no CRS on disk; it cannot be reprojected to "
                    f"{self.working_crs}. Record the declared CRS at retrieval time."
                )
            return gdf.to_crs(self.working_crs)
        if suffix not in TABLE_SUFFIXES:
            raise ValueError(f"{record.name!r}: unsupported table file {suffix!r}")
        return pd.read_parquet(record.path) if suffix == ".parquet" else pd.read_csv(record.path)

    def clear_cache(self) -> None:
        self._cache.clear()

    def save_manifest(self) -> Path:
        return prov.write_manifest(self.records(), path=self.manifest_path, root=self.root)

    def load_manifest(self) -> list[DatasetRecord]:
        records = prov.read_manifest(path=self.manifest_path, root=self.root)
        self._records = {record.name: record for record in records}
        self._cache.clear()
        return self.records()


def _self_check() -> int:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="registry_check_"))
    manifest = root / "snapshot" / "manifest.json"
    geoid = f"{config.STUDY_AREA.county_geoid}001000"

    vector_path = root / "tracts.geojson"
    gpd.GeoDataFrame(
        {Col.GEOID: [geoid]},
        geometry=gpd.points_from_xy([-98.5], [39.8]),
        crs=config.STORAGE_CRS,
    ).to_file(vector_path, driver="GeoJSON")

    table_path = root / "acs.csv"
    pd.DataFrame({Col.GEOID: [geoid], Col.POPULATION: [4200]}).to_csv(table_path, index=False)

    raster_path = root / "elevation.tif"
    raster_path.write_bytes(b"not a real raster")

    def make_prov(name: str, declared: str, working: str = "EPSG:3857") -> Provenance:
        return Provenance(
            dataset=name,
            source_url=f"https://example.invalid/{name}",
            retrieved_at=prov.utc_now(),
            declared_crs=declared,
            working_crs=working,
            vintage="test",
            feature_count=1,
            license="test",
        )

    registry = Registry(manifest_path=manifest, root=root)
    registry.register("tracts", "vector", vector_path, make_prov("tracts", config.STORAGE_CRS))
    registry.register("acs", "table", table_path, make_prov("acs", "n/a"))
    registry.register("elevation", "raster", raster_path, make_prov("elevation", "EPSG:5070"))

    checks: list[tuple[str, bool]] = []

    try:
        registry.register("bad", "vector", vector_path, None)
        checks.append(("registering without provenance raises", False))
    except MissingProvenance:
        checks.append(("registering without provenance raises", True))

    try:
        registry.register("other", "vector", vector_path, make_prov("tracts", config.STORAGE_CRS))
        checks.append(("provenance naming a different dataset raises", False))
    except ValueError:
        checks.append(("provenance naming a different dataset raises", True))

    try:
        registry.load("nope")
        checks.append(("unknown name raises", False))
    except DatasetNotRegistered:
        checks.append(("unknown name raises", True))

    try:
        registry.register("ghost", "vector", root / "missing.geojson", make_prov("ghost", config.STORAGE_CRS))
        checks.append(("missing file raises", False))
    except FileNotFoundError:
        checks.append(("missing file raises", True))

    checks.append(
        (
            "register stamps the registry working CRS onto provenance",
            registry.provenance_of("tracts").working_crs == registry.working_crs,
        )
    )

    tracts = registry.load("tracts")
    checks.append(("vector arrives in the working CRS", tracts.crs.to_string() == registry.working_crs))

    tracts["injected"] = 99
    checks.append(("mutating a loaded frame cannot poison the cache", "injected" not in registry.load("tracts").columns))

    vector_path.unlink()
    checks.append(("second load is served from cache, not disk", len(registry.load("tracts")) == 1))

    checks.append(("table loads", len(registry.load("acs")) == 1))
    checks.append(("names are sorted", registry.names() == ["acs", "elevation", "tracts"]))
    checks.append(("provenance reachable by name", registry.provenance_of("tracts").dataset == "tracts"))

    try:
        registry.load("elevation")
        checks.append(("loading a raster directs you to path_of", False))
    except ValueError:
        checks.append(("loading a raster directs you to path_of", True))
    checks.append(("raster reachable by path", registry.path_of("elevation").exists()))

    registry.save_manifest()
    stored = manifest.read_text(encoding="utf-8")
    checks.append(("manifest stores paths relative to root", '"path": "elevation.tif"' in stored))
    checks.append(("manifest records the working CRS actually applied", f'"working_crs": "{registry.working_crs}"' in stored))

    reloaded = Registry(manifest_path=manifest, root=root)
    reloaded.load_manifest()
    checks.append(("manifest round-trips through the registry", reloaded.records() == registry.records()))
    checks.append(("reloaded registry loads data", len(reloaded.load("acs")) == 1))

    failed = 0
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        failed += 0 if ok else 1

    print(f"\nworking crs: {registry.working_crs}   manifest: {manifest}")
    print("all checks passed" if failed == 0 else f"{failed} check(s) failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_self_check())
