# Kingsland Properties KPI Dashboard

## Overview
Flask web app that pulls live data from the AppFolio API and renders a 4-tab KPI dashboard (Leasing, Delinquency, Maintenance, Marketing) with drill-down modals, trend charts, and an in-app review log. The Marketing tab is powered by the RentEngine public API.

## Architecture
- **Backend**: Python + Flask, `requests` for AppFolio + RentEngine APIs, `apscheduler` for daily 6am sync
- **Frontend**: Jinja2 + plain JS + Chart.js — no SPA framework
- **Port**: 5000 (workflow `Start application` runs `python app.py`)

## Key Files
- `app.py` — Flask app, AppFolio API client, KPI compute functions, /sync /filter /sync-status /log-reviews routes
- `templates/dashboard.html` — Single-page dashboard with all 3 tabs and modal infrastructure
- `static/style.css` — All styles
- `static/logo.png` — Kingsland logo
- `data/raw_data.json` — Raw all-time pull from AppFolio (built on Sync)
- `data/cache.json` — Computed KPIs for current selected range
- `data/kpi_history.json` — Daily snapshot for long-term trend charts (one entry per day)
- `data/sync_log.json` — Sync attempts log; credentials in error URLs are scrubbed via `scrub_creds()`
- `data/reviews.json` — Manual reviews counted by team member

## Date Range Filter
Selectable from the header dropdown. Supported keys (validated by `validate_range_key()`):
- `today`, `last_7_days`, `last_30_days`, `last_90_days`, `ytd`, `last_year`, `all_time`
- `custom:YYYY-MM-DD:YYYY-MM-DD` — built from the date pickers in the header when "Custom Range…" is selected

Snapshot KPIs (occupancy, delinquency totals, evictions) always reflect today's current state and do **not** change with the dropdown — a banner on the Delinquency tab calls this out. Date-filterable KPIs include move-ins/outs, notices, applications, renewals, and work orders.

## Drill-down Modals
Click any card with the orange hover hint to see the underlying list. Modal types include `occupancy`, `all_units`, `owner_holds`, `vacant`, `mtm`, `current`, `evictions` (works on both Leasing and Delinquency tabs), `notice_rented/unrented`, `moveins`, `moveouts`, `renewals`, `applications`, `delinquency`, `unit_turns` (active unit turns), and several work-order variants.

## Charts
- **Leasing**: occupancy & pre-leased trend (from `kpi_history.json`)
- **Delinquency**: total delinquent and collection % trends, plus per-PM delinquency trend
- **Maintenance**: WO type doughnut + 12-week completion-rate and WO-created trends, computed live from raw work order data via `get_wo_weekly_trend()` (with cache fallback if raw not yet synced)

## Unit Turns (Maintenance tab)
- Sourced from AppFolio `unit_turn_detail` report (`fetch_unit_turns()`, body `{"paginate_results": false}`); raw stored under `unit_turns` key.
- `compute_unit_turns()` applies a rolling 12-month window in-app (not the date dropdown). Goal/target is `TURN_TARGET_DAYS = 14`.
- Metrics: avg days to complete (last 12mo), turns in progress, count over target (flagged "needs questioning"), % of completed turns over target. Drilldown modal `unit_turns` lists active turns with on-track/over-target badges.
- Numbers depend on staff opening a turn at move-out and setting the turn end date when ready — surfaced via an inline note.

## Security
- AppFolio credentials are sent via `requests.post(url, auth=(cid, secret))` — never embedded in URLs
- `scrub_creds()` redacts any `https://user:pass@host` patterns from error messages before they're persisted to `data/sync_log.json` or returned in API responses
- Required secrets: `APPFOLIO_CLIENT_ID`, `APPFOLIO_CLIENT_SECRET`, optional `APPFOLIO_DATABASE` (default `neighborhoodent`)
- Marketing secret: `RENTENGINE_API_TOKEN` (Bearer token for the RentEngine API; sent via `Authorization: Bearer …`, never in URLs)

## Custom Jinja2 Filters
- `fmt_currency` — `$1,234`
- `fmt_pct` — `12.3%`

## Marketing & Showings (RentEngine)
- Powered by the RentEngine public API (`RENTENGINE_BASE`), Bearer auth via `RENTENGINE_API_TOKEN`.
- `fetch_marketing()` pulls units, prospects, and rental-application groups, then per-unit leasing-performance reports (throttled to 5 concurrent via `ThreadPoolExecutor`). Window is a trailing 12 months, **independent of the header date dropdown**.
- `compute_marketing()` builds: 8 KPI tiles (avg/median days-on-market, units on market, showing completion %, new prospects, showings completed, total calls, outbound texts), a leasing funnel (Prospects → Scheduled → Completed → Applications), new-prospects-by-source bar, a property-health donut (Healthy / At-Risk / Unknown / Off-Market), and per-unit/prospect/application detail lists for drilldowns.
- Showing completion target is `SHOWING_COMPLETION_TARGET = 95` (%).
- Data is cached in-memory for `MARKETING_TTL_SECONDS = 600` (10 min). `/marketing` returns `{ready, refreshing, error, data, fetched_at, stale}`; the first call kicks a background refresh and returns `ready:false`, and the frontend polls every 4s until ready. `/marketing?force=1` forces a refresh.
- Units that return 404 "No leasing performance data found" (no activity yet) are handled gracefully as zeros.

## Dependencies
- flask, requests, apscheduler

## Recent Changes
- Jun 2026: PM delinquency breakdown now classifies the full portfolio by property group (first group wins on overlap) with a "Not Under PM Group" catch-all, so the per-manager rows always reconcile exactly to the top-line portfolio totals. Fixed property-group IDs to the client's real groups (Tiffany=37, Hailey=38).
- Jun 2026: Added the Marketing & Showings tab powered by the RentEngine API (12-mo rolling window, 8 KPIs, funnel, source bar, property-health donut, drilldowns; 10-min in-memory cache with background refresh).
- Jun 2026: Added Unit Turns KPI to the Maintenance tab (target ≤ 14 days; flags active turns over target).
- Jun 2026: Collection Rate switched to Total Collected ÷ Total Rent Charged (Option A); history backfilled to match.
- Jun 2026: Fixed Google review logging to merge per-person counts instead of overwriting; removed Travis & Ashley from the team list.
- Apr 2026: Active Evictions card on Delinquency tab now drills down to the unit list.
- Apr 2026: Added Custom Date Range with date pickers; persists across tab switches and Sync.
- Apr 2026: Completion-rate trend now shows 12 weekly buckets computed from the actual work-order created_at dates instead of relying on a single daily snapshot.
- Apr 2026: Removed credentials from AppFolio request URLs and scrubbed historical sync log entries.
