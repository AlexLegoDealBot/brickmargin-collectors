-- ============================================================================
-- BrickMargin · migration 016 · minifigures
--
-- A second catalogue beside sets. Same shape of question — what is it, what
-- does it sell for, how many sales stand behind that — answered from
-- BrickLink, which is where minifigures genuinely trade and the only source
-- that identifies them by their proper ID rather than a seller's phrasing.
-- ============================================================================

create table if not exists minifigs (
  fig_num        text primary key,          -- BrickLink ID, e.g. sw0001
  name           text not null,
  theme          text,
  year_released  integer,
  image_url      text,
  -- which sets it appears in (from Rebrickable), for the set page and search
  in_sets        text[] default '{}',
  -- completed sales, six months, from BrickLink
  sold_new       numeric(10,2),
  sold_new_qty   integer,
  sold_used      numeric(10,2),
  sold_used_qty  integer,
  sold_min       numeric(10,2),
  sold_max       numeric(10,2),
  checked_at     timestamptz,
  created_at     timestamptz not null default now()
);
create index if not exists minifigs_theme_idx on minifigs (theme);
create index if not exists minifigs_sold_idx on minifigs (sold_new desc) where sold_new is not null;
create index if not exists minifigs_checked_idx on minifigs (checked_at nulls first);
create index if not exists minifigs_name_idx on minifigs using gin (to_tsvector('simple', name));

grant select on minifigs to anon, authenticated;

-- The view is created in migration_016b, which names its columns explicitly
-- so that adding a column to `minifigs` can never break it again.

notify pgrst, 'reload schema';

-- 1.1: the Rebrickable ID, so a catalogue run can resume where it stopped
alter table minifigs add column if not exists rb_num text;
create unique index if not exists minifigs_rb_idx on minifigs (rb_num) where rb_num is not null;
notify pgrst, 'reload schema';

-- 2.0: figures are keyed on Rebrickable's id and carry BrickLink's separately,
-- resolved lazily by the price step. 'none' means BrickLink has no such figure.
alter table minifigs add column if not exists bl_num text;
create index if not exists minifigs_bl_idx on minifigs (bl_num) where bl_num is not null;
create index if not exists minifigs_sets_idx on minifigs using gin (in_sets);
notify pgrst, 'reload schema';
