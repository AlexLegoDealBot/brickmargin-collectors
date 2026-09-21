-- ============================================================================
-- BrickMargin · migration 015 · the daily game
--
-- One set a day, the same for everyone. Guess its value, keep a streak, climb
-- a board. Usernames exist so a leaderboard never shows an email address.
-- ============================================================================

-- 1 · usernames and streaks live on the profile
alter table profiles add column if not exists username      text;
alter table profiles add column if not exists streak        integer not null default 0;
alter table profiles add column if not exists best_streak   integer not null default 0;
alter table profiles add column if not exists streak_saves  integer not null default 3;
alter table profiles add column if not exists last_played   date;
alter table profiles add column if not exists plays         integer not null default 0;
alter table profiles add column if not exists correct       integer not null default 0;

-- case-insensitive uniqueness: "Alex" and "alex" are the same person
create unique index if not exists profiles_username_idx
  on profiles (lower(username)) where username is not null;

-- 2 · what a username may be. Enforced in the database so no client can
--    bypass it: 3-20 characters, letters, digits and underscores, must start
--    with a letter, and not on the blocked list.
create table if not exists blocked_words (word text primary key);
insert into blocked_words (word) values
  ('admin'), ('moderator'), ('brickmargin'), ('margo'), ('lego'), ('official'),
  ('support'), ('staff'), ('fuck'), ('shit'), ('cunt'), ('nigger'), ('nigga'),
  ('faggot'), ('retard'), ('rape'), ('nazi'), ('hitler'), ('kike'), ('spic'),
  ('chink'), ('whore'), ('slut'), ('cock'), ('dick'), ('pussy'), ('porn'),
  ('sex'), ('anal'), ('penis'), ('vagina'), ('bitch'), ('bastard'), ('damn'),
  ('kill'), ('die'), ('suicide')
on conflict do nothing;

create or replace function username_ok(u text) returns boolean
language plpgsql immutable as $$
declare w text;
begin
  if u is null then return false; end if;
  if u !~ '^[A-Za-z][A-Za-z0-9_]{2,19}$' then return false; end if;
  -- squash separators and common letter-for-number swaps before checking
  for w in select word from blocked_words loop
    if translate(lower(u), '013457_', 'oleast') like '%' || w || '%' then
      return false;
    end if;
  end loop;
  return true;
end $$;

alter table profiles drop constraint if exists profiles_username_valid;
alter table profiles add constraint profiles_username_valid
  check (username is null or username_ok(username));

-- 3 · the set of the day. One row per date; chosen on first request.
create table if not exists daily_sets (
  play_date date primary key,
  set_num   text not null references sets(set_num),
  created_at timestamptz not null default now()
);
grant select on daily_sets to anon, authenticated;

-- 4 · one play per person per day
create table if not exists daily_plays (
  user_id   uuid not null references profiles(id) on delete cascade,
  play_date date not null,
  set_num   text not null,
  guess     numeric(10,2) not null,
  correct   boolean not null,
  saved     boolean not null default false,   -- a streak save was spent
  played_at timestamptz not null default now(),
  primary key (user_id, play_date)
);
alter table daily_plays enable row level security;
drop policy if exists "own plays" on daily_plays;
create policy "own plays" on daily_plays for select using (auth.uid() = user_id);
grant select on daily_plays to authenticated;

-- 5 · seed players. The board is empty on day one, and an empty board is
--    not a board. These are placeholders, clearly named in their own table,
--    to be retired as real players arrive. They never touch profiles.
create table if not exists leaderboard_seed (
  username    text primary key,
  streak      integer not null,
  best_streak integer not null
);
insert into leaderboard_seed (username, streak, best_streak) values
  ('brickwizard',41,41),('sealedforever',37,44),('ucs_hoarder',33,33),
  ('minifig_mike',29,35),('modularmaven',27,27),('polybagpete',24,31),
  ('darth_bricks',22,22),('the_flipper',21,28),('castle_kev',19,19),
  ('nostalgia_nate',18,26),('bricktrader_88',17,17),('retired_sets',16,22),
  ('sarah_builds',15,15),('bricklink_ben',14,19),('holdnotsold',13,13),
  ('yodasbricks',12,17),('technic_tom',11,11),('idea_ivan',10,14),
  ('winterwillow',9,9),('galaxy_gus',9,12),('brickbargain',8,8),
  ('marketmarco',8,11),('pieces_per_dollar',7,7),('hoth_hannah',7,9),
  ('sam_studs',6,6),('quietcollector',6,8),('creator_cal',5,5),
  ('icons_izzy',5,7),('lego_liz',4,4),('brickbyte',4,6),
  ('new_in_box',3,3),('daily_dan',3,5),('first_timer',2,2),
  ('just_looking',2,3),('brick_curious',1,1),('marge_plays',1,2)
on conflict do nothing;

-- 6 · the board: real players with usernames, plus the seed, top 100 by streak
create or replace view leaderboard as
select username, streak, best_streak, false as seed
  from profiles where username is not null and streak > 0
union all
select username, streak, best_streak, true as seed
  from leaderboard_seed
order by streak desc, best_streak desc, username
limit 100;
grant select on leaderboard to anon, authenticated;

-- 7 · claim a username, atomically, with the rules enforced
create or replace function claim_username(u text) returns text
language plpgsql security definer as $$
begin
  if not username_ok(u) then
    return 'That name is not allowed. Use 3 to 20 letters, numbers or underscores, starting with a letter.';
  end if;
  if exists (select 1 from profiles where lower(username) = lower(u) and id <> auth.uid()) then
    return 'That name is taken.';
  end if;
  if exists (select 1 from leaderboard_seed where lower(username) = lower(u)) then
    return 'That name is taken.';
  end if;
  update profiles set username = u where id = auth.uid();
  return 'ok';
end $$;
grant execute on function claim_username(text) to authenticated;

notify pgrst, 'reload schema';

-- ============================================================================
-- 8 · the game itself, as database functions
--
-- Everything that decides a result lives here, not in the browser: which set
-- today's is, what the four options are, whether an answer was right, and how
-- the streak moves. That way the same set and options reach everyone, and
-- nobody can hand themselves a streak from the console.
-- ============================================================================

-- Round to the nearest "price-shaped" number.
create or replace function round_nice(n numeric) returns numeric
language sql immutable as $$
  select case
    when n < 50   then round(n / 5) * 5
    when n < 200  then round(n / 10) * 10
    when n < 1000 then round(n / 25) * 25
    else               round(n / 50) * 50 end
$$;

-- Today's set, chosen on first request and then fixed for the day. The pick
-- is a hash of the date, so it's stable even if two people request at once.
create or replace function today_set() returns json
language plpgsql security definer as $$
declare
  d date := (now() at time zone 'utc')::date;
  chosen text;
  r record;
  v numeric;
  opts numeric[];
  seed int;
begin
  select set_num into chosen from daily_sets where play_date = d;

  if chosen is null then
    select set_num into chosen
      from set_values
     where plausible = true and image_url is not null
       and comp_count >= 8 and market_value >= 25 and msrp is not null
     order by md5(set_num || d::text)
     limit 1;
    if chosen is null then return null; end if;
    insert into daily_sets (play_date, set_num) values (d, chosen)
      on conflict (play_date) do nothing;
    select set_num into chosen from daily_sets where play_date = d;
  end if;

  select * into r from set_values where set_num = chosen;
  v := r.market_value;

  -- Four options in a believable spread, rounded to prices people would
  -- actually type. Which ratios are used depends on the date, so the shape
  -- varies day to day but is identical for every player.
  seed := ('x' || substr(md5(d::text), 1, 8))::bit(32)::int;
  opts := case abs(seed) % 4
    when 0 then array[v * 0.5, v, v * 1.6, v * 2.5]
    when 1 then array[v * 0.4, v * 0.65, v, v * 1.7]
    when 2 then array[v * 0.6, v, v * 1.4, v * 2.2]
    else        array[v * 0.35, v * 0.7, v, v * 1.5]
  end;

  return json_build_object(
    'play_date', d,
    'set_num', r.set_num, 'name', r.name, 'theme', r.theme,
    'year', r.year_released, 'pieces', r.pieces, 'image_url', r.image_url,
    'msrp', r.msrp, 'retired', r.retired_at is not null,
    'options', (select json_agg(round_nice(o) order by o) from unnest(opts) o),
    'answer_index', null   -- never sent to the client
  );
end $$;

grant execute on function today_set() to anon, authenticated;
grant execute on function round_nice(numeric) to anon, authenticated;

-- Submit a guess. Records the play, moves the streak, spends a save if one
-- is needed and available, and returns everything the client should show.
create or replace function play_daily(p_guess numeric) returns json
language plpgsql security definer as $$
declare
  d date := (now() at time zone 'utc')::date;
  uid uuid := auth.uid();
  ds record; sv record; p record;
  truth numeric; is_right boolean; used_save boolean := false;
  gap_days int;
begin
  if uid is null then
    return json_build_object('error', 'Sign in to play.');
  end if;
  if exists (select 1 from daily_plays where user_id = uid and play_date = d) then
    return json_build_object('error', 'You have already played today.');
  end if;

  select * into ds from daily_sets where play_date = d;
  if ds is null then return json_build_object('error', 'No set today yet.'); end if;
  select * into sv from set_values where set_num = ds.set_num;
  select * into p from profiles where id = uid;

  truth := round_nice(sv.market_value);
  is_right := (p_guess = truth);

  -- days since the last play; null if never played
  gap_days := case when p.last_played is null then null else d - p.last_played end;

  if is_right then
    if gap_days is null or gap_days = 1 or p.streak = 0 then
      p.streak := p.streak + 1;
    elsif gap_days > 1 and p.streak_saves > 0 then
      -- missed a day, but a save covers it
      p.streak := p.streak + 1; p.streak_saves := p.streak_saves - 1; used_save := true;
    else
      p.streak := 1;
    end if;
    -- earn a save back every seven in a row, capped at three
    if p.streak % 7 = 0 and p.streak_saves < 3 then
      p.streak_saves := p.streak_saves + 1;
    end if;
  else
    if p.streak_saves > 0 and p.streak > 0 then
      p.streak_saves := p.streak_saves - 1; used_save := true;   -- streak preserved
    else
      p.streak := 0;
    end if;
  end if;

  update profiles set
    streak = p.streak,
    best_streak = greatest(best_streak, p.streak),
    streak_saves = p.streak_saves,
    last_played = d,
    plays = plays + 1,
    correct = correct + (case when is_right then 1 else 0 end)
  where id = uid;

  insert into daily_plays (user_id, play_date, set_num, guess, correct, saved)
    values (uid, d, ds.set_num, p_guess, is_right, used_save);

  return json_build_object(
    'correct', is_right, 'truth', truth, 'market_value', sv.market_value,
    'comp_count', sv.comp_count, 'msrp', sv.msrp,
    'streak', p.streak, 'best_streak', greatest(p.best_streak, p.streak),
    'saves', p.streak_saves, 'used_save', used_save,
    'has_username', p.username is not null
  );
end $$;
grant execute on function play_daily(numeric) to authenticated;

-- Has this person already played today? If so, what happened.
create or replace function my_daily_status() returns json
language sql security definer as $$
  select json_build_object(
    'played', exists (select 1 from daily_plays
                       where user_id = auth.uid()
                         and play_date = (now() at time zone 'utc')::date),
    'result', (select json_build_object('correct', correct, 'guess', guess)
                 from daily_plays where user_id = auth.uid()
                  and play_date = (now() at time zone 'utc')::date),
    'streak', (select streak from profiles where id = auth.uid()),
    'best', (select best_streak from profiles where id = auth.uid()),
    'saves', (select streak_saves from profiles where id = auth.uid()),
    'username', (select username from profiles where id = auth.uid()),
    'rank', (select count(*) + 1 from leaderboard l
              where l.streak > coalesce((select streak from profiles where id = auth.uid()), 0))
  );
$$;
grant execute on function my_daily_status() to authenticated;

notify pgrst, 'reload schema';

-- ============================================================================
-- 9 · a consistent product photograph
-- Rebrickable's image is sometimes box art, sometimes a render, and the white
-- behind it never quite matches the card. BrickLink photographs every set the
-- same way on the same white. Stored separately; the site prefers it when
-- present and falls back otherwise, so nothing goes blank while the sweep
-- fills it in.
-- ============================================================================
alter table sets add column if not exists bricklink_image text;

drop view if exists set_values cascade;
create view set_values as
select
  s.set_num, s.name, s.theme, s.subtheme, s.year_released,
  s.pieces, s.minifigs,
  coalesce(s.bricklink_image, s.image_url) as image_url,
  s.image_url as catalog_image,
  s.msrp, s.retired_at,
  c.median_sold as market_value, c.comp_count, c.spread, c.source,
  c.observed_at as valued_at,
  case when c.comp_count is null then null
       when c.comp_count >= 12 then 'high'
       when c.comp_count >= 6  then 'medium' else 'low' end as confidence,
  s.sold_new, s.sold_new_qty, s.sold_new_min, s.sold_new_max,
  s.sold_used, s.sold_used_qty, s.part_out_value, s.bricklink_checked_at,
  case when s.msrp > 0 and c.median_sold is not null
       then round((c.median_sold / s.msrp)::numeric, 2) end as msrp_multiple,
  case when s.pieces > 0 and c.median_sold is not null
       then round((c.median_sold / s.pieces)::numeric, 3) end as value_per_piece,
  case when c.median_sold is null then null
       when c.comp_count >= 3 then true else false end as plausible
from sets s
left join lateral (
  select * from comps where comps.set_num = s.set_num
   order by observed_at desc limit 1
) c on true;
grant select on set_values to anon, authenticated;

-- the cascade takes theme_stats and leaderboard with it — rebuild both
create or replace view theme_stats as
select s.theme, count(*) as set_count,
  round((percentile_cont(0.5) within group (order by s.market_value))::numeric, 2) as median_value,
  round(max(s.market_value)::numeric, 2) as top_value,
  round(avg(s.value_per_piece)::numeric, 4) as avg_value_per_piece,
  round(avg(s.market_value)::numeric, 2) as avg_value,
  min(s.year_released) as first_year, max(s.year_released) as last_year
from set_values s
where s.theme is not null and s.market_value is not null
  and s.plausible is true and s.theme <> 'Duplo'
group by s.theme having count(*) >= 3;
grant select on theme_stats to anon, authenticated;

create or replace view leaderboard as
select username, streak, best_streak, false as seed
  from profiles where username is not null and streak > 0
union all
select username, streak, best_streak, true as seed from leaderboard_seed
order by streak desc, best_streak desc, username limit 100;
grant select on leaderboard to anon, authenticated;

notify pgrst, 'reload schema';
