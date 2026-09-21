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

# Ask for yardage slightly before the practice ends, so the swimmer is still at
# the pool and can answer on the spot rather than remembering later.
PROMPT_LEAD = datetime.timedelta(minutes=10)

# iClassPro doesn't reliably expose a start time field, but it does put the time
# in the class name: "El Segundo: Monday 09/21 at 12:00pm".
_NAME_TIME_RE = re.compile(r"\bat\s+(\d{1,2}:\d{2}\s*[ap]\.?m\.?)", re.IGNORECASE)


def time_from_class_name(name):
    """Pull '12:00pm' out of a class name. Returns None when there isn't one."""
    if not name:
        return None
    m = _NAME_TIME_RE.search(str(name))
    return _parse_time(m.group(1)) if m else None


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


def _practice_window(class_date, start_time, end_time, class_name=None):
    """Local start/end of one practice, as timezone-aware datetimes.

    iClassPro does not reliably send a start time, so fall back to the time
    embedded in the class name before giving up. Returns (None, None) only when
    neither source has one."""
    start = _parse_time(start_time) or time_from_class_name(class_name)
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
    starts_at, ends_at = _practice_window(cdate, start_time, end_time, class_name)
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
                returning id, class_date, class_name
                """,
                (amount, int(practice_id), key),
            )
            row = cur.fetchone()
            if not row:
                return False
            cdate, cname = row[1], row[2]
    except Exception as e:
        _log.warning("Tally yardage update failed: %s", e)
        return False

    # Mirror into Artie once the number is the swimmer's own, never while it's
    # still the 3000 default. Failures here are logged, not raised.
    try:
        sync_to_artie(key, cdate, amount, cname)
    except Exception as e:
        _log.warning("Artie sync failed after yardage update: %s", e)
    return True


# ── Sharing into Artie's team feed ────────────────────────────────────────
# Artie's dashboard is team-wide and unfiltered, so a synced swim is visible to
# the whole crew. Reggie tells swimmers "only you can see this", so sharing is
# strictly opt-in per swimmer and off unless they turned it on themselves.

_YARDS_TO_METRES = 0.9144


def get_sharing(key):
    """{'enabled': bool, 'athlete_name': str} for this swimmer. Defaults to off."""
    off = {"enabled": False, "athlete_name": ""}
    if not enabled() or not valid_key(key):
        return off
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select enabled, athlete_name from reggie.artie_sharing where user_key = %s",
                (key,),
            )
            row = cur.fetchone()
            if not row:
                return off
            return {"enabled": bool(row[0]), "athlete_name": row[1] or ""}
    except Exception as e:
        _log.warning("Sharing read failed: %s", e)
        return off


def set_sharing(key, is_enabled, athlete_name):
    """Turn team sharing on or off. Enabling requires a name, because that is
    how the crew will see the swim attributed on Artie's dashboard."""
    if not enabled() or not valid_key(key):
        return False
    name = (athlete_name or "").strip()[:80]
    if is_enabled and not name:
        return False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into reggie.artie_sharing (user_key, athlete_name, enabled)
                values (%s, %s, %s)
                on conflict (user_key) do update
                   set enabled      = excluded.enabled,
                       athlete_name = excluded.athlete_name,
                       updated_at   = now()
                """,
                (key, name or "Swimmer", bool(is_enabled)),
            )
        return True
    except Exception as e:
        _log.warning("Sharing write failed: %s", e)
        return False


def sync_to_artie(key, class_date, yards, class_name=None):
    """Mirror one confirmed swim into Artie's workouts table.

    No-op unless this swimmer opted in. Never raises: a sharing failure must
    not cost the swimmer their tally entry, let alone their registration."""
    if not enabled() or not valid_key(key):
        return False
    share = get_sharing(key)
    if not share["enabled"]:
        return False

    cdate  = _parse_date(class_date)
    amount = clamp_yards(yards)
    if amount is None:
        return False
    metres    = round(amount * _YARDS_TO_METRES, 1)
    date_text = cdate.isoformat()
    file_name = "reggie-pool-%s" % date_text

    try:
        with _connect() as conn, conn.cursor() as cur:
            # Don't fight a workout that's already there by hand. Reggie only
            # ever owns rows it wrote (source='reggie'); anything the swimmer
            # entered themselves for that day wins and is left untouched.
            cur.execute(
                """
                select 1 from public.workouts
                 where activity = 'pool_swim'
                   and workout_date = %s
                   and name = %s
                   and coalesce(source, '') <> 'reggie'
                 limit 1
                """,
                (date_text, share["athlete_name"]),
            )
            if cur.fetchone():
                _log.info("Artie sync: manual pool_swim already logged for %s", date_text)
                return False

            cur.execute(
                """
                insert into public.workouts
                    (name, file_name, file_type, workout_date,
                     distance_m, activity, source)
                values (%s, %s, 'Pool Swim', %s, %s, 'pool_swim', 'reggie')
                on conflict (file_name) where source = 'reggie'
                do update set distance_m = excluded.distance_m,
                              name       = excluded.name
                returning id
                """,
                (share["athlete_name"], file_name, date_text, metres),
            )
            return cur.fetchone() is not None
    except Exception as e:
        _log.warning("Artie sync failed (tally unaffected): %s", e)
        return False


def sync_all_to_artie(key):
    """Push every confirmed practice into Artie. Used when sharing is first
    switched on, so the crew feed isn't missing everything logged so far."""
    data = list_practices(key)
    if not data:
        return 0
    return sum(
        1 for e in data["entries"]
        if e["confirmed"] and e["date"]
        and sync_to_artie(key, e["date"], e["yards"], e["name"])
    )


def add_practice(key, class_name=None, class_date=None, yards=None):
    """Manually log a practice Reggie didn't register — or one deleted by
    mistake and no longer undoable. Returns the new row id, or None."""
    if not enabled() or not valid_key(key):
        return None
    cdate  = _parse_date(class_date)
    amount = clamp_yards(yards)
    if amount is None:
        amount = DEFAULT_YARDS
    name = (class_name or "").strip()[:200] or "Practice"
    starts_at, ends_at = _practice_window(cdate, None, None, name)
    try:
        with _connect() as conn, conn.cursor() as cur:
            # class_id "manual" keeps these out of the (user_key, class_id,
            # class_date) uniqueness rule that governs real registrations,
            # except against another manual entry on the same day.
            cur.execute(
                """
                insert into reggie.practices
                    (user_key, class_id, class_name, class_date,
                     starts_at, ends_at, yards, yards_confirmed)
                values (%s, 'manual', %s, %s, %s, %s, %s, true)
                on conflict do nothing
                returning id
                """,
                (key, name, cdate, starts_at, ends_at, amount),
            )
            row = cur.fetchone()
            if not row:
                return None
            pid = row[0]
    except Exception as e:
        _log.warning("Tally manual add failed: %s", e)
        return None

    # A manual entry is confirmed by definition, so it syncs like any other.
    try:
        sync_to_artie(key, cdate, amount, name)
    except Exception as e:
        _log.warning("Artie sync failed after manual add: %s", e)
    return pid


def restore_practice(key, practice_id):
    """Undo a delete. The row was only soft-deleted, so this is always safe."""
    if not enabled() or not valid_key(key):
        return False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update reggie.practices
                   set deleted_at = null
                 where id = %s and user_key = %s and deleted_at is not null
                returning id
                """,
                (int(practice_id), key),
            )
            return cur.fetchone() is not None
    except Exception as e:
        _log.warning("Tally restore failed: %s", e)
        return False


def delete_practice(key, practice_id):
    """Soft-delete one entry — for a class that was registered but skipped.
    Recoverable via restore_practice; nothing here ever hard-deletes."""
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

        # Ask 10 minutes before the end, while the swimmer is still at the pool.
        # When the class time was never captured, fall back to the day being
        # over — a late prompt beats the silent never-prompt that NULL times
        # used to cause.
        if ends_at:
            is_over = now >= (ends_at - PROMPT_LEAD)
        else:
            is_over = bool(cdate and cdate < today)
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
        # Newest unconfirmed first. A practice stays here until its yardage is
        # confirmed or it's removed — there is deliberately no expiry, so the
        # ask survives closing the app, and persists until the swimmer answers.
        # Newest-first matters when a backlog builds: the swim they can still
        # remember is the one they just did, not one from three weeks ago.
        "pending":      pending,
    }
