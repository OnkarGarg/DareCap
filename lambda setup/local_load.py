"""Run on your laptop (not Lambda).

1. Converts token.pickle -> gdrive_token.json (for SSM)
2. Opens a real browser window so you can log in to Barchart, then saves
   storage_state.json (for S3)

Rerun step 2 whenever Lambda reports the Barchart session has expired.
"""
import os
import pickle
import sys

from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def export_drive_token():
    if not os.path.exists("token.pickle"):
        print("token.pickle not found - run your old upload script once first.")
        return
    with open("token.pickle", "rb") as f:
        creds = pickle.load(f)
    if not creds.refresh_token:
        sys.exit("No refresh token in token.pickle - delete it and re-auth.")
    with open("gdrive_token.json", "w") as f:
        f.write(creds.to_json())
    print("Wrote gdrive_token.json")


def export_barchart_session():
    with sync_playwright() as p:
        # bundled Chromium, same as Lambda, so the session matches
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.goto("https://www.barchart.com/login")
        input("Log in in the browser window, open your watchlist, then press Enter here... ")
        ctx.storage_state(path="storage_state.json")
        browser.close()
    print("Wrote storage_state.json")


if __name__ == "__main__":
    export_drive_token()
    export_barchart_session()
    print("\nNow upload them:")
    print("  aws ssm put-parameter --name /scraper/gdrive-token --type SecureString "
          "--value file://gdrive_token.json --overwrite")
    print("  aws s3 cp storage_state.json s3://<your-bucket>/barchart/storage_state.json")