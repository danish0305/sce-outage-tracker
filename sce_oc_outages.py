"""
SCE Orange County outage scraper.

Pulls SCE's live outage data directly from the ArcGIS FeatureService discovered in Step 1
(no browser / no auth needed, confirmed in Step 2), filters to Orange County, CA, and
writes a CSV.

Endpoint (Esri ArcGIS, self-hosted on Esri Managed Cloud Services):
  https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/outage/MapServer/{layer}/query
    layer 0 = "Outage"           (active / unplanned)
    layer 1 = "Scheduled Outage" (planned maintenance)

Resolution note: the feed exposes per-outage POINT locations (lat/lng), city/ZIP/county,
and exact customer counts. The `Circuit_Number` field exists in the schema but SCE leaves
it null in the public feed, so true circuit-level naming is NOT available (verified Step 2).

Usage:
  python sce_oc_outages.py                 # writes orange_county_outages_latest.csv (+ history append)
  python sce_oc_outages.py --out foo.csv   # custom output path
  python sce_oc_outages.py --no-history    # skip history append
"""

import argparse
import csv
import datetime as dt
import pathlib
import sys
import time

import requests

BASE = "https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/outage/MapServer"
LAYERS = {0: "active", 1: "scheduled"}          # layer id -> feed label
DEFAULT_COUNTIES = ["ORANGE"]                    # SCE tags county directly via CountyName
PAGE = 2000                                      # default page size (== service maxRecordCount)


def build_where(counties):
    """Build a `UPPER(CountyName) IN (...)` clause from a list of county names."""
    quoted = ",".join("'" + c.strip().upper().replace("'", "''") + "'" for c in counties)
    return f"UPPER(CountyName) IN ({quoted})"

# Fields we ask the service for (a superset of what we output, for flexibility).
OUTFIELDS = ",".join([
    "OBJECTID", "IncidentId", "OanNo", "IncidentType", "Status", "OutageType",
    "JobStatus", "NoOfAffectedCust_Inci", "MemoCauseCd", "MemoCauseCdDesc",
    "CrewStatusCd", "CrewStatusCdDesc", "ResultCdDesc", "ProblemCode",
    "CountyName", "CityName", "ZipcodeName", "DistrictNo", "SectorNo",
    "Circuit_Number", "RO_Group_Numbers", "Future_RO_Groups",
    "OutageStartDateTime", "ERT", "ERT_LOC", "LastChngDateTime", "PublishDate",
])

# Orange County bounding box (from the brief) — used only as a post-hoc sanity check,
# NOT as the primary filter (CountyName is authoritative).
OC_BBOX = {"lat_min": 33.35, "lat_max": 33.95, "lng_min": -118.13, "lng_max": -117.4}

# CSV columns, in order.
COLUMNS = [
    "outage_id", "oan_no", "feed", "status", "outage_type",
    "customers_affected", "cause", "circuit_name",
    "city", "county", "zip", "latitude", "longitude",
    "start_time_utc", "etr", "crew_status", "result", "problem_code",
    "incident_type", "district_no", "sector_no",
    "last_change", "publish_date", "source_layer", "retrieved_utc",
]


class SCEOutageError(Exception):
    pass


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "SCE-OC-outage-tracker/1.0 (+research)"})
    return s


def _epoch_ms_to_iso_utc(v):
    if v in (None, ""):
        return ""
    try:
        return dt.datetime.fromtimestamp(int(v) / 1000, tz=dt.timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OSError):
        return str(v)


def fetch_layer(session, layer, where, timeout=40, retries=3):
    """Query one MapServer layer with pagination. Returns list of ArcGIS features."""
    feats = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": OUTFIELDS,
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": PAGE,
            "orderByFields": "OBJECTID",
            "f": "json",
        }
        last_err = None
        for attempt in range(retries):
            try:
                r = session.get(f"{BASE}/{layer}/query", params=params, timeout=timeout)
                r.raise_for_status()
                j = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        else:
            raise SCEOutageError(
                f"Layer {layer} query failed after {retries} tries: {last_err}")

        if "error" in j:
            raise SCEOutageError(f"ArcGIS error on layer {layer}: {j['error']}")

        batch = j.get("features", [])
        feats.extend(batch)
        if j.get("exceededTransferLimit") and batch:
            offset += len(batch)
            continue
        if len(batch) == PAGE:          # be safe even if flag absent
            offset += len(batch)
            continue
        break
    return feats


def parse_feature(feat, layer, retrieved_utc):
    a = feat.get("attributes", {}) or {}
    g = feat.get("geometry") or {}
    lat = g.get("y")
    lng = g.get("x")
    return {
        "outage_id": a.get("IncidentId"),
        "oan_no": a.get("OanNo"),
        "feed": LAYERS.get(layer, str(layer)),
        "status": a.get("Status"),
        "outage_type": a.get("OutageType"),
        "customers_affected": a.get("NoOfAffectedCust_Inci"),
        "cause": a.get("MemoCauseCdDesc"),
        "circuit_name": a.get("Circuit_Number"),   # ~always null in public feed
        "city": (a.get("CityName") or "").strip(),
        "county": (a.get("CountyName") or "").strip(),
        "zip": a.get("ZipcodeName"),
        "latitude": lat,
        "longitude": lng,
        "start_time_utc": _epoch_ms_to_iso_utc(a.get("OutageStartDateTime")),
        "etr": a.get("ERT"),
        "crew_status": a.get("CrewStatusCdDesc"),
        "result": a.get("ResultCdDesc"),
        "problem_code": a.get("ProblemCode"),
        "incident_type": a.get("IncidentType"),
        "district_no": a.get("DistrictNo"),
        "sector_no": a.get("SectorNo"),
        "last_change": a.get("LastChngDateTime"),
        "publish_date": a.get("PublishDate"),
        "source_layer": f"{layer}:{LAYERS.get(layer, '')}",
        "retrieved_utc": retrieved_utc,
    }


def collect(counties=DEFAULT_COUNTIES):
    """Return (rows, stats). Rows are dicts keyed by COLUMNS."""
    session = _session()
    where = build_where(counties)
    retrieved_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, stats = [], {}
    for layer in LAYERS:
        feats = fetch_layer(session, layer, where)
        stats[LAYERS[layer]] = len(feats)
        for f in feats:
            rows.append(parse_feature(f, layer, retrieved_utc))
    stats["total"] = len(rows)
    return rows, stats, retrieved_utc


def write_csv(rows, path):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def append_history(rows, path):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def sanity_report(rows, allowed_counties=DEFAULT_COUNTIES):
    """Print a quick QA summary; return dict of flags for programmatic checks.

    Note: one physical incident can legitimately appear as several rows (per ZIP/city),
    so a non-zero duplicate `outage_id` count is informational, not an error. The OC bbox
    check is likewise informational (rows in other counties fall outside it by design)."""
    n = len(rows)
    allowed = {c.strip().upper() for c in allowed_counties}
    ids = [r["outage_id"] for r in rows]
    dup = n - len(set(ids))
    non_match = [r for r in rows if (r["county"] or "").upper() not in allowed]
    have_geo = [r for r in rows if r["latitude"] is not None and r["longitude"] is not None]
    in_oc_bbox = [
        r for r in have_geo
        if (OC_BBOX["lat_min"] <= r["latitude"] <= OC_BBOX["lat_max"]
            and OC_BBOX["lng_min"] <= r["longitude"] <= OC_BBOX["lng_max"])
    ]
    circuits = [r for r in rows if r["circuit_name"]]
    cities = sorted({r["city"] for r in rows if r["city"]})
    by_county = {}
    for r in rows:
        by_county[(r["county"] or "").upper()] = by_county.get((r["county"] or "").upper(), 0) + 1

    print("\n=== SANITY REPORT ===")
    print(f"rows: {n}")
    print(f"rows per county: {by_county}")
    print(f"distinct outage incidents (unique outage_id): {len(set(ids))} "
          f"(duplicate rows across ZIP/city: {dup}, informational)")
    print(f"rows tagged with an unexpected county (should be 0): {len(non_match)}")
    print(f"rows with geometry: {len(have_geo)}/{n}")
    print(f"rows within OC bbox (informational): {len(in_oc_bbox)}")
    print(f"rows with a non-null circuit_name: {len(circuits)}")
    print(f"customers_affected populated: "
          f"{sum(1 for r in rows if r['customers_affected'] not in (None,''))}/{n}")
    print(f"distinct cities ({len(cities)}): {cities}")
    return {
        "rows": n, "unique_incidents": len(set(ids)), "duplicates": dup,
        "unexpected_county": len(non_match), "with_circuit": len(circuits),
        "by_county": by_county,
    }


def main():
    global PAGE
    ap = argparse.ArgumentParser()
    here = pathlib.Path(__file__).parent
    ap.add_argument("--out", default=str(here / "orange_county_outages_latest.csv"))
    ap.add_argument("--history", default=str(here / "orange_county_outages_history.csv"))
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--counties", default=",".join(DEFAULT_COUNTIES),
                    help="Comma-separated county names, e.g. \"ORANGE,LOS ANGELES\"")
    ap.add_argument("--page-size", type=int, default=PAGE,
                    help="Rows per ArcGIS request; lower to stress-test pagination")
    args = ap.parse_args()

    counties = [c.strip() for c in args.counties.split(",") if c.strip()]
    PAGE = args.page_size

    try:
        rows, stats, retrieved = collect(counties)
    except SCEOutageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"Retrieved {retrieved} | counties={counties} | page_size={PAGE} | "
          f"active={stats['active']} scheduled={stats['scheduled']} total={stats['total']}")

    out = write_csv(rows, args.out)
    print(f"Wrote {out}")
    if not args.no_history:
        h = append_history(rows, args.history)
        print(f"Appended {len(rows)} rows to {h}")

    sanity_report(rows, counties)
    return 0


if __name__ == "__main__":
    sys.exit(main())
