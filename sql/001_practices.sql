-- Reggie practice tally — run once in the Supabase SQL editor.
--
-- Lives in its own `reggie` schema so Reggie and Artie stay independent
-- inside the shared Supabase project.

create schema if not exists reggie;

create table if not exists reggie.practices (
    id             bigint generated always as identity primary key,
    -- HMAC-SHA256(TALLY_SECRET, lowercased email). Never the email itself:
    -- the table carries no personally identifying information.
    user_key       text        not null,
    class_id       text        not null,
    student_id     text,
    class_name     text,
    class_date     date        not null,
    class_date_raw text,

    -- Local start/end of the practice, used to decide when it is over and the
    -- swimmer can be asked for yardage. Null when the class times were unknown.
    starts_at      timestamptz,
    ends_at        timestamptz,

    -- Estimated yardage. Defaults to 3000; `yards_confirmed` stays false until
    -- the swimmer actually adjusts it, so totals can distinguish a real number
    -- from an untouched assumption.
    yards          integer     not null default 3000,
    yards_confirmed boolean    not null default false,

    registered_at  timestamptz not null default now(),
    deleted_at     timestamptz
);

-- One practice per class per day. A retry after a flaky checkout is a no-op
-- rather than a double count.
create unique index if not exists practices_active_uniq
    on reggie.practices (user_key, class_id, class_date)
    where deleted_at is null;

-- Serves the "my last year" read.
create index if not exists practices_user_date_idx
    on reggie.practices (user_key, class_date desc);

-- Yardage must stay in a sane range even if a client misbehaves.
alter table reggie.practices drop constraint if exists practices_yards_range;
alter table reggie.practices add constraint practices_yards_range
    check (yards >= 0 and yards <= 30000);

-- ── Privacy ───────────────────────────────────────────────────────────────
-- RLS on with no policies = deny by default. Supabase's anon and authenticated
-- roles (the ones a browser or the auto-generated REST API would use) can read
-- nothing at all, so a leaked anon key exposes no one's tally.
--
-- Reggie connects as the schema owner over the session pooler and bypasses RLS;
-- it scopes every statement to a single user_key in application code.
--
-- When the second app is wired up, add a policy here granting it access to a
-- single user_key rather than opening the table up.

alter table reggie.practices enable row level security;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on reggie.practices from anon;
        revoke all on schema reggie from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on reggie.practices from authenticated;
        revoke all on schema reggie from authenticated;
    end if;
end $$;
