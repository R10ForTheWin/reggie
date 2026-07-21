"""
Reggie – Flask web app
"""

import json
import logging
import os
import re as _re
import signal
import threading
import time
import urllib.request
import uuid

logging.basicConfig(level=logging.INFO)

from flask import Flask, jsonify, redirect, render_template, request

app  = Flask(__name__)
_jobs      = {}
_jobs_lock = threading.Lock()

# ── Surf conditions (Manhattan Beach / Topaz St, NOAA buoy 46222) ──────────
_surf_cache     = {"data": None, "ts": 0}
_SURF_CACHE_TTL = 1800  # 30 min
_MB_LAT, _MB_LON = 33.886, -118.406


def _deg_to_cardinal(deg):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(deg / 22.5) % 16]


def _wind_state(avg_speed_mph, avg_dir_deg):
    if avg_speed_mph < 4:
        return "glassy"
    raw   = abs(avg_dir_deg - 90)
    angle = 360 - raw if raw > 180 else raw
    if angle < 30:  return "off"
    if angle < 60:  return "cross-off"
    if angle < 120: return "cross"
    if angle < 150: return "cross-on"
    return "on"


def _fetch_url(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "Reggie/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_surf_data():
    result = {"temp_f": None, "wave_ft": None, "wind_mph": None, "wind_dir": None, "wind_state": None}

    try:
        text = _fetch_url("https://www.ndbc.noaa.gov/data/realtime2/46222.txt")
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            cols = line.split()
            if len(cols) < 15:
                continue
            if result["wave_ft"] is None and cols[8] != "MM":
                try:
                    result["wave_ft"] = round(float(cols[8]) * 3.281 * 2) / 2
                except ValueError:
                    pass
            if result["temp_f"] is None and cols[14] != "MM":
                try:
                    c = float(cols[14])
                    if c < 90:
                        result["temp_f"] = round((c * 9 / 5 + 32) * 10) / 10
                except ValueError:
                    pass
            if result["wave_ft"] is not None and result["temp_f"] is not None:
                break
    except Exception:
        pass

    try:
        meteo_url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={_MB_LAT}&longitude={_MB_LON}"
            "&current=wind_speed_10m,wind_direction_10m"
            "&wind_speed_unit=mph&timezone=America%2FLos_Angeles"
        )
        cur   = json.loads(_fetch_url(meteo_url)).get("current", {})
        speed = cur.get("wind_speed_10m")
        deg   = cur.get("wind_direction_10m")
        if speed is not None and deg is not None:
            result["wind_mph"]   = round(speed)
            result["wind_dir"]   = _deg_to_cardinal(deg)
            result["wind_state"] = _wind_state(speed, deg)
    except Exception:
        pass

    return result

_REDIRECT_TO = os.environ.get("REDIRECT_TO", "").strip()
if _REDIRECT_TO:
    @app.before_request
    def _redirect_all():
        from flask import make_response
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10;url={_REDIRECT_TO}">
  <title>Reggie has moved!</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; max-width: 480px; margin: 60px auto; padding: 24px; text-align: center; background: #f5f5f5; }}
    h1 {{ font-size: 1.6em; color: #1a1a1a; }}
    p {{ color: #555; line-height: 1.6; }}
    a.btn {{ display: inline-block; margin: 16px 0; padding: 14px 28px; background: #0070f3; color: white; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 1.1em; }}
    .steps {{ text-align: left; background: white; border-radius: 12px; padding: 20px 24px; margin-top: 24px; }}
    .steps h2 {{ font-size: 1em; margin-top: 0; }}
    .steps ol {{ padding-left: 20px; color: #333; }}
    .steps li {{ margin-bottom: 8px; }}
  </style>
</head>
<body>
  <h1>🚀 Reggie has a new home!</h1>
  <p>We've moved to a faster, more reliable server. You'll be redirected automatically in 10 seconds.</p>
  <a class="btn" href="{_REDIRECT_TO}">Go Now</a>
  <div class="steps">
    <h2>📱 Update your Home Screen icon:</h2>
    <ol>
      <li><strong>Delete</strong> the old Reggie icon from your home screen</li>
      <li>Tap <strong>Go Now</strong> above to open the new site in Safari</li>
      <li>Tap the <strong>Share</strong> button (box with arrow ↑)</li>
      <li>Scroll down and tap <strong>"Add to Home Screen"</strong></li>
      <li>Tap <strong>Add</strong> — done!</li>
    </ol>
  </div>
</body>
</html>"""
        return make_response(html, 200)

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
        "frame-src 'self' https://www.youtube.com https://www.youtube-nocookie.com; "
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


@app.route("/api/surf")
def api_surf():
    now = time.time()
    if _surf_cache["data"] is not None and (now - _surf_cache["ts"]) < _SURF_CACHE_TTL:
        return jsonify(_surf_cache["data"])
    data = _fetch_surf_data()
    _surf_cache["data"] = data
    _surf_cache["ts"]   = now
    return jsonify(data)


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
