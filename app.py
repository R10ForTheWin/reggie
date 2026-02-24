"""
Reggie – Flask web app
"""

import logging
import os
import re as _re
import signal
import threading
import time
import uuid

logging.basicConfig(level=logging.INFO)

from flask import Flask, jsonify, redirect, render_template, request

app  = Flask(__name__)
_jobs      = {}
_jobs_lock = threading.Lock()

# Max 3 concurrent browser contexts — shared browser keeps memory manageable
_browser_lock = threading.BoundedSemaphore(3)


def _sigterm_handler(signum, frame):
    """Mark any in-progress jobs as failed before gunicorn shuts us down."""
    with _jobs_lock:
        for j in _jobs.values():
            if j["status"] == "running":
                j["status"] = "error"
                j["message"] = "Server restarted mid-job — please try again."
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)

signal.signal(signal.SIGTERM, _sigterm_handler)


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
        _jobs[jid] = {
            "status":     "running",
            "message":    "Starting...",
            "result":     None,
            "created_at": time.time(),
            "cancelled":  False,
        }
    return jid


def _make_callback(jid):
    """Return a callback that forwards progress messages and raises if job was cancelled."""
    def _cb(msg):
        with _jobs_lock:
            j = _jobs.get(jid)
            cancelled = j and j.get("cancelled")
        if cancelled:
            raise InterruptedError("Registration cancelled by user.")
        _update(jid, message=msg)
    return _cb


def _update(jid, **kw):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid].update(kw)


def _get(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
        if not j:
            return {"status": "not_found", "message": "Job not found"}
        return dict(j)


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
    from automation import _new_context
    try:
        context, page = _new_context()
        with context:
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
                                 callback=_make_callback(jid))
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

            def _on_checkout_confirmed(result):
                result_data = result if isinstance(result, dict) else {}
                _update(jid, status="done", message="Registration complete!",
                        result={"dry_run": False, **result_data})

            result = run_registration(email, password, class_id, student_id,
                                      promo_code=promo or None,
                                      callback=_make_callback(jid),
                                      dry_run=dry_run,
                                      on_checkout_confirmed=_on_checkout_confirmed)
            if result == "dry_run":
                _update(jid, status="done",
                        message="Dry run complete — everything worked up to checkout!",
                        result={"dry_run": True})
            elif _get(jid)["status"] != "done":
                # Fallback: on_checkout_confirmed wasn't reached (left_cart was False)
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


@app.route("/api/cancel/<jid>", methods=["POST"])
def api_cancel(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
        if not j:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if j["status"] == "running":
            j["cancelled"] = True
    return jsonify({"ok": True})


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
    "cancelled to avoid",
    "was rejected",
    "could not be entered",
    "registration cancelled",
    "couldn't apply promo",
    "cancelled by user",
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
