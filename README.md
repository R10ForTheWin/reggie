# Reggie

Automated swim practice registration for SCAQ teams on iClassPro.

## What It Does
Reggie turns SCAQ practice registration on iClassPro into one tap. Sign in once, pick a practice from the open classes, and Reggie completes the registration in the portal for you, promo code included. Built for swimmers who would rather swim than fight a registration page.

## Live App
[reggie-production.up.railway.app](https://reggie-production.up.railway.app)

## Tech Stack
- Python / Flask
- Playwright (browser automation)
- Railway
- Supabase (practice tally only — optional)

## Practice Tally

Each swimmer gets a running count of the practices Reggie registered them for,
with an estimated yardage per practice (default 3,000, adjustable). Once a
practice has finished, the app asks for that day's yardage on a slider. The home
screen shows total yards on a rolling odometer, with miles and swims since the
first one; the history screen filters by week, month, year to date or all time.

**Private by default.** A swimmer can opt in to share confirmed swims with the
[Artie](https://github.com/R10ForTheWin/artie) crew dashboard; otherwise only
they see their numbers.

Rows are keyed by `HMAC-SHA256(TALLY_SECRET, lowercased email)` — the table
stores no email addresses, one swimmer cannot derive another's key without the
server secret, and no endpoint lists or aggregates across swimmers. Because the
key is derived rather than random, it survives a reinstall or a new phone.

### Setup

1. Run `sql/001_practices.sql` once in the Supabase SQL editor. It creates the
   `reggie` schema and enables row-level security with no policies, so
   Supabase's `anon` and `authenticated` roles can read nothing.
2. Set two environment variables on Railway:

   | Variable | Value |
   |---|---|
   | `TALLY_DB_URL` | Supabase **Session pooler** connection URI |
   | `TALLY_SECRET` | `python3 -c "import secrets;print(secrets.token_hex(32))"` |

   `TALLY_SECRET` must not change once there is history — rotating it orphans
   every existing row.

Leave either variable unset and the tally disappears from the UI entirely;
registration is unaffected. Tally writes are best-effort by design: if Supabase
is paused or unreachable, a swimmer loses a tally entry, never their spot in a
class.

### Optional

`TALLY_TZ` overrides the timezone used to decide when a practice is over
(default `America/Los_Angeles`).

## Status
Live and in active use.
