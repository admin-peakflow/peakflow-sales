"""Contract every registry adapter implements.

An adapter turns one public business registry into canonical rows
(`grizly.schema`). Rules for adding one are in README ("Adding a country"):
verify every claim against the live API, record a trimmed fixture, write a
mapping test, document the limits you found in the module docstring.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from grizly.util import slug

Log = Callable[[str], None]


class SourceError(RuntimeError):
    """Request the registry rejects; retrying will not help (query too broad etc.)."""


@dataclass
class Query:
    industry_codes: list[str]                  # native codes of the registry
    regions: set[str] = field(default_factory=set)   # ISO 3166-2; empty = whole country
    legal_form_classes: list[str] = field(default_factory=list)  # empty = all
    min_employees: int | None = None           # used where the registry filters on it
    limit: int = 0


class Source(ABC):
    name: ClassVar[str]             # `<iso2>-<registry>`, e.g. `cz-ares`
    country: ClassVar[str]          # ISO 3166-1 alpha-2
    currency: ClassVar[str]         # ISO 4217 of revenue figures
    language: ClassVar[str]         # main language of company websites
    industry_scheme: ClassVar[str]  # e.g. `CZ-NACE`, `NAF`, `SN`
    id_label: ClassVar[str]         # what the national id is called (IČO, SIREN…)
    needs_key: ClassVar[str | None] = None   # env var with an API key, if any
    has_details: ClassVar[bool] = False      # True if `details()` makes extra calls
    provides: ClassVar[str] = ""    # one line for `grizly sources`

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent

    @abstractmethod
    def regions(self) -> dict[str, str]:
        """ISO 3166-2 code -> human name."""

    @abstractmethod
    def search(self, q: Query, log: Log) -> Iterator[dict[str, str]]:
        """Canonical rows, lazily. The caller stops iterating at its limit."""

    def details(self, row: dict[str, str]) -> dict[str, str]:
        """Extra per-company calls (employees, officers, accounts). Default: none."""
        return {}

    def normalize(self, row: dict[str, str]) -> None:
        """Backfill derived columns on rows from older files or CSV imports."""
        row["country"] = row.get("country") or self.country
        row["industry_scheme"] = row.get("industry_scheme") or self.industry_scheme

    @abstractmethod
    def selftest(self) -> str:
        """One cheap live call. Returns a human-readable detail or raises."""

    # ---------------------------------------------------------------- helpers

    def resolve_regions(self, names: list[str]) -> set[str]:
        """`jihomoravsky`, `CZ-64`, `Jihomoravský kraj` -> {`CZ-64`}. Unknown -> error."""
        table = self.regions()
        out: set[str] = set()
        for raw in names:
            want, up = slug(raw), raw.strip().upper()
            hit = next((c for c in table
                        if up == c.upper() or want == slug(c.split("-", 1)[-1])), None)
            if hit is None:
                hit = next((c for c, n in table.items() if want == slug(n)), None)
            if hit is None and len(want) >= 4:
                hit = next((c for c, n in table.items() if want in slug(n)), None)
            if hit is None:
                options = ", ".join(f"{c} ({n})" for c, n in sorted(table.items()))
                raise SourceError(f"unknown region '{raw}' for {self.name}. Use: {options}")
            out.add(hit)
        return out

    def http_json(self, url: str, payload: dict[str, Any] | None = None, *,
                  timeout: int = 45, retries: int = 3) -> Any:
        """GET/POST JSON. 400 = bad query (raise, no retry). 404 = {}. Rest: retry."""
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if body:
            headers["Content-Type"] = "application/json"
        last = ""
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", errors="replace")
                if e.code == 400:
                    raise SourceError(self.error_message(text)) from e
                if e.code == 404:
                    return {}
                last = f"HTTP {e.code}"
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                last = str(e)
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{self.name}: no answer after {retries} attempts: {last}")

    def error_message(self, body: str) -> str:
        return body[:300]


def person_name(*parts: str | None) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def title_name(s: str) -> str:
    """`DUPONT` -> `Dupont`, `JEAN-MARC` -> `Jean-Marc`. Registries shout names."""
    if not s.isupper():
        return s
    return " ".join("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))
                    for w in s.split())
