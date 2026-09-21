-- ============================================================================
-- BrickMargin · migration 009 · live deals
--
-- A deal is a live listing priced below what the set is worth. We already
-- know what every set is worth (comps) and eBay tells us what people are
-- asking right now — the gap between those two is the product.
--
-- Two rules are baked into the schema rather than trusted to the collector:
--   * total_price = item + shipping, and it is NOT NULL. A $1 set with $200
--     postage must never be able to rank above an honest $150 listing.
--   * a listing is only visible while it is still for sale. Anything the
--     feed stops returning is marked gone, so sold items leave the site.
-- ============================================================================

create table if not exists deals (
  item_id        text primary key,              -- eBay listing id
  set_num        text not null references sets(set_num) on delete cascade,

  title          text not null,
  condition      text not null,                 -- 'new' | 'used'
  item_price     numeric(10,2) not null check (item_price >= 0),
  shipping       numeric(10,2) not null default 0 check (shipping >= 0),
  total_price    numeric(10,2) not null check (total_price > 0),

  market_value   numeric(10,2) not null,        -- the comp at capture time
  discount_pct   numeric(6,2)  not null,        -- vs market value
  net_if_flipped numeric(10,2),                 -- after fees + postage
  margin_pct     numeric(6,2),                  -- profit over total_price

  seller         text,
  feedback_pct   numeric(5,2),
  image_url      text,
  item_url       text not null,

  first_seen     timestamptz not null default now(),
  last_seen      timestamptz not null default now(),
  gone_at        timestamptz                    -- set when it stops appearing
);

create index if not exists deals_live_idx
  on deals (margin_pct desc) where gone_at is null;
create index if not exists deals_set_idx  on deals (set_num) where gone_at is null;
create index if not exists deals_cond_idx on deals (condition) where gone_at is null;
create index if not exists deals_seen_idx on deals (last_seen);

-- Only live deals are readable. A sold listing is not a deal, and the site
-- physically cannot show one.
create or replace view live_deals as
select
  d.item_id, d.set_num, d.title, d.condition,
  d.item_price, d.shipping, d.total_price,
  d.market_value, d.discount_pct, d.net_if_flipped, d.margin_pct,
  d.seller, d.feedback_pct, d.item_url,
  coalesce(d.image_url, s.image_url) as image_url,
  s.name  as set_name,
  s.theme,
  s.year_released,
  s.pieces,
  s.msrp,
  d.first_seen, d.last_seen
from deals d
join sets s on s.set_num = d.set_num
where d.gone_at is null;

alter table deals enable row level security;

drop policy if exists "public read deals" on deals;
create policy "public read deals" on deals
  for select using (gone_at is null);

grant select on deals      to anon, authenticated;
grant select on live_deals to anon, authenticated;

-- Housekeeping: anything not seen in 48 hours is treated as ended. The
-- collector calls this at the end of every run.
create or replace function expire_stale_deals()
returns integer
language plpgsql
as $$
declare
  n integer;
begin
  update deals
     set gone_at = now()
   where gone_at is null
     and last_seen < now() - interval '48 hours';
  get diagnostics n = row_count;
  return n;
end;
$$;
