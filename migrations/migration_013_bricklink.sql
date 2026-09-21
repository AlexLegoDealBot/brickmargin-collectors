-- ============================================================================
-- BrickMargin · migration 013 · BrickLink sold prices
--
-- Everything on the site until now has been built from ASKING prices — live
-- eBay listings, which overstate value because unsold listings linger while
-- completed sales vanish from view. We have said so openly, in the product
-- and in our own research.
--
-- BrickLink publishes six months of real completed transactions, split by
-- condition, with the quantity actually sold. That is a different class of
-- evidence, and these columns hold it alongside the asking-price figures so
-- the two can be compared rather than conflated.
--
-- Safe to run more than once.
-- ============================================================================

alter table sets add column if not exists sold_new       numeric(10,2);
alter table sets add column if not exists sold_new_qty   integer;
alter table sets add column if not exists sold_new_min   numeric(10,2);
alter table sets add column if not exists sold_new_max   numeric(10,2);
alter table sets add column if not exists sold_used      numeric(10,2);
alter table sets add column if not exists sold_used_qty  integer;
alter table sets add column if not exists part_out_value numeric(10,2);
alter table sets add column if not exists bricklink_checked_at timestamptz;

-- The rotation reads least-recently-checked first, so this index is the
-- difference between a fast query and a full table scan every run.
create index if not exists sets_bl_checked_idx
  on sets (bricklink_checked_at nulls first);
create index if not exists sets_sold_new_idx
  on sets (sold_new desc) where sold_new is not null;

-- Rebuild set_values so every page can show sold prices beside asking prices.
-- CREATE OR REPLACE cannot insert columns mid-list, so drop and recreate.
drop view if exists set_values cascade;

create view set_values as
select
  s.set_num, s.name, s.theme, s.subtheme, s.year_released,
  s.pieces, s.minifigs, s.image_url, s.msrp, s.retired_at,
  c.median_sold   as market_value,
  c.comp_count,
  c.spread,
  c.source,
  c.observed_at   as valued_at,
  -- Confidence is derived here, not stored: comps records how many listings
  -- survived filtering, and the grade is a reading of that number. Deriving
  -- it means the rule lives in one place and old rows re-grade themselves if
  -- we ever change where the thresholds sit.
  case
    when c.comp_count is null then null
    when c.comp_count >= 12 then 'high'
    when c.comp_count >= 6  then 'medium'
    else 'low'
  end as confidence,
  -- real completed sales, from BrickLink
  s.sold_new, s.sold_new_qty, s.sold_new_min, s.sold_new_max,
  s.sold_used, s.sold_used_qty, s.part_out_value,
  s.bricklink_checked_at,
  case when s.msrp > 0 and c.median_sold is not null
       then round((c.median_sold / s.msrp)::numeric, 2) end as msrp_multiple,
  case when s.pieces > 0 and c.median_sold is not null
       then round((c.median_sold / s.pieces)::numeric, 3) end as value_per_piece,
  case when c.median_sold is null then null
       when c.comp_count >= 3 then true else false end as plausible
from sets s
left join lateral (
  select * from comps
   where comps.set_num = s.set_num
   order by observed_at desc
   limit 1
) c on true;

grant select on set_values to anon, authenticated;

notify pgrst, 'reload schema';
