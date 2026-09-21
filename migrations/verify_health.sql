-- ============================================================================
-- BrickMargin · post-migration health check
--
-- Run this after any migration that drops a view. It reads every table and
-- view the site depends on; anything missing throws here rather than showing
-- up as an empty page nobody notices for a fortnight.
-- ============================================================================

select 'sets'        as object, count(*) as rows from sets
union all select 'comps',       count(*) from comps
union all select 'set_values',  count(*) from set_values
union all select 'theme_stats', count(*) from theme_stats
union all select 'live_deals',  count(*) from live_deals
union all select 'deals',       count(*) from deals
union all select 'profiles',    count(*) from profiles
union all select 'watches',     count(*) from watches
union all select 'collections', count(*) from collections
union all select 'alerts_sent', count(*) from alerts_sent;

-- The columns migration 013 added, and how far the sweep has got.
select
  count(*)                                          as sets_total,
  count(*) filter (where sold_new is not null)      as with_sold_price,
  count(*) filter (where bricklink_checked_at is not null) as checked,
  count(*) filter (where msrp is not null)          as with_retail
from sets;

-- set_values must still expose everything the site reads.
select set_num, name, theme, market_value, comp_count, spread,
       confidence, msrp, msrp_multiple, value_per_piece, plausible,
       sold_new, sold_new_qty, retired_at
from set_values
limit 1;
