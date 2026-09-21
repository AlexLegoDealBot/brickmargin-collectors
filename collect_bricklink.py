#!/usr/bin/env python3
"""
BrickMargin · BrickLink price guide collector
=============================================

Fetches **completed transaction prices** — what sets actually sold for, not
what sellers are asking.

This is the most significant accuracy upgrade the site has had. Everything
until now has been built on live eBay listings, which systematically overstate
value because unsold listings linger while sales disappear from view. We have
said so plainly on the site and in our own research. BrickLink publishes six
months of real completed sales, split by condition, and that is a different
class of data.

What it writes, per set:
  sold_new / sold_used     — six-month average sale price by condition
  sold_new_qty / used_qty  — how many actually sold, which is the sample size
  sold_min / sold_max      — the range those sales fell in
  part_out_value           — what the individual parts are worth

Authentication is OAuth 1.0a, signed per request. BrickLink is stricter than
the other APIs we use: the signature covers the method, the URL and every
query parameter in sorted order, and any mismatch returns a bare 401 with no
explanation. The signing code below is therefore deliberately explicit.

Environment
-----------
  SUPABASE_URL, SUPABASE_SERVICE_KEY        required
  BRICKLINK_CONSUMER_KEY, ..._SECRET        required
  BRICKLINK_TOKEN, ..._TOKEN_SECRET         required
  BATCH_SIZE      default 180 — sets per run (BrickLink allows 5,000/day)
  PROBE           default true — print, write nothing
"""

import hashlib
import hmac
import os
import random
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# One session, with retries.
#
# A single TCP reset from eBay killed a whole run and threw away 129 comps
# already gathered — the network is not reliable and a collector that assumes
# it is will keep dying halfway. This retries connection errors and 5xx
# responses five times with a widening backoff, and reuses connections, which
# also makes every run a little faster.
# ---------------------------------------------------------------------------
HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(
    max_retries=Retry(
        total=5, connect=5, read=5, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    ),
    pool_connections=10, pool_maxsize=20,
))
from supabase import create_client

VERSION = "2.1-partout"

API = "https://api.bricklink.com/api/store/v1"


def require(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: {name} is required. Add it to the collectors repo "
                 f"under Settings → Secrets and variables → Actions.")
    return v


SUPABASE_URL = require("SUPABASE_URL")
SUPABASE_KEY = require("SUPABASE_SERVICE_KEY")
CONSUMER_KEY = require("BRICKLINK_CONSUMER_KEY")
CONSUMER_SECRET = require("BRICKLINK_CONSUMER_SECRET")
TOKEN = require("BRICKLINK_TOKEN")
TOKEN_SECRET = require("BRICKLINK_TOKEN_SECRET")

# BrickLink allows 5,000 calls a day. At ~1.8 calls per set (the second is
# skipped when a set isn't in their catalogue), 500 sets per run across four
# runs is ~3,600/day — comfortably inside the limit with room for retries,
# and it sweeps the whole 6,800-set catalogue in about three days rather
# than a fortnight.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE") or 400)
PART_OUT = os.environ.get("PART_OUT", "false").strip().lower() == "true"
PROBE = os.environ.get("PROBE", "true").strip().lower() != "false"
PAUSE = 0.35


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def quote(value):
    """Percent-encoding per RFC 5849 — stricter than urlencode's default."""
    return urllib.parse.quote(str(value), safe="~")


def sign(method, url, params):
    """
    Build an OAuth 1.0a signature.

    Every query parameter must appear in the base string, sorted by key, and
    percent-encoded twice over by the time it reaches the header. Getting any
    of that subtly wrong returns 401 with no hint as to which part, so this is
    written out step by step rather than cleverly.
    """
    oauth = {
        "oauth_consumer_key": CONSUMER_KEY,
        "oauth_token": TOKEN,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": f"{random.getrandbits(64):x}",
        "oauth_version": "1.0",
    }

    everything = {**params, **oauth}
    normalised = "&".join(
        f"{quote(k)}={quote(everything[k])}" for k in sorted(everything)
    )
    base = "&".join([method.upper(), quote(url), quote(normalised)])
    key = f"{quote(CONSUMER_SECRET)}&{quote(TOKEN_SECRET)}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()

    import base64
    oauth["oauth_signature"] = base64.b64encode(digest).decode()

    header = "OAuth " + ", ".join(
        f'{quote(k)}="{quote(v)}"' for k, v in sorted(oauth.items())
    )
    return header


def call(path, params=None, verbose=False, missing_is_404=False):
    """
    One signed GET.

    BrickLink returns its real reason inside a `meta` object rather than in
    the HTTP status, so a failure looks identical to an empty result unless
    you read the body. `verbose` prints that body — the difference between
    "something went wrong" and "TOKEN_IP_MISMATCHED".
    """
    params = params or {}
    url = f"{API}{path}"
    try:
        resp = HTTP.get(
            url, params=params,
            headers={"Authorization": sign("GET", url, params)},
            timeout=30,
        )
    except Exception as exc:
        log(f"    request failed: {exc}")
        return None

    if verbose:
        log(f"    HTTP {resp.status_code}")
        log(f"    body: {resp.text[:400]}")

    if resp.status_code == 429:
        return "RATE_LIMIT"

    try:
        body = resp.json()
    except Exception:
        if verbose:
            log("    response was not JSON — that usually means the request "
                "never reached the API")
        return None

    meta = body.get("meta", {}) or {}
    code = meta.get("code")

    if code and code != 200:
        # These are the ones worth naming, because each has a different fix.
        hints = {
            "TOKEN_IP_MISMATCHED":
                "The access token is locked to an IP. GitHub Actions runs from "
                "changing addresses — set the token's allowed IP to 0.0.0.0 "
                "with mask 255.255.255.255.",
            "BAD_OAUTH_REQUEST":
                "The signature was rejected. Usually one of the four secrets "
                "has a stray space or newline, or was renewed after being "
                "saved to GitHub.",
            "PERMISSION_DENIED":
                "The credentials are valid but not permitted for this call.",
            "RESOURCE_NOT_FOUND":
                "That set is not in BrickLink's catalogue under this number.",
        }
        desc = meta.get("description") or meta.get("message") or ""
        # 404s are routine: catalogues disagree at the edges. Counted at the
        # end rather than shouted about forty times a run.
        if not (missing_is_404 and code == 404):
            log(f"    BrickLink says: {code} — {desc}")
        if code in hints:
            log(f"    → {hints[code]}")
        if missing_is_404 and code == 404:
            return "NOT_IN_CATALOGUE"
        return None

    if resp.status_code == 404:
        return "NOT_IN_CATALOGUE" if missing_is_404 else None
    if resp.status_code != 200:
        log(f"    HTTP {resp.status_code} on {path}")
        return None

    return body.get("data")


def bl_number(set_num):
    """
    BrickLink numbers sets as "10312-1"; our catalogue already matches, but
    older imports sometimes lack the variant, and BrickLink rejects those.
    """
    return set_num if "-" in set_num else f"{set_num}-1"


def price_guide(set_num, condition, guide_type="sold"):
    """
    Six months of completed sales for one set in one condition.

    guide_type 'sold' is the whole point of this collector — 'stock' would
    return current asking prices, which is what we already have from eBay and
    is precisely the weaker data.
    """
    data = call(f"/items/SET/{bl_number(set_num)}/price", {
        "guide_type": guide_type,
        "new_or_used": condition,          # N or U
        "currency_code": "USD",
        "country_code": "US",
    }, missing_is_404=True)
    if data in (None, "RATE_LIMIT", "NOT_IN_CATALOGUE"):
        return data

    def num(key):
        try:
            return float(data.get(key))
        except (TypeError, ValueError):
            return None

    qty = data.get("total_quantity") or 0
    if not qty:
        return None

    return {
        "avg": num("avg_price"),
        "qty_avg": num("qty_avg_price"),   # weighted by quantity sold
        "min": num("min_price"),
        "max": num("max_price"),
        "qty": int(qty),
        "lots": int(data.get("unit_quantity") or 0),
    }


def part_out(set_num):
    """
    What a set is worth broken into pieces.

    BrickLink's price guide covers whole sets; a part-out value has to be
    assembled from the set's own inventory — every part and minifigure it
    contains, each priced at what that piece actually sells for. It is the
    number resellers check before buying, because a set can be worth more in
    pieces than sealed, and almost nobody publishes it free.

    Two calls per set: the inventory, then a bulk price lookup is not offered,
    so this prices the minifigures (which carry most of the value and are few)
    and estimates the parts from their average sold price. Honest about being
    an estimate — the site says so.
    """
    inv = call(f"/items/SET/{bl_number(set_num)}/subsets", {"break_minifigs": "false"},
               missing_is_404=True)
    if inv in (None, "RATE_LIMIT", "NOT_IN_CATALOGUE"):
        return None

    fig_total, part_count, fig_count = 0.0, 0, 0
    figs = []
    for entry in inv or []:
        for e in entry.get("entries", []):
            it = e.get("item") or {}
            qty = int(e.get("quantity") or 0)
            if it.get("type") == "MINIFIG":
                figs.append((it["no"], qty))
            elif it.get("type") == "PART":
                part_count += qty

    # figures are few and valuable — price them properly
    for fig_no, qty in figs[:12]:
        d = call(f"/items/MINIFIG/{fig_no}/price", {
            "guide_type": "sold", "new_or_used": "N",
            "currency_code": "USD", "country_code": "US"}, missing_is_404=True)
        time.sleep(PAUSE)
        if isinstance(d, dict) and d.get("total_quantity"):
            try:
                fig_total += float(d.get("qty_avg_price") or d.get("avg_price") or 0) * qty
                fig_count += qty
            except (TypeError, ValueError):
                pass

    if part_count == 0 and fig_count == 0:
        return None
    # A loose LEGO part averages around eleven cents sold on BrickLink across
    # the whole catalogue. Using one figure keeps this to two calls a set;
    # the site labels it an estimate rather than a measurement.
    parts_value = part_count * 0.11
    return {
        "part_out_value": round(parts_value + fig_total, 2),
        "part_out_figs": round(fig_total, 2),
        "part_out_parts": part_count,
        "part_out_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    log(f"BrickMargin BrickLink collector — version {VERSION}  probe={PROBE}")
    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Credentials first: a clear failure here beats 180 silent 401s.
    log("  checking credentials…")

    # Confirm the secrets arrived intact before blaming the signature. A
    # trailing newline pasted into a GitHub secret is invisible and breaks
    # the signature in a way that looks exactly like a wrong key.
    for name, val in (("CONSUMER_KEY", CONSUMER_KEY), ("CONSUMER_SECRET", CONSUMER_SECRET),
                      ("TOKEN", TOKEN), ("TOKEN_SECRET", TOKEN_SECRET)):
        clean = val.strip()
        flag = "" if clean == val else "  ← has whitespace around it"
        log(f"    {name}: {len(clean)} chars, starts {clean[:4]}…{flag}")

    # The simplest authenticated call there is — no set number involved, so a
    # failure here is definitely credentials rather than a catalogue miss.
    who = call("/items/SET/10276-1", verbose=True)
    if who is None:
        sys.exit("ERROR: the credential check failed. The BrickLink response "
                 "above says why.")
    log(f"  credentials accepted — catalogue reachable ({who.get('name', '?')})")

    # Sets we haven't priced from BrickLink yet, or priced longest ago.
    resp = (client.table("sets")
            .select("set_num,name,bricklink_checked_at")
            .order("bricklink_checked_at", desc=False, nullsfirst=True)
            .limit(BATCH_SIZE).execute())
    targets = resp.data or []
    log(f"  {len(targets)} sets queued (least recently checked first)")

    rows, priced, missing = [], 0, 0

    for i, s in enumerate(targets, 1):
        set_num = s["set_num"]
        # The catalogue entry first: it carries the product photograph, and
        # a 404 here means the set isn't on BrickLink at all, so we skip the
        # price calls entirely.
        item = call(f"/items/SET/{bl_number(set_num)}", missing_is_404=True)
        if item == "RATE_LIMIT":
            log("  RATE LIMITED — stopping cleanly with what we have")
            break
        if item == "NOT_IN_CATALOGUE" or item is None:
            missing += 1
            rows.append({"set_num": set_num,
                         "bricklink_checked_at": datetime.now(timezone.utc).isoformat()})
            time.sleep(PAUSE)
            continue
        img = item.get("image_url") or ""
        bl_image = ("https:" + img) if img.startswith("//") else (img or None)
        time.sleep(PAUSE)

        new = price_guide(set_num, "N")
        if new == "RATE_LIMIT":
            log("  RATE LIMITED — stopping cleanly with what we have")
            break
        time.sleep(PAUSE)

        # A 404 means the number is not in BrickLink's catalogue at all —
        # promotional codes, trading cards, internal LEGO part numbers. Asking
        # again for the used price wastes a call on a question already
        # answered, and those were 7% of the budget.
        if new == "NOT_IN_CATALOGUE":
            missing += 1
            rows.append({
                "set_num": set_num,
                "bricklink_checked_at": datetime.now(timezone.utc).isoformat(),
            })
            continue

        used = price_guide(set_num, "U")
        if used == "RATE_LIMIT":
            log("  RATE LIMITED — stopping cleanly with what we have")
            break
        if used == "NOT_IN_CATALOGUE":
            used = None
        time.sleep(PAUSE)

        if not new and not used:
            missing += 1
            rows.append({
                "set_num": set_num,
                "bricklink_checked_at": datetime.now(timezone.utc).isoformat(),
            })
            continue

        row = {
            "set_num": set_num,
            "bricklink_checked_at": datetime.now(timezone.utc).isoformat(),
            "bricklink_image": bl_image,
        }
        if new:
            row.update({
                "sold_new": round(new["qty_avg"] or new["avg"] or 0, 2),
                "sold_new_qty": new["qty"],
                "sold_new_min": round(new["min"] or 0, 2),
                "sold_new_max": round(new["max"] or 0, 2),
            })
        # Part-out is two extra calls, so it is reserved for sets someone
        # might actually break: worth enough to be worth the effort, and not
        # already done. The sold price we have just measured is the test.
        if PART_OUT and not s.get("part_out_at"):
            worth = (new or {}).get("qty_avg") or (new or {}).get("avg") or 0
            if worth and float(worth) >= 60:
                # Part-out is a bonus on top of the sold prices. If it fails
                # for one set, that set simply goes without — losing a whole
                # 500-set sweep to it would be absurd.
                try:
                    po = part_out(set_num)
                except Exception as exc:
                    log(f"      part-out failed: {type(exc).__name__}: {str(exc)[:70]}")
                    po = None
                if po:
                    row.update(po)
                    if PROBE:
                        log(f"      part-out {po['part_out_value']:,.0f} "
                            f"({po['part_out_parts']} parts + {po['part_out_figs']:,.0f} in figures)")

        if used:
            row.update({
                "sold_used": round(used["qty_avg"] or used["avg"] or 0, 2),
                "sold_used_qty": used["qty"],
            })
        rows.append(row)
        priced += 1

        if PROBE and priced <= 12:
            n = f"${row.get('sold_new', 0):.2f} ({row.get('sold_new_qty', 0)} sold)" if new else "—"
            u = f"${row.get('sold_used', 0):.2f} ({row.get('sold_used_qty', 0)} sold)" if used else "—"
            log(f"    {set_num:>10}  new {n:<22} used {u}")

        if i % 40 == 0:
            log(f"  {i}/{len(targets)} · {priced} with sold data · {missing} without")

    log(f"  {priced} sets have completed-sale prices, {missing} have none on record")

    if PROBE:
        log("  PROBE — nothing written")
        return

    # UPDATE, not upsert.
    #
    # Postgres builds the candidate row and checks NOT NULL *before* it
    # detects the conflict and switches to an update, so a partial payload of
    # {set_num, sold_new, ...} fails on `name` being null even though the set
    # already exists. The catalog collector hit this exact wall in July and
    # solved it by merging the full row into the payload — but here that would
    # mean fetching every set first just to satisfy a constraint.
    #
    # These sets always exist already (we read them from `sets` to build the
    # batch), so an update is both correct and simpler: it touches only the
    # columns we actually measured and never constructs a candidate row.
    written, failed = 0, 0
    for row in rows:
        payload = {k: v for k, v in row.items() if k != "set_num"}
        try:
            client.table("sets").update(payload).eq("set_num", row["set_num"]).execute()
            written += 1
        except Exception as exc:
            failed += 1
            if failed <= 3:
                log(f"  {row['set_num']}: {str(exc)[:120]}")
    log(f"  wrote {written}" + (f", {failed} failed" if failed else ""))
    log("Done.")


if __name__ == "__main__":
    main()
