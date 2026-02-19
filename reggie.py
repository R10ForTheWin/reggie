#!/usr/bin/env python3
"""
Reggie - Swim Practice Registration Helper
Automates registration on portal.iclasspro.com/scaq
"""

import os
import sys
import re
import getpass
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Config ───────────────────────────────────────────────────────────────────
PORTAL     = "https://portal.iclasspro.com/scaq"
LOC_ID     = 1
STUDENT_ID = 3328   # detected from recorded session


def load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def get_credentials():
    email    = os.environ.get("REGGIE_EMAIL")
    password = os.environ.get("REGGIE_PASSWORD")
    if not email:
        email = input("  Email: ").strip()
    if not password:
        password = getpass.getpass("  Password: ")
    return email, password


def pick_class(class_list):
    print("\n" + "─" * 60)
    print("  Available Classes")
    print("─" * 60)
    for i, cls in enumerate(class_list, 1):
        name       = cls.get("className") or cls.get("name") or f"Class {cls.get('id')}"
        day        = cls.get("dayOfWeek") or ""
        start_time = cls.get("startTime") or ""
        end_time   = cls.get("endTime") or ""
        instructor = (cls.get("instructorName") or
                      (cls.get("instructor") or {}).get("name") or "")
        spots      = cls.get("openSpots")
        schedule   = f"{day} {start_time}–{end_time}".strip(" –")
        spots_str  = f"  ({spots} spots open)" if spots is not None else ""

        print(f"\n  {i:>2}. {name}")
        if schedule:   print(f"       {schedule}")
        if instructor: print(f"       Instructor: {instructor}")
        if spots_str:  print(f"      {spots_str}")

    print("\n" + "─" * 60)
    while True:
        try:
            raw = input("  Enter class number: ").strip()
            idx = int(raw) - 1
            if 0 <= idx < len(class_list):
                return class_list[idx]
            print(f"  Enter a number between 1 and {len(class_list)}")
        except ValueError:
            print("  Please enter a valid number")
        except KeyboardInterrupt:
            print("\n  Cancelled.")
            sys.exit(0)


def pick_date(dates):
    if len(dates) == 1:
        label = dates[0].get("startDate") or dates[0].get("date") or str(dates[0])
        print(f"  Auto-selected start date: {label}")
        return dates[0]

    print("\n" + "─" * 60)
    print("  Available Start Dates")
    print("─" * 60)
    for i, d in enumerate(dates, 1):
        label = d.get("startDate") or d.get("date") or str(d)
        print(f"  {i}. {label}")
    print("─" * 60)

    while True:
        try:
            raw = input("  Enter date number (or Enter for first): ").strip()
            if not raw:
                return dates[0]
            idx = int(raw) - 1
            if 0 <= idx < len(dates):
                return dates[idx]
            print(f"  Enter a number between 1 and {len(dates)}")
        except ValueError:
            print("  Please enter a valid number")
        except KeyboardInterrupt:
            print("\n  Cancelled.")
            sys.exit(0)


def run():
    load_env()

    print()
    print("=" * 40)
    print("          REGGIE")
    print("   Swim Registration Helper")
    print("=" * 40)
    print()

    email, password = get_credentials()

    captured = {
        "classes":   None,
        "cart_item": None,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=60)
        context = browser.new_context()
        page    = context.new_page()

        # ── Intercept API responses ───────────────────────────────────────
        def on_response(resp):
            try:
                url = resp.url
                if resp.status != 200:
                    return
                if "/jwt/v1/classes?" in url and "token=" in url:
                    captured["classes"] = resp.json()
                elif ("/jwt/v1/new-cart-item/class-enrollment/" in url
                      and "startDate" not in url):
                    captured["cart_item"] = resp.json()
            except Exception:
                pass

        page.on("response", on_response)

        # ── 1. Login ──────────────────────────────────────────────────────
        print("\n[1/5] Logging in...")
        page.goto(f"{PORTAL}/login")
        page.wait_for_load_state("networkidle")

        # Exclude the hidden "Forgot Password" email field (id="emailForgot")
        page.locator('input[type="email"]:not([id="emailForgot"])').first.fill(email)
        page.locator('input[type="password"]').first.fill(password)
        page.locator('button[type="submit"]').first.click()

        try:
            page.wait_for_url("**/scaq/dashboard**", timeout=20000)
        except PlaywrightTimeout:
            print("[!] Login failed — check your credentials in .env and try again.")
            browser.close()
            sys.exit(1)

        print("[1/5] Logged in")

        # ── 2. Load classes ───────────────────────────────────────────────
        print("[2/5] Loading available classes...")
        page.goto(f"{PORTAL}/classes?futureOpeningDate=false&selectedStudents={STUDENT_ID}")
        page.wait_for_load_state("networkidle")

        # Wait up to 15s for the class API response
        for _ in range(30):
            if captured["classes"]:
                break
            page.wait_for_timeout(500)

        data       = captured["classes"] or {}
        class_list = (data.get("data")
                      or data.get("classes")
                      or (data if isinstance(data, list) else []))

        if not class_list:
            print("[!] Could not load class list automatically.")
            print("    Please select your class manually in the browser.")
            print("    Navigate all the way to the cart page, then press Enter here.")
            input("  Press Enter when you're on the cart page...")
            # Skip to checkout
            page.goto(f"{PORTAL}/cart")
            page.wait_for_load_state("networkidle")
        else:
            # ── 3. Pick class ─────────────────────────────────────────────
            chosen     = pick_class(class_list)
            class_id   = chosen.get("id")
            class_name = chosen.get("className") or chosen.get("name") or str(class_id)
            print(f"\n[3/5] Selected: {class_name}")

            # Navigate directly to enrollment page (skips class detail click)
            enroll_url = (
                f"{PORTAL}/enroll/new-cart-item"
                f"?objectId={class_id}"
                f"&bookingType=classEnroll"
                f"&selectedStudents={STUDENT_ID}"
                f"&open"
            )
            page.goto(enroll_url)
            page.wait_for_load_state("networkidle")

            # Wait for cart-item API response (contains start date options)
            for _ in range(20):
                if captured["cart_item"]:
                    break
                page.wait_for_timeout(500)

            # ── 4. Start date ─────────────────────────────────────────────
            print("[4/5] Selecting start date...")
            cart_data = captured["cart_item"] or {}
            dates = (cart_data.get("startDates")
                     or cart_data.get("availableStartDates")
                     or cart_data.get("sessions")
                     or [])

            date_value = None
            if dates:
                chosen_date = pick_date(dates)
                date_value  = (chosen_date.get("startDate")
                               or chosen_date.get("date")
                               or str(chosen_date))
            else:
                print("  Could not detect start dates — please select one in the browser.")
                input("  Press Enter once a start date is selected...")

            # Click the matching date in the UI if we know it
            if date_value:
                try:
                    page.get_by_text(date_value).first.click()
                    page.wait_for_timeout(400)
                except Exception:
                    # Date text may be formatted differently; fall back to first option
                    try:
                        page.locator(
                            '[class*="date"], [class*="start"], [data-value]'
                        ).first.click()
                        page.wait_for_timeout(400)
                    except Exception:
                        print("  Could not auto-click date — please select it manually.")
                        input("  Press Enter when date is selected...")

            # Click Add to Cart
            try:
                page.get_by_role(
                    "button",
                    name=re.compile(r"add.to.cart|continue|enroll|register", re.IGNORECASE)
                ).first.click()
                page.wait_for_url("**/scaq/cart**", timeout=15000)
            except Exception:
                print("  Please click 'Add to Cart' in the browser.")
                input("  Press Enter once you're on the cart page...")

        # ── 5. Checkout ───────────────────────────────────────────────────
        print("[5/5] Checking out...")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        try:
            page.get_by_role(
                "button",
                name=re.compile(r"checkout|process|submit|pay|complete|confirm", re.IGNORECASE)
            ).first.click()
            page.wait_for_timeout(4000)
            print("\nRegistration complete!")
        except Exception:
            print("  Please click the checkout button manually.")
            input("  Press Enter when done...")

        input("\nPress Enter to close the browser...")
        browser.close()


if __name__ == "__main__":
    run()
