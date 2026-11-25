"""Shared helpers: SEC identity, input parsing, the fund reference table, output."""
import argparse
import os
import sys
from functools import lru_cache

import polars as pl
from edgar import set_identity

# SEC requires a contact in the User-Agent. Put your email here (the only place it is set),
# or set the EDGAR_IDENTITY environment variable instead.
IDENTITY = "marc.chags@gmail.com"

if "@" in IDENTITY:
    set_identity(IDENTITY)
elif not os.environ.get("EDGAR_IDENTITY"):
    sys.exit("Set IDENTITY in etf_common.py (or the EDGAR_IDENTITY env var) to your email; SEC requires it.")

ID_COLS = ["ticker", "series_id", "class_id", "fund_name", "trust_cik", "trust_name"]


def parse_etfs(etfs: str) -> list[str]:
    """'TVAL' | 'TVAL,S000090467' | path to a .csv with an 'ETF' header -> unique upper-case tickers / series IDs."""
    raw = pl.read_csv(etfs)["ETF"].to_list() if etfs.lower().endswith(".csv") else etfs.split(",")
    return list(dict.fromkeys(t.strip().upper() for t in raw if t and t.strip()))


@lru_cache(maxsize=1)
def fund_table() -> pl.DataFrame:
    """One row per share class: SEC's series file (every class, some without a ticker) plus its daily ticker file."""
    from edgar.funds.reference import get_bulk_fund_data
    from edgar.reference.tickers import get_mutual_fund_tickers

    series = pl.from_pandas(get_bulk_fund_data()).select(
        class_id="Class ID", series_id="Series ID", fund_name="Series Name", class_name="Class Name",
        trust_cik=pl.col("CIK Number").cast(pl.Int64), trust_name="Entity Name", ticker="Class Ticker",
    ).drop_nulls("class_id").unique("class_id", maintain_order=True)
    daily = pl.from_pandas(get_mutual_fund_tickers()).select(
        "ticker", class_id="classId", series_id="seriesId", trust_cik=pl.col("cik").cast(pl.Int64),
    ).unique("class_id", maintain_order=True)
    both = series.join(daily, on="class_id", how="full", coalesce=True, suffix="_daily")
    return both.select(
        pl.coalesce("ticker_daily", "ticker").str.to_uppercase().alias("ticker"),
        pl.coalesce("series_id", "series_id_daily").alias("series_id"), "class_id", "fund_name",
        pl.coalesce("trust_cik", "trust_cik_daily").alias("trust_cik"), "trust_name", "class_name",
    )


def is_etf() -> pl.Expr:
    """Mutual fund classes trade under five letters ending in X; ETF classes do not. No ticker: go by the name."""
    named = pl.concat_str("fund_name", "class_name", separator=" ", ignore_nulls=True).str.contains(r"\bETF\b")
    return pl.when(pl.col("ticker").is_null()).then(named).otherwise(~pl.col("ticker").str.contains(r"^[A-Z]{4}X$"))


def cli(description: str, **extra) -> argparse.Namespace:
    """Common command line: one positional ETF list plus --out, and any extra flags."""
    p = argparse.ArgumentParser(description=description)
    p.add_argument("etfs", help="tickers or series IDs (S000090467), comma-separated, or path to a .csv with an 'ETF' header")
    p.add_argument("--out", help="write the result to this .csv")
    for flag, kw in extra.items():
        p.add_argument(f"--{flag}", **kw)
    return p.parse_args()


def show(df: pl.DataFrame, out: str | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=250, fmt_str_lengths=60, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True, tbl_hide_dtype_separator=True):
        print(df)
    if out:
        df.write_csv(out)
        print(f"wrote {out}", file=sys.stderr)
