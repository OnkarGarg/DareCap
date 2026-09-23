from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup as bs

url = "https://www.barchart.com/my/watchlist?viewName=main"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="./barchart_profile",  # cookies/login saved here
        channel="chrome",                    # your installed Chrome, not bundled Chromium
        headless=True,                       # make it false for the first login
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",

    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="networkidle")

    # input("Log in if needed, then press Enter...")  # only needed the first run
    # page.goto(url, wait_until="networkidle")

    html = page.content()
    ctx.close()

soup = bs(html, "html.parser")
print(soup.prettify())

