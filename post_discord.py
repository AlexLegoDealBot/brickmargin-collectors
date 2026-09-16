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
        "author": {"name": f"{band} · " + ("under retail" if d.get("deal_type") == "retail" else "under resale"),
                   "icon_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
        "title": f"{d['set_name']}",
        "url": affiliate(d["item_url"]),
        "description": f"## {usd(d['total_price'])}  ·  {off}\n{saving}",
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
        "footer": {"text": "BrickMargin · every price includes shipping · tap the title to buy",
                   "icon_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    embed = {k: v for k, v in embed.items() if v is not None}
    if PROBE:
        log(f"  would post: {d['set_name']} {usd(d['total_price'])} ({off})"); sent += 1; continue
    target = HOOK_RETAIL if d.get("deal_type") == "retail" else HOOK_MARKET
    if not target:
        continue
    r = requests.post(target, json={"embeds": [embed], "username": "BrickMargin",
                                  "avatar_url": "https://www.brickmargin.com/brickmargin-round-512.png"}, timeout=20)
    if r.status_code < 300:
        client.table("discord_posts").insert({"item_id": d["item_id"], "set_num": d["set_num"]}).execute()
        sent += 1; time.sleep(1.2)          # Discord rate limits webhooks
    else:
        log(f"  HTTP {r.status_code} {r.text[:120]}")
log(f"{'would post' if PROBE else 'posted'} {sent}")
