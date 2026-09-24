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

VERSION = "2.1-sitelook"

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


CAMPAIGN = os.environ.get("EBAY_CAMPAIGN_ID", "5339178457").strip()


def affiliate(url):
    """The same tracking the site uses, so an email click earns like any other."""
    if not url or not CAMPAIGN:
        return url or ""
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        u = urlparse(url); q = dict(parse_qsl(u.query))
        q.update({"mkevt": "1", "mkcid": "1", "mkrid": "711-53200-19255-0",
                  "siteid": "0", "campid": CAMPAIGN, "toolid": "10001", "customid": "alert"})
        return urlunparse(u._replace(query=urlencode(q)))
    except Exception:
        return url


def photo_for(item, values):
    """
    A photograph an inbox will actually load. Gmail fetches images through
    its own servers, and BrickLink's host refuses server requests, so its
    photos come through blank. A deal carries the eBay listing photo; anything
    else uses the catalogue image from Rebrickable, which serves anyone.
    """
    if item.get("image_url") and "bricklink" not in item["image_url"]:
        return item["image_url"]
    v = values.get(item.get("set_num")) or {}
    return v.get("catalog_image") or None


def build_html(name, items, site="https://www.brickmargin.com"):
    """
    An email that looks like the site it came from.

    Mail clients won't load Inter and ignore most CSS, so everything here is
    tables and inline styles — the one thing every client agrees on. The
    site's look survives that: a yellow block with a thick black border, a
    hard offset shadow (a black table peeking out behind), heavy type, and a
    coloured stripe along the top of each card.
    """
    FONT = "'Helvetica Neue',Helvetica,Arial,sans-serif"
    INK, YEL, RED = "#1A1A1A", "#FFD500", "#E3000B"
    stripes = ["#00A3E0", "#FF8DC7", "#4CBB17", "#FF8A00", "#8C4DFF", "#FFD500"]

    def shadowed(inner, bg="#ffffff", radius=18, pad=0):
        # A hard offset shadow, built from two nested tables: the black one
        # behind, shifted down and right, the coloured one in front.
        return f"""
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="border-collapse:separate">
  <tr><td style="padding:0 5px 5px 0;background:{INK};border-radius:{radius + 2}px">
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
           style="background:{bg};border:3px solid {INK};border-radius:{radius}px;border-collapse:separate">
      <tr><td style="padding:{pad}px">{inner}</td></tr>
    </table>
  </td></tr>
</table>"""

    cards = []
    for n, it in enumerate(items):
        stripe = it.get("colour") or stripes[n % len(stripes)]
        buy = it.get("buy_url")
        btn_bg, btn_fg, label = (RED, "#ffffff", "Open the listing on eBay &rarr;") if buy \
            else (YEL, INK, "See the set &rarr;")
        href = buy or f'{site}/set/{it["set_num"]}'
        button = f"""
<table cellpadding="0" cellspacing="0" role="presentation" style="border-collapse:separate">
  <tr><td style="padding:0 3px 3px 0;background:{INK};border-radius:999px">
    <a href="{href}" style="display:inline-block;background:{btn_bg};color:{btn_fg};font-family:{FONT};
       font-size:15px;font-weight:800;text-decoration:none;padding:12px 22px;border:2px solid {INK};
       border-radius:999px">{label}</a>
  </td></tr>
</table>"""
        photo = (f'<img src="{it["image_url"]}" width="112" height="112" alt="" '
                 f'style="display:block;width:112px;height:112px;object-fit:contain;background:#ffffff;'
                 f'border:2px solid {INK};border-radius:12px">') if it.get("image_url") else ""
        body = f"""
<table width="100%" cellpadding="0" cellspacing="0" role="presentation">
  <tr><td height="10" style="background:{stripe};border-radius:15px 15px 0 0;font-size:0;line-height:0">&nbsp;</td></tr>
  <tr><td style="padding:18px">
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
      {"<td width='128' valign='top' style='padding-right:16px'>" + photo + "</td>" if photo else ""}
      <td valign="top" style="font-family:{FONT};color:{INK}">
        <div style="display:inline-block;background:{YEL};border:2px solid {INK};border-radius:999px;
             padding:4px 11px;font-size:12px;font-weight:900;letter-spacing:.06em;text-transform:uppercase">
          {it.get("figure") or "Update"}</div>
        <div style="font-size:21px;font-weight:900;line-height:1.2;letter-spacing:-.02em;margin:10px 0 6px">
          {it["name"]}</div>
        <div style="font-size:15px;font-weight:600;line-height:1.5;margin:0 0 16px">{it["detail"]}</div>
        {button}
      </td>
    </tr></table>
  </td></tr>
</table>"""
        cards.append(f'<tr><td style="padding:0 0 20px">{shadowed(body)}</td></tr>')

    greeting = f"Hi {name}," if name else "Hi there,"
    header = f"""
<table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr>
  <td style="font-family:{FONT};color:{INK};padding:26px 28px">
    <table cellpadding="0" cellspacing="0" role="presentation"><tr>
      <td style="width:30px;height:30px;background:{YEL};border:3px solid {INK};border-radius:8px;
          font-size:0;line-height:0">&nbsp;</td>
      <td style="padding-left:10px;font-family:{FONT};font-size:19px;font-weight:900;letter-spacing:-.02em">
        BrickMargin</td>
    </tr></table>
    <div style="font-size:30px;font-weight:900;line-height:1.1;letter-spacing:-.03em;margin:18px 0 8px">
      {len(items)} update{"" if len(items) == 1 else "s"} on sets you watch</div>
    <div style="font-size:16px;font-weight:600;line-height:1.5">{greeting} here is what moved since we last wrote.</div>
  </td>
</tr></table>"""

    footer = f"""
<tr><td style="padding:8px 4px 0;font-family:{FONT};color:{INK}">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"><tr><td align="center"
      style="padding:18px 0;border-top:2px dashed #CFCFCB">
    <a href="{site}/watchlist" style="color:{INK};font-weight:800;font-size:14px;text-decoration:underline">Your watchlist</a>
    &nbsp;&nbsp;&middot;&nbsp;&nbsp;
    <a href="{site}/deals/mine" style="color:{INK};font-weight:800;font-size:14px;text-decoration:underline">Deals on your sets</a>
    &nbsp;&nbsp;&middot;&nbsp;&nbsp;
    <a href="{site}/settings" style="color:{INK};font-weight:600;font-size:14px;text-decoration:underline">Email settings</a>
    <div style="margin-top:14px;font-size:12px;line-height:1.6;font-weight:600;color:#4A4A4A">
      Every price includes shipping. eBay links are affiliate links &mdash; buying through them supports
      BrickMargin at no cost to you, and never changes what we show.
    </div>
  </td></tr></table>
</td></tr>"""

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>BrickMargin</title></head>
<body style="margin:0;padding:0;background:#F4F4F1">
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#F4F4F1">
<tr><td align="center" style="padding:28px 14px">
  <table width="600" cellpadding="0" cellspacing="0" role="presentation" style="max-width:600px;width:100%">
    <tr><td style="padding:0 0 26px">{shadowed(header, bg=YEL, radius=22)}</td></tr>
    {"".join(cards)}
    {footer}
  </table>
</td></tr></table>
</body></html>"""


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def usd(n):
    return f"${round(float(n)):,}"


def as_utc(value):
    """
    Parse a Postgres timestamp or date into an aware UTC datetime.

    retired_at is a DATE — "2018-12-05" — so fromisoformat returns a naive
    datetime, and subtracting an aware now() raises TypeError. Timestamps
    from timestamptz columns arrive aware and with a trailing Z that older
    Pythons won't parse. Both cases funnel through here so no caller has to
    know which kind of column it's holding.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


def build_email(name, items, values=None):
    """
    The email people receive.

    This used to be a plain grey list with no pictures and no way to buy —
    and a redesigned template sat beside it, never called. Now the text
    version is kept for clients that want it, and the HTML is the site's own
    look: photos, coloured cards, and a button straight to the listing.
    """
    values = values or {}
    lines = []
    for it in items:
        it["image_url"] = photo_for(it, values)
        lines.append(f"- {it['name']}: {it['detail']} ({it['figure']})\n"
                     f"  {it.get('buy_url') or SITE_URL + '/set/' + it['set_num']}")
    html = build_html(name, items, site=SITE_URL)
    text = (f"Hi {name or 'there'},\n\n" + "\n\n".join(lines) +
            f"\n\nYour watchlist: {SITE_URL}/watchlist\nEmail settings: {SITE_URL}/settings\n")
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
               .select("user_id,set_num,notify_email,target_drop_pct,target_price")
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
                .select("id,email,username,alert_email,alert_price,alert_discount,alert_drop_pct,notify_all")
                .in_("id", user_ids).execute()).data or []}

    values = {v["set_num"]: v for v in (client.table("set_values")
              .select("set_num,name,market_value,comp_count,msrp,catalog_image")
              .in_("set_num", set_nums).execute()).data or []}

    retiring = {}
    for s in (client.table("sets").select("set_num,retired_at")
              .in_("set_num", set_nums).execute()).data or []:
        if s.get("retired_at"):
            retiring[s["set_num"]] = s["retired_at"]

    deals = {}
    for d in (client.table("live_deals")
              .select("set_num,total_price,deal_type,pct_off_msrp,discount_pct,item_url,image_url")
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
        # Per-set and per-person switches. A watch with email off, or a
        # person who turned everything off, never generates an email.
        if w.get("notify_email") is False:
            continue
        prof = profiles.get(w["user_id"]) or {}
        if prof.get("notify_all") is False:
            continue
        by_user.setdefault(w["user_id"], []).append(w["set_num"])

    sent_rows, emails = [], 0

    for uid, nums in by_user.items():
        prof = profiles.get(uid) or {}
        email = (prof.get("email") or "").strip()
        if not email or not prof.get("alert_email", True):
            continue

        threshold = float(w.get("target_drop_pct") or prof.get("alert_drop_pct") or 5)
        w_by_set = {x["set_num"]: x for x in watches if x["user_id"] == uid}
        items = []

        for set_num in nums:
            val = values.get(set_num)
            if not val:
                continue
            name = val.get("name") or set_num

            # --- reached a target price ("tell me when it hits $400") ---
            tp = w_by_set.get(set_num, {}).get("target_price")
            cur = float(val.get("market_value") or 0)
            if tp is not None and cur and cur <= float(tp):
                key = (uid, set_num, "target_price")
                if key not in recent:
                    items.append({
                        "set_num": set_num, "name": name,
                        "detail": f"Now {usd(cur)} · your target was {usd(float(tp))}",
                        "figure": "Target hit", "colour": "#0b6b3a",
                    })
                    sent_rows.append({"user_id": uid, "set_num": set_num, "kind": "target_price",
                                      "detail": f"{usd(cur)} reached target {usd(float(tp))}"})

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
                        "colour": "#E3000B",
                        # straight to the listing, through the campaign, so an
                        # alert that says "it's cheap" is one tap from buying
                        "buy_url": affiliate(d.get("item_url")),
                        "image_url": (d.get("image_url") or "").replace("/s-l225.", "/s-l500."),
                    })
                    sent_rows.append({
                        "user_id": uid, "set_num": set_num, "kind": "deal",
                        "detail": off, "value_at": round(float(d["total_price"]), 2)})
                    continue

            # --- leaving production ---
            iso = retiring.get(set_num)
            retire_dt = as_utc(iso)
            if retire_dt:
                days = (retire_dt - datetime.now(timezone.utc)).days
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
        who = (prof.get("username") or "").strip() or None
        html, text = build_email(who, items, values)

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
