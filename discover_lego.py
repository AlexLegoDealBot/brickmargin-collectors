#!/usr/bin/env python3
"""
BrickMargin — LEGO.com Listing Discovery
=========================================
Collector #2a. Finds which of our sets LEGO.com actually sells, and where.

WHY THIS IS SEPARATE FROM PRICE COLLECTION
  We have 6,678 set numbers and zero product URLs. A price collector needs a
  page to poll. This builds that map once a week; the price collector then
  polls the map many times a day.

  It also answers a question we can't currently answer: how many of our sets
  are actually buyable right now? That number is the real size of the deal
  universe, and it decides how the polling tiers get budgeted.

HOW IT FINDS PAGES — POLITELY
  We read robots.txt, take the sitemap locations LEGO publishes there, and walk
  them. Sitemaps exist precisely so automated clients can discover pages without
  crawling. This is the front door, not a side window: a handful of requests
  total, no product-page hammering, and an honest User-Agent.

ENVIRONMENT VARIABLES
  SUPABASE_URL           required
  SUPABASE_SERVICE_KEY   required
  LEGO_LOCALE            optional — URL segment to keep, default 'en-us'
  PROBE                  optional — set to 1 to print findings, write nothing
"""

import gzip
import io
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from supabase import create_client

VERSION = "1.1-sitemap-discovery"

ROBOTS_URL = "https://www.lego.com/robots.txt"
FALLBACK_SITEMAPS = ["https://www.lego.com/sitemap.xml"]

# Identify ourselves honestly rather than masquerading as a browser.
USER_AGENT = "BrickMargin/1.0 (LEGO price tracker; contact via brickmargin.com)"

SLEEP = 1.0            # between sitemap fetches
UPSERT_BATCH = 200
PAGE = 1000            # Supabase returns 1000 rows max per select


def require(name):
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"ERROR: missing environment variable {name}")
    return val


SUPABASE_URL = require("SUPABASE_URL")
SUPABASE_SERVICE_KEY = require("SUPABASE_SERVICE_KEY")
LEGO_LOCALE = os.environ.get("LEGO_LOCALE", "en-us").strip().lower()
PROBE = os.environ.get("PROBE", "").strip().lower() in ("1", "true", "yes")


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ----------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------

def fetch(url, timeout=45):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_xml(url):
    """
    Sitemaps are often gzipped. requests handles Content-Encoding automatically,
    but a literal .xml.gz file has to be decompressed by hand.
    """
    resp = fetch(url)
    content = resp.content
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.GzipFile(fileobj=io.BytesIO(content)).read()
    return content


def sitemaps_from_robots():
    """robots.txt lists Sitemap: lines. That's the published, intended entry point."""
    try:
        text = fetch(ROBOTS_URL, timeout=30).text
    except Exception as exc:
        log(f"  robots.txt unreachable ({exc}) — using fallback")
        return list(FALLBACK_SITEMAPS)

    found = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)
    if PROBE:
        log(f"  robots.txt lists {len(found)} sitemap(s)")
        for u in found[:10]:
            log(f"      {u}")
    return found or list(FALLBACK_SITEMAPS)


def parse_sitemap(xml_bytes):
    """
    Returns (child_sitemap_urls, page_urls). A sitemap index contains <sitemap>
    entries pointing at more sitemaps; a leaf sitemap contains <url> entries.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log(f"    XML parse failed: {exc}")
        return [], []

    # Namespaces vary, so match on the tag's local name instead.
    children, pages = [], []
    for elem in root:
        tag = elem.tag.split("}")[-1]
        loc = None
        for sub in elem:
            if sub.tag.split("}")[-1] == "loc" and sub.text:
                loc = sub.text.strip()
                break
        if not loc:
            continue
        if tag == "sitemap":
            children.append(loc)
        elif tag == "url":
            pages.append(loc)
    return children, pages


# ----------------------------------------------------------------------
# Set number extraction
# ----------------------------------------------------------------------

# LEGO product URLs look like:
#   https://www.lego.com/en-us/product/millennium-falcon-75192
# The set number is the trailing digit run in the slug.
PRODUCT_RE = re.compile(rf"/{re.escape(LEGO_LOCALE)}/product/([^/?#]+)", re.I)
TRAILING_NUM_RE = re.compile(r"(\d{4,8})$")


def set_number_from_url(url):
    """
    Extract the set number from a product slug.

    Guard against year-shaped false positives: a marketing page like
    /product/gift-idea-2024 would otherwise parse as set "2024" and could be
    mis-mapped onto a real catalog entry. Modern set numbers are 5+ digits,
    so any bare 4-digit value in year range is rejected.
    """
    m = PRODUCT_RE.search(url)
    if not m:
        return None
    slug = m.group(1).rstrip("/")
    n = TRAILING_NUM_RE.search(slug)
    if not n:
        return None

    num = n.group(1)
    if len(num) == 4 and 1900 <= int(num) <= 2100:
        return None
    return num


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

def fetch_all_sets(client):
    """Supabase caps a select at 1000 rows, so page through explicitly."""
    rows, start = [], 0
    while True:
        resp = (client.table("sets").select("set_num,name")
                .order("set_num").range(start, start + PAGE - 1).execute())
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        start += PAGE
    return rows


def build_index(sets):
    """
    Map base number -> set_num. '75192-1' indexes under '75192'.
    Where variants collide (10242-1 and 10242-2), prefer the lowest variant:
    that's the original release and the one LEGO.com would be selling.
    """
    index = {}
    for s in sets:
        set_num = s["set_num"]
        base = set_num.split("-")[0]
        try:
            variant = int(set_num.split("-")[1])
        except (IndexError, ValueError):
            variant = 1
        existing = index.get(base)
        if existing is None or variant < existing[1]:
            index[base] = (set_num, variant)
    return {k: v[0] for k, v in index.items()}


def lego_retailer_id(client):
    resp = client.table("retailers").select("id").eq("slug", "lego").execute()
    if not resp.data:
        sys.exit("ERROR: no retailer with slug 'lego'. Did the schema seed run?")
    return resp.data[0]["id"]


def upsert_listings(client, rows):
    written = 0
    for i in range(0, len(rows), UPSERT_BATCH):
        batch = rows[i:i + UPSERT_BATCH]
        client.table("listings").upsert(batch, on_conflict="retailer_id,url").execute()
        written += len(batch)
        log(f"    wrote {written}/{len(rows)}")
    return written


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    log(f"BrickMargin LEGO.com discovery — version {VERSION}")
    log(f"Locale {LEGO_LOCALE}" + ("  [PROBE MODE — no writes]" if PROBE else ""))

    client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    sets = fetch_all_sets(client)
    if not sets:
        sys.exit("ERROR: no sets in the database. Run the catalog collector first.")
    index = build_index(sets)
    log(f"  {len(sets)} sets loaded, {len(index)} unique base numbers")

    # ---- walk the sitemaps ----
    queue = sitemaps_from_robots()
    seen_sitemaps = set()
    product_urls = {}       # base number -> url
    non_product = 0
    unmatched = {}

    while queue:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        try:
            xml_bytes = fetch_xml(sm_url)
        except Exception as exc:
            log(f"    {sm_url} failed: {exc}")
            continue

        children, pages = parse_sitemap(xml_bytes)
        if children:
            queue.extend(children)
            log(f"    index: {sm_url.split('/')[-1]} -> {len(children)} child sitemaps")
        if pages:
            hits = 0
            for url in pages:
                num = set_number_from_url(url)
                if not num:
                    non_product += 1
                    continue
                hits += 1
                if num in index:
                    product_urls.setdefault(num, url)
                else:
                    unmatched.setdefault(num, url)
            log(f"    pages: {sm_url.split('/')[-1]} -> {len(pages)} urls, "
                f"{hits} product pages")

            if PROBE and hits:
                log("      --- sample product URLs ---")
                shown = 0
                for url in pages:
                    num = set_number_from_url(url)
                    if num:
                        state = "MATCHED" if num in index else "no catalog entry"
                        log(f"      [{state:16}] {num:>8}  {url}")
                        shown += 1
                    if shown >= 6:
                        break

        time.sleep(SLEEP)
        if len(seen_sitemaps) > 60:
            log("    stopping: sitemap walk exceeded 60 files")
            break

    log("=" * 50)
    log(f"  sitemaps read     : {len(seen_sitemaps)}")
    log(f"  matched to catalog: {len(product_urls)}")
    log(f"  product pages with no catalog entry: {len(unmatched)}")
    log(f"  non-product urls skipped: {non_product}")

    if not product_urls:
        sys.exit("ERROR: no product URLs matched. The URL pattern likely changed — "
                 "check the sample output above.")

    if PROBE:
        log("PROBE complete. Nothing was written.")
        return

    retailer_id = lego_retailer_id(client)
    now = datetime.now(timezone.utc).isoformat()
    rows = [{
        "set_num": index[num],
        "retailer_id": retailer_id,
        "url": url,
        "sku": num,
        "active": True,
        "last_seen_at": now,
    } for num, url in product_urls.items()]

    written = upsert_listings(client, rows)
    log(f"Done. {written} LEGO.com listings ready to poll.")


if __name__ == "__main__":
    main()
