"""Load Barchart watchlist CSVs from S3 into MySQL.

S3 is the source of truth; the table can be dropped and rebuilt at any time.

Usage:
    python load.py daily 2026-10-06    # load one day's file (re-running is safe)
    python load.py rebuild             # drop the table and reload every file from S3

Env vars:
    ENV          prod | test   (default: test)
    BUCKET       S3 bucket name
    DB_HOST, DB_USER, DB_PASSWORD
    SSL_CA       path to global-bundle.pem (default: ./global-bundle.pem)

S3 layout:  raw/watchlist/YYYY-MM-DD.csv   (prod)
            test/watchlist/YYYY-MM-DD.csv  (test)

Requires: boto3, pymysql (and tzdata on Windows, for the time-zone database).
"""
import csv
import io
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
import pymysql

ENV = os.environ.get("ENV", "test")
BUCKET = os.environ["BUCKET"]
PREFIX = "raw/watchlist/" if ENV == "prod" else "test/watchlist/"
DB_NAME = "watchlist" if ENV == "prod" else "watchlist_test"
TABLE = "watchlist"

NUM = "DECIMAL(20,6)"
ET = ZoneInfo("America/New_York")

# CSV header -> (db column, SQL type).
# Columns are matched by NAME, so their order in the CSV doesn't matter.
# To start tracking a new CSV column, add a line here; the table gets the
# column automatically on the next run (old rows stay NULL).
# Never delete a line here once data exists - if Barchart drops a column,
# leave the mapping in place and new rows just get NULL.
COLUMNS = {
    "Symbol":            ("symbol",          "VARCHAR(32)"),
    "Name":              ("name",            "VARCHAR(255)"),
    "Latest":            ("latest",          NUM),
    "Previous":          ("previous_close",  NUM),          # new
    "Change":            ("price_change",    NUM),
    "%Change":           ("pct_change",      NUM),
    "5D %Chg":           ("pct_change_5d",   NUM),          # new
    "1M %Chg":           ("pct_change_1m",   NUM),          # new
    "52W %Chg":          ("pct_change_52w",  NUM),          # new
    "YTD %Chg":          ("pct_change_ytd",  NUM),          # new
    "Open":              ("open_price",      NUM),
    "High":              ("high_price",      NUM),
    "Low":               ("low_price",       NUM),
    "14D RSI":           ("rsi14",           NUM),
    "20D RelVol":        ("rel_volume_20d",  NUM),          # new
    "Volume":            ("volume",          "BIGINT"),
    "52W Low":           ("low_52w",         NUM),          # new
    "52W High":          ("high_52w",        NUM),          # new
    "Std Dev":           ("std_dev",         NUM),          # new
    "Strike/1 Std Dev":  ("strike_1sd",      "VARCHAR(64)"),  # new
    "Strike/2 Std Dev":  ("strike_2sd",      "VARCHAR(64)"),  # new
    "IV":                ("iv",              NUM),          # new
    "IV Rank":           ("iv_rank",         NUM),          # new
    "IV Pctl":           ("iv_pctl",         NUM),          # new
    "Imp Vol":           ("imp_vol",         NUM),          # new
    "Time":              ("time_raw",        "VARCHAR(32)"),
    "Options":           ("options_raw",     "VARCHAR(64)"),
    "yahoo":             ("yahoo",           "VARCHAR(255)"),
    "Price mean20":      ("price_mean20",    NUM),
    "Price std20":       ("price_std20",     NUM),
    "Price zscore":      ("price_zscore",    NUM),
    "% mean20":          ("pct_mean20",      NUM),
    "% std20":           ("pct_std20",       NUM),
    "% zscore":          ("pct_zscore",      NUM),
}
# Derived from `Time`, not read from the CSV directly.
DERIVED = {
    "last_trade_date": "DATE",      # trading day of the quote
    "last_trade_utc":  "DATETIME",  # only when Barchart gives a time of day
}

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.csv$")
TIME_ET_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*ET$", re.I)
EMPTY = {"", "N/A", "NA", "-", "--"}


def parse_value(raw, sql_type):
    if raw is None:
        return None
    s = raw.strip()
    if s in EMPTY:
        return None
    if sql_type.startswith(("DECIMAL", "BIGINT")):
        if s.lower() == "unch":
            return 0
        s = s.replace(",", "").replace("%", "").replace("+", "")
        try:
            return int(float(s)) if sql_type == "BIGINT" else float(s)
        except ValueError:
            return None  # raw value is still in S3
    return s


def last_weekday(d):
    """Most recent Mon-Fri on or before d (doesn't know market holidays)."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def parse_time(raw, snapshot_date):
    """Barchart's Time column is either '16:00 ET' (time on the latest
    session) or a date like '2026-10-09' / '10/9/2026'.
    Returns (last_trade_date, last_trade_utc)."""
    if not raw or raw.strip() in EMPTY:
        return None, None
    s = raw.strip()
    m = TIME_ET_RE.match(s)
    if m:
        d = last_weekday(date.fromisoformat(snapshot_date))
        local = datetime(d.year, d.month, d.day, int(m[1]), int(m[2]), tzinfo=ET)
        return d, local.astimezone(timezone.utc).replace(tzinfo=None)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date(), None
        except ValueError:
            pass
    return None, None  # unknown format; time_raw keeps the original


# def connect():
#     return pymysql.connect(
#         host=os.environ["DB_HOST"],
#         user=os.environ["DB_USER"],
#         password=os.environ["DB_PASSWORD"],
#         database=DB_NAME,
#         ssl={"ca": os.environ.get("SSL_CA", "global-bundle.pem")},
#     )

def connect():
    host = os.environ["DB_HOST"]
    tunnel = host in ("127.0.0.1", "localhost")
    return pymysql.connect(
        host=host,
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=DB_NAME,
        ssl={"ca": os.environ.get("SSL_CA", "global-bundle.pem"),
             "check_hostname": not tunnel},
    )


def all_columns():
    return [(c, t) for c, t in COLUMNS.values()] + list(DERIVED.items())


def ensure_schema(cur):
    """Create the table if missing, and add any mapped columns it lacks."""
    cols = ",\n  ".join(
        f"`{c}` {t} NULL" for c, t in all_columns() if c != "symbol"
    )
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS `{TABLE}` (
          snapshot_date DATE NOT NULL,
          symbol VARCHAR(32) NOT NULL,
          {cols},
          extra JSON NULL,
          source_key VARCHAR(255) NOT NULL,
          loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (snapshot_date, symbol)
        )""")
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
        (DB_NAME, TABLE),
    )
    existing = {r[0] for r in cur.fetchall()}
    for c, t in all_columns():
        if c not in existing:
            print(f"Adding column `{c}` {t}")
            cur.execute(f"ALTER TABLE `{TABLE}` ADD COLUMN `{c}` {t} NULL")


def parse_csv(text, snapshot_date, key):
    """Turn one CSV into row dicts. Unmapped columns go into `extra` (JSON)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    reader.fieldnames = [h.strip() for h in reader.fieldnames]
    headers = reader.fieldnames

    unknown = [h for h in headers if h and h not in COLUMNS]
    missing = [h for h in COLUMNS if h not in headers]
    if unknown:
        print(f"WARNING {key}: unmapped columns {unknown} -> stored in `extra`")
    if missing:
        print(f"NOTE {key}: columns not in this file {missing} -> NULL")

    rows = []
    for r in reader:
        sym = (r.get("Symbol") or "").strip()
        if not sym or sym.lower().startswith("downloaded"):  # blank/footer lines
            continue
        rec = {"snapshot_date": snapshot_date, "symbol": sym}
        for h, (c, t) in COLUMNS.items():
            if c != "symbol":
                rec[c] = parse_value(r.get(h), t)
        rec["last_trade_date"], rec["last_trade_utc"] = parse_time(
            r.get("Time"), snapshot_date)
        extra = {h: r[h] for h in unknown if r.get(h) not in (None, "")}
        rec["extra"] = json.dumps(extra) if extra else None
        rec["source_key"] = key
        rows.append(rec)
    return rows


def insert_rows(cur, rows):
    if not rows:
        return 0
    cols = list(rows[0])
    col_sql = ", ".join(f"`{c}`" for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(
        f"`{c}`=VALUES(`{c}`)" for c in cols if c not in ("snapshot_date", "symbol")
    )
    sql = (f"INSERT INTO `{TABLE}` ({col_sql}) VALUES ({placeholders}) "
           f"ON DUPLICATE KEY UPDATE {updates}")
    cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


# ---- S3 ----

s3 = boto3.client("s3")


def read_key(key):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8-sig")


def all_keys():
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
        keys += [o["Key"] for o in page.get("Contents", []) if DATE_RE.search(o["Key"])]
    return sorted(keys)


def load_key(cur, key):
    d = DATE_RE.search(key).group(1)
    return insert_rows(cur, parse_csv(read_key(key), d, key))


# ---- entry points ----

def load_day(d):
    """Call this from the scraper after uploading today's CSV to S3."""
    key = f"{PREFIX}{d}.csv"
    conn = connect()
    with conn.cursor() as cur:
        ensure_schema(cur)
        n = load_key(cur, key)
    conn.commit()
    conn.close()
    print(f"[{ENV}] {key}: {n} rows")


def rebuild():
    if ENV == "prod" and "--yes" not in sys.argv:
        sys.exit("Refusing to rebuild prod without --yes")
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
        ensure_schema(cur)
        keys = all_keys()
        total = 0
        for key in keys:
            total += load_key(cur, key)
            conn.commit()
    conn.close()
    print(f"[{ENV}] rebuilt from {len(keys)} files, {total} rows")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "daily" and len(sys.argv) > 2:
        load_day(sys.argv[2])
    elif cmd == "rebuild":
        rebuild()
    else:
        sys.exit(__doc__)