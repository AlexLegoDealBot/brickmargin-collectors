#!/usr/bin/env python3
"""
BrickMargin · alert sender
==========================

Emails people when something happens to a set they're watching.

Three things count as news:

  price_drop — the value fell by more than their threshold since we last
               told them anything about that set.
  deal       — a live listing appeared below market or below retail.
  retiring   — the set leaves production within 60 days.

Everything else is noise, and noise is how you lose an inbox. Three rules
enforce that:

  * One email per person per run, containing every set that moved. Nobody
    wants six separate emails.
  * Nothing repeats inside a cooling-off window, so a set that stays cheap
    for a week is mentioned once, not forty times.
  * A move is only real if it clears the same sanity bounds the site uses —
    at least 5 listings behind both readings, and never more than 25%, which
    is always a data artefact rather than a market event.

Environment
-----------
  SUPABASE_URL, SUPABASE_SERVICE_KEY   required
  RESEND_API_KEY                       required
  ALERT_FROM      default 'BrickMargin <alerts@brickmargin.com>'
  SITE_URL        default 'https://www.brickmargin.com'
  COOLDOWN_HOURS  default 72
  MAX_PER_EMAIL   default 12
  PROBE           default true — print the emails, send nothing
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

VERSION = "1.1-preflight"

MIN_SAMPLE = 5          # listings behind a reading, matching lib/movement.ts
MAX_MOVE_PCT = 25       # above this it's an artefact, not a price move
RETIRE_DAYS = 60


def require(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: {name} is required")
    return v


SUPABASE_URL = require("SUPABASE_URL")
SUPABASE_KEY = require("SUPABASE_SERVICE_KEY")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
if not RESEND_KEY and os.environ.get("PROBE", "true").strip().lower() == "false":
    sys.exit("ERROR: RESEND_API_KEY is required to send. Add it to the "
             "collectors repo: Settings -> Secrets and variables -> Actions. "
             "(Probe mode runs without it.)")
ALERT_FROM = os.environ.get("ALERT_FROM") or "BrickMargin <alerts@brickmargin.com>"
SITE_URL = (os.environ.get("SITE_URL") or "https://www.brickmargin.com").rstrip("/")
COOLDOWN_HOURS = int(os.environ.get("COOLDOWN_HOURS") or 72)
MAX_PER_EMAIL = int(os.environ.get("MAX_PER_EMAIL") or 12)
PROBE = os.environ.get("PROBE", "true").strip().lower() != "false"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def usd(n):
    return f"${round(float(n)):,}"


def send_email(to, subject, html, text):
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_KEY}",
                 "Content-Type": "application/json"},
        json={"from": ALERT_FROM, "to": [to], "subject": subject,
              "html": html, "text": text},
        timeout=30,
    )
    if resp.status_code >= 300:
        log(f"    send failed {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def build_email(name, items):
    """
    Plain, quiet, and legible on a phone. No images, no marketing furniture —
    somebody who asked to be told a price changed wants the price.
    """
    rows = []
    lines = []
    for it in items:
        rows.append(f"""
        <tr>
          <td style="padding:14px 0;border-bottom:1px solid #e4e4e0;">
            <a href="{SITE_URL}/set/{it['set_num']}"
               style="color:#0f1826;text-decoration:none;font-weight:700;font-size:16px;">
              {it['name']}</a>
            <div style="color:#4a4038;font-size:13px;margin-top:4px;">
              {it['detail']}
            </div>
          </td>
          <td align="right" style="padding:14px 0;border-bottom:1px solid #e4e4e0;
              font-family:ui-monospace,Menlo,monospace;font-size:17px;
              color:{it['colour']};white-space:nowrap;">
            {it['figure']}
          </td>
        </tr>""")
        lines.append(f"- {it['name']}: {it['detail']} ({it['figure']})\n"
                     f"  {SITE_URL}/set/{it['set_num']}")

    html = f"""<!doctype html><html><body style="margin:0;padding:24px;
background:#f8f4ec;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;">
<div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e4e4e0;
     border-radius:14px;padding:28px;">
  <p style="margin:0 0 4px;font-size:11px;letter-spacing:.16em;text-transform:uppercase;
     color:#6b5a48;font-weight:700;">BrickMargin</p>
  <h1 style="margin:0 0 6px;font-size:22px;color:#0f1826;">
    {len(items)} set{'s' if len(items) != 1 else ''} you're watching changed</h1>
  <p style="margin:0 0 18px;color:#4a4038;font-size:14px;line-height:1.5;">
    Measured from live listings. Values are median asking prices, so treat
    them as a ceiling rather than a sale price.</p>
  <table width="100%" cellpadding="0" cellspacing="0">{''.join(rows)}</table>
  <p style="margin:22px 0 0;font-size:13px;">
    <a href="{SITE_URL}/watchlist" style="color:#0b6b3a;font-weight:700;
       text-decoration:none;">Open your watchlist &rarr;</a></p>
  <p style="margin:20px 0 0;padding-top:16px;border-top:1px solid #e4e4e0;
     color:#6b5a48;font-size:12px;line-height:1.5;">
    You're getting this because you saved these sets on BrickMargin.
    <a href="{SITE_URL}/settings" style="color:#6b5a48;">Turn alerts off</a>
    any time. Independent price research; not affiliated with the LEGO Group.</p>
</div></body></html>"""

    text = (f"{len(items)} set(s) you're watching changed\n\n"
            + "\n".join(lines)
            + f"\n\nYour watchlist: {SITE_URL}/watchlist"
              f"\nTurn alerts off: {SITE_URL}/settings\n")
    return html, text


def preflight(client):
    """
    Check every table and column this run depends on, and say plainly which
    one is missing.

    The first version assumed the schema was in place and crashed with a raw
    PostgREST traceback if it wasn't — which tells you a request failed but
    not which migration you skipped. A scheduled job that fails silently for
    weeks is worse than one that never ran, so this names the problem and the
    fix in one line.
    """
    problems = []

    checks = [
        ("watches", "user_id,set_num,active", "migration_007_auth.sql"),
        ("profiles", "id,email,alert_email,alert_price,alert_discount,alert_drop_pct",
         "migration_012_alerts.sql (adds alert_drop_pct)"),
        ("alerts_sent", "id,user_id,set_num,kind,sent_at", "migration_012_alerts.sql"),
        ("set_values", "set_num,name,market_value,comp_count,msrp", "migration_003"),
        ("live_deals", "set_num,total_price,deal_type", "migration_011_retail_deals.sql"),
        ("comps", "set_num,observed_at,median_sold,comp_count", "migration_001"),
    ]
    for table, cols, fix in checks:
        try:
            client.table(table).select(cols).limit(1).execute()
        except Exception as exc:
            problems.append(f"{table}: {str(exc)[:150]}  → run {fix}")

    return problems


def main():
    log(f"BrickMargin alert sender — version {VERSION}  probe={PROBE}")
    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    problems = preflight(client)
    if problems:
        log("  PREFLIGHT FAILED — the schema isn\u2019t ready for alerts:")
        for pr in problems:
            log(f"    {pr}")
        sys.exit("Fix the above, then re-run. Nothing was sent.")
    log("  preflight ok — all tables and columns present")

    watches = (client.table("watches")
               .select("user_id,set_num")
               .eq("active", True)
               .not_.is_("set_num", "null")
               .execute()).data or []
    if not watches:
        log("  nobody is watching anything yet")
        return

    user_ids = sorted({w["user_id"] for w in watches})
    set_nums = sorted({w["set_num"] for w in watches})
    log(f"  {len(watches)} watches · {len(user_ids)} people · {len(set_nums)} sets")

    profiles = {p["id"]: p for p in (client.table("profiles")
                .select("id,email,alert_email,alert_price,alert_discount,alert_drop_pct")
                .in_("id", user_ids).execute()).data or []}

    values = {v["set_num"]: v for v in (client.table("set_values")
              .select("set_num,name,market_value,comp_count,msrp")
              .in_("set_num", set_nums).execute()).data or []}

    retiring = {}
    for s in (client.table("sets").select("set_num,retired_at")
              .in_("set_num", set_nums).execute()).data or []:
        if s.get("retired_at"):
            retiring[s["set_num"]] = s["retired_at"]

    deals = {}
    for d in (client.table("live_deals")
              .select("set_num,total_price,deal_type,pct_off_msrp,discount_pct")
              .in_("set_num", set_nums).execute()).data or []:
        prior = deals.get(d["set_num"])
        if not prior or d["total_price"] < prior["total_price"]:
            deals[d["set_num"]] = d

    # Price history, newest first, thin readings discarded.
    since = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
    history = {}
    for c in (client.table("comps")
              .select("set_num,observed_at,median_sold,comp_count")
              .in_("set_num", set_nums)
              .gte("observed_at", since)
              .not_.is_("median_sold", "null")
              .order("observed_at", desc=True)
              .limit(6000).execute()).data or []:
        if (c.get("comp_count") or 0) < MIN_SAMPLE:
            continue
        history.setdefault(c["set_num"], []).append(c)

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    recent = set()
    for a in (client.table("alerts_sent")
              .select("user_id,set_num,kind")
              .gte("sent_at", cutoff).execute()).data or []:
        recent.add((a["user_id"], a["set_num"], a["kind"]))

    by_user = {}
    for w in watches:
        by_user.setdefault(w["user_id"], []).append(w["set_num"])

    sent_rows, emails = [], 0

    for uid, nums in by_user.items():
        prof = profiles.get(uid) or {}
        email = (prof.get("email") or "").strip()
        if not email or not prof.get("alert_email", True):
            continue

        threshold = float(prof.get("alert_drop_pct") or 5)
        items = []

        for set_num in nums:
            val = values.get(set_num)
            if not val:
                continue
            name = val.get("name") or set_num

            # --- price move ---
            if prof.get("alert_price", True):
                rows = history.get(set_num, [])
                if len(rows) >= 2:
                    now_v = float(rows[0]["median_sold"])
                    was_v = float(rows[-1]["median_sold"])
                    if was_v > 0:
                        pct = (now_v - was_v) / was_v * 100
                        if abs(pct) >= threshold and abs(pct) <= MAX_MOVE_PCT:
                            key = (uid, set_num, "price_drop")
                            if key not in recent:
                                down = pct < 0
                                items.append({
                                    "set_num": set_num, "name": name,
                                    "detail": (f"{'Fell' if down else 'Rose'} from "
                                               f"{usd(was_v)} · {rows[0].get('comp_count')} listings"),
                                    "figure": f"{'▼' if down else '▲'} {abs(pct):.1f}%",
                                    "colour": "#a8342a" if down else "#0b6b3a",
                                })
                                sent_rows.append({
                                    "user_id": uid, "set_num": set_num,
                                    "kind": "price_drop",
                                    "detail": f"{pct:.1f}% to {usd(now_v)}",
                                    "value_at": round(now_v, 2)})
                                continue

            # --- a listing worth seeing ---
            if prof.get("alert_discount", True) and set_num in deals:
                key = (uid, set_num, "deal")
                if key not in recent:
                    d = deals[set_num]
                    off = (f"{round(d['pct_off_msrp'])}% under retail"
                           if d.get("pct_off_msrp") else
                           f"{round(d['discount_pct'])}% under market")
                    items.append({
                        "set_num": set_num, "name": name,
                        "detail": f"Listed now · {off}",
                        "figure": usd(d["total_price"]),
                        "colour": "#0b6b3a",
                    })
                    sent_rows.append({
                        "user_id": uid, "set_num": set_num, "kind": "deal",
                        "detail": off, "value_at": round(float(d["total_price"]), 2)})
                    continue

            # --- leaving production ---
            iso = retiring.get(set_num)
            if iso:
                days = (datetime.fromisoformat(iso.replace("Z", "+00:00"))
                        - datetime.now(timezone.utc)).days
                if 0 < days <= RETIRE_DAYS:
                    key = (uid, set_num, "retiring")
                    if key not in recent:
                        items.append({
                            "set_num": set_num, "name": name,
                            "detail": f"Leaves production in about {days} days",
                            "figure": usd(val.get("market_value") or 0),
                            "colour": "#9a6b12",
                        })
                        sent_rows.append({
                            "user_id": uid, "set_num": set_num, "kind": "retiring",
                            "detail": f"{days} days", "value_at": None})

        if not items:
            continue
        items = items[:MAX_PER_EMAIL]

        subject = (f"{items[0]['name']} {items[0]['figure']}"
                   if len(items) == 1
                   else f"{len(items)} sets you're watching changed")
        html, text = build_email(prof.get("email"), items)

        if PROBE:
            log(f"  WOULD SEND to {email}: {subject}")
            for it in items:
                log(f"      {it['name']} — {it['detail']} ({it['figure']})")
        else:
            if send_email(email, subject, html, text):
                emails += 1
                log(f"  sent to {email}: {subject}")

    if PROBE:
        log(f"  PROBE — {len(sent_rows)} alerts would go out, nothing sent or logged")
        return

    # Log what actually went out. If this fails we\u2019d re-send the same
    # alerts next run, so it\u2019s worth shouting about — but the emails are
    # already delivered, so failing the job here helps nobody.
    if sent_rows:
        try:
            for i in range(0, len(sent_rows), 200):
                client.table("alerts_sent").insert(sent_rows[i:i + 200]).execute()
        except Exception as exc:
            log(f"  WARNING: emails sent but the log write failed: {exc}")
            log("  the same alerts may repeat next run")
    log(f"  {emails} emails sent · {len(sent_rows)} alerts logged")
    log("Done.")


if __name__ == "__main__":
    main()
