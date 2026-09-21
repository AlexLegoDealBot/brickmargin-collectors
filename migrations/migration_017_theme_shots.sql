-- ============================================================================
-- BrickMargin · migration 017 · theme photographs
--
-- The themes page fetched every set with a photo in one query and picked
-- three per theme in code. Supabase caps a response at 1,000 rows, so any
-- theme whose sets weren't in the first thousand got blank white squares —
-- which was most of them. This view does the picking in the database: the
-- top three sets by value for every theme, in one row-capped-proof query.
-- ============================================================================
create or replace view theme_shots as
select theme, set_num, name, image_url, market_value, rn
from (
  select s.theme, s.set_num, s.name, s.image_url, s.market_value,
         row_number() over (partition by s.theme order by s.market_value desc nulls last) as rn
  from set_values s
  where s.theme is not null and s.image_url is not null
) t
where rn <= 3;
grant select on theme_shots to anon, authenticated;
notify pgrst, 'reload schema';
