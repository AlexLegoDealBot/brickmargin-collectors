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

VERSION = "1.2-heartbeat"

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
    Pull every minifigure Rebrickable knows, with its BrickLink ID and sets.

    Resumable, because it has to be: Rebrickable allows about one request a
    second and there are north of thirteen thousand figures, each needing two
    calls. That's five hours, and a workflow run is capped at six. So this
    writes every fifty figures rather than at the end, records each figure's
    Rebrickable ID, and on the next run skips everything already stored. Run
    it as many times as it takes; nothing is lost between runs.
    """
    if not RB_KEY: sys.exit("ERROR: REBRICKABLE_KEY is required for MODE=catalog")

    have = set()
    got = client.table("minifigs").select("rb_num").not_.is_("rb_num", "null").execute()
    for r in (got.data or []): have.add(r["rb_num"])
    log(f"  {len(have)} figures already stored — resuming")

    written, skipped, batch = 0, 0, []
    started = time.time()
    url = RB + "/minifigs/?page_size=1000&ordering=-num_parts"
    hdr = {"Authorization": f"key {RB_KEY}"}

    def flush():
        nonlocal batch, written
        if not batch: return
        if not PROBE:
            client.table("minifigs").upsert(batch, on_conflict="fig_num").execute()
        written += len(batch); batch = []

    while url:
        rel = url[len(RB):] if url.startswith(RB) else url
        data = rb_get(rel) if rel.startswith("/") else requests.get(url, headers=hdr, timeout=30).json()
        log(f"  list page fetched: {len(data.get('results', []))} figures on it")
        for m in data.get("results", []):
            if m["set_num"] in have: skipped += 1; continue
            try:
                d = rb_get(f"/minifigs/{m['set_num']}/")
                bl = ((d.get("external_ids") or {}).get("BrickLink") or [None])[0]
                if not bl:
                    have.add(m["set_num"]); continue
                sets = rb_get(f"/minifigs/{m['set_num']}/sets/", {"page_size": 50}).get("results", [])
            except Exception as exc:
                log(f"    {m['set_num']}: {str(exc)[:60]}"); continue
            batch.append({"fig_num": bl, "rb_num": m["set_num"], "name": m["name"],
                          "image_url": m.get("set_img_url"),
                          "in_sets": [x["set_num"] for x in sets]})
            have.add(m["set_num"])
            if (written + len(batch)) % 10 == 0:
                log(f"  {written + len(batch)} figures so far · {skipped} skipped · {int(time.time()-started)}s")
            if PROBE and len(batch) <= 10:
                log(f"    {bl:>10}  {m['name'][:48]}  in {len(sets)} sets")
            if len(batch) >= 50:
                flush(); log(f"  {written} written · {skipped} skipped · {int(time.time()-started)}s")
            if PROBE and written + len(batch) >= 20:
                log("  PROBE — stopping early, nothing written"); return
            # leave the runner ten minutes short of its ceiling
            if time.time() - started > 340 * 60:
                flush(); log("  time limit approaching — stopping cleanly; run again to continue"); return
        url = data.get("next")
    flush()
    log(f"  catalogue complete: {written} written this run, {skipped} already had")


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
