from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup as bs
from urllib.parse import unquote
import json
import yfinance as yf

# link to the watchlist page on barchart
url = "https://www.barchart.com/my/watchlist?viewName=main"
symbols = []

# setting up a headless web browser with our chrome user data directory so that we dont have to login to the page
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="./barchart_profile",
        channel="chrome",
        headless=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    )
    # waiting for the page to load
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="networkidle")

    # safeguard in case we get logged out from our session. if that is the case, we will have have to run it headed and logging in once again
    if "login" in page.url:
        raise RuntimeError("Barchart session expired - run headed and log in again")

    # extracting the html from the page so that we can extract the symbols
    html = page.content()
    soup = bs(html, "html.parser")

    # searching for the data table tag
    tag = soup.find('div', class_="bc-datatable")

    # working through the data table tag to extract the symbols and append them to the api form
    api_config = tag.attrs["data-api-config"]
    data = json.loads(api_config)
    print(data["api"])
    symbols = data["api"]["symbols"].split(",")

    #TODO add a system that will check the last harvested list of tickers. 
    # if there is a change compared to the lsit, we download a file and get the 
    # list of stock names and use that to get tickers in the yahoo market

    # closing the browser context, saving all the user data to barchar profile
    ctx.close()

print(symbols)

lookback = 20

close = yf.download(symbols, period="3mo", auto_adjust=True)["Close"][symbols].dropna()
while True:
    close = close.iloc[-(lookback + 1):]

    norm = close / close.iloc[0]
    value = norm.mean(axis=1)
    window = value.iloc[:-1]
    current = value.iloc[-1]

    z = (current - window.mean()) / window.std(ddof=1)
    print(f"Z-score: {z:.3f}")