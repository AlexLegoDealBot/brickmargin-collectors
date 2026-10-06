"""
BrickMargin — LEGO news for Discord.

Reads the news feeds of the big LEGO sites and posts each new article to
#lego-news: the site's name, the headline, and the link. Discord turns the
link into the publisher's own preview card, so we never copy their words or
pictures — readers click through to the original.

Rules it keeps:
  * Nothing is posted twice (every article is remembered in news_posts).
  * The first time a feed is seen, its existing articles are recorded
    quietly rather than dumped into the channel.
  * Only articles from the last 24 hours, and at most MAX_PER_RUN per run,
    so a busy news day never floods the channel.
  * One broken feed never stops the others.

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY, DISCORD_WEBHOOK_NEWS
Optional:    NEWS_FEEDS ("Name|url;Name|url"), MAX_PER_RUN (default 6)
"""
import os
import sys
import time
import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client

VERSION = "1.0"

SB = os.environ.get("SUPABASE_URL", "").strip()
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
HOOK = (os.environ.get("DISCORD_WEBHOOK_NEWS") or "").strip()
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN") or 6)

DEFAULT_FEEDS = [
    ("The Brothers Brick", "https://www.brothers-brick.com/feed/"),
    ("Brick Fanatics", "https://www.brickfanatics.com/feed/"),
    ("Jay's Brick Blog", "https://jaysbrickblog.com/feed/"),
    ("The Brick Fan", "https://www.thebrickfan.com/feed/"),
]


def feeds():
    raw = (os.environ.get("NEWS_FEEDS") or "").strip()
    if not raw:
        return DEFAULT_FEEDS
    out = []
    for part in raw.split(";"):
        if "|" in part:
            name, url = part.split("|", 1)
            out.append((name.strip(), url.strip()))
    return out or DEFAULT_FEEDS


def log(msg):
    print(msg, flush=True)


def parse(xml_text):
    """RSS 2.0 or Atom → [(guid, title, link, published_utc_or_None)]."""
    root = ET.fromstring(xml_text)
    items = []
    for it in root.iter("item"):                                   # RSS
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        when = None
        raw = it.findtext("pubDate")
        if raw:
            try:
                when = email.utils.parsedate_to_datetime(raw).astimezone(timezone.utc)
            except Exception:
                when = None
        if title and link:
            items.append((guid, title, link, when))
    if items:
        return items
    ns = {"a": "http://www.w3.org/2005/Atom"}                       # Atom
    for e in root.findall("a:entry", ns):
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        link_el = e.find("a:link", ns)
        link = (link_el.get("href") if link_el is not None else "") or ""
        guid = (e.findtext("a:id", default="", namespaces=ns) or link).strip()
        when = None
        raw = e.findtext("a:published", default="", namespaces=ns) or e.findtext("a:updated", default="", namespaces=ns)
        if raw:
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                when = None
        if title and link:
            items.append((guid, title, link, when))
    return items


def main():
    if not (SB and KEY):
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY required")
    log(f"BrickMargin news — version {VERSION} · webhook={'set' if HOOK else 'MISSING'}")
    if not HOOK:
        log("  DISCORD_WEBHOOK_NEWS is not set — nothing to post to. Add the secret to switch news on.")
        return
    client = create_client(SB, KEY)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    posted = 0
    ua = {"User-Agent": "BrickMarginNews/1.0 (+https://www.brickmargin.com)"}

    for source, url in feeds():
        try:
            r = requests.get(url, headers=ua, timeout=20)
            r.raise_for_status()
            items = parse(r.text)
        except Exception as e:
            log(f"  {source}: feed failed ({type(e).__name__}) — skipped")
            continue

        known = {row["guid"] for row in (client.table("news_posts").select("guid")
                 .eq("source", source).execute().data or [])}
        first_sight = not known
        fresh = [i for i in items if i[0] not in known]
        if first_sight:
            # Record the back catalogue quietly; only articles from now on get posted.
            if fresh:
                client.table("news_posts").upsert(
                    [{"guid": g, "source": source, "title": t[:300], "link": l, "posted": False} for g, t, l, _ in fresh],
                    on_conflict="guid").execute()
            log(f"  {source}: first run — {len(fresh)} existing articles recorded, none posted")
            continue

        # oldest first, so the channel reads in order
        fresh.sort(key=lambda i: i[3] or datetime.now(timezone.utc))
        for guid, title, link, when in fresh:
            recent = when is None or when >= since
            if recent and posted >= MAX_PER_RUN:
                continue              # over this run's cap: leave it for the next run
            send = recent
            if send:
                body = {"content": f"📰 **{source}** · {title}\n{link}",
                        "username": "BrickMargin News",
                        "avatar_url": "https://www.brickmargin.com/brickmargin-round-512.png",
                        "allowed_mentions": {"parse": []}}
                resp = requests.post(HOOK, json=body, timeout=20)
                if resp.status_code >= 300:
                    log(f"  {source}: Discord HTTP {resp.status_code} — will retry next run")
                    continue
                posted += 1
                time.sleep(1.5)
            client.table("news_posts").upsert(
                {"guid": guid, "source": source, "title": title[:300], "link": link, "posted": send},
                on_conflict="guid").execute()
            if send:
                log(f"  posted · {source} · {title[:80]}")
    log(f"done — {posted} article(s) posted")


if __name__ == "__main__":
    main()
