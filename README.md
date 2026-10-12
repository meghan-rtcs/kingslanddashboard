# Kingsland Properties KPI Dashboard v2

Flask/Python dashboard pulling live data from AppFolio Reports API v2.

## Replit Secrets Required

| Secret | Value |
|--------|-------|
| `APPFOLIO_CLIENT_ID` | Your AppFolio Client ID |
| `APPFOLIO_CLIENT_SECRET` | Your AppFolio Client Secret |
| `APPFOLIO_DATABASE` | `neighborhoodent` |

## Property Groups (configured in app.py)

| PM Name | Group ID |
|---------|----------|
| Alex Lennox | 34 |
| Stefany Seiler | 33 |
| Tiffany Smith | 30 |

## Data Storage

All data stored as JSON in `/data/`:
- `cache.json` — latest sync results
- `kpi_history.json` — one snapshot per day across all tabs
- `reviews_history.json` — weekly 5-star Google review entries
- `sync_log.json` — sync history/errors

## API Calls per Sync

1. Rent Roll (Itemized) → actual charged rent for collection %
2. Rent Roll → unit statuses / occupancy
3. Unit Vacancy Detail → days vacant, vacant units
4. Leasing Summary → move-ins, move-outs, renewals
5. Leasing Funnel Performance → applications
6. Delinquency (portfolio total)
7. Delinquency × 3 property groups (Alex, Stefany, Tiffany)
8. Work Order → maintenance metrics

Total: ~8 calls. Rate limit: 7 per 15s — sync takes ~25–35 seconds.

## Schedule

Auto-syncs daily at 6am via APScheduler. Manual "Sync Now" button available in the header.

## Tabs

- **Leasing** — Occupancy, Pre-Leased, unit statuses, move-ins/outs, renewals, applications, 5-star reviews
- **Delinquency** — Collection %, total delinquent, aging buckets, PM breakdown table, historical trend charts
- **Maintenance** — Work order counts, completion rate, vendor split, WO by type donut, trend charts
