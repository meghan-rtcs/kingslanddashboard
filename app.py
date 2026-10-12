import os
import re
import hmac
import html
import json
import time
import logging
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(override=True)

import requests
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'static'))
app.config['TEMPLATES_AUTO_RELOAD'] = True

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Password Protection (session login page) ────────────────────────────────
# The whole dashboard is gated behind a single shared password stored in the
# DASHBOARD_PASSWORD secret. Visitors see a landing page asking for the
# password only; a signed session cookie keeps them logged in afterwards.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
app.secret_key = os.environ.get("SESSION_SECRET", "")
if not app.secret_key:
    raise RuntimeError("SESSION_SECRET is not set — required for login sessions.")

# Data/AJAX endpoints that should get a JSON 401 rather than an HTML redirect.
_JSON_PATHS = ("/sync", "/filter", "/sync-status", "/marketing", "/log-reviews")

@app.before_request
def require_password():
    if not DASHBOARD_PASSWORD:
        # Fail closed: never serve the dashboard unprotected.
        return Response("Dashboard password is not configured.", 503)
    if request.endpoint is None:
        return None  # unknown route — let Flask return its normal 404
    if request.endpoint in ("login", "static"):
        return None
    if session.get("authenticated"):
        return None
    if request.method != "GET" or request.path.startswith(_JSON_PATHS) \
       or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if DASHBOARD_PASSWORD and hmac.compare_digest(pw, DASHBOARD_PASSWORD):
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        error = "Incorrect password. Please try again."
    if session.get("authenticated"):
        return redirect("/")
    return render_template("login.html", error=error)

@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
RAW_FILE         = DATA_DIR / "raw_data.json"       # full AppFolio pull
KPI_HISTORY_FILE = DATA_DIR / "kpi_history.json"    # one entry per day for trend charts
REVIEWS_FILE     = DATA_DIR / "reviews_history.json"
CACHE_FILE       = DATA_DIR / "cache.json"          # current computed KPIs
SYNC_LOG_FILE    = DATA_DIR / "sync_log.json"

# ─── Client Config ────────────────────────────────────────────────────────────

PROPERTY_GROUPS = {
    "Tiffany": "37",
    "Hailey":  "38",
}

# Catch-all bucket for any property not inside a configured PROPERTY_GROUPS group,
# so the per-PM breakdown always reconciles to the portfolio totals.
PM_OTHER_LABEL = "Not Under PM Group"

REVIEW_TEAM_MEMBERS = ["Team Total", "Taylor", "Mike", "Hailey", "Tiffany", "Alex"]
TURN_TARGET_DAYS    = 14   # Goal: complete a unit turn in this many days or fewer
KINGSLAND_VENDOR    = "Kingsland Properties LLC"
COMPLETED_STATUSES  = {"Completed", "Completed No Need To Bill"}
WORK_DONE_STATUSES  = {"Work Done"}
OPEN_STATUSES       = {"New", "Assigned", "Scheduled", "Waiting", "Estimate Requested", "Estimated"}

# All-time pull range — no point going before AppFolio usage
ALL_TIME_FROM = date(2020, 1, 1)

# ─── RentEngine (Marketing & Showings) ────────────────────────────────────────
RENTENGINE_BASE           = "https://app.rentengine.io/api/public/v1"
SHOWING_COMPLETION_TARGET = 95     # % — target showing completion rate
MARKETING_TTL_SECONDS     = 600    # 10-minute in-memory cache
RE_PAGE_LIMIT             = 100    # max page size RentEngine allows
RE_MAX_PAGES              = 100    # hard safety cap on pagination
RE_UNIT_CONCURRENCY       = 5      # parallel per-unit report calls (avoid 429s)

sync_lock        = threading.Lock()
sync_in_progress = False
active_range_key = "last_30_days"

# In-memory marketing cache (RentEngine is slow: one report call per unit)
_marketing_cache      = {"data": None, "fetched_at": 0.0, "error": None}
_marketing_lock       = threading.Lock()
_marketing_refreshing = False

# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_json(path, default):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return default

def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str))

def clean_num(s):
    try:
        return float(str(s).replace(",", "").replace("$", "").replace("%", "").strip() or 0)
    except:
        return 0.0

def is_owner_hold(row):
    tags = str(row.get("unit_tags") or row.get("tags") or "").lower()
    return "hold" in tags

def fd(d):  return d.strftime("%Y-%m-%d")
def fm(d):  return d.strftime("%Y-%m")

def parse_date(s):
    """Parse an AppFolio date string (YYYY-MM-DD, optionally with time) to a date, or None."""
    if not s:
        return None
    txt = str(s).strip()[:10]
    try:
        return datetime.strptime(txt, "%Y-%m-%d").date()
    except ValueError:
        return None

# ─── Date Range ───────────────────────────────────────────────────────────────

def get_date_range(range_key):
    today = date.today()
    if range_key == "today":
        return today, today
    elif range_key == "last_7_days":
        return today - timedelta(days=7), today
    elif range_key == "last_30_days":
        return today - timedelta(days=30), today
    elif range_key == "last_90_days":
        return today - timedelta(days=90), today
    elif range_key == "ytd":
        return date(today.year, 1, 1), today
    elif range_key == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif range_key == "all_time":
        return ALL_TIME_FROM, today
    elif isinstance(range_key, str) and range_key.startswith("custom:"):
        try:
            _, from_str, to_str = range_key.split(":")
            df = datetime.strptime(from_str, "%Y-%m-%d").date()
            dt = datetime.strptime(to_str,   "%Y-%m-%d").date()
            return df, dt
        except Exception:
            return today - timedelta(days=30), today
    return today - timedelta(days=30), today

# ─── AppFolio API ─────────────────────────────────────────────────────────────

def get_creds():
    cid    = os.environ.get("APPFOLIO_CLIENT_ID", "")
    secret = os.environ.get("APPFOLIO_CLIENT_SECRET", "")
    db     = os.environ.get("APPFOLIO_DATABASE", "neighborhoodent")
    return cid, secret, db

_CRED_URL_RE = re.compile(r"https://[^/@]+:[^/@]+@")

def scrub_creds(text):
    """Replace any embedded basic-auth in URLs with [REDACTED]."""
    try:    return _CRED_URL_RE.sub("https://[REDACTED]@", str(text))
    except: return str(text)

def af_post(endpoint, payload, retry=3):
    """POST to an AppFolio report endpoint and return the merged result list,
    following pagination cursors. AppFolio's `next_page_url` must be re-POSTed
    with the original body and Basic Auth — a bare GET hits their marketing
    site and returns a 404 HTML page."""
    cid, secret, db = get_creds()
    if not cid or not secret:
        raise ValueError("AppFolio credentials not configured.")
    url         = f"https://{db}.appfolio.com/api/v2/reports/{endpoint}.json"
    all_results = []

    def _normalize_next(nu):
        if not nu: return None
        for _ in range(4):
            d = html.unescape(nu)
            if d == nu: break
            nu = d
        if nu.startswith("/"):
            nu = f"https://{db}.appfolio.com{nu}"
        return nu

    for attempt in range(retry):
        try:
            r = requests.post(url, json=payload, auth=(cid, secret), timeout=90)
            if r.status_code == 400:
                raise requests.HTTPError(f"400 on {endpoint}: {r.text[:300]}", response=r)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 15))
                logger.warning(f"Rate limited on {endpoint}, waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data     = r.json()
            results  = data.get("results", data) if isinstance(data, dict) else data
            all_results.extend(results if isinstance(results, list) else [])
            next_url = _normalize_next(data.get("next_page_url") if isinstance(data, dict) else None)
            page_attempts = 0
            while next_url:
                time.sleep(0.5)
                # Pagination must POST WITHOUT the original filters — the
                # metadata_id in the URL already encodes them. AppFolio rejects
                # the request with 400 if both are sent.
                r2 = requests.post(next_url, json={}, auth=(cid, secret), timeout=90)
                if r2.status_code in (404, 410) and page_attempts < 2:
                    # metadata_id may have expired briefly; retry the same cursor.
                    page_attempts += 1
                    logger.warning(f"  page returned {r2.status_code}, retrying ({page_attempts}/2)")
                    time.sleep(2 * page_attempts)
                    continue
                if r2.status_code >= 400:
                    logger.error(f"  page fetch failed status={r2.status_code} body={r2.text[:200]!r}")
                r2.raise_for_status()
                page_attempts = 0
                d2       = r2.json()
                batch    = d2.get("results", d2) if isinstance(d2, dict) else d2
                all_results.extend(batch if isinstance(batch, list) else [])
                next_url = _normalize_next(d2.get("next_page_url") if isinstance(d2, dict) else None)
            return all_results
        except requests.HTTPError:
            if attempt == retry - 1:
                raise
            time.sleep(2 ** attempt)
    return all_results

# ─── Fetchers (all-time pulls for raw storage) ────────────────────────────────

def fetch_rent_roll():
    """Snapshot of current unit statuses — as of today."""
    return af_post("rent_roll", {
        "as_of_to":          fd(date.today()),
        "unit_visibility":   "active",
        "non_revenue_units": "0",
    })

def fetch_unit_vacancy():
    """Current vacancy snapshot."""
    return af_post("unit_vacancy", {"unit_visibility": "active"})

def fetch_tenant_tickler():
    """All move-ins, move-outs, notices from 2020 to today."""
    return af_post("tenant_tickler", {
        "property_visibility": "active",
        "occurred_on_from":    fd(ALL_TIME_FROM),
        "occurred_on_to":      fd(date.today()),
        "events":              ["Move-in", "Move-out", "Notice"],
    })

def fetch_rental_applications():
    """All applications from 2020 to today."""
    return af_post("rental_applications", {
        "property_visibility":                      "active",
        "rental_application_statuses":              ["all"],
        "rental_applications_filter_date_range_by": "Rental Application Received Date",
        "received_on_from":                         fd(ALL_TIME_FROM),
        "received_on_to":                           fd(date.today()),
    })

def fetch_renewal_summary():
    """All renewals from 2020 to today."""
    return af_post("renewal_summary", {
        "unit_visibility":          "active",
        "start_on_from":            fm(ALL_TIME_FROM),
        "start_on_to":              fm(date.today()),
        "statuses":                 ["Renewed", "Did Not Renew", "Month To Month", "Pending"],
        "include_tenant_transfers": "0",
    })

def fetch_tenant_transactions():
    """Current month transactions — used for collection %."""
    return af_post("tenant_transactions_summary", {
        "tenant_visibility":   "active",
        "property_visibility": "active",
        "properties": {
            "properties_ids":      [],
            "property_groups_ids": [],
            "portfolios_ids":      [],
            "owners_ids":          [],
        },
        "month_of_to": fm(date.today()),
    })

def fetch_tenant_transactions_by_group(group_id):
    """Current month transactions for one PM group."""
    return af_post("tenant_transactions_summary", {
        "tenant_visibility":   "active",
        "property_visibility": "active",
        "properties": {
            "properties_ids":      [],
            "property_groups_ids": [group_id],
            "portfolios_ids":      [],
            "owners_ids":          [],
        },
        "month_of_to": fm(date.today()),
    })

def fetch_delinquency(group_id=None):
    """Current delinquency — always live, never date-filtered."""
    payload = {
        "property_visibility":    "active",
        "tenant_statuses":        ["0", "4"],
        "amount_owed_in_account": "all",
    }
    if group_id:
        payload["properties"] = {
            "properties_ids":      [],
            "property_groups_ids": [group_id],
            "portfolios_ids":      [],
            "owners_ids":          [],
        }
    return af_post("delinquency", payload)

def fetch_work_orders():
    """All work orders from 2020 to today."""
    return af_post("work_order", {
        "property_visibility":    "active",
        "status_date_range_from": fd(ALL_TIME_FROM),
        "status_date_range_to":   fd(date.today()),
        "status_date":            "0",  # Created On
        "work_order_statuses":    ["0","1","2","9","3","6","8","12","4","5","7"],
        "work_order_types":       ["unit_turn", "tenant_requested", "internal"],
    })

def fetch_unit_turns():
    """All unit-turn detail rows. The rolling 12-month window is applied later
    in compute_unit_turns() — turn completions are sparse, so we pull everything
    and filter in-app rather than via a short date toolbar range."""
    return af_post("unit_turn_detail", {"paginate_results": False})

# ─── Filter Raw Data by Date Range ───────────────────────────────────────────

def filter_by_range(rows, date_field, date_from, date_to):
    """Filter a list of rows by a date field within the given range."""
    from_str = fd(date_from)
    to_str   = fd(date_to)
    result   = []
    for r in rows:
        val = str(r.get(date_field) or "")[:10]
        if val and from_str <= val <= to_str:
            result.append(r)
    return result

# ─── Compute: Leasing ─────────────────────────────────────────────────────────

def compute_leasing(rent_roll, vacancy, tickler, apps, renewals, date_from, date_to):
    # Occupancy — always current snapshot, not date-filtered
    holds  = [r for r in rent_roll if is_owner_hold(r)]
    active = [r for r in rent_roll if not is_owner_hold(r)]

    total_units  = len(rent_roll)
    owner_holds  = len(holds)
    total_active = len(active)

    current         = sum(1 for r in active if r.get("status") == "Current")
    evict           = sum(1 for r in active if r.get("status") == "Evict")
    vacant_rented   = sum(1 for r in active if r.get("status") == "Vacant-Rented")
    vacant_unrented = sum(1 for r in active if r.get("status") == "Vacant-Unrented")
    notice_rented   = sum(1 for r in active if r.get("status") == "Notice-Rented")
    notice_unrented = sum(1 for r in active if r.get("status") == "Notice-Unrented")

    occupied   = current + evict + notice_rented + notice_unrented
    pre_leased = occupied + vacant_rented
    occ_pct    = round(occupied   / total_active * 100, 1) if total_active > 0 else 0
    pre_pct    = round(pre_leased / total_active * 100, 1) if total_active > 0 else 0

    rents    = [clean_num(r["rent"]) for r in active if r.get("rent") and clean_num(r["rent"]) > 0]
    avg_rent = round(sum(rents) / len(rents), 2) if rents else 0

    mtm_count    = sum(1 for r in active if r.get("status") == "Current" and not r.get("lease_to"))
    termed_count = sum(1 for r in active if r.get("status") == "Current" and r.get("lease_to"))

    # Vacancy — current snapshot
    vacant_not_rented_count  = sum(1 for r in vacancy if r.get("unit_status") == "Vacant-Unrented")
    vacant_rented_soon_count = sum(1 for r in vacancy if r.get("unit_status") == "Vacant-Rented")
    days_vals = [clean_num(r["days_vacant"]) for r in vacancy
                 if r.get("days_vacant") and clean_num(r["days_vacant"]) > 0]
    avg_days_vacant = round(sum(days_vals) / len(days_vals), 1) if days_vals else 0

    # Tickler — filter by selected date range using occurred_date
    filtered_tickler = filter_by_range(tickler, "occurred_date", date_from, date_to)
    move_ins  = sum(1 for r in filtered_tickler if r.get("event") == "Move-in")
    move_outs = sum(1 for r in filtered_tickler if r.get("event") == "Move-out")
    notices   = sum(1 for r in filtered_tickler if r.get("event") == "Notice")

    # Applications — filter by received date
    filtered_apps = filter_by_range(apps, "received", date_from, date_to)
    apps_total    = len(filtered_apps)

    # Renewals — filter by start date
    filtered_renewals = filter_by_range(renewals, "lease_from", date_from, date_to)
    renewed = [r for r in filtered_renewals if r.get("status") == "Renewed"]
    seen, unique_renewals = set(), 0
    for r in renewed:
        oid = r.get("occupancy_id")
        if oid and oid not in seen:
            seen.add(oid); unique_renewals += 1
        elif not oid:
            unique_renewals += 1

    # Drill-down detail
    movein_detail  = [{"tenant": r.get("tenant",""), "unit": r.get("unit",""),
                        "property": r.get("property",""), "date": r.get("occurred_date","")}
                       for r in filtered_tickler if r.get("event") == "Move-in"]
    moveout_detail = [{"tenant": r.get("tenant",""), "unit": r.get("unit",""),
                        "property": r.get("property",""), "date": r.get("occurred_date","")}
                       for r in filtered_tickler if r.get("event") == "Move-out"]
    app_detail     = [{"applicant": r.get("applicants",""), "unit": r.get("unit_name",""),
                        "property": r.get("property_name",""), "status": r.get("status",""),
                        "received": str(r.get("received",""))[:10]}
                       for r in filtered_apps]
    renewal_detail = [{"tenant": r.get("tenant","") or r.get("occupancy_name",""),
                        "unit": r.get("unit",""), "property": r.get("property",""),
                        "status": r.get("status","")}
                       for r in renewed]
    def vac_row(r):
        return {"unit": r.get("unit",""), "property": r.get("property",""),
                "days_vacant": r.get("days_vacant",0),
                "available": r.get("available_on","")}
    vacant_unrented_detail = [vac_row(r) for r in vacancy
                              if r.get("unit_status") == "Vacant-Unrented"]
    vacant_rented_detail   = [vac_row(r) for r in vacancy
                              if r.get("unit_status") == "Vacant-Rented"]

    def unit_row(r):
        return {
            "tenant":   r.get("tenant","") or "Vacant",
            "unit":     r.get("unit","") or r.get("property_address",""),
            "property": r.get("property",""),
            "status":   r.get("status",""),
            "rent":     r.get("rent",""),
            "lease_to": r.get("lease_to","") or "Month-to-Month",
        }

    # Detail lists for rent-roll based cards — all from current snapshot
    eviction_detail     = [unit_row(r) for r in active if r.get("status") == "Evict"]
    mtm_detail          = [unit_row(r) for r in active if r.get("status") == "Current" and not r.get("lease_to")]
    hold_detail         = [unit_row(r) for r in holds]
    all_units_detail    = [unit_row(r) for r in rent_roll]
    notice_rented_detail   = [unit_row(r) for r in active if r.get("status") == "Notice-Rented"]
    notice_unrented_detail = [unit_row(r) for r in active if r.get("status") == "Notice-Unrented"]
    current_detail      = [unit_row(r) for r in active if r.get("status") == "Current"]

    return {
        "total_units": total_units, "owner_holds": owner_holds, "total_active": total_active,
        "current": current, "evict": evict, "vacant_rented": vacant_rented,
        "vacant_unrented": vacant_unrented, "notice_rented": notice_rented,
        "notice_unrented": notice_unrented, "occupied": occupied, "pre_leased": pre_leased,
        "occ_pct": occ_pct, "pre_leased_pct": pre_pct, "avg_rent": avg_rent,
        "mtm_count": mtm_count, "termed_count": termed_count,
        "vacant_not_rented": vacant_not_rented_count,
        "vacant_rented_soon": vacant_rented_soon_count,
        "avg_days_vacant": avg_days_vacant,
        "move_ins": move_ins, "move_outs": move_outs, "notices": notices,
        "renewals": unique_renewals, "apps_total": apps_total,
        "apps_per_movein": round(apps_total / move_ins, 1) if move_ins > 0 else 0,
        "movein_detail": movein_detail, "moveout_detail": moveout_detail,
        "app_detail": app_detail, "renewal_detail": renewal_detail,
        "vacant_unrented_detail": vacant_unrented_detail,
        "vacant_rented_detail":   vacant_rented_detail,
        "eviction_detail": eviction_detail,
        "mtm_detail": mtm_detail,
        "hold_detail": hold_detail,
        "all_units_detail": all_units_detail,
        "notice_rented_detail": notice_rented_detail,
        "notice_unrented_detail": notice_unrented_detail,
        "current_detail": current_detail,
    }

# ─── Compute: Delinquency (always current, never date-filtered) ───────────────

def compute_delinquency(delinq_rows, txn_rows, rent_roll):
    active      = [r for r in delinq_rows if not is_owner_hold(r)]
    tenant_rows = [r for r in active if clean_num(r.get("amount_receivable", 0)) > 0]

    total_delinquent = sum(clean_num(r["amount_receivable"]) for r in tenant_rows)
    bucket_0_30      = sum(clean_num(r.get("00_to30", 0))   for r in tenant_rows)
    bucket_30_plus   = sum(clean_num(r.get("30_plus", 0))   for r in tenant_rows)
    tenant_count     = len(tenant_rows)

    active_evictions = sum(1 for r in rent_roll
                           if r.get("status") == "Evict" and not is_owner_hold(r))

    # Collections — current month only
    # Formula: cash collected this month / rent charged this month
    # (apples-to-apples: same report, same time window, same tenant set)
    current_txn     = [r for r in txn_rows if r.get("occupancy_status") == "Current"]
    total_rent      = sum(clean_num(r.get("rent_charges", 0))  for r in current_txn)
    total_collected = sum(clean_num(r.get("cash_payments", 0)) for r in current_txn)
    collection_pct  = round(min(100, max(0, total_collected / total_rent * 100)), 1) \
                      if total_rent > 0 else 0

    delinq_detail = sorted([
        {"tenant":  r.get("name",""), "unit": r.get("unit",""),
         "property": r.get("property",""), "status": r.get("tenant_status",""),
         "amount":  clean_num(r.get("amount_receivable",0)),
         "0_30":    clean_num(r.get("00_to30",0)),
         "30_plus": clean_num(r.get("30_plus",0)),
         "notes":   r.get("delinquency_notes","") or ""}
        for r in tenant_rows
    ], key=lambda x: x["amount"], reverse=True)

    return {
        "total_delinquent": round(total_delinquent, 2),
        "bucket_0_30":      round(bucket_0_30, 2),
        "bucket_30_plus":   round(bucket_30_plus, 2),
        "tenant_count":     tenant_count,
        "total_rent":       round(total_rent, 2),
        "total_collected":  round(total_collected, 2),
        "collection_pct":   collection_pct,
        "active_evictions": active_evictions,
        "delinq_detail":    delinq_detail,
    }

def pm_group_label_list():
    """Ordered list of breakdown rows: each configured PM group, then the catch-all."""
    return list(PROPERTY_GROUPS.keys()) + [PM_OTHER_LABEL]


def build_pid_to_pm(raw):
    """Map each property_id to the PM group it belongs to, derived from the per-group
    raw pulls. The first configured group wins if a property appears in more than one,
    so every property is counted exactly once."""
    pid_to_pm = {}
    for pm_name in PROPERTY_GROUPS:
        for key in (f"delinq_{pm_name}", f"txn_{pm_name}"):
            for r in raw.get(key, []):
                pid = r.get("property_id")
                if pid is not None and pid not in pid_to_pm:
                    pid_to_pm[pid] = pm_name
    return pid_to_pm


def compute_pm_delinquency(raw):
    """Classify the FULL portfolio delinquency + collections by PM group, with an
    'Other' catch-all for any property not in a configured group. Because every row
    of the full report is assigned to exactly one bucket, the breakdown always sums
    back to the portfolio totals shown at the top of the tab."""
    pid_to_pm = build_pid_to_pm(raw)
    labels    = pm_group_label_list()
    pm_data   = {n: {"total_delinquent": 0.0, "tenant_count": 0,
                     "bucket_0_30": 0.0, "bucket_30_plus": 0.0,
                     "_rent": 0.0, "_collected": 0.0} for n in labels}

    # Delinquency — from the full (unfiltered) delinquency pull
    for r in raw.get("delinquency", []):
        if is_owner_hold(r):
            continue
        amt = clean_num(r.get("amount_receivable", 0))
        if amt <= 0:
            continue
        bucket = pm_data[pid_to_pm.get(r.get("property_id"), PM_OTHER_LABEL)]
        bucket["total_delinquent"] += amt
        bucket["bucket_0_30"]      += clean_num(r.get("00_to30", 0))
        bucket["bucket_30_plus"]   += clean_num(r.get("30_plus", 0))
        bucket["tenant_count"]     += 1

    # Collections — from the full transactions pull (current occupancy only)
    for r in raw.get("txn", []):
        if r.get("occupancy_status") != "Current":
            continue
        bucket = pm_data[pid_to_pm.get(r.get("property_id"), PM_OTHER_LABEL)]
        bucket["_rent"]      += clean_num(r.get("rent_charges", 0))
        bucket["_collected"] += clean_num(r.get("cash_payments", 0))

    out = {}
    for name in labels:
        b    = pm_data[name]
        rent = b.pop("_rent")
        col  = b.pop("_collected")
        b["collection_pct"]   = round(min(100, max(0, col / rent * 100)), 1) if rent > 0 else 0
        b["total_delinquent"] = round(b["total_delinquent"], 2)
        b["bucket_0_30"]      = round(b["bucket_0_30"], 2)
        b["bucket_30_plus"]   = round(b["bucket_30_plus"], 2)
        out[name] = b
    return out

# ─── Compute: Maintenance (filtered by date range) ────────────────────────────

def compute_maintenance(all_wo_rows, active_unit_count, date_from, date_to):
    # Filter work orders by created_at date
    wo_rows = filter_by_range(all_wo_rows, "created_at", date_from, date_to)

    total_created   = len(wo_rows)
    total_completed = sum(1 for r in wo_rows if r.get("status") in COMPLETED_STATUSES)
    work_done       = sum(1 for r in wo_rows if r.get("status") in WORK_DONE_STATUSES)
    total_open      = sum(1 for r in wo_rows if r.get("status") in OPEN_STATUSES)
    pct_completed   = round(total_completed / total_created * 100, 1) if total_created > 0 else 0

    ks_rows  = [r for r in wo_rows if r.get("vendor") == KINGSLAND_VENDOR]
    out_rows = [r for r in wo_rows if r.get("vendor") and r.get("vendor") != KINGSLAND_VENDOR]
    ks_comp  = sum(1 for r in ks_rows  if r.get("status") in COMPLETED_STATUSES)
    out_comp = sum(1 for r in out_rows if r.get("status") in COMPLETED_STATUSES)

    amounts     = [clean_num(r["amount"]) for r in wo_rows
                   if r.get("amount") and clean_num(r["amount"]) > 0]
    total_spend = round(sum(amounts), 2)
    avg_cost    = round(total_spend / len(amounts), 2) if amounts else 0
    avg_per_ten = round(total_created / active_unit_count, 2) if active_unit_count > 0 else 0

    unit_turn = sum(1 for r in wo_rows if r.get("work_order_type") == "Unit Turn")
    resident  = sum(1 for r in wo_rows if r.get("work_order_type") == "Resident")
    internal  = sum(1 for r in wo_rows if r.get("work_order_type") == "Internal")

    wo_detail = sorted([
        {"number":   r.get("work_order_number",""),
         "unit":     r.get("unit_name","") or r.get("unit_address",""),
         "property": r.get("property_name","") or r.get("property",""),
         "type":     r.get("work_order_type",""),
         "status":   r.get("status",""),
         "vendor":   r.get("vendor","") or "—",
         "amount":   clean_num(r.get("amount",0)),
         "created":  r.get("created_at",""),
         "desc":     r.get("job_description","") or ""}
        for r in wo_rows
    ], key=lambda x: x["created"], reverse=True)

    return {
        "total_created": total_created, "total_completed": total_completed,
        "pct_completed": pct_completed, "work_done": work_done, "total_open": total_open,
        "kingsland_created": len(ks_rows), "kingsland_completed": ks_comp,
        "outside_created": len(out_rows), "outside_completed": out_comp,
        "total_spend": total_spend, "avg_cost": avg_cost, "avg_per_tenancy": avg_per_ten,
        "by_type": {"unit_turn": unit_turn, "resident": resident, "internal": internal},
        "wo_detail": wo_detail,
    }

# ─── Compute: Unit Turns (rolling 12-month window) ────────────────────────────

def compute_unit_turns(turn_rows):
    """Unit-turn timing KPI from AppFolio unit_turn_detail rows.

    Goal: complete a turn in TURN_TARGET_DAYS or fewer. Anything over is flagged
    for questioning. Uses a rolling 12-month window for completed turns because
    turn completions are sparse."""
    today  = date.today()
    cutoff = today - timedelta(days=365)

    def days_for(r):
        # Prefer AppFolio's precomputed duration; else derive from the two dates.
        precomputed = clean_num(r.get("total_days_to_complete", 0))
        if precomputed > 0:
            return int(precomputed)
        mo = parse_date(r.get("move_out_date"))
        te = parse_date(r.get("turn_end_date"))
        if mo and te:
            return max(0, (te - mo).days)
        return 0

    active, completed = [], []
    for r in turn_rows:
        mo = parse_date(r.get("move_out_date"))
        te = parse_date(r.get("turn_end_date"))
        if not te and mo and mo <= today:
            active.append(r)                       # in progress
        elif te and cutoff <= te <= today:
            completed.append(r)                    # finished within last 12 months

    completed_12mo = len(completed)
    avg_days = round(sum(days_for(r) for r in completed) / completed_12mo, 1) \
               if completed_12mo else 0

    active_detail      = []
    over_target_active = 0
    for r in active:
        mo          = parse_date(r.get("move_out_date"))
        days_active = (today - mo).days if mo else 0
        flagged     = days_active > TURN_TARGET_DAYS
        if flagged:
            over_target_active += 1
        emi   = parse_date(r.get("expected_move_in_date"))
        stage = f"Expected move-in {fd(emi)}" if emi else "In Progress"
        active_detail.append({
            "unit":        r.get("unit", "") or r.get("unit_address", "") or "",
            "property":    r.get("property", "") or "",
            "started":     fd(mo) if mo else "—",
            "days_active": days_active,
            "stage":       stage,
            "flagged":     flagged,
        })
    active_detail.sort(key=lambda x: x["days_active"], reverse=True)

    completed_detail      = []
    over_target_completed = 0
    for r in completed:
        days    = days_for(r)
        flagged = days > TURN_TARGET_DAYS
        if flagged:
            over_target_completed += 1
        mo = parse_date(r.get("move_out_date"))
        te = parse_date(r.get("turn_end_date"))
        completed_detail.append({
            "unit":     r.get("unit", "") or r.get("unit_address", "") or "",
            "property": r.get("property", "") or "",
            "started":  fd(mo) if mo else "—",
            "ended":    fd(te) if te else "—",
            "days":     days,
            "flagged":  flagged,
        })
    completed_detail.sort(key=lambda x: x["days"], reverse=True)

    over_target_pct = round(over_target_completed / completed_12mo * 100, 1) \
                      if completed_12mo else 0

    return {
        "target_days":             TURN_TARGET_DAYS,
        "in_progress":             len(active),
        "completed_12mo":          completed_12mo,
        "avg_days_completed_12mo": avg_days,
        "over_14_count":           over_target_active,
        "over_14_count_completed": over_target_completed,
        "over_14_pct_completed":   over_target_pct,
        "active_detail":           active_detail,
        "completed_detail":        completed_detail,
    }

# ─── Build Cache from Raw Data ────────────────────────────────────────────────

def build_cache(raw, range_key):
    """Re-compute all KPIs from raw data for the given range. No API calls."""
    date_from, date_to = get_date_range(range_key)

    rent_roll = raw.get("rent_roll", [])
    vacancy   = raw.get("vacancy", [])
    tickler   = raw.get("tickler", [])
    apps      = raw.get("apps", [])
    renewals  = raw.get("renewals", [])
    txn_rows  = raw.get("txn", [])
    delinq    = raw.get("delinquency", [])
    wo_rows   = raw.get("work_orders", [])

    active_count     = len([r for r in rent_roll if not is_owner_hold(r)])
    leasing_kpis     = compute_leasing(rent_roll, vacancy, tickler, apps, renewals, date_from, date_to)
    delinq_kpis      = compute_delinquency(delinq, txn_rows, rent_roll)
    delinq_kpis["pm_breakdown"] = compute_pm_delinquency(raw)
    maintenance_kpis = compute_maintenance(wo_rows, active_count, date_from, date_to)
    maintenance_kpis["unit_turns"] = compute_unit_turns(raw.get("unit_turns", []))

    cache = {
        "synced_at": raw.get("synced_at", ""),
        "range_key": range_key,
        "date_from": fd(date_from),
        "date_to":   fd(date_to),
        "leasing":     leasing_kpis,
        "delinquency": delinq_kpis,
        "maintenance": maintenance_kpis,
    }
    save_json(CACHE_FILE, cache)
    return cache

# ─── Save History Snapshot ────────────────────────────────────────────────────

def _maint_snapshot(maint):
    """Strip drill-down detail from the maintenance block before storing history."""
    out = {k: v for k, v in maint.items() if k != "wo_detail"}
    if isinstance(out.get("unit_turns"), dict):
        out["unit_turns"] = {k: v for k, v in out["unit_turns"].items()
                             if k not in ("active_detail", "completed_detail")}
    return out

def save_history_snapshot(cache):
    """Save today's KPI snapshot for trend charts. Overwrites if already synced today."""
    today_str = date.today().isoformat()
    history   = load_json(KPI_HISTORY_FILE, [])
    entry = {
        "date":      today_str,
        "synced_at": cache["synced_at"],
        # Strip drill-down detail to keep file lean
        "leasing":     {k: v for k, v in cache["leasing"].items()     if not k.endswith("_detail")},
        "delinquency": {k: v for k, v in cache["delinquency"].items() if k != "delinq_detail"},
        "maintenance": _maint_snapshot(cache["maintenance"]),
    }
    history = [h for h in history if h.get("date") != today_str]
    history.append(entry)
    history.sort(key=lambda x: x["date"])
    save_json(KPI_HISTORY_FILE, history)

# ─── Main Sync (hits AppFolio, stores raw, builds cache) ─────────────────────

def do_sync(range_key="last_30_days"):
    global sync_in_progress, active_range_key
    with sync_lock:
        if sync_in_progress:
            return {"ok": False, "error": "Sync already in progress"}
        sync_in_progress = True
        active_range_key = range_key

    logger.info(f"Starting sync — will pull all data from {fd(ALL_TIME_FROM)} to today")

    try:
        raw = {}

        logger.info("Fetching Rent Roll...")
        raw["rent_roll"] = fetch_rent_roll()
        time.sleep(1)

        logger.info("Fetching Unit Vacancy...")
        raw["vacancy"] = fetch_unit_vacancy()
        time.sleep(1)

        logger.info("Fetching Tenant Tickler (all time)...")
        raw["tickler"] = fetch_tenant_tickler()
        time.sleep(1)

        logger.info("Fetching Rental Applications (all time)...")
        raw["apps"] = fetch_rental_applications()
        time.sleep(1)

        logger.info("Fetching Renewal Summary (all time)...")
        raw["renewals"] = fetch_renewal_summary()
        time.sleep(1)

        logger.info("Fetching Tenant Transactions (current month)...")
        raw["txn"] = fetch_tenant_transactions()
        time.sleep(1)

        logger.info("Fetching Tenant Transactions by PM...")
        for pm_name, group_id in PROPERTY_GROUPS.items():
            try:
                raw[f"txn_{pm_name}"] = fetch_tenant_transactions_by_group(group_id)
                time.sleep(1)
            except Exception as e:
                logger.error(f"TXN error {pm_name}: {e}")
                raw[f"txn_{pm_name}"] = []

        logger.info("Fetching Delinquency (portfolio)...")
        raw["delinquency"] = fetch_delinquency()
        time.sleep(1)

        logger.info("Fetching Delinquency by PM...")
        for pm_name, group_id in PROPERTY_GROUPS.items():
            try:
                raw[f"delinq_{pm_name}"] = fetch_delinquency(group_id=group_id)
                time.sleep(1)
            except Exception as e:
                logger.error(f"Delinq error {pm_name}: {e}")
                raw[f"delinq_{pm_name}"] = []

        logger.info("Fetching Work Orders (all time)...")
        wo_error = None
        try:
            raw["work_orders"] = fetch_work_orders()
            logger.info(f"Work Orders fetched: {len(raw['work_orders'])} rows")
        except Exception as e:
            wo_error = scrub_creds(e)
            logger.error(f"Work Orders error: {wo_error}")
            # Preserve previously-synced work orders if we have them
            prev = load_json(RAW_FILE, {}) or {}
            raw["work_orders"] = prev.get("work_orders", [])
            logger.info(f"Falling back to {len(raw['work_orders'])} previously-cached work orders")
        time.sleep(1)

        logger.info("Fetching Unit Turns (all time)...")
        ut_error = None
        try:
            raw["unit_turns"] = fetch_unit_turns()
            logger.info(f"Unit Turns fetched: {len(raw['unit_turns'])} rows")
        except Exception as e:
            ut_error = scrub_creds(e)
            logger.error(f"Unit Turns error: {ut_error}")
            prev = load_json(RAW_FILE, {}) or {}
            raw["unit_turns"] = prev.get("unit_turns", [])
            logger.info(f"Falling back to {len(raw['unit_turns'])} previously-cached unit turns")
        time.sleep(1)

        raw["synced_at"] = datetime.utcnow().isoformat() + "Z"

        # Save raw data to disk
        logger.info("Saving raw data...")
        save_json(RAW_FILE, raw)

        # Build cache for current range
        logger.info(f"Building cache for range: {range_key}")
        cache = build_cache(raw, range_key)

        # Save today's snapshot to history (overwrite if already synced today)
        save_history_snapshot(cache)

        # Sync log
        logs       = load_json(SYNC_LOG_FILE, [])
        warnings   = []
        if wo_error: warnings.append(f"Work Orders: {wo_error}")
        if ut_error: warnings.append(f"Unit Turns: {ut_error}")
        log_entry  = {"synced_at": raw["synced_at"],
                      "status":    "partial" if warnings else "success",
                      "range_key": range_key,
                      "rows":      {k: len(v) for k, v in raw.items()
                                    if isinstance(v, list)}}
        if warnings: log_entry["warning"] = " · ".join(warnings)
        logs.insert(0, log_entry)
        save_json(SYNC_LOG_FILE, logs[:100])

        logger.info("Sync complete." + (" (with warnings)" if warnings else ""))
        return {"ok": True, "synced_at": raw["synced_at"], "wo_error": wo_error, "ut_error": ut_error}

    except Exception as e:
        safe_err = scrub_creds(e)
        logger.error(f"Sync error: {safe_err}")
        logs = load_json(SYNC_LOG_FILE, [])
        logs.insert(0, {"synced_at": datetime.utcnow().isoformat()+"Z",
                         "status": "error", "error": safe_err})
        save_json(SYNC_LOG_FILE, logs[:100])
        return {"ok": False, "error": safe_err}
    finally:
        sync_in_progress = False

def sync_in_background(range_key="last_30_days"):
    threading.Thread(target=do_sync, args=(range_key,), daemon=True).start()

# ─── Reviews ──────────────────────────────────────────────────────────────────

def get_reviews_history():
    return load_json(REVIEWS_FILE, [])

def save_review_entry(counts_by_person):
    """Add the submitted counts to today's entry (don't overwrite).
    The form logs *new* reviews — blank fields mean 'no change for this person',
    not 'reset to zero'. So we merge by summing each person's count into
    today's existing entry, then recompute Team Total and YTD cumulatives."""
    history   = get_reviews_history()
    today_str = date.today().isoformat()
    today_yr  = date.today().year

    # Pull today's existing entry (if any) and pop it from history
    existing  = next((h for h in history if h.get("date") == today_str), None)
    history   = [h for h in history if h.get("date") != today_str]
    base      = (existing or {}).get("counts", {})

    # Merge: add new counts on top of today's existing per-person counts
    merged = {}
    for person in REVIEW_TEAM_MEMBERS:
        if person == "Team Total":
            continue
        merged[person] = int(base.get(person, 0)) + int(counts_by_person.get(person, 0))
    merged["Team Total"] = sum(v for p, v in merged.items() if p != "Team Total")

    # Recompute YTD cumulative from all prior history + today's merged counts
    cumulative = {}
    for person in REVIEW_TEAM_MEMBERS:
        ytd = sum(h.get("counts",{}).get(person,0)
                  for h in history if str(h.get("date",""))[:4] == str(today_yr))
        cumulative[person] = ytd + merged.get(person, 0)

    history.append({"date": today_str, "year": today_yr,
                     "counts": merged, "cumulative": cumulative})
    history.sort(key=lambda x: x["date"])
    save_json(REVIEWS_FILE, history)
    return cumulative

def get_reviews_chart_data():
    history     = get_reviews_history()
    yr          = date.today().year
    ytd         = [h for h in history if h.get("year") == yr]
    team_counts = [h.get("counts",{}).get("Team Total",0) for h in ytd]
    team_cum    = [h.get("cumulative",{}).get("Team Total",0) for h in ytd]
    latest_cum  = ytd[-1].get("cumulative",{}) if ytd else {}
    return {
        "labels": [h["date"] for h in ytd], "team_counts": team_counts,
        "team_cum": team_cum, "ytd_total": team_cum[-1] if team_cum else 0,
        "latest_cum": latest_cum,
    }

# ─── History Chart Data ───────────────────────────────────────────────────────

def get_history_chart_data():
    history = load_json(KPI_HISTORY_FILE, [])
    if not history:
        return {"labels":[],"delinq_trend":[],"collection_trend":[],
                "occ_trend":[],"pre_trend":[],"completion_trend":[],
                "wo_trend":[],"pm_trends":{pm:[] for pm in pm_group_label_list()}}
    return {
        "labels":           [h["date"] for h in history],
        "delinq_trend":     [h.get("delinquency",{}).get("total_delinquent",0) for h in history],
        "collection_trend": [h.get("delinquency",{}).get("collection_pct",0)   for h in history],
        "occ_trend":        [h.get("leasing",{}).get("occ_pct",0)              for h in history],
        "pre_trend":        [h.get("leasing",{}).get("pre_leased_pct",0)       for h in history],
        "completion_trend": [h.get("maintenance",{}).get("pct_completed",0)    for h in history],
        "wo_trend":         [h.get("maintenance",{}).get("total_created",0)    for h in history],
        "pm_trends": {
            pm: [h.get("delinquency",{}).get("pm_breakdown",{}).get(pm,{}).get("total_delinquent",0)
                 for h in history]
            for pm in pm_group_label_list()
        },
    }


def get_wo_weekly_trend(weeks_back=12):
    """Build week-over-week WO trend from raw data (preferred) or cache wo_detail (fallback)."""
    raw      = load_json(RAW_FILE,   None)
    cache    = load_json(CACHE_FILE, None)
    wo_rows  = []
    date_key = "created_at"

    if raw and raw.get("work_orders"):
        wo_rows  = raw["work_orders"]
        date_key = "created_at"
    elif cache and cache.get("maintenance", {}).get("wo_detail"):
        # Fallback: cache wo_detail uses 'created' and 'status' (already-cleaned)
        wo_rows  = cache["maintenance"]["wo_detail"]
        date_key = "created"

    today       = date.today()
    monday_this = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(weeks_back - 1, -1, -1):
        wk_start = monday_this - timedelta(weeks=i)
        wk_end   = wk_start + timedelta(days=6)
        weeks.append((wk_start, wk_end))

    labels, created, completed, pct = [], [], [], []
    for wk_start, wk_end in weeks:
        wk_rows = filter_by_range(wo_rows, date_key, wk_start, wk_end) if wo_rows else []
        c       = len(wk_rows)
        cc      = sum(1 for r in wk_rows if r.get("status") in COMPLETED_STATUSES)
        labels.append(wk_start.strftime("%-m/%-d"))
        created.append(c)
        completed.append(cc)
        pct.append(round(cc / c * 100, 1) if c > 0 else 0)
    return {"labels": labels, "created": created, "completed": completed, "pct_completed": pct}

# ─── Routes ───────────────────────────────────────────────────────────────────

# ─── Marketing & Showings (RentEngine API) ───────────────────────────────────

def get_rentengine_token():
    return os.environ.get("RENTENGINE_API_TOKEN", "")

def _re_request(path, params=None, retries=4):
    """GET a RentEngine endpoint. Retries on 429 with backoff; raises with the
    status + body on any other non-2xx so failures are visible (not swallowed)."""
    token = get_rentengine_token()
    if not token:
        raise ValueError("RentEngine API token not configured.")
    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    url     = f"{RENTENGINE_BASE}{path}"
    for attempt in range(retries):
        r = requests.get(url, headers=headers, params=params or {}, timeout=60)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 2 ** attempt))
            logger.warning(f"RentEngine 429 on {path}, waiting {wait}s")
            time.sleep(min(wait, 30))
            continue
        if not (200 <= r.status_code < 300):
            raise requests.HTTPError(f"{r.status_code} on {path}: {r.text[:300]}")
        return r.json()
    raise requests.HTTPError(f"RentEngine rate-limited after {retries} attempts: {path}")

def _re_unwrap(data):
    """RentEngine returns either a bare list/object or an envelope {'data': ...}."""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data

def re_paginated(path, params=None):
    """Fetch every page of a list endpoint (limit + 0-indexed page_number).
    Stops when a page returns fewer than the limit; hard-capped at RE_MAX_PAGES."""
    out = []
    for page in range(RE_MAX_PAGES):
        q = dict(params or {})
        q["limit"]       = RE_PAGE_LIMIT
        q["page_number"] = page
        items = _re_unwrap(_re_request(path, q)) or []
        if not isinstance(items, list):
            items = []
        out.extend(items)
        if len(items) < RE_PAGE_LIMIT:
            break
    return out

def re_unit_report(unit_id, start_iso, end_iso):
    """Per-unit leasing-performance report for the window."""
    data = _re_request(f"/reporting/leasing-performance/units/{unit_id}",
                       {"start": start_iso, "end": end_iso})
    rep  = _re_unwrap(data)
    return rep if isinstance(rep, dict) else {}

def _norm_label(s):
    """Trim + Title-Case so casing variants merge (e.g. 'zillow' == 'Zillow')."""
    return " ".join(str(s or "").strip().split()).title() or "Unknown"

def _unit_address(u):
    addr = u.get("address") or {}
    formatted = addr.get("formatted_address") if isinstance(addr, dict) else None
    return formatted or f"#{u.get('id')}"

def _median(nums):
    s = sorted(nums)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return round(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2, 1)

def fetch_marketing():
    """Pull RentEngine data over a trailing 12-month window and compute the
    Marketing & Showings metrics. Slow (one report call per unit) — callers
    should rely on the 10-minute cache + background refresh, not call directly."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    end       = datetime.utcnow()
    start     = end - timedelta(days=365)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso   = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    units      = re_paginated("/units")
    prospects  = re_paginated("/prospects",
                              {"created_after": start_iso, "created_before": end_iso})
    app_groups = re_paginated("/rental_application_groups",
                              {"created_after": start_iso, "created_before": end_iso})

    reports = {}
    with ThreadPoolExecutor(max_workers=RE_UNIT_CONCURRENCY) as ex:
        futs = {ex.submit(re_unit_report, u.get("id"), start_iso, end_iso): u.get("id")
                for u in units if u.get("id") is not None}
        for f in as_completed(futs):
            uid = futs[f]
            try:
                reports[uid] = f.result() or {}
            except Exception as e:
                logger.warning(f"RentEngine unit report {uid} failed: {scrub_creds(e)}")
                reports[uid] = {}

    return compute_marketing(units, prospects, app_groups, reports, start_iso, end_iso)

def compute_marketing(units, prospects, app_groups, reports, start_iso, end_iso):
    def num(v):
        try:    return float(v)
        except: return 0.0

    units_detail   = []
    on_market_doms = []
    sum_sched = sum_comp = sum_calls = sum_texts = 0
    total_vacant  = 0
    health_counts = {}

    for u in units:
        uid       = u.get("id")
        rep       = reports.get(uid, {}) or {}
        status    = u.get("status") or "—"
        on_market = not re.search(r"leased", str(status), re.I)   # ignore is_occupied (it lags)
        if not bool(u.get("is_occupied")):
            total_vacant += 1
        dom    = num(rep.get("days_on_market"))
        sched  = int(num(rep.get("showings_scheduled")))
        comp   = int(num(rep.get("showings_completed")))
        calls  = int(num(rep.get("total_calls")))
        texts  = int(num(rep.get("outbound_texts")))
        health = _norm_label(rep.get("property_health") or u.get("property_health"))

        sum_sched += sched; sum_comp += comp; sum_calls += calls; sum_texts += texts

        if on_market:
            on_market_doms.append(dom)
            health_counts[health] = health_counts.get(health, 0) + 1

        units_detail.append({
            "id":        uid,
            "address":   _unit_address(u),
            "status":    status,
            "on_market": on_market,
            "health":    health,
            "days":      round(dom, 1),
            "scheduled": sched,
            "completed": comp,
            "rate":      round(comp / sched * 100, 1) if sched else 0.0,
            "calls":     calls,
            "texts":     texts,
        })

    units_on_market = sum(1 for d in units_detail if d["on_market"])
    avg_dom         = round(sum(on_market_doms) / len(on_market_doms), 1) if on_market_doms else 0.0
    median_dom      = _median(on_market_doms)
    completion_rate = round(sum_comp / sum_sched * 100, 1) if sum_sched else 0.0
    new_prospects   = len(prospects)
    applications    = len(app_groups)

    # New prospects grouped by normalized source, descending
    src_counts       = {}
    prospects_detail = []
    for p in prospects:
        src = _norm_label(p.get("source"))
        src_counts[src] = src_counts.get(src, 0) + 1
        prospects_detail.append({
            "name":    p.get("name") or "—",
            "source":  src,
            "created": str(p.get("created_at") or "")[:10],
        })
    prospects_by_source = sorted(
        [{"source": k, "count": v} for k, v in src_counts.items()],
        key=lambda x: x["count"], reverse=True)

    # Property-health donut: on-market units by health + Off-Market bucket
    property_health = sorted(
        [{"label": k, "count": v} for k, v in health_counts.items()],
        key=lambda x: x["count"], reverse=True)
    off_market = max(total_vacant - units_on_market, 0)
    if off_market:
        property_health.append({"label": "Off-Market", "count": off_market})

    funnel = [
        {"stage": "Prospects",          "value": new_prospects},
        {"stage": "Showings Scheduled", "value": sum_sched},
        {"stage": "Showings Completed", "value": sum_comp},
        {"stage": "Applications",       "value": applications},
    ]

    apps_detail = [{
        "name":    a.get("name") or a.get("applicant_name") or f"#{a.get('id')}",
        "created": str(a.get("created_at") or "")[:10],
    } for a in app_groups]

    return {
        "window":            {"start": start_iso, "end": end_iso},
        "target_completion": SHOWING_COMPLETION_TARGET,
        "kpis": {
            "avg_dom":            avg_dom,
            "median_dom":         median_dom,
            "units_on_market":    units_on_market,
            "completion_rate":    completion_rate,
            "new_prospects":      new_prospects,
            "showings_scheduled": sum_sched,
            "showings_completed": sum_comp,
            "total_calls":        sum_calls,
            "outbound_texts":     sum_texts,
            "applications":       applications,
            "total_vacant":       total_vacant,
            "total_units":        len(units),
        },
        "prospects_by_source": prospects_by_source,
        "property_health":     property_health,
        "funnel":              funnel,
        "units_detail":        sorted(units_detail, key=lambda d: d["days"], reverse=True),
        "prospects_detail":    sorted(prospects_detail, key=lambda d: d["created"], reverse=True),
        "applications_detail": sorted(apps_detail, key=lambda d: d["created"], reverse=True),
    }

def _refresh_marketing():
    global _marketing_refreshing
    try:
        data = fetch_marketing()
        with _marketing_lock:
            _marketing_cache["data"]       = data
            _marketing_cache["fetched_at"] = time.time()
            _marketing_cache["error"]      = None
        logger.info("Marketing data refreshed.")
    except Exception as e:
        err = str(scrub_creds(e))
        logger.error(f"Marketing refresh failed: {err}")
        with _marketing_lock:
            _marketing_cache["error"]      = err
            _marketing_cache["fetched_at"] = time.time()
    finally:
        with _marketing_lock:
            _marketing_refreshing = False

def get_marketing(force=False):
    """Return cached marketing data, kicking a background refresh when missing
    or older than the TTL. Never blocks the request on the slow RentEngine pull."""
    global _marketing_refreshing
    now = time.time()
    with _marketing_lock:
        data       = _marketing_cache["data"]
        fetched_at = _marketing_cache["fetched_at"]
        error      = _marketing_cache["error"]
        age        = now - fetched_at
        stale      = data is None or age >= MARKETING_TTL_SECONDS
        kick       = (force or stale) and not _marketing_refreshing
        if kick:
            _marketing_refreshing = True
        refreshing = _marketing_refreshing
    if kick:
        threading.Thread(target=_refresh_marketing, daemon=True).start()
    return {
        "ready":      data is not None,
        "data":       data,
        "fetched_at": fetched_at,
        "refreshing": refreshing,
        "stale":      data is not None and age >= MARKETING_TTL_SECONDS,
        "error":      error,
    }

RANGE_LABELS = {
    "today":        "Today",
    "last_7_days":  "Last 7 Days",
    "last_30_days": "Last 30 Days",
    "last_90_days": "Last 90 Days",
    "ytd":          "Year to Date",
    "last_year":    "Last Year",
    "all_time":     "All Time",
}

@app.route("/")
def dashboard():
    cache          = load_json(CACHE_FILE, None)
    chart_data     = get_history_chart_data()
    chart_data["wo_weekly"] = get_wo_weekly_trend()
    reviews_data   = get_reviews_chart_data()
    cid, _, _      = get_creds()
    selected_range = request.args.get("range", "last_30_days")
    active_tab     = request.args.get("tab", "leasing")
    custom_from    = request.args.get("from", "")
    custom_to      = request.args.get("to",   "")
    if selected_range == "custom" and custom_from and custom_to:
        selected_range = f"custom:{custom_from}:{custom_to}"
        if cache:
            raw = load_json(RAW_FILE, None)
            if raw:
                cache = build_cache(raw, selected_range)
    return render_template("dashboard.html",
        cache=cache, chart_data=chart_data, reviews_data=reviews_data,
        has_creds=bool(cid), selected_range=selected_range, active_tab=active_tab,
        custom_from=custom_from, custom_to=custom_to,
        range_labels=RANGE_LABELS, property_groups=pm_group_label_list(),
        team_members=REVIEW_TEAM_MEMBERS, sync_in_progress=sync_in_progress,
    )

VALID_RANGE_KEYS = set(["today","last_7_days","last_30_days","last_90_days",
                        "ytd","last_year","all_time"])

def validate_range_key(range_key):
    """Return (ok: bool, normalized_key_or_error_msg: str)."""
    if not isinstance(range_key, str) or not range_key:
        return False, "Missing range_key."
    if range_key in VALID_RANGE_KEYS:
        return True, range_key
    if range_key.startswith("custom:"):
        parts = range_key.split(":")
        if len(parts) != 3:
            return False, "Custom range must look like custom:YYYY-MM-DD:YYYY-MM-DD."
        try:
            df = datetime.strptime(parts[1], "%Y-%m-%d").date()
            dt = datetime.strptime(parts[2], "%Y-%m-%d").date()
        except ValueError:
            return False, "Custom range dates must be YYYY-MM-DD."
        if df > dt:
            return False, "'From' date must be on or before 'To' date."
        return True, range_key
    return False, f"Unknown range_key: {range_key}"

@app.route("/sync", methods=["POST"])
def sync():
    range_key = (request.json or {}).get("range_key", "last_30_days")
    ok, val = validate_range_key(range_key)
    if not ok:
        return jsonify({"ok": False, "error": val})
    if sync_in_progress:
        return jsonify({"ok": False, "error": "Sync already in progress"})
    sync_in_background(val)
    return jsonify({"ok": True})

@app.route("/filter", methods=["POST"])
def filter_data():
    """Re-filter already-stored raw data for a new range. No API calls."""
    range_key = (request.json or {}).get("range_key", "last_30_days")
    ok, val = validate_range_key(range_key)
    if not ok:
        return jsonify({"ok": False, "error": val})
    raw = load_json(RAW_FILE, None)
    if not raw:
        return jsonify({"ok": False, "error": "No data synced yet. Please sync first."})
    try:
        cache = build_cache(raw, val)
        return jsonify({"ok": True, "synced_at": cache["synced_at"], "range_key": val})
    except Exception as e:
        return jsonify({"ok": False, "error": scrub_creds(e)})

@app.route("/sync-status")
def sync_status():
    cache = load_json(CACHE_FILE, None)
    raw   = load_json(RAW_FILE, None)
    return jsonify({
        "in_progress": sync_in_progress,
        "synced_at":   raw.get("synced_at") if raw else None,
        "range_key":   active_range_key,
        "has_raw":     raw is not None,
    })

@app.route("/marketing")
def marketing():
    force = request.args.get("force") == "1"
    return jsonify(get_marketing(force=force))

@app.route("/log-reviews", methods=["POST"])
def log_reviews():
    data = request.json or {}
    try:
        counts = {p: int(data.get(p, 0) or 0) for p in REVIEW_TEAM_MEMBERS}
        if counts.get("Team Total", 0) == 0:
            counts["Team Total"] = sum(counts[p] for p in REVIEW_TEAM_MEMBERS if p != "Team Total")
        cumulative   = save_review_entry(counts)
        reviews_data = get_reviews_chart_data()
        return jsonify({"ok": True, "cumulative": cumulative, "reviews_data": reviews_data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.template_filter("fmt_currency")
def fmt_currency(value):
    try:    return "${:,.0f}".format(float(value))
    except: return "—"

@app.template_filter("fmt_pct")
def fmt_pct(value):
    try:    return "{:.1f}%".format(float(value))
    except: return "—"

# ─── Scheduler ────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(lambda: do_sync("last_30_days"), "cron", hour=6, minute=0)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
