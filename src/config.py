"""Settings, paths, CRS constants and the study-area parameter.

Nothing here is specific to one county except the two StudyArea values. There is
no bounding box in this file, in any form: the study-area extent is derived from
the retrieved tract layer at run time by derive_bbox().

Importing this module does not touch the filesystem. Entry points that write
call ensure_dirs() themselves.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pyproj import CRS

from .contracts import BBox

PROJECT_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(PROJECT_ROOT / ".env", encoding="utf-8-sig")

DATA_DIR = PROJECT_ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshot"
DERIVED_DIR = DATA_DIR / "derived"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
PAPER_DIR = OUTPUTS_DIR / "paper"
LOGS_DIR = PROJECT_ROOT / "logs"
MANIFEST_PATH = SNAPSHOT_DIR / "manifest.json"


def ensure_dirs() -> None:
    for directory in (DATA_DIR, SNAPSHOT_DIR, DERIVED_DIR, OUTPUTS_DIR, PAPER_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


_PLACEHOLDERS = frozenset(
    {"replace-me", "your-project-id", "your-key-here", "changeme", "todo"}
)


def _setting(name: str) -> str:
    """Read a setting, treating an unedited .env.example placeholder as unset.

    A placeholder that reads as a real value produces a config self-check that
    passes and a run that then dies at the network boundary.
    """
    value = os.getenv(name, "").strip().strip('"').strip("'").strip()
    return "" if value.lower() in _PLACEHOLDERS else value


STORAGE_CRS = "EPSG:4326"
DEFAULT_WORKING_CRS = "EPSG:5070"


@dataclass(frozen=True, slots=True)
class StudyArea:
    """One county. Threaded from here into every module; nothing downstream may
    hardcode a county, a state or an extent."""

    name: str
    state_fips: str
    county_fips: str
    working_crs: str = DEFAULT_WORKING_CRS

    def __post_init__(self) -> None:
        if len(self.state_fips) != 2 or not self.state_fips.isdigit():
            raise ValueError(f"state_fips must be two digits, got {self.state_fips!r}")
        if len(self.county_fips) != 3 or not self.county_fips.isdigit():
            raise ValueError(f"county_fips must be three digits, got {self.county_fips!r}")
        if CRS.from_user_input(self.working_crs).is_geographic:
            raise ValueError(
                f"working_crs must be a projected CRS, got {self.working_crs!r}; "
                "area, distance and buffer operations return degrees in a geographic CRS"
            )

    @property
    def county_geoid(self) -> str:
        return f"{self.state_fips}{self.county_fips}"


STUDY_AREA = StudyArea(
    name="Charleston County, South Carolina",
    state_fips="45",
    county_fips="019",
)

TRANSFER_AREA = StudyArea(
    name="Chatham County, Georgia",
    state_fips="13",
    county_fips="051",
)


def derive_bbox(tracts: Any) -> BBox:
    """Study-area extent in EPSG:4326, computed from the retrieved tract layer.

    The only supported way to obtain the study-area extent. A literal bounding
    box anywhere in this project is a bug.
    """
    if tracts is None or len(tracts) == 0:
        raise ValueError("cannot derive a bounding box from an empty tract layer")
    minx, miny, maxx, maxy = tracts.to_crs(STORAGE_CRS).total_bounds
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        raise ValueError(
            f"tract layer has {len(tracts)} rows but no usable geometry; "
            "cannot derive a bounding box"
        )
    return (float(minx), float(miny), float(maxx), float(maxy))


REQUEST_TIMEOUT_S = 60.0
MAX_RETRIES = 3
RETRY_BACKOFF_S = 2.0

MAX_ITERATIONS = 6

CENSUS_API_KEY = _setting("CENSUS_API_KEY")

CLEMSON_API_BASE_URL = _setting("CLEMSON_API_BASE_URL")
OPENAI_PROXY_URL = _setting("OPENAI_PROXY_URL")
CLEMSON_API_KEY = _setting("CLEMSON_API_KEY")
CLEMSON_MODEL = _setting("CLEMSON_MODEL")

GOOGLE_CLOUD_PROJECT = _setting("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = _setting("GOOGLE_CLOUD_LOCATION") or "us-central1"
GEMINI_MODEL = _setting("GEMINI_MODEL")

PROVIDER = "vertex" if GOOGLE_CLOUD_PROJECT else "openai"

if PROVIDER == "vertex":
    API_BASE_URL = (
        f"https://{GOOGLE_CLOUD_LOCATION}-aiplatform.googleapis.com/v1beta1"
        f"/projects/{GOOGLE_CLOUD_PROJECT}/locations/{GOOGLE_CLOUD_LOCATION}/endpoints/openapi"
    )
    if not GEMINI_MODEL:
        MODEL = ""
    elif GEMINI_MODEL.startswith("google/"):
        MODEL = GEMINI_MODEL
    else:
        MODEL = f"google/{GEMINI_MODEL}"
    API_KEY = ""
    ENDPOINT_SOURCE = "GOOGLE_CLOUD_PROJECT (Vertex AI OpenAI-compatible endpoint)"
else:
    API_BASE_URL = OPENAI_PROXY_URL or CLEMSON_API_BASE_URL
    MODEL = CLEMSON_MODEL
    API_KEY = CLEMSON_API_KEY
    ENDPOINT_SOURCE = "OPENAI_PROXY_URL" if OPENAI_PROXY_URL else "CLEMSON_API_BASE_URL"

SUPPORTS_MODEL_LISTING = PROVIDER == "openai"


def missing_settings() -> list[str]:
    missing: list[str] = []
    if PROVIDER == "vertex":
        if not GEMINI_MODEL:
            missing.append("GEMINI_MODEL")
        return missing
    if not API_BASE_URL:
        missing.append("OPENAI_PROXY_URL or CLEMSON_API_BASE_URL")
    if not MODEL:
        missing.append("CLEMSON_MODEL")
    return missing


def setting_warnings() -> list[str]:
    """Settings that are not required to start but will bite later."""
    warnings: list[str] = []
    if not CENSUS_API_KEY:
        warnings.append(
            "CENSUS_API_KEY is unset; a keyless ACS request returns HTTP 200 with an "
            "HTML 'Missing Key' page, not data. Free key: "
            "https://api.census.gov/data/key_signup.html"
        )
    return warnings
