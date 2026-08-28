"""
SDGE outage scraper (Kubra StormCenter).

Confirmed design (from Steps 1-2 discovery + a 24-min stability poll):
  * Platform: Kubra. Map at https://outage.sdge.com/. Endpoints on kubra.io, no auth/cookie.
  * Kubra rotates the data deployment GUID every ~9-12 min, so we RE-READ currentState every
    run and rebuild all data paths from it (never hardcode the GUID).
  * Outages are tiled by quadkey. We walk the quadtree: cluster:true records aggregate a
    sub-area (recurse into 4 child quadkeys); cluster:false records are individual outages
    (record and stop). qkh (CDN shard) = quadkey's last 3 chars reversed.
  * Synthetic stable ID = (circuits, start_time). inc_id is ALWAYS null and the "9-0" id is a
    positional per-tile index (same outage differs by zoom) -- neither is used for identity.
  * Rich fields confirmed: cause (orig + EN text), circuits, customersOut (authoritative; NOT
    cust_a), etr, communities, crew_status, plus encoded point + polygon geometry.
  * Timestamps are true UTC (ISO 'Z') -- no LADWP-style local-as-UTC conversion needed.
  * Resolved outages simply vanish from the feed (no restored flag).

One-time manual validation run. Not scheduled.
"""

import argparse, csv, datetime as dt, json, math, pathlib, sys, time
import requests

INST = "baefd37d-17f9-4bb7-befb-f2f2b38b8cb8"
VIEW = "031b05f7-e0fe-4bd0-98fa-a70f017e8a5e"
CURRENT = f"https://kubra.io/stormcenter/api/v1/stormcenters/{INST}/views/{VIEW}/currentState?preview=false"

# SDGE territory bounding box (San Diego County + southern Orange County), padded.
BBOX = {"lat_min": 32.3, "lat_max": 33.7, "lng_min": -117.9, "lng_max": -116.0}
SEED_ZOOM = 4          # seed quadkeys at z4 (served as clusters); recursion handles the rest
MAX_ZOOM = 16
FETCH_CAP = 4000

COLUMNS = [
    "synthetic_id", "circuits", "communities", "customers_out", "cause", "cause_detail",
    "crew_status", "start_utc", "etr_utc", "n_out", "centroid_lat", "centroid_lng",
    "geom_point_encoded", "geom_polygon_encoded",
    "inc_id_raw", "positional_id_raw", "tile_quadkey", "deployment_guid", "retrieved_utc",
]


class SDGEError(Exception):
    pass


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "SDGE-outage-tracker/1.0 (+research)"})
    return s


def latlng_to_quadkey(lat, lng, z):
    sinlat = math.sin(lat * math.pi / 180)
    x = (lng + 180) / 360
    y = 0.5 - math.log((1 + sinlat) / (1 - sinlat)) / (4 * math.pi)
    n = 2 ** z
    tx = min(n - 1, max(0, int(x * n)))
    ty = min(n - 1, max(0, int(y * n)))
    qk = ""
    for i in range(z, 0, -1):
        d = 0; mask = 1 << (i - 1)
        if tx & mask: d += 1
        if ty & mask: d += 2
        qk += str(d)
    return qk


def seed_quadkeys():
    qs = set()
    lat = BBOX["lat_min"]
    while lat <= BBOX["lat_max"]:
        lng = BBOX["lng_min"]
        while lng <= BBOX["lng_max"]:
            qs.add(latlng_to_quadkey(lat, lng, SEED_ZOOM))
            lng += 0.15
        lat += 0.15
    return sorted(qs)


def decode_polyline(enc):
    """Google encoded polyline (precision 5) -> list of (lat, lng)."""
    if not enc:
        return []
    coords, idx, lat, lng = [], 0, 0, 0
    while idx < len(enc):
        for is_lng in (False, True):
            shift, result = 0, 0
            while True:
                b = ord(enc[idx]) - 63; idx += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng: lng += d
            else: lat += d
        coords.append((lat / 1e5, lng / 1e5))
    return coords


def get_currentstate(session, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(CURRENT, timeout=30); r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                raise SDGEError(f"currentState failed: {e}")
            time.sleep(1.5 * (attempt + 1))


def walk_outages(session, cluster_tmpl):
    """Quadtree walk. Returns dict keyed by (circuits, start_time) -> raw record + tile."""
    def qkh(q): return q[-3:][::-1]
    def url(q): return f"https://kubra.io/{cluster_tmpl.replace('{qkh}', qkh(q))}/public/cluster-1/{q}.json"

    outages, visited, queue, fetches = {}, set(), list(seed_quadkeys()), 0
    while queue and fetches < FETCH_CAP:
        q = queue.pop()
        if q in visited:
            continue
        visited.add(q); fetches += 1
        try:
            r = session.get(url(q), timeout=25)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json().get("file_data", [])
        except ValueError:
            continue
        for item in data:
            d = item.get("desc", {}) or {}
            if d.get("cluster") is True:
                if len(q) < MAX_ZOOM:
                    queue.extend(q + c for c in "0123")
            else:
                key = (d.get("circuits"), d.get("start_time"))
                if key not in outages:
                    outages[key] = {"desc": d, "item_id": item.get("id"),
                                    "geom": item.get("geom", {}), "tile": q}
    return outages, fetches


def parse_outage(key, rec, retrieved_utc, dep_guid):
    d = rec["desc"]; g = rec.get("geom") or {}
    cause = d.get("cause") or {}
    pt_enc = (g.get("p") or [None])[0]
    poly_enc = (g.get("a") or [None])[0]
    clat = clng = None
    pts = decode_polyline(pt_enc) if pt_enc else []
    if pts:
        clat, clng = pts[0]
    return {
        "synthetic_id": f"{key[0]}|{key[1]}",
        "circuits": d.get("circuits"),
        "communities": d.get("communities"),
        "customers_out": d.get("customersOut"),          # authoritative (NOT cust_a)
        "cause": cause.get("orig"),
        "cause_detail": cause.get("EN-US"),
        "crew_status": d.get("crew_status"),
        "start_utc": d.get("start_time"),                # true UTC already
        "etr_utc": d.get("etr"),                         # estimated; true UTC
        "n_out": d.get("n_out"),
        "centroid_lat": clat,
        "centroid_lng": clng,
        "geom_point_encoded": pt_enc,
        "geom_polygon_encoded": poly_enc,
        "inc_id_raw": d.get("inc_id"),                   # always null (captured for the record)
        "positional_id_raw": rec.get("item_id"),         # "9-0" style, NOT stable
        "tile_quadkey": rec.get("tile"),
        "deployment_guid": dep_guid,
        "retrieved_utc": retrieved_utc,
    }


def collect():
    s = _session()
    retrieved = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cs = get_currentstate(s)
    tmpl = cs["data"]["cluster_interval_generation_data"]
    dep_guid = cs["data"]["interval_generation_data"].split("/")[-1]
    # summary totals (cross-check)
    summ = s.get(f"https://kubra.io/data/{dep_guid}/public/summary-1/data.json", timeout=30).json()
    tot = summ["summaryFileData"]["totals"][0]
    summary = {"total_outages": tot.get("total_outages"),
               "total_cust_a": tot.get("total_cust_a", {}).get("val"),
               "page_mode": summ["summaryFileData"].get("page_mode", {}).get("mode")}
    raw, fetches = walk_outages(s, tmpl)
    rows = [parse_outage(k, v, retrieved, dep_guid) for k, v in raw.items()]
    return rows, summary, fetches, retrieved, dep_guid


def write_csv(rows, path):
    path = pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS); w.writeheader(); w.writerows(rows)
    return path


def append_history(rows, path):
    if not rows:
        return None
    path = pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new: w.writeheader()
        w.writerows(rows)
    return path


def sanity_report(rows, summary, fetches):
    n = len(rows)
    print("\n=== SDGE SANITY REPORT ===")
    print("NOTE: synthetic ID = (circuits, start_time). inc_id is always null; the '9-0' id is")
    print("      positional per-tile, NOT stable. Resolved outages vanish from the feed.")
    print(f"quadkey tiles fetched: {fetches}")
    print(f"outages scraped (deduped): {n}")
    print(f"summary total_outages:     {summary['total_outages']}   <-- cross-check")
    print(f"page_mode: {summary['page_mode']}  | summary total_cust_a: {summary['total_cust_a']}")
    match = "MATCH" if n == summary["total_outages"] else "!!! MISMATCH — check walk coverage"
    print(f"count cross-check: scraped {n} vs summary {summary['total_outages']} -> {match}")
    if n:
        cust_sum = sum(int(r["customers_out"]) for r in rows if str(r["customers_out"]).isdigit())
        print(f"sum(customers_out) scraped: {cust_sum}")
        def pct(field): return f"{sum(1 for r in rows if r[field] not in (None,''))}/{n}"
        print(f"field completeness -> circuits:{pct('circuits')} customers_out:{pct('customers_out')} "
              f"start_utc:{pct('start_utc')} etr_utc:{pct('etr_utc')} cause:{pct('cause')} "
              f"centroid:{pct('centroid_lat')} polygon:{pct('geom_polygon_encoded')}")
        print(f"inc_id populated (expect 0): {sum(1 for r in rows if r['inc_id_raw'] not in (None,''))}/{n}")
        print(f"distinct causes: {sorted({r['cause'] or '(none)' for r in rows})}")


def main():
    ap = argparse.ArgumentParser()
    here = pathlib.Path(__file__).parent
    ap.add_argument("--out", default=str(here / "sdge_outages_latest.csv"))
    ap.add_argument("--history", default=str(here / "sdge_outages_history.csv"))
    ap.add_argument("--no-history", action="store_true")
    args = ap.parse_args()
    try:
        rows, summary, fetches, retrieved, dep = collect()
    except SDGEError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2
    print(f"Retrieved {retrieved} | deployment={dep} | outages={len(rows)}")
    print(f"Wrote {write_csv(rows, args.out)}")
    if not args.no_history:
        h = append_history(rows, args.history)
        print(f"History: {'appended '+str(len(rows))+' rows to '+str(h) if h else 'no rows (empty)'}")
    sanity_report(rows, summary, fetches)
    return 0


if __name__ == "__main__":
    sys.exit(main())
