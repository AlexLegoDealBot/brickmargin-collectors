#!/usr/bin/env python3
"""
BrickMargin · minifigure collector
==================================

Two sources, two jobs:

  Rebrickable  — the catalogue. Every minifigure, its name, and which sets it
                 appears in. Free, and already the source for our set list.
  BrickLink    — the prices. Six months of completed sales by condition.
                 This is where figures actually trade, and where the sw0001
                 style IDs come from, so we key everything on those.

Rebrickable's figure IDs (fig-000123) are not BrickLink's (sw0001), but
Rebrickable publishes the mapping in each figure's external_ids, which is
what makes the join possible without guessing.

Environment
-----------
  SUPABASE_URL, SUPABASE_SERVICE_KEY, REBRICKABLE_KEY,
  BRICKLINK_CONSUMER_KEY/SECRET, BRICKLINK_TOKEN/SECRET   required
  MODE       catalog | prices   (default: prices)
  BATCH_SIZE default 350
  PROBE      default true
"""

import os, sys, time, hmac, hashlib, base64, random, urllib.parse
from datetime import datetime, timezone
import requests
from supabase import create_client

VERSION = "2.0-bulk"

def require(n):
    v = os.environ.get(n, "").strip()
    if not v: sys.exit(f"ERROR: {n} is required")
    return v

SB_URL = require("SUPABASE_URL"); SB_KEY = require("SUPABASE_SERVICE_KEY")
MODE = os.environ.get("MODE", "prices").strip().lower()
PROBE = os.environ.get("PROBE", "true").strip().lower() != "false"
BATCH = int(os.environ.get("BATCH_SIZE") or 350)
RB_KEY = os.environ.get("REBRICKABLE_KEY", "").strip()
CK = os.environ.get("BRICKLINK_CONSUMER_KEY", "").strip()
CS = os.environ.get("BRICKLINK_CONSUMER_SECRET", "").strip()
TK = os.environ.get("BRICKLINK_TOKEN", "").strip()
TS = os.environ.get("BRICKLINK_TOKEN_SECRET", "").strip()

def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)
def now(): return datetime.now(timezone.utc).isoformat()

# ---------------------------------------------------------------- BrickLink
BL = "https://api.bricklink.com/api/store/v1"
def q(v): return urllib.parse.quote(str(v), safe="~")
def bl_get(path, params=None):
    params = params or {}; url = BL + path
    o = {"oauth_consumer_key": CK, "oauth_token": TK, "oauth_signature_method": "HMAC-SHA1",
         "oauth_timestamp": str(int(time.time())), "oauth_nonce": f"{random.getrandbits(64):x}",
         "oauth_version": "1.0"}
    allp = {**params, **o}
    base = "&".join(["GET", q(url), q("&".join(f"{q(k)}={q(allp[k])}" for k in sorted(allp)))])
    sig = base64.b64encode(hmac.new(f"{q(CS)}&{q(TS)}".encode(), base.encode(), hashlib.sha1).digest()).decode()
    o["oauth_signature"] = sig
    hdr = "OAuth " + ", ".join(f'{q(k)}="{q(v)}"' for k, v in sorted(o.items()))
    r = requests.get(url, params=params, headers={"Authorization": hdr}, timeout=30)
    if r.status_code == 429: return "RATE_LIMIT"
    try: body = r.json()
    except Exception: return None
    code = (body.get("meta") or {}).get("code")
    if code == 404: return "MISSING"
    if code and code != 200:
        log(f"    BrickLink {code}: {(body.get('meta') or {}).get('description', '')[:80]}"); return None
    return body.get("data")

def price(fig, cond):
    d = bl_get(f"/items/MINIFIG/{fig}/price", {"guide_type": "sold", "new_or_used": cond,
                                               "currency_code": "USD", "country_code": "US"})
    if d in (None, "RATE_LIMIT", "MISSING"): return d
    qty = int(d.get("total_quantity") or 0)
    if not qty: return None
    f = lambda k: float(d[k]) if d.get(k) not in (None, "") else None
    return {"avg": f("qty_avg_price") or f("avg_price"), "min": f("min_price"), "max": f("max_price"), "qty": qty}

# --------------------------------------------------------------- Rebrickable
RB = "https://rebrickable.com/api/v3/lego"
RB_LAST = [0.0]
def rb_get(path, params=None, attempt=0):
    """One Rebrickable call, held to their rate limit, with visible backoff."""
    # never more than one call per 1.1s, whatever the caller does
    wait = 1.1 - (time.time() - RB_LAST[0])
    if wait > 0: time.sleep(wait)
    RB_LAST[0] = time.time()
    r = requests.get(RB + path, params=params or {}, headers={"Authorization": f"key {RB_KEY}"}, timeout=30)
    if r.status_code == 429:
        pause = min(60, 5 * (2 ** attempt))
        log(f"    Rebrickable throttled us — waiting {pause}s (attempt {attempt + 1})")
        time.sleep(pause)
        if attempt >= 6: raise RuntimeError("Rebrickable keeps throttling; stopping this run")
        return rb_get(path, params, attempt + 1)
    r.raise_for_status(); return r.json()

def catalog(client):
    """
    Build the figure catalogue from Rebrickable's bulk files.

    The API route needed two calls per figure — 26,000 calls at one per
    second, seven hours, and a rate limit to trip over. Rebrickable publishes
    the same data as gzipped CSVs, so this is three downloads and a join:

      minifigs.csv          every figure: number, name, image
      inventory_minifigs    which inventories contain which figure
      inventories.csv       which set each inventory belongs to

    That gives us the figure list and the sets each appears in. The one thing
    the CSVs lack is BrickLink's ID, so figures are keyed by Rebrickable's
    fig-XXXXXX and the price step resolves BrickLink IDs lazily, only for the
    figures it's about to price. Minutes instead of hours, and nothing to
    babysit.
    """
    import csv, gzip, io, collections

    def grab(name):
        url = f"https://cdn.rebrickable.com/media/downloads/{name}.csv.gz"
        log(f"  downloading {name}.csv.gz")
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        text = gzip.decompress(r.content).decode("utf-8", "replace")
        return list(csv.DictReader(io.StringIO(text)))

    figs = grab("minifigs")
    log(f"  {len(figs):,} figures in the catalogue")
    inv_figs = grab("inventory_minifigs")
    inventories = grab("inventories")

    # inventory id -> set number, then figure -> the sets it appears in
    inv_set = {i["id"]: i["set_num"] for i in inventories}
    in_sets = collections.defaultdict(set)
    for r in inv_figs:
        s_num = inv_set.get(r["inventory_id"])
        if s_num:
            in_sets[r["fig_num"]].add(s_num)
    log(f"  mapped {len(in_sets):,} figures to their sets")

    rows = [{
        "fig_num": f["fig_num"],            # keyed on Rebrickable id for now
        "rb_num": f["fig_num"],
        "name": f["name"],
        "image_url": f.get("img_url") or None,
        "in_sets": sorted(in_sets.get(f["fig_num"], []))[:60],
    } for f in figs if f.get("name")]

    if PROBE:
        for r in rows[:10]:
            log(f"    {r['fig_num']:>14}  {r['name'][:48]:<50} in {len(r['in_sets'])} sets")
        log(f"  PROBE — {len(rows):,} figures ready, nothing written")
        return

    written = 0
    for i in range(0, len(rows), 500):
        client.table("minifigs").upsert(rows[i:i + 500], on_conflict="fig_num").execute()
        written += len(rows[i:i + 500])
        if written % 5000 == 0:
            log(f"  wrote {written:,}")
    log(f"  catalogue complete: {written:,} figures")


def prices(client):
    """Sold prices for the least-recently-checked figures."""
    for n, v in (("BRICKLINK_CONSUMER_KEY", CK), ("BRICKLINK_TOKEN", TK)):
        if not v: sys.exit(f"ERROR: {n} is required for MODE=prices")
    # Price the figures most likely to be looked up: the ones in the most
    # sets first, since those are the ones people actually own.
    figs = (client.table("minifigs").select("fig_num,rb_num,bl_num,name,in_sets")
            .is_("checked_at", "null").limit(BATCH).execute()).data or []
    log(f"  {len(figs)} figures queued")
    priced, missing, updates = 0, 0, []
    for i, f in enumerate(figs, 1):
        # BrickLink prices by its own id (sw0001), Rebrickable by fig-000123.
        # Resolve once, remember it, and skip the lookup ever after.
        bl = f.get("bl_num")
        if not bl:
            if not RB_KEY:
                log("  REBRICKABLE_KEY needed once to resolve BrickLink ids"); break
            try:
                d = rb_get(f"/minifigs/{f['rb_num'] or f['fig_num']}/")
                bl = ((d.get("external_ids") or {}).get("BrickLink") or [None])[0]
            except Exception as exc:
                log(f"    {f['fig_num']}: {str(exc)[:60]}"); continue
            if not bl:
                updates.append((f["fig_num"], {"checked_at": now(), "bl_num": "none"}))
                missing += 1
                continue
            updates.append((f["fig_num"], {"bl_num": bl}))
        if bl == "none":
            missing += 1
            updates.append((f["fig_num"], {"checked_at": now()}))
            continue

        new = price(bl, "N")
        if new == "RATE_LIMIT": log("  rate limited — stopping"); break
        row = {"checked_at": now()}
        if new == "MISSING":
            missing += 1
        else:
            time.sleep(0.3)
            used = price(bl, "U")
            if used == "RATE_LIMIT": break
            if new:
                row.update({"sold_new": round(new["avg"], 2), "sold_new_qty": new["qty"],
                            "sold_min": new["min"], "sold_max": new["max"]}); priced += 1
            if used and used not in ("MISSING",):
                row.update({"sold_used": round(used["avg"], 2), "sold_used_qty": used["qty"]})
        updates.append((f["fig_num"], row))
        if PROBE and i <= 10:
            log(f"    {f['fig_num']:>10}  new {'$%.2f (%d sold)' % (new['avg'], new['qty']) if isinstance(new, dict) else '—':<22} {f['name'][:40]}")
        time.sleep(0.3)
    log(f"  {priced} priced · {missing} not on BrickLink")
    if PROBE: log("  PROBE — nothing written"); return
    for fig, row in updates:
        client.table("minifigs").update(row).eq("fig_num", fig).execute()
    log(f"  wrote {len(updates)}")

def main():
    log(f"BrickMargin minifig collector — version {VERSION}  mode={MODE}  probe={PROBE}")
    client = create_client(SB_URL, SB_KEY)
    (catalog if MODE == "catalog" else prices)(client)
    log("Done.")

if __name__ == "__main__":
    main()
