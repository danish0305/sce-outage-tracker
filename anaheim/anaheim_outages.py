"""
Anaheim Public Utilities (APU) outage scraper.

Endpoint (validated: plain HTTP, no auth/cookies, returns GeoJSON):
  GET https://gis.anaheim.net/electricoutages/ActiveIncidents.cshtml

CRITICAL RESOLUTION NOTE (confirmed in discovery): each GeoJSON feature is ONE AFFECTED
SERVICE POINT, not one outage. 19 features shared a single ID=292655. So:
  * outages     = distinct ID values
  * points      = feature count  (a useful customers-affected PROXY, since there is no
                                  customer-count field in this feed)
We therefore emit TWO files: one row per incident (aggregated) and one row per point.

Confirmed semantics:
  ID        -> STABLE across polls (verified 3 polls, single incident observed)
  startTime -> "M/D/YYYY h:mm AM" PACIFIC LOCAL (verified against live clocks)
  endTime   -> same format; ESTIMATED restoration (was in the future), not confirmed
  No cause, no circuit/substation/feeder, no customer count fields exist.

The historical endpoint PreviousIncidents.cshtml is NOT used: it returns HTTP 500 for every
date range (server-side broken) and its UI toggle is hidden by the city. No history available.
"""

import argparse, csv, datetime as dt, json, pathlib, re, sys, time
import requests

URL = "https://gis.anaheim.net/electricoutages/ActiveIncidents.cshtml"

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:
    PACIFIC = None

INCIDENT_COLUMNS = ["utility", "incident_id", "type", "affected_points",
                    "start_local_raw", "start_utc", "etr_local_raw", "etr_utc",
                    "centroid_lat", "centroid_lng",
                    "bbox_lat_min", "bbox_lng_min", "bbox_lat_max", "bbox_lng_max",
                    "retrieved_utc"]
POINT_COLUMNS = ["utility", "incident_id", "type", "latitude", "longitude",
                 "start_local_raw", "start_utc", "etr_local_raw", "etr_utc", "retrieved_utc"]


def pacific_to_utc_str(s):
    """'8/27/2026 9:51 AM' (Pacific) -> true UTC ISO."""
    if not s:
        return ""
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M"):
        try:
            naive = dt.datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
        if PACIFIC is None:
            return naive.strftime("%Y-%m-%dT%H:%M:%S(local)")
        return naive.replace(tzinfo=PACIFIC).astimezone(dt.timezone.utc)\
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def fetch(session, retries=3, timeout=30):
    last = None
    for attempt in range(retries):
        try:
            r = session.get(URL, timeout=timeout); r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e; time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"ActiveIncidents failed after {retries} tries: {last}")


def write_csv(rows, path, cols):
    path = pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    return path


def append_csv(rows, path, cols):
    if not rows: return None
    path = pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new: w.writeheader()
        w.writerows(rows)
    return path


def main():
    ap = argparse.ArgumentParser()
    here = pathlib.Path(__file__).parent
    ap.add_argument("--out", default=str(here / "anaheim_outages_latest.csv"))
    ap.add_argument("--history", default=str(here / "anaheim_outages_history.csv"))
    ap.add_argument("--points-out", default=str(here / "anaheim_points_latest.csv"))
    ap.add_argument("--points-history", default=str(here / "anaheim_points_history.csv"))
    args = ap.parse_args()

    retrieved = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = requests.Session(); s.headers.update({"User-Agent": "LA-muni-outage-tracker/1.0 (+research)"})
    try:
        gj = fetch(s)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2

    feats = gj.get("features", []) if isinstance(gj, dict) else []
    points, groups = [], {}
    for f in feats:
        p = f.get("properties", {}) or {}
        c = (f.get("geometry") or {}).get("coordinates") or [None, None]
        lng, lat = (c[0], c[1]) if len(c) >= 2 else (None, None)
        inc = p.get("ID")
        row = {"utility": "anaheim", "incident_id": inc, "type": p.get("type"),
               "latitude": lat, "longitude": lng,
               "start_local_raw": p.get("startTime"), "start_utc": pacific_to_utc_str(p.get("startTime")),
               "etr_local_raw": p.get("endTime"), "etr_utc": pacific_to_utc_str(p.get("endTime")),
               "retrieved_utc": retrieved}
        points.append(row)
        g = groups.setdefault(inc, {"props": p, "lats": [], "lngs": []})
        if lat is not None: g["lats"].append(lat); g["lngs"].append(lng)

    incidents = []
    for inc, g in groups.items():
        p = g["props"]; lats, lngs = g["lats"], g["lngs"]
        incidents.append({
            "utility": "anaheim", "incident_id": inc, "type": p.get("type"),
            "affected_points": len(lats),          # customers-affected proxy
            "start_local_raw": p.get("startTime"), "start_utc": pacific_to_utc_str(p.get("startTime")),
            "etr_local_raw": p.get("endTime"), "etr_utc": pacific_to_utc_str(p.get("endTime")),
            "centroid_lat": round(sum(lats) / len(lats), 6) if lats else None,
            "centroid_lng": round(sum(lngs) / len(lngs), 6) if lngs else None,
            "bbox_lat_min": min(lats) if lats else None, "bbox_lng_min": min(lngs) if lngs else None,
            "bbox_lat_max": max(lats) if lats else None, "bbox_lng_max": max(lngs) if lngs else None,
            "retrieved_utc": retrieved,
        })

    print(f"Retrieved {retrieved} | Anaheim APU | features(points)={len(points)} "
          f"distinct incidents={len(incidents)}")
    print(f"Wrote {write_csv(incidents, args.out, INCIDENT_COLUMNS)}")
    print(f"Wrote {write_csv(points, args.points_out, POINT_COLUMNS)}")
    hi = append_csv(incidents, args.history, INCIDENT_COLUMNS)
    hp = append_csv(points, args.points_history, POINT_COLUMNS)
    print(f"History: incidents {'+'+str(len(incidents)) if hi else '(none)'}, "
          f"points {'+'+str(len(points)) if hp else '(none)'}")

    print("\n=== ANAHEIM SANITY REPORT ===")
    print("NOTE: features are AFFECTED SERVICE POINTS, not outages -- outages = distinct ID.")
    print("      No cause / circuit / customer-count fields exist; point count is the proxy.")
    print(f"points: {len(points)} | incidents: {len(incidents)}")
    if incidents:
        for i in incidents:
            print(f"  ID={i['incident_id']} points={i['affected_points']} "
                  f"start_local={i['start_local_raw']} -> start_utc={i['start_utc']} "
                  f"etr_utc={i['etr_utc']} centroid=({i['centroid_lat']},{i['centroid_lng']})")
        print(f"start_utc populated: {sum(1 for i in incidents if i['start_utc'])}/{len(incidents)}")
    else:
        print("No active outages right now (valid empty result).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
