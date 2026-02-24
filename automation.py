"""
Reggie – Playwright automation functions
Called by app.py in background threads.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

_log = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_TOKEN_TTL  = 2592000  # 30 days
_CACHE_DIR  = "/tmp/reggie_cache"


def _cache_key(email):
    return hashlib.sha256(email.lower().encode()).hexdigest()


def _load_cache(email):
    try:
        path = os.path.join(_CACHE_DIR, _cache_key(email) + ".json")
        with open(path) as f:
            entry = json.load(f)
        if entry.get("expires_at", 0) > time.time():
            return entry
    except Exception:
        pass
    return None


def _save_cache(email, data):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = os.path.join(_CACHE_DIR, _cache_key(email) + ".json")
        data["expires_at"] = time.time() + _TOKEN_TTL
        with open(path, "w") as f:
            json.dump(data, f)
        os.chmod(path, 0o600)
    except Exception:
        pass



def _get_cached_token(email):
    with _cache_lock:
        entry = _load_cache(email)
        return entry.get("token") if entry else None


def _cache_token(email, token):
    with _cache_lock:
        entry = _load_cache(email) or {}
        entry["token"] = token
        _save_cache(email, entry)


def _invalidate_token(email):
    # Just clear the token field, keep session state
    with _cache_lock:
        entry = _load_cache(email)
        if entry:
            entry.pop("token", None)
            _save_cache(email, entry)


def _get_cached_session(email):
    with _cache_lock:
        entry = _load_cache(email)
        return entry.get("session") if entry else None


def _cache_session(email, state):
    with _cache_lock:
        entry = _load_cache(email) or {}
        entry["session"] = state
        _save_cache(email, entry)


def _invalidate_session(email):
    with _cache_lock:
        entry = _load_cache(email)
        if entry:
            entry.pop("session", None)
            _save_cache(email, entry)

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


def _new_browser(p, storage_state=None):
    browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
    ctx_kw = dict(
        user_agent=UA,
        viewport={"width": 390, "height": 844},
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    if storage_state:
        ctx_kw["storage_state"] = storage_state
    context = browser.new_context(**ctx_kw)
    page = context.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except Exception:
        pass
    return browser, context, page


def _click_first(page, text_re, timeout=5000):
    """Try role=button, then broad Ionic selector, then get_by_text. Silently skip if not found."""
    try:
        page.get_by_role("button", name=text_re).first.click(timeout=timeout)
        return
    except Exception:
        pass
    try:
        page.locator('button, ion-button, ion-item, ion-card, a, [role="button"]').filter(
            has_text=text_re
        ).first.click(timeout=timeout)
        return
    except Exception:
        pass
    try:
        page.get_by_text(text_re).first.click(timeout=timeout)
        return
    except Exception:
        pass
    _log.warning("_click_first: no element found for %r on %s", text_re, page.url)


def _login(page, email, password):
    # Locations page → select SCAQ
    # domcontentloaded is enough — _click_first waits for the element itself
    page.goto("https://portal.iclasspro.com/scaq/locations?next=https://portal.iclasspro.com/scaq")
    page.wait_for_load_state("domcontentloaded")
    _click_first(page, re.compile("SCAQ", re.IGNORECASE))
    page.wait_for_load_state("domcontentloaded")

    # "Click to begin" interstitial
    _click_first(page, re.compile(r"click.to.begin", re.IGNORECASE))
    page.wait_for_load_state("domcontentloaded")

    # "Welcome Info" → Got It
    _click_first(page, re.compile(r"got.it", re.IGNORECASE))
    page.wait_for_load_state("domcontentloaded")

    # Go directly to login page (location session already set)
    page.goto(f"{PORTAL}/login")
    page.wait_for_load_state("networkidle")  # keep — need Angular form to render

    # "Are you a current customer?" → Yes
    _click_first(page, re.compile(r"^Yes$", re.IGNORECASE))
    # No sleep — email input wait below handles timing

    # Fill email
    try:
        email_input = page.locator('input[type="email"]:not([id="emailForgot"])')
        email_input.first.wait_for(state="attached", timeout=60000)
        email_input.first.click()
        email_input.first.fill(email)
    except PlaywrightTimeout:
        raise Exception(
            f"Could not find login form (page: {page.url}). "
            "PerimeterX may be blocking the server — try again in a moment."
        )

    # Fill password
    pwd_input = page.locator('input[type="password"]').first
    pwd_input.click()
    pwd_input.fill(password)
    # No sleep — click Next immediately
    # The visible submit is the "Next" nav button; the form's button[type=submit] is hidden
    submitted = False
    try:
        page.get_by_role("button", name=re.compile(r"^next$", re.IGNORECASE)).first.click()
        submitted = True
    except Exception:
        pass
    if not submitted:
        try:
            page.locator('button, ion-button').filter(
                has_text=re.compile(r"^next$", re.IGNORECASE)
            ).first.click()
            submitted = True
        except Exception:
            pass
    if not submitted:
        # Last resort: press Enter on the password field
        pwd_input.press("Enter")
    try:
        page.wait_for_function("!window.location.href.includes('/login')", timeout=30000)
    except PlaywrightTimeout:
        raise Exception("Login failed — double-check your email and password.")


def _api_get(path, params, token):
    """Direct HTTP call to iClassPro JWT API using a captured browser token."""
    p = dict(params)
    p["token"] = token
    url = f"https://app.iclasspro.com/api/jwt/v1/{path}?{urllib.parse.urlencode(p)}"
    req = urllib.request.Request(url, headers={
        "Accept":  "application/json, text/plain, */*",
        "Origin":  "https://portal.iclasspro.com",
        "Referer": "https://portal.iclasspro.com/scaq/",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _browser_get_token(email, password, cb):
    """Launch browser, log in, capture JWT token and save session state."""
    captured = {"token": None}

    with sync_playwright() as p:
        browser, context, page = _new_browser(p)

        def on_response(resp):
            try:
                if resp.status != 200 or captured["token"]:
                    return
                if "/jwt/v1/login" in resp.url:
                    data = resp.json()
                    tok = data.get("token") or data.get("access_token")
                    if tok:
                        captured["token"] = tok
                        return
                if "app.iclasspro.com/api/jwt/v1/" in resp.url and "token=" in resp.url:
                    tok = urllib.parse.parse_qs(
                        urllib.parse.urlparse(resp.url).query
                    ).get("token", [None])[0]
                    if tok:
                        captured["token"] = tok
            except Exception:
                pass

        page.on("response", on_response)
        _login(page, email, password)

        for _ in range(10):
            if captured["token"]:
                break
            page.wait_for_timeout(300)

        # Only save session if login produced a valid token
        if captured["token"]:
            try:
                _cache_session(email, context.storage_state())
            except Exception:
                pass

        browser.close()

    return captured["token"]


def get_classes(email, password, callback=None):
    """Return available classes. Uses cached JWT token when available — browser only on first call."""
    def cb(msg):
        if callback:
            callback(msg)

    # Fast path: cached token → no browser needed
    token = _get_cached_token(email)
    if token:
        cb("Using saved session...")
    else:
        cb("Logging in...")
        token = _browser_get_token(email, password, cb)
        if not token:
            raise Exception("Could not capture session token — please try again.")
        _cache_token(email, token)

    for attempt in range(2):
        try:
            cb("Detecting your student profile...")
            students_raw = _api_get("students", {}, token)
            lst = (students_raw.get("data") or students_raw.get("students")
                   or (students_raw if isinstance(students_raw, list) else []))
            if not lst:
                raise Exception("Could not detect your student profile — please try again.")
            student_id = lst[0].get("id") or lst[0].get("studentId")
            if not student_id:
                raise Exception("Could not detect your student profile — please try again.")

            cb("Loading available classes...")
            classes_raw = _api_get("classes", {
                "locationId":        1,
                "limit":             100,
                "page":              1,
                "students":          student_id,
                "futureOpeningDate": "false",
            }, token)
            classes_lst = (classes_raw.get("data") or classes_raw.get("classes")
                           or (classes_raw if isinstance(classes_raw, list) else []))
            return {"classes": classes_lst, "student_id": student_id}

        except Exception as e:
            if attempt == 0 and ("401" in str(e) or "403" in str(e)):
                # Token expired — invalidate and do a fresh browser login, then retry
                _invalidate_token(email)
                cb("Session expired — logging in again...")
                token = _browser_get_token(email, password, cb)
                if not token:
                    raise Exception("Could not refresh session — please try again.")
                _cache_token(email, token)
                continue
            raise


def _clear_cart(page, cb):
    """Remove all items from the iClassPro cart."""
    _log.info("Cart: clearing stale items...")
    cb("Clearing previous cart contents...")
    for _ in range(10):  # safety limit — max 10 items
        try:
            remove_btn = page.locator(
                'button, ion-button, ion-item, a, [role="button"]'
            ).filter(has_text=re.compile(r"^remove$", re.IGNORECASE)).first
            if remove_btn.count() == 0:
                break
            remove_btn.click(timeout=5000)
            page.wait_for_load_state("networkidle")
        except Exception:
            break
    _log.info("Cart: cleared")


def run_registration(email, password, class_id, student_id, promo_code=None, callback=None, dry_run=False):
    """Complete the full registration flow for a given class."""
    def cb(msg):
        if callback:
            callback(msg)

    captured = {"cart_item": None}

    enroll_url = (
        f"{PORTAL}/enroll/new-cart-item"
        f"?objectId={class_id}"
        f"&bookingType=classEnroll"
        f"&selectedStudents={student_id}"
        f"&open"
    )

    with sync_playwright() as p:
        cached_state = _get_cached_session(email)
        browser, context, page = _new_browser(p, storage_state=cached_state)

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

        if cached_state:
            # Try jumping straight to the enrollment page
            cb("Opening enrollment page...")
            page.goto(enroll_url)
            page.wait_for_load_state("networkidle")
            # If the session expired the portal redirects to login
            if "/login" in page.url:
                cb("Session expired — logging in again...")
                _invalidate_token(email)
                _invalidate_session(email)
                _login(page, email, password)
                try:
                    _cache_session(email, context.storage_state())
                except Exception:
                    pass
                page.goto(enroll_url)
                page.wait_for_load_state("networkidle")
        else:
            cb("Logging in...")
            try:
                _login(page, email, password)
                try:
                    _cache_session(email, context.storage_state())
                except Exception:
                    pass
            except Exception:
                _invalidate_token(email)
                _invalidate_session(email)
                raise
            cb("Opening enrollment page...")
            page.goto(enroll_url)
            page.wait_for_load_state("networkidle")

        # If a previous interrupted run left items in the cart, clear them first
        # so we always register exactly the class the user selected.
        if "/scaq/cart" in page.url:
            _log.warning("Landed on cart — clearing stale cart before retrying enrollment")
            _clear_cart(page, cb)
            page.goto(enroll_url)
            page.wait_for_load_state("networkidle")

        for _ in range(60):
            if captured["cart_item"]:
                break
            page.wait_for_timeout(100)

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
            if "/scaq/cart" not in page.url:
                raise Exception("Could not add to cart — you may already be enrolled in this class.")

        page.wait_for_load_state("networkidle")  # ensure Angular cart is fully rendered

        if promo_code:
            cb(f"Applying promo code {promo_code}...")
            promo_applied = False

            # Wait for the cart to fully render before touching the promo section.
            # Angular SPA can still be painting after networkidle fires.
            try:
                page.get_by_text(
                    re.compile(r"promo code", re.IGNORECASE)
                ).first.wait_for(state="visible", timeout=15000)
                _log.info("Promo: cart promo section visible")
            except Exception as e:
                _log.warning("Promo: timed out waiting for promo section: %s", e)

            # Step 1: Click "Use Promo Code" to reveal the input
            _log.info("Promo: looking for 'Use Promo Code' trigger on %s", page.url)
            _PROMO_TEXT = re.compile(r"use promo code|promo code|have a promo|enter.*code", re.IGNORECASE)
            try:
                cnt = 0
                for role in ("link", "button"):
                    link = page.get_by_role(role, name=_PROMO_TEXT)
                    cnt = link.count()
                    _log.info("Promo: get_by_role(%r) count=%d", role, cnt)
                    if cnt > 0:
                        break
                if cnt == 0:
                    link = page.locator(
                        'ion-button, ion-item, ion-label, a, button'
                    ).filter(has_text=_PROMO_TEXT)
                    cnt = link.count()
                    _log.info("Promo: ionic selector count=%d", cnt)
                if cnt == 0:
                    link = page.get_by_text(_PROMO_TEXT)
                    cnt = link.count()
                    _log.info("Promo: get_by_text count=%d", cnt)
                if cnt > 0:
                    link.first.click(timeout=5000)
                    _log.info("Promo: trigger clicked")
                else:
                    _log.warning("Promo: trigger not found — input may already be visible")
            except Exception as e:
                _log.warning("Promo: trigger step failed: %s", e)

            # Step 2: Fill and submit
            # Ionic renders ion-input as a native <input class="native-input"> internally.
            # Exclude checkboxes/radios explicitly — the cart has ~20 hidden checkbox inputs
            # (ion-checkbox components) that would otherwise be matched by broad selectors.
            _PROMO_INPUT_SEL = (
                'ion-input input[type="text"], '
                'ion-input input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]):not([type="email"]):not([type="password"]), '
                'input[type="text"], '
                'input[placeholder*="romo" i], '
                'input[placeholder*="ode" i]'
            )
            try:
                promo_input = page.locator(_PROMO_INPUT_SEL).last
                _log.info("Promo: waiting for input to be visible")
                promo_input.wait_for(state="visible", timeout=8000)
                _log.info("Promo: input visible, filling code")
                promo_input.click()
                promo_input.fill(promo_code)

                # Step 3: Click the submit button (arrow).
                # xpath '../..//button' is confirmed working — try it first,
                # then fall back to CSS siblings with short timeouts.
                submit_clicked = False
                for xpath in [
                    '../..//button', '../..//ion-button',
                    '../button', '../ion-button',
                    '../../..//button', '../../..//ion-button',
                ]:
                    try:
                        promo_input.locator(f'xpath={xpath}').first.click(timeout=1000)
                        submit_clicked = True
                        _log.info("Promo: submit via xpath '%s'", xpath)
                        break
                    except Exception:
                        pass
                if not submit_clicked:
                    for btn_sel in [
                        'ion-input ~ button', 'ion-input ~ ion-button',
                        'input[type="text"] ~ button', 'input[type="text"] ~ ion-button',
                    ]:
                        try:
                            page.locator(btn_sel).last.click(timeout=500)
                            submit_clicked = True
                            _log.info("Promo: submit via CSS '%s'", btn_sel)
                            break
                        except Exception:
                            pass
                if not submit_clicked:
                    _log.warning("Promo: no submit button found — pressing Enter")
                    promo_input.press("Enter")

                page.wait_for_load_state("networkidle")
                # Wake as soon as the DOM reflects promo success or rejection
                try:
                    page.wait_for_function(
                        """() => {
                            const b = (document.body.innerText || '').toLowerCase();
                            return b.includes('promo applied') || b.includes('invalid promo') ||
                                   b.includes('code not found') || b.includes('not a valid') ||
                                   b.includes('code has expired') || b.includes('cannot be applied') ||
                                   b.includes('not applicable') || b.includes('invalid code');
                        }""",
                        timeout=3000,
                    )
                except Exception:
                    pass

                # Step 4: Verify success
                body = page.inner_text("body").lower()
                has_promo_applied = "promo applied" in body
                has_code_name    = promo_code.lower() in body
                _log.info("Promo: body check — 'promo applied'=%s, code_in_body=%s",
                          has_promo_applied, has_code_name)
                if has_promo_applied or has_code_name:
                    promo_applied = True
                    _log.info("Promo: SUCCESS")
                else:
                    reject_phrases = [
                        "invalid promo", "promo code is not valid", "code is not valid",
                        "code not found", "not a valid", "code has expired", "code expired",
                        "cannot be applied", "not applicable", "invalid code",
                    ]
                    if any(p in body for p in reject_phrases):
                        raise Exception(
                            f"Promo code '{promo_code}' was rejected by iClassPro — "
                            "registration cancelled to avoid a full-price charge."
                        )
                    _log.warning("Promo: neither confirmed nor rejected — treating as unconfirmed")

            except Exception as e:
                _log.warning("Promo: exception in fill/submit: %s", e)
                if "rejected" in str(e):
                    raise  # Real rejection — surface immediately

            if not promo_applied:
                # The cart lives in the server's browser — the user can't access it
                # to apply the promo manually, so just cancel and let them retry.
                raise Exception(
                    f"Couldn't apply promo code '{promo_code}' automatically — "
                    "registration cancelled. Please try again."
                )

        if dry_run:
            cb("Dry run complete — stopping before checkout.")
            browser.close()
            return "dry_run"

        cb("Completing checkout...")
        try:
            page.get_by_role(
                "button",
                name=re.compile(r"checkout|process|submit|pay|complete|confirm", re.IGNORECASE)
            ).first.click()
        except Exception:
            raise Exception("Could not complete checkout automatically.")
        try:
            page.wait_for_function("!window.location.href.includes('/cart')", timeout=30000)
        except PlaywrightTimeout:
            _log.warning("Cart redirect timed out — checkout may still have succeeded")

        browser.close()

    return {}
