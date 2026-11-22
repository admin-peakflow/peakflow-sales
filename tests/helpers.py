from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from grizly import profile as profiles
from grizly.fx import FX

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_profile(name: str) -> profiles.Profile:
    return profiles.load(str(ROOT / "profiles" / f"{name}.toml"))


def static_fx(rates: dict[str, float] | None = None) -> FX:
    return FX(source="static", rates=rates or {}, cache_dir=Path(tempfile.gettempdir()))
