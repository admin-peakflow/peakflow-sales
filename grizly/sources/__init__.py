"""Registry adapters. Add a country = add a module + one line in SOURCES."""

from __future__ import annotations

from grizly.sources.base import Query, Source, SourceError
from grizly.sources.cz_ares import AresSource
from grizly.sources.fr_sirene import SireneSource
from grizly.sources.no_brreg import BrregSource

SOURCES: dict[str, type[Source]] = {
    AresSource.name: AresSource,
    BrregSource.name: BrregSource,
    SireneSource.name: SireneSource,
}


def get(name: str, user_agent: str) -> Source:
    if name not in SOURCES:
        raise SourceError(f"unknown source '{name}'. Available: {', '.join(SOURCES)}")
    return SOURCES[name](user_agent)


def for_country(country: str, user_agent: str) -> Source | None:
    """Adapter for a country, used to normalize rows from files (no network)."""
    for cls in SOURCES.values():
        if cls.country == country.upper():
            return cls(user_agent)
    return None


__all__ = ["SOURCES", "Query", "Source", "SourceError", "for_country", "get"]
