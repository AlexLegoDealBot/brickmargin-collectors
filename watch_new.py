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
import base64, os, sys, time
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client

VERSION = "1.3-channels"

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
                "sort": "newlyListed", "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:US"},
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


def announce(d, client):
    """One deal, straight to Discord, remembered so it is never posted twice."""
    if not HOOK or float(d.get("score") or 0) < MIN_POST_SCORE:
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
        "author": {"name": f"{band} · " + ("under retail — buy it" if d.get("deal_type") == "retail" else "under resale — flip it"),
                   "icon_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
        "title": d["title"][:240],
        "url": affiliate(d["item_url"]),
        "description": f"## {usd(d['total_price'])}  ·  {off}\n{saving}",
        "color": colour,
        "thumbnail": {"url": d["image_url"]} if d.get("image_url") else None,
        "fields": [
            {"name": "Retail", "value": usd(d["msrp"]) if d.get("msrp") else "—", "inline": True},
            {"name": "Resells for", "value": usd(d["market_value"]), "inline": True},
            {"name": "Condition", "value": (d.get("condition") or "new").title(), "inline": True},
            {"name": "Deal score", "value": f"`{bar}` {float(d.get('score') or 0):.1f}", "inline": False},
            {"name": "Set page", "value": f"[{d['set_num'].split('-')[0]} on BrickMargin](https://www.brickmargin.com/set/{d['set_num']})", "inline": True},
            {"name": "Seller", "value": d.get("seller") or "—", "inline": True},
        ],
        "footer": {"text": "BrickMargin · price includes shipping · tap the title to buy",
                   "icon_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Still in production and under shop price → the "buy it" room.
    # Retired and under what it resells for → the "flip it" room.
    target = HOOK_RETAIL if d.get("deal_type") == "retail" else HOOK_MARKET
    if not target:
        return
    try:
        r = HTTP.post(target, json={"embeds": [embed], "username": "BrickMargin",
                                  "avatar_url": "https://www.brickmargin.com/brickmargin-round-512.png"},
                      timeout=20)
        if r.status_code < 300:
            client.table("discord_posts").upsert(
                {"item_id": d["item_id"], "set_num": d["set_num"]}, on_conflict="item_id").execute()
            time.sleep(1.2)          # Discord rate-limits webhooks
        else:
            log(f"    discord HTTP {r.status_code}")
    except Exception as exc:
        log(f"    discord failed: {str(exc)[:80]}")


def main():
    log(f"BrickMargin fast lane — version {VERSION}  probe={PROBE}  {MINUTES}m at {EVERY}s")
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
            import re
            nums = re.findall(r"(?<!\d)(\d{4,7})(?!\d)", title)
            row = next((by_number[n] for n in nums if n in by_number), None)
            if not row: continue

            keep, _ = is_relevant(title, row["set_num"].split("-")[0], row.get("name"), row.get("theme"))
            if not keep: continue

            try: price = float(item["price"]["value"])
            except Exception: continue
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
                    for f in sorted(found, key=lambda x: -float(x.get("score") or 0)):
                        announce(f, client)
                except Exception as exc:
                    log(f"    write failed: {str(exc)[:100]}")
        log(f"  sweep {sweeps}: {len(items)} listings, {len(fresh)} new, {len(found)} deals")
        time.sleep(EVERY)

    log(f"Done. {sweeps} sweeps, {posted} deals written.")

if __name__ == "__main__":
    main()
