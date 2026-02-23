"""
Reggie – Flask web app
"""

import logging
import os
import re as _re
import threading
import time
import uuid

logging.basicConfig(level=logging.INFO)

from flask import Flask, jsonify, redirect, render_template, request

app  = Flask(__name__)
_jobs      = {}
_jobs_lock = threading.Lock()

# Only one Playwright browser at a time — prevents OOM on free tier
_browser_lock = threading.BoundedSemaphore(1)


# ── Security headers ───────────────────────────────────────────────────────

@app.before_request
def https_redirect():
    if request.headers.get("X-Forwarded-Proto", "https") == "http":
        return redirect(request.url.replace("http://", "https://"), 301)


@app.after_request
def security_headers(response):
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "media-src 'self'; "
        "connect-src 'self'; "
        "worker-src 'self';"
    )
    return response


# ── Job helpers ───────────────────────────────────────────────────────────

def _cleanup():
    """Remove only terminal-state jobs older than 10 minutes. Never delete running jobs."""
    cutoff = time.time() - 600
    with _jobs_lock:
        stale = [
            jid for jid, j in _jobs.items()
            if j["status"] in ("done", "error") and j["created_at"] < cutoff
        ]
        for jid in stale:
            del _jobs[jid]


def _create_job():
    jid = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[jid] = {"status": "running", "message": "Starting...",
                      "result": None, "created_at": time.time()}
    return jid


def _update(jid, **kw):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def _get(jid):
    with _jobs_lock:
        return dict(_jobs.get(jid, {"status": "not_found", "message": "Job not found"}))


# ── Routes ────────────────────────────────────────────────────────────────

@app.route("/ping")
def ping():
    return "ok", 200


@app.route("/snap")
def debug_screenshot():
    """Debug route — only available when DEBUG_ROUTES=true env var is set."""
    if os.environ.get("DEBUG_ROUTES", "").lower() != "true":
        return "Not found", 404

    import io
    import re as _re
    from flask import send_file
    from playwright.sync_api import sync_playwright
    from automation import _new_browser
    try:
        with sync_playwright() as p:
            browser, _ctx, page = _new_browser(p)
            page.goto("https://portal.iclasspro.com/scaq/locations?next=https://portal.iclasspro.com/scaq")
            page.wait_for_load_state("networkidle")
            for text in [_re.compile("SCAQ", _re.IGNORECASE),
                         _re.compile(r"click.to.begin", _re.IGNORECASE),
                         _re.compile(r"got.it", _re.IGNORECASE)]:
                try:
                    page.get_by_role("button", name=text).first.click(timeout=4000)
                    page.wait_for_load_state("networkidle")
                except Exception:
                    pass
            page.goto("https://portal.iclasspro.com/scaq/login")
            page.wait_for_load_state("networkidle")
            try:
                page.get_by_role("button", name=_re.compile("^Yes$", _re.IGNORECASE)).first.click(timeout=4000)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            img_bytes = page.screenshot(full_page=True)
            browser.close()
        return send_file(io.BytesIO(img_bytes), mimetype="image/png")
    except Exception as e:
        return "Snapshot error — check server logs", 500


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/classes", methods=["POST"])
def api_classes():
    _cleanup()
    data     = request.json or {}
    email    = data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    jid = _create_job()

    def run():
        waited = 0
        while not _browser_lock.acquire(timeout=5):
            waited += 5
            if waited >= 120:
                _update(jid, status="error",
                        message="Server is too busy right now — please try again in a moment.")
                return
            _update(jid, message=f"Server is busy, waiting... ({waited}s)")
        try:
            from automation import get_classes
            _update(jid, message="Logging in...")
            result = get_classes(email, password,
                                 callback=lambda m: _update(jid, message=m))
            _update(jid, status="done", message="Classes loaded", result=result)
        except Exception as e:
            _update(jid, status="error", message=_safe_error(e))
        finally:
            _browser_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/register", methods=["POST"])
def api_register():
    _cleanup()
    data       = request.json or {}
    email      = data.get("email", "").strip()
    password   = data.get("password", "").strip()
    class_id   = data.get("class_id")
    student_id = data.get("student_id")
    promo      = data.get("promo_code", "").strip()
    dry_run    = bool(data.get("dry_run", False))

    if not all([email, password, class_id, student_id]):
        return jsonify({"error": "Missing required fields"}), 400
    if not _re.match(r'^\d+$', str(class_id)):
        return jsonify({"error": "Invalid class_id"}), 400
    if not _re.match(r'^\d+$', str(student_id)):
        return jsonify({"error": "Invalid student_id"}), 400

    jid = _create_job()

    def run():
        waited = 0
        while not _browser_lock.acquire(timeout=5):
            waited += 5
            if waited >= 120:
                _update(jid, status="error",
                        message="Server is too busy right now — please try again in a moment.")
                return
            _update(jid, message=f"Server is busy, waiting... ({waited}s)")
        try:
            from automation import run_registration
            result = run_registration(email, password, class_id, student_id,
                                      promo_code=promo or None,
                                      callback=lambda m: _update(jid, message=m),
                                      dry_run=dry_run)
            if result == "dry_run":
                _update(jid, status="done",
                        message="Dry run complete — everything worked up to checkout!",
                        result={"dry_run": True})
            else:
                result_data = result if isinstance(result, dict) else {}
                _update(jid, status="done", message="Registration complete!",
                        result={"dry_run": False, **result_data})
        except Exception as e:
            _update(jid, status="error", message=_safe_error(e))
        finally:
            _browser_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/job/<jid>")
def api_job(jid):
    return jsonify(_get(jid))


# ── Helpers ───────────────────────────────────────────────────────────────

# Known user-safe messages — pass through as-is
_SAFE_ERRORS = (
    "Login failed",
    "PerimeterX",
    "already enrolled",
    "Could not add to cart",
    "Could not complete checkout",
    "Checkout did not complete",
    "Could not detect your student profile",
    "Could not refresh session",
    "Another job is already running",
    "Unauthorized",
)

def _safe_error(exc):
    msg = str(exc)
    for safe in _SAFE_ERRORS:
        if safe.lower() in msg.lower():
            return msg
    # Don't leak internal details — log server-side and return generic message
    app.logger.error("Internal error: %s", msg)
    return "Something went wrong on the server — please try again."


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
