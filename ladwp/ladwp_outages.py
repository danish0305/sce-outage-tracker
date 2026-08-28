"""
LADWP (Los Angeles Dept. of Water & Power) outage scraper.

Source: public ArcGIS Online hosted feature layer used by LADWP's outage map
(discovered/validated Steps 1-2; no auth, no cookie needed):
  https://services2.arcgis.com/v0bIBBLIiGigCimX/ArcGIS/rest/services/PowerOutages_Data/FeatureServer/0/query

Field meanings (confirmed from the web map's customer-facing popup config):
  CITY_NAM           -> "Neighborhood"
  COUNT_IN_RANK      -> "Customers Out"           (live customers-affected count)
  ETR_DATETIME(_CHAR)-> "Estimated Restore Time"  (a FORECAST, not confirmed restoration)
  FAC_JOB_STATUS_NAM -> "Status"                  (e.g. CREWS WORKING)
  OUTAGE_RANK        -> "Outage #"                (per-neighborhood ordinal, NOT severity)
  COUNT_OTHERS       -> (not shown; observed always null)
  CENTROID_LAT/LNG   -> point location

Behavioral notes (from a 27-min poll):
  * OBJECTID is NOT stable -- the layer is republished each cycle with fresh OBJECTIDs,
    so there is no durable outage key. Each run is a point-in-time SNAPSHOT.
  * Restored outages simply disappear from the feed (no "restored" record).
  * The feed carries NO cause, NO start time, and NO circuit/feeder detail.

Geographic scope: the feed has no region/county field. LADWP serves both the LA metro
area (~lat 33.6-34.5) and the far-north Owens Valley (~lat 36.3-37.7). We classify each
row by latitude and default to LA-metro only.

Usage:
  python ladwp_outages.py                      # LA metro only (default)
  python ladwp_outages.py --region owens       # Owens Valley only
  python ladwp_outages.py --region all         # everything, tagged by region
"""

import argparse
import csv
import datetime as dt
import pathlib
import sys
import time

import requests

try:
    from zoneinfo import ZoneInfo            # Python 3.9+
    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:                            # pragma: no cover - fallback if tzdata missing
    from datetime import timezone, timedelta
    _PACIFIC = None

BASE = ("https://services2.arcgis.com/v0bIBBLIiGigCimX/ArcGIS/rest/services/"
        "PowerOutages_Data/FeatureServer/0")
PAGE = 2000                       # == service maxRecordCount
REGION_LAT_SPLIT = 35.5           # < split => LA metro; >= split => Owens Valley

OUTFIELDS = ",".join([
    "OBJECTID", "CITY_NAM", "OUTAGE_RANK", "COUNT_IN_RANK", "COUNT_OTHERS",
    "FAC_JOB_STATUS_NAM", "ETR_DATETIME", "ETR_DATETIME_CHAR",
    "CENTROID_LAT", "CENTROID_LNG",
])

COLUMNS = [
    "region", "neighborhood", "customers_out", "status", "outage_number",
    "etr_display", "etr_utc", "latitude", "longitude",
    "count_others", "objectid_snapshot", "retrieved_utc",
]


class LADWPError(Exception):
    pass


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "LADWP-outage-tracker/1.0 (+research)"})
    return s


def etr_display_to_utc(display):
    """Convert LADWP's 'Estimated Restore Time' display string to a TRUE UTC ISO stamp.

    `etr_display` (ETR_DATETIME_CHAR, e.g. "08/12/2026 02:00") is authoritative and is
    LADWP's customer-facing time in PACIFIC local. We must NOT trust the feed's numeric
    ETR_DATETIME epoch: it encodes that same wall-clock naively as UTC (the classic ArcGIS
    "local-time-stamped-as-UTC" trap), which is off by the Pacific offset.

    We parse the display string in America/Los_Angeles (so DST/PDT-vs-PST is handled
    automatically, correct year-round) and convert to UTC.
    e.g. "08/12/2026 02:00" (PDT, UTC-7) -> "2026-08-12T09:00:00Z".
    """
    if not display:
        return ""
    try:
        naive = dt.datetime.strptime(display.strip(), "%m/%d/%Y %H:%M")
    except (ValueError, TypeError):
        return ""  # unparseable -> leave blank rather than emit a wrong value
    if _PACIFIC is not None:
        local = naive.replace(tzinfo=_PACIFIC)
    else:  # fallback: America/Los_Angeles DST rules for the given date
        from datetime import timezone, timedelta
        local = naive.replace(tzinfo=timezone(timedelta(hours=-(7 if _is_pdt(naive) else 8))))
    return local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_pdt(naive):
    """Rough US DST test (2nd Sun Mar -> 1st Sun Nov) for the tzdata-missing fallback only."""
    y = naive.year
    mar = dt.date(y, 3, 8); mar += dt.timedelta(days=(6 - mar.weekday()) % 7)      # 2nd Sunday
    nov = dt.date(y, 11, 1); nov += dt.timedelta(days=(6 - nov.weekday()) % 7)     # 1st Sunday
    return dt.datetime.combine(mar, dt.time(2)) <= naive < dt.datetime.combine(nov, dt.time(2))


def classify_region(lat):
    if lat is None:
        return "UNKNOWN"
    return "OWENS_VALLEY" if lat >= REGION_LAT_SPLIT else "LA_METRO"


def fetch_all(session, timeout=40, retries=3):
    """Fetch every active-outage feature (paginated). Returns raw ArcGIS features."""
    feats, offset = [], 0
    while True:
        params = {
            "where": "1=1", "outFields": OUTFIELDS, "returnGeometry": "true",
            "outSR": "4326", "resultOffset": offset, "resultRecordCount": PAGE,
            "orderByFields": "OBJECTID", "f": "json",
        }
        last_err = None
        for attempt in range(retries):
            try:
                r = session.get(f"{BASE}/query", params=params, timeout=timeout)
                r.raise_for_status()
                j = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        else:
            raise LADWPError(f"query failed after {retries} tries: {last_err}")
        if "error" in j:
            raise LADWPError(f"ArcGIS error: {j['error']}")
        batch = j.get("features", [])
        feats.extend(batch)
        if j.get("exceededTransferLimit") and batch:
            offset += len(batch); continue
        if len(batch) == PAGE:
            offset += len(batch); continue
        break
    return feats


def parse_feature(feat, retrieved_utc):
    a = feat.get("attributes", {}) or {}
    g = feat.get("geometry") or {}
    lat = a.get("CENTROID_LAT", g.get("y"))
    lng = a.get("CENTROID_LNG", g.get("x"))
    return {
        "region": classify_region(lat),
        "neighborhood": (a.get("CITY_NAM") or "").strip(),
        "customers_out": a.get("COUNT_IN_RANK"),
        "status": a.get("FAC_JOB_STATUS_NAM"),
        "outage_number": a.get("OUTAGE_RANK"),
        "etr_display": a.get("ETR_DATETIME_CHAR"),   # authoritative (Pacific local)
        # True UTC derived from the Pacific display string, NOT from the feed's epoch
        # (which is local-time-stamped-as-UTC and would be wrong -- see etr_display_to_utc).
        "etr_utc": etr_display_to_utc(a.get("ETR_DATETIME_CHAR")),
        "latitude": lat,
        "longitude": lng,
        "count_others": a.get("COUNT_OTHERS"),
        # WARNING: OBJECTID is NOT a stable identifier. LADWP truncates & republishes this
        # layer every refresh, so the SAME physical outage gets a NEW OBJECTID each poll.
        # It must NEVER be used to track, join, or dedupe outages across polls -- it is
        # captured only as a within-snapshot row marker.
        "objectid_snapshot": a.get("OBJECTID"),
        "retrieved_utc": retrieved_utc,
    }


def collect(region="la"):
    session = _session()
    retrieved_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [parse_feature(f, retrieved_utc) for f in fetch_all(session)]
    want = {"la": "LA_METRO", "owens": "OWENS_VALLEY"}.get(region)
    if want:
        rows = [r for r in rows if r["region"] == want]
    return rows, retrieved_utc


def write_csv(rows, path):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


def append_history(rows, path):
    # Each run is a point-in-time snapshot (no stable ID), so we always append with
    # retrieved_utc when there are rows.
    if not rows:
        return None
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerows(rows)
    return path


def sanity_report(rows, region):
    n = len(rows)
    print("\n=== LADWP SANITY REPORT ===")
    print("NOTE: This feed has no stable outage ID — each run is an independent snapshot,")
    print("      not a continuation of previous rows (OBJECTID rotates every refresh).")
    print(f"region filter: {region} | rows: {n}")
    if n == 0:
        print("No active outages in scope right now (valid empty result).")
        return
    by_region = {}
    for r in rows:
        by_region[r["region"]] = by_region.get(r["region"], 0) + 1
    total_cust = sum((r["customers_out"] or 0) for r in rows)
    lats = [r["latitude"] for r in rows if r["latitude"] is not None]
    print(f"rows per region: {by_region}")
    print(f"total customers_out across scope: {total_cust}")
    print(f"customers_out populated: {sum(1 for r in rows if r['customers_out'] not in (None,''))}/{n}")
    print(f"etr_utc populated: {sum(1 for r in rows if r['etr_utc'])}/{n}")
    if lats:
        print(f"latitude range: {min(lats):.3f} .. {max(lats):.3f}")
    print(f"distinct neighborhoods: {sorted({r['neighborhood'] for r in rows if r['neighborhood']})}")


def main():
    global PAGE
    ap = argparse.ArgumentParser()
    here = pathlib.Path(__file__).parent
    ap.add_argument("--region", choices=["la", "owens", "all"], default="la",
                    help="la = LA metro (default); owens = Owens Valley; all = both")
    ap.add_argument("--out")
    ap.add_argument("--history")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--page-size", type=int, default=PAGE)
    args = ap.parse_args()
    PAGE = args.page_size

    out = args.out or str(here / f"ladwp_{args.region}_outages_latest.csv")
    hist = args.history or str(here / f"ladwp_{args.region}_outages_history.csv")

    try:
        rows, retrieved = collect(args.region)
    except LADWPError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"Retrieved {retrieved} | region={args.region} | rows={len(rows)}")
    p = write_csv(rows, out)
    print(f"Wrote {p}")
    if not args.no_history:
        h = append_history(rows, hist)
        print(f"History: {'appended '+str(len(rows))+' rows to '+str(h) if h else 'no rows (empty)'}")
    sanity_report(rows, args.region)
    return 0


if __name__ == "__main__":
    sys.exit(main())
