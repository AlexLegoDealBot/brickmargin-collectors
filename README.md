# BrickMargin Collectors

The data engine. These scripts fill the Supabase database the website reads from.

**Collector #1 — catalog** ✅ live — 6,678 sets
**Collector #3 — eBay comps** ✅ live — resale prices
Collector #2 — LEGO.com prices *(next)*

Built #3 before #2 deliberately: eBay keys already work, it's an official API
rather than scraping, and it writes straight to `comps` keyed on `set_num`
without needing the `listings` table populated first.

---

## Files

```
brickmargin-collectors/
├── collect_catalog.py
├── collect_comps.py
├── migration_002_comps.sql
├── requirements.txt
├── README.md
└── .github/
    └── workflows/
        ├── catalog.yml
        └── comps.yml
```

`catalog.yml` **must** be at `.github/workflows/catalog.yml`. Anywhere else and
GitHub won't see the workflow at all.

---

## Secrets

**Settings → Secrets and variables → Actions.** Names are case-sensitive.

| Secret | Required | Source |
|---|---|---|
| `SUPABASE_URL` | ✅ | Supabase → Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY` | ✅ | Supabase → Settings → API → **service_role** key |
| `REBRICKABLE_API_KEY` | ✅ | rebrickable.com → Settings → API |
| `BRICKSET_API_KEY` | ⬜ optional | brickset.com/tools/webservices/requestkey |
| `EBAY_APP_ID` | ✅ | developer.ebay.com → your app → **Production** Client ID |
| `EBAY_CERT_ID` | ✅ | developer.ebay.com → your app → **Production** Client Secret |

Sandbox eBay keys return no real listings. Production only.

Secrets are write-only — you can't read them back, and they're masked in logs.
That's the correct home for `service_role`, which bypasses every database
security rule.

---

## Running it

**Actions** tab → **Catalog Collector** (left sidebar) → **Run workflow**
(right side) → **Run workflow**.

⚠️ **Never use the "Re-run jobs" button on an old run.** Re-running checks out
the same commit as the original, so it replays old code and ignores anything
you've committed since. Always start a fresh run from the Run workflow button.

**Probe mode** (checked by default) prints sample data and writes nothing.
Use it after any code change to confirm the shape of what's coming back before
loading thousands of rows.

The first log line prints the script version. If it doesn't match the `VERSION`
value at the top of `collect_catalog.py`, GitHub ran stale code.

---

## Two sources, one required

**Rebrickable — required.** Set numbers, names, themes, subthemes, years, piece
counts, images. The catalog skeleton. Keys are issued instantly.

**Brickset — optional.** MSRP, minifig counts, age ratings, retirement dates.
The script validates the key with a single call up front; if it fails, it logs
one line and skips enrichment. **The catalog still loads.** Keys are approved
manually and can take days.

Why both: Rebrickable is a parts database with no retail price, and without MSRP
there's no "percent off." Brickset has the commercial fields. Neither replaces
the other.

When your Brickset key starts working, update that one secret and re-run. MSRP
and retirement dates flow into rows that already exist. No code changes.

Also note the two sources disagree on piece counts — Rebrickable counts its own
parts inventory, Brickset prints the box number. Brickset's value wins because
it's the one collectors recognize.

---

## Troubleshooting

| Log says | Meaning |
|---|---|
| `missing environment variable X` | Secret name misspelled or absent |
| `Rebrickable rejected the key (401)` | Bad `REBRICKABLE_API_KEY` |
| `Brickset: key INVALID — skipping` | Expected for now. Not an error. |
| Old wording, no `version` line | Stale code — see the Re-run warning above |
| No workflow in Actions tab | `catalog.yml` at the wrong path |

Re-running is always safe. Sets update in place, new ones get added, nothing
duplicates. A run that dies halfway costs nothing but time.

Test a key without touching code:

```
https://brickset.com/api/v3.asmx/checkKey?apiKey=YOUR_KEY
https://rebrickable.com/api/v3/lego/sets/?key=YOUR_KEY&page_size=1
```

---

## Schedule

Runs weekly, Mondays 09:00 UTC, full load with no probe. Set metadata barely
changes, so weekly is plenty. Prices are a different story — that's collector #2.


---

## Collector #3 — eBay comps

Builds the resale side of the margin calculation.

### These are asking prices, not sold prices

eBay's sold data lives behind Marketplace Insights, a limited-release API that
small developers are routinely denied. So we use the Browse API, which returns
**active listings** — what sellers hope to get, not what buyers paid. Asking
prices run high.

They're stored raw and honestly under `source='ebay_active'`. The haircut gets
applied in the scoring layer where it's visible and tunable, rather than baked
invisibly into stored data. When BrickLink seller access or Marketplace
Insights comes through, real sold data lands under a different `source` and
takes precedence. Nothing here gets wasted.

`sold_count_30d` and `sell_through` are deliberately left NULL — active
listings carry no sold volume, and inventing numbers there would corrupt the
margin math downstream.

### Data quality

Two gates decide whether a listing counts. The set number must appear in the
title — the strongest signal we have that it's the right product. And the title
must avoid the junk list: instructions, empty boxes, custom builds, display
cases, light kits, parts lots.

Then a two-pass outlier trim: preliminary median, drop anything below 35% or
above 250% of it, recompute. eBay is full of fantasy pricing, and one $5,000
listing on an $850 set used to inflate `spread` from $67 to $2,127 — which
mattered because `spread` is the confidence signal. Now a tight cluster with
one absurd listing reads as high-confidence, while sellers genuinely
disagreeing reads as low.

Sets with fewer than `MIN_LISTINGS` survivors record nothing rather than a
misleading number.

**Condition contamination is the real enemy.** eBay's `conditions:{NEW}` filter
is self-reported and routinely wrong. The first live run kept a Ferris Wheel
listed at "Complete Set Displayed Only" and two Ferrari F40s at "100%
COMPLETE + Box Included" — all used sets, inside a sealed median. So the junk
list also excludes used-set vocabulary: `100% complete` (sealed sets say
NISB), `bagged` (loose polybags, no box), `displayed`, `pre-owned`, `opened`,
`missing`.

That created a second problem: "never opened" and "unopened" contain "opened"
but mean the set IS sealed. The `NEGATIONS` map handles those, so the filter
doesn't discard the listings it should trust most. There's an 18-case
regression suite behind this logic.

**`STRICT_SEALED`** flips the gate from "no bad words" to "must positively say
sealed / NISB / factory sealed / unopened". Much tighter, but it also drops
honest listings whose title just says "New". Toggle it in the workflow inputs
and compare medians on sets you've actually sold — the right setting is an
empirical question, not a design one.

### Rotation

eBay allows 5,000 Browse calls/day, application-wide. With ~6,700 sets there's
no full daily refresh, so each run takes the stalest batch —
`comps_last_checked_at` ascending, never-checked first. Four runs a day at 400
sets cycles the catalog roughly every 4 days.

Sets are stamped as checked even when eBay returns nothing, so dead sets don't
clog the front of the queue forever.

Run `migration_002_comps.sql` in Supabase before the first comps run — it adds
that column and its index.

### Verify

```sql
-- Most recent comps, richest first
select set_num, median_sold, comp_count, spread, observed_at
from comps order by observed_at desc limit 20;

-- Rotation progress
select count(*) filter (where comps_last_checked_at is null) as never_checked,
       count(*) filter (where comps_last_checked_at is not null) as checked
from sets;
```

Sanity check the numbers against sets you know. If a median looks wrong for a
set you've actually sold, tell me — your domain knowledge is the only real
validation this data gets.
