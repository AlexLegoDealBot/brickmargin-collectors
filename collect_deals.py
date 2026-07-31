#!/usr/bin/env python3
"""
BrickMargin · deals collector
=============================

Finds live eBay listings priced below what the set is actually worth.

We already know what 4,000+ sets are worth, because we measure it eight times
a day. eBay tells us what people are asking right now. The gap between those
two numbers is the whole product: not "this set is worth $900", but "this
one is listed at $540 and it's worth $900".

What this does NOT do, deliberately:

  * It never ranks by percentage off an asking price. A seller can invent any
    "was" price. Every figure here is measured against our own comp.
  * It never compares item prices. Shipping is part of what you pay, so
    total = item + postage, always, and the gap is computed from the total.
    The $1 item with $200 postage sorts exactly where it belongs.
  * It never leaves a sold listing on the site. Anything the feed stops
    returning is marked gone after 48 hours and disappears from the view.

Environment
-----------
  SUPABASE_URL, SUPABASE_SERVICE_KEY   required
  EBAY_APP_ID, EBAY_CERT_ID            required
  MIN_DISCOUNT      default 15   — percent below market before it's a deal
  MIN_MARGIN        default 20   — percent profit after fees before it's shown
  MAX_SETS          default 300  — sets to scan per run
  MIN_VALUE         default 60   — ignore cheap sets; postage eats the margin
  PROBE             default true — print, write nothing
"""

import base64
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from supabase import create_client

VERSION = "1.3-confident"

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE = "EBAY_US"

# The site's default cost model. Kept in step with lib/settings.ts.
MARKETPLACE_PCT = 13.25
PROMOTED_PCT = 2.0
SHIP_TO_BUYER = 12.0

LISTINGS_PER_SET = 30
PAUSE = 0.25


def require(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: {name} is required")
    return v


def num_env(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


SUPABASE_URL = require("SUPABASE_URL")
SUPABASE_KEY = require("SUPABASE_SERVICE_KEY")
EBAY_APP_ID = require("EBAY_APP_ID")
EBAY_CERT_ID = require("EBAY_CERT_ID")

MIN_DISCOUNT = num_env("MIN_DISCOUNT", 15)
# The ceiling matters more than the floor. Nothing sells a $1,600 set for
# $6 — a listing that cheap is a minifigure, a manual or an empty box that
# happens to mention the number. Anything below (100 - MAX_DISCOUNT)% of
# value is rejected as not-the-set rather than celebrated as a bargain.
# 70% was still generous enough to admit scam bait. A genuine underpriced
# set is usually 15-55% under market; below that, the listing is lying.
MAX_DISCOUNT = num_env("MAX_DISCOUNT", 55)
MAX_PER_SET = int(num_env("MAX_PER_SET", 3))
# Feedback percentage alone means nothing — 100% from two sales is riskier
# than 98% from five thousand. Both gates must pass, and a seller with no
# feedback history at all is rejected rather than given the benefit of the
# doubt. We are sending real people to spend real money.
MIN_FEEDBACK = num_env("MIN_FEEDBACK", 97)
MIN_FEEDBACK_COUNT = int(num_env("MIN_FEEDBACK_COUNT", 50))
# A deal is a gap between a listing and OUR value — so a wrong value invents
# a deal that never existed. Only sets we're confident about qualify.
MIN_COMPS = int(num_env("MIN_COMPS", 5))
US_ONLY = os.environ.get("US_ONLY", "true").strip().lower() != "false"
MIN_MARGIN = num_env("MIN_MARGIN", 20)
MAX_SETS = int(num_env("MAX_SETS", 300))
MIN_VALUE = num_env("MIN_VALUE", 60)
PROBE = os.environ.get("PROBE", "true").strip().lower() != "false"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_token():
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    resp = requests.post(
        EBAY_OAUTH_URL,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"ERROR: eBay auth failed ({resp.status_code}). "
                 f"Check EBAY_APP_ID and EBAY_CERT_ID.")
    token = resp.json().get("access_token")
    if not token:
        sys.exit("ERROR: no access_token from eBay")
    log("  eBay token acquired")
    return token


def base_number(set_num):
    return set_num.split("-")[0]


def money(node):
    """eBay returns money as {'value': '12.34', 'currency': 'USD'}."""
    if not node:
        return None
    try:
        return float(node.get("value"))
    except (TypeError, ValueError):
        return None


def shipping_cost(item):
    """
    Postage, in dollars. Free shipping is 0. A listing whose postage we
    cannot determine is skipped entirely rather than guessed at — an unknown
    shipping cost is exactly how the $1-item trick gets through.
    """
    options = item.get("shippingOptions") or []
    if not options:
        return None
    first = options[0]
    if (first.get("shippingCostType") or "").upper() == "CALCULATED":
        return None                      # varies by buyer address; not comparable
    cost = money(first.get("shippingCost"))
    return 0.0 if cost is None and first.get("shippingCost") else cost


# Words that mean "this is not the complete set". The first probe run
# returned 4,872 "deals" because this list was a tenth of this length and
# the set number was matched as a substring.
JUNK = (
    "instruction", "manual", "booklet", "sticker", "decal", "poster",
    "catalog", "catalogue", "magazine", "card", "sealed bag", "polybag",
    "bag only", "box only", "empty box", "empty", "no box", "box no",
    "minifig", "mini fig", "minifigure", "figure only", "fig only",
    "part", "parts", "piece only", "pieces only", "brick only", "bricks only",
    "replacement", "spare", "missing", "incomplete", "damaged", "broken",
    "read description", "for parts", "not complete", "keychain", "key chain",
    "magnet", "pen", "shirt", "mug", "custom", "compatible", "knock",
    "knockoff", "moc", "lot of", "bundle", "job lot", "choose", "pick your",
    "you pick", "select", "assorted", "random",
    # Knock-off sellers rarely say "fake" — they say these instead.
    "generic", "unbranded", "aftermarket", "replica", "clone", "third party",
    "compatible with", "fits lego", "lego style", "building blocks",
    "block set", "not lego", "no logo", "brick set compatible", "oem",
)


def is_relevant(title, num):
    """
    The listing must plausibly BE the set.

    The number has to appear as its own token — plain substring matching
    let 70922 match inside longer part numbers and turned accessories into
    thousand-percent bargains.
    """
    t = (title or "").lower()
    if not re.search(rf"(?<!\d){re.escape(num)}(?!\d)", t):
        return False, "number missing"
    for bad in JUNK:
        if bad in t:
            return False, bad
    return True, ""


def condition_of(item):
    """
    Two buckets only, and 'used' has to genuinely mean used.

    eBay's condition ids: 1000 new, 1500 open box, 2000-2500 refurbished,
    3000 used, 4000-6000 damaged/parts. Anything ambiguous is dropped rather
    than filed under a label that would mislead.
    """
    cid = str(item.get("conditionId") or "").strip()
    if cid == "1000":
        return "new"
    if cid in ("3000", "2750"):
        return "used"
    return None


def search(token, set_num, condition_filter):
    resp = requests.get(
        EBAY_SEARCH_URL,
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
                 "Content-Type": "application/json"},
        params={
            "q": f"LEGO {base_number(set_num)}",
            "filter": f"conditions:{{{condition_filter}}},buyingOptions:{{FIXED_PRICE}}",
            # eBay's own "LEGO Complete Sets & Packs" category. The single
            # most effective filter available — it excludes the parts,
            # minifigure and instruction categories at source.
            "category_ids": "19006",
            "sort": "price",
            "limit": LISTINGS_PER_SET,
        },
        timeout=45,
    )
    if resp.status_code == 429:
        return "RATE_LIMIT"
    if resp.status_code != 200:
        log(f"  {set_num}: HTTP {resp.status_code}")
        return None
    return resp.json().get("itemSummaries", []) or []


def evaluate(item, set_row):
    """Turn one listing into a deal row, or None."""
    num = base_number(set_row["set_num"])
    title = item.get("title") or ""

    keep, _why = is_relevant(title, num)
    if not keep:
        return None

    cond = condition_of(item)
    if cond is None:
        return None

    price = money(item.get("price"))
    if price is None or price <= 0:
        return None

    ship = shipping_cost(item)
    if ship is None:
        return None                       # unknown postage → not comparable

    total = round(price + ship, 2)
    value = float(set_row["market_value"])

    # A used set is not worth what a sealed one is. Discounting the
    # benchmark rather than the listing keeps the comparison honest.
    benchmark = value if cond == "new" else value * 0.72

    discount = (benchmark - total) / benchmark * 100
    if discount < MIN_DISCOUNT:
        return None
    if discount > MAX_DISCOUNT:
        return None          # too good to be the set — it isn't the set

    # --- who is selling it -------------------------------------------------
    seller_node = item.get("seller") or {}
    try:
        seller_pct = float(seller_node.get("feedbackPercentage"))
    except (TypeError, ValueError):
        seller_pct = None
    try:
        seller_count = int(seller_node.get("feedbackScore"))
    except (TypeError, ValueError):
        seller_count = None

    if seller_pct is None or seller_count is None:
        return None                       # no history published — no thanks
    if seller_pct < MIN_FEEDBACK:
        return None
    if seller_count < MIN_FEEDBACK_COUNT:
        return None

    if US_ONLY:
        country = ((item.get("itemLocation") or {}).get("country") or "").upper()
        if country and country != "US":
            return None                   # long-haul knock-off shipping

    fees = benchmark * (MARKETPLACE_PCT + PROMOTED_PCT) / 100
    net = max(0.0, benchmark - fees - SHIP_TO_BUYER)
    margin = (net - total) / total * 100 if total else 0
    if margin < MIN_MARGIN:
        return None

    return {
        "item_id": str(item.get("itemId")),
        "set_num": set_row["set_num"],
        "title": title[:300],
        "condition": cond,
        "item_price": round(price, 2),
        "shipping": round(ship, 2),
        "total_price": total,
        "market_value": round(value, 2),
        "discount_pct": round(discount, 2),
        "net_if_flipped": round(net, 2),
        "margin_pct": round(margin, 2),
        "seller": seller_node.get("username"),
        "feedback_pct": seller_pct,
        "image_url": (item.get("image") or {}).get("imageUrl"),
        "item_url": item.get("itemWebUrl") or "",
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }


def main():
    log(f"BrickMargin deals collector — version {VERSION}")
    log(f"  discount {MIN_DISCOUNT}-{MAX_DISCOUNT}% · min margin {MIN_MARGIN}% · "
        f"max {MAX_PER_SET}/set · seller >={MIN_FEEDBACK}% "
        f"with >={MIN_FEEDBACK_COUNT} sales · US_ONLY={US_ONLY} · "
        f"{MAX_SETS} sets · probe={PROBE}")

    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Scan the sets worth scanning: priced, plausible, and expensive enough
    # that postage doesn't eat the whole margin.
    resp = (client.table("set_values")
            .select("set_num,name,market_value,comp_count,confidence")
            .eq("plausible", True)
            .in_("confidence", ["high", "medium"])
            .gte("comp_count", MIN_COMPS)
            .gte("market_value", MIN_VALUE)
            .order("market_value", desc=True)
            .limit(MAX_SETS)
            .execute())
    sets = resp.data or []
    log(f"  scanning {len(sets)} sets (confidence high/medium, "
        f">={MIN_COMPS} listings behind each value)")

    token = get_token()
    found, scanned = [], 0

    for set_row in sets:
        scanned += 1
        for cond_filter in ("NEW", "USED"):
            items = search(token, set_row["set_num"], cond_filter)
            if items == "RATE_LIMIT":
                log("  RATE LIMITED — stopping cleanly, keeping what we have")
                items = None
                sets = []
                break
            for item in (items or []):
                deal = evaluate(item, set_row)
                if deal:
                    found.append(deal)
            time.sleep(PAUSE)
        if scanned % 25 == 0:
            log(f"  {scanned} scanned · {len(found)} deals so far")

    # One listing can arrive twice (both condition passes, multi-quantity),
    # and one set with many cheap listings must not flood the board.
    unique = {}
    for d in found:
        prior = unique.get(d["item_id"])
        if not prior or d["margin_pct"] > prior["margin_pct"]:
            unique[d["item_id"]] = d

    per_set = {}
    for d in sorted(unique.values(), key=lambda x: -x["margin_pct"]):
        bucket = per_set.setdefault(d["set_num"], [])
        if len(bucket) < MAX_PER_SET:
            bucket.append(d)
    found = [d for bucket in per_set.values() for d in bucket]

    log(f"  found {len(found)} deals across {len(per_set)} sets")

    if PROBE:
        for d in sorted(found, key=lambda x: -x["margin_pct"])[:12]:
            log(f"    {d['condition']:4} {d['set_num']:>10} "
                f"${d['total_price']:>8.2f} (item ${d['item_price']:.2f} "
                f"+ ship ${d['shipping']:.2f}) vs ${d['market_value']:.2f} "
                f"→ {d['margin_pct']:.0f}% · seller {d['seller']} "
                f"{d['feedback_pct']}%")
        log("  PROBE — nothing written")
        return

    written = 0
    for i in range(0, len(found), 200):
        batch = found[i:i + 200]
        client.table("deals").upsert(batch, on_conflict="item_id").execute()
        written += len(batch)
    log(f"  wrote {written}")

    # Anything that stopped appearing is sold or ended — take it off the site.
    try:
        expired = client.rpc("expire_stale_deals").execute()
        log(f"  expired {expired.data} listings no longer live")
    except Exception as exc:
        log(f"  expire step skipped: {exc}")

    log("Done.")


if __name__ == "__main__":
    main()
