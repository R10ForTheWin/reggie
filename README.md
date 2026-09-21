# Reggie

Automated swim practice registration for SCAQ teams on iClassPro.

## What It Does
Reggie handles the tedious part of swim team logistics — monitoring available practice slots and auto-registering before they fill up. Built for busy parents who cannot babysit a registration portal.

## Live App
[reggie-production.up.railway.app](https://reggie-production.up.railway.app)

## Tech Stack
- Python / Flask
- Playwright (browser automation)
- Railway
- Supabase (practice tally only — optional)

## Practice Tally

Each swimmer gets a private running count of the practices Reggie registered
them for, with an estimated yardage per practice (default 3,000, adjustable).
Once a practice has finished, the app asks for that day's yardage on a slider;
the home screen shows practices and yards for the past 12 months, and the
history screen shows the all-time totals.

**A swimmer only ever sees their own numbers. Nothing is shared with the team.**

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
