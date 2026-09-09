#!/usr/bin/env python3
"""
BrickMargin · weekly digest
===========================

One email a week to everyone with a watchlist: every watched set, what it's
worth now, and how it moved. Sent whether or not anything triggered — the
alert emails write when something happens; this one keeps the site in mind
when nothing does, which is when people forget it exists.

Honours the same switches as alerts: profiles.notify_all, and a person with
no active watches gets nothing.

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY, RESEND_API_KEY
             FROM_EMAIL (default alerts@brickmargin.com), PROBE (default true)
"""
import os, sys
from datetime import datetime, timedelta, timezone
import requests
from supabase import create_client

SB = os.environ.get("SUPABASE_URL", "").strip(); KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
RESEND = os.environ.get("RESEND_API_KEY", "").strip()
FROM = os.environ.get("FROM_EMAIL") or "BrickMargin <alerts@brickmargin.com>"
PROBE = os.environ.get("PROBE", "true").strip().lower() != "false"
SITE = "https://www.brickmargin.com"
if not (SB and KEY): sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY required")
def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)
usd = lambda n: f"${round(float(n)):,}"

client = create_client(SB, KEY)
watches = (client.table("watches").select("user_id,set_num").eq("active", True).execute()).data or []
by_user = {}
for w in watches: by_user.setdefault(w["user_id"], set()).add(w["set_num"])
log(f"{len(by_user)} people with watchlists")

profiles = {p["id"]: p for p in ((client.table("profiles").select("id,notify_all").execute()).data or [])}
nums = sorted({n for s in by_user.values() for n in s})
vals = {v["set_num"]: v for v in ((client.table("set_values")
        .select("set_num,name,market_value,msrp,comp_count,sold_new,retired_at").in_("set_num", nums).execute()).data or [])}
week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
old = {}
for c in ((client.table("comps").select("set_num,median_sold,observed_at").in_("set_num", nums)
           .lte("observed_at", week).order("observed_at", desc=True).execute()).data or []):
    old.setdefault(c["set_num"], c["median_sold"])
deals = {d["set_num"]: d for d in ((client.table("live_deals").select("set_num,total_price,item_url,pct_off_msrp,discount_pct")
         .in_("set_num", nums).execute()).data or [])}

sent = 0
for uid, sets in by_user.items():
    prof = profiles.get(uid) or {}
    if prof.get("notify_all") is False: continue
    u = client.auth.admin.get_user_by_id(uid)
    email = getattr(getattr(u, "user", None), "email", None)
    if not email: continue
    rows = []
    for n in sorted(sets):
        v = vals.get(n)
        if not v or not v.get("market_value"): continue
        now_v = float(v["market_value"]); was = old.get(n)
        chg = f"{'+' if now_v >= was else ''}{round((now_v/float(was)-1)*100)}%" if was else "new"
        d = deals.get(n)
        deal = f' &nbsp;·&nbsp; <a href="{d["item_url"]}" style="color:#E3000B;font-weight:700">on sale at {usd(d["total_price"])}</a>' if d else ""
        rows.append(f'<tr><td style="padding:10px 0;border-bottom:1px solid #eee"><a href="{SITE}/set/{n}" style="color:#1A1A1A;font-weight:700;text-decoration:none">{v["name"]}</a>'
                    f'<div style="font-size:13px;color:#1A1A1A">{usd(now_v)} &nbsp;·&nbsp; {chg} this week &nbsp;·&nbsp; {v.get("comp_count") or 0} listings{deal}</div></td></tr>')
    if not rows: continue
    html = f'''<div style="font-family:Inter,Arial,sans-serif;max-width:560px;margin:0 auto;color:#1A1A1A">
<div style="background:#FFD500;border:3px solid #1A1A1A;border-radius:18px;padding:22px 24px;margin-bottom:18px">
<div style="font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase">Your week on BrickMargin</div>
<div style="font-size:24px;font-weight:900;margin-top:6px">{len(rows)} watched set{"s" if len(rows)!=1 else ""}, checked every day.</div></div>
<table style="width:100%;border-collapse:collapse">{''.join(rows)}</table>
<p style="font-size:13px;margin-top:22px"><a href="{SITE}/watchlist" style="color:#1A1A1A;font-weight:700">Open your watchlist</a> &nbsp;·&nbsp;
<a href="{SITE}/settings" style="color:#1A1A1A">Email settings</a></p></div>'''
    if PROBE:
        log(f"  would send to {email}: {len(rows)} sets"); sent += 1; continue
    r = requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND}"},
                      json={"from": FROM, "to": [email], "subject": f"Your {len(rows)} watched sets this week", "html": html}, timeout=30)
    if r.status_code < 300: sent += 1
    else: log(f"  {email}: HTTP {r.status_code} {r.text[:80]}")
log(f"{'would send' if PROBE else 'sent'} {sent} digests")
