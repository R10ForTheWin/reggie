"""
Reggie – Playwright automation functions
Called by app.py in background threads.
"""

import re
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

PORTAL = "https://portal.iclasspro.com/scaq"


LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
]

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _new_browser(p):
    browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
    context = browser.new_context(
        user_agent=UA,
        viewport={"width": 390, "height": 844},
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    page = context.new_page()
    # Remove webdriver flag
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except Exception:
        pass
    return browser, page


def _login(page, email, password):
    # Hit the locations page first and auto-select SCAQ
    page.goto(f"https://portal.iclasspro.com/scaq/locations?next=https://portal.iclasspro.com/scaq")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    # Click SCAQ using role-based selector
    try:
        page.get_by_role("button", name=re.compile("SCAQ", re.IGNORECASE)).first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
    except Exception:
        try:
            page.locator('button, ion-button').filter(has_text="SCAQ").first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
        except Exception:
            pass

    # "Click to begin" interstitial (appears after SCAQ selection)
    try:
        page.get_by_role("button", name=re.compile("click.to.begin", re.IGNORECASE)).first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
    except Exception:
        try:
            page.locator('button, ion-button, ion-item, ion-card, a, [role="button"]').filter(
                has_text=re.compile("click to begin", re.IGNORECASE)
            ).first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
        except Exception:
            try:
                page.get_by_text(re.compile("click to begin", re.IGNORECASE)).first.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
            except Exception:
                pass

    # "Welcome Info / Got It!" modal
    try:
        page.get_by_role("button", name=re.compile("got.it", re.IGNORECASE)).first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
    except Exception:
        try:
            page.locator('button, ion-button, ion-item, ion-card, a, [role="button"]').filter(
                has_text=re.compile("got.it", re.IGNORECASE)
            ).first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
        except Exception:
            try:
                page.get_by_text(re.compile("got.it", re.IGNORECASE)).first.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
            except Exception:
                pass

    # Navigate directly to login page — session/location already established
    page.goto(f"{PORTAL}/login")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)

    # "Are you a current customer?" → click Yes
    try:
        page.get_by_role("button", name=re.compile("^Yes$", re.IGNORECASE)).first.click()
        page.wait_for_timeout(2500)
    except Exception:
        try:
            page.locator('button, ion-button, ion-item, [role="button"]').filter(
                has_text=re.compile("^Yes$", re.IGNORECASE)
            ).first.click()
            page.wait_for_timeout(2500)
        except Exception:
            pass

    try:
        email_input = page.locator('input[type="email"]:not([id="emailForgot"])')
        email_input.first.wait_for(state="attached", timeout=60000)
        page.wait_for_timeout(1000)
        email_input.first.click()
        page.wait_for_timeout(500)
        email_input.first.fill(email)
    except PlaywrightTimeout:
        # Save screenshot for debugging and raise a clear error
        try:
            page.screenshot(path="/tmp/reggie_login_debug.png", full_page=True)
        except Exception:
            pass
        raise Exception(
            f"Could not find login form (page: {page.url}). "
            "PerimeterX may be blocking the server — try again in a moment."
        )

    page.locator('input[type="password"]').first.fill(password)
    page.wait_for_timeout(500)
    # Try submit button, then Next nav button (Ionic portals use nav-style submit)
    try:
        page.locator('button[type="submit"]').first.click()
    except Exception:
        try:
            page.get_by_role("button", name=re.compile(r"next|sign.in|log.in|submit", re.IGNORECASE)).first.click()
        except Exception:
            page.locator('button, ion-button').filter(has_text=re.compile(r"next|sign.in|log.in", re.IGNORECASE)).first.click()
    try:
        page.wait_for_url("**/scaq/**", timeout=30000)
        # Make sure we're not still on the login page
        if "/login" in page.url:
            raise PlaywrightTimeout("Still on login page")
    except PlaywrightTimeout:
        raise Exception("Login failed — double-check your email and password.")


def get_classes(email, password, callback=None):
    """Log in and return list of available classes + detected student ID."""
    def cb(msg):
        if callback:
            callback(msg)

    captured = {"students": None, "classes": None}

    with sync_playwright() as p:
        browser, page = _new_browser(p)

        def on_response(resp):
            try:
                if resp.status != 200:
                    return
                if "/jwt/v1/students" in resp.url:
                    captured["students"] = resp.json()
                elif "/jwt/v1/classes?" in resp.url and "token=" in resp.url:
                    captured["classes"] = resp.json()
            except Exception:
                pass

        page.on("response", on_response)

        cb("Logging in...")
        _login(page, email, password)

        cb("Detecting your student profile...")
        page.goto(f"{PORTAL}/enroll/select-students")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        student_id = None
        if captured["students"]:
            raw = captured["students"]
            lst = raw.get("data") or raw.get("students") or (raw if isinstance(raw, list) else [])
            if lst:
                student_id = lst[0].get("id") or lst[0].get("studentId")

        if not student_id:
            student_id = 3328  # fallback from recorded session

        cb("Loading available classes...")
        page.goto(f"{PORTAL}/classes?futureOpeningDate=false&selectedStudents={student_id}")
        page.wait_for_load_state("networkidle")

        for _ in range(30):
            if captured["classes"]:
                break
            page.wait_for_timeout(500)

        browser.close()

    raw   = captured["classes"] or {}
    lst   = raw.get("data") or raw.get("classes") or (raw if isinstance(raw, list) else [])
    return {"classes": lst, "student_id": student_id}


def run_registration(email, password, class_id, student_id, promo_code=None, callback=None):
    """Complete the full registration flow for a given class."""
    def cb(msg):
        if callback:
            callback(msg)

    captured = {"cart_item": None}

    with sync_playwright() as p:
        browser, page = _new_browser(p)

        def on_response(resp):
            try:
                if resp.status != 200:
                    return
                if ("/jwt/v1/new-cart-item/class-enrollment/" in resp.url
                        and "startDate" not in resp.url):
                    captured["cart_item"] = resp.json()
            except Exception:
                pass

        page.on("response", on_response)

        cb("Logging in...")
        _login(page, email, password)

        cb("Opening enrollment page...")
        enroll_url = (
            f"{PORTAL}/enroll/new-cart-item"
            f"?objectId={class_id}"
            f"&bookingType=classEnroll"
            f"&selectedStudents={student_id}"
            f"&open"
        )
        page.goto(enroll_url)
        page.wait_for_load_state("networkidle")

        for _ in range(20):
            if captured["cart_item"]:
                break
            page.wait_for_timeout(500)

        cb("Selecting start date...")
        cart_data = captured["cart_item"] or {}
        dates = (cart_data.get("startDates")
                 or cart_data.get("availableStartDates")
                 or cart_data.get("sessions")
                 or [])

        if dates:
            date_val = dates[0].get("startDate") or dates[0].get("date") or str(dates[0])
            try:
                page.get_by_text(date_val).first.click()
                page.wait_for_timeout(400)
            except Exception:
                try:
                    page.locator('[class*="date"], [class*="start"]').first.click()
                except Exception:
                    pass

        cb("Adding to cart...")
        try:
            page.get_by_role(
                "button",
                name=re.compile(r"add.to.cart|continue|enroll|register", re.IGNORECASE)
            ).first.click()
            page.wait_for_url("**/scaq/cart**", timeout=15000)
        except Exception:
            raise Exception("Could not add to cart — you may already be enrolled in this class.")

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        if promo_code:
            cb(f"Applying promo code {promo_code}...")
            try:
                promo_input = page.locator(
                    'input[placeholder*="promo" i], input[placeholder*="coupon" i], '
                    'input[placeholder*="code" i], input[name*="promo" i], input[name*="coupon" i]'
                ).first
                promo_input.fill(promo_code)
                page.get_by_role(
                    "button",
                    name=re.compile(r"apply|add|submit", re.IGNORECASE)
                ).first.click()
                page.wait_for_timeout(1500)
            except Exception as e:
                cb(f"Note: could not auto-apply promo code ({e})")

        cb("Completing checkout...")
        try:
            page.get_by_role(
                "button",
                name=re.compile(r"checkout|process|submit|pay|complete|confirm", re.IGNORECASE)
            ).first.click()
            page.wait_for_timeout(4000)
        except Exception:
            raise Exception("Could not complete checkout automatically.")

        browser.close()

    return True
