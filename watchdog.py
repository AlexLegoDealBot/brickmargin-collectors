#!/usr/bin/env python3
"""
BrickMargin · watchdog
======================

Answers one question every morning: is anything quietly broken?

Twice now a pipeline has failed and sat failing for a fortnight — the comps
collector crashed on a missing import and the "priced right now" figure froze,
and nobody knew until it was noticed by eye. Collectors already log their own
failures, but a log nobody reads is not a warning. This is the thing that
reads them.

It checks freshness (did each collector actually write recently), data sanity
(are the numbers plausible), and whether the site is even up. Then it emails
ONLY if something is wrong. A watchdog that emails every day when all is well
teaches you to ignore it, and then it has cost you the thing it was for.

Environment
-----------
  SUPABASE_URL, SUPABASE_SERVICE_KEY   required
  RESEND_API_KEY                       required to send (probe runs without)
  ALERT_TO         where to send warnings — required to send
  ALERT_FROM       default 'BrickMargin <alerts@brickmargin.com>'
  SITE_URL         default 'https://www.brickmargin.com'
  ALWAYS_SEND      'true' to email even when healthy (a weekly all-clear)
  PROBE            default true — print the report, send nothing
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from supabase import create_client

VERSION = "1.0"


def require(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"ERROR: {name} is required")
    return v


SUPABASE_URL = require("SUPABASE_URL")
SUPABASE_KEY = require("SUPABASE_SERVICE_KEY")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
ALERT_TO = os.environ.get("ALERT_TO", "").strip()
ALERT_FROM = os.environ.get("ALERT_FROM") or "BrickMargin <alerts@brickmargin.com>"
SITE_URL = (os.environ.get("SITE_URL") or "https://www.brickmargin.com").rstrip("/")
ALWAYS_SEND = os.environ.get("ALWAYS_SEND", "false").strip().lower() == "true"
PROBE = os.environ.get("PROBE", "true").strip().lower() != "false"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def as_utc(value):
    """Parse a Postgres date or timestamp into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_since(value):
    dt = as_utc(value)
    if not dt:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


class Report:
    """Findings, split by how loudly they need saying."""

    def __init__(self):
        self.broken = []    # something is failing right now
        self.warn = []      # not failing, but heading that way
        self.ok = []        # checked and healthy — shown for context

    def fail(self, title, detail, fix):
        self.broken.append({"title": title, "detail": detail, "fix": fix})

    def caution(self, title, detail, fix):
        self.warn.append({"title": title, "detail": detail, "fix": fix})

    def good(self, title, detail):
        self.ok.append({"title": title, "detail": detail})

    @property
    def healthy(self):
        return not self.broken and not self.warn


def newest(client, table, column, extra=None):
    """The most recent value of a timestamp column, or None if the table is empty."""
    try:
        q = client.table(table).select(column).order(column, desc=True).limit(1)
        if extra:
            q = extra(q)
        rows = q.execute().data or []
        return rows[0][column] if rows else None
    except Exception as exc:
        raise RuntimeError(f"{table}.{column}: {str(exc)[:120]}") from exc


def count(client, table, build=None):
    q = client.table(table).select("*", count="exact", head=True)
    if build:
        q = build(q)
    return q.execute().count or 0


def check_freshness(client, r):
    """
    Did each collector actually write recently?

    The thresholds are deliberately generous — roughly triple each job's
    schedule — so a single skipped run doesn't cry wolf, but a genuinely
    stalled pipeline is caught within a day.
    """
    checks = [
        # table,   column,        every,   warn_h, broken_h, what it means
        ("comps", "observed_at", "8× daily", 12, 30,
         "Comps Collector — set values stop updating and the site goes stale"),
        ("deals", "last_seen", "6× daily", 10, 26,
         "Deals Collector — the deals board empties out"),
        ("sets", "updated_at", "weekly", 200, 400,
         "Catalog Collector — no new sets, no MSRP, no retirement dates"),
    ]

    for table, column, cadence, warn_h, broken_h, meaning in checks:
        try:
            latest = newest(client, table, column)
        except RuntimeError as exc:
            r.fail(f"{table} unreadable", str(exc),
                   "Check the Supabase project is up and the service key is valid.")
            continue

        if latest is None:
            r.fail(f"{table} is empty", f"No rows at all in {table}.",
                   f"Run the collector that fills it ({cadence}).")
            continue

        age = hours_since(latest)
        if age is None:
            r.caution(f"{table} timestamp unreadable", f"Could not parse {latest}.",
                      "Probably harmless, but worth a look.")
        elif age > broken_h:
            r.fail(f"{table} has not been written in {age:.0f} hours",
                   f"Expected every {cadence}. {meaning}.",
                   "Open GitHub Actions and look for a red run. The first log "
                   "line gives the collector version that ran.")
        elif age > warn_h:
            r.caution(f"{table} is {age:.0f} hours old",
                      f"Expected every {cadence}. Not broken yet.",
                      "Worth checking the last run finished cleanly.")
        else:
            r.good(table, f"written {age:.1f} hours ago")


def check_data(client, r):
    """Are the numbers themselves plausible?"""
    try:
        tracked = count(client, "sets")
        priced = count(client, "set_values")
        deals_live = count(client, "live_deals")
    except Exception as exc:
        r.fail("Cannot count rows", str(exc)[:160],
               "Check the Supabase service key and that the views exist.")
        return

    r.good("catalogue", f"{tracked:,} sets tracked · {priced:,} priced")

    if tracked and priced / tracked < 0.5:
        r.caution("Fewer than half the catalogue is priced",
                  f"{priced:,} of {tracked:,}.",
                  "Normal while the comps rotation catches up; a problem if "
                  "it doesn't improve over a week.")

    if deals_live == 0:
        r.caution("The deals board is empty",
                  "No live rows in live_deals.",
                  "Either genuinely nothing qualifies, or the deals collector "
                  "is rejecting everything. Run it in probe mode and read the "
                  "rejection tally at the bottom.")
    else:
        r.good("deals board", f"{deals_live} live listings")

    # Values that can't be right. A sealed set over $50k is a data error, and
    # a non-positive price is always one.
    try:
        absurd = count(client, "set_values",
                       lambda q: q.gt("market_value", 50000))
        if absurd:
            r.fail(f"{absurd} sets priced above $50,000",
                   "Almost certainly wrong-product listings in the sample.",
                   "Check the comps filters — this is the signature of a "
                   "listing that isn't the set.")
    except Exception:
        pass

    try:
        bad_deals = count(client, "deals",
                          lambda q: q.is_("gone_at", "null").lte("total_price", 0))
        if bad_deals:
            r.fail(f"{bad_deals} live deals priced at or below zero",
                   "These would render as free on the board.",
                   "Check the deals collector's price parsing.")
    except Exception:
        pass


def check_alerts(client, r):
    """Alerts are allowed to be quiet — but not silent forever."""
    try:
        latest = newest(client, "alerts_sent", "sent_at")
    except RuntimeError:
        r.caution("alerts_sent unreadable",
                  "The alert log table may be missing.",
                  "Run migration_012_alerts.sql.")
        return

    watchers = count(client, "watches", lambda q: q.eq("active", True))
    if watchers == 0:
        r.good("alerts", "nobody is watching anything yet — nothing to send")
        return

    if latest is None:
        r.good("alerts", f"{watchers} watches, nothing has qualified yet")
        return

    days = (hours_since(latest) or 0) / 24
    r.good("alerts", f"{watchers} watches · last sent {days:.1f} days ago")


def check_site(r):
    """Is the site actually up? A failed deploy is invisible from the database."""
    try:
        resp = requests.get(SITE_URL, timeout=20,
                            headers={"User-Agent": "BrickMargin-watchdog"})
        if resp.status_code >= 500:
            r.fail(f"The site returned {resp.status_code}",
                   f"{SITE_URL} is erroring.",
                   "Check the latest Vercel deployment for a build failure.")
        elif resp.status_code >= 400:
            r.caution(f"The site returned {resp.status_code}",
                      f"{SITE_URL} responded oddly.", "Worth a look.")
        else:
            r.good("site", f"{SITE_URL} responded {resp.status_code}")
    except Exception as exc:
        r.fail("The site did not respond", str(exc)[:160],
               "Check Vercel — this usually means a failed deploy or an "
               "expired domain.")


def build_email(r):
    def block(items, colour, heading):
        if not items:
            return ""
        rows = "".join(f"""
          <tr><td style="padding:12px 0;border-bottom:1px solid #e4e4e0;">
            <div style="font-weight:700;color:{colour};font-size:15px;">{i['title']}</div>
            <div style="color:#4a4038;font-size:13.5px;margin-top:4px;line-height:1.5;">
              {i['detail']}</div>
            <div style="color:#6b5a48;font-size:12.5px;margin-top:6px;line-height:1.5;">
              → {i['fix']}</div>
          </td></tr>""" for i in items)
        return (f'<p style="margin:22px 0 4px;font-size:11px;letter-spacing:.14em;'
                f'text-transform:uppercase;color:#6b5a48;font-weight:700;">{heading}</p>'
                f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>')

    okline = " · ".join(f"{i['title']}: {i['detail']}" for i in r.ok)
    html = f"""<!doctype html><html><body style="margin:0;padding:24px;
background:#f8f4ec;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;">
<div style="max-width:580px;margin:0 auto;background:#fff;border:1px solid #e4e4e0;
     border-radius:14px;padding:28px;">
  <p style="margin:0 0 4px;font-size:11px;letter-spacing:.16em;text-transform:uppercase;
     color:#6b5a48;font-weight:700;">BrickMargin · daily check</p>
  <h1 style="margin:0 0 6px;font-size:21px;color:#0f1826;">
    {"Everything looks healthy" if r.healthy
      else f"{len(r.broken)} broken, {len(r.warn)} worth watching"}</h1>
  {block(r.broken, '#a8342a', 'Broken')}
  {block(r.warn, '#9a6b12', 'Worth watching')}
  <p style="margin:22px 0 0;padding-top:16px;border-top:1px solid #e4e4e0;
     color:#6b5a48;font-size:12px;line-height:1.6;">{okline}</p>
</div></body></html>"""

    lines = []
    for label, items in (("BROKEN", r.broken), ("WORTH WATCHING", r.warn)):
        if items:
            lines.append(label)
            for i in items:
                lines.append(f"  {i['title']}\n    {i['detail']}\n    -> {i['fix']}")
    lines.append("")
    lines.append(okline)
    return html, "\n".join(lines)


def main():
    log(f"BrickMargin watchdog — version {VERSION}  probe={PROBE}")
    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    r = Report()

    check_freshness(client, r)
    check_data(client, r)
    check_alerts(client, r)
    check_site(r)

    for i in r.broken:
        log(f"  BROKEN   {i['title']} — {i['detail']}")
    for i in r.warn:
        log(f"  WARNING  {i['title']} — {i['detail']}")
    for i in r.ok:
        log(f"  ok       {i['title']}: {i['detail']}")

    if r.healthy and not ALWAYS_SEND:
        log("  all healthy — no email sent (that's the point)")
        return

    html, text = build_email(r)
    subject = ("BrickMargin: all healthy" if r.healthy
               else f"BrickMargin: {len(r.broken)} broken, {len(r.warn)} warnings")

    if PROBE or not RESEND_KEY or not ALERT_TO:
        if not PROBE:
            log("  cannot send: RESEND_API_KEY or ALERT_TO is missing")
        log(f"  WOULD SEND: {subject}")
        log(text)
        return

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_KEY}",
                 "Content-Type": "application/json"},
        json={"from": ALERT_FROM, "to": [ALERT_TO], "subject": subject,
              "html": html, "text": text},
        timeout=30)
    if resp.status_code >= 300:
        log(f"  send failed {resp.status_code}: {resp.text[:200]}")
        sys.exit(1)
    log(f"  emailed {ALERT_TO}: {subject}")

    # A broken pipeline should also turn the Actions run red, so the failure
    # is visible in GitHub as well as in the inbox.
    if r.broken:
        sys.exit(1)


if __name__ == "__main__":
    main()
