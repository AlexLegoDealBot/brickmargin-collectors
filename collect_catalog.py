#!/usr/bin/env python3
"""
BrickMargin — Catalog Collector
================================
Loads the LEGO set catalog into Supabase. This is collector #1 of 3.

WHAT IT DOES
  Asks Brickset for every set released in a range of years, converts each one
  into a row that matches our `sets` table, and upserts it. Re-running is safe:
  existing sets get updated, new ones get added, nothing gets duplicated.

WHY BRICKSET AND NOT REBRICKABLE
  Rebrickable has great part data but no MSRP. Brickset gives us US retail
  price, minifig count, subtheme, and a retirement date signal in one call.
  MSRP is non-negotiable for us: without it there is no "percent off."

ENVIRONMENT VARIABLES (set these in Railway)
  SUPABASE_URL           required — from Supabase → Settings → API
  SUPABASE_SERVICE_KEY   required — the service_role key, NOT the anon key
  BRICKSET_API_KEY       required — from brickset.com/tools/webservices/v3
  MIN_YEAR               optional — default 2015
  MAX_YEAR               optional — default next year
  PROBE                  optional — set to 1 to print raw data and write NOTHING

RUN IT IN PROBE MODE FIRST. It prints what Brickset actually returns so we can
confirm the field names before touching the database.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timezone

import requests
from supabase import create_client

BRICKSET_URL = "https://brickset.com/api/v3.asmx/getSets"
PAGE_SIZE = 500          # Brickset's maximum per page
SLEEP_BETWEEN_CALLS = 1.0  # be a polite API citizen
UPSERT_BATCH = 250       # rows per database write


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

def require(name):
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"ERROR: missing environment variable {name}")
    return val


SUPABASE_URL = require("SUPABASE_URL")
SUPABASE_SERVICE_KEY = require("SUPABASE_SERVICE_KEY")
BRICKSET_API_KEY = require("BRICKSET_API_KEY")

MIN_YEAR = int(os.environ.get("MIN_YEAR", "2015"))
MAX_YEAR = int(os.environ.get("MAX_YEAR", str(date.today().year + 1)))
PROBE = os.environ.get("PROBE", "").strip().lower() in ("1", "true", "yes")


def log(msg):
    """Timestamped output so Railway's log view is readable."""
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# ----------------------------------------------------------------------
# Brickset
# ----------------------------------------------------------------------

def fetch_page(year, page_number):
    """
    Ask Brickset for one page of sets from one year.
    Returns (list_of_sets, total_match_count).
    """
    params = {
        "apiKey": BRICKSET_API_KEY,
        "userHash": "",  # empty is fine for public set data
        "params": json.dumps({
            "year": str(year),
            "pageSize": PAGE_SIZE,
            "pageNumber": page_number,
        }),
    }

    resp = requests.get(BRICKSET_URL, params=params, timeout=45)
    resp.raise_for_status()
    data = resp.json()

    status = data.get("status")
    if status != "success":
        # Brickset reports key problems and rate limits here, not via HTTP codes.
        raise RuntimeError(f"Brickset said: {status} — {data.get('message')}")

    return data.get("sets", []), data.get("matches", 0)


# ----------------------------------------------------------------------
# Transform: Brickset's shape -> our `sets` table
# ----------------------------------------------------------------------

def clean_date(value):
    """Brickset dates arrive as '2020-12-31T00:00:00Z'. We want '2020-12-31'."""
    if not value or not isinstance(value, str):
        return None
    candidate = value[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
        return candidate
    except ValueError:
        return None


def positive_int(value):
    """Brickset uses 0 to mean 'unknown' for pieces and minifigs. So do we -> None."""
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def transform(s):
    """
    Convert one Brickset set into one row for our table.
    Returns None if the record is unusable.
    """
    number = str(s.get("number") or "").strip()
    if not number:
        return None

    # Brickset splits '75192-1' into number=75192, numberVariant=1.
    # Rebrickable uses the joined form, so we match that for future compatibility.
    variant = s.get("numberVariant") or 1
    set_num = f"{number}-{variant}"

    us = (s.get("LEGOCom") or {}).get("US") or {}
    msrp = us.get("retailPrice")
    if isinstance(msrp, (int, float)) and msrp <= 0:
        msrp = None

    # A past dateLastAvailable is our best retirement signal from this source.
    last_available = clean_date(us.get("dateLastAvailable"))
    retired_at = None
    if last_available and last_available < date.today().isoformat():
        retired_at = last_available

    age_range = s.get("ageRange") or {}

    return {
        "set_num": set_num,
        "name": (s.get("name") or "Unknown").strip(),
        "theme": s.get("theme"),
        "subtheme": s.get("subtheme"),
        "year_released": positive_int(s.get("year")),
        "pieces": positive_int(s.get("pieces")),
        "minifigs": positive_int(s.get("minifigs")),
        "msrp": msrp,
        "age_min": positive_int(age_range.get("min")),
        "retired_at": retired_at,
        "image_url": (s.get("image") or {}).get("imageURL"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

def upsert_sets(client, rows):
    """Write rows in batches. on_conflict tells Postgres to update, not duplicate."""
    written = 0
    for i in range(0, len(rows), UPSERT_BATCH):
        batch = rows[i:i + UPSERT_BATCH]
        client.table("sets").upsert(batch, on_conflict="set_num").execute()
        written += len(batch)
        log(f"    wrote {written}/{len(rows)}")
    return written


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    log("BrickMargin catalog collector starting")
    log(f"Years {MIN_YEAR}–{MAX_YEAR}" + ("  [PROBE MODE — no writes]" if PROBE else ""))

    client = None
    if not PROBE:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    total_seen = 0
    total_written = 0
    skipped = 0

    for year in range(MIN_YEAR, MAX_YEAR + 1):
        page = 1
        year_rows = []

        while True:
            try:
                sets, matches = fetch_page(year, page)
            except Exception as exc:
                log(f"  {year} page {page} FAILED: {exc}")
                break

            if page == 1:
                log(f"  {year}: {matches} sets reported")
                if matches == 0:
                    break

            # PROBE MODE: dump one real record and stop. This lets us verify
            # Brickset's actual field names before we trust the transform.
            if PROBE and sets:
                log("  --- raw Brickset record ---")
                print(json.dumps(sets[0], indent=2)[:2500], flush=True)
                log("  --- our transformed row ---")
                print(json.dumps(transform(sets[0]), indent=2), flush=True)
                log("PROBE complete. Nothing was written.")
                return

            for s in sets:
                total_seen += 1
                row = transform(s)
                if row:
                    year_rows.append(row)
                else:
                    skipped += 1

            if len(sets) < PAGE_SIZE:
                break
            page += 1
            time.sleep(SLEEP_BETWEEN_CALLS)

        if year_rows:
            # Deduplicate within the year — a repeated set_num in one batch
            # would make Postgres reject the whole upsert.
            unique = {r["set_num"]: r for r in year_rows}
            log(f"  {year}: upserting {len(unique)} sets")
            total_written += upsert_sets(client, list(unique.values()))

        time.sleep(SLEEP_BETWEEN_CALLS)

    log("=" * 50)
    log(f"Done. Seen {total_seen}, written {total_written}, skipped {skipped}")

    if total_written == 0:
        sys.exit("ERROR: nothing was written — check the log above")


if __name__ == "__main__":
    main()
