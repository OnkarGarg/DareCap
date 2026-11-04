import json
import os
import io
from urllib.parse import unquote, urlparse, parse_qs
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

import yfinance as yf
import pandas as pd

# here is the command you need to run to update the lambda function on AWS
# cd "C:\Users\onkar\DareCap\lambda setup\Docker_files"

# $REGION = "eu-north-1"
# $ACCT = aws sts get-caller-identity --query Account --output text
# $REGISTRY = "$ACCT.dkr.ecr.$REGION.amazonaws.com"
# $REPO = "$REGISTRY/barchart_scraper"

# aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY
# docker build --platform linux/amd64 --provenance=false -t "${REPO}:latest" .
# docker push "${REPO}:latest"

# aws lambda update-function-code --function-name barchart-scraper --image-uri "${REPO}:latest" --region $REGION


# ---- config (set as Lambda environment variables) ----
STATE_BUCKET = os.environ["STATE_BUCKET"]                     # private S3 bucket holding the Barchart session
STATE_KEY = os.environ.get("STATE_KEY", "barchart/storage_state.json")
TICKER_CACHE = os.environ.get("TICKER_CACHE", "barchart/ticker_cache.json")
TOKEN_PARAM = os.environ.get("TOKEN_PARAM", "/scraper/gdrive-token")  # SSM SecureString
FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
ARCHIVE_FOLDER_ID = os.environ["ARCHIVE_DRIVE_FOLDER_ID"]

WATCHLIST_ID = 569527
DAILY_WATCHLIST_URL = f"https://www.barchart.com/my/watchlist?watchlist={WATCHLIST_ID}&viewName=main"
DOWNLOAD_URL = "https://www.barchart.com/my/download"
CORE_API = "https://www.barchart.com/proxies/core-api/v1/quotes/get"
EARNINGS_PAGE = "https://www.barchart.com/stocks/earnings-dividends"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# must match the UA used in setup_local.py when the session was created
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# Lambda: only /tmp is writable
STATE_PATH = "/tmp/storage_state.json"
CACHE_PATH = "/tmp/ticker_cache.json"
CSV_PATH = "/tmp/watchlist.csv"

# so in the future if you want to find the api field,
# what you are supposed to do is to add that field to a test view,
# inspect the page, go to the network tab and then filter for "core-api"
# and the reload the page and the network activity that is captured contains the field name

FORM = {
    "fileName": "Watchlist Daily-Intraday",
    "meta": "symbol.normalized,field.shortName,field.type",
    "isWatchlist": "true",
    "symbols": ",",
    "method": "quotes",
    
    "fields": "symbol,symbolName,lastPrice,previousPrice,priceChange,"
    "percentChange,percentChange5d,percentChange1m,percentChange1y,percentChangeYtd,"
    "openPrice,highPrice,lowPrice,relativeStrength14d,relativeVolume20d,volume,lowPrice1y,highPrice1y,"
    "standardDeviation,optionsStrikeWithin1StdDev,optionsStrikeWithin2StdDev,"
    "optionsVolatility,optionsImpliedVolatilityRank1y,optionsImpliedVolatilityPercentile1y,"
    "optionsWeightedImpliedVolatility,tradeTime,raw,symbolCode,updatedFields,lastPriceDirection,hasOptions",

    "page": "1",
    "limit": "5000",
    "hasOptions": "true",
    "exclude": "customLinks,symbolType",
    "customGetParameters": "",
    "pageTitle": " Watchlist  ",
    "symbolNotes": "{}",
}

CALENDAR_PARAMS = {
    "lists": "stocks.earnings_calendar.us.overall",
    "fields": "symbol,symbolName,timeCode,estimatedEarnings,lastReportedEarnings,"
        "lastEarningsSurpriseAmount,lastEarningsSurprisePercent,symbolCode,symbolType,hasOptions",
    "meta": "field.type,field.shortName,description,field.description,lists.lastUpdate",
    "orderBy": "timeCode",
    "orderDir": "desc",
    "hasOptions": "true",
    "raw": "1",
}

s3 = boto3.client("s3")
ssm = boto3.client("ssm")
ARCHIVE_BUCKET = os.environ["ARCHIVE_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "raw/watchlist/")
EARNINGS_PREFIX = os.environ.get("EARNINGS_PREFIX", "raw/earnings_calendar/")
EARNINGS_DAYS_AHEAD = 15

PAGE_LIMIT = 100

def earnings_dates(today):
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    ahead = [today + timedelta(days=i) for i in range(EARNINGS_DAYS_AHEAD + 1)]
    return [prev] + [d for d in ahead if d.weekday() < 5]

def fetch_core_api(ctx, headers, params):
    rows, n = [], 1
    while True:
        resp = ctx.request.get(CORE_API, params={**params, "page": str(n), "limit": str(PAGE_LIMIT)},
                               headers=headers)
        if not resp.ok:
            raise RuntimeError(f"core-api failed on page {n}: HTTP {resp.status} {resp.text()[:300]}")
        payload = resp.json()
        data = payload.get("data", [])
        rows.extend(data)
        if len(data) < PAGE_LIMIT:
            return rows
        n += 1

def scrape():
    # pull the saved login session (cookies + localStorage) from S3
    s3.download_file(STATE_BUCKET, STATE_KEY, STATE_PATH)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--no-sandbox", "--single-process",
            "--disable-gpu", "--disable-dev-shm-usage",
        ])
        ctx = browser.new_context(storage_state=STATE_PATH, user_agent=UA)
        page = ctx.new_page()

        # page.goto(DAILY_WATCHLIST_URL, wait_until="domcontentloaded")
        # try:
        #     # networkidle can hang on pages with live quote polling; wait for the table instead
        #     table = page.wait_for_selector("div.bc-datatable[data-api-config]",
        #                                    state="attached", timeout=30_000)
        # except PWTimeout:
        #     ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        #     s3.put_object(Bucket=STATE_BUCKET, Key=f"debug/{ts}.png",
        #                   Body=page.screenshot(full_page=True))
        #     s3.put_object(Bucket=STATE_BUCKET, Key=f"debug/{ts}.html",
        #                   Body=page.content().encode())
        #     title = page.title()
        #     print(f"TITLE: {title!r}  debug/{ts}.*")
        #     logged_out = ("login" in page.url
        #                   or page.locator("a[href*='/login']").count() > 0)
        #     if logged_out:
        #         raise RuntimeError("Barchart session expired - rerun setup_local.py and re-upload state")
        #     raise RuntimeError(f"Watchlist table not found (blocked or layout changed). "
        #                        f"Title: {title!r} URL: {page.url}")

        # api_config = json.loads(table.get_attribute("data-api-config"))
        # form = {**FORM, "symbols": api_config["api"]["symbols"]}

        try:
            with page.expect_request(
                lambda r: "/proxies/core-api/v1/quotes/get" in r.url and "isWatchlist=true" in r.url,
                timeout=45_000,
            ) as req_info:
                page.goto(DAILY_WATCHLIST_URL, wait_until="domcontentloaded")
        except PWTimeout:
            ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            s3.put_object(Bucket=STATE_BUCKET, Key=f"debug/{ts}.png",
                          Body=page.screenshot(full_page=True))
            s3.put_object(Bucket=STATE_BUCKET, Key=f"debug/{ts}.html",
                          Body=page.content().encode())
            logged_out = page.evaluate(
                "!document.documentElement.classList.contains('bc-authenticated')")
            if logged_out:
                raise RuntimeError("Barchart session expired - rerun setup_local.py and re-upload state")
            raise RuntimeError(f"Watchlist quote request not seen (blocked or layout changed). "
                               f"Title: {page.title()!r} debug/{ts}.*")

        # guard against Barchart silently falling back to a different watchlist again
        boot = json.loads(page.locator("#bc-wl-v2-bootstrap").text_content())
        if boot.get("currentWatchlistId") != WATCHLIST_ID:
            raise RuntimeError(f"Loaded watchlist {boot.get('currentWatchlistId')}, expected {WATCHLIST_ID}. "
                               f"Available: {boot.get('watchlists')}")

        symbols = parse_qs(urlparse(req_info.value.url).query)["symbols"][0]
        print(f"Watchlist {WATCHLIST_ID}: {symbols.count(',') + 1} symbols")
        form = {**FORM, "symbols": symbols}

        headers = {"Referer": DAILY_WATCHLIST_URL}
        xsrf = next((c["value"] for c in ctx.cookies() if c["name"] == "XSRF-TOKEN"), None)
        if xsrf:
            headers["X-XSRF-TOKEN"] = unquote(xsrf)

        resp = ctx.request.post(DOWNLOAD_URL, form=form, headers=headers)
        body = resp.body()
        if not resp.ok or body.lstrip()[:1] == b"<":
            raise RuntimeError(f"Download failed: HTTP {resp.status}, got HTML instead of CSV")

        with page.expect_request(lambda r: "/proxies/core-api/" in r.url) as req_info:
            page.goto(EARNINGS_PAGE, wait_until="domcontentloaded")
        captured = req_info.value.all_headers()
        api_headers = {k: v for k, v in captured.items()
            if not k.startswith(":") and k.lower() not in ("cookie", "host", "content-length")}

        today = datetime.now(ZoneInfo("America/New_York")).date()
        calendars = {}
        for d in earnings_dates(today):
            rows = fetch_core_api(ctx, api_headers,
                                  {**CALENDAR_PARAMS, "startDate": d.isoformat(), "endDate": d.isoformat()})
            calendars[d.isoformat()] = rows
            print(f"Earnings {d}: {len(rows)} rows")

        # save refreshed cookies so the session keeps rolling forward
        ctx.storage_state(path=STATE_PATH)
        browser.close()

    s3.upload_file(STATE_PATH, STATE_BUCKET, STATE_KEY)
    archive_earnings(today, calendars)
    return body


def zscore(body):
    df = pd.read_csv(io.BytesIO(body))
    df = df[df["Name"].notna()].copy()
    symbols = df["Symbol"]
    symbols_to_read = []
    new_reads = []
    names = df.set_index("Symbol")["Name"]

    try:
        s3.download_file(STATE_BUCKET, TICKER_CACHE, CACHE_PATH)
        with open(CACHE_PATH) as f:
            ticker_cache = json.load(f)
    except ClientError:
        ticker_cache = {}

    for ticker in symbols:
        if ticker not in ticker_cache:
            new_reads.append(ticker)

    if new_reads:
        hist_data = yf.download(new_reads, period="3mo", progress=False, auto_adjust=True)
        hist_close = hist_data["Close"]
        failed = hist_close.columns[hist_close.isna().all()].tolist()
        ok = [t for t in new_reads if t not in failed]
        for ticker in ok:
            ticker_cache[ticker] = ticker
        for ticker in failed:
            company_name = names[ticker]
            quotes = yf.Search(company_name, max_results=5).quotes
            expected = {"INDEX"} if ticker.startswith("$") else {"EQUITY", "ETF"}
            match = next((q["symbol"] for q in quotes if q.get("quoteType") in expected), None)
            if match is None:
                print(f"Could not resolve {ticker} ({company_name})")
                continue
            print(f"Matched {ticker} ({company_name}) to {match}")
            ticker_cache[ticker] = match

    with open(CACHE_PATH, "w") as f:
        json.dump(ticker_cache, f)
    s3.upload_file(CACHE_PATH, STATE_BUCKET, TICKER_CACHE)

    for ticker in symbols:
        if ticker in ticker_cache:
            symbols_to_read.append(ticker_cache[ticker])

    hist_data = yf.Tickers(symbols_to_read).history(period="2mo", progress=False)
    today = pd.Timestamp.now(tz="America/New_York").date()
    close = hist_data["Close"]
    df["%Change"] = pd.to_numeric(
        df["%Change"].astype(str).str.replace(r"[%,+]", "", regex=True),
        errors="coerce",
    )
    close = close[close.index.date < today]
    pct = close.pct_change(fill_method=None) * 100

    close20 = close.tail(20)
    pct20 = pct.tail(20)

    price_means = close20.mean()
    price_stds = close20.std()
    pct_means = pct20.mean()
    pct_stds = pct20.std()
    
    df["yahoo"] = df["Symbol"].map(ticker_cache)
    df["Price mean20"] = df["yahoo"].map(price_means)
    df["Price std20"] = df["yahoo"].map(price_stds)
    df["Price zscore"] = (df["Latest"] - df["Price mean20"]) / df["Price std20"]
    df["% mean20"] = df["yahoo"].map(pct_means)
    df["% std20"] = df["yahoo"].map(pct_stds)
    df["% zscore"] = (df["%Change"] - df["% mean20"]) / df["% std20"]

    return df


def archive_to_s3(csv_bytes: bytes) -> str:
    d = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    key = f"{S3_PREFIX}{d}.csv"
    s3.put_object(Bucket=ARCHIVE_BUCKET, Key=key, Body=csv_bytes, ContentType="text/csv")
    return key

def archive_earnings(run_date, calendars):
    for report_date, rows in calendars.items():
        if not rows:
            continue
        body = pd.DataFrame([r["raw"] for r in rows]).to_csv(index=False).encode()
        key = f"{EARNINGS_PREFIX}{run_date}/{report_date}.csv"
        s3.put_object(Bucket=ARCHIVE_BUCKET, Key=key, Body=body, ContentType="text/csv")
    print(f"Archived earnings for {len(calendars)} dates under {EARNINGS_PREFIX}{run_date}/")

def upload():
    info = json.loads(ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)["Parameter"]["Value"])
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    creds.refresh(Request())
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    q = f"name = 'watchlist.csv' and '{FOLDER_ID}' in parents and trashed = false"
    existing = drive.files().list(q=q, fields="files(id, createdTime)").execute().get("files", [])
    media = MediaFileUpload(CSV_PATH, mimetype="text/csv", resumable=True)

    file_id = drive.files().create(
            body={"name": "watchlist.csv", "parents": [FOLDER_ID]},
            media_body=media, fields="id",
        ).execute()["id"]
        
    for f in existing:
        old_file_id = f["id"]
        old_file_date = f["createdTime"][:10]
        drive.files().update(fileId=old_file_id, addParents=ARCHIVE_FOLDER_ID, removeParents=FOLDER_ID, body={"name":f"watchlist_{old_file_date}.csv"}).execute()

    return file_id


def daily_market_close():
    body = scrape()
    df = zscore(body=body)
    df.to_csv(CSV_PATH, index=False)
    with open(CSV_PATH, "rb") as f:
        s3_upload_key = archive_to_s3(f.read())
    print(f"Uploaded {s3_upload_key} to S3 bucket")
    file_id = upload()
    print(f"Uploaded watchlist.csv -> {file_id}")
    return {"status": "ok", "file_id": file_id, "s3_upload_key":s3_upload_key}

def lambda_handler(event, context):
    event = event or {}
    job = event.get("job", "daily_market_close")
    if job == "daily_market_close":
        return daily_market_close()
    raise ValueError(f"Unknown job: {job}")