-- ============================================================================
-- BrickMargin · migration 016b · repair the minifigure view
--
-- migration_016 added bl_num to `minifigs`, but minifig_values is a
-- `select *` over that table — so the new column shifted the view's column
-- order and CREATE OR REPLACE refused, since it can only append at the end.
-- Same wall as migration 010. The fix is the same: drop and recreate, and
-- name the columns explicitly this time so a future column can't do it again.
--
-- Safe to run more than once. Run this INSTEAD of re-running 016.
-- ============================================================================

-- the columns 016 was trying to add, applied idempotently
alter table minifigs add column if not exists bl_num text;
alter table minifigs add column if not exists rb_num text;
create unique index if not exists minifigs_rb_idx on minifigs (rb_num) where rb_num is not null;
create index if not exists minifigs_bl_idx on minifigs (bl_num) where bl_num is not null;
create index if not exists minifigs_sets_idx on minifigs using gin (in_sets);

drop view if exists minifig_values cascade;

create view minifig_values as
select
  fig_num, rb_num, bl_num, name, theme, year_released, image_url, in_sets,
  sold_new, sold_new_qty, sold_used, sold_used_qty, sold_min, sold_max,
  checked_at, created_at,
  case when sold_new_qty >= 10 then 'high'
       when sold_new_qty >= 4  then 'medium'
       when sold_new_qty >= 1  then 'low' end as confidence
from minifigs
where sold_new is not null;

grant select on minifig_values to anon, authenticated;

notify pgrst, 'reload schema';

-- 2.1: how many sets a figure appears in, so pricing starts with the ones
-- people actually own rather than the obscure end of the catalogue.
alter table minifigs add column if not exists set_count integer default 0;
create index if not exists minifigs_popular_idx on minifigs (set_count desc) where checked_at is null;
notify pgrst, 'reload schema';
