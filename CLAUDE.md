# grizly — kontext pro Claude

Jeden soubor: `grizly.py`. **ARES → web → Claude Code → CSV.** Bez závislostí.

Specifikace polí: `10 Sales/(C) Grizly analýza — specifikace dat`
Prahy a routing: `10 Sales/(C) Sales proces — ERP, CRM, Second Brain, automatizace` §3

## Pravidla, která se nesmí obejít

1. **Nic se nedomýšlí.** Co ARES neví, je prázdné. `obrat_odhad` má vždy
   `obrat_odhad_priznak = odhad` a ve skóre poloviční body.
2. **Odhad obratu se nedělá pod 10 zaměstnanců.** Sedmičlenný velkoobchod může
   mít 30 i 300 mil.; odhad by firmu posunul nad práh, kam nepatří.
3. **`personalizace` bez doslovné citace z textu se zahodí.** Vymyšlená chvála
   je horší než prázdné pole.
4. **`enrich_error` ≠ `enrich_warnings`.** Error = běh selhal, warnings = doběhlo
   s výhradou. Slít je znamená nenajít chybu ve 100 řádcích.
5. **`--dry-run` nesmí nastavit `enrich_quality`.** Jinak by řádek vypadal hotový
   a ostrý běh by ho přeskočil.

## Ověřená omezení ARES (22. 11. 2026, proti produkci)

| Věc | Chování |
|---|---|
| `sidlo.kodKraje` v dotazu | **NEFILTRUJE** — vrací celostátní počet. Filtruje se na klientovi. |
| `sidlo.textovaAdresa` | filtruje jako substring (`Brno` funguje) |
| Cap | 1 000 na dotaz, ale `start` funguje správně |
| Víc NACE v dotazu | sčítá se → přeteče cap. Dotazovat po jednom. |
| Kategorie zaměstnanců | jen v `/ekonomicke-subjekty-res/{ico}`, ne ve vyhledávání |
| Jednatelé | `/ekonomicke-subjekty-vr/{ico}`, aktivní = bez `datumVymazu` |
| NACE 2025 | `czNace` = 2025, `czNace2008` = starší. Kódy se liší (`62020` v 2025 není). |

## Co nefunguje a proč to tam není

- **Scraping vyhledávačů.** DuckDuckGo zablokuje po ~5 dotazech, Bing vrací
  odkazy v jiném tvaru než `uddg=`. Web se hledá hádáním domény + ověřením,
  že je na stránce název firmy. Volitelně Serper.dev.
- **OAuth login Claude v kontejneru.** Funguje jen `ANTHROPIC_API_KEY`.

## Struktura souboru

```
KONFIGURACE → ČÍSELNÍKY → ARES → PRAVIDLA → HTML→TEXT → WEB
→ CLAUDE CODE → VALIDACE → CSV → KROKY (pull/enrich/all/selftest/probe) → CLI
```

Sekce jsou oddělené komentářem s `=====`. Při úpravě drž pořadí — CLI na konci
volá funkce z předchozích sekcí.

## Test bez utrácení

`python3 grizly.py selftest` — Python, `claude --version`, klíč, ARES, HTML
parser, pravidla, CSV s diakritikou. Nic nestojí.
