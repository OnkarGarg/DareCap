from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup as bs

url = "https://www.barchart.com/my/watchlist?viewName=main"


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="./barchart_profile",
        # channel="chrome",
        headless=False,
        accept_downloads=True,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="networkidle")

    with page.expect_download() as dl_info:
        page.locator('a[data-bc-download-button*="Watchlist Daily"]').click()    
    download = dl_info.value
    print("url:", download.url)
    print("name:", download.suggested_filename)
    print("failure:", download.failure())
    download.save_as(f"./{download.suggested_filename}")

    ctx.close()