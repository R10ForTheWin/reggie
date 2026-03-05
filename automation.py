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
import urllib.error
import urllib.parse
import urllib.request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

_log = logging.getLogger(__name__)

# URL patterns whose response bodies are too large/noisy to log (legal text, org config, etc.)
_SKIP_BODY = ("/jwt/v1/policies", "/jwt/v1/organizations", "/customer-portal-notifications/")

def _log_url(url):
    """Strip JWT token from URL for readable logs."""
    return re.sub(r'token=[^&]+', 'token=…', url)

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

# ── Per-thread browser ─────────────────────────────────────────────────────
# Each gunicorn gthread gets its own Playwright + Chromium instance stored in
# thread-local storage. This avoids the "cannot switch to a different thread"
# crash that occurs when a global sync_playwright instance's internal event-
# loop thread exits while other threads still try to use it.

_thread_local = threading.local()


def _get_browser():
    """Return this thread's Chromium browser, launching it if needed."""
    try:
        if getattr(_thread_local, 'browser', None) is not None and _thread_local.browser.is_connected():
            return _thread_local.browser
    except Exception:
        pass
    # Browser gone or not yet created for this thread — (re)launch
    if getattr(_thread_local, 'pw', None) is not None:
        try:
            _thread_local.pw.__exit__(None, None, None)
        except Exception:
            pass
    _thread_local.pw      = sync_playwright().__enter__()
    _thread_local.browser = _thread_local.pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
    return _thread_local.browser


def _new_context(storage_state=None):
    """Create a new browser context (and page) from the shared browser."""
    ctx_kw = dict(
        user_agent=UA,
        viewport={"width": 390, "height": 844},
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    if storage_state:
        ctx_kw["storage_state"] = storage_state
    context = _get_browser().new_context(**ctx_kw)
    page = context.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except Exception:
        pass
    return context, page


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
    _log.debug("_click_first: no element found for %r on %s", text_re, page.url)


def _login(page, email, password):
    # Locations page → select SCAQ
    # domcontentloaded is enough — _click_first waits for the element itself
    page.goto("https://portal.iclasspro.com/scaq/locations?next=https://portal.iclasspro.com/scaq",
              wait_until="domcontentloaded", timeout=60000)
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
    page.goto(f"{PORTAL}/login", wait_until="domcontentloaded", timeout=60000)
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


_API_HEADERS = {
    "Accept":   "application/json, text/plain, */*",
    "Origin":   "https://portal.iclasspro.com",
    "Referer":  "https://portal.iclasspro.com/scaq/",
    "User-Agent": UA,
}

def _api_url(path, params, token):
    p = dict(params)
    p["token"] = token
    return f"https://app.iclasspro.com/api/jwt/v1/{path}?{urllib.parse.urlencode(p)}"


def _api_call(req, path):
    """Execute a urllib request, logging response body on HTTP errors."""
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        _log.error("HTTP %s from %s — body: %s", e.code, path, body)
        raise Exception(f"HTTP {e.code} from {path}: {body}")


def _api_get(path, params, token):
    """Direct HTTP GET to iClassPro JWT API."""
    req = urllib.request.Request(_api_url(path, params, token), headers=_API_HEADERS)
    return _api_call(req, path)


def _api_post(path, params, token, body=None):
    """Direct HTTP POST to iClassPro JWT API."""
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(
        _api_url(path, params, token), data=data, method="POST",
        headers={**_API_HEADERS, "Content-Type": "application/json"},
    )
    return _api_call(req, path)


def _api_delete(path, params, token):
    """Direct HTTP DELETE to iClassPro JWT API."""
    req = urllib.request.Request(
        _api_url(path, params, token), method="DELETE", headers=_API_HEADERS,
    )
    return _api_call(req, path)


def _api_refresh(token):
    """Refresh JWT and return new access token (returns original on failure)."""
    try:
        result = _api_post("refresh", {}, token)
        data = result.get("data") or result
        return data.get("access_token") or token
    except Exception as e:
        _log.warning("Token refresh failed: %s", e)
        return token


def _api_login(email, password):
    """Try direct JWT API login without browser. Returns token or None."""
    try:
        data = json.dumps({
            "email":    email,
            "password": password,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://app.iclasspro.com/api/jwt/v1/login",
            data=data,
            method="POST",
            headers={**_API_HEADERS, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        tok = (result.get("token")
               or result.get("access_token")
               or (result.get("data") or {}).get("token")
               or (result.get("data") or {}).get("access_token"))
        if tok:
            _log.info("Direct API login: SUCCESS")
        else:
            _log.warning("Direct API login: no token in response: %s", str(result)[:200])
        return tok
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        _log.warning("Direct API login HTTP %s: %s", e.code, body)
        return None
    except Exception as e:
        _log.warning("Direct API login failed: %s", e)
        return None


def _browser_get_token(email, password, cb):
    """Log in via browser, capture JWT token and save session state."""
    captured = {"token": None}
    context, page = _new_context()

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

    def on_request(req):
        try:
            if "/jwt/v1/login" in req.url and req.method == "POST":
                _log.info("Browser login request body: %s", req.post_data)
        except Exception:
            pass

    try:
        page.on("request", on_request)
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
    finally:
        context.close()

    return captured["token"]


def _get_token(email, password, cb):
    """Get JWT token: try direct API login first, fall back to browser."""
    tok = _api_login(email, password)
    if tok:
        return tok
    _log.warning("Direct API login failed — falling back to browser login")
    cb("Opening browser to log in... (much faster after login is cached)")
    return _get_token(email, password, cb)



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
        token = _get_token(email, password, cb)
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
                token = _get_token(email, password, cb)
                if not token:
                    raise Exception("Could not refresh session — please try again.")
                _cache_token(email, token)
                continue
            raise


def run_registration(email, password, class_id, student_id, promo_code=None, callback=None, dry_run=False, on_checkout_confirmed=None):
    """Complete the full registration flow via direct iClassPro API calls.
    Browser is only used to obtain the JWT token when not cached."""
    def cb(msg):
        if callback:
            callback(msg)

    # ── 1. Get JWT token (cached or via browser login) ───────────────────────
    token = _get_cached_token(email)
    if token:
        cb("Using saved session...")
    else:
        cb("Logging in... (much faster after login is cached)")
        token = _get_token(email, password, cb)
        if not token:
            raise Exception("Could not capture session token — please try again.")
        _cache_token(email, token)

    for attempt in range(2):
        try:
            # ── 2. Clear stale cart ───────────────────────────────────────────
            cb("Checking cart...")
            cart = _api_get("validate-cart/1", {}, token)
            cart_items = (cart.get("data") or cart).get("cartItems") or []
            if cart_items:
                _log.info("Cart: %d stale item(s) — clearing", len(cart_items))
                cb("Clearing previous cart contents...")
                _api_delete("remove-all-cart-items", {}, token)
                _log.info("Cart: cleared")

            # ── 3. Get cart item + available start dates ──────────────────────
            cb("Loading class details...")
            cart_item = _api_get(
                f"new-cart-item/class-enrollment/{class_id}",
                {"locationId": "", "studentId": student_id},
                token,
            )
            _log.info("new-cart-item raw response: %s", str(cart_item)[:500])
            cart_item_data = cart_item.get("data") or cart_item
            dates = (cart_item_data.get("startDates")
                     or cart_item_data.get("availableStartDates")
                     or cart_item_data.get("sessions")
                     or [])
            if not dates:
                _log.warning("No start dates in response — full response: %s", str(cart_item)[:500])
                raise Exception("Could not add to cart — you may already be enrolled in this class.")
            # Pick the first date that is today or in the future; fall back to dates[0]
            import datetime as _dt
            _today = _dt.date.today().isoformat()
            _upcoming = [d for d in dates if (d.get("startDate") or d.get("date") or "") >= _today]
            _chosen = _upcoming[0] if _upcoming else dates[0]
            date_val = _chosen.get("startDate") or _chosen.get("date") or str(_chosen)
            _log.info("Selected date: %s (today=%s, %d dates available, %d upcoming)",
                      date_val, _today, len(dates), len(_upcoming))

            # ── 4. Get cart item pinned to specific date ──────────────────────
            cb("Selecting start date...")
            cart_item_dated = _api_get(
                f"new-cart-item/class-enrollment/{class_id}",
                {"locationId": "", "studentId": student_id, "date": date_val},
                token,
            )
            _log.info("Cart item dated: %s", str(cart_item_dated)[:300])

            # ── 5. Validate cart item ─────────────────────────────────────────
            cb("Validating cart item...")
            validate_result = _api_post("validate-cart-item", {}, token, body=cart_item_dated)
            _log.info("validate-cart-item response: %s", str(validate_result)[:300])
            v_errors = validate_result.get("errors") or []
            if v_errors:
                raise Exception(f"Could not add to cart — {v_errors[0]}")

            # ── 6. Add to cart ────────────────────────────────────────────────
            cb("Adding to cart...")
            add_result = _api_post("add-cart-item", {}, token, body=cart_item_dated)
            _log.info("add-cart-item response: %s", str(add_result)[:300])
            a_errors = add_result.get("errors") or []
            if a_errors and not add_result.get("success"):
                raise Exception(f"Could not add to cart — {a_errors[0]}")

            # ── 7. Apply promo ────────────────────────────────────────────────
            if promo_code:
                cart_check = _api_get("validate-cart/1", {}, token)
                _log.info("cart before promo: %s", str(cart_check)[:300])
                cb(f"Applying promo code {promo_code}...")
                _promo_body = {"promoCode": promo_code, "promoCodes": []}
                _log.info("add-promo-code request body: %s", _promo_body)
                promo_result = _api_post("add-promo-code", {"locationId": 1}, token, body={
                    "promoCode": promo_code,
                    "promoCodes": [],
                })
                _log.info("add-promo-code response: %s", str(promo_result)[:300])
                p_errors = promo_result.get("errors") or []
                p_promos = (promo_result.get("data") or {}).get("cartItemPromoCodes") or []
                if p_errors:
                    raise Exception(
                        f"Promo code '{promo_code}' was rejected by iClassPro — "
                        "registration cancelled to avoid a full-price charge."
                    )
                if not p_promos:
                    raise Exception(
                        f"Couldn't apply promo code '{promo_code}' automatically — "
                        "registration cancelled. Please try again."
                    )
                _log.info("Promo: SUCCESS")

            if dry_run:
                cb("Dry run complete — stopping before checkout.")
                return "dry_run"

            # ── 8. Fetch payment method + final cart total ────────────────────
            cb("Completing checkout...")
            pm_resp = _api_get("family-payment-method", {}, token)
            _log.info("family-payment-method response: %s", str(pm_resp)[:300])
            pm_list = pm_resp.get("paymentMethods") or (pm_resp.get("data") or {}).get("paymentMethods") or []
            primary = next((p for p in pm_list if p.get("isPrimary")), pm_list[0] if pm_list else None)
            pm_id   = str(primary["id"]) if primary else "3805"

            cart_final = _api_get("validate-cart/1", {}, token)
            _log.info("validate-cart final response: %s", str(cart_final)[:400])
            total = (cart_final.get("data") or cart_final).get("totalCartAmount") or 0

            # ── 9. Refresh token immediately before checkout ──────────────────
            token = _api_refresh(token)
            _cache_token(email, token)

            # ── 10. Process cart (the actual checkout) ────────────────────────
            process_body = {
                "useCardOnFile":      True,
                "paymentAmount":      total,
                "paymentTotal":       total,
                "paymentType":        "",
                "useAccountCredit":   False,
                "paymentMethodId":    pm_id,
                "guestCheckoutName":  None,
                "guestCheckoutPhone": None,
                "guestCheckoutEmail": None,
                "technologyFeeAmount": 0,
            }
            _log.info("process-cart request body: %s", process_body)
            checkout_result = _api_post("process-cart/1", {}, token, body=process_body)
            _log.info("process-cart response: %s", str(checkout_result)[:300])

            inner = checkout_result.get("data") or checkout_result
            c_errors = inner.get("errors") or checkout_result.get("errors") or []
            if c_errors and not inner.get("success", True):
                raise Exception(f"Could not complete checkout — {c_errors[0]}")

            _log.info("Checkout: SUCCESS")
            if on_checkout_confirmed:
                try:
                    on_checkout_confirmed({})
                except Exception:
                    pass
            return {}

        except Exception as e:
            if attempt == 0 and ("401" in str(e) or "403" in str(e) or "Unauthorized" in str(e)):
                _invalidate_token(email)
                cb("Session expired — logging in again...")
                token = _get_token(email, password, cb)
                if not token:
                    raise Exception("Could not refresh session — please try again.")
                _cache_token(email, token)
                continue
            raise
