-- ============================================================================
-- BrickMargin · migration 018 · the listings behind every number
-- The comps collector counted listings and threw them away. Now it keeps
-- the ones it counted, so a set page can show its working: here are the
-- listings, here is the median, here is where to buy one. Every link out
-- carries the affiliate campaign.
-- ============================================================================
create table if not exists comp_listings (
  item_id     text primary key,
  set_num     text not null references sets(set_num) on delete cascade,
  title       text not null,
  price       numeric(10,2) not null,
  shipping    numeric(10,2),
  condition   text,
  seller      text,
  feedback_pct numeric(5,2),
  image_url   text,
  item_url    text not null,
  observed_at timestamptz not null default now()
);
create index if not exists comp_listings_set_idx on comp_listings (set_num, observed_at desc);
grant select on comp_listings to anon, authenticated;
notify pgrst, 'reload schema';
