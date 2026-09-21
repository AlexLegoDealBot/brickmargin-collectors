-- ============================================================================
-- BrickMargin · migration 021 · a living leaderboard
--
-- One hundred seed players instead of thirty-six, and they behave like people:
-- each day most of them play on and their streaks grow, a few forget and fall
-- off the board, and a few newcomers appear near the bottom. The churn runs
-- once a day, the first time anyone opens the Daily Set. Real players sit in
-- the same ranking and climb past them; nothing distinguishes the two except
-- that seeds can't answer the actual question.
--
-- Seed names are curated here. Real usernames pass username_ok(), which
-- rejects the blocked list, so nothing inappropriate reaches the board.
-- ============================================================================
alter table leaderboard_seed add column if not exists last_played date not null default current_date;
alter table leaderboard_seed add column if not exists active boolean not null default true;

delete from leaderboard_seed;
insert into leaderboard_seed (username, streak, best_streak, last_played) values
  ('MattEli',2,11,current_date - 0),
  ('mralex',2,8,current_date - 1),
  ('MorganFL86',2,8,current_date - 0),
  ('QuietSealed',5,7,current_date - 0),
  ('moc99',4,12,current_date - 1),
  ('bigmorgan',9,21,current_date - 0),
  ('MorganIcons',2,2,current_date - 1),
  ('RaySF83',3,9,current_date - 1),
  ('bricks28',19,21,current_date - 0),
  ('RyanCA86',7,15,current_date - 0),
  ('ColeKai',4,12,current_date - 1),
  ('NateUK16',2,10,current_date - 1),
  ('KyleBeth',2,2,current_date - 0),
  ('grumpytaylor',3,6,current_date - 0),
  ('classicnyc',12,18,current_date - 1),
  ('KyleFL50',17,20,current_date - 1),
  ('vintagebos',2,5,current_date - 1),
  ('tinymorgan',2,12,current_date - 1),
  ('BuildsQuiet',3,10,current_date - 1),
  ('bencastle',2,12,current_date - 1),
  ('jamie_afol',2,2,current_date - 1),
  ('OldMOC',6,14,current_date - 1),
  ('Brick209',6,16,current_date - 0),
  ('vintagetx',2,7,current_date - 1),
  ('brick98',2,13,current_date - 0),
  ('Polybag371',60,60,current_date - 0),
  ('LukeAUS49',7,18,current_date - 1),
  ('AvaMOC5',3,9,current_date - 1),
  ('Plates416',3,11,current_date - 0),
  ('noah1996',21,22,current_date - 0),
  ('mrtaylor',2,8,current_date - 1),
  ('plates51',3,7,current_date - 0),
  ('JessRetired7',5,15,current_date - 0),
  ('vintage48',2,6,current_date - 1),
  ('joshretired',3,8,current_date - 0),
  ('MattTiles',26,29,current_date - 0),
  ('EvanPlates2',7,17,current_date - 1),
  ('LeoSets9',14,19,current_date - 0),
  ('ModularsGrumpy',4,4,current_date - 0),
  ('sets56',2,4,current_date - 0),
  ('ashsets',2,3,current_date - 0),
  ('lazymatt',7,8,current_date - 1),
  ('AshRiley',2,10,current_date - 0),
  ('finn1989',3,6,current_date - 0),
  ('MiaRetired4',2,7,current_date - 1),
  ('owen_technic',2,13,current_date - 0),
  ('sets28',2,11,current_date - 1),
  ('TechnicGrumpy',4,9,current_date - 1),
  ('JamieNinjago',11,12,current_date - 0),
  ('VintageLazy',7,17,current_date - 1),
  ('Icons948',11,16,current_date - 0),
  ('NatePolybag',2,3,current_date - 1),
  ('modularsden',5,9,current_date - 0),
  ('will_icons',2,8,current_date - 1),
  ('NateMOC',2,13,current_date - 1),
  ('ChrisRetired',2,4,current_date - 0),
  ('ColeBOS62',6,9,current_date - 1),
  ('BigPirates',5,10,current_date - 1),
  ('kelly_icons',7,16,current_date - 1),
  ('LuckyMinifigs',2,9,current_date - 0),
  ('NatePDX55',2,7,current_date - 0),
  ('MorganBrick5',2,9,current_date - 1),
  ('justrob',2,12,current_date - 1),
  ('happyjoel',2,4,current_date - 0),
  ('dan2008',2,11,current_date - 1),
  ('ash_retired',4,10,current_date - 0),
  ('ClassicOld',3,8,current_date - 1),
  ('sean2005',2,12,current_date - 1),
  ('Icons622',11,21,current_date - 0),
  ('beth_space',2,12,current_date - 1),
  ('NinjagoBig',2,8,current_date - 0),
  ('JordanRetired5',2,10,current_date - 1),
  ('PolybagGrumpy',2,11,current_date - 1),
  ('LuckySealed',7,19,current_date - 1),
  ('RyanPHX90',7,14,current_date - 1),
  ('LukeMinifigs',2,14,current_date - 0),
  ('kyleafol',4,14,current_date - 1),
  ('RayDEN54',2,14,current_date - 0),
  ('sealed95',6,14,current_date - 1),
  ('joel_classic',4,9,current_date - 0),
  ('polybagpdx',3,6,current_date - 0),
  ('AlexATX20',36,38,current_date - 1),
  ('technic24',25,32,current_date - 0),
  ('casey2006',3,5,current_date - 1),
  ('tom1987',2,8,current_date - 0),
  ('sealedsf',2,6,current_date - 1),
  ('miatechnic',2,10,current_date - 0),
  ('SealedSleepy',48,55,current_date - 1),
  ('mia_vintage',2,7,current_date - 1),
  ('nate1980',2,7,current_date - 1),
  ('RayPDX42',2,9,current_date - 0),
  ('RetiredMs',2,14,current_date - 1),
  ('tinysean',2,14,current_date - 1),
  ('josh_moc',2,6,current_date - 1),
  ('Retired713',2,11,current_date - 0),
  ('sleepytheo',6,7,current_date - 1),
  ('owen1982',33,42,current_date - 0),
  ('JackSpace',7,15,current_date - 1),
  ('technicdfw',3,13,current_date - 1),
  ('JustMinifigs',2,3,current_date - 0);

-- The daily churn. Idempotent per day: it does nothing if it already ran.
create table if not exists seed_churn_log (ran_on date primary key);

create or replace function churn_seed() returns void
language plpgsql security definer as $$
declare d date := (now() at time zone 'utc')::date;
begin
  if exists (select 1 from seed_churn_log where ran_on = d) then return; end if;
  insert into seed_churn_log (ran_on) values (d);

  -- most active players play on: streak +1
  update leaderboard_seed set streak = streak + 1, best_streak = greatest(best_streak, streak + 1), last_played = d
   where active and random() < 0.86;

  -- a few forget: they drop off the board (streak resets, marked inactive)
  update leaderboard_seed set active = false, streak = 0
   where active and last_played < d and random() < 0.6;

  -- a few newcomers (or returners) appear with a short streak
  update leaderboard_seed set active = true, streak = 1 + floor(random() * 3)::int, last_played = d
   where not active and random() < 0.12;

  -- nobody sits at the top forever: the longest streaks occasionally break
  update leaderboard_seed set active = false, streak = 0
   where active and streak > 40 and random() < 0.15;
end $$;
grant execute on function churn_seed() to anon, authenticated;

-- the board only shows active seeds with a streak
create or replace view leaderboard as
select username, streak, best_streak, false as seed
  from profiles where username is not null and streak > 0
union all
select username, streak, best_streak, true as seed
  from leaderboard_seed where active and streak > 0
order by streak desc, best_streak desc, username limit 100;
grant select on leaderboard to anon, authenticated;

-- churn happens the first time today's set is requested
create or replace function today_set() returns json
language plpgsql security definer as $$
declare
  d date := (now() at time zone 'utc')::date;
  chosen text; r record; v numeric; opts numeric[]; seed int;
begin
  perform churn_seed();
  select set_num into chosen from daily_sets where play_date = d;
  if chosen is null then
    select set_num into chosen from set_values
     where plausible = true and image_url is not null and comp_count >= 8 and market_value >= 25 and msrp is not null
     order by md5(set_num || d::text) limit 1;
    if chosen is null then return null; end if;
    insert into daily_sets (play_date, set_num) values (d, chosen) on conflict (play_date) do nothing;
    select set_num into chosen from daily_sets where play_date = d;
  end if;
  select * into r from set_values where set_num = chosen;
  v := r.market_value;
  seed := ('x' || substr(md5(d::text), 1, 8))::bit(32)::int;
  opts := case abs(seed) % 4
    when 0 then array[v * 0.5, v, v * 1.6, v * 2.5]
    when 1 then array[v * 0.4, v * 0.65, v, v * 1.7]
    when 2 then array[v * 0.6, v, v * 1.4, v * 2.2]
    else        array[v * 0.35, v * 0.7, v, v * 1.5] end;
  return json_build_object(
    'play_date', d, 'set_num', r.set_num, 'name', r.name, 'theme', r.theme,
    'year', r.year_released, 'pieces', r.pieces, 'image_url', r.image_url,
    'msrp', r.msrp, 'retired', r.retired_at is not null,
    'options', (select json_agg(round_nice(o) order by o) from unnest(opts) o),
    'answer_index', null);
end $$;
grant execute on function today_set() to anon, authenticated;

notify pgrst, 'reload schema';
