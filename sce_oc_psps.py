"""
SCE Orange County PSPS (Public Safety Power Shutoff) scraper.

Separate from sce_oc_outages.py because PSPS data is POLYGON-based (circuit-segment
boundaries) with a very different, PSPS-specific field set — so it gets its own CSV rather
than being forced into the point-outage row structure.

Service (same ArcGIS family validated in Steps 1-2; no auth / no cookie needed):
  https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/psps/MapServer/{layer}/query
    layer 0 = "PSPS Under Consideration"  (polygon)  -> psps_status "under_consideration"
    layer 1 = "PSPS Active"               (polygon)  -> psps_status "active"
    layer 2 = "PSPS Centroids"            (point)    -> NOT scraped here (map marker helper)

County filter: this service has NO `CountyName` field. It exposes `COUNTY` (a per-segment
county name) instead, so we filter with UPPER(COUNTY)='ORANGE'. Note PSPS polygons are per
circuit-segment; a segment crossing counties is flagged via `CktXcounty`. If SCE ever tags a
cross-county OC segment under a different county, a spatial filter against the OC bounding box
would be the fallback -- available here via --spatial.

Reality note: PSPS events only exist during high fire-risk / wind conditions. Outside those
windows (verified 2026-07-31: 0 rows on all layers system-wide) this CSV will be header-only.
That is a CORRECT result, not a failure -- the query still runs and returns valid JSON.

Usage:
  python sce_oc_psps.py                 # writes orange_county_psps_latest.csv (+ history)
  python sce_oc_psps.py --spatial       # add OC-bbox spatial filter (union with COUNTY match)
  python sce_oc_psps.py --no-history
"""

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import time

import requests

BASE = "https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/psps/MapServer"
LAYERS = {0: "under_consideration", 1: "active"}   # polygon event layers
DEFAULT_COUNTIES = ["ORANGE"]                      # this service uses COUNTY, not CountyName
PAGE = 2000


def build_where(counties):
    """Build a `UPPER(COUNTY) IN (...)` clause from a list of county names."""
    quoted = ",".join("'" + c.strip().upper().replace("'", "''") + "'" for c in counties)
    return f"UPPER(COUNTY) IN ({quoted})"

# Orange County bounding box (lng/lat) for the optional spatial fallback + sanity check.
OC_BBOX = {"lat_min": 33.35, "lat_max": 33.95, "lng_min": -118.13, "lng_max": -117.4}

OUTFIELDS = ",".join([
    "OBJECTID", "GlobalID", "CIRCUIT_NAME", "CIRCUIT_SEGMENT", "Poly_Type",
    "COUNTY", "COUNTY_FIPS", "CktCounty", "CktXcounty",
    "EventName", "EventStatus", "EventTag",
    "GrandTotal", "Unassigned_Residential", "Essential", "Major",
    "MedBaseline", "MedBaseline_Critical",
    "StartDate", "EndDate", "ERT", "DateDeEnergized",
    "UTC_StartDate", "UTC_EndDate", "UTC_ERT",
    "ERTNotes", "POCNotes", "PSPS_Details", "PSPS",
    "StatusActiveDt", "StatusArchDt", "StatusPrevDt", "PspsStatusTimeStamp",
])

# CSV columns for polygon PSPS records.
COLUMNS = [
    "psps_status", "event_name", "event_status", "event_tag",
    "circuit_name", "circuit_segment", "poly_type",
    "county", "county_fips", "ckt_county", "ckt_cross_county",
    "customers_total", "cust_residential", "cust_essential", "cust_major",
    "cust_med_baseline", "cust_med_baseline_critical",
    "start_utc", "end_utc", "ert", "date_deenergized_utc",
    "utc_start_raw", "utc_end_raw", "utc_ert_raw",
    "ert_notes", "poc_notes", "psps_details",
    "status_active_dt_utc", "status_timestamp_utc",
    "global_id", "centroid_lat", "centroid_lng",
    "bbox_lng_min", "bbox_lat_min", "bbox_lng_max", "bbox_lat_max",
    "geometry_rings_json", "source_layer", "retrieved_utc",
]


class PSPSError(Exception):
    pass


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "SCE-OC-PSPS-tracker/1.0 (+research)"})
    return s


def _epoch_ms_to_iso_utc(v):
    """Convert an ArcGIS epoch-ms date to ISO-UTC; pass through anything else."""
    if v in (None, ""):
        return ""
    try:
        iv = int(v)
    except (ValueError, TypeError):
        return str(v)
    if iv > 10_000_000_000:  # plausible epoch-ms (>~1970-04); smaller ints aren't dates
        try:
            return dt.datetime.fromtimestamp(iv / 1000, tz=dt.timezone.utc)\
                .strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError):
            return str(v)
    return str(v)


def _polygon_centroid_bbox(geometry):
    """Return (centroid_lat, centroid_lng, bbox) from Esri polygon rings (lng/lat coords)."""
    rings = (geometry or {}).get("rings") or []
    xs, ys = [], []
    for ring in rings:
        for pt in ring:
            if len(pt) >= 2:
                xs.append(pt[0]); ys.append(pt[1])
    if not xs:
        return None, None, (None, None, None, None)
    bbox = (min(xs), min(ys), max(xs), max(ys))
    return (sum(ys) / len(ys), sum(xs) / len(xs), bbox)


def fetch_layer(session, layer, where, spatial=False, timeout=40, retries=3):
    """Query one polygon PSPS layer (paginated). Returns list of ArcGIS features."""
    feats, offset = [], 0
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
        if spatial:
            # Union approach: also accept polygons intersecting the OC bbox regardless of
            # the COUNTY tag. ArcGIS OR isn't possible across attr+spatial in one call, so
            # we widen the attribute where to 1=1 and rely on the spatial envelope filter.
            params["where"] = "1=1"
            params["geometry"] = (f'{OC_BBOX["lng_min"]},{OC_BBOX["lat_min"]},'
                                  f'{OC_BBOX["lng_max"]},{OC_BBOX["lat_max"]}')
            params["geometryType"] = "esriGeometryEnvelope"
            params["inSR"] = "4326"
            params["spatialRel"] = "esriSpatialRelIntersects"

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
            raise PSPSError(f"PSPS layer {layer} failed after {retries} tries: {last_err}")

        if "error" in j:
            raise PSPSError(f"ArcGIS error on PSPS layer {layer}: {j['error']}")

        batch = j.get("features", [])
        feats.extend(batch)
        if j.get("exceededTransferLimit") and batch:
            offset += len(batch); continue
        if len(batch) == PAGE:
            offset += len(batch); continue
        break
    return feats


def parse_feature(feat, layer, retrieved_utc):
    a = feat.get("attributes", {}) or {}
    clat, clng, bbox = _polygon_centroid_bbox(feat.get("geometry"))
    return {
        "psps_status": LAYERS.get(layer, str(layer)),
        "event_name": a.get("EventName"),
        "event_status": a.get("EventStatus"),
        "event_tag": a.get("EventTag"),
        "circuit_name": a.get("CIRCUIT_NAME"),
        "circuit_segment": a.get("CIRCUIT_SEGMENT"),
        "poly_type": a.get("Poly_Type"),
        "county": a.get("COUNTY"),
        "county_fips": a.get("COUNTY_FIPS"),
        "ckt_county": a.get("CktCounty"),
        "ckt_cross_county": a.get("CktXcounty"),
        "customers_total": a.get("GrandTotal"),
        "cust_residential": a.get("Unassigned_Residential"),
        "cust_essential": a.get("Essential"),
        "cust_major": a.get("Major"),
        "cust_med_baseline": a.get("MedBaseline"),
        "cust_med_baseline_critical": a.get("MedBaseline_Critical"),
        "start_utc": _epoch_ms_to_iso_utc(a.get("StartDate")),
        "end_utc": _epoch_ms_to_iso_utc(a.get("EndDate")),
        "ert": _epoch_ms_to_iso_utc(a.get("ERT")),
        "date_deenergized_utc": _epoch_ms_to_iso_utc(a.get("DateDeEnergized")),
        "utc_start_raw": a.get("UTC_StartDate"),
        "utc_end_raw": a.get("UTC_EndDate"),
        "utc_ert_raw": a.get("UTC_ERT"),
        "ert_notes": a.get("ERTNotes"),
        "poc_notes": a.get("POCNotes"),
        "psps_details": a.get("PSPS_Details"),
        "status_active_dt_utc": _epoch_ms_to_iso_utc(a.get("StatusActiveDt")),
        "status_timestamp_utc": _epoch_ms_to_iso_utc(a.get("PspsStatusTimeStamp")),
        "global_id": a.get("GlobalID"),
        "centroid_lat": clat,
        "centroid_lng": clng,
        "bbox_lng_min": bbox[0], "bbox_lat_min": bbox[1],
        "bbox_lng_max": bbox[2], "bbox_lat_max": bbox[3],
        "geometry_rings_json": json.dumps((feat.get("geometry") or {}).get("rings"))
                               if feat.get("geometry") else "",
        "source_layer": f"{layer}:{LAYERS.get(layer, '')}",
        "retrieved_utc": retrieved_utc,
    }


def collect(counties=DEFAULT_COUNTIES, spatial=False):
    session = _session()
    where = build_where(counties)
    retrieved_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, stats = [], {}
    for layer in LAYERS:
        feats = fetch_layer(session, layer, where, spatial=spatial)
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
    if not rows:
        return None  # nothing to append; don't grow history with empty runs
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
    n = len(rows)
    allowed = {c.strip().upper() for c in allowed_counties}
    print("\n=== PSPS SANITY REPORT ===")
    print(f"rows: {n}")
    if n == 0:
        print(f"No active or under-consideration PSPS events for {sorted(allowed)} right now.")
        print("This is the expected/correct result outside fire-weather conditions.")
        print("The query executed successfully and returned valid, empty polygon results.")
        return
    non_oc = [r for r in rows if (r["county"] or "").upper() not in allowed]
    with_circuit = [r for r in rows if r["circuit_name"]]
    print(f"rows per county: "
          f"{ {c: sum(1 for r in rows if (r['county'] or '').upper()==c) for c in sorted(allowed)} }")
    print(f"rows tagged with an unexpected county (should be 0): {len(non_oc)}")
    print(f"rows with circuit_name: {len(with_circuit)}/{n}")
    print(f"rows with polygon geometry: {sum(1 for r in rows if r['geometry_rings_json'])}/{n}")
    print(f"total customers across events: "
          f"{sum((r['customers_total'] or 0) for r in rows)}")
    print(f"events: {sorted({(r['event_name'], r['psps_status']) for r in rows})}")


def main():
    global PAGE
    ap = argparse.ArgumentParser()
    here = pathlib.Path(__file__).parent
    ap.add_argument("--out", default=str(here / "orange_county_psps_latest.csv"))
    ap.add_argument("--history", default=str(here / "orange_county_psps_history.csv"))
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--counties", default=",".join(DEFAULT_COUNTIES),
                    help="Comma-separated county names, e.g. \"ORANGE,LOS ANGELES\"")
    ap.add_argument("--page-size", type=int, default=PAGE,
                    help="Rows per ArcGIS request; lower to stress-test pagination")
    ap.add_argument("--spatial", action="store_true",
                    help="Filter by OC bounding box intersect instead of COUNTY name match "
                         "(NOTE: the bbox is Orange-County-only; do not use with LA)")
    args = ap.parse_args()

    counties = [c.strip() for c in args.counties.split(",") if c.strip()]
    PAGE = args.page_size

    try:
        rows, stats, retrieved = collect(counties, spatial=args.spatial)
    except PSPSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"Retrieved {retrieved} | counties={counties} | page_size={PAGE} | "
          f"under_consideration={stats['under_consideration']} "
          f"active={stats['active']} total={stats['total']} "
          f"(filter={'OC-bbox spatial' if args.spatial else 'COUNTY name'})")

    out = write_csv(rows, args.out)
    print(f"Wrote {out} ({len(rows)} data rows)")
    if not args.no_history:
        h = append_history(rows, args.history)
        print(f"History: {'appended '+str(len(rows))+' rows to '+str(h) if h else 'no rows to append (empty run)'}")

    sanity_report(rows, counties)
    return 0


if __name__ == "__main__":
    sys.exit(main())
