-- ============================================================================
-- BrickMargin · migration 012 · alert delivery log   (v2 — self-repairing)
--
-- The watch list, the price history and the email preferences already exist.
-- The only missing piece is memory: which alerts we've already sent, so a set
-- that stays cheap for a week doesn't email someone every four hours.
--
-- Why v2: the first version used a bare `create table if not exists`. If an
-- `alerts_sent` table already existed in any other shape — from a half-applied
-- run — the create was skipped silently and the next statement failed with
-- "column kind does not exist". This version adds every column individually,
-- so it repairs a partial table as readily as it builds a new one.
--
-- Safe to run repeatedly, and safe to run over a broken half-finished attempt.
-- ============================================================================

-- 1 · the table, at its most minimal
create table if not exists alerts_sent (
  id bigserial primary key
);

-- 2 · every column, added only if missing. This is the self-repairing part.
alter table alerts_sent add column if not exists user_id  uuid;
alter table alerts_sent add column if not exists set_num  text;
alter table alerts_sent add column if not exists kind     text;
alter table alerts_sent add column if not exists detail   text;
alter table alerts_sent add column if not exists value_at numeric(10,2);
alter table alerts_sent add column if not exists sent_at  timestamptz
  not null default now();

-- 3 · constraints, guarded so a re-run doesn't error on an existing name
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'alerts_sent_user_fk'
  ) then
    alter table alerts_sent
      add constraint alerts_sent_user_fk
      foreign key (user_id) references public.profiles(id) on delete cascade;
  end if;

  if not exists (
    select 1 from pg_constraint where conname = 'alerts_sent_set_fk'
  ) then
    alter table alerts_sent
      add constraint alerts_sent_set_fk
      foreign key (set_num) references public.sets(set_num) on delete cascade;
  end if;
end $$;

-- 4 · indexes, now that the columns definitely exist
create index if not exists alerts_recent_idx
  on alerts_sent (user_id, set_num, kind, sent_at desc);
create index if not exists alerts_sent_at_idx
  on alerts_sent (sent_at desc);

-- 5 · a person may read their own alert history and nobody else's
alter table alerts_sent enable row level security;

drop policy if exists "own alerts" on alerts_sent;
create policy "own alerts" on alerts_sent
  for select using (auth.uid() = user_id);

grant select on alerts_sent to authenticated;

-- 6 · how far a set must move before it earns an email, per person
alter table public.profiles
  add column if not exists alert_drop_pct numeric(5,2) not null default 5;

-- 7 · PostgREST caches the schema; without this the API can report the new
--     columns as missing for a minute or two after the migration.
notify pgrst, 'reload schema';
