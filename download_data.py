from playwright.sync_api import sync_playwright

url = "https://www.barchart.com/my/watchlist?viewName=main"
sel = 'a[data-bc-download-button*="Watchlist Daily"]'

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="./barchart_profile",
        channel="chrome",
        headless=False,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector(sel)

    with ctx.expect_event("request", lambda r: "/my/download" in r.url and "onlyCheckPermissions" not in (r.post_data or "")) as req_info:
        page.locator(sel).click()
    req = req_info.value
    print(req.post_data)
    resp = ctx.request.fetch(
        req.url,
        method=req.method,
        headers={k: v for k, v in req.headers.items() if not k.startswith(":")},
        data=req.post_data_buffer,
    )
    print(resp.status, resp.headers.get("content-type"))

    with open("watchlist.csv", "wb") as f:
        f.write(resp.body())

    ctx.close()
