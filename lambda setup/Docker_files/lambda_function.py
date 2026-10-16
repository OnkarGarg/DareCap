import json
import os
import io
from urllib.parse import unquote
from datetime import datetime
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

DAILY_WATCHLIST_URL = "https://www.barchart.com/my/watchlist?watchlist=1798025&viewName=main"
DOWNLOAD_URL = "https://www.barchart.com/my/download"
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

s3 = boto3.client("s3")
ssm = boto3.client("ssm")
ARCHIVE_BUCKET = os.environ["ARCHIVE_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "raw/watchlist/")


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

        page.goto(DAILY_WATCHLIST_URL, wait_until="domcontentloaded")
        try:
            # networkidle can hang on pages with live quote polling; wait for the table instead
            table = page.wait_for_selector("div.bc-datatable[data-api-config]",
                                           state="attached", timeout=30_000)
        except PWTimeout:
            if "login" in page.url:
                raise RuntimeError("Barchart session expired - rerun setup_local.py and re-upload state")
            raise RuntimeError(f"Watchlist table not found (blocked or layout changed). URL: {page.url}")

        api_config = json.loads(table.get_attribute("data-api-config"))
        form = {**FORM, "symbols": api_config["api"]["symbols"]}

        headers = {"Referer": DAILY_WATCHLIST_URL}
        xsrf = next((c["value"] for c in ctx.cookies() if c["name"] == "XSRF-TOKEN"), None)
        if xsrf:
            headers["X-XSRF-TOKEN"] = unquote(xsrf)

        resp = ctx.request.post(DOWNLOAD_URL, form=form, headers=headers)
        body = resp.body()
        if not resp.ok or body.lstrip()[:1] == b"<":
            raise RuntimeError(f"Download failed: HTTP {resp.status}, got HTML instead of CSV")

        # save refreshed cookies so the session keeps rolling forward
        ctx.storage_state(path=STATE_PATH)
        browser.close()

    s3.upload_file(STATE_PATH, STATE_BUCKET, STATE_KEY)
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


def lambda_handler(event, context):
    body = scrape()
    df = zscore(body=body)
    df.to_csv(CSV_PATH, index=False)
    with open(CSV_PATH, "rb") as f:
        s3_upload_key = archive_to_s3(f.read())
    print(f"Uploaded {s3_upload_key} to S3 bucket")
    file_id = upload()
    print(f"Uploaded watchlist.csv -> {file_id}")
    return {"status": "ok", "file_id": file_id, "s3_upload_key":s3_upload_key}