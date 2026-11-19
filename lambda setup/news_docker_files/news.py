"""
News collector for the Barchart watchlist.

Flow:
  1. Load the Barchart session (same storage_state + UA as the scraper).
  2. Read the watchlist symbols and their symbolType (1 = stock, 7 = ETF).
  3. For each ETF, take its top-N holdings by weight (EtfConstituents).
  4. Fetch news once per unique symbol (watchlist + holdings), paging back
     until the last LOOKBACK_MIN minutes are covered (hourly run + overlap).
  5. Dedupe articles by id and record which fetched symbols each came from.
  6. Archive one JSON file per run to S3: raw/news/<date>/<run_ts>.json
     Holdings are fetched once per NY date and cached in raw/etf_holdings/<date>.json.

Lambda: barchart-news, own image (lambda setup/news_files). Handler news.lambda_handler.
        Env: STATE_BUCKET, ARCHIVE_BUCKET, optional STATE_KEY / NEWS_PREFIX / HOLDINGS_PREFIX.
        Deploy: lambda setup/news_files/deploy.ps1
Local:  python news.py --state state.json   (writes to ./news_out/ instead of S3)
"""
import argparse
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

BASE = "https://www.barchart.com"
WATCHLIST_ID = 569527
DAILY_WATCHLIST_URL = f"{BASE}/my/watchlist?watchlist={WATCHLIST_ID}&viewName=main"
NEWS_URL = f"{BASE}/news/load-more-stories"
HOLDINGS_URL = f"{BASE}/proxies/core-api/v1/EtfConstituents"
QUOTES_URL = f"{BASE}/proxies/core-api/v1/quotes/get"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

STOCK, ETF = 1, 7
TOP_HOLDINGS = int(os.environ.get("NEWS_TOP_HOLDINGS", "10"))
LOOKBACK_MIN = int(os.environ.get("NEWS_LOOKBACK_MIN", "75"))  # hourly run + 15 min overlap
MAX_PAGES = int(os.environ.get("NEWS_MAX_PAGES", "3"))
REQUEST_DELAY = float(os.environ.get("NEWS_REQUEST_DELAY", "1.0"))
MAX_RETRIES = int(os.environ.get("NEWS_MAX_RETRIES", "5"))
FETCH_BODIES = os.environ.get("NEWS_FETCH_BODIES", "1") == "1"
MAX_BODIES = int(os.environ.get("NEWS_MAX_BODIES", "500"))
SAMPLE_DIR = os.environ.get("NEWS_SAMPLE_DIR")  # local only: save one raw article page for checking

NEWS_PREFIX = os.environ.get("NEWS_PREFIX", "raw/news/")
HOLDINGS_PREFIX = os.environ.get("HOLDINGS_PREFIX", "raw/etf_holdings/")

CHICAGO = ZoneInfo("America/Chicago")   # Barchart prints CST/CDT
DROP_HEADERS = ("cookie", "host", "content-length")


# ---------- parsing ----------

_RELATIVE = re.compile(r"^(\d+|an?|one)\s+(second|minute|min|hour|hr|day)s?\s+ago$", re.I)
_UNIT = {"second": "seconds", "minute": "minutes", "min": "minutes",
         "hour": "hours", "hr": "hours", "day": "days"}
_unparsed_seen = set()


def parse_published(s, now_utc):
    """Barchart time string -> UTC datetime, or None if the format is unknown.
    Handles 'Fri Nov 13, 4:04PM CST' (no year: take the latest year that isn't
    in the future), '6 minutes ago', and 'just now'."""
    s = (s or "").strip()
    if s.lower() in ("just now", "now"):
        return now_utc
    m = _RELATIVE.match(s)
    if m:
        n = 1 if m.group(1).lower() in ("a", "an", "one") else int(m.group(1))
        return now_utc - timedelta(**{_UNIT[m.group(2).lower()]: n})

    # older years: 'Wed Jul 23, 2025' (date only, no time)
    try:
        dt = datetime.strptime(s, "%a %b %d, %Y")
        return dt.replace(tzinfo=CHICAGO).astimezone(timezone.utc)
    except ValueError:
        pass

    # current year: 'Fri Nov 13, 4:04PM CST' (no year)
    core = re.sub(r"\s+(CST|CDT)$", "", s)
    for year in (now_utc.year, now_utc.year - 1):
        try:
            dt = datetime.strptime(f"{year} {core}", "%Y %a %b %d, %I:%M%p")
        except ValueError:
            break
        utc = dt.replace(tzinfo=CHICAGO).astimezone(timezone.utc)
        if utc <= now_utc + timedelta(hours=1):
            return utc

    if s not in _unparsed_seen:
        _unparsed_seen.add(s)
        print(f"  unparsed published time: {s!r}")
    return None


def _clean(t):
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def _norm(t):
    return _clean(t).lower()


_STORY_SPLIT = re.compile(r'<div class="story clearfix')
_LINK = re.compile(r'<a href="([^"]+)"[^>]*class="story-link"[^>]*>(.*?)</a>', re.S)
_EXCERPT = re.compile(r'<p class="story-excerpt[^"]*">(.*?)</p>', re.S)
_SYMBOL = re.compile(r'class="story-symbol[^"]*".*?<a[^>]*href="/[^"]*/quotes/([^"/]+)"', re.S)


def parse_content(content):
    """Map normalized title -> {url, excerpt, tagged_symbols} from the HTML block."""
    out = {}
    for block in _STORY_SPLIT.split(content)[1:]:
        m = _LINK.search(block)
        if not m:
            continue
        url, title = m.group(1).replace("\\/", "/"), m.group(2)
        ex = _EXCERPT.search(block)
        out[_norm(title)] = {
            "url": url,
            "excerpt": _clean(ex.group(1)) if ex else None,
            "tagged_symbols": sorted(set(_SYMBOL.findall(block))),
        }
    return out


def parse_news_payload(payload, now_utc):
    items = json.loads(payload["items"]) if isinstance(payload["items"], str) else payload["items"]
    extra = parse_content(payload.get("content", ""))
    rows = []
    for it in items:
        e = extra.get(_norm(it["title"]), {})
        pub = parse_published(it.get("published"), now_utc)
        rows.append({
            "id": it["id"],
            "slug": it["slug"],
            "title": html.unescape(it["title"]),
            "source": it.get("feedName"),
            "published_raw": it.get("published"),
            "published_utc": pub.isoformat() if pub else None,
            "url": e.get("url") or f"{BASE}/story/news/{it['id']}/{it['slug']}",
            "excerpt": e.get("excerpt"),
            "tagged_symbols": e.get("tagged_symbols", []),
        })
    return rows, payload.get("timestamp")


# ---------- Barchart calls ----------

def request(ctx, method, url, label, **kw):
    """ctx.request.get/post with spacing and backoff on 429/5xx. Returns response or None."""
    wait = 5
    for attempt in range(MAX_RETRIES + 1):
        time.sleep(REQUEST_DELAY)
        r = getattr(ctx.request, method)(url, **kw)
        if r.ok:
            return r
        if r.status == 429 or r.status >= 500:
            if attempt == MAX_RETRIES:
                break
            ra = r.headers.get("retry-after")
            delay = int(ra) if ra and ra.isdigit() else wait
            print(f"  {label}: HTTP {r.status}, retry in {delay}s")
            time.sleep(delay)
            wait = min(wait * 2, 120)
            continue
        break
    print(f"  {label}: HTTP {r.status}, giving up")
    return None


def clean_headers(h):
    return {k: v for k, v in h.items() if not k.startswith(":") and k.lower() not in DROP_HEADERS}


def capture_session(page):
    """Load the watchlist page (symbols + core-api headers) and a news page
    (load-more headers). Returns (symbols, api_headers, news_headers)."""
    with page.expect_request(
        lambda r: "/proxies/core-api/v1/quotes/get" in r.url and "isWatchlist=true" in r.url,
        timeout=45_000,
    ) as req_info:
        page.goto(DAILY_WATCHLIST_URL, wait_until="domcontentloaded")
    boot = json.loads(page.locator("#bc-wl-v2-bootstrap").text_content())
    if boot.get("currentWatchlistId") != WATCHLIST_ID:
        raise RuntimeError(f"Loaded watchlist {boot.get('currentWatchlistId')}, expected {WATCHLIST_ID}")
    symbols = parse_qs(urlparse(req_info.value.url).query)["symbols"][0].split(",")
    api_headers = clean_headers(req_info.value.all_headers())

    page.goto(f"{BASE}/stocks/quotes/AAPL/news", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    with page.expect_request(lambda r: "/news/load-more-stories" in r.url, timeout=30_000) as news_req:
        for _ in range(6):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(1500)
    news_headers = clean_headers(news_req.value.all_headers())
    return [s for s in symbols if s], api_headers, news_headers


def get_symbol_types(ctx, headers, symbols):
    types = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        r = request(ctx, "get", QUOTES_URL, "quotes", headers=headers, params={
            "symbols": ",".join(chunk), "fields": "symbol,symbolName,symbolType", "raw": "1"})
        if r is None:
            raise RuntimeError("quotes failed after retries")
        for row in r.json().get("data", []):
            raw = row.get("raw", row)
            types[raw["symbol"]] = {"type": int(raw.get("symbolType") or 0), "name": raw.get("symbolName")}
    return types


def get_holdings(ctx, headers, etf, top_n=TOP_HOLDINGS):
    r = request(ctx, "get", HOLDINGS_URL, f"holdings {etf}", headers=headers, params={
        "composite": etf,
        "fields": "symbol,symbolName,percent,sharesHeld,symbolCode,symbolType",
        "orderBy": "percent", "orderDir": "desc",
        "page": "1", "limit": str(top_n), "raw": "1",
    })
    if r is None:
        return None  # None = failed; [] = ETF has no holdings listed
    return [row.get("raw", row) for row in r.json().get("data", [])]


def get_news(ctx, headers, symbol, symbol_type, since_utc, now_utc):
    """Page back from now until older than since_utc or MAX_PAGES."""
    rows, before = [], int(now_utc.timestamp())
    for _ in range(MAX_PAGES):
        body = {"before": before, "section": "quotes", "subSection": symbol.lower(),
                "search": [], "useThumbnail": True, "symbolType": str(symbol_type)}
        r = request(ctx, "post", NEWS_URL, f"news {symbol}", headers=headers, data=json.dumps(body))
        if r is None:
            break
        page_rows, cursor = parse_news_payload(r.json(), now_utc)
        rows.extend(page_rows)
        if not page_rows or not cursor or cursor <= since_utc.timestamp() or cursor >= before:
            break
        before = cursor
    # unparsed times (None) are dropped; parse_published logs each new format once
    return [x for x in rows if x["published_utc"] and x["published_utc"] >= since_utc.isoformat()]


def get_article_body(ctx, url, save_sample=False):
    """Fetch the article page and extract the main text. Returns (text, status)."""
    import trafilatura  # lazy: only needed when bodies are on

    headers = {"referer": BASE + "/news",
               "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    r = request(ctx, "get", url, f"article {url.rsplit('/', 2)[-2]}", headers=headers)
    if r is None:
        return None, "http_error"
    page = r.text()
    if save_sample and SAMPLE_DIR:
        os.makedirs(SAMPLE_DIR, exist_ok=True)
        with open(os.path.join(SAMPLE_DIR, "sample_article.html"), "w", encoding="utf-8") as f:
            f.write(page)
    if urlparse(r.url).netloc and "barchart.com" not in urlparse(r.url).netloc:
        return None, f"external:{urlparse(r.url).netloc}"
    text = trafilatura.extract(page, include_comments=False, include_tables=True, favor_precision=True)
    if not text:
        return None, "no_text"
    return text, "ok"


# ---------- run ----------

class S3Store:
    def __init__(self, s3, bucket):
        self.s3, self.bucket = s3, bucket

    def get_json(self, key):
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except self.s3.exceptions.NoSuchKey:
            return None

    def put(self, key, body):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")


class LocalStore:
    def __init__(self, root):
        self.root = root

    def get_json(self, key):
        path = os.path.join(self.root, key)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def put(self, key, body):
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)


def ny_date():
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def collect(session_path, archive_store, headless=True):
    now_utc = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(minutes=LOOKBACK_MIN)
    print(f"Window: {since_utc.isoformat()} -> {now_utc.isoformat()}")

    with sync_playwright() as p:
        # container flags only on Lambda; --single-process crashes Chromium on Windows
        on_lambda = "AWS_LAMBDA_FUNCTION_NAME" in os.environ
        args = (["--no-sandbox", "--single-process", "--disable-gpu", "--disable-dev-shm-usage"]
                if on_lambda else [])
        browser = p.chromium.launch(headless=headless, args=args)
        ctx = browser.new_context(storage_state=session_path, user_agent=UA)
        page = ctx.new_page()

        watchlist, api_headers, news_headers = capture_session(page)
        types = get_symbol_types(ctx, api_headers, watchlist)
        etfs = [s for s in watchlist if types.get(s, {}).get("type") == ETF]
        print(f"Watchlist: {len(watchlist)} symbols, {len(etfs)} ETFs")

        # ETF look-through, cached once per NY date
        hold_key = f"{HOLDINGS_PREFIX}{ny_date()}.json"
        cached = archive_store.get_json(hold_key)
        holdings = (cached or {}).get("holdings", {})
        missing = [s for s in etfs if s not in holdings]
        holdings_failed = []
        for s in missing:
            rows = get_holdings(ctx, api_headers, s)
            if rows is None:
                holdings_failed.append(s)
            else:
                holdings[s] = rows
        if missing:
            archive_store.put(hold_key, json.dumps(
                {"run_utc": now_utc.isoformat(), "holdings": holdings}, indent=1).encode())
        print(f"Holdings: {len(holdings)} ETFs ({len(missing) - len(holdings_failed)} fetched now), "
              f"failed: {holdings_failed}")

        # fetch list: watchlist stocks/ETFs + stock holdings of watchlist ETFs
        fetch = {s: types[s]["type"] for s in watchlist if types.get(s, {}).get("type") in (STOCK, ETF)}
        for etf in etfs:
            for h in holdings.get(etf, []):
                if int(h.get("symbolType") or 0) == STOCK and h.get("symbol"):
                    fetch.setdefault(h["symbol"], STOCK)
        skipped = [s for s in watchlist if s not in fetch]
        print(f"Fetching news for {len(fetch)} symbols; skipped (not stock/ETF): {skipped}")

        articles, hits = {}, {}  # id -> article, id -> set(fetched symbols)
        for i, (sym, st) in enumerate(sorted(fetch.items()), 1):
            for a in get_news(ctx, news_headers, sym, st, since_utc, now_utc):
                articles.setdefault(a["id"], a)
                hits.setdefault(a["id"], set()).add(sym)
            if i % 50 == 0:
                print(f"  {i}/{len(fetch)} symbols, {len(articles)} articles in window")

        for aid, a in articles.items():
            a["fetched_for"] = sorted(hits[aid])

        # bodies, newest first, capped
        ordered = sorted(articles.values(), key=lambda a: a["published_utc"] or "", reverse=True)
        statuses = {}
        for i, a in enumerate(ordered):
            if not FETCH_BODIES:
                a["body"], a["body_status"] = None, "disabled"
            elif i >= MAX_BODIES:
                a["body"], a["body_status"] = None, "skipped_cap"
            else:
                a["body"], a["body_status"] = get_article_body(ctx, a["url"], save_sample=(i == 0))
            k = a["body_status"].split(":")[0]
            statuses[k] = statuses.get(k, 0) + 1
        print(f"Articles: {len(articles)} in window; bodies: {statuses}")

        browser.close()   # session file is not written back; the daily scraper owns it

    result = {
        "run_utc": now_utc.isoformat(),
        "since_utc": since_utc.isoformat(),
        "watchlist": watchlist,
        "symbol_types": types,
        "holdings_key": hold_key,
        "holdings_failed": holdings_failed,
        "skipped": skipped,
        "articles": sorted(articles.values(), key=lambda a: a["published_utc"] or "9999", reverse=True),
    }
    return result


def run(session_path, archive_store, headless=True):
    result = collect(session_path, archive_store, headless=headless)
    run_ts = result["run_utc"][:19].replace(":", "").replace("-", "")
    news_key = f"{NEWS_PREFIX}{ny_date()}/{run_ts}.json"
    archive_store.put(news_key, json.dumps(result, indent=1).encode())
    print(f"Archived {len(result['articles'])} articles -> {news_key}")
    return {"status": "ok", "articles": len(result["articles"]),
            "news_key": news_key, "holdings_key": result["holdings_key"]}


def news_job(s3, state_bucket, state_key, archive_bucket, session_path="/tmp/storage_state.json"):
    """Lambda entry point."""
    s3.download_file(state_bucket, state_key, session_path)
    return run(session_path, S3Store(s3, archive_bucket))


def lambda_handler(event, context):
    """Use as the handler if this is deployed as its own function."""
    import boto3
    return news_job(boto3.client("s3"), os.environ["STATE_BUCKET"],
                    os.environ.get("STATE_KEY", "barchart/storage_state.json"),
                    os.environ["ARCHIVE_BUCKET"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state.json", help="Barchart session (storage_state) file")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--out", default="news_out", help="local stand-in for the S3 buckets")
    args = ap.parse_args()

    SAMPLE_DIR = SAMPLE_DIR or args.out
    print(run(args.state, LocalStore(args.out), headless=not args.headed))