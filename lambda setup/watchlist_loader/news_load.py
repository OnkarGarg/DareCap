"""Load news-collector JSON from S3 into MySQL.

S3 layout: raw/news/<ny_date>/<run_ts>.json        (prod)   -> news, news_symbols
           raw/etf_holdings/<ny_date>.json          (prod)   -> etf_holdings
           test/news/..., test/etf_holdings/...     (test)

news          one row per Barchart article id. Re-loading keeps the stored row, but
              fills body/body_status if the stored body is still NULL.
news_symbols  (article_id, symbol, relation); INSERT IGNORE, so links seen in later
              runs are still added.
etf_holdings  (as_of_date, etf, symbol); upsert.

Usage:
    python news_load.py file <s3 key>
    python news_load.py rebuild [--yes]
"""
import json
import re
import sys
from datetime import datetime, timezone

import load

ROOT = "raw/" if load.ENV == "prod" else "test/"
NEWS_PREFIX = f"{ROOT}news/"
HOLDINGS_PREFIX = f"{ROOT}etf_holdings/"
NEWS_KEY_RE = re.compile(r"news/(\d{4}-\d{2}-\d{2})/(\d{8}T\d{6})\.json$")
HOLDINGS_KEY_RE = re.compile(r"etf_holdings/(\d{4}-\d{2}-\d{2})\.json$")


def handles(key):
    return ((key.startswith(NEWS_PREFIX) and NEWS_KEY_RE.search(key) is not None)
            or (key.startswith(HOLDINGS_PREFIX) and HOLDINGS_KEY_RE.search(key) is not None))


def ensure_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `news` (
          id BIGINT UNSIGNED NOT NULL,
          title VARCHAR(1024) NOT NULL,
          slug VARCHAR(512) NULL,
          source VARCHAR(128) NULL,
          published_utc DATETIME NULL,
          published_raw VARCHAR(64) NULL,
          url VARCHAR(1024) NULL,
          excerpt TEXT NULL,
          body MEDIUMTEXT NULL,
          body_status VARCHAR(128) NULL,
          first_seen_utc DATETIME NOT NULL,
          source_key VARCHAR(255) NOT NULL,
          loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (id),
          INDEX idx_published (published_utc)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `news_symbols` (
          article_id BIGINT UNSIGNED NOT NULL,
          symbol VARCHAR(32) NOT NULL,
          relation ENUM('fetched_for', 'tagged') NOT NULL,
          PRIMARY KEY (article_id, symbol, relation),
          INDEX idx_symbol (symbol)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS `etf_holdings` (
          as_of_date DATE NOT NULL,
          etf VARCHAR(32) NOT NULL,
          symbol VARCHAR(32) NOT NULL,
          holding_rank SMALLINT NOT NULL,
          symbol_name VARCHAR(255) NULL,
          weight_pct DECIMAL(10,6) NULL,
          shares_held BIGINT NULL,
          symbol_type TINYINT NULL,
          source_key VARCHAR(255) NOT NULL,
          loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (as_of_date, etf, symbol),
          INDEX idx_symbol (symbol, as_of_date)
        )""")


# ---- parsing ----

def _utc(iso):
    """ISO string with offset -> naive UTC datetime."""
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone(timezone.utc).replace(tzinfo=None)


def _clip(s, n):
    return s[:n] if isinstance(s, str) else s


def parse_news(payload, key):
    run_utc = _utc(payload["run_utc"])
    articles, links = [], []
    for a in payload.get("articles", []):
        aid = int(a["id"])
        articles.append((
            aid, _clip(a["title"], 1024), _clip(a.get("slug"), 512), _clip(a.get("source"), 128),
            _utc(a.get("published_utc")), _clip(a.get("published_raw"), 64), _clip(a.get("url"), 1024),
            a.get("excerpt"), a.get("body"), _clip(a.get("body_status"), 128), run_utc, key,
        ))
        links += [(aid, _clip(s, 32), "fetched_for") for s in a.get("fetched_for", [])]
        links += [(aid, _clip(s, 32), "tagged") for s in a.get("tagged_symbols", [])]
    return articles, links


def parse_holdings(payload, as_of, key):
    rows = []
    for etf, holdings in payload.get("holdings", {}).items():
        rank = 0
        for h in holdings:
            if not h.get("symbol"):
                continue  # cash / unnamed lines
            rank += 1
            st = h.get("symbolType")
            rows.append((as_of, _clip(etf, 32), _clip(h["symbol"], 32), rank,
                         _clip(h.get("symbolName"), 255), h.get("percent"), h.get("sharesHeld"),
                         int(st) if st is not None else None, key))
    return rows


# ---- inserts ----

NEWS_SQL = """
    INSERT INTO `news` (id, title, slug, source, published_utc, published_raw, url,
                        excerpt, body, body_status, first_seen_utc, source_key)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
      body_status = IF(body IS NULL, VALUES(body_status), body_status),
      body        = IF(body IS NULL, VALUES(body), body)"""
# body_status is assigned first because MySQL applies assignments left to right

LINK_SQL = "INSERT IGNORE INTO `news_symbols` (article_id, symbol, relation) VALUES (%s, %s, %s)"

HOLDINGS_SQL = """
    INSERT INTO `etf_holdings` (as_of_date, etf, symbol, holding_rank, symbol_name,
                                weight_pct, shares_held, symbol_type, source_key)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
      holding_rank = VALUES(holding_rank), symbol_name = VALUES(symbol_name),
      weight_pct = VALUES(weight_pct), shares_held = VALUES(shares_held),
      symbol_type = VALUES(symbol_type), source_key = VALUES(source_key)"""


def load_key(cur, key):
    payload = json.loads(load.read_key(key))
    m = HOLDINGS_KEY_RE.search(key)
    if m:
        rows = parse_holdings(payload, m.group(1), key)
        if rows:
            cur.executemany(HOLDINGS_SQL, rows)
        return f"{len(rows)} holdings"
    articles, links = parse_news(payload, key)
    if articles:
        cur.executemany(NEWS_SQL, articles)
    if links:
        cur.executemany(LINK_SQL, links)
    return f"{len(articles)} articles, {len(links)} symbol links"


def load_file(key):
    conn = load.connect()
    with conn.cursor() as cur:
        ensure_schema(cur)
        n = load_key(cur, key)
    conn.commit()
    conn.close()
    print(f"[{load.ENV}] {key}: {n}")


def rebuild():
    if load.ENV == "prod" and "--yes" not in sys.argv:
        sys.exit("Refusing to rebuild prod without --yes")
    keys = []
    for prefix, rx in ((HOLDINGS_PREFIX, HOLDINGS_KEY_RE), (NEWS_PREFIX, NEWS_KEY_RE)):
        for page in load.s3.get_paginator("list_objects_v2").paginate(Bucket=load.BUCKET, Prefix=prefix):
            keys += sorted(o["Key"] for o in page.get("Contents", []) if rx.search(o["Key"]))
    # news keys sort by date then run time, so first_seen_utc comes from the earliest run
    conn = load.connect()
    with conn.cursor() as cur:
        for t in ("news_symbols", "news", "etf_holdings"):
            cur.execute(f"DROP TABLE IF EXISTS `{t}`")
        ensure_schema(cur)
        for key in keys:
            load_key(cur, key)
            conn.commit()
    conn.close()
    print(f"[{load.ENV}] rebuilt news tables from {len(keys)} files")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "file" and len(sys.argv) > 2:
        load_file(sys.argv[2])
    elif cmd == "rebuild":
        rebuild()
    else:
        sys.exit(__doc__)