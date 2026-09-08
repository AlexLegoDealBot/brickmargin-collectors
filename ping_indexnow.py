#!/usr/bin/env python3
"""
Tell search engines what changed, right after we change it.

Run at the end of a collector. It reads which sets were re-priced recently
and pushes those URLs to IndexNow via the site's own endpoint. Bing acts on
these within minutes rather than waiting to crawl, and Bing's index is what
ChatGPT search reads — which is where a growing share of "what's this LEGO
set worth" now gets asked.

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY, INDEXNOW_SECRET
             SITE_URL (default https://www.brickmargin.com)
             HOURS    how far back to look, default 6
"""
import os, sys
from datetime import datetime, timedelta, timezone
import requests
from supabase import create_client

SB = os.environ.get("SUPABASE_URL", "").strip()
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
SECRET = os.environ.get("INDEXNOW_SECRET", "").strip()
SITE = (os.environ.get("SITE_URL") or "https://www.brickmargin.com").rstrip("/")
HOURS = int(os.environ.get("HOURS") or 6)
if not (SB and KEY and SECRET):
    sys.exit("ERROR: SUPABASE_URL, SUPABASE_SERVICE_KEY and INDEXNOW_SECRET are required")

client = create_client(SB, KEY)
since = (datetime.now(timezone.utc) - timedelta(hours=HOURS)).isoformat()
rows = (client.table("comps").select("set_num").gte("observed_at", since).limit(9000).execute()).data or []
paths = sorted({f"/set/{r['set_num']}" for r in rows})
print(f"{len(paths)} sets changed in the last {HOURS}h")
if not paths:
    sys.exit(0)
r = requests.post(f"{SITE}/api/indexnow", json={"paths": paths},
                  headers={"x-indexnow-secret": SECRET, "Content-Type": "application/json"}, timeout=30)
print(f"submitted: HTTP {r.status_code} {r.text[:120]}")
