"""Currency conversion for thresholds. Rules compare money in the profile currency.

Rates are ECB reference rates (units per 1 EUR), fetched once a day and cached,
only when a conversion is actually needed. A profile can pin rates in
`[fx] rates` or forbid the network with `[fx] source = "static"`.
A missing rate means "unknown revenue", never a guessed one.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


class FX:
    def __init__(self, *, source: str, rates: dict[str, float], cache_dir: Path) -> None:
        self.source = source
        self.pinned = {k.upper(): v for k, v in rates.items()}
        self.cache_dir = cache_dir
        self._ecb: dict[str, float] | None = None
        self._lock = threading.Lock()
        self.warnings: list[str] = []

    def rate(self, ccy: str) -> float | None:
        """Units of `ccy` per 1 EUR."""
        ccy = ccy.upper()
        if ccy == "EUR":
            return self.pinned.get("EUR", 1.0)
        if ccy in self.pinned:
            return self.pinned[ccy]
        if self.source != "ecb":
            return None
        return self._ecb_rates().get(ccy)

    def convert(self, amount: float | None, src: str, dst: str) -> float | None:
        if amount is None:
            return None
        src, dst = (src or dst).upper(), dst.upper()
        if src == dst:
            return amount
        r_src, r_dst = self.rate(src), self.rate(dst)
        if not r_src or not r_dst:
            msg = f"no FX rate {src}->{dst}; revenue treated as unknown"
            if msg not in self.warnings:
                self.warnings.append(msg)
            return None
        return amount / r_src * r_dst

    def _ecb_rates(self) -> dict[str, float]:
        with self._lock:
            if self._ecb is not None:
                return self._ecb
            cache = self.cache_dir / f"fx-ecb-{date.today().isoformat()}.json"
            if cache.exists():
                try:
                    self._ecb = {k: float(v) for k, v in json.loads(cache.read_text()).items()}
                    return self._ecb
                except (json.JSONDecodeError, ValueError, AttributeError):
                    pass
            self._ecb = fetch_ecb()
            if self._ecb:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(self._ecb))
            return self._ecb


def fetch_ecb(timeout: int = 20) -> dict[str, float]:
    try:
        with urllib.request.urlopen(ECB_URL, timeout=timeout) as r:
            xml = r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return {}
    return parse_ecb(xml)


def parse_ecb(xml: str) -> dict[str, float]:
    return {m.group(1): float(m.group(2))
            for m in re.finditer(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([0-9.]+)['\"]", xml)}
