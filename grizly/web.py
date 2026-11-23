"""Find a company's website and pull a few text pages from it. Any country.

What it deliberately does not do:
- scrape search engines (DuckDuckGo blocks after ~5 queries, Bing returns links
  in a different shape). The site is found by guessing domains from the name
  and verifying the company name appears on the page; Serper.dev optionally.
- scrape LinkedIn (terms of service, account bans)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from typing import ClassVar

from grizly.util import fold

MAX_PAGE_CHARS = 12_000
MAX_PAGES = 6
FETCH_DELAY = 1.0  # politeness between requests to the same site

# Paths that usually carry signal, by website language. English is always tried too.
LANG_PATHS: dict[str, list[str]] = {
    "cs": ["/o-nas", "/onas", "/kontakt", "/kontakty", "/kariera", "/produkty", "/sluzby"],
    "sk": ["/o-nas", "/kontakt", "/kariera", "/produkty", "/sluzby"],
    "no": ["/om-oss", "/kontakt", "/kontakt-oss", "/karriere", "/jobb", "/produkter",
           "/tjenester"],
    "fr": ["/a-propos", "/qui-sommes-nous", "/contact", "/nous-contacter", "/carrieres",
           "/recrutement", "/produits", "/services"],
    "de": ["/ueber-uns", "/uber-uns", "/kontakt", "/karriere", "/jobs", "/produkte",
           "/leistungen", "/impressum"],
    "pl": ["/o-nas", "/kontakt", "/kariera", "/produkty", "/uslugi"],
    "en": ["/about", "/about-us", "/contact", "/contact-us", "/careers", "/jobs", "/products",
           "/services", "/team"],
}
# Link keywords (diacritics-free, matched against folded URL).
LINK_HINTS = (
    "o-nas", "onas", "o_nas", "kontakt", "kariera", "produkt", "sluzb", "sortiment", "nabidk",
    "o-firme", "tym", "kdo-jsme", "about", "contact", "career", "job", "product", "service",
    "team", "om-oss", "karriere", "jobb", "tjenest", "propos", "qui-sommes", "carriere",
    "recrutement", "equipe", "ueber", "uber-uns", "leistung", "impressum", "uslugi",
)
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".zip", ".doc", ".css", ".js", ".xml", ".ico",
            ".webp", ".svg", ".woff", ".gif", ".mp4")
PARKED = ("parkovaci stranka", "domena je na prodej", "domain is for sale", "this domain",
          "parked", "domenet er til salgs", "ce domaine est a vendre", "domain steht zum verkauf",
          "domain kaufen", "buy this domain")
# Legal-form words and generic words removed before guessing a domain.
NAME_NOISE = re.compile(
    r"\b(s\s?r\s?o|a\s?s|spol|v\s?o\s?s|k\s?s|druzstvo|asa|ans|da|enk|sa|sas|sasu|sarl|eurl|"
    r"sci|snc|gmbh|ag|kg|ug|ohg|mbh|ltd|limited|plc|llp|llc|inc|corp|co|bv|nv|oy|oyj|ab|aps|"
    r"srl|spa|sl|sp\s?z\s?o\s?o|kft|zrt|holding|group|groupe|international|invest|servis|"
    r"system|systems|cz|sk|no|fr|de|eu|norge|france)\b")
DIRECTORY_HOSTS = (
    "facebook", "linkedin", "wikipedia", "google", "youtube", "instagram", "twitter", "x.com",
    "justice.cz", "ares.gov", "firmy.cz", "mapy.cz", "kurzy.cz", "proff.no", "brreg.no",
    "gulesider", "purehelp", "societe.com", "pappers", "infogreffe", "annuaire-entreprises",
    "verif.com", "companieshouse", "yelp", "kompass", "dnb.com", "opencorporates", "northdata",
    # Czech registry mirrors and job boards that Google ranks for company names
    "finmag.cz", "chytryrejstrik", "fakturujzdarma", "dostartu", "jenprace", "rejstrik",
    "podnikatel.cz", "infobel", "zivefirmy", "najisto", "obchodni-rejstrik", "seznam.cz",
    "hlidacstatu", "detail.cz", "imon.cz", "edb.cz", "merk.cz", "cylex", "zlatestranky",
    "firmyvdosahu", "expanzo",
)
COUNTRY_TLDS: dict[str, tuple[str, ...]] = {
    "CZ": ("cz", "com", "eu"), "SK": ("sk", "com", "eu"), "NO": ("no", "com"),
    "FR": ("fr", "com", "eu"), "DE": ("de", "com", "eu"), "AT": ("at", "com", "eu"),
    "PL": ("pl", "com", "eu"), "GB": ("co.uk", "com", "uk"), "US": ("com",),
}


def tlds_for(country: str) -> tuple[str, ...]:
    c = country.upper()
    return COUNTRY_TLDS.get(c, (c.lower(), "com") if c else ("com",))


# --------------------------------------------------------------------------- HTML -> text

class _Text(HTMLParser):
    """Drops scripts, styles, navigation and footer. The rest is text."""

    SKIP: ClassVar[frozenset[str]] = frozenset(
        {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"})
    BLOCK: ClassVar[frozenset[str]] = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"})

    CHROME: ClassVar[frozenset[str]] = frozenset({"nav", "footer", "header"})

    def __init__(self, keep_chrome: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self.skip = self.SKIP - self.CHROME if keep_chrome else self.SKIP

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.skip:
            self._skip += 1
        elif tag in self.BLOCK and self._skip == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.skip and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self.parts.append(data)


def html_to_text(html: str, *, keep_chrome: bool = False) -> str:
    """Page text. `keep_chrome` keeps header/footer/nav — that is where the company id
    and address usually are, so the site audit needs them; extraction does not."""
    p = _Text(keep_chrome)
    with contextlib.suppress(Exception):  # broken HTML is no reason to stop a batch
        p.feed(html)
    raw = "".join(p.parts)
    raw = re.sub("[ \t\u00a0]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


# --------------------------------------------------------------------------- fetching

class Fetcher:
    def __init__(self, user_agent: str, language: str = "en") -> None:
        self.user_agent = user_agent
        self.language = language

    def get(self, url: str, timeout: int = 20) -> str | None:
        headers = {"User-Agent": self.user_agent,
                   "Accept-Language": f"{self.language},en;q=0.5"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = r.headers.get("Content-Type", "")
                if "html" not in ctype and "text" not in ctype:
                    return None
                charset = r.headers.get_content_charset() or "utf-8"
                return str(r.read(2_000_000).decode(charset, errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, LookupError):
            return None

    def robots(self, base: str) -> urllib.robotparser.RobotFileParser | None:
        """Parsed robots.txt, fetched with our UA. Missing or unreadable = allowed."""
        text = self.get(urllib.parse.urljoin(base, "/robots.txt"), timeout=10)
        if not text:
            return None
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        return rp


def normalize_base(url: str) -> str:
    url = re.sub(r"[\x00-\x20\x7f]+", "", url)  # "URL can't contain control characters"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def name_tokens(name: str, min_len: int) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", fold(name.lower())) if len(t) >= min_len]


def candidate_domains(name: str, tlds: tuple[str, ...]) -> list[str]:
    """Likely domains from a company name. Deterministic, no third-party API."""
    core = fold(name.lower()).replace(".", " ").replace(",", " ")
    core = NAME_NOISE.sub(" ", core)
    tokens = [t for t in re.findall(r"[a-z0-9]+", core) if len(t) >= 2]
    if not tokens:
        return []
    long_tokens = [t for t in tokens if len(t) >= 3] or tokens
    stems: list[str] = []
    for stem in ("".join(tokens), "-".join(tokens), long_tokens[0], "".join(long_tokens[:2])):
        if stem and len(stem) >= 3 and stem not in stems:
            stems.append(stem)
    # National domain first for EVERY name variant, then .com, then .eu: a Czech
    # firm's own site is far more likely `xyz.cz` than `xyz.com` (often a namesake).
    return [f"https://{s}.{tld}" for tld in tlds for s in stems]


def distinctive_tokens(name: str) -> list[str]:
    """Name words that identify the company: no legal form, no generic words, 5+ chars."""
    core = NAME_NOISE.sub(" ", fold(name.lower()).replace(".", " ").replace(",", " "))
    return list(dict.fromkeys(t for t in re.findall(r"[a-z0-9]+", core) if len(t) >= 5))


def looks_like_company_page(html: str, name: str) -> bool:
    """A 200 means nothing (parking pages, typos, namesakes).

    Two distinctive name words must appear (one if the name has only one):
    `happy.cz` mentions "happy" but is not HAPPY LYFE THERAPEUTICS. A wrong site
    is worse than none — the personalization would describe another company.
    """
    text = fold(html_to_text(html)[:20_000].lower())
    if any(s in text for s in PARKED):
        return False
    tokens = distinctive_tokens(name) or name_tokens(name, 5)
    if not tokens:
        return False
    return sum(t in text for t in tokens) >= min(2, len(tokens))


_warned: set[str] = set()


def _warn_once(msg: str) -> None:
    """A dead search key must not look like "no website" on every row."""
    if msg not in _warned:
        _warned.add(msg)
        print(f"  ! {msg}", file=sys.stderr)


def serper(name: str, city: str, country: str, language: str) -> list[str]:
    """Optional Google search via Serper.dev (free tier). Needs SERPER_API_KEY.
    Non-directory result sites, name-like domains first."""
    # `docker run --env-file` keeps quotes as part of the value (compose strips
    # them): a quoted key gets 403 on every query and every site looks "not found".
    key = (os.environ.get("SERPER_API_KEY") or "").strip().strip("\"'")
    if not key:
        return []
    body = json.dumps({"q": f"{name} {city}".strip(), "gl": country.lower() or "us",
                       "hl": language}).encode()
    req = urllib.request.Request("https://google.serper.dev/search", data=body,
                                 headers={"X-API-KEY": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        _warn_once(f"Serper search failed: HTTP {e.code} — check SERPER_API_KEY / credits; "
                   "falling back to guessed domains")
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    hits: list[str] = []
    for item in (data.get("organic") or [])[:8]:
        host = urllib.parse.urlparse(str(item.get("link") or "")).netloc.lower()
        if not host or any(s in host for s in DIRECTORY_HOSTS) or host in hits:
            continue
        hits.append(host)
    # The domain need not contain the name (lesamh.cz, a brand, a shortening): the
    # site audit decides. Name-like domains go first; the rest stay in Google order.
    hits.sort(key=lambda h: not domain_matches_name(h, name))
    return [f"https://{h}" for h in hits]


def domain_matches_name(url_or_host: str, name: str) -> bool:
    """Loose: the domain contains a 4+ letter name word, all name words joined
    (`LESA - MH` -> lesamh), or is a 4+ letter shortening of them (`danlux` ->
    DANLUX s.r.o., `holik` -> Holík International)."""
    host = urllib.parse.urlparse(url_or_host).netloc or url_or_host
    host = re.sub(r"^www\.", "", host.lower()).rsplit(".", 1)[0]
    compact = fold(host).replace("-", "").replace(".", "")
    core = NAME_NOISE.sub(" ", fold(name.lower()).replace(".", " ").replace(",", " "))
    tokens = re.findall(r"[a-z0-9]+", core) or name_tokens(name, 1)
    joined = "".join(tokens)
    if not compact or not joined:
        return False
    return (any(len(t) >= 4 and t in compact for t in tokens) or joined in compact
            or (len(compact) >= 4 and joined.startswith(compact)))


def website_candidates(fetcher: Fetcher, name: str, city: str, country: str,
                       limit: int = 3) -> list[str]:
    """Candidate sites, best first. Search hits are not name-filtered — the audit
    decides (see `pipeline.find_verified_site`); guessed domains must pass the name check."""
    out: list[str] = []
    for found in serper(name, city, country, fetcher.language):
        if len(out) >= limit:
            break
        out.append(normalize_base(found))
    for candidate in candidate_domains(name, tlds_for(country))[:12]:
        if len(out) >= limit:
            break
        base = normalize_base(candidate)
        if base in out:
            continue
        html = fetcher.get(candidate, timeout=10)
        if html and looks_like_company_page(html, name):
            out.append(base)
        time.sleep(0.3)
    return out


def find_website(fetcher: Fetcher, name: str, city: str, country: str) -> str | None:
    """First candidate (no audit). Kept for `probe --dry-run` style quick looks."""
    found = website_candidates(fetcher, name, city, country, limit=1)
    return found[0] if found else None


def internal_links(base: str, html: str) -> list[str]:
    """Same-host links, the ones with signal (about, contact, careers…) first."""
    host = urllib.parse.urlparse(base).netloc
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
        if any(x in clean.lower() for x in SKIP_EXT):
            continue
        found.append(clean)

    def folded(u: str) -> str:
        return fold(urllib.parse.unquote(u).lower())

    hot = [u for u in found if any(k in folded(u) for k in LINK_HINTS)]
    return (hot + [u for u in found if u not in hot])[:20]


def gather_site(fetcher: Fetcher, base_url: str, max_pages: int = MAX_PAGES,
                chrome: dict[str, str] | None = None) -> dict[str, str]:
    """A few pages as {url: text}. Empty = site unreadable.

    `chrome`, if given, receives the same pages WITH header/footer/nav text, for
    the site audit (company id, address).

    Duplicates are dropped: SPAs return the same page on every path, and without
    this the page budget would be spent on one page six times.
    """
    out: dict[str, str] = {}
    seen: set[str] = set()
    robots = fetcher.robots(base_url)

    def take(url: str, html: str | None = None) -> bool:
        if len(out) >= max_pages:
            return False
        if robots is not None and not robots.can_fetch(fetcher.user_agent, url):
            return False
        html = html if html is not None else fetcher.get(url)
        if not html:
            return False
        head = html[:2000].lower()
        if "<html" not in head and "<body" not in head:
            return False  # static asset served as text/html
        text = html_to_text(html)
        if len(text) < 200:
            return False
        digest = hashlib.sha256(text[:4000].encode("utf-8", "replace")).hexdigest()
        if digest in seen:
            return False
        seen.add(digest)
        out[url] = text[:MAX_PAGE_CHARS]
        if chrome is not None:
            chrome[url] = html_to_text(html, keep_chrome=True)[:MAX_PAGE_CHARS]
        time.sleep(FETCH_DELAY)
        return True

    home = urllib.parse.urljoin(base_url, "/")
    home_html = fetcher.get(home)
    if not take(home, home_html):
        return {}
    for url in internal_links(base_url, home_html or ""):
        if len(out) >= max_pages:
            break
        take(url)
    paths = LANG_PATHS.get(fetcher.language, []) + LANG_PATHS["en"]
    for path in dict.fromkeys(paths):
        if len(out) >= max_pages:
            break
        take(urllib.parse.urljoin(base_url, path))
    return out
