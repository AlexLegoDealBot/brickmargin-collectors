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
MIN_SCORE = float(os.environ.get("MIN_SCORE") or 5.0)
MAX_POSTS = int(os.environ.get("MAX_POSTS") or 8)
PROBE = os.environ.get("PROBE", "true").strip().lower() != "false"
SITE = "https://www.brickmargin.com"
if not (SB and KEY): sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY required")
if not HOOK and not PROBE: sys.exit("ERROR: DISCORD_WEBHOOK required unless probing")
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
    embed = {
        "title": f"{d['set_name']} — {usd(d['total_price'])}",
        "url": d["item_url"],
        "description": f"**{off}**" + (f" · retail {usd(d['msrp'])}" if d.get("msrp") else "")
                       + (f" · resells {usd(d['market_value'])}" if d.get("market_value") else ""),
        "color": 0xE3000B,
        "thumbnail": {"url": d["image_url"]} if d.get("image_url") else None,
        "fields": [
            {"name": "Set", "value": f"[{d['set_num'].split('-')[0]}]({SITE}/set/{d['set_num']})", "inline": True},
            {"name": "Theme", "value": d.get("theme") or "—", "inline": True},
            {"name": "Score", "value": f"{d.get('score', 0):.1f}/10", "inline": True},
            {"name": "Condition", "value": (d.get("condition") or "new").title(), "inline": True},
            {"name": "Seller", "value": d.get("seller") or "—", "inline": True},
        ],
        "footer": {"text": "BrickMargin · prices include shipping"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    embed = {k: v for k, v in embed.items() if v is not None}
    if PROBE:
        log(f"  would post: {d['set_name']} {usd(d['total_price'])} ({off})"); sent += 1; continue
    r = requests.post(HOOK, json={"embeds": [embed]}, timeout=20)
    if r.status_code < 300:
        client.table("discord_posts").insert({"item_id": d["item_id"], "set_num": d["set_num"]}).execute()
        sent += 1; time.sleep(1.2)          # Discord rate limits webhooks
    else:
        log(f"  HTTP {r.status_code} {r.text[:120]}")
log(f"{'would post' if PROBE else 'posted'} {sent}")
