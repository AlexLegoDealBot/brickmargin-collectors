#!/usr/bin/env python3
"""
BrickMargin · the weekly Top 10
===============================

Every Sunday morning, one post: the ten biggest savings posted to the deal
rooms in the last seven days, one per set. A deal that is still live links
straight to eBay; one that has sold links to the set's page, which always
shows today's price.

Every listing is re-checked against TODAY's rules before it can appear. Last
week's raw top ten would have led with a $28 bag of cloth sails "92% off"
Pirates of Barracuda Bay and two probable knock-offs, all posted before the
stricter filters and #too-good-to-be-true existed. A recap is the post people
screenshot, so it only shows what we would still stand behind:
  - the title must still pass the same set checks the deal rooms use
  - nothing the current price floors would send to #too-good-to-be-true
  - nothing from a seller who, that same week, listed something too good to
    be true (a seller with a $47 "Millennium Falcon" doesn't get a pass on
    their other listings)

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY
             DISCORD_WEBHOOK_RECAP      where to post (falls back to DISCORD_WEBHOOK_BEST)
             DISCORD_ALERTS_CHANNEL_ID  optional: makes "#set-alerts" a clickable link
             PROBE                      default true: print the post, send nothing
"""
import os, sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
from supabase import create_client

# One definition of "is this listing really the set": the collector's own.
# The collector insists on eBay keys when it loads; this script never calls
# eBay, so it doesn't get them — placeholders satisfy the check.
os.environ.setdefault("EBAY_APP_ID", "not-used-by-the-recap")
os.environ.setdefault("EBAY_CERT_ID", "not-used-by-the-recap")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_deals import is_relevant, looks_partial, MAX_DISCOUNT, MSRP_FLOOR_NEW   # noqa: E402

VERSION = "1.0"

SB = (os.environ.get("SUPABASE_URL") or "").strip()
KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
HOOK = (os.environ.get("DISCORD_WEBHOOK_RECAP") or os.environ.get("DISCORD_WEBHOOK_BEST") or "").strip()
ALERTS_CH = (os.environ.get("DISCORD_ALERTS_CHANNEL_ID") or "").strip()
PROBE = (os.environ.get("PROBE") or "true").strip().lower() != "false"
CAMPAIGN = (os.environ.get("EBAY_CAMPAIGN_ID") or "5339178457").strip()
SITE = "https://www.brickmargin.com"
LOGO = "https://www.brickmargin.com/brickmargin-round-512.png"
MIN_PCT, MIN_SAVING, TOP = 20, 20, 10

usd = lambda n: f"${float(n):,.2f}".replace(".00", "")


def affiliate(url):
    if not url or not CAMPAIGN:
        return url
    try:
        u = urlparse(url); q = dict(parse_qsl(u.query))
        q.update({"mkevt": "1", "mkcid": "1", "mkrid": "711-53200-19255-0", "siteid": "0",
                  "campid": CAMPAIGN, "toolid": "10001", "customid": "discord-recap"})
        return urlunparse(u._replace(query=urlencode(q)))
    except Exception:
        return url


def chunks(xs, n=100):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def week_of_deals(client, since):
    """Deals that reached the deal rooms since `since` (never the too-good room)."""
    ids, start = [], 0
    while True:
        rows = (client.table("discord_posts").select("item_id,check_at")
                .gte("posted_at", since.isoformat()).order("posted_at").range(start, start + 999).execute()).data or []
        ids += [r["item_id"] for r in rows if not r.get("check_at")]
        if len(rows) < 1000:
            break
        start += 1000
    deals = []
    for part in chunks(ids):
        deals += (client.table("deals")
                  .select("item_id,set_num,title,seller,total_price,msrp,market_value,deal_type,"
                          "pct_off_msrp,discount_pct,item_url,gone_at,condition")
                  .in_("item_id", part).execute()).data or []
    return deals


def load_sets(client, set_nums):
    out = {}
    for part in chunks(sorted(set_nums)):
        for r in (client.table("set_values").select("set_num,name,theme").in_("set_num", part).execute()).data or []:
            out[r["set_num"]] = r
    return out


def problem(d, s):
    """Why today's rules would keep this listing out of the deal rooms, or None."""
    title = d.get("title") or ""
    if title:                                   # no title, nothing to judge it by: price rules only
        ok, why = is_relevant(title, d["set_num"].split("-")[0], s.get("name"), s.get("theme"))
        if not ok:
            return why
        if looks_partial(title, s.get("name")):
            return "part of a set"
    total = float(d["total_price"])
    value = float(d.get("market_value") or 0)
    msrp = float(d.get("msrp") or 0)
    if value and (1 - total / value) * 100 > MAX_DISCOUNT:
        return f"{(1 - total / value) * 100:.0f}% under value"
    if msrp and total < msrp * MSRP_FLOOR_NEW:
        return f"{(1 - total / msrp) * 100:.0f}% under retail"
    if not msrp and value and total < value * 0.48:
        return f"{(1 - total / value) * 100:.0f}% under value"
    return None


def pick(deals, sets):
    """Re-check every listing, drop sellers with a too-good listing, then the
    biggest saving per set, then the ten biggest of those."""
    flagged, bad_sellers = {}, set()
    for d in deals:
        why = problem(d, sets.get(d["set_num"]) or {})
        if why:
            flagged[d["item_id"]] = why
            if d.get("seller"):
                bad_sellers.add(d["seller"])
    best = {}
    for d in deals:
        if (d.get("condition") or "new") != "new" or d["item_id"] in flagged or d.get("seller") in bad_sellers:
            continue
        retail = d.get("deal_type") == "retail" and d.get("msrp")
        ref = float(d["msrp"]) if retail else float(d.get("market_value") or 0)
        pct = float((d.get("pct_off_msrp") if retail else d.get("discount_pct")) or 0)
        saving = ref - float(d["total_price"])
        if pct < MIN_PCT or saving < MIN_SAVING:
            continue
        d = {**d, "_saving": saving, "_pct": pct, "_retail": bool(retail)}
        if d["set_num"] not in best or saving > best[d["set_num"]]["_saving"]:
            best[d["set_num"]] = d
    return sorted(best.values(), key=lambda d: -d["_saving"])[:TOP], flagged, bad_sellers


def main():
    print(f"BrickMargin weekly Top 10 — version {VERSION}  probe={PROBE}")
    if not (SB and KEY):
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
    if not HOOK and not PROBE:
        sys.exit("ERROR: no webhook — set DISCORD_WEBHOOK_RECAP (or DISCORD_WEBHOOK_BEST)")
    client = create_client(SB, KEY)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    deals = week_of_deals(client, since)
    sets = load_sets(client, {d["set_num"] for d in deals})
    top, flagged, bad_sellers = pick(deals, sets)
    print(f"{len(deals)} deals posted this week · {len(flagged)} fail today's checks · "
          f"{len(bad_sellers)} sellers set aside · {len(top)} make the list")
    if len(top) < 3:
        print(f"Fewer than 3 qualifying deals this week ({len(top)}) — not posting a thin list.")
        return

    fields = []
    for n, d in enumerate(top, 1):
        name = (sets.get(d["set_num"]) or {}).get("name") or d["set_num"]
        num = d["set_num"].split("-")[0]
        link = (f"[Still live on eBay →]({affiliate(d['item_url'])})" if not d.get("gone_at")
                else f"[Sold · today's price →]({SITE}/set/{d['set_num']})")
        fields.append({"name": f"{n}. {name} ({num})"[:256],
                       "value": f"**{usd(d['total_price'])}** · {d['_pct']:.0f}% under {'retail' if d['_retail'] else 'resale'} "
                                f"· saved {usd(d['_saving'])}\n{link}"[:1024]})
    alerts = f"<#{ALERTS_CH}>" if ALERTS_CH.isdigit() else "#set-alerts"
    embed = {
        "author": {"name": "BrickMargin · weekly Top 10", "icon_url": LOGO},
        "title": "🏆 This week's 10 best LEGO deals",
        "url": f"{SITE}/week",
        "description": (f"The biggest savings posted here {since:%b %-d} – {now:%b %-d}, one per set. "
                        f"Missed one? Use `/track` in {alerts} and the bot pings you the moment your set hits your price."),
        "color": 0xFFD500,
        "fields": fields,
        "footer": {"text": "Affiliate links to eBay: BrickMargin may earn a commission if you buy", "icon_url": LOGO},
        "timestamp": now.isoformat(),
    }
    if PROBE:
        print(f"PROBE — would post {len(fields)} deals:")
        for f in fields:
            print(f"  {f['name']}  |  {f['value'].splitlines()[0]}")
        for iid, why in list(flagged.items())[:15]:
            print(f"  left out {iid}: {why}")
        return
    r = requests.post(HOOK, json={"embeds": [embed], "username": "BrickMargin", "avatar_url": LOGO,
                                  "allowed_mentions": {"parse": []}}, timeout=20)
    if r.status_code >= 300:
        sys.exit(f"ERROR: Discord answered {r.status_code}: {r.text[:200]}")
    print(f"Posted the Top {len(fields)}.")


if __name__ == "__main__":
    main()
