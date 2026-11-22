#!/usr/bin/env python3
"""grizly.py — Grizly analýza v jednom souboru. Bez pip install, jen Python 3.11+.

Co to dělá:
    ARES (seznam firem)  ->  web firmy  ->  claude -p (strict JSON)  ->  CSV

Proč jeden soubor: běží to na serveru, kde je jen `claude` a API klíč.
Žádné závislosti, žádný build, žádný Docker. Zkopíruj, spusť, dostaneš CSV.

Použití:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 grizly.py pull  --kraj jihomoravsky,zlinsky --limit 100
    python3 grizly.py enrich --in grizly.csv --limit 20
    python3 grizly.py all   --kraj jihomoravsky --limit 50

    # bez utrácení: jen stáhne weby a vypíše, co by poslal modelu
    python3 grizly.py enrich --in grizly.csv --limit 5 --dry-run

Co skript NEDĚLÁ (a je to záměr):
    - nedomýšlí si čísla: co ARES neví, je prázdné
    - nevěří modelu: personalizace bez doslova opsané citace se zahodí
    - nescrapuje LinkedIn (podmínky + ban účtu)
    - nečte účetní závěrky (to je jiná, dražší pipeline)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

VERSION = "1.0"

# =============================================================================
# KONFIGURACE
# =============================================================================

UA = "Mozilla/5.0 (compatible; PeakFlowLeadgen/1.0; +https://peakflow.cz)"
DELIMITER = ";"
ENCODING = "utf-8-sig"
ARES = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"

MAX_PAGE_CHARS = 12_000     # jedna stránka
MAX_TOTAL_CHARS = 40_000    # všechno dohromady (drží cenu)
MAX_PAGES = 6
FETCH_DELAY = 1.0           # slušnost mezi requesty na týž web
ARES_DELAY = 0.25

CACHE_DIR = Path(os.environ.get("GRIZLY_CACHE", "grizly-cache"))
LOG_FILE = Path(os.environ.get("GRIZLY_LOG", "grizly-cost.jsonl"))

MODEL = os.environ.get("GRIZLY_MODEL", "sonnet")
WORKERS = int(os.environ.get("GRIZLY_WORKERS", "3"))
BUDGET_PER_COMPANY = float(os.environ.get("GRIZLY_BUDGET", "0.15"))

# Cesty, které u české firmy nejčastěji nesou signál.
PATHS = ["/", "/o-nas", "/o-nas/", "/onas", "/kontakt", "/kontakt/",
         "/kariera", "/kariéra", "/kariera/", "/produkty", "/sluzby", "/služby"]

# Claude Code nesmí v tomhle běhu na nic šáhnout. Chceme jen text odpovědi.
# Záměrně jen kanonické názvy nástrojů — neznámý název by mohl shodit běh.
NO_TOOLS = ",".join([
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite",
])

# =============================================================================
# ČÍSELNÍKY — ověřené proti živému ARES (22. 11. 2026), nevyvozovat z hlavy
# =============================================================================

KRAJE = {
    19: "Hlavní město Praha", 27: "Středočeský kraj", 35: "Jihočeský kraj",
    43: "Plzeňský kraj", 51: "Karlovarský kraj", 60: "Ústecký kraj",
    78: "Liberecký kraj", 86: "Královéhradecký kraj", 94: "Pardubický kraj",
    108: "Kraj Vysočina", 116: "Jihomoravský kraj", 124: "Olomoucký kraj",
    132: "Moravskoslezský kraj", 141: "Zlínský kraj",
}

KATPO = {  # kód -> (label, min, max)
    "000": ("Neuvedeno", 0, None), "110": ("Bez zaměstnanců", 0, 0),
    "120": ("1–5 zaměstnanců", 1, 5), "130": ("6–9 zaměstnanců", 6, 9),
    "210": ("10–19 zaměstnanců", 10, 19), "220": ("20–24 zaměstnanců", 20, 24),
    "230": ("25–49 zaměstnanců", 25, 49), "240": ("50–99 zaměstnanců", 50, 99),
    "310": ("100–199 zaměstnanců", 100, 199), "320": ("200–249 zaměstnanců", 200, 249),
    "330": ("250–499 zaměstnanců", 250, 499), "340": ("500–999 zaměstnanců", 500, 999),
    "410": ("1000–1499 zaměstnanců", 1000, 1499),
    "420": ("1500–1999 zaměstnanců", 1500, 1999),
    "430": ("2000–2499 zaměstnanců", 2000, 2499),
    "440": ("2500–2999 zaměstnanců", 2500, 2999),
    "450": ("3000–3999 zaměstnanců", 3000, 3999),
    "460": ("4000–4999 zaměstnanců", 4000, 4999),
    "470": ("5000–9999 zaměstnanců", 5000, 9999),
    "510": ("10 000 a více zaměstnanců", 10000, None),
}

FORMY_FIRMY = ["112", "121"]  # s.r.o., a.s. — co nás zajímá
FORMA_FO = "101"
FORMA_VEREJNA = ("301", "331", "706")  # státní podnik, příspěvkovka, spolek

# Vlna 1: velkoobchod (46) + výroba (22, 25, 28) — §10 sales procesu
NACE_VLNA_1 = [
    "46110", "46120", "46130", "46140", "46150", "46160", "46170", "46180", "46190",
    "46210", "46220", "46230", "46240",
    "46310", "46320", "46330", "46340", "46350", "46360", "46370", "46380", "46390",
    "46410", "46420", "46430", "46440", "46450", "46460", "46470", "46480", "46490",
    "46500", "46610", "46620", "46630", "46640",
    "46710", "46720", "46730",
    "46810", "46820", "46830", "46840", "46850", "46860", "46870", "46890", "46900",
    "22290", "25610", "25620", "25710", "25990",
    "28110", "28150", "28410", "28990",
]

NACE_SOFTWARE = ("62", "631")
KRAJE_MORAVA = {108, 116, 124, 132, 141}

# Odhad obratu z počtu zaměstnanců, když výsledovka chybí (a chybí skoro vždy).
# Zdroj: medián tržeb na zaměstnance v ČR podle odvětví, zaokrouhleno dolů.
# Je to ODHAD — v CSV má vždy `obrat_odhad_priznak = odhad` a skóre za něj
# dostane jen polovinu bodů. Nikdy se nesmí vydávat za skutečnost.
TRZBY_NA_ZAMESTNANCE: dict[str, float] = {
    "velkoobchod": 12_000_000,   # NACE 46 — nízká marže, vysoký obrat
    "vyroba": 4_000_000,         # NACE 22, 25, 28
    "sluzby": 2_500_000,         # default, konzervativní
}


def odvetvi_skupina(nace: list[str]) -> str:
    """Zařadí NACE do skupiny pro odhad obratu."""
    if any(n.startswith("46") for n in nace):
        return "velkoobchod"
    if any(n.startswith(("22", "23", "24", "25", "26", "27", "28", "29", "30",
                         "31", "32", "33")) for n in nace):
        return "vyroba"
    return "sluzby"


def odhad_obratu(c: dict[str, Any]) -> tuple[int | None, str]:
    """Odhad ročních tržeb z kategorie zaměstnanců. (částka, příznak).

    Odhad se NEDĚLÁ u firem pod 10 zaměstnanců. Právě tam je nejvíc vedle
    (sedmičlenný velkoobchod může mít 30 i 300 mil.) a hrozí, že se firma
    dostane nad práh jen díky odhadu a Tomáš volá někomu, komu nemá co nabídnout.
    U otevřené horní hranice (250+) se taky nedělá — tam je firma nad stropem.
    """
    lo = _num(c.get("zamestnanci_min"))
    hi = _num(c.get("zamestnanci_max"))
    if lo is None or hi is None or hi == 0 or hi < TH["odhad_min_lide"]:
        return None, ""
    stred = (lo + hi) / 2
    nace = [n for n in str(c.get("nace") or "").split(";") if n]
    na_cloveka = TRZBY_NA_ZAMESTNANCE[odvetvi_skupina(nace)]
    return int(stred * na_cloveka), "odhad"

# Prahy a váhy — §3.1 a §3.3 sales procesu. Mění se tady, ne v logice.
TH = {
    "obrat_min_mil": 10.0, "obrat_max_mil": 1000.0,
    "lide_min": 5, "lide_max": 250,
    "crm_obchodnici": 3, "crm_obrat_mil": 10.0,
    "erp_obrat_mil": 30.0, "erp_lide": 20,
    "sb_lide": 10, "ai_let": 10, "ai_obrat_max_mil": 100.0,
    # Odhad obratu se nedělá pod tímhle počtem zaměstnanců — je moc nejistý
    # a mohl by firmu posunout nad práh, kam nepatří.
    "odhad_min_lide": 10,
}
W = {"velikost": 30, "signal": 25, "personalizace": 15,
     "kontakt": 15, "morava": 10, "bez_it": 5}

# =============================================================================
# ARES
# =============================================================================

def _http_json(url: str, payload: dict[str, Any] | None = None,
               timeout: int = 45, retries: int = 3) -> dict[str, Any]:
    """JSON z ARES. 400 = špatný dotaz, neopakovat. Ostatní 3× s backoffem."""
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"accept": "application/json"}
    if body:
        headers["Content-Type"] = "application/json"
    last: str = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())  # type: ignore[no-any-return]
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            if e.code == 400:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = {}
                raise AresError(parsed.get("popis", text[:200])) from e
            if e.code == 404:
                return {}
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise AresError(f"ARES neodpověděl po {retries} pokusech: {last}")


class AresError(RuntimeError):
    """Chyba, kterou nemá smysl opakovat (příliš široký dotaz apod.)."""


def ares_search_nace(nace: str, formy: list[str] | None = None) -> list[dict[str, Any]]:
    """Všechny subjekty s daným NACE, stránkované po 1 000.

    Ověřená omezení (22. 11. 2026):
      - `sidlo.kodKraje` v dotazu NEFILTRUJE (vrací celostátní počet)
      - víc NACE v jednom dotazu se sčítá a přeteče cap 1 000
      - cap je 1 000 na dotaz, ale `start` funguje správně
    """
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        payload: dict[str, Any] = {"czNace": [nace], "pocet": 1000, "start": start}
        if formy:
            payload["pravniFormaRos"] = formy
        res = _http_json(f"{ARES}/ekonomicke-subjekty/vyhledat", payload)
        batch = res.get("ekonomickeSubjekty") or []
        total = int(res.get("pocetCelkem") or 0)
        out.extend(batch)
        start += len(batch)
        if not batch or start >= total:
            break
        time.sleep(ARES_DELAY)
    return out


def ares_res(ico: str) -> dict[str, Any]:
    """RES — jediné místo, kde je kategorie počtu pracovníků."""
    return _http_json(f"{ARES}/ekonomicke-subjekty-res/{ico}")


def ares_vr(ico: str) -> dict[str, Any]:
    """Veřejný rejstřík — statutární orgány (jednatelé)."""
    return _http_json(f"{ARES}/ekonomicke-subjekty-vr/{ico}")


def _first(seq: Any) -> dict[str, Any]:
    return (seq or [{}])[0] if isinstance(seq, list) else {}


def _age(datum: str | None) -> int | None:
    if not datum:
        return None
    try:
        d = date.fromisoformat(datum[:10])
    except ValueError:
        return None
    return (date.today() - d).days // 365


def ares_company(record: dict[str, Any]) -> dict[str, Any]:
    """Záznam z vyhledávání → plochý dict pro CSV."""
    sidlo = record.get("sidlo") or {}
    return {
        "ico": record.get("ico", ""),
        "nazev": (record.get("obchodniJmeno") or "").strip(),
        "nace": ";".join(record.get("czNace") or record.get("czNace2008") or []),
        "kraj": sidlo.get("nazevKraje") or "",
        "mesto": sidlo.get("nazevObce") or "",
        "pravni_forma": record.get("pravniForma") or "",
        "datum_vzniku": (record.get("datumVzniku") or "")[:10],
        "vek_let": str(_age(record.get("datumVzniku")) or ""),
        "web": "",
        "jednatele": "",
        "zamestnanci_kat": "",
        "zamestnanci_min": "",
        "zamestnanci_max": "",
        "obrat": "",
        "obrat_rok": "",
        "obrat_odhad": "",
        "obrat_odhad_priznak": "",
        "obor": "",
        "popis_kratky": "",
        "personalizace": "",
        "fyzicke_zbozi": "",
        "eshop": "",
        "obchodnici_odhad": "",
        "crm_signal": "",
        "it_oddeleni": "",
        "ai_obsah": "",
        "tagy": "",
        "potreby": "",
        "lead_offer": "",
        "skore": "0",
        "enriched_dne": "",
        "enrich_quality": "",
        "enrich_warnings": "",
        "enrich_cost_usd": "",
        "enrich_error": "",
    }


def enrich_ares(ico: str) -> dict[str, str]:
    """Kategorie zaměstnanců + aktivní jednatelé. Dva callly na firmu."""
    out = {"zamestnanci_kat": "", "zamestnanci_min": "", "zamestnanci_max": "",
           "jednatele": ""}
    try:
        res = ares_res(ico)
    except AresError:
        res = {}
    kod = ((_first(res.get("zaznamy")).get("statistickeUdaje")) or {}).get(
        "kategoriePoctuPracovniku")
    if kod in KATPO:
        label, lo, hi = KATPO[kod]
        out["zamestnanci_kat"] = label
        out["zamestnanci_min"] = str(lo)
        out["zamestnanci_max"] = "" if hi is None else str(hi)

    try:
        vr = ares_vr(ico)
    except AresError:
        vr = {}
    jmena: list[str] = []
    for zaznam in vr.get("zaznamy") or []:
        for organ in zaznam.get("statutarniOrgany") or []:
            for clen in organ.get("clenoveOrganu") or []:
                if clen.get("datumVymazu"):
                    continue  # vymazaný člen — neaktuální
                osoba = clen.get("fyzickaOsoba") or {}
                casti = (osoba.get("titulPred"), osoba.get("jmeno"),
                         osoba.get("prijmeni"))
                jmeno = " ".join(p for p in casti if p).strip()
                if jmeno and jmeno not in jmena:
                    jmena.append(jmeno)
    out["jednatele"] = "; ".join(jmena)
    return out


# =============================================================================
# PRAVIDLA — tagy, potřeby, linie, skóre (§3 sales procesu)
# =============================================================================

TAG_VYLOUCOVACI = {"SOFTWARE", "AI_SOFTWARE", "POD_PRAHEM", "NAD_STROPEM",
                   "VEREJNY_SEKTOR", "FYZICKA_OSOBA", "KLIENT", "NEOSLOVOVAT"}


def _num(v: Any) -> float | None:
    """Prázdno → None. Nikdy 0, to by rozbilo prahy."""
    if v is None:
        return None
    s = str(v).strip().replace(" ", "").replace("\u00a0", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def classify(c: dict[str, Any]) -> dict[str, Any]:
    """Doplní tagy, potřeby, linii a skóre. Idempotentní."""
    obrat = _num(c.get("obrat"))
    obrat_odhad = _num(c.get("obrat_odhad"))
    # Skutečný obrat má přednost. Když není, pracuje se s odhadem — ale
    # `obrat_odhad_priznak` to v CSV přizná a skóre dá jen polovinu bodů.
    pouzity = obrat if obrat else obrat_odhad
    obrat_mil = pouzity / 1_000_000 if pouzity else None
    lide_min = _num(c.get("zamestnanci_min")) or 0
    lide_max = _num(c.get("zamestnanci_max")) or 0
    nace = [n for n in str(c.get("nace") or "").split(";") if n]
    forma = str(c.get("pravni_forma") or "")
    vek = _num(c.get("vek_let")) or 0
    tagy: set[str] = set()

    # --- vylučovací ------------------------------------------------------
    if forma == FORMA_FO:
        tagy.add("FYZICKA_OSOBA")
    if forma in FORMA_VEREJNA:
        tagy.add("VEREJNY_SEKTOR")
    if any(n.startswith(NACE_SOFTWARE) for n in nace):
        tagy.add("SOFTWARE")
    if obrat_mil is not None and obrat_mil < TH["obrat_min_mil"]:
        tagy.add("POD_PRAHEM")
    if obrat_mil is None and lide_max and lide_max <= TH["lide_min"]:
        tagy.add("POD_PRAHEM")
    if (obrat_mil is not None and obrat_mil > TH["obrat_max_mil"]) or (
            lide_min >= TH["lide_max"]):
        tagy.add("NAD_STROPEM")

    # --- potřeby ---------------------------------------------------------
    potreby: set[str] = set()
    obchodnici = _num(c.get("obchodnici_odhad")) or 0
    fyz = _tri(c.get("fyzicke_zbozi"))
    if obchodnici >= TH["crm_obchodnici"] and (obrat_mil or 0) >= TH["crm_obrat_mil"]:
        potreby.add("CRM")
    if ((obrat_mil or 0) >= TH["erp_obrat_mil"] and lide_min >= TH["erp_lide"]
            and fyz):
        potreby.add("ERP")
    if fyz:
        potreby.add("SKLAD")
    if (vek >= TH["ai_let"] and obrat_mil is not None
            and TH["obrat_min_mil"] <= obrat_mil <= TH["ai_obrat_max_mil"]
            and _tri(c.get("it_oddeleni")) is False
            and _tri(c.get("ai_obsah")) is False):
        potreby.add("AI_FIRST")

    # --- routing: jedna linie --------------------------------------------
    vylouceny = bool(tagy & TAG_VYLOUCOVACI)
    if vylouceny:
        offer = "NONE"
    elif "ERP" in potreby:
        offer = "ERP"
    elif "CRM" in potreby:
        offer = "CRM"
    elif lide_min >= TH["sb_lide"]:
        offer = "SECOND_BRAIN"
    else:
        offer = "NONE"

    # --- skóre -----------------------------------------------------------
    skore = 0
    # Velikost boduje jen u linií, které mají obratový práh (CRM, ERP).
    # SECOND_BRAIN se řídí počtem lidí, tam obrat nic neříká.
    if offer in ("CRM", "ERP") and obrat_mil is not None:
        lo = TH["erp_obrat_mil"] if offer == "ERP" else TH["crm_obrat_mil"]
        if lo <= obrat_mil <= TH["obrat_max_mil"]:
            skore += W["velikost"] // 2 if c.get("obrat_odhad_priznak") else W["velikost"]
    elif offer == "SECOND_BRAIN":
        # 10–19 lidí = 15 bodů, 100+ = plný počet.
        skore += min(W["velikost"], int(W["velikost"] * lide_min / 100))
    if c.get("personalizace"):
        skore += W["personalizace"]
    if c.get("jednatele"):
        skore += W["kontakt"]
    if _kraj_kod(str(c.get("kraj") or "")) in KRAJE_MORAVA:
        skore += W["morava"]
    if _tri(c.get("it_oddeleni")) is False:
        skore += W["bez_it"]

    c["tagy"] = ";".join(sorted(tagy))
    c["potreby"] = ";".join(sorted(potreby))
    c["lead_offer"] = offer
    c["skore"] = str(min(skore, 100))
    return c


def _tri(v: Any) -> bool | None:
    """CSV 'ano'/'ne'/'' → True/False/None."""
    s = str(v or "").strip().lower()
    if s in ("ano", "true", "1", "yes"):
        return True
    if s in ("ne", "false", "0", "no"):
        return False
    return None


def _kraj_kod(nazev: str) -> int | None:
    for kod, jmeno in KRAJE.items():
        if jmeno == nazev:
            return kod
    return None


# =============================================================================
# HTML → TEXT
# =============================================================================

class _Text(HTMLParser):
    """Zahodí skripty, styly, navigaci a patičku. Zbytek je text."""

    SKIP = {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK and self._skip == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - rozbité HTML není důvod shodit běh
        pass
    raw = "".join(p.parts)
    raw = re.sub(r"[ \t\u00a0]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def _ascii(s: str) -> str:
    """Diakritika pryč — `Holík` musí sedět na `holik.com`."""
    return s.translate(str.maketrans(
        "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ",
        "acdeeinorstuuyzACDEEINORSTUUYZ"))


# =============================================================================
# WEB
# =============================================================================

def _get(url: str, timeout: int = 20) -> str | None:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "cs,en;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return None
            charset = r.headers.get_content_charset() or "utf-8"
            return r.read(2_000_000).decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def _robots_ok(base: str, url: str) -> bool:
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(urllib.parse.urljoin(base, "/robots.txt"))
    try:
        rp.read()
        return rp.can_fetch(UA, url)
    except Exception:  # noqa: BLE001 - chybějící robots.txt není zákaz
        return True


def _normalize_base(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def candidate_domains(name: str) -> list[str]:
    """Poskládá pravděpodobné domény z názvu firmy.

    Deterministické, bez cizího API. Scraping vyhledávačů je křehký —
    DuckDuckGo zablokuje po pár dotazech, Bing vrací odkazy v jiném tvaru.
    """
    core = _ascii(name.lower())
    core = re.sub(r"\b(s\s?r\s?o|a\s?s|spol|v\s?o\s?s|druzstvo|holding|group|cz|sk|"
                  r"international|invest|servis|system|systems)\b", " ", core)
    tokens = [t for t in re.findall(r"[a-z0-9]+", core) if len(t) >= 2]
    if not tokens:
        return []
    long_tokens = [t for t in tokens if len(t) >= 3] or tokens
    stems: list[str] = []
    for stem in ("".join(tokens), "-".join(tokens), long_tokens[0],
                 "".join(long_tokens[:2])):
        if stem and len(stem) >= 3 and stem not in stems:
            stems.append(stem)
    return [f"https://{s}.{tld}" for s in stems for tld in ("cz", "com", "eu")]


def looks_like_company_page(html: str, name: str) -> bool:
    """Ověří, že stránka patří té firmě.

    Doména vracející 200 nic neznamená — parkovací stránky a překlepy taky
    vrací 200. Kontrolujeme, že se v textu potká výrazný token z názvu.
    """
    text = _ascii(html_to_text(html)[:20_000].lower())
    if any(s in text for s in ("parkovací stránka", "doména je na prodej",
                               "domain is for sale", "this domain", "parked")):
        return False
    tokens = [t for t in re.findall(r"[a-z0-9]+", _ascii(name.lower())) if len(t) >= 5]
    return bool(tokens) and any(t in text for t in tokens)


def _serper(name: str, city: str) -> str | None:
    """Volitelně Google přes Serper.dev (2 500 dotazů zdarma)."""
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        return None
    body = json.dumps({"q": f"{name} {city}".strip(), "gl": "cz", "hl": "cs"}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search", data=body,
        headers={"X-API-KEY": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    tokens = [t for t in re.findall(r"[a-z0-9]+", _ascii(name.lower())) if len(t) >= 5]
    skip = ("facebook", "linkedin", "wikipedia", "justice.cz", "ares.gov",
            "firmy.cz", "google", "youtube", "mapy.cz", "kurzy.cz")
    for item in (data.get("organic") or [])[:8]:
        host = urllib.parse.urlparse(str(item.get("link") or "")).netloc.lower()
        if not host or any(s in host for s in skip):
            continue
        if any(t in _ascii(host.replace("-", "").replace(".", "")) for t in tokens):
            return f"https://{host}"
    return None


def find_website(name: str, city: str = "", *, probe: bool = True) -> str | None:
    """Najde web firmy. Serper, jinak ověřené hádání domény. ARES web nevede."""
    found = _serper(name, city)
    if found:
        return found
    if not probe:
        return None
    for candidate in candidate_domains(name)[:9]:
        html = _get(candidate, timeout=10)
        if html and looks_like_company_page(html, name):
            return _normalize_base(candidate)
        time.sleep(0.3)
    return None


def _internal_links(base: str, html: str) -> list[str]:
    """Interní odkazy, prioritně ty se signálem (o nás, kontakt, kariéra…)."""
    host = urllib.parse.urlparse(base).netloc
    priority = ("o-nas", "onas", "o_nas", "kontakt", "kariera", "kariéra",
                "produkt", "sluzb", "služb", "sortiment", "nabidk", "nabídk",
                "o-firme", "tym", "tým", "kdo-jsme")
    skip_ext = (".pdf", ".jpg", ".jpeg", ".png", ".zip", ".doc", ".css", ".js",
                ".xml", ".ico", ".webp", ".svg", ".woff", ".gif", ".mp4")
    found: list[str] = []
    for m in re.finditer(r'href=["\']([^"\'#?]+)', html):
        href = m.group(1).strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        url = urllib.parse.urljoin(base, href)
        p = urllib.parse.urlparse(url)
        if p.netloc != host:
            continue
        clean = f"{p.scheme}://{p.netloc}{p.path}"
        if clean.rstrip("/") == base.rstrip("/") or clean in found:
            continue
        if any(x in clean.lower() for x in skip_ext):
            continue
        found.append(clean)
    hot = [u for u in found if any(k in u.lower() for k in priority)]
    return (hot + [u for u in found if u not in hot])[:20]


def gather_site(base_url: str, max_pages: int = MAX_PAGES) -> dict[str, str]:
    """Stáhne pár stránek. {url: text}. Prázdný dict = web nejde načíst.

    Duplicity se zahazují — SPA a Next.js weby vrací na každou cestu totéž
    a bez toho by se limit vyplýtval na jednu stránku šestkrát.
    """
    out: dict[str, str] = {}
    seen: set[str] = set()

    def take(url: str) -> bool:
        if len(out) >= max_pages or not _robots_ok(base_url, url):
            return False
        html = _get(url)
        if not html:
            return False
        head = html[:2000].lower()
        if "<html" not in head and "<body" not in head:
            return False  # statický asset servírovaný jako text/html
        text = html_to_text(html)
        if len(text) < 200:
            return False
        digest = hashlib.sha256(text[:4000].encode("utf-8", "replace")).hexdigest()
        if digest in seen:
            return False
        seen.add(digest)
        out[url] = text[:MAX_PAGE_CHARS]
        time.sleep(FETCH_DELAY)
        return True

    home = urllib.parse.urljoin(base_url, "/")
    if not take(home):
        return {}
    for url in _internal_links(base_url, _get(home) or ""):
        if len(out) >= max_pages:
            break
        take(url)
    for path in PATHS:
        if len(out) >= max_pages:
            break
        take(urllib.parse.urljoin(base_url, path))
    return out


# =============================================================================
# CLAUDE CODE
# =============================================================================

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["obor", "popis_kratky", "fyzicke_zbozi", "eshop",
                 "obchodnici_odhad", "crm_signal", "it_oddeleni", "ai_obsah",
                 "personalizace", "personalizace_quote", "kontakty"],
    "properties": {
        "obor": {"type": ["string", "null"],
                 "description": "Lidsky, 2–4 slova: 'velkoobchod stavebninami'."},
        "popis_kratky": {"type": ["string", "null"],
                         "description": "Jedna věta: co firma dělá a pro koho."},
        "fyzicke_zbozi": {"type": ["boolean", "null"]},
        "eshop": {"type": ["boolean", "null"]},
        "obchodnici_odhad": {"type": ["integer", "null"],
                             "description": "Kolik lidí dělá obchod."},
        "crm_signal": {"type": ["boolean", "null"]},
        "it_oddeleni": {"type": ["boolean", "null"]},
        "ai_obsah": {"type": ["boolean", "null"]},
        "personalizace": {"type": ["string", "null"],
                          "description": "Jedna konkrétní věta pro první řádek zprávy."},
        "personalizace_quote": {"type": ["string", "null"],
                                "description": "Doslova opsaná část textu, o kterou se "
                                               "personalizace opírá."},
        "kontakty": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["jmeno", "role", "telefon", "email"],
                "properties": {
                    "jmeno": {"type": ["string", "null"]},
                    "role": {"type": ["string", "null"]},
                    "telefon": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                },
            },
        },
    },
}

INSTRUCTION = """Jsi datový analytik. Z textu webu české firmy vytěž pole pro obchodní kartu.

Pravidla, která musíš dodržet:
1. Nic si nevymýšlej. Když to v textu není, vrať null. Null je správná odpověď.
2. `personalizace` smí být jen tehdy, když má v textu oporu. Musí jít o konkrétní
   věc (nová pobočka, nový produkt, inzerát, článek), ne o obecnou chválu typu
   "moderní firma" nebo "rodinná tradice".
3. `personalizace_quote` je DOSLOVA opsaná část textu, ze které personalizace
   vychází. Musí se dát v textu najít. Když cituješ, nic neupravuj.
4. `kontakty` jen zveřejněné pracovní kontakty. Žádné hádání e-mailů podle vzoru
   jmeno.prijmeni@. Když na webu není telefon, vrať null.
5. `obchodnici_odhad` = počet lidí v obchodu, které z webu vidíš. Ne počet
   zaměstnanců celkem.

Odpověz jen JSON podle schématu."""


class ClaudeResult:
    def __init__(self, payload: dict[str, Any] | None = None, cost_usd: float = 0.0,
                 error: str | None = None, raw: str = "") -> None:
        self.payload = payload or {}
        self.cost_usd = cost_usd
        self.error = error
        self.raw = raw


def _claude_bin() -> str:
    found = shutil.which("claude")
    if not found:
        raise RuntimeError(
            "`claude` není v PATH. Nainstaluj: npm i -g @anthropic-ai/claude-code\n"
            "a ověř, že máš ANTHROPIC_API_KEY."
        )
    return found


def _cache_path(company: dict[str, str], pages: dict[str, str]) -> Path:
    h = hashlib.sha256()
    h.update(str(company.get("ico", "")).encode())
    for url in sorted(pages):
        h.update(url.encode())
        h.update(pages[url][:4000].encode("utf-8", "replace"))
    return CACHE_DIR / f"{h.hexdigest()[:32]}.json"


def call_claude(company: dict[str, str], pages: dict[str, str], *,
                model: str = MODEL, timeout: int = 240,
                budget: float = BUDGET_PER_COMPANY) -> ClaudeResult:
    """Jeden `claude -p` běh. Text jde do stdin, instrukce jako argument."""
    blob = [f"# Firma: {company.get('nazev', '')} (IČO {company.get('ico', '')})"]
    if company.get("mesto"):
        blob.append(f"Sídlo: {company['mesto']}, {company.get('kraj', '')}")
    total = 0
    for url, text in pages.items():
        if total >= MAX_TOTAL_CHARS:
            break
        chunk = text[: MAX_TOTAL_CHARS - total]
        blob.append(f"\n\n## Stránka {url}\n{chunk}")
        total += len(chunk)

    cmd = [
        _claude_bin(), "-p", INSTRUCTION,
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA, ensure_ascii=False),
        "--model", model,
        "--max-budget-usd", f"{budget:.2f}",
        "--disallowed-tools", NO_TOOLS,
    ]
    env = dict(os.environ)
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")

    try:
        proc = subprocess.run(  # noqa: S603 - bin z shutil.which, bez shellu
            cmd, input="\n".join(blob), capture_output=True, text=True,
            timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return ClaudeResult(error=f"timeout {timeout}s")
    except OSError as e:
        return ClaudeResult(error=f"claude nešel spustit: {e}")

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "claude selhalo").strip()
        return ClaudeResult(error=msg[:500], raw=proc.stdout)

    try:
        env_json = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ClaudeResult(error="odpověď není JSON", raw=proc.stdout[:500])

    cost = float(env_json.get("total_cost_usd") or 0.0)
    if env_json.get("is_error"):
        return ClaudeResult(cost_usd=cost, raw=proc.stdout,
                            error=str(env_json.get("result"))[:300])

    payload = env_json.get("structured_output")
    if payload is None:
        result = env_json.get("result")
        if isinstance(result, dict):
            payload = result
        elif isinstance(result, str):
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                return ClaudeResult(cost_usd=cost, raw=proc.stdout[:500],
                                    error="result není JSON")
        else:
            return ClaudeResult(cost_usd=cost, raw=proc.stdout[:500],
                                error="chybí structured_output")
    return ClaudeResult(payload=payload, cost_usd=cost)


# =============================================================================
# VALIDACE — jediná obrana proti vymyšleným datům
# =============================================================================

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().replace("\u00a0", " ")).strip()


def validate(payload: dict[str, Any], pages: dict[str, str]) -> tuple[float, list[str]]:
    """Kontrola proti textu. (quality 0–1, seznam problémů).

    `personalizace` bez doslovné citace, která se v textu najde, se ZAHODÍ.
    Je to jediná věc, která rozhoduje, jestli se firma osloví — vymyšlená
    chvála znamená, že Tomáš pošle nesmysl a nikdo to nepozná.
    """
    problems: list[str] = []
    blob = _norm(" ".join(pages.values()))

    filled = 0
    keys = ("obor", "popis_kratky", "fyzicke_zbozi", "eshop",
            "obchodnici_odhad", "it_oddeleni")
    for k in keys:
        if payload.get(k) not in (None, ""):
            filled += 1
        else:
            problems.append(f"{k} chybí")

    personalizace = payload.get("personalizace")
    quote = payload.get("personalizace_quote")
    if personalizace and quote:
        q = _norm(str(quote))
        if len(q) >= 15 and q in blob:
            filled += 1
        else:
            problems.append("citace personalizace není v textu → personalizace zahozena")
            payload["personalizace"] = None
            payload["personalizace_quote"] = None
    elif personalizace:
        problems.append("personalizace bez citace → zahozena")
        payload["personalizace"] = None
    else:
        problems.append("personalizace chybí (firma se neosloví)")

    return filled / (len(keys) + 1), problems


def sanity_contacts(payload: dict[str, Any], pages: dict[str, str]) -> None:
    """Telefon a e-mail musí být v textu. Vymyšlený kontakt je horší než žádný."""
    blob = _norm(" ".join(pages.values()))
    digits = re.sub(r"\D", "", blob)
    kept = []
    for c in payload.get("kontakty") or []:
        tel = re.sub(r"\D", "", str(c.get("telefon") or ""))
        mail = str(c.get("email") or "").lower().strip()
        if tel and len(tel) >= 9 and tel[-9:] not in digits:
            continue
        if mail and mail not in blob:
            continue
        if not tel and not mail:
            continue
        kept.append(c)
    payload["kontakty"] = kept[:3]


# =============================================================================
# CSV
# =============================================================================

COLUMNS = [
    "ico", "nazev", "web", "nace", "obor", "kraj", "mesto", "pravni_forma",
    "datum_vzniku", "vek_let", "jednatele", "zamestnanci_kat", "zamestnanci_min",
    "zamestnanci_max", "obrat", "obrat_rok", "obrat_odhad", "obrat_odhad_priznak",
    "fyzicke_zbozi", "eshop", "obchodnici_odhad", "crm_signal",
    "it_oddeleni", "ai_obsah", "popis_kratky", "personalizace", "tagy", "potreby",
    "lead_offer", "skore", "enriched_dne", "enrich_quality", "enrich_warnings",
    "enrich_cost_usd", "enrich_error",
]

CONTACT_COLUMNS = ["ico", "nazev", "jmeno", "role", "telefon", "email", "source_url"]


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", newline="", encoding=ENCODING) as f:
        r = csv.DictReader(f, delimiter=DELIMITER)
        return list(r), list(r.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    """Atomický zápis — Coolify může kontejner kdykoli restartovat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding=ENCODING) as f:
        w = csv.DictWriter(f, fieldnames=columns, delimiter=DELIMITER,
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def log_cost(ico: str, cost: float, quality: float, note: str | None) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ico": ico, "cost_usd": round(cost, 5),
            "quality": round(quality, 2), "note": note,
        }, ensure_ascii=False) + "\n")


# =============================================================================
# KROKY
# =============================================================================

def enrich_one(company: dict[str, str], *, model: str, force: bool,
               use_search: bool, dry_run: bool) -> tuple[dict[str, str],
                                                          list[dict[str, str]]]:
    """Doplní jednu firmu. Nikdy nevyhodí výjimku — chyba jde do sloupce."""
    row = dict(company)
    ico = str(company.get("ico") or "")
    row["enriched_dne"] = date.today().isoformat()
    row["enrich_error"] = ""
    row.setdefault("enrich_quality", "")
    row.setdefault("enrich_warnings", "")
    row.setdefault("enrich_cost_usd", "")

    base = str(company.get("web") or "").strip()
    if not base and use_search:
        base = find_website(str(company.get("nazev") or ""),
                            str(company.get("mesto") or "")) or ""
    if not base:
        row["enrich_error"] = "web nenalezen"
        row["enrich_quality"] = "0.00"
        return row, []
    base = _normalize_base(base)
    row["web"] = base

    pages = gather_site(base)
    if not pages:
        row["enrich_error"] = "web nenačetl žádný text"
        row["enrich_quality"] = "0.00"
        return row, []

    if dry_run:
        print(f"    [dry-run] {ico} {base}: {len(pages)} stránek, "
              f"{sum(len(t) for t in pages.values())} znaků")
        for url, text in pages.items():
            print(f"      {url} — {len(text)} znaků")
        # Záměrně NEnastavujeme enrich_quality — jinak by řádek vypadal jako
        # hotový a další ostrý běh by ho přeskočil.
        row["enrich_quality"] = ""
        row["enrich_warnings"] = "dry-run: web nalezen, model nevolán"
        return row, []

    cache = _cache_path(company, pages)
    res: ClaudeResult | None = None
    if cache.exists() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            res = ClaudeResult(payload=data["payload"], cost_usd=data.get("cost", 0.0))
        except (json.JSONDecodeError, KeyError, TypeError):
            res = None  # poškozená cache = jako by nebyla
    if res is None:
        res = call_claude(company, pages, model=model)
        # Zapisujeme VŽDY, i při --force. Dřív se cache při --force
        # nepřepsala a příští běh by dostal starý výsledek.
        if not res.error:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"payload": res.payload, "cost": res.cost_usd},
                                        ensure_ascii=False), encoding="utf-8")

    if res.error:
        row["enrich_error"] = res.error
        row["enrich_quality"] = "0.00"
        log_cost(ico, res.cost_usd, 0.0, res.error)
        return row, []

    payload = res.payload
    sanity_contacts(payload, pages)
    quality, problems = validate(payload, pages)

    for col in ("obor", "popis_kratky", "personalizace"):
        if payload.get(col):
            row[col] = str(payload[col])
    for col in ("fyzicke_zbozi", "eshop", "crm_signal", "it_oddeleni", "ai_obsah"):
        val = payload.get(col)
        if isinstance(val, bool):
            row[col] = "ano" if val else "ne"
    if isinstance(payload.get("obchodnici_odhad"), int):
        row["obchodnici_odhad"] = str(payload["obchodnici_odhad"])

    row["enrich_quality"] = f"{quality:.2f}"
    row["enrich_cost_usd"] = f"{res.cost_usd:.4f}"
    if problems:
        # `enrich_error` = běh selhal, `enrich_warnings` = doběhlo s výhradou.
        # Míchat to znamená, že se ve 100 řádcích nedá najít skutečná chyba.
        row["enrich_warnings"] = "; ".join(problems)[:400]
    log_cost(ico, res.cost_usd, quality, row["enrich_warnings"] or None)

    contacts = []
    for c in payload.get("kontakty") or []:
        contacts.append({
            "ico": ico, "nazev": str(company.get("nazev") or ""),
            "jmeno": str(c.get("jmeno") or ""), "role": str(c.get("role") or ""),
            "telefon": str(c.get("telefon") or ""), "email": str(c.get("email") or ""),
            "source_url": base,
        })
    return row, contacts


def cmd_pull(args: argparse.Namespace) -> int:
    """ARES → seznam firem. Zdarma, bez klíče."""
    nace_list = _split(args.nace) or NACE_VLNA_1
    kraje = {k for k in (_kraj_kod(n) for n in _split_kraje(args.kraj)) if k}
    if args.kraj and not kraje:
        print(f"Neznámý kraj: {args.kraj}. Použij např. jihomoravsky, zlinsky, "
              f"moravskoslezsky.", file=sys.stderr)
        return 1
    limit = args.limit or 0

    companies: dict[str, dict[str, Any]] = {}
    for code in nace_list:
        try:
            found = ares_search_nace(code, FORMY_FIRMY)
        except AresError as e:
            print(f"  ! NACE {code}: {e}", file=sys.stderr)
            continue
        kept = 0
        for rec in found:
            if rec.get("datumZaniku"):
                continue
            if kraje and (rec.get("sidlo") or {}).get("kodKraje") not in kraje:
                continue
            c = ares_company(rec)
            if not c["ico"]:
                continue
            if c["ico"] in companies:
                companies[c["ico"]]["nace"] = ";".join(sorted(
                    set(str(companies[c["ico"]]["nace"]).split(";")) |
                    set(str(c["nace"]).split(";"))))
            else:
                companies[c["ico"]] = c
                kept += 1
        print(f"  NACE {code}: {len(found)} nalezeno, {kept} nových")
        if limit and len(companies) >= limit:
            break

    if not companies:
        print("Nic nenalezeno. Zkontroluj NACE a kraj.", file=sys.stderr)
        return 1

    selected = list(companies.values())[:limit] if limit else list(companies.values())

    if not args.no_detail:
        print(f"Obohacuji {len(selected)} firem z RES + VR…")
        for i, c in enumerate(selected, 1):
            c.update(enrich_ares(c["ico"]))
            if i % 25 == 0:
                print(f"  {i}/{len(selected)}")

    for c in selected:
        # Výsledovka chybí skoro vždy (malá s.r.o. ji nezveřejňuje), takže
        # bez odhadu by CRM/ERP linie nikdy neprošla. Odhad je přiznaný.
        if not c.get("obrat"):
            castka, priznak = odhad_obratu(c)
            if castka:
                c["obrat_odhad"] = str(castka)
                c["obrat_odhad_priznak"] = priznak
        classify(c)
    write_csv(Path(args.out), selected, COLUMNS)
    print(f"\n{len(selected)} firem → {args.out}")
    print("Dál: python3 grizly.py enrich --in " + args.out)
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    """Web + Claude. Tady se utrácí, proto `--limit` a `--dry-run`."""
    src = Path(args.input)
    if not src.exists():
        print(f"Chybí {src}. Nejdřív `grizly.py pull`.", file=sys.stderr)
        return 1
    rows, columns = read_csv(src)
    for col in COLUMNS:
        if col not in columns:
            columns.append(col)

    done: dict[str, dict[str, str]] = {}
    out_path = Path(args.output or args.input)
    # Resume se čte z cílového souboru, i když je to stejný soubor jako vstup.
    # Bez toho by každý běh začínal od nuly a `--limit` by pořád obohacoval
    # prvních N firem dokola.
    if out_path.exists() and not args.force:
        done_rows, _ = read_csv(out_path)
        # Neúspěchy (quality 0.00) se zkouší znovu — chybějící web není
        # důvod firmu zahodit navždy.
        done = {r["ico"]: r for r in done_rows
                if r.get("ico") and r.get("enrich_quality") not in (None, "", "0.00")}
        if done:
            print(f"Navazuji: {len(done)} firem už obohaceno.")

    todo = [r for r in rows if r.get("ico") and r["ico"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Není co dělat.")
        return 0

    mode = "DRY-RUN (nic se neposílá modelu)" if args.dry_run else f"model {args.model}"
    print(f"Obohacuji {len(todo)} firem, {mode}, {args.workers} paralelně\n")

    # Hotové řádky musí být ve `results` od začátku, jinak by je `_flush`
    # přepsal neobohacenou verzí ze vstupu a přišel bys o práci.
    results: dict[str, dict[str, str]] = dict(done)
    contacts: list[dict[str, str]] = []
    total_cost = 0.0
    ok = fail = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(enrich_one, c, model=args.model, force=args.force,
                        use_search=not args.no_web_search, dry_run=args.dry_run): c
            for c in todo
        }
        for i, fut in enumerate(as_completed(futures), 1):
            company = futures[fut]
            try:
                row, new_contacts = fut.result()
            except Exception as e:  # noqa: BLE001 - jeden rozbitý web nezabije dávku
                row = dict(company)
                row["enrich_error"] = f"neočekávaná chyba: {e}"[:400]
                row["enrich_quality"] = "0.00"
                new_contacts = []
            results[str(company["ico"])] = row
            contacts.extend(new_contacts)
            total_cost += _num(row.get("enrich_cost_usd")) or 0
            if row.get("enrich_error"):
                fail += 1
            elif row.get("enrich_quality"):
                ok += 1
            note = str(row.get("enrich_error") or row.get("enrich_warnings") or "")
            print(f"  [{i}/{len(todo)}] {str(company.get('nazev', ''))[:36]:36} "
                  f"q={row.get('enrich_quality') or '-':>7} {note[:56]}")
            if i % 10 == 0:
                _flush(rows, results, columns, out_path)

    _flush(rows, results, columns, out_path)
    if contacts and not args.dry_run:
        cpath = out_path.with_name("contacts-" + out_path.name)
        # Kontakty se připisují, ne přepisují — jinak by druhý běh smazal
        # kontakty z prvního.
        existing: list[dict[str, str]] = []
        if cpath.exists():
            existing, _ = read_csv(cpath)
        seen = {(r.get("ico"), r.get("telefon"), r.get("email")) for r in existing}
        for c in contacts:
            key = (c.get("ico"), c.get("telefon"), c.get("email"))
            if key not in seen:
                existing.append(c)
                seen.add(key)
        write_csv(cpath, existing, CONTACT_COLUMNS)
        print(f"kontakty → {cpath} ({len(existing)} celkem)")

    print(f"\nhotovo: {ok} obohaceno, {fail} s chybou, ${total_cost:.3f} → {out_path}")
    if total_cost and (ok + fail):
        print(f"cena na firmu: ${total_cost / (ok + fail):.3f}")
    return 0


def _flush(rows: list[dict[str, str]], results: dict[str, dict[str, str]],
           columns: list[str], out_path: Path) -> None:
    merged = [results.get(str(r.get("ico")), r) for r in rows]
    for r in merged:
        if not r.get("obrat") and not r.get("obrat_odhad"):
            castka, priznak = odhad_obratu(r)
            if castka:
                r["obrat_odhad"] = str(castka)
                r["obrat_odhad_priznak"] = priznak
        classify(r)
    write_csv(out_path, merged, columns)


def cmd_all(args: argparse.Namespace) -> int:
    """Pull + enrich v jednom kroku. Pro první běh na serveru."""
    args.out = args.out or "grizly.csv"
    rc = cmd_pull(args)
    if rc:
        return rc
    ns = argparse.Namespace(
        input=args.out, output=args.out, limit=args.enrich_limit,
        workers=args.workers, model=args.model, force=args.force,
        no_web_search=args.no_web_search, dry_run=args.dry_run)
    return cmd_enrich(ns)


def cmd_selftest(args: argparse.Namespace) -> int:
    """Ověří prostředí PŘED utrácením: Python, claude, klíč, ARES, HTML, pravidla.

    Nic nestojí. Když tohle projde, `enrich` má smysl pouštět.
    """
    ok = True

    def check(label: str, fn: Any) -> None:
        nonlocal ok
        try:
            detail = fn()
            print(f"  ✓ {label}: {detail}")
        except Exception as e:  # noqa: BLE001 - selftest má všechno vypsat
            ok = False
            print(f"  ✗ {label}: {e}")

    print(f"grizly {VERSION} — selftest\n")

    check("Python", lambda: f"{sys.version.split()[0]}")

    def _claude() -> str:
        path = _claude_bin()
        proc = subprocess.run([path, "--version"], capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "claude --version selhalo").strip()[:200])
        return f"{proc.stdout.strip()} ({path})"

    check("Claude Code", _claude)

    def _key() -> str:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY není nastavený")
        if not key.startswith("sk-"):
            raise RuntimeError("ANTHROPIC_API_KEY nevypadá jako klíč")
        return f"nastaven ({key[:7]}…{key[-4:]})"

    check("API klíč", _key)

    def _ares() -> str:
        res = _http_json(f"{ARES}/ekonomicke-subjekty/23903554")
        return f"{res.get('obchodniJmeno')} ({res.get('ico')})"

    check("ARES", _ares)

    def _html() -> str:
        text = html_to_text("<html><body><p>Řeřicha</p><script>x</script></body></html>")
        if "Řeřicha" not in text or "x" in text.replace("Řeřicha", ""):
            raise RuntimeError("parser nefunguje")
        return f"{len(text)} znaků, diakritika OK"

    check("HTML → text", _html)

    def _rules() -> str:
        c = {"ico": "1", "nazev": "T", "nace": "46110", "zamestnanci_min": "40",
             "zamestnanci_max": "99", "obrat_odhad": "504000000",
             "obrat_odhad_priznak": "odhad", "fyzicke_zbozi": "ano",
             "obchodnici_odhad": "5"}
        classify(c)
        if c["lead_offer"] != "ERP":
            raise RuntimeError(f"pravidla dala {c['lead_offer']}, čekal jsem ERP")
        return f"ERP routing OK, skóre {c['skore']}"

    check("Pravidla", _rules)

    def _write() -> str:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.csv"
            write_csv(p, [{"ico": "12345678", "nazev": "Žluťoučký"}], ["ico", "nazev"])
            rows, _ = read_csv(p)
            if rows[0]["nazev"] != "Žluťoučký":
                raise RuntimeError("CSV neudrží diakritiku")
            return "CSV roundtrip OK"

    check("CSV", _write)

    print()
    if ok:
        print("Vše v pořádku. Můžeš pustit:")
        print("  python3 grizly.py pull --kraj jihomoravsky --limit 20")
        print("  python3 grizly.py enrich --in grizly.csv --limit 3 --dry-run")
        return 0
    print("Něco chybí — oprav to, jinak `enrich` selže nebo utratí peníze naprázdno.")
    return 1


def cmd_probe(args: argparse.Namespace) -> int:
    """Jedna firma: ukaž web, stránky, co model vytěžil a proč."""
    company = {"ico": args.ico, "nazev": args.name, "mesto": args.city, "kraj": ""}
    base = args.web or find_website(args.name, args.city)
    print(f"web: {base}")
    if not base:
        return 1
    pages = gather_site(_normalize_base(base))
    print(f"stránek: {len(pages)}, znaků: {sum(len(t) for t in pages.values())}")
    for url, text in pages.items():
        print(f"  {url} ({len(text)})")
    if not pages:
        return 1
    if args.dry_run:
        print("\n[dry-run] model se nevolá.")
        return 0
    res = call_claude(company, pages, model=args.model, budget=0.5)
    if res.error:
        print(f"CHYBA: {res.error}", file=sys.stderr)
        return 1
    payload = res.payload
    sanity_contacts(payload, pages)
    quality, problems = validate(payload, pages)
    print("\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\nquality={quality:.2f} cost=${res.cost_usd:.4f}")
    for p in problems:
        print(f"  ! {p}")
    return 0


# =============================================================================
# CLI
# =============================================================================

def _split(v: str | None) -> list[str]:
    if not v:
        return []
    return [x.strip() for x in v.replace(",", " ").split() if x.strip()]


def _slug(name: str) -> str:
    return _ascii(name.strip().lower().replace("ě", "e"))


def _split_kraje(v: str | None) -> list[str]:
    """`jihomoravsky,zlinsky` → plné názvy krajů."""
    out = []
    for item in _split(v):
        slug = _slug(item)
        for jmeno in KRAJE.values():
            if slug in _slug(jmeno) or slug == _slug(jmeno).replace(" kraj", ""):
                out.append(jmeno)
                break
        else:
            out.append(item)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grizly.py",
        description="Grizly analýza: ARES → web → Claude → CSV. Jeden soubor, žádné pip.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""příklady:
  python3 grizly.py pull --kraj jihomoravsky,zlinsky --limit 100
  python3 grizly.py enrich --in grizly.csv --limit 20
  python3 grizly.py enrich --in grizly.csv --limit 5 --dry-run
  python3 grizly.py probe --ico 25322214 --name "Holík International s.r.o." --city Zlín
""")
    p.add_argument("--version", action="version", version=f"grizly {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_pull(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--nace", help="CZ-NACE čárkou, default = vlna 1")
        sp.add_argument("--kraj", help="kraje čárkou, např. jihomoravsky,zlinsky")
        sp.add_argument("--limit", type=int, default=0, help="max firem")
        sp.add_argument("--no-detail", action="store_true",
                        help="přeskočit RES + VR (rychlé, ale bez zaměstnanců)")
        sp.add_argument("--out", default="grizly.csv")

    def add_enrich(sp: argparse.ArgumentParser, *, limit_default: int = 0) -> None:
        sp.add_argument("--in", dest="input", default="grizly.csv")
        sp.add_argument("--out", dest="output", default=None,
                        help="default = přepíše vstup")
        sp.add_argument("--limit", type=int, default=limit_default)
        sp.add_argument("--workers", type=int, default=WORKERS)
        sp.add_argument("--model", default=MODEL)
        sp.add_argument("--force", action="store_true",
                        help="ignorovat cache i hotové řádky")
        sp.add_argument("--no-web-search", action="store_true",
                        help="web jen ze sloupce `web`, nehledat")
        sp.add_argument("--dry-run", action="store_true",
                        help="stáhni weby, ale nevolej model")

    sp_pull = sub.add_parser("pull", help="stáhni seznam firem z ARES")
    add_pull(sp_pull)
    sp_pull.set_defaults(func=cmd_pull)

    sp_enrich = sub.add_parser("enrich", help="obohať firmy z jejich webu")
    add_enrich(sp_enrich)
    sp_enrich.set_defaults(func=cmd_enrich)

    sp_all = sub.add_parser("all", help="pull + enrich")
    add_pull(sp_all)
    sp_all.add_argument("--enrich-limit", type=int, default=0,
                        help="kolik firem obohatit (0 = všechny)")
    sp_all.add_argument("--workers", type=int, default=WORKERS)
    sp_all.add_argument("--model", default=MODEL)
    sp_all.add_argument("--force", action="store_true")
    sp_all.add_argument("--no-web-search", action="store_true")
    sp_all.add_argument("--dry-run", action="store_true")
    sp_all.set_defaults(func=cmd_all)

    sp_selftest = sub.add_parser("selftest", help="ověř prostředí, nic nestojí")
    sp_selftest.set_defaults(func=cmd_selftest)

    sp_probe = sub.add_parser("probe", help="jedna firma, ukaž co model vytěžil")
    sp_probe.add_argument("--ico", required=True)
    sp_probe.add_argument("--name", required=True)
    sp_probe.add_argument("--city", default="")
    sp_probe.add_argument("--web", default="")
    sp_probe.add_argument("--model", default=MODEL)
    sp_probe.add_argument("--dry-run", action="store_true")
    sp_probe.set_defaults(func=cmd_probe)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nPřerušeno. Rozdělaná práce je v CSV.", file=sys.stderr)
        return 130
    except RuntimeError as e:
        print(f"CHYBA: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
