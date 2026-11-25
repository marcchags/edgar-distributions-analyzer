"""Trust (registrant CIK) behind each ETF.   get_etf_trust LVDS,TEMR   |   get_etf_trust path_to_csv.csv

Add --etfs to list every ETF in those trusts instead of one row per trust."""
import polars as pl

from etf_common import ID_COLS, cli, fund_table, is_etf, show
from get_etf import get_etf_ids


def get_etf_trusts(etfs: str) -> pl.DataFrame:
    """One row per trust: trust_cik, trust_name, and the requested tickers that belong to it."""
    return (get_etf_ids(etfs).group_by("trust_cik", "trust_name", maintain_order=True)
            .agg(tickers=pl.coalesce("ticker", "series_id").str.join(",")))


def get_etf_trust_ids(etfs: str) -> pl.DataFrame:
    """Same columns as get_etf_ids, expanded to every ETF in each requested ticker's trust."""
    ciks = get_etf_ids(etfs)["trust_cik"].unique()
    return (fund_table().filter(pl.col("trust_cik").is_in(ciks.implode()), is_etf())
            .sort("trust_cik", "ticker", "series_id").select(ID_COLS))


def main():
    args = cli(__doc__, etfs={"dest": "expand", "action": "store_true", "help": "list every ETF in each trust"})
    show((get_etf_trust_ids if args.expand else get_etf_trusts)(args.etfs), args.out)


if __name__ == "__main__":
    main()
