"""
Opens a real browser window so you can log in to Barchart, then saves
storage_state.json for the Lambda to use.

Rerun whenever Lambda reports the Barchart session has expired.
"""
from playwright.sync_api import sync_playwright

# must match UA in lambda_function.py, or Barchart may reject the session
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


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
    export_barchart_session()
    print("\nNow upload it:")
    print("  aws s3 cp storage_state.json s3://barchart--scraper/barchart/storage_state.json --region eu-north-1")