"""
Reggie – Flask web app
"""

import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request

app  = Flask(__name__)
_jobs      = {}
_jobs_lock = threading.Lock()


# ── Job helpers ───────────────────────────────────────────────────────────

def _cleanup():
    cutoff = time.time() - 600
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j["created_at"] < cutoff]
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
        from automation import get_classes
        try:
            _update(jid, message="Logging in...")
            result = get_classes(email, password,
                                 callback=lambda m: _update(jid, message=m))
            _update(jid, status="done", message="Classes loaded", result=result)
        except Exception as e:
            _update(jid, status="error", message=str(e))

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

    if not all([email, password, class_id, student_id]):
        return jsonify({"error": "Missing required fields"}), 400

    jid = _create_job()

    def run():
        from automation import run_registration
        try:
            run_registration(email, password, class_id, student_id,
                             promo_code=promo or None,
                             callback=lambda m: _update(jid, message=m))
            _update(jid, status="done", message="Registration complete!")
        except Exception as e:
            _update(jid, status="error", message=str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/job/<jid>")
def api_job(jid):
    return jsonify(_get(jid))


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
