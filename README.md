# Pegasus PC2476 Price Tracker

Tracks the price of one specific flight — **Pegasus Airlines PC2476, Ankara
Esenboğa (ESB) → Adnan Menderes (ADB), one way, 2026-09-17, ~19:30–20:45** —
every ~3 hours until it departs, using the SerpApi Google Flights API
(`selected_flights_json` pinning), and stores an independent observation
series in SQLite/CSV, automated via GitHub Actions.

It deliberately does **not** track the cheapest ESB–ADB flight, the cheapest
Pegasus flight, or Google's generic `price_insights.lowest_price` — see
"Why not `price_insights`?" below.

## How the price is actually extracted

SerpApi's own documented examples for `selected_flights_json` (including a
one-way, single-segment case structurally identical to PC2476) show that:

- **`selected_flights[0]` carries no price field.** It only has the
  itinerary's flight details (airports, times, flight number) plus a
  `booking_token` (one-way) or `departure_token`.
- **The price lives in `booking_options[i].together.price`** (a float in
  the requested `currency`). There is usually more than one entry — one
  per seller (the airline directly, OTAs, etc.) — for the *same* pinned
  itinerary. Google's UI shows the cheapest of these by default.
- **`price_insights.lowest_price`** is documented only as "the lowest
  price among the returned flights." When you pin a single itinerary via
  `selected_flights_json`, that pinned itinerary is the only one in the
  results, so in SerpApi's own examples this number happens to equal the
  cheapest `booking_options` price for that same itinerary — but that's
  an observed side effect, not a documented contract, so it is **never**
  used as the primary source here.
- **`price_insights.price_history`** is Google's own general route/typical
  pricing history — unrelated to this project's own PC2476 observations.

### Extraction hierarchy (`tracker/extract.py::extract_pc2476_price`)

1. **Validate** `selected_flights[0]` against the expected flight number
   (`PC2476`), route (`ESB`→`ADB`), date (`2026-09-17`), and (loosely,
   flagged-not-rejected) the ~19:30/20:45 schedule. A direct flight must
   have exactly one segment. Any mismatch → `FLIGHT_NOT_MATCHED`.
2. **Take the minimum** of all `booking_options[*].together.price` (or the
   `separate_tickets` departing+returning sum, defensively handled) for
   that validated itinerary. This is `pc2476_price`, with `source` recording
   the exact JSON path used, e.g. `booking_options[0].together.price`.
3. **Defensive fallback**: if `booking_options` is empty, scan
   `best_flights`/`other_flights` (only relevant if SerpApi ever returns
   them alongside a pinned search) for an entry whose own segments match
   PC2476, and use that entry's own `price` field.
4. If nothing usable was found → `PRICE_NOT_FOUND`.

`price_insights` (`google_lowest_price`, `google_price_level`,
`google_typical_price_low/high`) is extracted **separately**, in parallel,
and is stored in its own columns. It never feeds into `pc2476_price`.

Run `python -m tracker.main --from-file tests/sample_response.json` to see
this in action — it prints the exact `ExtractionResult`, including `source`,
with **zero SerpApi calls**.

## Project layout

```
tracker/
  config.py       flight identity, currency, quota limits, file paths
  extract.py       pure extraction logic (see above) - fully unit tested
  db.py            SQLite schema + helpers
  quota.py         guards run BEFORE any API call (quota / duplicate / end-date)
  serp_client.py   exactly one HTTPS call to SerpApi, no retries
  main.py          CLI entry point: one observation, save, exit
reports/
  generate_report.py   rebuilds CSV + chart from SQLite, zero API calls
tests/
  sample_response.json  realistic SerpApi response used for testing
  test_extract.py        13 tests covering the extraction hierarchy
.github/workflows/track_price.yml   the cron automation
data/                    flight_prices.db, price_history.csv, raw JSON evidence
```

## Setup

1. `pip install -r requirements.txt`
2. Get a SerpApi key (free plan: 250 searches/month) from serpapi.com.
3. **Local testing**: copy `.env.example` to `.env`, fill in your key, and
   `export $(cat .env | xargs)` before running commands (or use your
   shell/IDE's env-file support). `.env` is git-ignored.
4. **Production (GitHub Actions)**: add a repository secret named
   `SERPAPI_API_KEY` (Settings → Secrets and variables → Actions). The
   workflow reads it as `${{ secrets.SERPAPI_API_KEY }}` and the Python
   code only ever reads it via `os.environ["SERPAPI_API_KEY"]` — it is
   never written to source or printed to logs.

## Usage

```bash
# Normal scheduled behaviour: at most one SerpApi call, then exit.
python -m tracker.main

# Perform one real search AND save the complete raw JSON for inspection
# (uses one of your 250 monthly credits, same as a normal run).
python -m tracker.main --save-response sample_response.json

# Zero-cost dry run against a previously saved response - test PC2476
# detection, price extraction, and error handling as much as you like.
python -m tracker.main --from-file sample_response.json

# Rebuild data/price_history.csv and data/pc2476_price_chart.png from
# whatever is currently in SQLite. Zero API calls.
python -m reports.generate_report

# Run the test suite (zero API calls).
pytest tests/
```

## Quota protection (SerpApi free plan: 250 searches/month)

8 observations/day × 31 days = 248 — uncomfortably close to 250. Three
independent guards run **before** any request, in this order
(`tracker/quota.py::evaluate_guards`):

1. **Tracking complete** — if `now >= 2026-09-17 19:30 Europe/Istanbul`,
   record `TRACKING_COMPLETE` and stop. No more calls after the flight
   has departed, regardless of everything else.
2. **Monthly limit guard** — `MAX_MONTHLY_CALLS = 245` (a 5-call safety
   margin under 250), counted from SQLite (`counted_toward_quota = 1`
   rows this calendar month). If reached, record `MONTHLY_LIMIT_GUARD`
   and stop.
3. **Duplicate protection** — if the last *actual API-call* observation
   was less than 2h30m ago, record `SKIPPED_DUPLICATE` and stop. This
   protects the quota if GitHub Actions ever fires twice for the same
   slot (e.g. a manual `workflow_dispatch` landing near a scheduled run;
   the workflow's `concurrency` group also prevents literal overlap).

Every one of these paths writes a row to SQLite — nothing is silently
skipped, per the "no missing evidence" requirement (see `status` values
below).

Every execution makes **at most one** SerpApi HTTP request
(`tracker/serp_client.py::run_search`) — no retries, no follow-up calls
to double-check `price_insights` or booking details separately. Everything
needed is pulled from the single response.

## Status values

| status | meaning | counts toward monthly quota? |
|---|---|---|
| `SUCCESS` | price extracted and validated | yes |
| `PRICE_NOT_FOUND` | itinerary matched, no usable price found | yes |
| `FLIGHT_NOT_FOUND` | `selected_flights` missing entirely | yes |
| `FLIGHT_NOT_MATCHED` | response describes a different flight | yes |
| `API_ERROR` | SerpApi returned an `error` field | yes |
| `INVALID_RESPONSE` | malformed / non-JSON response | yes* |
| `NETWORK_ERROR` | request never confirmed reaching SerpApi | no |
| `SKIPPED_DUPLICATE` | too soon after the last real call | no |
| `MONTHLY_LIMIT_GUARD` | at/above `MAX_MONTHLY_CALLS` | no |
| `TRACKING_COMPLETE` | flight has already departed | no |

\* `INVALID_RESPONSE` counts because a response was received; a pure
network failure before any response (`NETWORK_ERROR`) does not.

## Scheduling (GitHub Actions, UTC vs. Europe/Istanbul)

Turkey is fixed at UTC+3 year-round (no DST). Target Istanbul times:
`00:00 03:00 06:00 09:00 12:00 15:00 18:00 21:00`. Because these are 3
hours apart and 3 evenly divides 24, the corresponding UTC times are the
*same set*, `0 3 6 9 12 15 18 21` — a `-3h` shift on an evenly-spaced
3-hour cycle just rotates which run is which, it doesn't change which UTC
hours are used. So the cron expression is simply:

```
0 */3 * * *
```

GitHub's schedule can run a few minutes late under load; that's fine —
the duplicate-protection guard (2h30m window) absorbs the drift, and the
tracking-complete check uses the actual current time, not the nominal
schedule.

## Persistence on GitHub Actions runners

Runners are ephemeral, so the workflow (`.github/workflows/track_price.yml`):
1. checks out the repo,
2. runs the tracker (writing to `data/flight_prices.db` and, via the
   report step, `data/price_history.csv` and `data/pc2476_price_chart.png`),
3. commits and pushes `data/` back to the repository if it changed.

The commit message includes `[skip ci]` as a safeguard against a future
push-triggered workflow re-running on the bot's own commit; the schedule
trigger itself is never re-triggered by a push, so this isn't currently
load-bearing, just defensive. `permissions: contents: write` is scoped to
only what's needed for that commit — no other repo permissions are granted.
`.env`, `*.env`, and any file literally named `SERPAPI_API_KEY` are
git-ignored; the key only ever exists as the `SERPAPI_API_KEY` GitHub
Actions secret and an environment variable at runtime.

## Raw evidence retention

The first observation, and any observation with a non-`SUCCESS` status or
a `pc2476_price` change from the previous `SUCCESS`, has its complete raw
JSON saved to `data/raw_responses/`, capped at the most recent
`MAX_RAW_RESPONSES_KEPT = 40` files (`tracker/config.py`) to bound repo
growth. Routine unchanged-price successes are not saved raw (the SQLite
row is already sufficient for those).

## Development on Colab vs. production on GitHub Actions

Colab is fine for interactively exploring a saved `sample_response.json`
or iterating on `tracker/extract.py`, but the tracker is designed to run
as a single short-lived process (`one observation, save, exit` — no
`while True` loop anywhere), so GitHub Actions' cron scheduling is the
intended production environment and your own machine/Colab notebook
never needs to stay running.
