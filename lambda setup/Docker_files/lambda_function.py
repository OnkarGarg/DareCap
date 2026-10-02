import json
import os
import io
from urllib.parse import unquote

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

# ---- config (set as Lambda environment variables) ----
BUCKET = os.environ["STATE_BUCKET"]                     # private S3 bucket holding the Barchart session
STATE_KEY = os.environ.get("STATE_KEY", "barchart/storage_state.json")
TICKER_CACHE = os.environ.get("TICKER_CACHE", "barchart/ticker_cache.json")
TOKEN_PARAM = os.environ.get("TOKEN_PARAM", "/scraper/gdrive-token")  # SSM SecureString
FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
ARCHIVE_FOLDER_ID = os.environ["ARCHIVE_DRIVE_FOLDER_ID"]

URL = "https://www.barchart.com/my/watchlist?viewName=main"
DOWNLOAD_URL = "https://www.barchart.com/my/download"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# must match the UA used in setup_local.py when the session was created
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# Lambda: only /tmp is writable
STATE_PATH = "/tmp/storage_state.json"
CACHE_PATH = "/tmp/ticker_cache.json"
CSV_PATH = "/tmp/watchlist.csv"

FORM = {
    "fileName": "Watchlist Daily-Intraday",
    "meta": "symbol.normalized,field.shortName,field.type",
    "isWatchlist": "true",
    "symbols": ",",
    "method": "quotes",
    "fields": "symbol,symbolName,lastPrice,priceChange,percentChange,openPrice,highPrice,lowPrice,volume,tradeTime,raw,symbolCode,updatedFields,lastPriceDirection,,hasOptions",
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


def scrape():
    # pull the saved login session (cookies + localStorage) from S3
    s3.download_file(BUCKET, STATE_KEY, STATE_PATH)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--no-sandbox", "--single-process",
            "--disable-gpu", "--disable-dev-shm-usage",
        ])
        ctx = browser.new_context(storage_state=STATE_PATH, user_agent=UA)
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded")
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

        headers = {"Referer": URL}
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

    s3.upload_file(STATE_PATH, BUCKET, STATE_KEY)
    return body

def zscore(body):
    df = pd.read_csv(io.BytesIO(body))
    df = df[df["Name"].notna()].copy()
    symbols = df["Symbol"]
    symbols_to_read = []
    new_reads = []
    names = df.set_index("Symbol")["Name"]

    try:
        s3.download_file(BUCKET, TICKER_CACHE, CACHE_PATH)
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
    s3.upload_file(CACHE_PATH, BUCKET, TICKER_CACHE)

    for ticker in symbols:
        if ticker in ticker_cache:
            symbols_to_read.append(ticker_cache[ticker])

    hist_data = yf.Tickers(symbols_to_read).history(period="2mo")
    today = pd.Timestamp.now(tz="America/New_York").date()
    closes = hist_data["Close"]
    closes = closes[closes.index.date < today].tail(20)
    means = closes.mean(axis=0)
    stds = closes.std(axis=0)

    df["yahoo"] = df["Symbol"].map(ticker_cache)
    df["mean20"] = df["yahoo"].map(means)
    df["std20"] = df["yahoo"].map(stds)
    df["zscore"] = (df["Latest"] - df["mean20"]) / df["std20"]

    return df

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
    file_id = upload()
    print(f"Uploaded watchlist.csv -> {file_id}")
    return {"status": "ok", "file_id": file_id}