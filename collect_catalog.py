#!/usr/bin/env python3
"""
BrickMargin — Catalog Collector
================================
Loads the LEGO set catalog into Supabase. Collector #1 of 3.

TWO SOURCES, ONE REQUIRED
  Rebrickable (required) -> set numbers, names, themes, subthemes, years,
                            piece counts, images. The backbone.
  Brickset (optional)    -> MSRP, minifig counts, age ratings, retirement
                            dates. Runs only if a working key is present.
                            A missing or invalid Brickset key logs a warning
                            and is skipped — it never fails the run.

WHY THIS SPLIT
  Rebrickable has no retail price, and MSRP is what makes "percent off"
  possible. But MSRP for sets currently on sale also comes free with the
  LEGO.com price collector (collector #2), so Brickset is really only
  essential for retired sets' historical MSRP. Not worth blocking on.

ENVIRONMENT VARIABLES
  SUPABASE_URL           required
  SUPABASE_SERVICE_KEY   required — the service_role key
  REBRICKABLE_API_KEY    required
  BRICKSET_API_KEY       optional — skipped if absent or invalid
  MIN_YEAR               optional — default 2015
  MAX_YEAR               optional — default next year
  PROBE                  optional — set to 1 to print samples, write nothing
"""

import json
import os
import sys
import time
from datetime import date, datetime, timezone

import requests
from supabase import create_client

# Bump this on every edit. It prints in the log so you can confirm at a
# glance which version of the file actually ran.
VERSION = "2.1-filtered"

REBRICKABLE_BASE = "https://rebrickable.com/api/v3/lego"
BRICKSET_BASE = "https://brickset.com/api/v3.asmx"

PAGE_SIZE = 1000
BRICKSET_PAGE_SIZE = 500
SLEEP = 0.6
UPSERT_BATCH = 250


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
REBRICKABLE_API_KEY = require("REBRICKABLE_API_KEY")
BRICKSET_API_KEY = os.environ.get("BRICKSET_API_KEY", "").strip()

# Rebrickable's catalog is a completist parts database: it includes books,
# magazines, keychains and gear alongside real sets. A deal tracker wants
# buildable sets that retailers actually discount, so we filter.
#   MIN_PARTS      — drop anything under this piece count (books have 1)
#   EXCLUDED_ROOTS — theme trees that are definitionally not buildable sets
MIN_PARTS = int_env("MIN_PARTS", 20)
EXCLUDED_ROOTS = {"Books", "Gear"}

MIN_YEAR = int_env("MIN_YEAR", 2015)
MAX_YEAR = int_env("MAX_YEAR", date.today().year + 1)
PROBE = os.environ.get("PROBE", "").strip().lower() in ("1", "true", "yes")


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def positive_int(value):
    """Sources use 0 to mean 'unknown' for pieces and minifigs. So do we -> None."""
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Rebrickable
# ----------------------------------------------------------------------

def rb_get(path, params=None):
    headers = {"Authorization": f"key {REBRICKABLE_API_KEY}"}
    resp = requests.get(f"{REBRICKABLE_BASE}{path}", headers=headers,
                        params=params or {}, timeout=45)
    if resp.status_code in (401, 403):
        sys.exit(f"ERROR: Rebrickable rejected the key ({resp.status_code}). "
                 "Check REBRICKABLE_API_KEY.")
    resp.raise_for_status()
    return resp.json()


def fetch_themes():
    """
    Rebrickable models subthemes as themes with a parent.
    We flatten: root ancestor becomes `theme`, the immediate theme becomes
    `subtheme` (when they differ).
    """
    raw = {}
    page = 1
    while True:
        data = rb_get("/themes/", {"page": page, "page_size": PAGE_SIZE})
        for t in data.get("results", []):
            raw[t["id"]] = {"name": t.get("name"), "parent_id": t.get("parent_id")}
        if not data.get("next"):
            break
        page += 1
        time.sleep(SLEEP)

    resolved = {}
    for tid in raw:
        # Walk up to the root, guarding against loops in the data.
        chain = []
        current, hops = tid, 0
        while current is not None and hops < 12:
            node = raw.get(current)
            if not node:
                break
            chain.append(node["name"])
            current = node["parent_id"]
            hops += 1

        root = chain[-1] if chain else None
        leaf = chain[0] if chain else None
        resolved[tid] = {
            "theme": root,
            "subtheme": leaf if (leaf and leaf != root) else None,
        }

    log(f"  themes resolved: {len(resolved)}")
    return resolved


def transform_rb(s, themes):
    """
    Returns (row, None) on success, or (None, reason) when filtered out.
    The reason strings feed the summary counts so the filters are visible
    rather than silently discarding data.
    """
    set_num = (s.get("set_num") or "").strip()
    if not set_num:
        return None, "no set_num"

    theme_info = themes.get(s.get("theme_id"), {})
    root = theme_info.get("theme")

    if root in EXCLUDED_ROOTS:
        return None, f"theme:{root}"

    pieces = positive_int(s.get("num_parts"))
    if pieces is None or pieces < MIN_PARTS:
        return None, "too few pieces"

    return {
        "set_num": set_num,
        "name": (s.get("name") or "Unknown").strip(),
        "theme": theme_info.get("theme"),
        "subtheme": theme_info.get("subtheme"),
        "year_released": positive_int(s.get("year")),
        "pieces": pieces,
        "image_url": s.get("set_img_url"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, None


# ----------------------------------------------------------------------
# Brickset (optional enrichment)
# ----------------------------------------------------------------------

def brickset_key_works():
    """Validate before looping, so a bad key costs one call instead of twelve."""
    if not BRICKSET_API_KEY:
        log("  Brickset: no key set — skipping enrichment")
        return False
    try:
        resp = requests.get(f"{BRICKSET_BASE}/checkKey",
                            params={"apiKey": BRICKSET_API_KEY}, timeout=30)
        ok = resp.json().get("status") == "success"
    except Exception as exc:
        log(f"  Brickset: key check failed ({exc}) — skipping enrichment")
        return False

    log("  Brickset: key valid" if ok else
        "  Brickset: key INVALID — skipping enrichment (catalog still loads)")
    return ok


def clean_date(value):
    if not value or not isinstance(value, str):
        return None
    candidate = value[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
        return candidate
    except ValueError:
        return None


def fetch_brickset_year(year, page_number):
    params = {
        "apiKey": BRICKSET_API_KEY,
        "userHash": "",
        "params": json.dumps({"year": str(year),
                              "pageSize": BRICKSET_PAGE_SIZE,
                              "pageNumber": page_number}),
    }
    resp = requests.get(f"{BRICKSET_BASE}/getSets", params=params, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Brickset: {data.get('message')}")
    return data.get("sets", []), data.get("matches", 0)


def transform_brickset(s):
    """Only the fields Rebrickable can't give us."""
    number = str(s.get("number") or "").strip()
    if not number:
        return None
    set_num = f"{number}-{s.get('numberVariant') or 1}"

    us = (s.get("LEGOCom") or {}).get("US") or {}
    msrp = us.get("retailPrice")
    if isinstance(msrp, (int, float)) and msrp <= 0:
        msrp = None

    last_available = clean_date(us.get("dateLastAvailable"))
    retired_at = (last_available
                  if last_available and last_available < date.today().isoformat()
                  else None)

    minifigs = positive_int(s.get("minifigs"))
    age_min = positive_int((s.get("ageRange") or {}).get("min"))

    row = {"set_num": set_num}
    if msrp is not None:
        row["msrp"] = msrp
    if minifigs is not None:
        row["minifigs"] = minifigs
    if age_min is not None:
        row["age_min"] = age_min
    if retired_at:
        row["retired_at"] = retired_at

    # Nothing worth writing if only the key is present.
    return row if len(row) > 1 else None


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

def upsert(client, rows, label):
    written = 0
    for i in range(0, len(rows), UPSERT_BATCH):
        batch = rows[i:i + UPSERT_BATCH]
        client.table("sets").upsert(batch, on_conflict="set_num").execute()
        written += len(batch)
    log(f"    {label}: wrote {written}")
    return written


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    log(f"BrickMargin catalog collector starting — version {VERSION}")
    log(f"Years {MIN_YEAR}-{MAX_YEAR}" + ("  [PROBE MODE — no writes]" if PROBE else ""))

    client = None if PROBE else create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    # ---- Phase A: Rebrickable (required) ----
    log("Phase A — Rebrickable catalog")
    themes = fetch_themes()

    rb_rows = {}
    dropped = {}
    page = 1
    while True:
        data = rb_get("/sets/", {
            "min_year": MIN_YEAR, "max_year": MAX_YEAR,
            "page": page, "page_size": PAGE_SIZE, "ordering": "set_num",
        })
        results = data.get("results", [])

        if page == 1:
            log(f"  {data.get('count', 0)} sets reported for {MIN_YEAR}-{MAX_YEAR}")
            if PROBE and results:
                # Show a record that survives the filters, not just the
                # alphabetically-first one (which is usually a book).
                sample = None
                for s in results:
                    row, _ = transform_rb(s, themes)
                    if row:
                        sample = (s, row)
                        break
                if sample:
                    log("  --- raw Rebrickable record (post-filter) ---")
                    print(json.dumps(sample[0], indent=2), flush=True)
                    log("  --- our transformed row ---")
                    print(json.dumps(sample[1], indent=2), flush=True)

        for s in results:
            row, reason = transform_rb(s, themes)
            if row:
                rb_rows[row["set_num"]] = row
            else:
                dropped[reason] = dropped.get(reason, 0) + 1

        log(f"  page {page}: {len(rb_rows)} kept, {sum(dropped.values())} filtered")
        if not data.get("next"):
            break
        page += 1
        time.sleep(SLEEP)

    log(f"  FILTERS: kept {len(rb_rows)}, dropped {sum(dropped.values())}")
    for reason, count in sorted(dropped.items(), key=lambda x: -x[1]):
        log(f"    {count:6}  {reason}")
    log(f"  (MIN_PARTS={MIN_PARTS}, excluded themes: {', '.join(sorted(EXCLUDED_ROOTS))})")

    if not PROBE:
        upsert(client, list(rb_rows.values()), "catalog")

    # ---- Phase B: Brickset (optional) ----
    log("Phase B — Brickset enrichment (MSRP, minifigs, retirement)")
    enriched = 0

    if brickset_key_works():
        for year in range(MIN_YEAR, MAX_YEAR + 1):
            page = 1
            year_rows = {}
            stop_year = False

            while True:
                try:
                    sets, matches = fetch_brickset_year(year, page)
                except Exception as exc:
                    log(f"  {year} page {page} failed: {exc}")
                    break
                if page == 1 and matches == 0:
                    break

                if PROBE and sets:
                    log(f"  --- sample Brickset enrichment ({year}) ---")
                    print(json.dumps(transform_brickset(sets[0]), indent=2), flush=True)
                    stop_year = True
                    break

                for s in sets:
                    row = transform_brickset(s)
                    # Only enrich sets Rebrickable already gave us, so we
                    # never insert a half-empty row.
                    if row and row["set_num"] in rb_rows:
                        year_rows[row["set_num"]] = row

                if len(sets) < BRICKSET_PAGE_SIZE:
                    break
                page += 1
                time.sleep(SLEEP)

            if year_rows and not PROBE:
                enriched += upsert(client, list(year_rows.values()), f"{year} enrich")
            if stop_year:
                break
            time.sleep(SLEEP)

    log("=" * 50)
    if PROBE:
        log("PROBE complete. Nothing was written.")
        return

    log(f"Done. Catalog: {len(rb_rows)} sets. Enriched: {enriched}.")
    if not rb_rows:
        sys.exit("ERROR: no sets collected — check the log above")


if __name__ == "__main__":
    main()
