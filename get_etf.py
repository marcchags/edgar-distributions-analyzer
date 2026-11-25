"""EDGAR identifiers for individual ETFs.   get_etf LVDS,TEMR   |   get_etf path_to_csv.csv

An ETF with no ticker on file at the SEC can be given by series ID instead: get_etf S000090467"""
import re
import sys

import polars as pl

from etf_common import ID_COLS, cli, fund_table, is_etf, parse_etfs, show


def get_etf_ids(etfs: str) -> pl.DataFrame:
    """One row per requested ETF: ticker, series_id, class_id, fund_name, trust_cik, trust_name."""
    funds, keys = fund_table(), parse_etfs(etfs)
    found = [funds.filter((pl.col("series_id") == key) & is_etf() if re.fullmatch(r"S\d{9}", key) else pl.col("ticker") == key)
             for key in keys]
    if missing := [k for k, f in zip(keys, found) if f.is_empty()]:
        print(f"not found on EDGAR: {', '.join(missing)}", file=sys.stderr)
    return pl.concat(found or [funds.clear()]).unique("class_id", maintain_order=True).select(ID_COLS)


def main():
    args = cli(__doc__)
    show(get_etf_ids(args.etfs), args.out)


if __name__ == "__main__":
    main()
