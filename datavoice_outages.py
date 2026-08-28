"""
DataVoice OMS outage scraper — shared by Glendale Water & Power and Azusa Light & Water.

Platform confirmed in discovery: outageentry.com = DataVoice International OMS.
  POST https://www.outageentry.com/Outage/ajax/ajaxShellOut.php
  form: action=get, client=<glendale|azusa>, target=cfa_device_markers, serviceIndex=1
No auth/cookie required (a SERVERID load-balancer cookie is set but not needed).

Confirmed field semantics (Glendale, 4-poll stability test):
  incident_id        -> STABLE across polls (e.g. "D172378") == the real key. Use this.
  substation, feeder -> circuit-level detail, stable per outage
  consumers_affected -> customers out
  start_date         -> "MM/DD hh:mm am" PACIFIC LOCAL, NO YEAR (verified by duration arithmetic)
  estimated_restore_time -> "Mon DD YYYY hh:mm am" PACIFIC LOCAL (estimate only; can be stale/past)
  duration           -> precomputed live counter, increments between polls
  outage_comment     -> usually null;  NOTE: this feed has NO cause field at all
  lat/lon + poly     -> point + affected-area polygon (37-40 vertices typical)

Timezone handling: start_date/ETR are parsed as America/Los_Angeles and converted to true UTC.
Year inference for start_date (which omits the year): assume the most recent occurrence not in
the future relative to now (handles Dec->Jan rollover).
"""

import argparse, csv, datetime as dt, json, pathlib, re, sys, time
import requests

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:
    PACIFIC = None

URL = "https://www.outageentry.com/Outage/ajax/ajaxShellOut.php"

COLUMNS = [
    "utility", "incident_id", "substation", "feeder", "consumers_affected",
    "start_local_raw", "start_utc", "etr_local_raw", "etr_utc", "duration_raw",
    "duration_minutes", "outage_comment", "alias", "opt_code",
    "latitude", "longitude", "poly_vertices", "poly_json", "retrieved_utc",
]


class DVError(Exception):
    pass


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "LA-muni-outage-tracker/1.0 (+research)"})
    return s


def pacific_to_utc(naive):
    if naive is None:
        return ""
    if PACIFIC is None:
        return naive.strftime("%Y-%m-%dT%H:%M:%S(local)")
    return naive.replace(tzinfo=PACIFIC).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_start(s):
    """'08/26 09:32 pm' (Pacific, no year) -> UTC ISO. Infers the most recent non-future year."""
    if not s:
        return ""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*([ap]m)\s*$", s.strip(), re.I)
    if not m:
        return ""
    mon, day, hh, mm, ap = int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5].lower()
    if ap == "pm" and hh != 12: hh += 12
    if ap == "am" and hh == 12: hh = 0
    now_pac = dt.datetime.now(PACIFIC) if PACIFIC else dt.datetime.now()
    for yr in (now_pac.year, now_pac.year - 1):
        try:
            cand = dt.datetime(yr, mon, day, hh, mm)
        except ValueError:
            continue
        # allow small clock skew into the future (2h)
        if cand <= now_pac.replace(tzinfo=None) + dt.timedelta(hours=2):
            return pacific_to_utc(cand)
    return ""


def parse_etr(s):
    """'Aug 27 2026 07:32 am' (Pacific) -> UTC ISO."""
    if not s:
        return ""
    for fmt in ("%b %d %Y %I:%M %p", "%B %d %Y %I:%M %p"):
        try:
            return pacific_to_utc(dt.datetime.strptime(s.strip(), fmt))
        except ValueError:
            continue
    return ""


def parse_duration(s):
    if not s:
        return ""
    h = re.search(r"(\d+)\s*hr", s); mi = re.search(r"(\d+)\s*min", s); d = re.search(r"(\d+)\s*day", s)
    total = 0
    if d: total += int(d[1]) * 1440
    if h: total += int(h[1]) * 60
    if mi: total += int(mi[1])
    return total or ""


def fetch(session, client, retries=3, timeout=30):
    form = {"action": "get", "client": client, "target": "cfa_device_markers",
            "serviceIndex": "1", "port": "", "includePrecictions": ""}
    last = None
    for attempt in range(retries):
        try:
            r = session.post(URL, data=form, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise DVError(f"{client}: request failed after {retries} tries: {last}")


def markers(payload):
    out = []
    if not isinstance(payload, dict):
        return out
    for k, v in payload.items():
        if isinstance(v, dict) and isinstance(v.get("markers"), list):
            out.extend(v["markers"])
    return out


def parse_marker(m, utility, retrieved):
    poly = m.get("poly") or []
    return {
        "utility": utility,
        "incident_id": m.get("incident_id"),          # STABLE key (verified for Glendale)
        "substation": m.get("substation"),
        "feeder": m.get("feeder"),
        "consumers_affected": m.get("consumers_affected"),
        "start_local_raw": m.get("start_date"),
        "start_utc": parse_start(m.get("start_date")),
        "etr_local_raw": m.get("estimated_restore_time"),
        "etr_utc": parse_etr(m.get("estimated_restore_time")),
        "duration_raw": m.get("duration"),
        "duration_minutes": parse_duration(m.get("duration")),
        "outage_comment": m.get("outage_comment"),    # no cause field exists in this feed
        "alias": m.get("alias"),
        "opt_code": m.get("opt_code"),
        "latitude": m.get("lat"),
        "longitude": m.get("lon"),
        "poly_vertices": len(poly),
        "poly_json": json.dumps(poly) if poly else "",
        "retrieved_utc": retrieved,
    }


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


def sanity_report(rows, utility):
    n = len(rows)
    print(f"\n=== {utility.upper()} (DataVoice) SANITY REPORT ===")
    print(f"outages: {n}")
    if not n:
        print("No active outages right now (valid empty result).")
        return
    def pct(f): return f"{sum(1 for r in rows if r[f] not in (None, '', 0))}/{n}"
    print(f"incident_id populated: {pct('incident_id')}  (STABLE key per discovery)")
    print(f"field completeness -> substation:{pct('substation')} feeder:{pct('feeder')} "
          f"customers:{pct('consumers_affected')} start_utc:{pct('start_utc')} "
          f"etr_utc:{pct('etr_utc')} polygon:{pct('poly_json')}")
    cust = sum(int(r["consumers_affected"]) for r in rows
               if str(r["consumers_affected"]).isdigit())
    print(f"total customers out: {cust}")
    print(f"incident_ids: {[r['incident_id'] for r in rows]}")
    print(f"substation/feeder pairs: {sorted({(r['substation'], r['feeder']) for r in rows})}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True, choices=["glendale", "azusa"])
    ap.add_argument("--out"); ap.add_argument("--history")
    ap.add_argument("--no-history", action="store_true")
    args = ap.parse_args()

    here = pathlib.Path(__file__).parent / args.client
    out = args.out or str(here / f"{args.client}_outages_latest.csv")
    hist = args.history or str(here / f"{args.client}_outages_history.csv")

    retrieved = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session = _session()
    try:
        payload = fetch(session, args.client)
    except DVError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2

    rows = [parse_marker(m, args.client, retrieved) for m in markers(payload)]
    print(f"Retrieved {retrieved} | client={args.client} | result={payload.get('result')} | outages={len(rows)}")
    print(f"Wrote {write_csv(rows, out)}")
    if not args.no_history:
        h = append_history(rows, hist)
        print(f"History: {'appended '+str(len(rows))+' rows to '+str(h) if h else 'no rows (empty run)'}")
    sanity_report(rows, args.client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
