-- Opt-in sharing of swim practices into Artie's team workout feed.
-- Run once in the Supabase SQL editor, after 001_practices.sql.
--
-- Artie's dashboard and records are team-wide and unfiltered, so anything
-- written to public.workouts is visible to the whole crew. Reggie promises its
-- swimmers "only you can see this", so this must never be on by default:
-- a swimmer turns it on for themselves, from their own device, or it stays off.

create table if not exists reggie.artie_sharing (
    user_key     text primary key,
    athlete_name text        not null,
    enabled      boolean     not null default false,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

alter table reggie.artie_sharing enable row level security;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on reggie.artie_sharing from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on reggie.artie_sharing from authenticated;
    end if;
end $$;

-- Lets a synced row be found again, so re-syncing updates rather than
-- duplicates. Partial, so it only constrains rows Reggie owns and leaves
-- hand-entered workouts alone.
create unique index if not exists workouts_reggie_file_name_uniq
    on public.workouts (file_name)
 where source = 'reggie';
