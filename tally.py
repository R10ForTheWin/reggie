"""
Practice tally — a private, per-user log of practices Reggie registered you for,
with an estimated yardage the swimmer can adjust once the practice is over.

Privacy model
-------------
Rows are keyed by an opaque `user_key` = HMAC-SHA256(TALLY_SECRET, lowercased
email). The secret lives only on the server, so knowing another swimmer's email
address is not enough to derive their key. There is deliberately no query here
that lists or aggregates across user_keys — every statement is scoped to one.

Failure policy
--------------
Every public function swallows its own errors and the module no-ops entirely
when TALLY_DB_URL / TALLY_SECRET are unset. A paused or unreachable database
must never cost someone their spot in a class: we would rather lose a tally
entry than fail a registration.
"""

import datetime
import hashlib
import hmac
import logging
import os
import re

_log = logging.getLogger(__name__)

_DB_URL  = os.environ.get("TALLY_DB_URL", "").strip()
_SECRET  = os.environ.get("TALLY_SECRET", "").strip()
_TIMEOUT = 8

_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

# SCAQ swims on Pacific time regardless of where the server runs.
_TZ_NAME = os.environ.get("TALLY_TZ", "America/Los_Angeles")

DEFAULT_YARDS = 3000
MIN_YARDS     = 0
MAX_YARDS     = 30000

# Most practices run an hour; a few go 1:15. Used only when the class listing
# didn't give us an end time.
_DEFAULT_DURATION = datetime.timedelta(hours=1)


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(_TZ_NAME)
    except Exception:
        return datetime.timezone.utc


def enabled():
    """True when both the database URL and the signing secret are configured."""
    return bool(_DB_URL and _SECRET)


def user_key(email):
    """Opaque per-user key. Stable across devices and reinstalls, so a swimmer
    who switches phones still sees the same history."""
    if not _SECRET or not email:
        return None
    return hmac.new(_SECRET.encode(), email.strip().lower().encode(),
                    hashlib.sha256).hexdigest()


def valid_key(key):
    return bool(key) and bool(_KEY_RE.match(key))


def clamp_yards(value):
    """Coerce client input into a sane range. Returns None if not a number."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(MIN_YARDS, min(MAX_YARDS, n))


def _connect():
    import psycopg
    # prepare_threshold=None disables prepared statements. They gain nothing at
    # this query volume, and they break against Supabase's transaction-mode
    # pooler (port 6543) — so this works with either pooler the URL points at.
    return psycopg.connect(_DB_URL, connect_timeout=_TIMEOUT,
                           prepare_threshold=None)


def _parse_date(raw):
    """iClassPro hands back dates in a few shapes. Fall back to today rather
    than dropping the row — an approximate date still counts as a practice."""
    if raw:
        text = str(raw).strip()[:10]
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return datetime.date.today()


def _parse_time(raw):
    """Class times arrive as '5:45am', '5:45 AM' or '05:45:00'."""
    if not raw:
        return None
    text = str(raw).strip().upper().replace(".", "")
    text = re.sub(r"\s+", " ", text)
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M:%S", "%H:%M", "%I %p"):
        try:
            return datetime.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _practice_window(class_date, start_time, end_time):
    """Local start/end of one practice, as timezone-aware datetimes.

    Returns (None, None) when the start time is unknown — the caller then has
    no basis to decide the practice is over, and simply won't prompt for it."""
    start = _parse_time(start_time)
    if not start or not class_date:
        return None, None
    tz = _tz()
    starts_at = datetime.datetime.combine(class_date, start, tzinfo=tz)

    end = _parse_time(end_time)
    if end:
        ends_at = datetime.datetime.combine(class_date, end, tzinfo=tz)
        # A class listed as ending before it starts has crossed midnight.
        if ends_at <= starts_at:
            ends_at += datetime.timedelta(days=1)
    else:
        ends_at = starts_at + _DEFAULT_DURATION
    return starts_at, ends_at


# ── Writes ────────────────────────────────────────────────────────────────

def record_practice(email, class_id, class_name=None, class_date=None,
                    student_id=None, start_time=None, end_time=None):
    """Log one practice. Returns True if a new row was written.

    Re-registering the same class on the same date is a no-op, so a retry after
    a flaky checkout cannot inflate the count."""
    if not enabled():
        return False
    key = user_key(email)
    if not key:
        return False
    cdate = _parse_date(class_date)
    starts_at, ends_at = _practice_window(cdate, start_time, end_time)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into reggie.practices
                    (user_key, class_id, student_id, class_name,
                     class_date, class_date_raw, starts_at, ends_at, yards)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict do nothing
                returning id
                """,
                (key, str(class_id), str(student_id) if student_id else None,
                 (class_name or "").strip()[:200] or None,
                 cdate, str(class_date)[:64] if class_date else None,
                 starts_at, ends_at, DEFAULT_YARDS),
            )
            return cur.fetchone() is not None
    except Exception as e:
        _log.warning("Tally write failed (registration unaffected): %s", e)
        return False


def set_yards(key, practice_id, yards):
    """Record the swimmer's own yardage for one practice.

    Scoped to the caller's own key, so an id alone reaches nothing."""
    if not enabled() or not valid_key(key):
        return False
    amount = clamp_yards(yards)
    if amount is None:
        return False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update reggie.practices
                   set yards = %s, yards_confirmed = true
                 where id = %s and user_key = %s and deleted_at is null
                returning id
                """,
                (amount, int(practice_id), key),
            )
            return cur.fetchone() is not None
    except Exception as e:
        _log.warning("Tally yardage update failed: %s", e)
        return False


def delete_practice(key, practice_id):
    """Soft-delete one entry — for a class that was registered but skipped."""
    if not enabled() or not valid_key(key):
        return False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update reggie.practices
                   set deleted_at = now()
                 where id = %s and user_key = %s and deleted_at is null
                returning id
                """,
                (int(practice_id), key),
            )
            return cur.fetchone() is not None
    except Exception as e:
        _log.warning("Tally delete failed: %s", e)
        return False


# ── Reads ─────────────────────────────────────────────────────────────────

def list_practices(key, limit=2000):
    # 2000 rows is roughly a decade of swimming four times a week. The client
    # filters by period from this one payload, so "All time" must not be a
    # truncated view.
    """Everything this one swimmer has logged, newest first, plus rolled-up
    counts. Returns None when the tally is unavailable so the UI can stay quiet
    rather than show a wrong zero."""
    if not enabled() or not valid_key(key):
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select id, class_name, class_date, starts_at, ends_at,
                       yards, yards_confirmed
                  from reggie.practices
                 where user_key = %s and deleted_at is null
                 order by class_date desc, id desc
                 limit %s
                """,
                (key, int(limit)),
            )
            rows = cur.fetchall()
    except Exception as e:
        _log.warning("Tally read failed: %s", e)
        return None

    now       = datetime.datetime.now(_tz())
    today     = now.date()
    year_ago  = today - datetime.timedelta(days=365)

    entries      = []
    last_year    = 0
    total_yards  = 0
    year_yards   = 0
    pending      = []

    for pid, name, cdate, starts_at, ends_at, yards, confirmed in rows:
        yards = yards or 0
        in_year = bool(cdate and cdate >= year_ago)
        if in_year:
            last_year  += 1
            year_yards += yards
        total_yards += yards

        # Only ask about a practice that has actually finished.
        is_over = bool(ends_at and ends_at <= now)
        entry = {
            "id":        pid,
            "name":      name or "Practice",
            "date":      cdate.isoformat() if cdate else None,
            "label":     cdate.strftime("%a %b %-d, %Y") if cdate else "",
            "time":      starts_at.strftime("%-I:%M %p").lower() if starts_at else "",
            "yards":     yards,
            "confirmed": bool(confirmed),
            "is_over":   is_over,
        }
        entries.append(entry)
        if is_over and not confirmed:
            pending.append(entry)

    return {
        "total":        len(entries),
        "last_year":    last_year,
        "total_yards":  total_yards,
        "year_yards":   year_yards,
        "default_yards": DEFAULT_YARDS,
        "entries":      entries,
        # Oldest unconfirmed first, so the swimmer clears the backlog in order.
        "pending":      list(reversed(pending)),
    }
