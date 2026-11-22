# grizly.py

Jeden soubor. Grizly analýza: **ARES → web firmy → Claude Code → CSV**.
Bez `pip install`, bez Dockeru, bez buildu. Python 3.11+ a `claude` v PATH.

## Než začneš utrácet

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 grizly.py selftest
```

`selftest` nic nestojí. Zkontroluje Python, `claude --version`, klíč, ARES,
HTML parser, pravidla a CSV s diakritikou. Když projde, má smysl pokračovat.

## Tři kroky

```bash
# 1) seznam firem z ARES (zdarma, bez klíče) — vlna 1 = velkoobchod + výroba
python3 grizly.py pull --kraj jihomoravsky,zlinsky --limit 100 --out grizly.csv

# 2) zkus na 3 firmách, jestli se weby najdou. Model se NEVOLÁ.
python3 grizly.py enrich --in grizly.csv --limit 3 --dry-run

# 3) ostrý běh
python3 grizly.py enrich --in grizly.csv --limit 20
```

Nebo obojí naráz:

```bash
python3 grizly.py all --kraj jihomoravsky --limit 100 --enrich-limit 20
```

Jedna firma při ladění:

```bash
python3 grizly.py probe --ico 25322214 --name "Holík International s.r.o." --city Zlín
```

## Co skript dělá

| Krok | Co | Cena |
|---|---|---|
| `pull` | ARES: NACE + právní forma, stránkované po 1 000 | zdarma |
| | RES: kategorie zaměstnanců | zdarma |
| | VR: aktivní jednatelé (bez `datumVymazu`) | zdarma |
| | odhad obratu z počtu lidí, s příznakem | zdarma |
| | pravidla: tagy, potřeby, jedna linie, skóre 0–100 | zdarma |
| `enrich` | najdi web → stáhni 6 stránek | zdarma |
| | `claude -p` se strict JSON schématem | ~$0,02–0,05/firma |
| | validace proti textu | zdarma |

## Tři věci, které skript záměrně nedělá

1. **Nedomýšlí si čísla.** Co ARES neví, je prázdné. Odhad obratu má vždy
   `obrat_odhad_priznak = odhad` a ve skóre dostane poloviční body. Pod 10
   zaměstnanců se odhad **nedělá** — je moc nejistý a mohl by firmu posunout
   nad práh, kam nepatří.

2. **Nevěří modelu.** `personalizace` bez **doslova opsané citace**, která se
   najde v textu stránky, se zahodí. Telefon a e-mail musí být v textu, jinak
   se kontakt zahodí. Vymyšlená chvála je horší než prázdné pole — Tomáš by
   poslal nesmysl a nikdo by si toho nevšiml.

3. **Nescrapuje LinkedIn** (podmínky služby, ban účtu) **ani vyhledávače**
   (DuckDuckGo zablokuje po pár dotazech, Bing vrací odkazy v jiném tvaru).
   Web se hledá hádáním domény z názvu + ověřením, že je na stránce název
   firmy. Volitelně přes `SERPER_API_KEY`.

## Výstup

`grizly.csv` — `;` + UTF-8 BOM, otevírá se v českém Excelu bez importu.
`contacts-grizly.csv` — max 3 kontakty na firmu, jen zveřejněné.

Sloupce, které rozhodují:

| Sloupec | Význam |
|---|---|
| `lead_offer` | `CRM` / `ERP` / `SECOND_BRAIN` / `NONE`. `NONE` = neoslovovat. |
| `skore` | 0–100, koho volat dřív |
| `tagy` | vylučovací: `SOFTWARE`, `POD_PRAHEM`, `NAD_STROPEM`, `VEREJNY_SEKTOR`… |
| `enrich_quality` | 0–1, jak moc je karta vyplněná |
| `enrich_error` | běh **selhal** (web nenalezen, Claude spadl) |
| `enrich_warnings` | doběhlo, ale něco se zahodilo (vymyšlená citace…) |

`enrich_error` a `enrich_warnings` jsou schválně dva sloupce. Když je sliješ
do jednoho, ve 100 řádcích nenajdeš skutečnou chybu.

## Náklady a jak je držet

- Strop **$0,15 na firmu** (`--max-budget-usd`), reálně $0,02–0,05.
- 100 firem ≈ **$2–5**.
- Cache v `grizly-cache/` — opakovaný běh na stejných datech nestojí nic.
  `--force` ji obejde.
- Průběžné ukládání po 10 firmách. Restart o nic nepřijde, hotové se přeskočí.
- `grizly-cost.jsonl` — po každé firmě řádek s cenou. `tail -5` a víš, kde jsi.

## Časté chyby

| Problém | Řešení |
|---|---|
| `claude` není v PATH | `npm i -g @anthropic-ai/claude-code` |
| `Failed to authenticate` | `ANTHROPIC_API_KEY` chybí nebo je propadlý. OAuth login v kontejneru nefunguje. |
| `web nenalezen` u půlky firem | Doplň `SERPER_API_KEY` (2 500 dotazů zdarma) nebo doplň `web` do CSV ručně. |
| Všechno `SECOND_BRAIN` | Chybí `obchodnici_odhad` a `obrat` → CRM/ERP nemůže projít. To je signál, že enrichment neproběhl, ne chyba pravidel. |
| `Neznámý kraj` | Použij `jihomoravsky`, `zlinsky`, `moravskoslezsky`, `olomoucky`, `vysocina`… |

## Co v tomhle souboru není

- **Účetní závěrky** (Sbírka listin) — jiná, dražší pipeline.
- **Google Sheets / Raynet** — výstup je CSV.
- **Plánované běhy** — spouští se ručně nebo z cronu.

## Konfigurace přes prostředí

| Proměnná | Default | Význam |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | povinné pro `enrich` |
| `SERPER_API_KEY` | — | volitelné, hledání webu přes Google |
| `GRIZLY_MODEL` | `sonnet` | model pro `claude -p` |
| `GRIZLY_WORKERS` | `3` | kolik firem paralelně |
| `GRIZLY_BUDGET` | `0.15` | strop v USD na firmu |
| `GRIZLY_CACHE` | `grizly-cache` | kde je cache |
| `GRIZLY_LOG` | `grizly-cost.jsonl` | kam logovat cenu |
