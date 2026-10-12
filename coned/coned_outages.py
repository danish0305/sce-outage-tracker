"""
Con Edison outage scraper (legacy Kubra/iFactor StormCenter, core 4.8.3, static S3 files).

Source (public, no auth / cookie / token):
  https://outagemap.coned.com/resources/data/external/interval_generation_data/
    metadata.json                  -> {"directory": "YYYY_MM_DD_HH_MM_SS"} (UTC; new each publish)
    <dir>/data.json                -> summary totals (the cross-check reference)
    <dir>/outages/<quadkey>.json   -> outage records; cluster:true = aggregate, cluster:false = one outage

Design decisions (all verified during discovery, 2026-10-12):
  * metadata.json is re-read every run (with a cache-buster: the CDN caches it 5-10 min).
  * FULL SCAN of every zoom-12 tile over the territory. Expanding clusters downward is NOT used:
    a cluster marker is stored in the tile holding its centroid, but its members can sit in a
    neighbouring tile whose own parent tile is empty, so a cluster-expansion walk silently missed
    4/19 outages on one publish. If clusters remain at z12, the next zoom is scanned over each
    cluster tile's 3x3 neighbourhood (members can cross tile edges), repeating as needed.
  * A 403 from S3 means "no such tile" (empty), not an auth failure.
  * The per-publish `id` is renumbered 1..N on every publish (17/18 changed across 3 publishes),
    so it is kept only as `publish_id_UNSTABLE`. Identity = decoded point location (5 dp, the
    encoding's native precision).
  * There is NO start time in the feed. `first_seen_utc` is when this scraper first observed the
    outage, so it can lag the real start by up to the gap between runs. Kept in a state file.
  * ETR may be a timestamp with explicit offset (converted to UTC) or a status sentinel
    (ETR-NULL / ETR-EXP), which is kept as a status, never parsed as a time.

PRIVACY: the premises layer (<dir>/premises/...) exposes plain-text customer addresses plus
`called` and `critical` flags. This scraper never builds, fetches or stores premises URLs.
"""

import argparse, csv, datetime as dt, json, math, pathlib, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests

BASE = "https://outagemap.coned.com/resources/data/external/interval_generation_data"
BBOX = (40.49, 41.37, -74.27, -73.48)          # five boroughs + Westchester (lat0, lat1, lng0, lng1)
SCAN_ZOOM, MAX_ZOOM = 12, 17
ETR_SENTINELS = {"ETR-NULL", "ETR-EXP"}
DRIFT_METERS = 50                               # first_seen carry-over if a centroid moves slightly

HERE = pathlib.Path(__file__).parent
OUT_LATEST = HERE / "coned_outages_latest.csv"
OUT_HISTORY = HERE / "coned_outages_history.csv"
OUT_HEARTBEAT = HERE / "coned_heartbeat_history.csv"
STATE = HERE / "coned_first_seen_state.json"

COLUMNS = [
    "utility", "synthetic_id_location", "publish_id_UNSTABLE", "latitude", "longitude",
    "customers_out", "reported_problem", "cause", "crew_status_code",
    "etr_status", "etr_utc", "etr_raw",
    "first_seen_utc", "first_seen_method", "minutes_since_first_seen",
    "has_polygon", "polygon_vertices", "polygon_json",
    "found_at_zoom", "directory", "publish_utc", "retrieved_utc",
]
HEARTBEAT_COLUMNS = [
    "utility", "directory", "publish_utc", "page_mode",
    "summary_outages", "summary_customers_out", "summary_customers_served",
    "walked_outages", "walked_customers_out", "crosscheck",
    "tiles_scanned", "deepest_zoom", "clusters_remaining", "http_ok", "error", "retrieved_utc",
]


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "nyc-outage-tracker/1.0 (+research)"})
    return s


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def decode(enc):
    """Google encoded polyline (precision 5) -> [(lat, lng), ...]."""
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(enc):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(enc[i]) - 63; i += 1
                result |= (b & 0x1F) << shift; shift += 5
                if b < 0x20: break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0: lat += delta
            else: lng += delta
        pts.append((round(lat / 1e5, 5), round(lng / 1e5, 5)))
    return pts


def tile_xy(lat, lng, z):
    s = math.sin(math.radians(lat)); n = 2 ** z
    return int((lng + 180) / 360 * n), int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)


def qk_from_xy(x, y, z):
    q = ""
    for i in range(z, 0, -1):
        m = 1 << (i - 1); q += str((1 if x & m else 0) + (2 if y & m else 0))
    return q


def xy_from_qk(q):
    x = y = 0
    for i, c in enumerate(q):
        m = 1 << (len(q) - 1 - i); d = int(c)
        if d & 1: x |= m
        if d & 2: y |= m
    return x, y


def territory_tiles(z):
    x0, y1 = tile_xy(BBOX[0], BBOX[2], z); x1, y0 = tile_xy(BBOX[1], BBOX[3], z)
    return [qk_from_xy(x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def children_of_neighbourhood(tiles):
    """Next-zoom children of each tile's 3x3 neighbourhood (cluster members can cross edges)."""
    out = set()
    for q in tiles:
        z = len(q); x, y = xy_from_qk(q)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nq = qk_from_xy(x + dx, y + dy, z)
                out.update(nq + c for c in "0123")
    return sorted(out)


def parse_offset_time(s):
    """'2026-10-12T12:30:00-0400' -> aware datetime, or None."""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z"):
        try:
            return dt.datetime.strptime(s, fmt)
        except (TypeError, ValueError):
            continue
    return None


def haversine_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def get_json(session, url, retries=3):
    if "/premises/" in url:                     # hard privacy guard
        raise RuntimeError("refusing to fetch the premises layer (customer addresses)")
    last = None
    for a in range(retries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 403:            # S3: key does not exist -> empty tile
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e; time.sleep(1.5 * (a + 1))
    raise RuntimeError(f"{url}: {last}")


def scan(session, directory):
    """Full scan at SCAN_ZOOM, then neighbourhood scans deeper while clusters remain."""
    def fetch(q):
        return q, get_json(session, f"{BASE}/{directory}/outages/{q}.json")

    found, tiles_scanned, z = {}, 0, SCAN_ZOOM
    tiles = territory_tiles(z)
    clusters_remaining = 0
    while True:
        with ThreadPoolExecutor(12) as ex:
            results = list(ex.map(fetch, tiles))
        tiles_scanned += len(tiles)
        cluster_tiles = set()
        for q, data in results:
            for rec in (data or {}).get("file_data", []):
                desc = rec.get("desc") or {}
                if desc.get("cluster"):
                    cluster_tiles.add(q)
                    continue
                pid = rec.get("id")
                # within ONE publish an outage keeps the same id at every zoom -> dedupe on it
                if pid not in found or z > found[pid]["zoom"]:
                    found[pid] = {"zoom": z, "rec": rec}
        clusters_remaining = len(cluster_tiles)
        if not cluster_tiles or z >= MAX_ZOOM:
            return found, tiles_scanned, z, clusters_remaining
        z += 1
        tiles = children_of_neighbourhood(cluster_tiles)


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_csv(path, cols, rows, mode):
    new = mode == "w" or not path.exists()
    with path.open(mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new: w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-history", action="store_true")
    args = ap.parse_args()

    retrieved = now_utc()
    s = _session()
    hb = {c: "" for c in HEARTBEAT_COLUMNS}
    hb.update(utility="coned", retrieved_utc=iso(retrieved), http_ok="true")
    try:
        meta = get_json(s, f"{BASE}/metadata.json?_={int(time.time() * 1000)}")
        directory = meta["directory"]
        summ = get_json(s, f"{BASE}/{directory}/data.json")["summaryFileData"]
        found, tiles_scanned, deepest, clusters_left = scan(s, directory)
    except Exception as e:
        hb.update(http_ok="false", error=str(e)[:300])
        write_csv(OUT_HEARTBEAT, HEARTBEAT_COLUMNS, [hb], "a")
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    pub = parse_offset_time(summ.get("date_generated"))
    publish_utc = iso(pub.astimezone(dt.timezone.utc)) if pub else ""

    # ---- build rows + first_seen bookkeeping -----------------------------------------
    state = load_state()                         # synthetic_id -> {"first_seen_utc", "lat", "lng"}
    rows, current = [], {}
    for pid, item in found.items():
        rec, desc = item["rec"], item["rec"].get("desc") or {}
        geom = rec.get("geom") or {}
        pt = decode(geom["p"][0])[0] if geom.get("p") else (None, None)
        ring = decode(geom["a"][0]) if geom.get("a") and geom["a"][0] else []
        sid = f"{pt[0]:.5f}|{pt[1]:.5f}" if pt[0] is not None else f"noloc-{pid}"
        etr_raw = desc.get("etr")
        if etr_raw in ETR_SENTINELS:
            etr_status, etr_utc = etr_raw, ""
        else:
            t = parse_offset_time(etr_raw)
            etr_status, etr_utc = ("ESTIMATED", iso(t.astimezone(dt.timezone.utc))) if t else (etr_raw or "", "")
        current[sid] = (pt, len(rows))
        rows.append({
            "utility": "coned", "synthetic_id_location": sid, "publish_id_UNSTABLE": pid,
            "latitude": pt[0], "longitude": pt[1],
            "customers_out": (desc.get("cust_a") or {}).get("val"),
            "reported_problem": desc.get("reported_problem"), "cause": desc.get("cause"),
            "crew_status_code": desc.get("crew_status"),
            "etr_status": etr_status, "etr_utc": etr_utc, "etr_raw": etr_raw,
            "has_polygon": bool(ring), "polygon_vertices": len(ring),
            "polygon_json": json.dumps(ring) if ring else "",
            "found_at_zoom": item["zoom"], "directory": directory,
            "publish_utc": publish_utc, "retrieved_utc": iso(retrieved),
        })

    walked_out = len(rows)
    walked_cust = sum(r["customers_out"] or 0 for r in rows)
    sum_out = summ.get("total_outages")
    sum_cust = (summ.get("total_cust_a") or {}).get("val")
    complete = (walked_out == sum_out and walked_cust == sum_cust and clusters_left == 0)

    # first_seen: exact location match, else nearest unmatched previous outage within DRIFT_METERS
    unmatched_prev = {k: v for k, v in state.items() if k not in current}
    new_state = {}
    for sid, (pt, idx) in current.items():
        if sid in state:
            fs, method = state[sid]["first_seen_utc"], "exact"
        else:
            best = None
            for k, v in unmatched_prev.items():
                d = haversine_m(pt, (v["lat"], v["lng"]))
                if d <= DRIFT_METERS and (best is None or d < best[0]):
                    best = (d, k)
            if best:
                fs, method = unmatched_prev.pop(best[1])["first_seen_utc"], f"drift<={DRIFT_METERS}m"
            else:
                fs, method = iso(retrieved), "new"
        new_state[sid] = {"first_seen_utc": fs, "lat": pt[0], "lng": pt[1]}
        fs_t = dt.datetime.strptime(fs, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        rows[idx].update(first_seen_utc=fs, first_seen_method=method,
                         minutes_since_first_seen=int((retrieved - fs_t).total_seconds() // 60))
    # Only forget vanished outages after a COMPLETE run; an incomplete scan must not reset them.
    if not complete:
        for k, v in state.items():
            new_state.setdefault(k, v)
    STATE.write_text(json.dumps(new_state, indent=1, sort_keys=True), encoding="utf-8")

    rows.sort(key=lambda r: -(r["customers_out"] or 0))
    write_csv(OUT_LATEST, COLUMNS, rows, "w")
    if not args.no_history and rows:
        write_csv(OUT_HISTORY, COLUMNS, rows, "a")

    hb.update(directory=directory, publish_utc=publish_utc,
              page_mode=(summ.get("page_mode") or {}).get("mode"),
              summary_outages=sum_out, summary_customers_out=sum_cust,
              summary_customers_served=summ.get("total_cust_s"),
              walked_outages=walked_out, walked_customers_out=walked_cust,
              crosscheck="MATCH" if complete else "MISMATCH",
              tiles_scanned=tiles_scanned, deepest_zoom=deepest, clusters_remaining=clusters_left)
    write_csv(OUT_HEARTBEAT, HEARTBEAT_COLUMNS, [hb], "a")

    print(f"Retrieved {iso(retrieved)} | directory={directory} (published {publish_utc}) | "
          f"tiles={tiles_scanned} deepest_zoom={deepest}")
    print(f"CROSS-CHECK: walked {walked_out} outages / {walked_cust} customers  vs  "
          f"summary {sum_out} / {sum_cust}  -> {hb['crosscheck']}"
          + (f"  (clusters left unresolved: {clusters_left})" if clusters_left else ""))
    if not complete:
        print("::warning::ConEd walked totals do not match data.json -- see coned_heartbeat_history.csv")
    methods = {}
    for r in rows: methods[r["first_seen_method"]] = methods.get(r["first_seen_method"], 0) + 1
    print(f"first_seen methods: {methods}   polygons: {sum(r['has_polygon'] for r in rows)}/{len(rows)}   "
          f"etr_status: { {k: sum(1 for r in rows if r['etr_status']==k) for k in {r['etr_status'] for r in rows}} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
