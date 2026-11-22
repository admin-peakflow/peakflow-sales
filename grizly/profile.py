"""Seller profile: everything that belongs to ONE selling company lives here.

A profile is a TOML file (see `profiles/`). The code knows nothing about any
seller's offers, thresholds or home region. Changing who we sell to, or
running the tool for another company, means editing or adding a profile —
never editing Python.

Profiles are validated on load: an unknown field or operator in a rule is an
error, not a rule that silently never matches.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grizly.schema import LEGAL_FORM_CLASSES, TAG_DO_NOT_CONTACT

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIRS = (Path.cwd() / "profiles", REPO_ROOT / "profiles")
DEFAULT_PROFILE = "peakflow"

Cond = dict[str, dict[str, Any]]


class ProfileError(ValueError):
    """Profile file is invalid. Message says where."""


# Facts a rule may test, with their kind. Signals declared in the profile are
# added per profile. Kind decides which operators make sense.
CORE_FACTS: dict[str, str] = {
    "revenue": "number",            # actual revenue, else estimate; in profile currency
    "revenue_actual": "number",     # only a published figure, never the estimate
    "revenue_is_estimate": "bool",
    "employees_min": "number",
    "employees_max": "number",
    "company_age_years": "number",
    "industry_code_count": "number",
    "country": "string",
    "legal_form": "string",
    "legal_form_class": "string",
    "hq_region": "string",
    "hq_region_code": "string",
    "hq_city": "string",
    "source": "string",
    "web": "string",
    "name": "string",
    "trade_name": "string",
    "industry": "string",
    "short_description": "string",
    "personalization": "string",
    "officers": "string",
    "industry_codes": "list",
    "registry_flags": "list",
    "tags": "list",
    "needs": "list",
    "lead_offer": "string",
}

OPS: dict[str, set[str]] = {
    "number": {"min", "max", "lt", "gt", "eq", "known"},
    "bool": {"is", "known"},
    "string": {"eq", "in", "present", "known", "prefix"},
    "list": {"any", "none", "prefix", "present"},
}
SIGNAL_KINDS = {"boolean": "bool", "integer": "number", "number": "number", "string": "string"}

LANGUAGE_NAMES = {
    "cs": "Czech", "sk": "Slovak", "en": "English", "de": "German", "fr": "French",
    "no": "Norwegian", "nb": "Norwegian", "sv": "Swedish", "da": "Danish", "fi": "Finnish",
    "pl": "Polish", "hu": "Hungarian", "nl": "Dutch", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "et": "Estonian", "lv": "Latvian", "lt": "Lithuanian",
}


@dataclass(frozen=True)
class Signal:
    """A fact the model extracts from the website, defined by the seller."""
    name: str
    type: str
    description: str
    quality: bool = False
    column_cs: str | None = None


@dataclass(frozen=True)
class Market:
    key: str
    source: str
    industry_codes: list[str]
    regions: list[str]
    legal_form_classes: list[str]
    min_employees: int | None
    language: str | None


@dataclass(frozen=True)
class NamedRule:
    name: str
    when: Cond


@dataclass(frozen=True)
class Offer:
    name: str
    when: Cond
    size_basis: str | None = None      # "revenue" | "employees" | None
    size_min: float | None = None
    size_max: float | None = None
    size_full_at: float | None = None


@dataclass(frozen=True)
class EstimateRule:
    country: str
    currency: str
    prefixes: tuple[str, ...]
    value: float


@dataclass(frozen=True)
class Suppression:
    file: Path
    tag: str


@dataclass(frozen=True)
class Example:
    name: str
    row: dict[str, str]
    expect: dict[str, Any]


@dataclass
class Profile:
    path: Path
    name: str
    website: str
    language: str
    currency: str
    context: str
    output_columns: str
    delimiter: str
    encoding: str
    default_market: str
    markets: dict[str, Market]
    signals: list[Signal]
    tags: list[NamedRule]
    needs: list[NamedRule]
    exclude_tags: set[str]
    offers: list[Offer]
    no_offer: str
    size_points: int
    estimate_factor: float
    score_points: list[tuple[int, Cond]]
    score_cap: int
    estimate_min_employees: int
    estimates: list[EstimateRule]
    fx_source: str
    fx_rates: dict[str, float]
    suppression: list[Suppression]
    examples: list[Example] = field(default_factory=list)

    def signal(self, name: str) -> Signal | None:
        return next((s for s in self.signals if s.name == name), None)

    def market(self, key: str | None = None) -> Market:
        k = key or self.default_market
        if k not in self.markets:
            raise ProfileError(f"{self.path.name}: market '{k}' not defined "
                               f"(have: {', '.join(sorted(self.markets)) or 'none'})")
        return self.markets[k]

    def language_for(self, country: str) -> str:
        """Language of generated text for a company in `country`."""
        for m in self.markets.values():
            if m.language and _source_country(m.source) == country.upper():
                return m.language
        return self.language

    def language_name(self, country: str) -> str:
        code = self.language_for(country)
        return LANGUAGE_NAMES.get(code, code)

    @property
    def default_country(self) -> str:
        if not self.markets:
            return ""
        return _source_country(self.market().source)


def _source_country(source_name: str) -> str:
    """`cz-ares` -> `CZ`. Adapters are named `<iso2>-<registry>`."""
    return source_name.split("-", 1)[0].upper() if "-" in source_name else ""


# --------------------------------------------------------------------------- load

def resolve(name_or_path: str | None) -> Path:
    """Profile name (`peakflow`) or path. Env `GRIZLY_PROFILE` is the default."""
    ref = name_or_path or os.environ.get("GRIZLY_PROFILE") or DEFAULT_PROFILE
    p = Path(ref)
    if p.suffix == ".toml" or p.exists():
        if not p.exists():
            raise ProfileError(f"profile not found: {p}")
        return p
    for d in PROFILE_DIRS:
        candidate = d / f"{ref}.toml"
        if candidate.exists():
            return candidate
    raise ProfileError(f"profile '{ref}' not found in {', '.join(str(d) for d in PROFILE_DIRS)}")


def load(name_or_path: str | None = None) -> Profile:
    path = resolve(name_or_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"{path.name}: invalid TOML: {e}") from e
    return parse(data, path)


def parse(data: dict[str, Any], path: Path) -> Profile:
    where = path.name
    seller = _table(data, "seller", where)
    output = _table(data, "output", where, required=False)
    rules = _table(data, "rules", where)
    scoring = _table(data, "scoring", where, required=False)
    estimate = _table(data, "estimate", where, required=False)
    fx = _table(data, "fx", where, required=False)

    signals = [_signal(s, f"{where} [[signals]] #{i + 1}")
               for i, s in enumerate(_list(data, "signals", where))]
    names = [s.name for s in signals]
    if len(set(names)) != len(names):
        raise ProfileError(f"{where}: duplicate signal names")
    for s in signals:
        if s.name in CORE_FACTS:
            raise ProfileError(f"{where}: signal '{s.name}' clashes with a core field")
    facts = dict(CORE_FACTS) | {s.name: SIGNAL_KINDS[s.type] for s in signals}

    markets_raw = _table(data, "markets", where, required=False)
    default_market = str(markets_raw.get("default") or "")
    markets: dict[str, Market] = {}
    for k, v in markets_raw.items():
        if k == "default":
            continue
        if not isinstance(v, dict):
            raise ProfileError(f"{where}: [markets.{k}] must be a table")
        markets[k] = _market(k, v, f"{where} [markets.{k}]")
    if markets and not default_market:
        default_market = next(iter(markets))
    if default_market and default_market not in markets:
        raise ProfileError(f"{where}: markets.default = '{default_market}' is not defined")

    tags = _named(rules, "tags", "tag", facts, where)
    needs = _named(rules, "needs", "need", facts, where)
    offers = [_offer(r, facts, f"{where} [[rules.offers]] #{i + 1}")
              for i, r in enumerate(_list(rules, "offers", where))]

    suppression = []
    for i, sup in enumerate(_list(data, "suppression", where)):
        w = f"{where} [[suppression]] #{i + 1}"
        suppression.append(Suppression(Path(_str(sup, "file", w)), _str(sup, "tag", w)))

    exclude = {str(t) for t in rules.get("exclude_tags") or []}
    exclude |= {s.tag for s in suppression}
    exclude.add(TAG_DO_NOT_CONTACT)

    points: list[tuple[int, Cond]] = []
    for i, p in enumerate(_list(scoring, "points", where)):
        w = f"{where} [[scoring.points]] #{i + 1}"
        points.append((_int(p, "points", w), _cond(p.get("when"), facts, w)))

    estimates = []
    for i, e in enumerate(_list(estimate, "revenue_per_employee", where)):
        w = f"{where} [[estimate.revenue_per_employee]] #{i + 1}"
        estimates.append(EstimateRule(
            country=_str(e, "country", w).upper(), currency=_str(e, "currency", w).upper(),
            prefixes=tuple(str(x) for x in e.get("industry_prefixes") or []),
            value=float(_num_field(e, "value", w))))

    columns = str(output.get("columns") or "en")
    if columns not in ("en", "cs"):
        raise ProfileError(f"{where}: [output] columns must be 'en' or 'cs'")
    fx_source = str(fx.get("source") or "ecb")
    if fx_source not in ("ecb", "static"):
        raise ProfileError(f"{where}: [fx] source must be 'ecb' or 'static'")

    profile = Profile(
        path=path,
        name=_str(seller, "name", f"{where} [seller]"),
        website=str(seller.get("website") or ""),
        language=str(seller.get("language") or "en"),
        currency=_str(seller, "currency", f"{where} [seller]").upper(),
        context=str(seller.get("context") or "").strip(),
        output_columns=columns,
        delimiter=str(output.get("delimiter") or ","),
        encoding=str(output.get("encoding") or "utf-8"),
        default_market=default_market,
        markets=markets,
        signals=signals,
        tags=tags,
        needs=needs,
        exclude_tags=exclude,
        offers=offers,
        no_offer=str(rules.get("no_offer") or "NONE"),
        size_points=int(scoring.get("size_points") or 0),
        estimate_factor=_num_field(scoring, "estimate_factor", f"{where} [scoring]")
        if "estimate_factor" in scoring else 0.5,
        score_points=points,
        score_cap=int(scoring.get("cap") or 100),
        estimate_min_employees=int(estimate.get("min_employees") or 0),
        estimates=estimates,
        fx_source=fx_source,
        fx_rates={str(k).upper(): float(v) for k, v in (fx.get("rates") or {}).items()},
        suppression=suppression,
    )
    profile.examples = [_example(e, f"{where} [[examples]] #{i + 1}")
                        for i, e in enumerate(_list(data, "examples", where))]
    if len(profile.delimiter) != 1:
        raise ProfileError(f"{where}: [output] delimiter must be one character")
    return profile


# --------------------------------------------------------------------------- pieces

def _named(rules: dict[str, Any], section: str, label: str, facts: dict[str, str],
           where: str) -> list[NamedRule]:
    out = []
    for i, r in enumerate(_list(rules, section, where)):
        w = f"{where} [[rules.{section}]] #{i + 1}"
        out.append(NamedRule(_str(r, label, w), _cond(r.get("when"), facts, w)))
    return out


def _signal(s: dict[str, Any], w: str) -> Signal:
    t = _str(s, "type", w)
    if t not in SIGNAL_KINDS:
        raise ProfileError(f"{w}: type must be one of {', '.join(SIGNAL_KINDS)}")
    name = _str(s, "name", w)
    if not name.isidentifier() or name.lower() != name:
        raise ProfileError(f"{w}: name '{name}' must be lower_snake_case")
    return Signal(name=name, type=t, description=_str(s, "description", w),
                  quality=bool(s.get("quality", False)),
                  column_cs=str(s["column_cs"]) if s.get("column_cs") else None)


def _market(key: str, v: dict[str, Any], w: str) -> Market:
    classes = [str(c) for c in v.get("legal_form_classes") or []]
    for c in classes:
        if c not in LEGAL_FORM_CLASSES:
            raise ProfileError(f"{w}: legal form class '{c}' unknown "
                               f"(use {', '.join(LEGAL_FORM_CLASSES)})")
    me = v.get("min_employees")
    return Market(key=key, source=_str(v, "source", w),
                  industry_codes=[str(c) for c in v.get("industry_codes") or []],
                  regions=[str(r) for r in v.get("regions") or []],
                  legal_form_classes=classes,
                  min_employees=int(me) if me is not None else None,
                  language=str(v["language"]) if v.get("language") else None)


def _offer(r: dict[str, Any], facts: dict[str, str], w: str) -> Offer:
    name = _str(r, "offer", w)
    when = _cond(r.get("when"), facts, w)
    size = r.get("size")
    if size is None:
        return Offer(name, when)
    if not isinstance(size, dict):
        raise ProfileError(f"{w}: size must be a table")
    basis = str(size.get("basis") or "")
    if basis == "revenue":
        return Offer(name, when, "revenue", _num_field(size, "min", w), _num_field(size, "max", w))
    if basis == "employees":
        return Offer(name, when, "employees", size_full_at=_num_field(size, "full_at", w))
    raise ProfileError(f"{w}: size.basis must be 'revenue' or 'employees'")


def _cond(raw: Any, facts: dict[str, str], w: str) -> Cond:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProfileError(f"{w}: `when` must be a table")
    out: Cond = {}
    for fact, ops in raw.items():
        if fact not in facts:
            raise ProfileError(f"{w}: unknown field '{fact}' in when "
                               f"(known: {', '.join(sorted(facts))})")
        if not isinstance(ops, dict) or not ops:
            raise ProfileError(f"{w}: '{fact}' needs operators, e.g. {{ min = 10 }}")
        allowed = OPS[facts[fact]]
        for op, val in ops.items():
            if op not in allowed:
                raise ProfileError(f"{w}: operator '{op}' not valid for {facts[fact]} field "
                                   f"'{fact}' (use {', '.join(sorted(allowed))})")
            if op in ("min", "max", "lt", "gt") and not isinstance(val, (int, float)):
                raise ProfileError(f"{w}: {fact}.{op} must be a number")
            if op in ("is", "known", "present") and not isinstance(val, bool):
                raise ProfileError(f"{w}: {fact}.{op} must be true or false")
            if op in ("in", "any", "none", "prefix") and not isinstance(val, list):
                raise ProfileError(f"{w}: {fact}.{op} must be a list")
        out[fact] = dict(ops)
    return out


def _example(e: dict[str, Any], w: str) -> Example:
    row = e.get("row")
    expect = e.get("expect")
    if not isinstance(row, dict) or not isinstance(expect, dict):
        raise ProfileError(f"{w}: needs `row` and `expect` tables")
    return Example(name=str(e.get("name") or w), row={k: str(v) for k, v in row.items()},
                   expect=dict(expect))


def _table(d: dict[str, Any], k: str, w: str, *, required: bool = True) -> dict[str, Any]:
    v = d.get(k)
    if v is None:
        if required:
            raise ProfileError(f"{w}: missing [{k}]")
        return {}
    if not isinstance(v, dict):
        raise ProfileError(f"{w}: [{k}] must be a table")
    return v


def _list(d: dict[str, Any], k: str, w: str) -> list[dict[str, Any]]:
    v = d.get(k) or []
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise ProfileError(f"{w}: {k} must be an array of tables ([[{k}]])")
    return v


def _str(d: dict[str, Any], k: str, w: str) -> str:
    v = d.get(k)
    if not isinstance(v, str) or not v.strip():
        raise ProfileError(f"{w}: `{k}` is required (text)")
    return v.strip()


def _int(d: dict[str, Any], k: str, w: str) -> int:
    v = d.get(k)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ProfileError(f"{w}: `{k}` is required (integer)")
    return v


def _num_field(d: dict[str, Any], k: str, w: str) -> float:
    v = d.get(k)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ProfileError(f"{w}: `{k}` is required (number)")
    return float(v)
