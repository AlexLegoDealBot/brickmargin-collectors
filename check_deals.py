#!/usr/bin/env python3
"""
BrickMargin · keep the deals board honest
=========================================

Sold listings were staying on the website for days, for two reasons:
  - the only thing that retired them was the full scan. That now runs once
    a day (eBay's 5,000-call limit) and covers only the 800 most valuable sets;
  - its backstop hides a deal 48 hours after it was last seen, and only
    runs when that scan finishes.

This runs every hour and does two jobs.

  1. Re-checks, straight from eBay, the deals people are most likely to look
     at: highest score first, anything not confirmed in CHECK_AFTER_HOURS.
     - sold, ended or out of stock: retired
     - price went up past the deal line: retired
     - price dropped (or rose but is still a deal): updated
     - still there at the same price: confirmed, so it stays up
  2. Retires instant-lane deals nobody has confirmed for FAST_EXPIRE_HOURS
     (default 24) — those listings are usually gone within hours — and
     anything at all unconfirmed for 48 hours.

It only spends eBay calls the rest of the system won't need. Before checking
anything it asks eBay how many calls are left today, and keeps a reserve for
the fast lane (which must never run dry) and the daily full scan.

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY, EBAY_APP_ID, EBAY_CERT_ID
             MAX_CHECKS (default 25 per run), CHECK_AFTER_HOURS (6),
             FAST_EXPIRE_HOURS (24), PROBE (default true: report, change nothing)
"""
import os, sys
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supabase import create_client                                                     # noqa: E402
from collect_deals import get_token, shipping_cost, money, log, num_env                 # noqa: E402

VERSION = "1.0"

SB = (os.environ.get("SUPABASE_URL") or "").strip()
KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
PROBE = (os.environ.get("PROBE") or "true").strip().lower() != "false"
MAX_CHECKS = int(num_env("MAX_CHECKS", 25))
CHECK_AFTER_HOURS = num_env("CHECK_AFTER_HOURS", 6)
FAST_EXPIRE_HOURS = num_env("FAST_EXPIRE_HOURS", 24)
# What the rest of the system needs until eBay's daily reset: the fast lane
# sweeps every 35 seconds (~103 searches an hour) plus a description check per
# candidate; the full scan needs ~900 once a day. Kept generous on purpose.
RESERVE_PER_HOUR = num_env("RESERVE_PER_HOUR", 130)
RESERVE_BASE = num_env("RESERVE_BASE", 1200)
FALLBACK_CHECKS = 5          # when eBay won't say how many calls are left

EBAY = "https://api.ebay.com"
# Retries for dropped connections and eBay hiccups, but NOT for 429: if eBay
# says slow down, this job stops instead of eating the fast lane's calls.
S = requests.Session()
S.mount("https://", HTTPAdapter(max_retries=Retry(total=3, connect=3, read=3, backoff_factor=1.5,
                                                  status_forcelist=(500, 502, 503, 504),
                                                  allowed_methods=frozenset(["GET"]), raise_on_status=False)))


def when(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def calls_left(tok):
    """(remaining, limit, hours until reset) for today's Browse API calls, or None."""
    try:
        r = S.get(f"{EBAY}/developer/analytics/v1_beta/rate_limit/",
                  params={"api_context": "buy", "api_name": "browse"},
                  headers={"Authorization": f"Bearer {tok}"}, timeout=20)
        if r.status_code != 200:
            log(f"  eBay rate-limit lookup: HTTP {r.status_code}")
            return None
        named, tightest = None, None
        for api in r.json().get("rateLimits") or []:
            if (api.get("apiName") or "").lower() != "browse":
                continue
            for res in api.get("resources") or []:
                for rate in res.get("rates") or []:
                    if rate.get("remaining") is None:
                        continue
                    if res.get("name") == "buy.browse":
                        named = rate
                    if tightest is None or rate["remaining"] < tightest["remaining"]:
                        tightest = rate
        rate = named or tightest
        if not rate:
            return None
        reset = when(rate.get("reset"))
        hours = max(0.0, (reset - datetime.now(timezone.utc)).total_seconds() / 3600) if reset else 24.0
        return int(rate["remaining"]), int(rate.get("limit") or 0), hours
    except Exception as exc:
        log(f"  eBay rate-limit lookup failed: {str(exc)[:80]}")
        return None


def budget(left):
    if left is None:
        return min(MAX_CHECKS, FALLBACK_CHECKS), "eBay didn't say how many calls are left — checking a few"
    remaining, limit, hours = left
    reserve = int(RESERVE_PER_HOUR * hours + RESERVE_BASE)
    n = max(0, min(MAX_CHECKS, remaining - reserve))
    return n, (f"eBay calls left today: {remaining:,} of {limit:,} (resets in {hours:.1f}h) · "
               f"keeping {reserve:,} for the fast lane and full scan")


def look_up(item_id, tok):
    """('gone', why) | ('live', item_price, shipping or None) | ('stop', why) | ('skip', why)"""
    try:
        r = S.get(f"{EBAY}/buy/browse/v1/item/{item_id}",
                  headers={"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}, timeout=20)
    except Exception as exc:
        return ("skip", f"network: {str(exc)[:60]}")
    if r.status_code in (404, 410):
        return ("gone", "no longer on eBay")
    if r.status_code == 429:
        return ("stop", "eBay rate limit")
    if r.status_code != 200:
        return ("skip", f"HTTP {r.status_code}")
    try:
        d = r.json()
    except Exception:
        return ("skip", "unreadable reply")
    for a in d.get("estimatedAvailabilities") or []:
        if (a.get("estimatedAvailabilityStatus") or "").upper() == "OUT_OF_STOCK":
            return ("gone", "sold out")
    end = when(d.get("itemEndDate"))
    if end and end <= datetime.now(timezone.utc):
        return ("gone", "listing ended")
    price = money(d.get("price"))
    if price is None:
        return ("skip", "no price shown")
    return ("live", price, shipping_cost(d))


def reprice(d, price, ship):
    """None if it's no longer a deal at the new price, else the fields to update."""
    ship = float(d.get("shipping") or 0) if ship is None else ship
    total = round(price + ship, 2)
    msrp, value = float(d.get("msrp") or 0), float(d.get("market_value") or 0)
    if d.get("deal_type") == "retail" and msrp:
        if total >= msrp * 0.88:
            return None
    elif value and total >= value * 0.85:
        return None
    upd = {"item_price": round(price, 2), "shipping": round(ship, 2), "total_price": total}
    if d.get("pct_off_msrp") is not None and msrp:
        upd["pct_off_msrp"] = round((1 - total / msrp) * 100, 2)
    if value:
        upd["discount_pct"] = round((1 - total / value) * 100, 2)
        if d.get("source") == "fast":                       # the fast lane's own margin formula
            upd["margin_pct"] = round((value / total - 1) * 100, 2)
    return upd


def main():
    log(f"BrickMargin deal freshness — version {VERSION} · probe={PROBE} · "
        f"re-check after {CHECK_AFTER_HOURS:g}h · instant deals expire after {FAST_EXPIRE_HOURS:g}h unconfirmed")
    if not (SB and KEY):
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY are required")
    client = create_client(SB, KEY)
    tok = get_token()
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()

    # 1. re-check what people see first
    n, why = budget(calls_left(tok))
    log(f"  {why} · checking {n}")
    tally = {"still live": 0, "retired: gone": 0, "retired: price up": 0, "price updated": 0, "skipped": 0}
    if n:
        cutoff = (now - timedelta(hours=CHECK_AFTER_HOURS)).isoformat()
        rows = (client.table("deals")
                .select("item_id,set_num,title,total_price,item_price,shipping,msrp,market_value,deal_type,pct_off_msrp,source,score")
                .is_("gone_at", "null").lt("last_seen", cutoff)
                .order("score", desc=True).limit(n).execute()).data or []
        for d in rows:
            res = look_up(d["item_id"], tok)
            name = (d.get("title") or d["set_num"])[:48]
            if res[0] == "stop":
                log(f"  stopping: {res[1]}")
                break
            if res[0] == "skip":
                tally["skipped"] += 1
                continue
            if res[0] == "gone":
                tally["retired: gone"] += 1
                log(f"    ✗ {name:50} {res[1]}")
                if not PROBE:
                    client.table("deals").update({"gone_at": stamp}).eq("item_id", d["item_id"]).execute()
                continue
            _, price, ship = res
            ship_now = float(d.get("shipping") or 0) if ship is None else ship
            if abs((price + ship_now) - float(d["total_price"])) <= 0.5:
                tally["still live"] += 1
                if not PROBE:
                    client.table("deals").update({"last_seen": stamp}).eq("item_id", d["item_id"]).execute()
                continue
            upd = reprice(d, price, ship)
            if upd is None:
                tally["retired: price up"] += 1
                log(f"    ✗ {name:50} now ${price + ship_now:,.2f} — no longer a deal")
                if not PROBE:
                    client.table("deals").update({"gone_at": stamp}).eq("item_id", d["item_id"]).execute()
            else:
                tally["price updated"] += 1
                log(f"    ↻ {name:50} ${float(d['total_price']):,.2f} → ${upd['total_price']:,.2f}")
                if not PROBE:
                    client.table("deals").update({**upd, "last_seen": stamp}).eq("item_id", d["item_id"]).execute()
        log("  checked: " + " · ".join(f"{v} {k}" for k, v in tally.items() if v) if any(tally.values()) else "  nothing due a check")

    # 2. retire what nobody has confirmed (after the checks, so a deal just confirmed stays up)
    fast_cutoff = (now - timedelta(hours=FAST_EXPIRE_HOURS)).isoformat()
    q = client.table("deals").select("item_id", count="exact").is_("gone_at", "null").eq("source", "fast").lt("last_seen", fast_cutoff)
    due = q.execute().count or 0
    if due and not PROBE:
        (client.table("deals").update({"gone_at": stamp})
         .is_("gone_at", "null").eq("source", "fast").lt("last_seen", fast_cutoff).execute())
    log(f"  {'would retire' if PROBE else 'retired'} {due} instant deals nobody has confirmed in {FAST_EXPIRE_HOURS:g}h")
    if not PROBE:
        try:
            log(f"  retired {client.rpc('expire_stale_deals').execute().data} listings unconfirmed for 48h")
        except Exception as exc:
            log(f"  48h sweep skipped: {str(exc)[:80]}")
    log("Done.")


if __name__ == "__main__":
    main()
