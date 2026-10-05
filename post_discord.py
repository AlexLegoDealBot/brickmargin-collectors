#!/usr/bin/env python3
"""
BrickMargin · Discord deal feed

Posts new deals to a Discord channel as they are found. Runs after the deals
collector; only ever posts a listing once.

A webhook is all Discord needs — no bot, no hosting, no permissions to
manage. Create one in the channel (Edit Channel → Integrations → Webhooks →
New Webhook → Copy URL) and store it as DISCORD_WEBHOOK.

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY, DISCORD_WEBHOOK
             MIN_SCORE (default 5.0), MAX_POSTS (default 8), PROBE (default true)
"""
import os, sys, time
from datetime import datetime, timezone
import requests
from supabase import create_client

SB = os.environ.get("SUPABASE_URL", "").strip(); KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
HOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
HOOK_RETAIL = (os.environ.get("DISCORD_WEBHOOK_RETAIL") or "").strip() or HOOK
HOOK_MARKET = (os.environ.get("DISCORD_WEBHOOK_MARKET") or "").strip() or HOOK

# #best-deals — the same rule as the fast lane, so the two never disagree:
# 30%+ off with $60+ saved, or 20%+ off with $150+ saved. ~9 a day.
HOOK_BEST = (os.environ.get("DISCORD_WEBHOOK_BEST") or "").strip()
BEST_MIN_PCT = float(os.environ.get("BEST_MIN_PCT") or 30)
BEST_MIN_SAVING = float(os.environ.get("BEST_MIN_SAVING") or 60)
BEST_BIG_SAVING = float(os.environ.get("BEST_BIG_SAVING") or 150)
BEST_BIG_MIN_PCT = float(os.environ.get("BEST_BIG_MIN_PCT") or 20)
BEST_DAILY_CAP = int(os.environ.get("BEST_DAILY_CAP") or 15)
BEST_ROLE_ID = (os.environ.get("DISCORD_BEST_ROLE_ID") or "").strip()


def saving_of(d):
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

MIN_SCORE = float(os.environ.get("MIN_SCORE") or 5.0)
MAX_POSTS = int(os.environ.get("MAX_POSTS") or 8)
PROBE = os.environ.get("PROBE", "true").strip().lower() != "false"
SITE = "https://www.brickmargin.com"
if not (SB and KEY): sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY required")
if not (HOOK_RETAIL or HOOK_MARKET) and not PROBE:
    sys.exit("ERROR: set DISCORD_WEBHOOK_RETAIL and DISCORD_WEBHOOK_MARKET (or DISCORD_WEBHOOK)")
CAMPAIGN = os.environ.get("EBAY_CAMPAIGN_ID", "5339178457").strip()

def affiliate(url: str) -> str:
    """
    Every link out earns, wherever it is posted.

    The Discord feed was sending raw eBay URLs — the same listings that earn
    a commission on the site were earning nothing in the channel. These are
    the exact parameters the site uses, mkevt=1 included: without it eBay
    records no click however correct the campaign looks.
    """
    if not url or not CAMPAIGN:
        return url
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        u = urlparse(url)
        q = dict(parse_qsl(u.query))
        q.update({"mkevt": "1", "mkcid": "1", "mkrid": "711-53200-19255-0",
                  "siteid": "0", "campid": CAMPAIGN, "toolid": "10001",
                  "customid": "discord"})
        return urlunparse(u._replace(query=urlencode(q)))
    except Exception:
        return url


def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)
usd = lambda n: f"${round(float(n)):,}"

client = create_client(SB, KEY)
posted = {r["item_id"] for r in (client.table("discord_posts").select("item_id").execute()).data or []}
deals = (client.table("live_deals")
         .select("item_id,set_num,set_name,theme,image_url,total_price,market_value,msrp,pct_off_msrp,discount_pct,margin_pct,score,condition,seller,item_url,deal_type")
         .gte("score", MIN_SCORE).order("score", desc=True).limit(60).execute()).data or []
fresh = [d for d in deals if d["item_id"] not in posted][:MAX_POSTS]
log(f"{len(deals)} deals at or above {MIN_SCORE}; {len(fresh)} not yet posted")

sent = 0
for d in fresh:
    off = (f"{round(d['pct_off_msrp'])}% under retail" if d.get("pct_off_msrp") and d.get("deal_type") != "market"
           else f"{round(d['discount_pct'])}% under market")
    # Colour carries the message before the words do: green for a good
    # discount, yellow for a fair one, red for the rare ones worth dropping
    # everything for. A row of these reads at a glance.
    pct = float(d.get("pct_off_msrp") or d.get("discount_pct") or 0)
    if pct >= 40:   colour, band = 0xE3000B, "🔥 HOT"
    elif pct >= 25: colour, band = 0xFF8A00, "⭐ Strong"
    elif pct >= 15: colour, band = 0xFFD500, "👍 Good"
    else:           colour, band = 0x4CBB17, "Worth a look"

    stars = "█" * max(1, min(10, round(float(d.get("score") or 0)))) + "░" * (10 - max(1, min(10, round(float(d.get("score") or 0)))))
    saving = ""
    if d.get("msrp"):
        saving = f"You save **{usd(float(d['msrp']) - float(d['total_price']))}** off retail"
    elif d.get("market_value"):
        saving = f"About **{usd(float(d['market_value']) - float(d['total_price']))}** under what it resells for"

    embed = {
        # EPN: every eBay link says it goes to eBay — author line, title, labelled link
        "author": {"name": f"eBay listing · {band} · " + ("under retail" if d.get("deal_type") == "retail" else "under resale"),
                   "icon_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
        "title": f"{d['set_name']} — View on eBay"[:256],
        "url": affiliate(d["item_url"]),
        "description": f"**[Buy on eBay →]({affiliate(d['item_url'])})**\n## {usd(d['total_price'])}  ·  {off}\n{saving}",
        "color": colour,
        "image": {"url": re.sub(r"/s-l\d+\.", "/s-l1600.", d["image_url"])} if d.get("image_url") else None,
        "fields": [
            {"name": "Retail", "value": usd(d["msrp"]) if d.get("msrp") else "—", "inline": True},
            {"name": "Resells for", "value": usd(d["market_value"]) if d.get("market_value") else "—", "inline": True},
            {"name": "Condition", "value": (d.get("condition") or "new").title(), "inline": True},
            {"name": "Deal score", "value": f"`{stars}` {float(d.get('score') or 0):.1f}", "inline": False},
            {"name": "Set page", "value": f"[{d['set_num'].split('-')[0]} on BrickMargin]({SITE}/set/{d['set_num']})", "inline": True},
            {"name": "Seller", "value": f"{d.get('seller') or '—'}", "inline": True},
        ],
        "footer": {"text": "Affiliate link to eBay: BrickMargin may earn a commission if you buy · price includes shipping",
                   "icon_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    embed = {k: v for k, v in embed.items() if v is not None}
    if PROBE:
        log(f"  would post: {d['set_name']} {usd(d['total_price'])} ({off})"); sent += 1; continue
    target = HOOK_RETAIL if d.get("deal_type") == "retail" else HOOK_MARKET
    if not target:
        continue
    main = {"embeds": [embed], "username": "BrickMargin",
            "avatar_url": "https://www.brickmargin.com/brickmargin-round-512.png"}
    role = theme_ping(d)
    if role:
        main["content"] = f"<@&{role}>"
        main["allowed_mentions"] = {"parse": [], "roles": [role]}
    r = requests.post(target, json=main, timeout=20)
    if r.status_code < 300:
        client.table("discord_posts").insert({"item_id": d["item_id"], "set_num": d["set_num"]}).execute()
        sent += 1; time.sleep(1.2)          # Discord rate limits webhooks

        if HOOK_BEST and is_best(d):
            today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
            used = (client.table("discord_posts").select("item_id", count="exact")
                    .gte("best_at", today).execute()).count or 0
            if used < BEST_DAILY_CAP:
                best = {**embed, "author": {**embed.get("author", {}),
                        "name": "🏆 Best deal · eBay listing · " + ("under retail" if d.get("deal_type") == "retail" else "under resale")}}
                payload = {"embeds": [best], "username": "BrickMargin",
                           "avatar_url": "https://www.brickmargin.com/brickmargin-round-512.png"}
                if BEST_ROLE_ID:
                    payload["content"] = f"<@&{BEST_ROLE_ID}>"
                    payload["allowed_mentions"] = {"parse": [], "roles": [BEST_ROLE_ID]}
                if requests.post(HOOK_BEST, json=payload, timeout=20).status_code < 300:
                    client.table("discord_posts").update({"best_at": datetime.now(timezone.utc).isoformat()}) \
                        .eq("item_id", d["item_id"]).execute()
                    log(f"  🏆 best deal: {d.get('set_name') or d['set_num']} saves ${saving_of(d):,.0f}")
                    time.sleep(1.2)
    else:
        log(f"  HTTP {r.status_code} {r.text[:120]}")
log(f"{'would post' if PROBE else 'posted'} {sent}")
