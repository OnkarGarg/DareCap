"""Load Barchart earnings-calendar CSVs from S3 into MySQL.

S3 layout: raw/earnings_calendar/<fetched_date>/<report_date>.csv   (prod)
           test/earnings_calendar/<fetched_date>/<report_date>.csv  (test)
Each file is the complete list for one report date, so loading it replaces that date.

Usage:
    python earnings_load.py file <s3 key>
    python earnings_load.py rebuild [--yes]
"""
import csv
import io
import re
import sys

import load

PREFIX = "raw/earnings_calendar/" if load.ENV == "prod" else "test/earnings_calendar/"
TABLE = "earnings_calendar"
KEY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})\.csv$")

COLUMNS = {
    "symbol":                      ("symbol",             "VARCHAR(32)"),
    "symbolName":                  ("symbol_name",        "VARCHAR(255)"),
    "timeCode":                    ("time_code",          "VARCHAR(32)"),
    "estimatedEarnings":           ("estimated_earnings", load.NUM),
    "lastReportedEarnings":        ("reported_earnings",  load.NUM),
    "lastEarningsSurpriseAmount":  ("surprise_amount",    load.NUM),
    "lastEarningsSurprisePercent": ("surprise_percent",   load.NUM),
}


def ensure_schema(cur):
    cols = ",\n  ".join(f"`{c}` {t} NULL" for c, t in COLUMNS.values() if c != "symbol")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS `{TABLE}` (
          report_date DATE NOT NULL,
          symbol VARCHAR(32) NOT NULL,
          {cols},
          fetched_date DATE NOT NULL,
          source_key VARCHAR(255) NOT NULL,
          loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (report_date, symbol)
        )""")


def parse_csv(text, report_date, fetched_date, key):
    rows = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        rec = {"report_date": report_date, "symbol": sym}
        for h, (c, t) in COLUMNS.items():
            if c != "symbol":
                rec[c] = load.parse_value(r.get(h), t)
        rec["fetched_date"] = fetched_date
        rec["source_key"] = key
        rows[sym] = rec  # dedupes if a symbol ever appears twice
    return list(rows.values())


def load_key(cur, key):
    fetched, report = KEY_RE.search(key).groups()

    cur.execute(f"SELECT MAX(fetched_date) FROM `{TABLE}` WHERE report_date = %s", (report,))
    newest = cur.fetchone()[0]
    if newest is not None and str(newest) > fetched:
        print(f"skipping {key}: already have data fetched {newest}")
        return 0

    rows = parse_csv(load.read_key(key), report, fetched, key)
    cur.execute(f"DELETE FROM `{TABLE}` WHERE report_date = %s", (report,))
    if rows:
        cols = list(rows[0])
        sql = (f"INSERT INTO `{TABLE}` ({', '.join(f'`{c}`' for c in cols)}) "
               f"VALUES ({', '.join(['%s'] * len(cols))})")
        cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def load_file(key):
    conn = load.connect()
    with conn.cursor() as cur:
        ensure_schema(cur)
        n = load_key(cur, key)
    conn.commit()
    conn.close()
    print(f"[{load.ENV}] {key}: {n} rows")


def rebuild():
    if load.ENV == "prod" and "--yes" not in sys.argv:
        sys.exit("Refusing to rebuild prod without --yes")
    keys = []
    for page in load.s3.get_paginator("list_objects_v2").paginate(Bucket=load.BUCKET, Prefix=PREFIX):
        keys += [o["Key"] for o in page.get("Contents", []) if KEY_RE.search(o["Key"])]
    keys.sort()  # fetched_date folder first, so newer fetches load last
    conn = load.connect()
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
        ensure_schema(cur)
        total = 0
        for key in keys:
            total += load_key(cur, key)
            conn.commit()
    conn.close()
    print(f"[{load.ENV}] rebuilt {TABLE} from {len(keys)} files, {total} rows")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "file" and len(sys.argv) > 2:
        load_file(sys.argv[2])
    elif cmd == "rebuild":
        rebuild()
    else:
        sys.exit(__doc__)