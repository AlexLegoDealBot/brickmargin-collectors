# BrickMargin collectors

Everything that feeds brickmargin.com. Nine Python scripts on GitHub Actions
writing to one Supabase database; the site only ever reads.

## What each one does

| Script | Version | What it does | Schedule |
|---|---|---|---|
| `watch_new.py` | 1.7-sealedonly | **The fast lane.** One broad query for eBay's newest LEGO listings every 90s, matched against the catalogue in memory. Finds a deal ~90 seconds after it is listed and posts it to Discord immediately. | chained, continuous |
| `collect_deals.py` | 4.2-sealedonly | **The thorough lane.** One eBay query per set. Slower, but catches listings whose titles omit the set number. | 6×/day |
| `collect_comps.py` | 2.6-resilient | Sealed asking prices — the median behind every set value, plus up to 12 listings per set. | 8×/day, 400 sets |
| `collect_bricklink.py` | 2.1-partout | Real completed sale prices, set photographs, and part-out values for sets over $60. | 4×/day, 400 sets |
| `collect_minifigs.py` | 3.1-resilient | Walks set inventories on BrickLink to build the figure catalogue, then prices it. | manual until complete |
| `send_alerts.py` | 2.0-branded | Emails people when a watched set drops, hits their target, or appears on the deals board. | daily |
| `send_digest.py` | 1.0 | One email a week: every watched set, what it is worth, how it moved. | Sundays |
| `post_discord.py` | 2.1-channels | Posts board deals to Discord. The fast lane posts its own. | after each deals run |
| `ping_indexnow.py` | 1.0 | Tells Bing which pages changed. | after each comps run |
| `watchdog.py` | — | Checks the data is still sane and shouts if it is not. | daily |

## Conventions that matter

**Sealed only on the deals board.** A used listing's condition is whatever
the seller typed. Used prices still appear on set pages, measured from
BrickLink completed sales where a grade means something. `ALLOW_USED=true`
overrides.

**Descriptions are read, not just titles.** Two listings once reached the
board that no title filter could have caught — loose bags sold as a set, and
an "escape pod only" listing. Every surviving listing now has its description
scanned for disqualifying phrases.

**In-production sets are judged against retail; retired sets against resale.**
A set still on shelves is only a deal if it beats the shop price.

**Every link out carries the affiliate campaign**, tagged by channel
(`deal`, `alert`, `digest`, `discord`, `fig`) so EPN reporting shows which
one converts.

**Collectors are resumable.** Every run stamps what it checked and takes the
stalest first, so a crash costs time, not data. Comps flushes every 50 sets.

## Secrets this repo expects

```
SUPABASE_URL              SUPABASE_SERVICE_KEY
EBAY_APP_ID               EBAY_CERT_ID
BRICKLINK_CONSUMER_KEY    BRICKLINK_CONSUMER_SECRET
BRICKLINK_TOKEN           BRICKLINK_TOKEN_SECRET
REBRICKABLE_API_KEY       REBRICKABLE_KEY
RESEND_API_KEY            INDEXNOW_SECRET
DISCORD_WEBHOOK_RETAIL    DISCORD_WEBHOOK_MARKET
```

## Migrations

`migrations/` holds every schema change in order. They have all been applied;
they are kept as the record of how the database got its shape.
