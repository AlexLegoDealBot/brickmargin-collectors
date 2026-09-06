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

VERSION = "1.0"

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
def rb_get(path, params=None):
    r = requests.get(RB + path, params=params or {}, headers={"Authorization": f"key {RB_KEY}"}, timeout=30)
    if r.status_code == 429: time.sleep(5); return rb_get(path, params)
    r.raise_for_status(); return r.json()

def catalog(client):
    """Pull every minifigure Rebrickable knows, with its BrickLink ID and sets."""
    if not RB_KEY: sys.exit("ERROR: REBRICKABLE_KEY is required for MODE=catalog")
    rows, page, url = [], 1, "/minifigs/"
    params = {"page_size": 1000, "ordering": "-num_parts"}
    while url:
        data = rb_get(url if url.startswith("/") else "", params) if url.startswith("/") else requests.get(url, headers={"Authorization": f"key {RB_KEY}"}, timeout=30).json()
        for m in data.get("results", []):
            rows.append(m)
        log(f"  page {page}: {len(rows)} figures so far")
        url = data.get("next"); params = None; page += 1
        if PROBE and page > 2: break
    log(f"  {len(rows)} figures from Rebrickable — resolving BrickLink IDs")

    out, done = [], 0
    for m in rows:
        # each figure's detail carries external_ids.BrickLink
        try:
            d = rb_get(f"/minifigs/{m['set_num']}/")
        except Exception:
            continue
        bl = ((d.get("external_ids") or {}).get("BrickLink") or [None])[0]
        if not bl: continue
        try:
            sets = rb_get(f"/minifigs/{m['set_num']}/sets/", {"page_size": 50}).get("results", [])
        except Exception:
            sets = []
        out.append({"fig_num": bl, "name": m["name"], "image_url": m.get("set_img_url"),
                    "in_sets": [s["set_num"] for s in sets]})
        done += 1
        if done % 100 == 0: log(f"  {done} resolved")
        time.sleep(0.6)   # Rebrickable: ~1 req/sec
        if PROBE and done >= 25: break
    log(f"  {len(out)} figures with BrickLink IDs")
    if PROBE:
        for r in out[:8]: log(f"    {r['fig_num']:>10}  {r['name'][:50]}  in {len(r['in_sets'])} sets")
        log("  PROBE — nothing written"); return
    for i in range(0, len(out), 200):
        client.table("minifigs").upsert(out[i:i+200], on_conflict="fig_num").execute()
    log(f"  wrote {len(out)}")

def prices(client):
    """Sold prices for the least-recently-checked figures."""
    for n, v in (("BRICKLINK_CONSUMER_KEY", CK), ("BRICKLINK_TOKEN", TK)):
        if not v: sys.exit(f"ERROR: {n} is required for MODE=prices")
    figs = (client.table("minifigs").select("fig_num,name")
            .order("checked_at", desc=False, nullsfirst=True).limit(BATCH).execute()).data or []
    log(f"  {len(figs)} figures queued")
    priced, missing, updates = 0, 0, []
    for i, f in enumerate(figs, 1):
        new = price(f["fig_num"], "N")
        if new == "RATE_LIMIT": log("  rate limited — stopping"); break
        row = {"checked_at": now()}
        if new == "MISSING":
            missing += 1
        else:
            time.sleep(0.3)
            used = price(f["fig_num"], "U")
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
