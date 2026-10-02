import json
import os
from urllib.parse import unquote

import boto3
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ---- config (set as Lambda environment variables) ----
BUCKET = os.environ["STATE_BUCKET"]                     # private S3 bucket holding the Barchart session
STATE_KEY = os.environ.get("STATE_KEY", "barchart/storage_state.json")
TOKEN_PARAM = os.environ.get("TOKEN_PARAM", "/scraper/gdrive-token")  # SSM SecureString
FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]

URL = "https://www.barchart.com/my/watchlist?viewName=main"
DOWNLOAD_URL = "https://www.barchart.com/my/download"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# must match the UA used in setup_local.py when the session was created
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# Lambda: only /tmp is writable
STATE_PATH = "/tmp/storage_state.json"
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

        with open(CSV_PATH, "wb") as f:
            f.write(body)

        # save refreshed cookies so the session keeps rolling forward
        ctx.storage_state(path=STATE_PATH)
        browser.close()

    s3.upload_file(STATE_PATH, BUCKET, STATE_KEY)


def upload():
    info = json.loads(ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)["Parameter"]["Value"])
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    creds.refresh(Request())
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    q = f"name = 'watchlist.csv' and '{FOLDER_ID}' in parents and trashed = false"
    existing = drive.files().list(q=q, fields="files(id)").execute().get("files", [])
    media = MediaFileUpload(CSV_PATH, mimetype="text/csv", resumable=True)

    if existing:
        # overwrite in place: file ID (and any links/imports pointing at it) stays the same
        file_id = existing[0]["id"]
        drive.files().update(fileId=file_id, media_body=media).execute()
        for dup in existing[1:]:
            drive.files().delete(fileId=dup["id"]).execute()
    else:
        file_id = drive.files().create(
            body={"name": "watchlist.csv", "parents": [FOLDER_ID]},
            media_body=media, fields="id",
        ).execute()["id"]

    return file_id


def lambda_handler(event, context):
    scrape()
    file_id = upload()
    print(f"Uploaded watchlist.csv -> {file_id}")
    return {"status": "ok", "file_id": file_id}