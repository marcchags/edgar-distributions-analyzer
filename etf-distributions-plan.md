# ETF Capital Gain Distributions — Project Plan (steering file)

Status: planning complete, no code written. Written 2026-11-24 at the end of a research session
in the `edgar-etf-querier` repo. This file is meant to be moved into a **new, separate repo** and
read at the start of a fresh session. Everything a new session needs is here; the old repo is
reference only.

---

## 1. Objective

Given one or more ETF tickers, report what distributions each ETF paid over the last 5 fiscal
years and the **character** of each (net investment income, net realized gain, return of
capital), so the user can see whether an ETF distributed capital gains.

Key domain point from the user: a realized gain in the Statement of Operations does **not** mean
a capital gain was distributed (in-kind redemptions defer gains). Only look at **Distributions**.

Terminal-first tool. No UI.

## 2. Decisions already made by the user (do not re-litigate)

| Topic | Decision |
|---|---|
| Repo | New standalone repo, separate from `edgar-etf-querier` |
| EDGAR access | `edgartools` (imports as `edgar` — fine in a new repo; never name a local package `edgar`) |
| DataFrames | `polars` |
| Code style | As streamlined, efficient and short as possible |
| Scope levels | `etf` (the tickers passed) or `trust` (all ETFs under the same registrant CIK). **Issuer level dropped.** |
| Share class | ETF share class only |
| Primary source | N-CSR financial highlights table |
| Corroboration | N-CEN (structured XML) as a cross-check |
| Character detail | Per-share **book** character only. No tax-note parsing (no long-term vs ordinary split). |
| Converted funds | Include predecessor mutual-fund years, with a flag |
| Lookback | Hardcoded 5 fiscal years = the single most recent **annual** N-CSR covering the fund. No semiannual (N-CSRS) stub. No `years` parameter. |

## 3. Inputs and public API

Input `etfs` accepts any of:
- a ticker: `"TVAL"`
- a comma-separated string: `"TVAL,LVDS"`
- a path to a `.csv` with header `ETF`, one ticker per row

Functions (each callable on its own):
- `get_etf_ids(etfs) -> pl.DataFrame` — one row per requested ETF:
  `ticker, series_id, class_id, fund_name, trust_cik, trust_name`
- `get_etf_trust_ids(etfs) -> pl.DataFrame` — same columns, expanded to every **ETF** in each
  requested ticker's trust
- `get_distributions(etfs, scope="etf") -> pl.DataFrame` — `scope` is `"etf"` or `"trust"`;
  resolves IDs, pulls filings, parses, corroborates

Plus a thin CLI, e.g. `python -m <pkg> TVAL,LVDS --scope trust --out result.csv`.

### Proposed output (long format, one row per fund × fiscal period × character)

`ticker, series_id, class_id, fund_name, trust_cik, period_end, period_label, character,
per_share, raw_label, accession_no, filing_date, source_url, parse_status, nav_check_ok,
converted_fund, ncen_non_income_distribution`

- `character` ∈ `net_investment_income | net_realized_gain | return_of_capital | other | total`
- `per_share` positive number; `0.0` when the table shows a dash or omits the row
- `parse_status` ∈ `ok | no_table_found | ambiguous | failed` — **"could not parse" must never
  look like "no capital gain"**
- Optionally a small per-fund summary helper: `any_capital_gain` over the 5 years

## 4. Verified findings (from EDGAR, 2026-11-24)

### 4.1 XBRL is not usable
Neither example filing nor the JPM annual N-CSR has any inline-XBRL tag for distributions.
Tags present are `oef:*` / `dei:*` (shareholder-report summary, prospectus fee/risk data) only.
The data must be parsed from HTML tables.

### 4.2 Financial highlights table = the source
Each annual N-CSR shows up to 5 fiscal years per fund. Two opposite layouts confirmed:

**T. Rowe Price (TVAL) — vertical; one fund per filing; periods are columns, items are rows.**
A character with no distribution is simply **omitted** (no "net realized gain" row at all).
```
                      | 6 Months Ended 6/30/26 | Year Ended 12/31/25 | 12/31/24 | 6/14/23(1) Through 12/31/23
NET ASSET VALUE
Beginning of period   | $ 35.22 | $ 30.79 | $ 27.22 | $ 25.00
...
Total from investment activities | 6.45 | 4.84 | 3.93 | 2.39
Distributions
Net investment income | -       | (0.41)  | (0.36)  | (0.17)
NET ASSET VALUE
End of period         | $ 41.67 | $ 35.22 | $ 30.79 | $ 27.22
```

**JPMorgan (LVDS) — horizontal; 13 funds per filing; items are columns, periods are rows.**
Fund name appears as a row inside the table. Nil is shown as `—` / `$—`.
```
Per share operating performance
Investment operations | Distributions
NAV begin | NII | Net realized and unrealized gains | Total from inv ops | Net investment income | Net realized gain | Total distributions
JPMorgan Fundamental Data Science Large Value ETF (f)
Year Ended June 30, 2026 | $53.36 | $0.83 | $11.50 | $12.33 | $(0.82 ) | $(7.63 ) | $(8.45 )
```
Parsing gotchas seen: numbers like `(7.63 )` with a space before the paren; `$` in its own
cell; non-breaking spaces; footnote markers glued to labels (`(a)`, `(1)`); wide tables may be
split into a left half and right half (JPM continues ratios in a second table). `lxml.html`
needs **bytes**, not str (docs carry an encoding declaration). Take only leaf tables (no nested
`<table>`).

### 4.3 Other statements are not enough
Statement of Changes shows only a total in dollars ("Total distributions to shareholders" at
JPM, "Net earnings" at T. Rowe) — no character split. Do not use it for character.

### 4.4 Mapping filings to funds
- `https://www.sec.gov/files/company_tickers_mf.json` → fields `[cik, seriesId, classId, symbol]`,
  one row per share class. Gives ticker → trust CIK, series, and the **ETF class ID** directly.
- Each filing's SGML header (`<accession>-index-headers.html` or the `.txt`) lists every
  `SERIES-ID`, `SERIES-NAME`, `CLASS-CONTRACT-ID`, `CLASS-CONTRACT-TICKER-SYMBOL` it covers.
- Trusts differ in how they file: JPM files a few combined N-CSRs per year, grouped by fiscal
  year-end (Feb 28, Jun 30, Oct 31); T. Rowe files **one N-CSR per fund** (many on one day).
  So "most recent N-CSR" means the most recent **annual N-CSR whose header includes the series**.
- EDGAR full-text search can pre-filter by series:
  `https://efts.sec.gov/LATEST/search-index?q="S000079838"&forms=N-CSR` (worked for NPORT-P in
  the old repo; filter is by filing date).

### 4.5 Test fixtures with expected values

| | TVAL | LVDS |
|---|---|---|
| Fund | T. Rowe Price Value ETF | JPMorgan Fundamental Data Science Large Value ETF |
| Trust | T. Rowe Price Exchange-Traded Funds, Inc. — CIK 1795351 | J.P. Morgan Exchange-Traded Fund Trust — CIK 1485894 |
| Series / class | S000079838 / C000241226 | S000090418 / C000257465 |
| Fiscal year-end | Dec 31 | Jun 30 |
| Filing examined | N-CSRS `0001193125-26-361986` (`d152895dncsrs.htm`, 0.8 MB) | N-CSR `0001193125-26-385040` (`d153689dncsr.htm`, 8.5 MB), period 2026-06-30 |

Expected per-share distributions:

- **LVDS** (year ended June 30): net investment income / net realized gain / total
  - 2026: 0.82 / **7.63** / 8.45
  - 2025: 0.94 / 3.23 / 4.17
  - 2024: 0.99 / 2.66 / 3.65
  - 2023: 0.99 / 2.14 / 3.13
  - 2022: 0.94 / 14.89 / 15.83
- **TVAL** (year ended Dec 31; inception 6/14/23): net investment income only, **no capital gain**
  - 2025: 0.41 — 2024: 0.36 — 2023 (partial): 0.17
  - These came from the semiannual report; the tool should read them from the most recent
    annual N-CSR (FY 2025), accession not yet looked up.
- Same JPM filing, extra checks: Equity Focus ETF net realized gain 2026 `—`, 2025 0.09,
  2024 `—`, 2023 0.50, 2022 5.58. Fundamental Data Science Large Growth ETF: 7.07, 4.72, 0.41,
  1.94, 14.78.

The JPM N-CSR header lists 13 series with tickers: JEPI, JAVA, JMEE, JEPQ, JGRO, JPSV, JPEF,
JTEK, LCDS, MCDS, SCDS, JUSA, LVDS — a ready-made `trust` scope test (note: the trust has more
ETFs in other fiscal-year-end filings).

### 4.6 Converted funds
LVDS appears to be a mutual fund converted to an ETF during FY2026: its Statement of Changes
still lists Class A, C, I, R2, R5 capital transactions, and the fund name carries a footnote
`(f)` in the highlights table. Its 2022–2025 capital gain history was therefore likely paid as
a mutual fund. **Not yet done:** read the footnote text and choose the detection rule
(candidates: footnote mentioning reorganization/conversion/predecessor; multiple share classes
in the same fund's statements).

### 4.7 N-CEN corroboration field
The N-CEN XML schema has, under `registrantInfo`:
- `isPaymentDividend` (yes/no)
- `paymentDividendSeries` → `isPaymentDividend` + `seriesInfo` (`seriesId`, `seriesName`)

Believed to be the Rule 19a-1 item: did the fund pay a distribution from a source other than
net income. **Unverified:** the exact semantics and how reliably issuers answer it. Validate
against TVAL (expect no) and LVDS (expect yes) before relying on it. N-CEN is one filing per
registrant per fiscal year, so 5 fiscal years = up to 5 small XML filings per trust; a trust
with several fiscal year-ends may file several per year — match by period.

N-CEN also identifies ETFs (`exchangeSeriesInfo.exchangeTradedFund` in the schema), usable to
keep only ETFs in `trust` scope when a trust also holds mutual funds. Simpler alternative to
test first: the class tickers in `company_tickers_mf.json` plus the header's class list.

### 4.8 Filings ruled out
- NPORT-P: no distribution fields.
- 485BPOS: same highlights table, bigger document, no advantage.
- Form 8937 / year-end capital gain notices: on issuer websites, not EDGAR.

## 5. Known limitations (state these in the README / output docs)

1. Parser is heuristic; labels and layouts vary by issuer. Unparsed funds are reported as such.
2. "Net realized gain" is **book** character: short-term and long-term gains combined. It is not
   the tax-basis long-term capital gain figure.
3. Fiscal-year granularity. No payment dates. Data lags by the ~2-month filing delay, so the
   most recent distributions may not be reported yet.
4. Funds younger than 5 years show fewer periods. Converted funds show predecessor history.
5. Only two issuers examined so far. iShares, Vanguard (ETF is a share class of a multi-class
   fund — must pick the ETF class's table), and a white-label trust are untested.

## 6. Parser design

1. Parse HTML bytes with `lxml.html`; collect leaf tables.
2. Candidate tables: contain "distribution" and a NAV marker ("net asset value").
3. Assign table → fund: fund name inside the table (JPM) or the only series in the filing header
   (T. Rowe); for multi-class funds, the section for the ETF class.
4. Detect orientation: period labels ("Year Ended", dates) in the header row → vertical;
   in the first column → horizontal.
5. Normalize to (period, label, value). Only labels inside the **Distributions** block count —
   "Net investment income" also appears under investment operations.
6. Map labels to character by keyword: `investment income` → NII; `realized`/`capital gain` →
   net realized gain; `return of capital`/`paid-in` → return of capital; `total` → total.
7. Validate each period: `NAV begin + total from operations − total distributions ≈ NAV end`
   (±0.02), and components sum to total. TVAL 2025: 30.79 + 4.84 − 0.41 = 35.22 ✓.
   Result goes in `nav_check_ok`.

## 7. Build phases

1. **Scaffold** — new repo, venv on Python 3.15, `edgartools` + `polars` (+ `lxml` if not
   transitive), `.gitignore`, copy this file in. No `.env`; identity via `set_identity` (§8).
2. **Explore edgartools first** — its API was **not** tested in the research session. Confirm,
   before designing around it: identity setup, ticker → fund/series/class lookup, listing a
   CIK's N-CSR / N-CEN filings, access to filing header series lists, HTML and attachment
   download, built-in caching and rate limiting, any existing N-CSR or N-CEN parsing. Use what
   exists; write only what is missing.
3. **ID resolution** — `get_etf_ids`, `get_etf_trust_ids`, input parsing (ticker / list / CSV).
4. **Filing selection** — most recent annual N-CSR covering each series; group funds by filing
   so a combined filing is downloaded and parsed once.
5. **Highlights parser** — section 6, against the fixtures in 4.5.
6. **N-CEN corroboration** — validate the field, then add the column.
7. **`get_distributions` + CLI.**
8. **Widen validation** — iShares, Vanguard, one white-label trust; fix label/layout gaps.

## 8. Environment notes

- Windows 11, PowerShell primary; Python 3.15.2 at
  `C:\Users\marcc\AppData\Local\Programs\Python\Python315\python.exe` (`py -3.15`).
- A pip dry run on 3.15 resolved `edgartools 6.4.0` and `polars 2.2.0` cleanly.
- SEC requires callers to identify themselves; limit 10 requests/second. **User decision: no
  `.env`.** Identity is set with one line in the code, using edgartools:
  `set_identity("<USER_EMAIL>")`, where the value is the user's own email address. Put it in
  one place only (package init or the CLI entry point), not in every module.
- A privacy hook in the user's Claude Code setup blocks the assistant from writing that address
  into files or commands (it refused both a shell command and an edit to this file). Do not
  work around it: write the line with the `<USER_EMAIL>` placeholder and ask the user to fill
  in their address themselves.
- Reference implementation of raw-EDGAR ticker/trust resolution and cached fetching lives in
  `c:\develop\edgar-etf-querier\edgar\` (`cik_lookup.py`, `trust_lookup.py`, `fetcher.py`) if
  edgartools lacks something.
