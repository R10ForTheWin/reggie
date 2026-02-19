"""
Reggie - Flow Recorder
Launches a headed browser so you can demonstrate the registration flow.
All navigation and interactions are printed to the console for scripting.
"""

from playwright.sync_api import sync_playwright

URL = "https://portal.iclasspro.com/scaq/locations?next=https:%2F%2Fportal.iclasspro.com%2Fscaq"

def on_request(request):
    if request.resource_type in ("xhr", "fetch"):
        print(f"[XHR] {request.method} {request.url[:120]}")

def on_navigation(frame):
    if frame.parent_frame is None:  # main frame only
        print(f"[NAV] {frame.url[:120]}")

def on_click(source):
    print(f"[CLICK] {source}")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=100)
    context = browser.new_context()
    page = context.new_page()

    # Log navigations and XHR
    page.on("request", on_request)
    page.on("framenavigated", on_navigation)

    print(f"[START] Opening {URL}")
    print("[INFO] Go through the login and registration flow normally.")
    print("[INFO] Close the browser window when done.\n")

    page.goto(URL)

    # Keep running until the browser is closed by the user
    try:
        page.wait_for_event("close", timeout=0)
    except Exception:
        pass

    print("\n[DONE] Browser closed. Review the log above to script the flow.")
    browser.close()
