"""Small helpers shared by every layer. No I/O here."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

# Letters NFKD does not decompose. Needed so `Bjørn` matches `bjorn.no`.
_EXTRA_FOLD = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "œ": "oe", "Œ": "OE",
    "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "ı": "i", "þ": "th", "Þ": "TH", "ð": "d",
})


def fold(s: str) -> str:
    """Strip diacritics in any Latin script: `Holík` -> `Holik`, `Bjørn` -> `Bjorn`."""
    s = s.translate(_EXTRA_FOLD)
    return "".join(ch for ch in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(ch))


def num(v: Any) -> float | None:
    """Empty -> None. Never 0: a missing number must not pass or fail a threshold."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    for ch in (" ", "\u00a0", "\u202f", "_"):
        s = s.replace(ch, "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_TRUE = {"yes", "true", "1", "ano", "ja", "oui", "si", "tak", "igen"}
_FALSE = {"no", "false", "0", "ne", "nei", "non", "nein", "nie", "nem"}


def tri(v: Any) -> bool | None:
    """CSV yes/no in any supported language -> True/False, unknown -> None."""
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def age_years(iso_date: str | None, today: date | None = None) -> int | None:
    if not iso_date:
        return None
    try:
        d = date.fromisoformat(iso_date[:10])
    except ValueError:
        return None
    return ((today or date.today()) - d).days // 365


def split_arg(v: str | None, *, spaces: bool = False) -> list[str]:
    """CLI list: `a,b;c`. With `spaces=True` whitespace separates too (codes)."""
    if not v:
        return []
    pattern = r"[,;\s]+" if spaces else r"[,;]+"
    return [x.strip() for x in re.split(pattern, v) if x.strip()]


def split_cell(v: Any) -> list[str]:
    """CSV cell with `;`-separated values -> list."""
    return [x.strip() for x in str(v or "").split(";") if x.strip()]


def join_cell(values: list[str]) -> str:
    return ";".join(values)


def norm_code(code: str) -> str:
    """Industry code for prefix matching: `46.11Z` -> `4611Z`, `46.180` -> `46180`."""
    return re.sub(r"[^0-9A-Za-z]", "", code).upper()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(s).lower()).strip("-")


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("\u00a0", " ")).strip()


def fmt_int(v: float | None) -> str:
    return "" if v is None else str(int(v))
