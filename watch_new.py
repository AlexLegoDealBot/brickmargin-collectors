#!/usr/bin/env python3
"""
BrickMargin · the fast lane
===========================

The deals collector asks eBay about one set at a time, which means a listing
can sit for hours before its set comes round again. This does the opposite:
one broad query for LEGO listings posted in the last few minutes, matched
against the catalogue we already hold in memory. A good listing is found
within a minute or two of going live, which on an underpriced set is the
whole game.

It does not replace the per-set collector — that one is thorough, this one is
quick. Both write to the same deals table and the same filters apply, so
nothing reaches the board that would not have reached it slowly.

Run it on a short loop (every two minutes) for as long as the runner allows,
then exit; the workflow starts it again.

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY, EBAY_APP_ID, EBAY_CERT_ID
             MINUTES   how long to keep looping, default 50
             EVERY     seconds between sweeps, default 90
             PROBE     default true
"""
import base64, os, re, sys, time
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client

VERSION = "2.1-ebaylabels"

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, backoff_factor=1.2, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]), raise_on_status=False)))

def env(n, d=None):
    v = (os.environ.get(n) or "").strip()
    if not v and d is None: sys.exit(f"ERROR: {n} is required")
    return v or d

SB_URL = env("SUPABASE_URL"); SB_KEY = env("SUPABASE_SERVICE_KEY")
APP_ID = env("EBAY_APP_ID"); CERT_ID = env("EBAY_CERT_ID")
MINUTES = int(env("MINUTES", "50")); EVERY = int(env("EVERY", "90"))
PROBE = env("PROBE", "true").lower() != "false"
# Posting after the loop ends would mean a fifty-minute delay on a feed whose
# whole purpose is speed. The announcement happens in the same breath as the
# find, so Discord sees a deal about ninety seconds after it is listed.
HOOK = (os.environ.get("DISCORD_WEBHOOK") or "").strip()
HOOK_RETAIL = (os.environ.get("DISCORD_WEBHOOK_RETAIL") or "").strip() or HOOK
HOOK_MARKET = (os.environ.get("DISCORD_WEBHOOK_MARKET") or "").strip() or HOOK
CAMPAIGN = (os.environ.get("EBAY_CAMPAIGN_ID") or "5339178457").strip()
MIN_POST_SCORE = float(os.environ.get("MIN_POST_SCORE") or 4.0)

# #best-deals: the handful a day worth a notification. A score alone runs
# generous — 30% off a $20 set scores like 30% off a $400 one — so "best"
# means a big percentage AND real money: at least 30% off and at least $60
# saved. Measured on a week of real deals, that is about nine a day,
# averaging 42% off and $126 saved. The ceiling is a safety net.
HOOK_BEST = (os.environ.get("DISCORD_WEBHOOK_BEST") or "").strip()
BEST_MIN_PCT = float(os.environ.get("BEST_MIN_PCT") or 30)
BEST_MIN_SAVING = float(os.environ.get("BEST_MIN_SAVING") or 60)
BEST_DAILY_CAP = int(os.environ.get("BEST_DAILY_CAP") or 15)
# ...or very big money at a decent percentage: $238 off a UCS Falcon at 28%
# is exactly the deal people want pinged about, and a flat 30% rule missed it.
BEST_BIG_SAVING = float(os.environ.get("BEST_BIG_SAVING") or 150)
BEST_BIG_MIN_PCT = float(os.environ.get("BEST_BIG_MIN_PCT") or 20)
BEST_ROLE_ID = (os.environ.get("DISCORD_BEST_ROLE_ID") or "").strip()   # optional: ping a role

def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)
usd = lambda n: f"${float(n):,.2f}"

# --- the filters live in the slow collector; import them so there is one
#     definition of what counts as a real, whole, sealed set ---------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_deals import (                      # noqa: E402
    is_relevant, shipping_cost, condition_of, description_rejects,
    MIN_FEEDBACK, MIN_FEEDBACK_COUNT,
)

EBAY = "https://api.ebay.com"

def token():
    r = HTTP.post(f"{EBAY}/identity/v1/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": "Basic " + base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

def newest(tok, limit=200):
    """Everything LEGO listed most recently, newest first."""
    r = HTTP.get(f"{EBAY}/buy/browse/v1/item_summary/search",
        headers={"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params={"q": "lego", "category_ids": "19006", "limit": limit,
                "sort": "newlyListed", "filter": "conditions:{NEW},buyingOptions:{FIXED_PRICE},itemLocationCountry:US"},
        timeout=30)
    if r.status_code == 429:
        log("  rate limited — backing off 60s"); time.sleep(60); return []
    if r.status_code != 200:
        log(f"  HTTP {r.status_code}"); return []
    return r.json().get("itemSummaries", []) or []

def affiliate(url: str) -> str:
    """Every link out earns, wherever it is posted."""
    if not url or not CAMPAIGN:
        return url
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        u = urlparse(url); q = dict(parse_qsl(u.query))
        q.update({"mkevt": "1", "mkcid": "1", "mkrid": "711-53200-19255-0",
                  "siteid": "0", "campid": CAMPAIGN, "toolid": "10001", "customid": "discord"})
        return urlunparse(u._replace(query=urlencode(q)))
    except Exception:
        return url


def big_image(url: str) -> str:
    """eBay encodes the size in the filename — s-l225 is a thumbnail, s-l1600
    is the full photograph. Swapping it costs nothing and fills the embed."""
    if not url:
        return url
    return re.sub(r"/s-l\d+\.", "/s-l1600.", url)


def saving_of(d):
    """What the post itself claims you save: off retail for a shop-price
    deal, off resale value for a retired one."""
    ref = d.get("msrp") if d.get("deal_type") == "retail" and d.get("msrp") else d.get("market_value")
    return float(ref or 0) - float(d.get("total_price") or 0)


def is_best(d):
    pct = float(d.get("pct_off_msrp") or d.get("discount_pct") or 0)
    save = saving_of(d)
    return ((pct >= BEST_MIN_PCT and save >= BEST_MIN_SAVING)
            or (pct >= BEST_BIG_MIN_PCT and save >= BEST_BIG_SAVING))


# Theme roles. Members pick the themes they collect when they join, and get
# pinged when a deal in one of them is at least 25% off and saves $25 or more.
# Measured on a month of real deals that's ~4–5 pings a day for Star Wars,
# 2–3 for Disney or Icons, about one or fewer for the rest — useful, not
# mutable. The secret is JSON mapping our theme names to Discord role IDs:
#   {"Star Wars": "1234...", "Icons": "5678...", ...}
import json as _json
try:
    THEME_ROLES = {k: str(v) for k, v in _json.loads(os.environ.get("DISCORD_THEME_ROLES") or "{}").items() if v}
except Exception:
    THEME_ROLES = {}
THEME_MIN_PCT = float(os.environ.get("THEME_MIN_PCT") or 25)
THEME_MIN_SAVING = float(os.environ.get("THEME_MIN_SAVING") or 25)


def theme_ping(d):
    """The role to ping for this deal, or None. Never more than one role."""
    role = THEME_ROLES.get(d.get("theme") or "")
    if not role:
        return None
    pct = float(d.get("pct_off_msrp") or d.get("discount_pct") or 0)
    return role if (pct >= THEME_MIN_PCT and saving_of(d) >= THEME_MIN_SAVING) or is_best(d) else None


def best_room_has_space(client):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    try:
        n = (client.table("discord_posts").select("item_id", count="exact")
             .gte("best_at", today).execute()).count or 0
        return n < BEST_DAILY_CAP
    except Exception:
        return False


def announce(d, client):
    """One deal, straight to Discord, remembered so it is never posted twice."""
    # Still in production and under shop price → the "buy it" room.
    # Retired and under what it resells for → the "flip it" room.
    target = HOOK_RETAIL if d.get("deal_type") == "retail" else HOOK_MARKET
    if not target:
        return
    if float(d.get("score") or 0) < MIN_POST_SCORE:
        log(f"    below post score ({float(d.get('score') or 0):.1f} < {MIN_POST_SCORE}) — not posted")
        return
    try:
        already = (client.table("discord_posts").select("item_id")
                   .eq("item_id", d["item_id"]).limit(1).execute()).data
        if already:
            return
    except Exception:
        pass

    pct = float(d.get("pct_off_msrp") or d.get("discount_pct") or 0)
    if pct >= 40:   colour, band = 0xE3000B, "🔥 HOT"
    elif pct >= 25: colour, band = 0xFF8A00, "⭐ Strong"
    elif pct >= 15: colour, band = 0xFFD500, "👍 Good"
    else:           colour, band = 0x4CBB17, "Worth a look"
    n = max(1, min(10, round(float(d.get("score") or 0))))
    bar = "█" * n + "░" * (10 - n)
    saving = (f"You save **{usd(float(d['msrp']) - float(d['total_price']))}** off retail"
              if d.get("msrp") else
              f"About **{usd(float(d['market_value']) - float(d['total_price']))}** under what it resells for")
    off = (f"{pct:.0f}% under retail" if d.get("deal_type") == "retail" else f"{pct:.0f}% under market")

    embed = {
        # EPN requires every eBay link to say it goes to eBay. It does, three
        # times: the author line, the clickable title, and a labelled link.
        "author": {"name": f"eBay listing · {band} · " + ("under retail" if d.get("deal_type") == "retail" else "under resale"),
                   "icon_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
        "title": ((d.get("set_name") or d["title"])[:200] + " — View on eBay"),
        "url": affiliate(d["item_url"]),
        "description": (
            f"**[Buy on eBay →]({affiliate(d['item_url'])})**\n"
            f"## {usd(d['total_price'])}  ·  {off}\n"
            f"{saving}\n\n"
            f"*{d['title'][:140]}*"
        ),
        "color": colour,
        "image": {"url": big_image(d["image_url"])} if d.get("image_url") else None,
        "fields": [
            {"name": "Retail", "value": usd(d["msrp"]) if d.get("msrp") else "—", "inline": True},
            {"name": "Resells for", "value": usd(d["market_value"]), "inline": True},
            {"name": "Condition", "value": (d.get("condition") or "new").title(), "inline": True},
            {"name": "Deal score", "value": f"`{bar}` {float(d.get('score') or 0):.1f}", "inline": False},
            {"name": "Set page", "value": f"[{d['set_num'].split('-')[0]} on BrickMargin](https://www.brickmargin.com/set/{d['set_num']})", "inline": True},
            {"name": "Seller", "value": d.get("seller") or "—", "inline": True},
        ],
        "footer": {"text": "Affiliate link to eBay: BrickMargin may earn a commission if you buy · price includes shipping",
                   "icon_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        main = {"embeds": [embed], "username": "BrickMargin",
                "avatar_url": "https://www.brickmargin.com/brickmargin-round-512.png"}
        role = theme_ping(d)
        if role:
            # mention only that one role — never @everyone, never anyone else
            main["content"] = f"<@&{role}>"
            main["allowed_mentions"] = {"parse": [], "roles": [role]}
        r = HTTP.post(target, json=main, timeout=20)
        if r.status_code < 300:
            client.table("discord_posts").upsert(
                {"item_id": d["item_id"], "set_num": d["set_num"]}, on_conflict="item_id").execute()
            time.sleep(1.2)          # Discord rate-limits webhooks

            if HOOK_BEST and is_best(d) and best_room_has_space(client):
                best = {**embed, "author": {**embed["author"],
                        "name": "🏆 Best deal · eBay listing · " + ("under retail" if d.get("deal_type") == "retail" else "under resale")}}
                payload = {"embeds": [best], "username": "BrickMargin",
                           "avatar_url": "https://www.brickmargin.com/brickmargin-round-512.png"}
                if BEST_ROLE_ID:
                    # mention only that role — never @everyone, never anyone else
                    payload["content"] = f"<@&{BEST_ROLE_ID}>"
                    payload["allowed_mentions"] = {"parse": [], "roles": [BEST_ROLE_ID]}
                rb = HTTP.post(HOOK_BEST, json=payload, timeout=20)
                if rb.status_code < 300:
                    client.table("discord_posts").update(
                        {"best_at": datetime.now(timezone.utc).isoformat()}).eq("item_id", d["item_id"]).execute()
                    log(f"    🏆 also posted to #best-deals (saves ${saving_of(d):,.0f})")
                    time.sleep(1.2)
                else:
                    log(f"    best-deals HTTP {rb.status_code}")
        else:
            log(f"    discord HTTP {r.status_code}")
    except Exception as exc:
        log(f"    discord failed: {str(exc)[:80]}")


def main():
    log(f"BrickMargin fast lane — version {VERSION}  probe={PROBE}  {MINUTES}m at {EVERY}s")
    log(f"  discord: retail={'set' if HOOK_RETAIL else 'MISSING'} · "
        f"resale={'set' if HOOK_MARKET else 'MISSING'} · best={'set' if HOOK_BEST else 'off'} · post score >= {MIN_POST_SCORE}")
    client = create_client(SB_URL, SB_KEY)

    # The catalogue, once, in memory. A set number appearing in a title is
    # what makes the match, so this is a dictionary lookup, not a query.
    rows = []
    for start in range(0, 12000, 1000):
        batch = (client.table("set_values")
                 .select("set_num,name,theme,msrp,market_value,comp_count,retired_at,sold_new,sold_new_qty,pieces")
                 .eq("is_accessory", False).not_.is_("market_value", "null")
                 .range(start, start + 999).execute()).data or []
        rows.extend(batch)
        if len(batch) < 1000: break
    by_number = {}
    for r in rows:
        by_number.setdefault(r["set_num"].split("-")[0], r)
    log(f"  {len(by_number):,} sets in memory")

    tok = token(); seen = set(); posted = 0
    stop = datetime.now(timezone.utc) + timedelta(minutes=MINUTES)
    sweeps = 0

    while datetime.now(timezone.utc) < stop:
        sweeps += 1
        items = newest(tok)
        fresh = [i for i in items if i.get("itemId") not in seen]
        for i in items: seen.add(i.get("itemId"))
        found = []

        for item in fresh:
            title = item.get("title", "")
            # a set number in the title is the only cheap way to match
            nums = re.findall(r"(?<!\d)(\d{4,7})(?!\d)", title)
            row = next((by_number[n] for n in nums if n in by_number), None)
            if not row: continue

            keep, _ = is_relevant(title, row["set_num"].split("-")[0], row.get("name"), row.get("theme"))
            if not keep: continue

            try: price = float(item["price"]["value"])
            except Exception: continue
            # sealed only, same as the slow lane
            if (condition_of(item) or "").lower() != "new":
                continue

            ship = shipping_cost(item)
            if ship is None:
                ship = 0.0 if price >= 50 else 8.0      # eBay omits it on free shipping
            total = price + ship

            seller = item.get("seller") or {}
            try:
                if float(seller.get("feedbackPercentage") or 0) < MIN_FEEDBACK: continue
                if int(seller.get("feedbackScore") or 0) < MIN_FEEDBACK_COUNT: continue
            except Exception: continue

            value = float(row["sold_new"]) if row.get("sold_new") and (row.get("sold_new_qty") or 0) >= 3 else float(row["market_value"])
            msrp = float(row["msrp"]) if row.get("msrp") else None
            in_production = not row.get("retired_at")

            # in-production sets are judged against retail, retired against resale
            if in_production and msrp:
                if total >= msrp * 0.88: continue
                kind, off = "retail", round((1 - total / msrp) * 100, 2)
            else:
                if total >= value * 0.85: continue
                kind, off = "market", round((1 - total / value) * 100, 2)

            # the same last gate as the slow lane: read the listing itself
            flag = description_rejects(str(item.get("itemId")), tok)
            if flag:
                log(f"    skipped — description says \"{flag}\"")
                continue

            # eBay fees (13.25%), promoted (2%), payment (2.9% + 30c) and
            # postage come off what a flip would actually clear.
            net = value - value * 0.1525 - (value * 0.029 + 0.30) - ship

            found.append({
                "item_id": str(item.get("itemId")), "set_num": row["set_num"],
                "title": title[:200],
                "item_price": round(price, 2), "shipping": round(ship, 2), "total_price": round(total, 2),
                "net_if_flipped": round(net, 2), "retired": bool(row.get("retired_at")),
                "market_value": value, "msrp": msrp, "pct_off_msrp": off if kind == "retail" else None,
                "discount_pct": round((1 - total / value) * 100, 2),
                "margin_pct": round((value / total - 1) * 100, 2),
                "condition": condition_of(item) or "new",
                "seller": seller.get("username"), "feedback_pct": float(seller.get("feedbackPercentage") or 0),
                "image_url": (item.get("image") or {}).get("imageUrl"),
                "item_url": item.get("itemWebUrl", ""), "deal_type": kind,
                "source": "fast",
                # a simple, explainable score: the discount, tempered by how
                # much evidence stands behind the value it is measured against
                "score": round(min(10.0, (off if kind == "retail" else (1 - total / value) * 100) / 6
                                   + min(3.0, (row.get("comp_count") or 0) / 12)), 1),
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "last_seen": datetime.now(timezone.utc).isoformat(),
            })

        if found:
            for f in found:
                log(f"  ★ {f['title'][:44]:46} {usd(f['total_price']):>10}  "
                    f"{float(f['pct_off_msrp'] or f['discount_pct']):.0f}% off  ({f['deal_type']})")
            if not PROBE:
                try:
                    client.table("deals").upsert(found, on_conflict="item_id").execute()
                    posted += len(found)
                except Exception as exc:
                    log(f"    database write failed: {str(exc)[:100]}")

                # Announcing is a separate concern: a Discord problem should
                # never look like a database problem, and it must not stop
                # the next sweep.
                try:
                    for f in sorted(found, key=lambda x: -float(x.get("score") or 0)):
                        # the catalogue is already in memory; give the embed a
                        # clean set name rather than a seller's title
                        cat = by_number.get(f["set_num"].split("-")[0]) or {}
                        announce({**f, "set_name": cat.get("name"), "theme": cat.get("theme")}, client)
                except Exception as exc:
                    log(f"    discord post failed: {type(exc).__name__}: {str(exc)[:100]}")
        log(f"  sweep {sweeps}: {len(items)} listings, {len(fresh)} new, {len(found)} deals")
        time.sleep(EVERY)

    log(f"Done. {sweeps} sweeps, {posted} deals written.")

if __name__ == "__main__":
    main()
