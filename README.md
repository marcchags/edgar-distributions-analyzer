# edgar-distributions-analyzer

What did an ETF distribute per share over its last five fiscal years, and with what character:
net investment income, net realized gain, or return of capital? Read from the financial highlights
table of each fund's most recent annual N-CSR on SEC EDGAR.

## Setup

1. `py -3.15 -m pip install -e .` puts `get_etf`, `get_etf_trust` and `get_distribution_data` on the PATH.
   Without it, run the scripts directly: `py -3.15 get_etf.py LVDS`.
2. Put your email in `IDENTITY` in `etf_common.py` (or set the `EDGAR_IDENTITY` environment variable).
   SEC requires a contact in the User-Agent.

## Use

Every command takes a ticker, a comma-separated list, or a `.csv` with an `ETF` header. `--out file.csv` saves the result.
An ETF with no ticker on file at the SEC (shown with a blank ticker) can be given by series ID instead, e.g. `S000090467`.

| Command | Result |
|---|---|
| `get_etf LVDS,TEMR` | series ID, class ID, fund name, trust CIK and trust name per ETF |
| `get_etf_trust LVDS,TEMR` | one row per trust (registrant CIK); add `--etfs` to list every ETF in those trusts |
| `get_distribution_data LVDS,TVAL` | distributions per fiscal year; add `--scope trust` for every ETF in the trusts |

`get_distribution_data` prints one row per fund and fiscal year. `--out` writes the long format: one row per
fund x fiscal period x character, with the source label, accession number and document URL.

From Python: `get_etf_ids`, `get_etf_trusts`, `get_etf_trust_ids`, `get_distributions(etfs, scope)`, `summarize(df)`.

## Reading the output

- `parse_status`: `ok`; `ambiguous` (several tables disagreed for the fund, first one shown); `no_table_found`
  (filing found, no highlights table readable); `no_filing` (no annual N-CSR yet, typical for new funds);
  `failed`. Anything but `ok` means "unknown", never "no capital gain".
- `nav_check_ok`: components add up to total distributions, and opening NAV + operations − distributions = closing NAV
  (within 0.02). Null when the table gave nothing to check against.
- `converted_fund`: a footnote on the fund's table mentions a reorganization, conversion or predecessor fund,
  so earlier years were probably paid as a mutual fund.
- `net_assets` (`aum_musd` on screen, in $ millions): the ETF share class's net assets at the latest fiscal
  year-end in the report, so it can be over a year old. Blank when the unit or row could not be read reliably.
- `ncen_non_income_distribution`: the N-CEN `isPaymentDividend` answer for the same fiscal period. It is **not** a
  capital gain indicator: the J.P. Morgan trust answered N for a year in which LVDS paid 7.63 per share of
  realized gain. Reported as filed, for reference only.

## Limitations

1. The parser is heuristic; labels and layouts vary by issuer. Checked against J.P. Morgan, T. Rowe Price,
   Vanguard, iShares (bond trust), Schwab, Invesco, ARK and Amplify filings.
2. Filings whose financial statements are not HTML tables cannot be read (iShares IVV reports `no_table_found`).
   Some Vanguard multi-fund filings come back `ambiguous` (VTI).
3. "Net realized gain" is book character: short- and long-term gains combined, not the tax-basis long-term figure.
4. Fiscal-year granularity, no payment dates, and a filing lag of about two months.
5. Funds younger than five years show fewer periods. Trust scope treats any share class whose ticker is not
   five letters ending in X as an ETF; a class with no ticker on file counts only if its name contains "ETF".
6. A trust is one SEC registrant. Fund families span several (EEM is in iShares, Inc.; other iShares funds are in
   iShares Trust or BlackRock ETF Trust), so trust scope does not cover a whole brand.
