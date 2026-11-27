"""Per-share distributions by character for the last 5 fiscal years, from each ETF's latest annual N-CSR.

get_distribution_data LVDS,TVAL [--scope trust] [--out result.csv]   |   get_distribution_data path_to_csv.csv
"""
import html
import re
import sys
from datetime import date, datetime, timedelta

import lxml.html
import polars as pl
from edgar import Company
from edgar.httprequests import download_text

from etf_common import cli, show
from get_etf import get_etf_ids
from get_etf_trust import get_etf_trust_ids

CHARACTERS = ["net_investment_income", "net_realized_gain", "return_of_capital", "total"]
SCHEMA = {
    "ticker": pl.String, "series_id": pl.String, "class_id": pl.String, "fund_name": pl.String,
    "trust_cik": pl.Int64, "period_end": pl.Date, "period_label": pl.String, "character": pl.String,
    "per_share": pl.Float64, "raw_label": pl.String, "accession_no": pl.String, "filing_date": pl.Date,
    "source_url": pl.String, "parse_status": pl.String, "nav_check_ok": pl.Boolean,
    "converted_fund": pl.Boolean, "ncen_non_income_distribution": pl.Boolean, "net_assets": pl.Float64,
}
LOOKBACK_DAYS = 550  # an annual N-CSR covering a live fund is always newer than this
TOL = 0.021
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})|\b(" + "|".join(MONTHS) + r")[a-z]*\.? (\d{1,2}), ?(\d{4})", re.I)
PREFIX = re.compile(r"\bended\b|\bthrough\b|\d/|\b(?:" + "|".join(MONTHS) + ")", re.I)  # 'Year Ended October 31,'
MARKER = re.compile(r"\((\w{1,2})\)")
CONVERTED = re.compile(r"reorganiz|conver(?:sion|ted)|predecessor|acquired fund", re.I)


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")).strip()


def _num(t: str) -> float | None:
    """'(7.63 )' -> -7.63, '$—' -> 0.0, '49.63(4)' -> 49.63, anything else (labels, lone footnote markers) -> None."""
    t = re.sub(r"[$\s,%]", "", re.sub(r"(?<=[\d)\N{EM DASH}\N{EN DASH}-])\s*\(\w{1,2}\)$", "", t))
    if t and not t.strip("\N{EM DASH}\N{EN DASH}-"):
        return 0.0
    m = None if re.fullmatch(r"\(\d{1,2}\)", t) else re.fullmatch(r"(\(|-)?(\d*\.?\d+)\)?", t)
    return (-1 if m[1] else 1) * float(m[2]) if m else None


def _date(label: str) -> date | None:
    """Last date in a period label ('9/28/21 Through 5/31/22' -> 2022-05-31)."""
    if not (ms := list(DATE.finditer(label))):
        return None
    m1, d1, y1, mon, d2, y2 = ms[-1].groups()
    y, m, d = (int(y1), int(m1), int(d1)) if m1 else (int(y2), MONTHS.index(mon[:3].lower()) + 1, int(d2))
    return date(y + (0 if y > 99 else 2000 if y < 70 else 1900), m, d)


def _rows(table) -> list[list[tuple[int, int, str]]]:
    """Non-empty cells per row as (first grid column, last + 1, text); lone '$', ')' and '%' cells dropped."""
    out = []
    for tr in table.iter("tr"):
        col, row = 0, []
        for td in tr.iterchildren("td", "th"):
            span = int(td.get("colspan") or 1)
            if (t := _ws(td.text_content())).strip("$)% "):
                row.append((col, col + span, t))
            col += span
        if row:
            out.append(row)
    return out


def _slot(cols, cell) -> int:
    """Index of the header column a value cell sits under: most overlap, else nearest."""
    return max(range(len(cols)), key=lambda i: (min(cols[i][1], cell[1]) - max(cols[i][0], cell[0]),
                                                -abs(cols[i][0] + cols[i][1] - cell[0] - cell[1])))


def _values(cols, row) -> dict[int, float]:
    """Numeric cells of a data row keyed by header column index."""
    cells = [(c, v) for c in row if (v := _num(c[2])) is not None]
    if len(cells) == len(cols):
        return {i: v for i, (_, v) in enumerate(cells)}
    return {_slot(cols, c): v for c, v in cells}


def _is_data(row) -> bool:
    return len(row) > 1 and _num(row[0][2]) is None and all(_num(c[2]) is not None for c in row[1:])


def _character(label: str) -> str:
    for pattern, name in ((r"total", "total"), (r"return of capital|paid[- ]in", "return_of_capital"),
                          (r"realized|gain", "net_realized_gain"), (r"income", "net_investment_income")):
        if re.search(pattern, label):
            return name
    return "other"


def _scale(label: str, table_text: str) -> float:
    """Multiplier to dollars for a 'Net assets, end of period (000)' row; the unit may sit elsewhere in the table."""
    for text, million, thousand in ((label, r"million|000,000", r"thousand|000"),
                                    (table_text, r"in millions|\(millions\)", r"in thousands|\(thousands\)|\(000|000['\N{RIGHT SINGLE QUOTATION MARK}]?s omitted")):
        if re.search(million, text, re.I):
            return 1e6
        if re.search(thousand, text, re.I):
            return 1e3
    return 1.0


def _analyze(seq: list[tuple[str, float | None]], table_text: str = "") -> dict | None:
    """Ordered (label, value) pairs for one fund and period -> the Distributions block and NAV roll-forward inputs.

    Only labels after 'Total from investment operations' count: 'Net investment income' also appears above it.
    """
    lab = [MARKER.sub("", label).lower() for label, _ in seq]
    find = lambda pat, i=0: next((j for j in range(i, len(lab)) if re.search(pat, lab[j]) and seq[j][1] is not None), None)
    begin, ops = find(r"beginning"), find(r"(total|net increase).*(operations|activities)")
    start = ops + 1 if ops is not None else next((j for j, s in enumerate(lab) if re.search(r"distribut|dividend", s)), None)
    if start is None or begin is None:  # no opening NAV: not a highlights table
        return None
    items = []
    for j in range(start, len(seq)):
        if re.search(r"net asset value|end of|total return|market price|ratio", lab[j]):
            break
        if seq[j][1] is not None:
            items.append((_character(lab[j]), seq[j][0], abs(seq[j][1])))
            if items[-1][0] == "total":
                break
    end, assets = find(r"end of (period|year)", start), find(r"^(total )?net assets(?!.*(ratio|average))", start)
    net_assets = assets is not None and abs(seq[assets][1]) * _scale(seq[assets][0], table_text)
    return {"items": items, "nav_begin": seq[begin][1], "ops": ops is not None and seq[ops][1],
            "nav_end": end is not None and seq[end][1],
            "total": next((v for c, _, v in items if c == "total"), sum(v for _, _, v in items)),
            "net_assets": net_assets if net_assets and net_assets >= 1e5 else None}  # smaller: a missed unit


def _continuation(table, n: int) -> list[list[tuple[str, float | None]]] | None:
    """Right-hand half of a wide highlights table: closing NAV, net assets and ratios for the same n rows.

    It repeats neither fund names nor periods, so its rows pair with the left half by position only.
    """
    for t in table.xpath("following::table[position() <= 6]"):
        rows = _rows(t)
        data = [r for r in rows if len(r) > 2 and all(_num(c[2]) is not None for c in r)]
        cols = next((r for r in reversed(rows[:rows.index(data[0])] if data else [])
                     if len(r) > 2 and all(_num(c[2]) is None for c in r)), None)
        if cols and len(data) == n and any(re.match(r"net assets", c[2], re.I) for c in cols):
            return [[(c[2], vals.get(i)) for i, c in enumerate(cols)] for vals in (_values(cols, r) for r in data)]
    return None


def _parse_table(table) -> list[dict]:
    """One highlights table -> [{name, label, seq, notes}] per fund x period, whichever way the table is laid out.

    name is the fund-name row above the period (horizontal layout) or None; notes are its footnote markers.
    """
    rows = _rows(table)
    data = [i for i, r in enumerate(rows) if _is_data(r)]
    if not data:
        return []
    head = rows[:data[0]]
    if 2 * sum(bool(DATE.search(rows[i][0][2])) for i in data) >= len(data):  # periods are rows, items are columns
        cols = next((r for r in reversed(head) if len(r) > 2 and all(_num(c[2]) is None for c in r)), None)
        if not cols:
            return []
        out, name = [], None
        for r in rows[data[0] - 1:]:
            if len(r) == 1 and _num(r[0][2]) is None:
                name = r[0][2]
            elif _is_data(r) and DATE.search(r[0][2]):
                vals = _values(cols, r[1:])
                out.append({"name": name, "label": r[0][2], "seq": [(c[2], vals.get(i)) for i, c in enumerate(cols)],
                            "notes": f"{name} {' '.join(c[2] for c in r)}", "wide": True})
        return out
    # periods are columns, items are rows. A column header is a full date ('Year Ended 5/31/26'), or a bare year
    # completed by a spanning cell in a row above ('Year Ended October 31,' over '2023 | 2022').
    cells = [(a, b, MARKER.sub("", t).strip()) for r in head for a, b, t in r]
    full = [c for c in cells if DATE.search(c[2]) and not re.search(r"(through|to|[\N{EN DASH}-])$", c[2], re.I)]
    years = [c for c in cells if re.fullmatch(r"(?:19|20)\d{2}", c[2])]
    prefix = [c for c in cells if c not in full and c not in years and PREFIX.search(c[2])]
    over = lambda c, col: c[0] < col[1] and col[0] < c[1]
    cols = [(c[0], 0, c, " ".join(p[2] for p in prefix + [c] if p is c or over(p, c))) for c in full]
    for y in years:
        parent = next((p for p in prefix if over(p, y)), prefix[-1] if prefix else y)
        cols.append((parent[0], y[0], y, f"{parent[2]} {y[2]}" if parent is not y else y[2]))
    if not cols:
        return []
    cols.sort(key=lambda c: c[:2])
    spans, used = [c[2] for c in cols], full + years + prefix
    seqs, notes = [[] for _ in cols], " ".join(c[2] for r in rows for c in r)
    for r in rows[data[0]:]:
        if _num(r[0][2]) is None:
            vals = _values(spans, r[1:])
            for i, seq in enumerate(seqs):
                seq.append((r[0][2], vals.get(i)))
    name = " ".join(c[2] for c in cells if c not in used) or None  # fund or share class named in the header
    return [{"name": name, "label": c[3], "seq": seq, "notes": notes, "wide": False} for c, seq in zip(cols, seqs)]


def _footnotes(table) -> dict[str, str]:
    """Footnote marker -> text from the tables that directly follow a highlights table."""
    notes = {}
    for t in table.xpath("following::table[position() <= 3]"):
        for r in _rows(t):
            if len(r) > 1 and (m := MARKER.fullmatch(r[0][2])):
                notes.setdefault(m[1], r[1][2])
    return notes


def parse_filing(doc: str, series: dict[str, str]) -> dict[str, dict]:
    """N-CSR HTML + {series_id: name} from its header -> {series_id: {status, converted, periods}}.

    Each period is {label, end, items [(character, raw_label, per_share)], nav_ok}. A table belongs to the fund
    named in a row inside it, else to the last fund named in the text before it, else to the filing's only series.
    """
    root = lxml.html.fromstring(doc.encode("utf-8"), parser=lxml.html.HTMLParser(encoding="utf-8"))
    # full names plus the name without its brand word ('Total Stock Market Index Fund'), longest first
    names = sorted({(v, s) for s, n in series.items() for v in (_ws(n).lower(), _ws(n).lower().split(" ", 1)[-1])
                    if v.count(" ") > 1 or v == _ws(n).lower()}, key=lambda x: -len(x[0]))
    match = lambda text: next((s for n, s in names if n in _ws(text).lower()), None)
    only = next(iter(series)) if len(series) == 1 else None
    found, context, heading = {}, only, ""
    for el in root.iter():
        if el.tag == "table" and el.find(".//table") is None:
            text = el.text_content().lower()
            if "net asset value" not in _ws(text) or not re.search(r"distribut|dividend", text):
                continue
            notes, sid, first, recs = None, context, "", _parse_table(el)
            if recs and recs[0]["wide"] and (more := _continuation(el, len(recs))):
                joined = [r | {"seq": r["seq"] + m} for r, m in zip(recs, more)]
                rolls = [a and a["nav_end"] is not False and abs(a["nav_begin"] + a["ops"] - a["total"] - a["nav_end"]) < TOL
                         for a in (_analyze(r["seq"]) for r in joined)]
                if sum(map(bool, rolls)) >= 0.8 * len(recs):  # else the rows do not line up: ignore the right half
                    recs = joined
            for rec in recs:
                if rec["name"] != first:  # a new fund-name row
                    first, sid = rec["name"], match(rec["name"] or "") or sid
                if not sid or not (res := _analyze(rec["seq"], _ws(text))):
                    continue
                notes = _footnotes(el) if notes is None else notes
                converted = any(CONVERTED.search(notes.get(m, "")) for m in MARKER.findall(rec["notes"]))
                cand = found.setdefault(sid, {}).setdefault(
                    id(el), {"heading": rec["name"] or heading, "converted": False, "periods": []})
                cand["converted"] |= converted
                cand["periods"].append({"label": rec["label"], "end": _date(rec["label"]), **res})
        else:
            for text in (el.text, el.tail):
                if text and (text := text.strip()) and len(text) < 150:
                    context, heading = only or match(text) or context, text
    return {sid: _merge(list(cands.values())) for sid, cands in found.items()}


def _merge(cands: list[dict]) -> dict:
    """Combine a fund's tables (a table can continue on the next page); disagreeing tables are 'ambiguous'."""
    key = lambda c: [(p["end"], p["label"], p["items"]) for p in c["periods"]]
    distinct = [c for i, c in enumerate(cands) if key(c) not in map(key, cands[:i])]
    ends = [p["end"] or p["label"] for c in distinct for p in c["periods"]]
    status = "ok"
    if len(ends) != len(set(ends)):  # several share classes: keep the ETF one if exactly one says so
        etf = [c for c in distinct if re.search(r"\betf\b", c["heading"], re.I)]
        distinct, status = (etf, "ok") if len(etf) == 1 else (distinct[:1], "ambiguous")
    periods = sorted((p for c in distinct for p in c["periods"]), key=lambda p: p["end"] or date.min, reverse=True)[:5]
    for p, later in zip(periods, [None] + periods):
        comps, total = [v for c, _, v in p["items"] if c != "total"], p["total"]
        nav_end = p["nav_end"] if p["nav_end"] is not False else later["nav_begin"] if later else False
        checks = [abs(sum(comps) - total) < TOL] if comps and len(comps) < len(p["items"]) else []
        if all(x is not False for x in (p["nav_begin"], p["ops"], nav_end)):
            checks.append(abs(p["nav_begin"] + p["ops"] - total - nav_end) < TOL)
        p["nav_ok"] = all(checks) if checks else None
    return {"status": status, "converted": any(c["converted"] for c in distinct), "periods": periods}


def _header_series(filing) -> dict[str, str]:
    """{series_id: series_name} for every series a filing covers, from its SGML header."""
    text = html.unescape(download_text(f"{filing.base_dir}/{filing.accession_no}-index-headers.html"))
    return dict(re.findall(r"<SERIES-ID>(\S+)\s*<SERIES-NAME>([^\n<]+)", text))


def _ncen_flags(filings, report_date) -> tuple[str | None, set[str]]:
    """N-CEN for the same fiscal period -> (registrant isPaymentDividend Y/N, series listed under it)."""
    ncen = next((f for f in filings if f.form == "N-CEN" and str(f.report_date) == str(report_date)), None)
    xml = (ncen.xml() or "") if ncen else ""
    listed = {s for block in re.findall(r"<paymentDividendSeries>.*?</paymentDividendSeries>", xml, re.S)
              for s in re.findall(r"S\d{9}", block)}
    answer = re.search(r"<isPaymentDividend>\s*(\w)", xml)
    return answer and answer[1].upper(), listed


def get_distributions(etfs: str, scope: str = "etf") -> pl.DataFrame:
    """Long table: one row per fund x fiscal period x character. scope 'trust' covers every ETF in each trust."""
    ids = (get_etf_trust_ids if scope == "trust" else get_etf_ids)(etfs)
    cutoff, rows = date.today() - timedelta(days=LOOKBACK_DAYS), []
    for (cik,), funds in ids.group_by("trust_cik", maintain_order=True):
        todo = {f["series_id"]: f for f in funds.iter_rows(named=True)}
        filings = list(Company(cik).get_filings(form=["N-CSR", "N-CEN"], amendments=False))
        for filing in filings:  # newest first
            if not todo or _as_date(filing.filing_date) < cutoff:
                break
            if filing.form != "N-CSR" or not (hit := todo.keys() & (series := _header_series(filing)).keys()):
                continue
            print(f"parsing {filing.accession_no} ({filing.report_date}) for {len(hit)} fund(s)", file=sys.stderr)
            base = {"accession_no": filing.accession_no, "filing_date": _as_date(filing.filing_date),
                    "source_url": filing.filing_url}
            try:
                parsed = parse_filing(download_text(filing.filing_url), series)
                answer, listed = _ncen_flags(filings, filing.report_date)
            except Exception as e:  # a filing we cannot read must not look like "no distributions"
                print(f"  failed: {e!r}", file=sys.stderr)
                rows += [todo.pop(s) | base | {"parse_status": "failed"} for s in hit]
                continue
            for sid in sorted(hit):
                fund = todo.pop(sid) | base | {"ncen_non_income_distribution": sid in listed if answer else None}
                res = parsed.get(sid)
                if not res or not res["periods"]:
                    rows.append(fund | {"parse_status": "no_table_found"})
                    continue
                fund |= {"parse_status": res["status"], "converted_fund": res["converted"],
                         "net_assets": res["periods"][0]["net_assets"]}  # at the latest fiscal year-end
                for p in res["periods"]:
                    got = {c: (raw, v) for c, raw, v in reversed(p["items"])}
                    got.setdefault("total", (None, sum(v for c, (_, v) in got.items())))
                    period = fund | {"period_end": p["end"], "period_label": p["label"], "nav_check_ok": p["nav_ok"]}
                    rows += [period | {"character": c, "raw_label": got.get(c, (None,))[0],
                                       "per_share": round(got.get(c, (None, 0.0))[1], 4)}
                             for c in CHARACTERS + [c for c in got if c not in CHARACTERS]]
        rows += [f | {"parse_status": "no_filing"} for f in todo.values()]
    order = {s: i for i, s in enumerate(ids["series_id"])}
    rows.sort(key=lambda r: order[r["series_id"]])  # stable: keeps periods newest-first within a fund
    return pl.DataFrame([{k: r.get(k) for k in SCHEMA} for r in rows], schema=SCHEMA)


def _as_date(d) -> date:
    return d if isinstance(d, date) else datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def summarize(df: pl.DataFrame) -> pl.DataFrame:
    """One row per fund x fiscal period for the screen: nii, cg (net realized gain) and roc per share."""
    keys = ["ticker", "series_id", "fund_name", "period_end", "parse_status", "nav_check_ok", "converted_fund",
            "ncen_non_income_distribution", "net_assets"]
    wide = (df.filter(pl.col("character").is_not_null())
            .pivot("character", index=keys, values="per_share", aggregate_function="first"))
    for c in CHARACTERS:
        wide = wide if c in wide.columns else wide.with_columns(pl.lit(None, pl.Float64).alias(c))
    rest = df.filter(pl.col("character").is_null()).select(keys)
    return (pl.concat([wide, rest], how="diagonal")  # total and the N-CEN flag stay in the long --out file only
            .select("ticker", "series_id", "fund_name", "period_end", nii="net_investment_income",
                    cg="net_realized_gain", roc="return_of_capital", parse_status="parse_status",
                    nav_check="nav_check_ok", converted_fund="converted_fund",
                    aum_musd=(pl.col("net_assets") / 1e6).round(1)))  # net assets at the latest fiscal year-end


def main():
    args = cli(__doc__, scope={"choices": ["etf", "trust"], "default": "etf", "help": "requested ETFs only, or every ETF in their trusts"})
    df = get_distributions(args.etfs, args.scope)
    show(summarize(df))
    if args.out:
        df.write_csv(args.out)
        print(f"wrote {args.out} (long format, {df.height} rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
