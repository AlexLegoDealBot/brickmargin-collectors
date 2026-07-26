#!/usr/bin/env python3
"""
BrickMargin — eBay Comps Collector
===================================
Collector #3. Builds the resale-value side of the margin calculation.

WHAT IT DOES
  For a batch of sets, searches eBay's live listings, throws out the junk
  (instructions, empty boxes, custom builds), and records the median asking
  price plus a confidence signal.

IMPORTANT — THESE ARE ASKING PRICES, NOT SOLD PRICES
  eBay's sold-price data (Marketplace Insights) is a limited-release API that
  small developers are routinely denied. So we use the Browse API, which
  returns ACTIVE listings — what sellers hope to get, not what buyers paid.
  Asking prices run high, typically by a meaningful margin.

  We store them raw and honestly under source='ebay_active'. The haircut gets
  applied later in the scoring layer, where it's visible and tunable, rather
  than baked invisibly into stored data. When BrickLink seller access or
  Marketplace Insights comes through, real sold data lands alongside this
  under a different source and takes precedence. Nothing here gets wasted.

RATE BUDGET
  Browse API allows 5,000 calls/day at the application level. One call per
  set. At the default batch of 400, four runs a day is 1,600 calls — a full
  catalog cycle every ~4 days with plenty of headroom.

ENVIRONMENT VARIABLES
  SUPABASE_URL           required
  SUPABASE_SERVICE_KEY   required
  EBAY_APP_ID            required — Client ID from developer.ebay.com
  EBAY_CERT_ID           required — Client Secret
  BATCH_SIZE             optional — sets per run, default 400
  MIN_LISTINGS           optional — fewer than this and we record nothing, default 3
  PROBE                  optional — set to 1 to print samples, write nothing
"""

import base64
import os
import statistics
import sys
import time
from datetime import datetime, timezone

import requests
from supabase import create_client

# Bump on every edit. Prints in the log so you can confirm which version ran.
VERSION = "1.3-negation-aware"

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE = "EBAY_US"

LISTINGS_PER_SET = 50      # one API call's worth; plenty for a median
SLEEP = 0.25               # polite pacing between calls
UPSERT_BATCH = 100

# Titles containing any of these are not the sealed set we're pricing.
# Kept deliberately conservative — over-filtering silently starves the median.
JUNK_TERMS = (
    # Not the product at all
    "instruction", "manual", "sticker", "empty box", "box only", "no box",
    "replacement", "custom", "bootleg", "knock off", "knockoff", "not lego",
    "compatible", "minifig only", "minifigure only", "figure only",
    "poster", "catalog", "magazine", "display case", "light kit",
    "incomplete", "parts only", "spare",
    # Third-party builds sold under the set's number
    "display build", "display model",
    # OPENED OR USED, despite the seller marking it NEW. eBay's condition
    # filter is self-reported and routinely wrong; these phrases are how
    # used sets actually describe themselves.
    #   "100% complete" is used-set language — sealed sets say NISB/sealed
    #   "bagged" means loose polybags, no sealed box, and prices lower
    #   "displayed" means it was built and put on a shelf
    "100% complete", "100 % complete", "complete set displayed",
    "bagged", "resealed", "re-sealed",
    "displayed", "display only", "on display",
    "pre-owned", "preowned", "pre owned", "used",
    "open box", "opened", "missing",
)

# Some junk terms invert when negated: "never opened" and "unopened" both
# contain "opened" but mean the set IS sealed. Without this, the filter
# throws away exactly the listings it should trust most.
NEGATIONS = {
    "opened":    ("never opened", "unopened", "not opened", "un-opened"),
    "used":      ("unused", "never used", "un-used"),
    "displayed": ("never displayed", "not displayed"),
    "missing":   ("no missing", "nothing missing", "not missing"),
    "open box":  ("never open box",),
}

# Positive sealed indicators, used only when STRICT_SEALED is on. Requiring
# one of these is a much tighter gate than excluding bad phrases, but it also
# drops honest listings whose title just says "New". Toggle and compare.
SEALED_TERMS = (
    "sealed", "nisb", "nib", "new in box", "brand new", "factory sealed",
    "unopened", "misb", "never opened", "never built",
)


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

def require(name):
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"ERROR: missing environment variable {name}")
    return val


def int_env(name, default):
    """Empty string counts as unset — GitHub Actions sends '' for blank inputs."""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


SUPABASE_URL = require("SUPABASE_URL")
SUPABASE_SERVICE_KEY = require("SUPABASE_SERVICE_KEY")
EBAY_APP_ID = require("EBAY_APP_ID")
EBAY_CERT_ID = require("EBAY_CERT_ID")

BATCH_SIZE = int_env("BATCH_SIZE", 400)
MIN_LISTINGS = int_env("MIN_LISTINGS", 3)
STRICT_SEALED = os.environ.get("STRICT_SEALED", "").strip().lower() in ("1", "true", "yes")
PROBE = os.environ.get("PROBE", "").strip().lower() in ("1", "true", "yes")


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ----------------------------------------------------------------------
# eBay auth
# ----------------------------------------------------------------------

def get_token():
    """
    Client-credentials OAuth. Returns an application token good for ~2 hours,
    which comfortably outlasts a single run.
    """
    creds = base64.b64encode(
        f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()

    resp = requests.post(
        EBAY_OAUTH_URL,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )

    if resp.status_code != 200:
        sys.exit("ERROR: eBay rejected the credentials "
                 f"({resp.status_code}). Check EBAY_APP_ID and EBAY_CERT_ID, "
                 "and confirm they're PRODUCTION keys, not Sandbox. "
                 f"Response: {resp.text[:300]}")

    token = resp.json().get("access_token")
    if not token:
        sys.exit(f"ERROR: no access_token in eBay response: {resp.text[:300]}")
    log("  eBay token acquired")
    return token


# ----------------------------------------------------------------------
# Search + filter
# ----------------------------------------------------------------------

def base_number(set_num):
    """'75192-1' -> '75192'. eBay listings use the plain set number."""
    return set_num.split("-")[0].strip()


def search_listings(token, set_num):
    """One API call. Returns raw itemSummaries, or None on failure."""
    num = base_number(set_num)
    resp = requests.get(
        EBAY_SEARCH_URL,
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
                 "Content-Type": "application/json"},
        params={
            "q": f"LEGO {num}",
            # New/sealed only, fixed price only — auctions mid-flight aren't
            # meaningful price signals.
            "filter": "conditions:{NEW},buyingOptions:{FIXED_PRICE}",
            "limit": LISTINGS_PER_SET,
        },
        timeout=45,
    )

    if resp.status_code == 429:
        log("  RATE LIMITED by eBay — stopping this run cleanly")
        return "RATE_LIMIT"
    if resp.status_code != 200:
        log(f"  {set_num}: HTTP {resp.status_code}")
        return None

    return resp.json().get("itemSummaries", []) or []


def is_relevant(title, num):
    """
    Gates, in order. Returns (keep: bool, reason: str) so the probe output can
    show exactly why something was excluded.

      1. The set number must appear in the title — the strongest signal we have
         that this listing is the right product.
      2. No junk terms (wrong product, third-party build, or used/opened).
      3. If STRICT_SEALED, the title must positively assert sealed condition.
    """
    low = title.lower()

    if num not in low:
        return False, "wrong set"

    for term in JUNK_TERMS:
        if term in low:
            if any(neg in low for neg in NEGATIONS.get(term, ())):
                continue          # negated — means the opposite, keep going
            return False, f"'{term}'"

    if STRICT_SEALED and not any(t in low for t in SEALED_TERMS):
        return False, "not asserted sealed"

    return True, "ok"


def summarize(items, set_num):
    """
    Turn raw listings into a comp row. Returns (row, kept_count, sample) or
    (None, kept_count, sample) when there's too little to be meaningful.
    """
    num = base_number(set_num).lower()
    prices = []
    sample = []

    for it in items:
        title = it.get("title") or ""
        price_obj = it.get("price") or {}
        if (price_obj.get("currency") or "USD") != "USD":
            continue
        try:
            price = float(price_obj.get("value"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        keep, reason = is_relevant(title, num)
        if len(sample) < 8:
            sample.append(("KEEP" if keep else f"drop {reason}",
                           round(price, 2), title[:64]))
        if keep:
            prices.append(price)

    if len(prices) < MIN_LISTINGS:
        return None, len(prices), sample

    prices.sort()

    # Two-pass outlier trim. eBay is full of fantasy pricing — someone asking
    # $5,000 for an $850 set — and a single one of those wrecks the spread we
    # use as a confidence signal. So: take a preliminary median (robust), drop
    # anything implausibly far from it, then recompute on what's left.
    # The band is deliberately generous; it removes absurdities, not variance.
    prelim = statistics.median(prices)
    trimmed = [p for p in prices if 0.35 * prelim <= p <= 2.5 * prelim]
    if len(trimmed) < MIN_LISTINGS:
        trimmed = prices          # trimming ate too much — keep the raw set
    outliers = len(prices) - len(trimmed)

    median = statistics.median(trimmed)

    # Spread = interquartile range. Wide means sellers disagree and the median
    # deserves less trust downstream.
    if len(trimmed) >= 4:
        q = statistics.quantiles(trimmed, n=4)   # [p25, p50, p75]
        spread = round(q[2] - q[0], 2)
    else:
        spread = round(trimmed[-1] - trimmed[0], 2)

    if outliers:
        sample.append(("trim", outliers, f"{outliers} implausible price(s) excluded"))

    # Full distribution of what fed the median. Five sample titles can't tell
    # you whether a median is sane; the shape of all of them can.
    if len(trimmed) >= 4:
        q = statistics.quantiles(trimmed, n=4)
        dist = (f"min ${trimmed[0]:,.0f} | p25 ${q[0]:,.0f} | med ${median:,.0f} "
                f"| p75 ${q[2]:,.0f} | max ${trimmed[-1]:,.0f}  (n={len(trimmed)})")
    else:
        dist = (f"min ${trimmed[0]:,.0f} | med ${median:,.0f} "
                f"| max ${trimmed[-1]:,.0f}  (n={len(trimmed)})")
    sample.append(("dist", 0, dist))

    row = {
        "set_num": set_num,
        "source": "ebay_active",       # ASKING prices — see module docstring
        "condition": "sealed",
        "median_sold": round(median, 2),
        "comp_count": len(trimmed),
        "spread": spread,
        # Deliberately left NULL: active listings carry no sold volume, and
        # inventing a number here would corrupt the margin math downstream.
        "sold_count_30d": None,
        "sell_through": None,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    return row, len(prices), sample


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

def pick_stale_sets(client, limit):
    """
    Oldest-checked sets first, never-checked ahead of those. This is what
    makes the rotation work within the daily call budget.
    """
    resp = (client.table("sets")
            .select("set_num,name,comps_last_checked_at")
            .order("comps_last_checked_at", desc=False, nullsfirst=True)
            .limit(limit)
            .execute())
    return resp.data or []


def write_comps(client, rows):
    written = 0
    for i in range(0, len(rows), UPSERT_BATCH):
        batch = rows[i:i + UPSERT_BATCH]
        client.table("comps").insert(batch).execute()
        written += len(batch)
    return written


def mark_checked(client, set_nums):
    """
    Stamp every set we attempted, including ones that returned nothing.
    Without this, dead sets clog the front of the queue on every run.
    """
    now = datetime.now(timezone.utc).isoformat()
    for i in range(0, len(set_nums), UPSERT_BATCH):
        batch = set_nums[i:i + UPSERT_BATCH]
        (client.table("sets")
         .update({"comps_last_checked_at": now})
         .in_("set_num", batch)
         .execute())


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    log(f"BrickMargin eBay comps collector — version {VERSION}")
    log(f"Batch {BATCH_SIZE}, min listings {MIN_LISTINGS}, "
        f"strict_sealed={'ON' if STRICT_SEALED else 'off'}"
        + ("  [PROBE MODE — no writes]" if PROBE else ""))

    client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    token = get_token()

    targets = pick_stale_sets(client, 5 if PROBE else BATCH_SIZE)
    if not targets:
        sys.exit("ERROR: no sets found. Has the catalog collector run?")
    log(f"  {len(targets)} sets queued (stalest first)")

    rows = []
    attempted = []
    too_thin = 0
    failed = 0

    for i, s in enumerate(targets, 1):
        set_num = s["set_num"]
        items = search_listings(token, set_num)

        if items == "RATE_LIMIT":
            break
        if items is None:
            failed += 1
            attempted.append(set_num)
            continue

        row, kept, sample = summarize(items, set_num)
        attempted.append(set_num)

        if PROBE:
            log(f"  --- {set_num} · {s.get('name')} ---")
            log(f"      eBay returned {len(items)}, kept {kept} after filtering")
            for verdict, price, title in sample:
                if verdict == "dist":
                    log(f"      DISTRIBUTION: {title}")
                elif verdict == "trim":
                    log(f"      TRIMMED: {title}")
                else:
                    log(f"      [{verdict:22}] ${price:>9,.2f}  {title}")
            if row:
                log(f"      -> median ${row['median_sold']:,.2f}  "
                    f"spread ${row['spread']:,.2f}  n={row['comp_count']}")
            else:
                log("      -> nothing recorded (too few listings)")
            continue

        if row:
            rows.append(row)
        else:
            too_thin += 1

        if i % 50 == 0:
            log(f"  {i}/{len(targets)} — {len(rows)} comps, "
                f"{too_thin} too thin, {failed} errors")
        time.sleep(SLEEP)

    if PROBE:
        log("=" * 50)
        log("PROBE complete. Nothing was written.")
        return

    written = write_comps(client, rows) if rows else 0
    if attempted:
        mark_checked(client, attempted)

    log("=" * 50)
    log(f"Done. Attempted {len(attempted)}, wrote {written} comps, "
        f"{too_thin} too thin, {failed} errors.")
    log(f"API calls used this run: ~{len(attempted) + 1}")


if __name__ == "__main__":
    main()
