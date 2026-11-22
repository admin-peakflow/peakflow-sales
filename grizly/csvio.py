"""CSV in and out. Inside the program rows always use canonical English columns
and English values (`yes`/`no`, `estimate`). The profile decides the output
dialect: delimiter, encoding and English or legacy Czech headers.

Reading accepts both header sets and any of `,` `;` tab, so files from grizly
1.x, Excel and other tools load without conversion.
"""

from __future__ import annotations

import csv
from pathlib import Path

from grizly.profile import Profile
from grizly.schema import BOOL_WORDS, CS_COMPANY, CS_CONTACT, ESTIMATE_FLAG, ESTIMATE_WORDS
from grizly.util import tri

READ_ENCODING = "utf-8-sig"  # reads files with and without BOM


def _cs_map(profile: Profile, contacts: bool) -> dict[str, str]:
    if contacts:
        return dict(CS_CONTACT)
    m = dict(CS_COMPANY)
    for s in profile.signals:
        if s.column_cs:
            m[s.name] = s.column_cs
    return m


def _bool_columns(profile: Profile) -> set[str]:
    return {s.name for s in profile.signals if s.type == "boolean"}


def sniff_delimiter(first_line: str) -> str:
    counts = {d: first_line.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def read_raw(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Rows exactly as in the file (any delimiter, with or without BOM)."""
    with path.open("r", newline="", encoding=READ_ENCODING) as f:
        first = f.readline()
        f.seek(0)
        reader = csv.DictReader(f, delimiter=sniff_delimiter(first))
        rows = [{k: (v if v is not None else "") for k, v in r.items() if k is not None}
                for r in reader]
        return rows, list(reader.fieldnames or [])


def read_rows(path: Path, profile: Profile, *, contacts: bool = False
              ) -> tuple[list[dict[str, str]], list[str]]:
    """-> (rows with canonical keys and values, canonical column order of the file)."""
    raw_rows, header = read_raw(path)
    back = reverse_aliases(profile, contacts=contacts)
    return [canonical(r, profile, back) for r in raw_rows], [back.get(h, h) for h in header]


def reverse_aliases(profile: Profile, *, contacts: bool = False) -> dict[str, str]:
    return {v: k for k, v in _cs_map(profile, contacts).items()}


def canonical(raw: dict[str, str], profile: Profile, back: dict[str, str]) -> dict[str, str]:
    """Legacy headers -> canonical names, localized yes/no -> yes/no."""
    row = {back.get(h, h): v for h, v in raw.items()}
    for col in _bool_columns(profile):
        if row.get(col):
            t = tri(row[col])
            row[col] = "" if t is None else ("yes" if t else "no")
    if row.get("revenue_estimate_flag"):
        row["revenue_estimate_flag"] = ESTIMATE_FLAG
    return row


def write_rows(path: Path, rows: list[dict[str, str]], columns: list[str], profile: Profile,
               *, contacts: bool = False) -> None:
    """Atomic write: a container restart mid-write must not leave half a file."""
    lang = profile.output_columns
    names = _cs_map(profile, contacts) if lang == "cs" else {}
    yes, no = BOOL_WORDS[lang]
    bools = _bool_columns(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding=profile.encoding) as f:
        w = csv.writer(f, delimiter=profile.delimiter)
        w.writerow([names.get(c, c) for c in columns])
        for r in rows:
            out = []
            for c in columns:
                v = str(r.get(c) if r.get(c) is not None else "")
                if c in bools and v:
                    t = tri(v)
                    v = yes if t else no if t is False else ""
                elif c == "revenue_estimate_flag" and v:
                    v = ESTIMATE_WORDS[lang]
                out.append(v)
            w.writerow(out)
    tmp.replace(path)


def merge_columns(existing: list[str], required: list[str]) -> list[str]:
    """Keep the file's own column order and extra columns, append what is missing."""
    out = list(existing)
    for c in required:
        if c not in out:
            out.append(c)
    return out
