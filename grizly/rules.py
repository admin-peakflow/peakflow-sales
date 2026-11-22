"""Deterministic decisions: revenue estimate, tags, needs, one offer, score 0–100.

All thresholds and names come from the profile. Semantics that hold for every
profile:
- an unknown value never passes a comparison (`revenue >= X` is false when
  revenue is unknown) — missing data must not qualify or disqualify silently
- the estimate is only used when no published revenue exists, and it is
  always flagged; size points for an estimate are scaled by `estimate_factor`
- any exclusion tag => the profile's `no_offer`
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from grizly import csvio
from grizly.fx import FX
from grizly.profile import Cond, Offer, Profile
from grizly.schema import ESTIMATE_FLAG, FLAG_OPT_OUT, TAG_DO_NOT_CONTACT, key
from grizly.util import age_years, fmt_int, norm_code, num, split_cell, tri

# ------------------------------------------------------------------ revenue estimate


def estimate_revenue(row: dict[str, str], profile: Profile) -> tuple[float | None, str]:
    """Annual revenue from headcount band when no accounts are published.

    Returns (amount, currency). Not done:
    - below `estimate.min_employees` — a 7-person wholesaler can turn over 30 or
      300 million; the estimate would push it over a threshold it does not meet
    - for open-ended or unknown bands
    - for countries the profile has no per-employee figures for
    """
    lo, hi = num(row.get("employees_min")), num(row.get("employees_max"))
    if lo is None or hi is None or hi == 0 or hi < profile.estimate_min_employees:
        return None, ""
    country = str(row.get("country") or "").upper()
    candidates = [e for e in profile.estimates if e.country == country]
    if not candidates:
        return None, ""
    codes = [norm_code(c) for c in split_cell(row.get("industry_codes"))]
    chosen = None
    for e in candidates:  # first rule whose prefix matches ANY code wins; order = priority
        if e.prefixes and any(c.startswith(tuple(norm_code(p) for p in e.prefixes))
                              for c in codes):
            chosen = e
            break
    if chosen is None:
        chosen = next((e for e in candidates if not e.prefixes), None)
    if chosen is None:
        return None, ""
    return (lo + hi) / 2 * chosen.value, chosen.currency


def ensure_estimate(row: dict[str, str], profile: Profile) -> None:
    """Fill `revenue_estimate` once, only when no published revenue exists."""
    if num(row.get("revenue")) or num(row.get("revenue_estimate")):
        return
    amount, ccy = estimate_revenue(row, profile)
    if amount:
        row["revenue_estimate"] = fmt_int(amount)
        row["revenue_estimate_flag"] = ESTIMATE_FLAG
        row["revenue_currency"] = row.get("revenue_currency") or ccy


# ------------------------------------------------------------------ facts + conditions

def facts(row: dict[str, str], profile: Profile, fx: FX) -> dict[str, Any]:
    """Typed view of a row for rule evaluation. Money is in the profile currency."""
    ccy = str(row.get("revenue_currency") or profile.currency)
    actual = num(row.get("revenue")) or None          # 0 = not published
    estimate = num(row.get("revenue_estimate")) or None
    actual_conv = fx.convert(actual, ccy, profile.currency)
    estimate_conv = fx.convert(estimate, ccy, profile.currency)
    revenue = actual_conv if actual_conv is not None else estimate_conv

    age = num(row.get("company_age_years"))
    if age is None:
        a = age_years(row.get("founded_on"))
        age = float(a) if a is not None else None
    codes = split_cell(row.get("industry_codes"))
    code_count = num(row.get("industry_code_count"))
    if code_count is None and codes:
        code_count = float(len(codes))

    f: dict[str, Any] = {
        "revenue": revenue,
        "revenue_actual": actual_conv,
        "revenue_is_estimate": None if revenue is None else actual_conv is None,
        "employees_min": num(row.get("employees_min")),
        "employees_max": num(row.get("employees_max")),
        "company_age_years": age,
        "industry_code_count": code_count,
        "industry_codes": codes,
        "registry_flags": split_cell(row.get("registry_flags")),
        "tags": [],
        "needs": [],
        "lead_offer": None,
    }
    for name in ("country", "legal_form", "legal_form_class", "hq_region", "hq_region_code",
                 "hq_city", "source", "web", "name", "trade_name", "industry",
                 "short_description", "personalization", "officers"):
        v = str(row.get(name) or "").strip()
        f[name] = v or None
    for s in profile.signals:
        raw = row.get(s.name)
        if s.type == "boolean":
            f[s.name] = tri(raw)
        elif s.type in ("integer", "number"):
            f[s.name] = num(raw)
        else:
            f[s.name] = str(raw or "").strip() or None
    return f


def check(cond: Cond, f: dict[str, Any]) -> bool:
    """All fields must match (AND). An empty condition matches everything."""
    return all(_check_field(f.get(field), ops) for field, ops in cond.items())


def _check_field(v: Any, ops: dict[str, Any]) -> bool:
    return all(_op(v, op, target) for op, target in ops.items())


def _op(v: Any, op: str, target: Any) -> bool:
    if op == "known":
        return (v is not None and v != []) is target
    if op == "present":
        return bool(v) is target
    if op == "is":
        return v is target
    if op in ("min", "max", "lt", "gt"):
        if v is None:
            return False
        x, t = float(v), float(target)
        if op == "min":
            return x >= t
        if op == "max":
            return x <= t
        return x < t if op == "lt" else x > t
    if op == "eq":
        return v is not None and (v == target or str(v) == str(target))
    if op == "in":
        return v is not None and v in target
    if op == "any":
        return bool(set(_as_list(v)) & {str(t) for t in target})
    if op == "none":
        return not (set(_as_list(v)) & {str(t) for t in target})
    if op == "prefix":
        prefixes = tuple(norm_code(str(p)) for p in target)
        return any(norm_code(x).startswith(prefixes) for x in _as_list(v))
    raise ValueError(f"unknown operator {op}")  # profile validation prevents this


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


# ------------------------------------------------------------------ classify

class Suppressor:
    """Do-not-contact lists from the profile (`[[suppression]]`): CSV with a company id."""

    def __init__(self, profile: Profile) -> None:
        self.by_tag: dict[str, set[tuple[str, str]]] = {}
        self.missing: list[Path] = []
        for s in profile.suppression:
            if not s.file.exists():
                self.missing.append(s.file)
                continue
            rows, _ = csvio.read_rows(s.file, profile)
            ids = self.by_tag.setdefault(s.tag, set())
            for r in rows:
                k = key(r)
                if k[1]:
                    ids.add(k)
                    ids.add(("", k[1]))  # lists without a country column match any country

    def tags_for(self, row: dict[str, str]) -> Iterable[str]:
        k = key(row)
        for tag, ids in self.by_tag.items():
            if k in ids or ("", k[1]) in ids:
                yield tag


def classify(row: dict[str, str], profile: Profile, fx: FX,
             suppressor: Suppressor | None = None) -> dict[str, str]:
    """Fill tags, needs, lead_offer and score. Idempotent: recomputed from data."""
    f = facts(row, profile, fx)

    tags: set[str] = set()
    for t in profile.tags:
        if check(t.when, f):
            tags.add(t.name)
    if FLAG_OPT_OUT in f["registry_flags"]:
        tags.add(TAG_DO_NOT_CONTACT)
    if suppressor:
        tags.update(suppressor.tags_for(row))
    f["tags"] = sorted(tags)

    needs: set[str] = set()
    for n in profile.needs:
        if check(n.when, f):
            needs.add(n.name)
    f["needs"] = sorted(needs)

    offer: Offer | None = None
    if not tags & profile.exclude_tags:
        offer = next((o for o in profile.offers if check(o.when, f)), None)
    f["lead_offer"] = offer.name if offer else profile.no_offer

    row["tags"] = ";".join(f["tags"])
    row["needs"] = ";".join(f["needs"])
    row["lead_offer"] = f["lead_offer"]
    row["score"] = str(score(f, offer, profile))
    return row


def score(f: dict[str, Any], offer: Offer | None, profile: Profile) -> int:
    total = 0
    if offer and offer.size_basis == "revenue" and f["revenue"] is not None:
        lo = offer.size_min if offer.size_min is not None else float("-inf")
        hi = offer.size_max if offer.size_max is not None else float("inf")
        if lo <= f["revenue"] <= hi:
            total += (int(profile.size_points * profile.estimate_factor)
                      if f["revenue_is_estimate"] else profile.size_points)
    elif offer and offer.size_basis == "employees" and offer.size_full_at:
        people = f["employees_min"] or 0
        total += min(profile.size_points, int(profile.size_points * people / offer.size_full_at))
    for points, cond in profile.score_points:
        if check(cond, f):
            total += points
    return max(0, min(total, profile.score_cap))
