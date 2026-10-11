from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup as bs
from urllib.parse import unquote
import json

# link to the watchlist page on barchart
url = "https://www.barchart.com/my/watchlist?viewName=main"

# this is all the information required to make a successful api call to barchart servers
form = {
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
    symbols = data["api"]["symbols"].split(",")
    form["symbols"] = ",".join(symbols)

    # it is an authentication thing to ensure there is no cross site forgery
    xsrf = next((c["value"] for c in ctx.cookies() if c["name"] == "XSRF-TOKEN"), None)
    headers = {"Referer": url}
    if xsrf:
        headers["X-XSRF-TOKEN"] = unquote(xsrf)

    # sending the api request to the server
    resp = ctx.request.post("https://www.barchart.com/my/download", form=form, headers=headers)

    # saving the response from the server
    with open("watchlist.csv", "wb") as f:
        f.write(resp.body())

    # closing the browser context, saving all the user data to barchar profile
    ctx.close()