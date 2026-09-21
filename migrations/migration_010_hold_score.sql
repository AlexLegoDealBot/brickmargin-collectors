-- ============================================================================
-- BrickMargin · migration 010 · hold score, retirement, multiple sources
--
-- Three additions:
--   source   — deals no longer come from one marketplace, so every row names
--              where it came from and the site can show it.
--   score    — a 1-10 hold rating, so a visitor doesn't have to read four
--              numbers and do the weighing themselves.
--   retired  — whether the set has left production. A discounted set still on
--              shelves is the buy-and-hold candidate; a discounted retired set
--              is just a cheaper version of what the market already priced.
--
-- Note on the view: CREATE OR REPLACE VIEW can only append columns at the end
-- of the existing column list. These new columns belong beside the other deal
-- fields, so the view is dropped and rebuilt instead. Nothing is lost — a view
-- stores no data, only the query used to assemble it.
--
-- Safe to run more than once.
-- ============================================================================

alter table deals add column if not exists source text not null default 'ebay';
alter table deals add column if not exists score numeric(3,1);
alter table deals add column if not exists retired boolean not null default false;

create index if not exists deals_score_idx
  on deals (score desc) where gone_at is null;
create index if not exists deals_hold_idx
  on deals (retired, score desc) where gone_at is null;

drop view if exists live_deals;

create view live_deals as
select
  d.item_id, d.set_num, d.title, d.condition, d.source, d.score, d.retired,
  d.item_price, d.shipping, d.total_price,
  d.market_value, d.discount_pct, d.net_if_flipped, d.margin_pct,
  d.seller, d.feedback_pct, d.item_url,
  coalesce(d.image_url, s.image_url) as image_url,
  s.name  as set_name,
  s.theme,
  s.year_released,
  s.pieces,
  s.msrp,
  s.retired_at,
  d.first_seen, d.last_seen
from deals d
join sets s on s.set_num = d.set_num
where d.gone_at is null;

grant select on live_deals to anon, authenticated;

-- PostgREST caches the schema; without this the API can still report the new
-- columns as missing for a minute or two after the migration.
notify pgrst, 'reload schema';
