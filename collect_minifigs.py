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

VERSION = "3.0-bricklink"

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
    Build the figure catalogue from BrickLink, one set at a time.

    Rebrickable turned out to have no BrickLink IDs for minifigures — only
    for parts and sets — so the previous approach was matching against a
    field that doesn't exist, and every figure came back "not on BrickLink".

    BrickLink's own set inventories fix this at the root: /items/SET/{no}/
    subsets lists every minifigure in a set WITH its BrickLink ID. Walk our
    set catalogue, collect the figures, and the mapping problem disappears
    because there is no mapping — the IDs are BrickLink's from the start.
    Images come from BrickLink's predictable URL pattern, so no extra call.

    One call per set, ~6,800 sets, 5,000 calls a day: two or three runs.
    Resumable — each set is marked when done.
    """
    todo = (client.table("sets").select("set_num,name,theme,year_released")
            .is_("minifigs_checked_at", "null")
            .order("year_released", desc=True).limit(BATCH).execute()).data or []
    log(f"  {len(todo)} sets to inventory this run")
    figs, done, empty = {}, [], 0

    for i, st in enumerate(todo, 1):
        no = st["set_num"] if "-" in st["set_num"] else st["set_num"] + "-1"
        d = bl_get(f"/items/SET/{no}/subsets", {"break_minifigs": "false"})
        if d == "RATE_LIMIT":
            log("  rate limited — stopping cleanly; run again to continue"); break
        done.append(st["set_num"])
        if d in (None, "MISSING"):
            empty += 1; continue
        found = 0
        for entry in d or []:
            for e in entry.get("entries", []):
                it = e.get("item") or {}
                if it.get("type") != "MINIFIG": continue
                fig = it["no"]; found += 1
                row = figs.setdefault(fig, {
                    "fig_num": fig, "bl_num": fig, "name": it.get("name", fig),
                    "theme": st.get("theme"), "year_released": st.get("year_released"),
                    "image_url": f"https://img.bricklink.com/ItemImage/MN/0/{fig}.png",
                    "in_sets": set(),
                })
                row["in_sets"].add(st["set_num"])
                # earliest appearance wins for year
                if st.get("year_released") and (row["year_released"] or 9999) > st["year_released"]:
                    row["year_released"] = st["year_released"]
        if i % 50 == 0:
            log(f"  {i}/{len(todo)} sets · {len(figs)} figures so far")
        if PROBE and i >= 15: break
        time.sleep(0.25)

    rows = []
    for r in figs.values():
        r["in_sets"] = sorted(r["in_sets"]); r["set_count"] = len(r["in_sets"]); rows.append(r)
    log(f"  {len(rows)} figures from {len(done)} sets ({empty} had no inventory on BrickLink)")

    if PROBE:
        for r in rows[:10]: log(f"    {r['fig_num']:>10}  {r['name'][:46]:<48} in {r['set_count']} sets")
        log("  PROBE — nothing written"); return

    # merge in_sets with what's already stored, so a figure seen across runs
    # keeps every set rather than only the latest batch's
    if rows:
        have = (client.table("minifigs").select("fig_num,in_sets")
                .in_("fig_num", [r["fig_num"] for r in rows]).execute()).data or []
        prev = {h["fig_num"]: set(h.get("in_sets") or []) for h in have}
        for r in rows:
            r["in_sets"] = sorted(set(r["in_sets"]) | prev.get(r["fig_num"], set()))
            r["set_count"] = len(r["in_sets"])
        for i in range(0, len(rows), 300):
            client.table("minifigs").upsert(rows[i:i+300], on_conflict="fig_num").execute()
    for i in range(0, len(done), 300):
        client.table("sets").update({"minifigs_checked_at": now()}).in_("set_num", done[i:i+300]).execute()
    log(f"  wrote {len(rows)} figures · marked {len(done)} sets")


def prices(client):
    """Sold prices for the least-recently-checked figures."""
    for n, v in (("BRICKLINK_CONSUMER_KEY", CK), ("BRICKLINK_TOKEN", TK)):
        if not v: sys.exit(f"ERROR: {n} is required for MODE=prices")
    # Price the figures most likely to be looked up: the ones in the most
    # sets first, since those are the ones people actually own.
    figs = (client.table("minifigs").select("fig_num,bl_num,name,set_count")
            .is_("checked_at", "null")
            .order("set_count", desc=True).limit(BATCH).execute()).data or []
    log(f"  {len(figs)} figures queued")
    priced, missing, updates = 0, 0, []
    for i, f in enumerate(figs, 1):
        bl = f.get("bl_num") or f["fig_num"]
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
