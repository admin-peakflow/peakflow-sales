"""The only defence against invented data: check the model's answer against the text.

- `personalization` without a verbatim quote that is found in a page is DROPPED.
  It decides whether a company is contacted at all; invented praise means a
  salesperson sends nonsense and nobody notices.
- phone numbers and e-mails must appear in the page text, else the contact goes.
"""

from __future__ import annotations

import re
from typing import Any

from grizly.profile import Profile
from grizly.util import norm_space

MIN_QUOTE = 15
MIN_PHONE_DIGITS = 8
_PHONE_RUN = re.compile(r"\+?\d[\d\s().\-/]{6,}\d")


def validate(payload: dict[str, Any], pages: dict[str, str], profile: Profile
             ) -> tuple[float, list[str], str]:
    """-> (quality 0–1, problems, URL of the page the personalization quote is on)."""
    problems: list[str] = []
    keys = ["industry", "short_description"] + [s.name for s in profile.signals if s.quality]
    filled = 0
    for k in keys:
        if payload.get(k) not in (None, ""):
            filled += 1
        else:
            problems.append(f"{k} missing")

    source_url = ""
    personalization = payload.get("personalization")
    quote = payload.get("personalization_quote")
    if personalization and quote:
        q = norm_space(str(quote))
        hit = next((url for url, text in pages.items() if q in norm_space(text)), None)
        if len(q) >= MIN_QUOTE and hit:
            filled += 1
            source_url = hit
        else:
            problems.append("personalization quote not in page text -> personalization dropped")
            payload["personalization"] = None
            payload["personalization_quote"] = None
    elif personalization:
        problems.append("personalization without quote -> dropped")
        payload["personalization"] = None
    else:
        problems.append("personalization missing (company will not be contacted)")

    return filled / (len(keys) + 1), problems, source_url


def page_phones(pages: dict[str, str]) -> list[str]:
    return [re.sub(r"\D", "", m.group(0)) for text in pages.values()
            for m in _PHONE_RUN.finditer(text)]


def phone_on_page(tel: str, phones: list[str]) -> bool:
    """Compare the national part: the model may add or drop a country prefix.

    The last min(9, len) digits must match the end of a number on the page, or
    sit inside a longer digit run (two numbers written without a separator).
    """
    for p in phones:
        n = min(9, len(p), len(tel))
        if n >= MIN_PHONE_DIGITS and p[-n:] == tel[-n:]:
            return True
        tail = tel[-min(9, len(tel)):]
        if len(tail) >= MIN_PHONE_DIGITS and tail in p:
            return True
    return False


def sanity_contacts(payload: dict[str, Any], pages: dict[str, str]) -> None:
    """Phone and e-mail must be in the text. An invented contact is worse than none."""
    blob = norm_space(" ".join(pages.values()))
    phones = page_phones(pages)
    kept = []
    for c in payload.get("contacts") or []:
        if not isinstance(c, dict):
            continue
        tel = re.sub(r"\D", "", str(c.get("phone") or ""))
        mail = str(c.get("email") or "").lower().strip()
        if tel:
            if len(tel) < MIN_PHONE_DIGITS:
                c["phone"] = None
                tel = ""
            elif not phone_on_page(tel, phones):
                continue
        if mail and mail not in blob:
            continue
        if not tel and not mail:
            continue
        kept.append(c)
    payload["contacts"] = kept[:3]
