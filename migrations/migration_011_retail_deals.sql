-- ============================================================================
-- BrickMargin · migration 011 · retail discounts
--
-- Until now a "deal" meant one thing: priced below what the set already
-- fetches on the secondary market. That's the reseller's definition, it's
-- rare, and it left the board empty most days.
--
-- Shoppers use the other definition — below the retail price. Now that MSRP
-- is populated we can measure that too, so a deal is either:
--
--   'retail'  — under the set's RRP. What most people mean by a sale.
--   'market'  — under what it resells for. The arbitrage the flippers want.
--   'both'    — under both, which is the genuinely good one.
--
-- Safe to run more than once.
-- ============================================================================

alter table deals add column if not exists deal_type text not null default 'market';
alter table deals add column if not exists msrp numeric(10,2);
alter table deals add column if not exists pct_off_msrp numeric(6,2);

create index if not exists deals_type_idx
  on deals (deal_type, score desc) where gone_at is null;
create index if not exists deals_retail_idx
  on deals (pct_off_msrp desc) where gone_at is null;

drop view if exists live_deals;

create view live_deals as
select
  d.item_id, d.set_num, d.title, d.condition, d.source, d.score, d.retired,
  d.deal_type, d.pct_off_msrp,
  d.item_price, d.shipping, d.total_price,
  d.market_value, d.discount_pct, d.net_if_flipped, d.margin_pct,
  d.seller, d.feedback_pct, d.item_url,
  coalesce(d.image_url, s.image_url) as image_url,
  s.name  as set_name,
  s.theme,
  s.year_released,
  s.pieces,
  coalesce(d.msrp, s.msrp) as msrp,
  s.retired_at,
  d.first_seen, d.last_seen
from deals d
join sets s on s.set_num = d.set_num
where d.gone_at is null;

grant select on live_deals to anon, authenticated;

notify pgrst, 'reload schema';
