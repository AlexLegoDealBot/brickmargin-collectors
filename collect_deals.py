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
from concurrent.futures import ThreadPoolExecutor, as_completed
from supabase import create_client

VERSION = "3.1-retail"

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE = "EBAY_US"

# The site's default cost model. Kept in step with lib/settings.ts.
MARKETPLACE_PCT = 13.25
PROMOTED_PCT = 2.0
SHIP_TO_BUYER = 12.0

LISTINGS_PER_SET = 50
PAUSE = 0.05
# eBay answers in ~350ms, so a serial scan spends nearly all its time waiting
# rather than working. Eight workers turns a seven-minute run into about one.
# Held at eight deliberately: the Browse API has a daily call budget, and a
# burst wide enough to trip rate limiting costs more time than it saves.
WORKERS = int(os.environ.get("WORKERS", "8"))


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

# Two definitions of "deal", because two kinds of people are looking.
#
#   RETAIL — under the set's recommended price. What a shopper means by a
#            sale, what BrickHound-style sites show, and far more common.
#   MARKET — under what the set already resells for. The reseller's
#            definition: rarer, and the one worth real money.
#
# A listing qualifies on either, and gets flagged 'both' when it clears the
# two — which is the genuinely exceptional case.
MIN_RETAIL_DISCOUNT = num_env("MIN_RETAIL_DISCOUNT", 12)
MIN_DISCOUNT = num_env("MIN_DISCOUNT", 12)
# The ceiling matters more than the floor. Nothing sells a $1,600 set for
# $6 — a listing that cheap is a minifigure, a manual or an empty box that
# happens to mention the number. Anything below (100 - MAX_DISCOUNT)% of
# value is rejected as not-the-set rather than celebrated as a bargain.
# 70% was still generous enough to admit scam bait. A genuine underpriced
# set is usually 15-55% under market; below that, the listing is lying.
MAX_DISCOUNT = num_env("MAX_DISCOUNT", 52)
# A complete sealed set does not sell for a third of its retail price. When
# we know the set's MSRP, that becomes a hard floor: anything under this
# fraction of retail is a component, a part-out, or a fiction — never the
# whole box. This catches what vocabulary filters can't, because a seller
# listing one minifigure writes an honest title we simply can't parse.
MSRP_FLOOR_NEW = num_env("MSRP_FLOOR_NEW", 0.45)
MSRP_FLOOR_USED = num_env("MSRP_FLOOR_USED", 0.32)
# "Used" here means one thing: the complete set, opened. Not a part-built
# copy, not missing three pieces, not "most of it". eBay's condition codes
# can't express completeness, so the seller has to say it — a used listing
# must positively assert that the set is complete, or it doesn't qualify.
# This rejects plenty of honest listings that simply didn't use the word.
# That's the right trade: an incomplete set sold as complete is the one
# mistake that would cost us a reader for good.
REQUIRE_COMPLETE_USED = os.environ.get(
    "REQUIRE_COMPLETE_USED", "true").strip().lower() != "false"
# Requiring the word "sealed" on new listings turned out to be the single
# biggest yield killer: plenty of honest sellers write "LEGO 10312 Jazz Club
# Modular Building" and let eBay's own New condition speak for itself. The
# structural gates — theme-aware name matching and the partial-build
# patterns — already catch what this was guarding against, so the sealed
# claim now earns score rather than deciding entry.
REQUIRE_SEALED_NEW = os.environ.get(
    "REQUIRE_SEALED_NEW", "false").strip().lower() == "true"
MAX_PER_SET = int(num_env("MAX_PER_SET", 3))
# Feedback percentage alone means nothing — 100% from two sales is riskier
# than 98% from five thousand. Both gates must pass, and a seller with no
# feedback history at all is rejected rather than given the benefit of the
# doubt. We are sending real people to spend real money.
MIN_FEEDBACK = num_env("MIN_FEEDBACK", 95)
MIN_FEEDBACK_COUNT = int(num_env("MIN_FEEDBACK_COUNT", 15))
# A deal is a gap between a listing and OUR value — so a wrong value invents
# a deal that never existed. Only sets we're confident about qualify.
MIN_COMPS = int(num_env("MIN_COMPS", 5))
# The buy-and-hold thesis needs a set that is still in production. Once a
# set retires the market has already repriced it, so a discount there is
# just a discount — not a position. Retired sets are excluded by default.
EXCLUDE_RETIRED = os.environ.get("EXCLUDE_RETIRED", "true").strip().lower() != "false"
SOURCE = "ebay"
US_ONLY = os.environ.get("US_ONLY", "true").strip().lower() != "false"
MIN_MARGIN = num_env("MIN_MARGIN", 15)
MAX_SETS = int(num_env("MAX_SETS", 1200))
MIN_VALUE = num_env("MIN_VALUE", 40)
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
    # PART OF A SET, sold as its own item. These listings are honest — the
    # seller says exactly what they have — so the only failure was ours in
    # not reading it. A Charizard from set 72153 carries the set number AND
    # a word from the set name, and passes every other gate.
    "only", "just the", "from set", "from the set", "part of set",
    "parting out", "part out", "split", "single figure", "one figure",
    "individual", "loose", "second floor", "2nd floor", "third floor",
    "3rd floor", "first floor", "1st floor", "ground floor", "top floor",
    "middle floor", "one level", "single level", "no minifig", "without minifig",
    "figure from", "figs from", "minifigs from", "base only", "stand only",
    "display only", "no box", "sealed bag from", "bags only", "bag from",
    "partial", "incomplete set", "portion",
    # Knock-off sellers rarely say "fake" — they say these instead.
    "generic", "unbranded", "aftermarket", "replica", "clone", "third party",
    "compatible with", "fits lego", "lego style", "building blocks",
    "block set", "not lego", "no logo", "brick set compatible", "oem",
)


NAME_STOP = {
    "the", "and", "of", "a", "an", "with", "set", "lego", "for", "from",
    "edition", "collection", "series", "pack", "mini", "large", "small",
}


def name_tokens(name, theme=None):
    """
    Distinctive words from a set's name — with the theme's own words removed.

    This is subtler than it looks and it cost us a bad listing. "Death Star"
    yields ["death", "star"], but "star" is also in the theme "Star Wars", so
    EVERY Star Wars listing satisfied it. A TIE Fighter listing carrying
    75419 in its title passed the name gate on the strength of the word
    "Star" alone.

    Words the theme already contains carry no evidence about which set this
    is, so they're struck out. What remains is what actually identifies the
    product: "death".
    """
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    theme_words = set(re.findall(r"[a-z0-9]+", (theme or "").lower()))
    return [w for w in words
            if len(w) >= 4 and w not in NAME_STOP and w not in theme_words]


def is_relevant(title, num, set_name=None, theme=None):
    """
    The listing must plausibly BE the set.

    Three gates, and the third is the one that matters. A patch shop listing
    "LEGO 72153 embroidered patch" clears a number check and a category check
    — sellers miscategorise constantly — but it will never contain the word
    "Venusaur". Requiring a distinctive word from the set's own name is what
    separates the set from things that merely mention it.
    """
    t = (title or "").lower()

    if "lego" not in t:
        return False, "not lego"

    if not re.search(rf"(?<!\d){re.escape(num)}(?!\d)", t):
        return False, "number missing"

    for bad in JUNK:
        if bad in t:
            return False, bad

    tokens = name_tokens(set_name, theme)
    if tokens:
        hits = sum(1 for tok in tokens if tok in t)
        if hits == 0:
            return False, "name absent"
        # A set named for several things must be described by several of
        # them. "Venusaur, Charizard and Blastoise" needs at least two —
        # a listing naming only Charizard is selling only Charizard.
        need = 2 if len(tokens) >= 3 else 1
        if hits < need:
            return False, "partial name match"

    return True, ""


# Both conditions must now assert what they are. Blocklists lose to wording
# we didn't anticipate — "dinosaurs from 76949", "Jazz Club floor 2" — but a
# seller with the actual sealed box says so, because it's their selling point.
# Requiring the claim inverts the burden: unusual wording now fails closed.
SEALED_WORDS = (
    "sealed", "nisb", "nib", "new in box", "brand new", "unopened",
    "factory sealed", "never opened", "mint in box", "misb", "new sealed",
)

# Floors and levels of a modular, in every phrasing a seller might use.
PART_PATTERNS = (
    r"\b(floor|level|story|storey|section|module|tier)\s*#?\s*\d\b",
    r"\b\d\s*(st|nd|rd|th)?\s*(floor|level|story|storey|section)\b",
    r"\b(top|middle|bottom|upper|lower|ground|first|second|third)\s+"
    r"(floor|level|story|storey|section|half|part)\b",
    r"\b(figures?|minifigs?|minifigures?|dinosaurs?|animals?|creatures?|"
    r"dragons?|vehicles?|cars?|ships?|droids?)\s+(only|alone|from)\b",
    r"\bno\s+(box|set|build|building|instructions?|minifigs?|bricks?)\b",
    r"\bjust\s+(the\s+)?\w+\b",
)

# A used listing must claim the set is whole.
COMPLETE_WORDS = (
    "complete", "100%", "all pieces", "all parts", "full set", "whole set",
    "nothing missing", "no missing", "everything included", "all minifigs",
    "with all", "w/ all", "entire set",
)

# ...and is rejected outright if it admits otherwise.
INCOMPLETE_WORDS = (
    "missing", "incomplete", "not complete", "as is", "as-is", "damaged",
    "broken", "for parts", "spares", "repair", "unchecked", "untested",
    "not verified", "may be missing", "possibly missing", "no instructions",
    "without instructions", "no manual", "some pieces", "few pieces",
)


def looks_partial(title):
    """Catches the phrasings a plain word list misses."""
    t = (title or "").lower()
    return any(re.search(pat, t) for pat in PART_PATTERNS)


def new_is_sealed(title):
    """A 'new' listing has to claim the box is sealed."""
    t = (title or "").lower()
    return any(w in t for w in SEALED_WORDS)


def used_is_complete(title):
    """
    A used listing has to say the set is whole.

    Sellers of genuinely complete used sets almost always say so, because
    it's their main selling point. Sellers of incomplete ones say that too,
    in their own words — so both lists earn their place.
    """
    t = (title or "").lower()
    for bad in INCOMPLETE_WORDS:
        if bad in t:
            return False
    if not REQUIRE_COMPLETE_USED:
        return True
    return any(good in t for good in COMPLETE_WORDS)


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


def search(token, set_num, condition_filter="NEW|USED"):
    """
    One call per set, both conditions at once.

    The old code made a NEW request and a USED request per set — double the
    calls against a daily budget, for data eBay is happy to return together.
    """
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


REJECTED = []
REJECT_STATS = {}


def reject(reason):
    """Tally why a listing didn't qualify. An empty board should explain itself."""
    REJECT_STATS[reason] = REJECT_STATS.get(reason, 0) + 1
    return None


def hold_score(margin, discount, comps, seller_pct, seller_count, retired):
    """
    A 1-10 rating, so nobody has to weigh four numbers themselves.

    Deliberately blunt about what it rewards: profit after fees carries the
    most weight, then how far under market it sits, then how much evidence
    stands behind our value, then who is selling it. A set still in
    production earns a full point on top, because that is the position this
    board exists to find.

    It is a summary of figures shown on the same card, never a substitute
    for them — the numbers stay visible so anyone can disagree with the
    weighting.
    """
    pts = 0.0

    # Profit after fees — up to 4.
    pts += min(4.0, max(0.0, (margin - 20) / 25))

    # Distance under market — up to 2.
    pts += min(2.0, max(0.0, (discount - 15) / 17.5))

    # Evidence behind our value — up to 1.5.
    pts += min(1.5, (comps or 0) / 14)

    # Who is selling — up to 1.5.
    if seller_pct is not None and seller_count is not None:
        trust = ((seller_pct - 97) / 3) * 0.6 + min(1.0, seller_count / 500) * 0.9
        pts += max(0.0, min(1.5, trust))

    # Still buyable at retail — the hold candidate.
    if not retired:
        pts += 1.0

    return round(max(1.0, min(10.0, pts)), 1)


def evaluate(item, set_row):
    """Turn one listing into a deal row, or None."""
    num = base_number(set_row["set_num"])
    title = item.get("title") or ""

    keep, why = is_relevant(title, num, set_row.get("name"), set_row.get("theme"))
    if not keep:
        if PROBE and why in ("partial name match", "name absent"):
            REJECTED.append(f"{why:20} {title[:80]}")
        return reject(why)

    cond = condition_of(item)
    if cond is None:
        return None
    if looks_partial(title):
        return reject("partial build")
    if cond == "used" and not used_is_complete(title):
        return reject("used: no completeness claim")
    if cond == "new" and REQUIRE_SEALED_NEW and not new_is_sealed(title):
        return reject("new: no sealed claim")

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

    # --- against the retail price -----------------------------------------
    msrp = set_row.get("msrp")
    msrp = float(msrp) if msrp else None
    pct_off_msrp = None
    retail_deal = False
    if msrp and msrp > 0 and cond == "new":
        # Only sealed sets are compared to RRP — a used set being under
        # retail is not news, it's the definition of used.
        pct_off_msrp = (msrp - total) / msrp * 100
        retail_deal = pct_off_msrp >= MIN_RETAIL_DISCOUNT

    market_deal = discount >= MIN_DISCOUNT
    if not (market_deal or retail_deal):
        return reject("above both retail and market price")
    if discount > MAX_DISCOUNT:
        return reject("discount above ceiling")          # too good to be the set — it isn't the set

    # Retail-price floor. The strongest whole-set test we have, because it
    # doesn't depend on how the seller worded their title.
    if msrp:
        floor = msrp * (MSRP_FLOOR_NEW if cond == "new" else MSRP_FLOOR_USED)
        if total < floor:
            return reject("below MSRP floor")

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
        return reject("seller: no feedback published")                       # no history published — no thanks
    if seller_pct < MIN_FEEDBACK:
        return reject("seller: rating too low")
    if seller_count < MIN_FEEDBACK_COUNT:
        return reject("seller: too few sales")

    if US_ONLY:
        country = ((item.get("itemLocation") or {}).get("country") or "").upper()
        if country and country != "US":
            return None                   # long-haul knock-off shipping

    fees = benchmark * (MARKETPLACE_PCT + PROMOTED_PCT) / 100
    net = max(0.0, benchmark - fees - SHIP_TO_BUYER)
    margin = (net - total) / total * 100 if total else 0
    # A thin resale margin only disqualifies a market deal. A shopper buying
    # a set to build doesn't care what it would net them on resale.
    if margin < MIN_MARGIN:
        market_deal = False
        if not retail_deal:
            return reject("margin too thin")

    retired = bool(set_row.get("retired_at"))
    deal_type = ("both" if (market_deal and retail_deal)
                 else "market" if market_deal else "retail")

    # Score on whichever gap is the selling point, so a straightforward
    # retail sale isn't punished for having no resale margin.
    score_basis = margin if market_deal else (pct_off_msrp or 0)
    score_gap = discount if market_deal else (pct_off_msrp or 0)
    score = hold_score(score_basis, score_gap, set_row.get("comp_count"),
                       seller_pct, seller_count, retired)

    return {
        "item_id": str(item.get("itemId")),
        "source": SOURCE,
        "score": score,
        "retired": retired,
        "deal_type": deal_type,
        "msrp": round(msrp, 2) if msrp else None,
        "pct_off_msrp": round(pct_off_msrp, 2) if pct_off_msrp is not None else None,
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
        f"msrp floor {MSRP_FLOOR_NEW:.0%}/{MSRP_FLOOR_USED:.0%} · "
        f"max {MAX_PER_SET}/set · seller >={MIN_FEEDBACK}% "
        f"with >={MIN_FEEDBACK_COUNT} sales · US_ONLY={US_ONLY} · "
        f"{MAX_SETS} sets · probe={PROBE}")

    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Scan the sets worth scanning: priced, plausible, and expensive enough
    # that postage doesn't eat the whole margin.
    resp = (client.table("set_values")
            .select("set_num,name,theme,market_value,comp_count,confidence,msrp")
            .eq("plausible", True)
            .in_("confidence", ["high", "medium"])
            .gte("comp_count", MIN_COMPS)
            .gte("market_value", MIN_VALUE)
            .order("market_value", desc=True)
            .limit(MAX_SETS)
            .execute())
    sets = resp.data or []

    # Retirement status lives on `sets`, not on the values view.
    nums = [r["set_num"] for r in sets]
    retired_map = {}
    for i in range(0, len(nums), 200):
        chunk = nums[i:i + 200]
        got = (client.table("sets").select("set_num,retired_at")
               .in_("set_num", chunk).execute())
        for r in (got.data or []):
            retired_map[r["set_num"]] = r.get("retired_at")

    for r in sets:
        r["retired_at"] = retired_map.get(r["set_num"])

    if EXCLUDE_RETIRED:
        before = len(sets)
        sets = [r for r in sets if not r.get("retired_at")]
        log(f"  {before - len(sets)} retired sets excluded — "
            f"this board is for sets you can still buy and hold")

    log(f"  scanning {len(sets)} sets (confidence high/medium, "
        f">={MIN_COMPS} listings behind each value)")

    token = get_token()
    found = []
    rate_limited = False

    def scan(set_row):
        """One set, one call. Returns its qualifying deals."""
        items = search(token, set_row["set_num"])
        if items == "RATE_LIMIT":
            return "RATE_LIMIT"
        out = []
        for item in (items or []):
            deal = evaluate(item, set_row)
            if deal:
                out.append(deal)
        time.sleep(PAUSE)
        return out

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(scan, r): r for r in sets}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                result = fut.result()
            except Exception as exc:
                log(f"  {futures[fut]['set_num']}: {exc}")
                continue
            if result == "RATE_LIMIT":
                rate_limited = True
                continue
            found.extend(result)
            if done % 50 == 0:
                log(f"  {done}/{len(sets)} scanned · {len(found)} candidates")

    if rate_limited:
        log("  NOTE: eBay rate-limited some calls — results are partial")

    # One listing can arrive twice (multi-quantity, repeated pages), and a
    # single set with several cheap listings must not flood the board.
    unique = {}
    for d in found:
        prior = unique.get(d["item_id"])
        if not prior or d["margin_pct"] > prior["margin_pct"]:
            unique[d["item_id"]] = d

    per_set = {}
    for d in sorted(unique.values(), key=lambda x: -(x.get("score") or x["margin_pct"])):
        bucket = per_set.setdefault(d["set_num"], [])
        if len(bucket) < MAX_PER_SET:
            bucket.append(d)
    found = [d for bucket in per_set.values() for d in bucket]

    kinds = {}
    for d in found:
        kinds[d["deal_type"]] = kinds.get(d["deal_type"], 0) + 1
    log(f"  found {len(found)} deals across {len(per_set)} sets "
        f"({', '.join(f'{v} {k}' for k, v in sorted(kinds.items())) or 'none'})")

    # Why the rest didn't qualify. When the board comes back empty this is
    # the difference between tuning the right dial and guessing.
    if REJECT_STATS:
        log("  rejections by reason:")
        for reason, n in sorted(REJECT_STATS.items(), key=lambda kv: -kv[1]):
            log(f"    {n:6}  {reason}")

    if PROBE:
        for d in sorted(found, key=lambda x: -x["margin_pct"])[:12]:
            log(f"    {d['condition']:4} {d['set_num']:>10} "
                f"${d['total_price']:>8.2f} (item ${d['item_price']:.2f} "
                f"+ ship ${d['shipping']:.2f}) vs ${d['market_value']:.2f} "
                f"→ {d['margin_pct']:.0f}% · score {d['score']}/10 · "
                f"{d['seller']} {d['feedback_pct']}%")
        if REJECTED:
            log(f"  {len(REJECTED)} near-misses rejected — a sample:")
            for line in REJECTED[:15]:
                log(f"    {line}")
        log("  PROBE — nothing written")
        return

    written = 0
    for i in range(0, len(found), 200):
        batch = found[i:i + 200]
        client.table("deals").upsert(batch, on_conflict="item_id").execute()
        written += len(batch)
    log(f"  wrote {written}")

    # Retire anything that's gone.
    #
    # The 48-hour sweep alone was too slow: a set sells, and its listing sits
    # on the board for another two days pointing at a dead page. Because we
    # just re-scanned every one of these sets, we know precisely which
    # listings still exist — anything we didn't see this run is gone NOW.
    try:
        scanned_nums = sorted({r["set_num"] for r in sets})
        seen_ids = {d["item_id"] for d in found}
        retired_now = 0
        for i in range(0, len(scanned_nums), 100):
            chunk = scanned_nums[i:i + 100]
            live = (client.table("deals")
                    .select("item_id")
                    .in_("set_num", chunk)
                    .is_("gone_at", "null")
                    .execute())
            stale = [r["item_id"] for r in (live.data or [])
                     if r["item_id"] not in seen_ids]
            for j in range(0, len(stale), 100):
                (client.table("deals")
                 .update({"gone_at": datetime.now(timezone.utc).isoformat()})
                 .in_("item_id", stale[j:j + 100]).execute())
                retired_now += len(stale[j:j + 100])
        log(f"  retired {retired_now} listings that are no longer live")
    except Exception as exc:
        log(f"  immediate retire skipped: {exc}")

    # Backstop for sets not covered by this run.
    try:
        expired = client.rpc("expire_stale_deals").execute()
        log(f"  expired {expired.data} stale listings")
    except Exception as exc:
        log(f"  expire step skipped: {exc}")

    log("Done.")


if __name__ == "__main__":
    main()
