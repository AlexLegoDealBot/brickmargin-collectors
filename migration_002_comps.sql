-- ============================================================
-- BrickMargin — migration 002: comps rotation tracking
-- Paste into Supabase → SQL Editor → Run. Safe to re-run.
-- ============================================================
--
-- WHY THIS EXISTS
--   eBay's Browse API allows 5,000 calls/day and we have ~6,700 sets, so
--   we can't refresh every set daily. The collector processes a batch of
--   the STALEST sets each run and rotates through the catalog over several
--   days.
--
--   Finding "stalest" by scanning the comps table gets slower every day as
--   history accumulates. A timestamp on the set itself makes it a single
--   indexed lookup that stays fast forever.
--
--   It's also updated for sets where eBay returns nothing, so dead sets
--   don't get retried on every run.

alter table public.sets
  add column if not exists comps_last_checked_at timestamptz;

-- nulls first: never-checked sets go to the front of the queue.
create index if not exists sets_comps_stale_idx
  on public.sets (comps_last_checked_at asc nulls first);

-- Document what the comps.source values mean, so future-you doesn't
-- mistake asking prices for realized sale prices.
comment on column public.comps.source is
  'Data origin. ebay_active = median ASKING price of live listings (runs high, '
  'apply a haircut before using as resale value). ebay_sold / bricklink_sold = '
  'realized sale prices, trustworthy as-is.';

comment on column public.comps.median_sold is
  'Median price for this source. For source=ebay_active this is an ASKING '
  'price, not a sale price.';

comment on column public.comps.comp_count is
  'Number of listings that survived filtering and fed the median. Low counts '
  'mean low confidence.';

-- Verify:
select column_name, data_type
from information_schema.columns
where table_schema = 'public' and table_name = 'sets'
  and column_name = 'comps_last_checked_at';
