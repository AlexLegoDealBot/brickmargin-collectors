-- ============================================================================
-- BrickMargin · migration 014 · rebuild theme_stats
--
-- Migration 013 rebuilt set_values with `drop view ... cascade`, which is the
-- only way to change a view's column order — but cascade takes everything
-- built on top of the view with it, silently. theme_stats was one of those
-- things, and losing it breaks the insights page, all 71 theme pages, the
-- homepage theme tiles and the Explore theme filter, with no error anywhere
-- to say why.
--
-- This recreates it. Run it whether or not you think it's missing: it is
-- idempotent, and if theme_stats survived, this simply redefines it
-- identically.
-- ============================================================================

create or replace view theme_stats as
select
  s.theme,
  count(*)                                        as set_count,
  round(
    (percentile_cont(0.5) within group (order by s.market_value))::numeric, 2
  )                                               as median_value,
  round(max(s.market_value)::numeric, 2)          as top_value,
  round(avg(s.value_per_piece)::numeric, 4)       as avg_value_per_piece,
  round(avg(s.market_value)::numeric, 2)          as avg_value,
  min(s.year_released)                            as first_year,
  max(s.year_released)                            as last_year
from set_values s
where s.theme is not null
  and s.market_value is not null
  and s.plausible is true
  -- Duplo's bricks are several times larger, so its value-per-piece figure
  -- is a geometry artefact rather than a market signal. Excluding it keeps
  -- the per-piece column comparable across every other theme.
  and s.theme <> 'Duplo'
group by s.theme
having count(*) >= 3;

grant select on theme_stats to anon, authenticated;

notify pgrst, 'reload schema';
