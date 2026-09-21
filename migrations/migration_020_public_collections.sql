-- ============================================================================
-- BrickMargin · migration 020 · public collections
-- A collector can choose to show their shelf. What they paid stays private —
-- only the sets, quantities and today's values are visible, at /u/<username>.
-- ============================================================================
alter table profiles add column if not exists collection_public boolean not null default false;
-- username lives in migration 015; make sure it exists so this works either way
alter table profiles add column if not exists username text;
create unique index if not exists profiles_username_idx on profiles (lower(username)) where username is not null;

create or replace function public_collection(u text) returns table (
  username text, set_num text, name text, theme text, image_url text,
  qty integer, condition text, market_value numeric
) language sql security definer stable as $$
  select p.username, c.set_num, s.name, s.theme, s.image_url, c.qty, c.condition, s.market_value
    from profiles p
    join collections c on c.user_id = p.id
    join set_values s on s.set_num = c.set_num
   where p.collection_public = true
     and lower(p.username) = lower(u)
     and coalesce(c.status, 'holding') <> 'sold'
   order by s.market_value desc nulls last;
$$;
grant execute on function public_collection(text) to anon, authenticated;
notify pgrst, 'reload schema';
