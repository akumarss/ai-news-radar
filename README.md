# AI news radar

A static dashboard that collects GenAI, LLM and ML news from research labs,
Hacker News, arXiv, Reddit and Bluesky, collapses duplicate coverage of the
same story into one card, ranks what's left, and republishes itself every
three hours. Runs entirely on GitHub Actions and GitHub Pages — no server, no
cost.

## Why these sources

The announcements that are easiest to miss — a new OpenAI framework, a Meta
research release, an Azure tool — are published on company blogs that all
expose free RSS. Social platforms mostly *link* to those pages rather than
originate them, which matters because social APIs have largely closed:

| Source | Access |
|---|---|
| Vendor and lab RSS | Free, no auth. The primary spine |
| Hacker News (Algolia) | Free, no auth, no key |
| arXiv | Free, no auth |
| Reddit | Free for non-commercial use, 100 QPM, OAuth app needed |
| Bluesky | Free; post search needs a session from an app password |
| X / Twitter | No free tier since Feb 2026 — pay-per-use only. Not used |
| LinkedIn | No post-search API outside the partner program. Not used |

Skipping X and LinkedIn costs almost nothing in coverage, because the
underlying announcement arrives through RSS first anyway.

## Setup

1. Create a **public** repo (public repos get unmetered Actions minutes) and
   push these files to `main`.
2. Settings → Pages → Source: *Deploy from a branch*, branch `main`, folder
   `/docs`.
3. Actions → *Update AI news radar* → **Run workflow** for a first run.
4. Open `https://<username>.github.io/<repo>/`.

The repo ships with sample data so the layout is visible before the first
real run. A banner marks it as sample; the first collection overwrites it.

### Optional: Reddit

Reddit is skipped silently unless credentials exist. To enable it, create an
app at <https://www.reddit.com/prefs/apps> (type: *script*), then add repo
secrets `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`. Add
`REDDIT_USERNAME` and `REDDIT_PASSWORD` too if application-only auth gets
rejected. New OAuth apps currently need manual approval, which can take a
few weeks.

### Optional: Bluesky

Create an app password at Settings → App Passwords (never your account
password), then add secrets `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`.

## Running locally

On macOS use `python3` — there is no bare `python` on a stock system.

```bash
pip3 install -r requirements.txt
python3 test_pipeline.py         # offline tests, no network needed
python3 collect.py --check-feeds # diagnose every RSS feed
python3 collect.py --dry-run     # fetch and rank, write nothing
python3 collect.py               # write docs/data/*.json
python3 -m http.server -d docs 8000
```

Then open <http://localhost:8000>. Opening `docs/index.html` directly by
double-clicking will *not* work: on `file://` the page's `fetch()` for
`data/items.json` is blocked by CORS and you get the empty state. It has to
be served over HTTP.

Note that `--dry-run` deliberately writes nothing, so the dashboard won't
change after it. Run `collect.py` without flags to actually update the page.

### Feeds rot — check them periodically

Vendor blogs rename and retire RSS paths without notice. `--check-feeds`
separates a dead URL from a live feed that simply hasn't published lately:

```
FEED                   ENTRIES  NEWEST       STATUS
------------------------------------------------------------------
OpenAI                      32  2026-08-07   ok
Anthropic                   15  2026-05-22   stale — newest is 80d old
Sebastian Raschka            8  2026-06-30   ok
```

Anthropic, Mistral and Cohere publish no official feed. Anthropic is wired
to a community mirror that has lagged before; their launches reliably reach
Hacker News regardless, so coverage survives either way.

## How ranking works

No LLM in the loop — scoring is deterministic, so the same inputs always
produce the same feed and you can reason about why something ranked where it
did.

```
score = (relevance + engagement + corroboration + 3) x source_weight
        x (0.35 + 0.65 x recency)
```

- **relevance** — topic keyword hits, weighted more in the title than the body
- **engagement** — `log1p` of HN points, Reddit upvotes and Bluesky likes,
  capped so one viral post can't dominate the page
- **corroboration** — a bonus per additional independent source carrying the
  same story. This is the signal that surfaces things you'd otherwise miss
- **recency** — 36-hour half-life, applied as a damper rather than a
  multiplier so a significant paper from Tuesday still outranks a shallow
  post from an hour ago

### Deduplication

The same announcement typically arrives four times: the lab blog, an HN
thread, an arXiv preprint and a Bluesky link. Pass one groups on canonical
URL (tracking parameters stripped, `www` and AMP normalised). Pass two
compares title token overlap to catch sources that link to different URLs for
one story. The highest-weighted source becomes the card's face — a lab's own
announcement outranks someone linking to it — and the rest become source
badges.

Link wrappers (`safelinks.protection.outlook.com`, `t.co`, `lnkd.in` and
friends) are dropped outright. They point at redirectors, not documents, and
show up constantly in links copied out of Teams or Outlook.

## Tuning

Everything lives in `config.yml`; no code changes needed.

- **Add a source** — append to `feeds` with a `weight` and `tier`
- **Too much noise** — raise `hackernews.min_points` / `reddit.min_score`, or
  trim the `required` list
- **Missing a subject** — add terms under `topics`; they become filter chips
  automatically
- **Feed feels stale** — lower `scoring.half_life_hours`

One trap worth knowing: Hacker News queries are quoted (`'"LLM"'`) and sent
with `advancedSyntax`. Unquoted, Algolia's typo tolerance matches `LLM`
against `limiting` and the feed fills with unrelated posts.

## Layout

```
collect.py          orchestrator; writes docs/data/*.json
sources.py          one fetcher per source, each fails soft
pipeline.py         canonicalisation, dedupe, filtering, scoring
config.yml          feeds, keywords, thresholds
test_pipeline.py    offline tests, run in CI before each collection
make_sample.py      regenerates the placeholder dataset
docs/index.html     the dashboard (vanilla JS + Chart.js)
docs/data/          generated feed data, committed by the workflow
```

## Notes

- Scheduled workflows are disabled after 60 days without repo activity. The
  data commits reset that counter, so an active radar keeps itself alive.
- Cron times are UTC and GitHub delays scheduled runs under load; a 3-hourly
  job in practice lands within about 20 minutes of the hour.
- If every source fails, the collector exits non-zero without writing, so a
  network blip can't blank the dashboard.
- Read state and filter selections are stored in `localStorage`, per browser.
