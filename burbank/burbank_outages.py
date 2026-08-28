"""
Burbank Water & Power outage scraper (self-hosted "POP" REST API).

  GET /POP/model/PopOutage?ServiceType=Electric          -> per-outage list (schema UNVERIFIED:
                                                            was [] during discovery)
  GET /POP/model/PopOutageSummary?ServiceType=Electric   -> aggregate affected/total
No auth, no cookies.

Snapshot semantics: this writes a row EVERY run even when quiet, so we build a real time
series (a zero-outage row is a valid observation, not a gap). Two outputs:
  *_summary_history.csv : one row per run (always) -- affected/total counts
  *_outages_latest.csv / *_outages_history.csv : per-outage rows when any exist

UNVERIFIED (flagged, not assumed):
  - Per-outage field names. Only 4 are known from the server's display templates:
    outageStepOffTime (start), publishedEtr (estimated restore), areaName, displayID.
  - displayID stability: UNTESTED (no outage yet). Treat with SDGE-style suspicion.
  - Timezone of outageStepOffTime / publishedEtr: UNKNOWN. We therefore store the RAW value
    verbatim plus UTC+Pacific capture clocks, so it can be resolved later by arithmetic
    rather than guessed now.
Whenever an outage IS present, the full raw JSON is archived to capture/live/.
"""

import argparse, csv, datetime as dt, json, pathlib, sys, time
import requests

BASE = "https://pop.burbankwaterandpower.com/POP/model/"
SUMMARY = BASE + "PopOutageSummary?ServiceType={st}"
OUTAGE = BASE + "PopOutage?ServiceType={st}"

SUMMARY_COLUMNS = ["utility", "service_type", "affected_count", "total_count",
                   "outage_records", "retrieved_utc", "retrieved_pacific"]
# Real schema (confirmed from a live capture 2026-08-27): top-level id / serviceType /
# currentAffected / lat / lon / geoJSONPolygon, with the four display fields NESTED inside
# `fieldValues` (areaName, outageStepOffTime, displayID, publishedEtr).
OUTAGE_COLUMNS = ["utility", "service_type", "record_id", "displayID", "areaName",
                  "current_affected", "outage_start_utc", "published_etr_utc",
                  "latitude", "longitude", "has_polygon",
                  "all_fields_json", "retrieved_utc", "retrieved_pacific"]


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": "LA-muni-outage-tracker/1.0 (+research)"})
    return s


def clocks():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        pac = now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        pac = ""
    return now.strftime("%Y-%m-%dT%H:%M:%SZ"), pac


def get_json(session, url, retries=3, timeout=30):
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=timeout); r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e; time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url} failed: {last}")


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
    ap.add_argument("--service-type", default="Electric")
    ap.add_argument("--out", default=str(here / "burbank_outages_latest.csv"))
    ap.add_argument("--history", default=str(here / "burbank_outages_history.csv"))
    ap.add_argument("--summary-history", default=str(here / "burbank_summary_history.csv"))
    args = ap.parse_args()
    st = args.service_type
    utc, pac = clocks()
    s = _session()

    try:
        summary = get_json(s, SUMMARY.format(st=st))
        payload = get_json(s, OUTAGE.format(st=st))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2

    affected = sum(r.get("affectedCount") or 0 for r in summary if isinstance(r, dict))
    total = max((r.get("totalCount") or 0) for r in summary if isinstance(r, dict)) if summary else 0
    n = len(payload) if isinstance(payload, list) else 0

    print(f"Retrieved {utc} | Burbank POP {st} | affectedCount={affected} total={total} outage_records={n}")

    # always-on snapshot row
    append_csv([{"utility": "burbank", "service_type": st, "affected_count": affected,
                 "total_count": total, "outage_records": n,
                 "retrieved_utc": utc, "retrieved_pacific": pac}],
               args.summary_history, SUMMARY_COLUMNS)

    rows = []
    if n:
        # archive the raw payload -- this is the schema evidence we've been waiting for
        live = pathlib.Path(args.out).parent / "capture" / "live"
        live.mkdir(parents=True, exist_ok=True)
        stamp = utc.replace(":", "").replace("-", "")
        (live / f"popoutage_{stamp}.json").write_text(
            json.dumps({"captured": {"utc": utc, "pacific": pac}, "summary": summary,
                        "PopOutage": payload}, indent=1), encoding="utf-8")
        print(f"  *** {n} outage record(s) -- raw payload archived; FIELDS: "
              f"{sorted(payload[0].keys())}")
        for o in payload:
            fv = o.get("fieldValues") or {}   # the 4 display fields live in here, not top level
            rows.append({
                "utility": "burbank", "service_type": o.get("serviceType") or st,
                "record_id": o.get("id"),              # Mongo-style ObjectId; stability TBD
                "displayID": fv.get("displayID"),      # e.g. "J.E.26.08.00491"; stability TBD
                "areaName": fv.get("areaName"),
                "current_affected": o.get("currentAffected"),
                # Confirmed true UTC (ISO 'Z'): a live capture showed outageStepOffTime
                # 16:27:21Z == 09:27 PDT, 45 min before a 17:12:55Z capture. Stored as-is.
                "outage_start_utc": fv.get("outageStepOffTime"),
                "published_etr_utc": fv.get("publishedEtr"),
                "latitude": o.get("lat"), "longitude": o.get("lon"),
                "has_polygon": bool(o.get("geoJSONPolygon")),
                "all_fields_json": json.dumps(o),      # nothing lost
                "retrieved_utc": utc, "retrieved_pacific": pac,
            })

    print(f"Wrote {write_csv(rows, args.out, OUTAGE_COLUMNS)}")
    h = append_csv(rows, args.history, OUTAGE_COLUMNS)
    print(f"History: {'appended '+str(len(rows))+' outage rows' if h else 'no outage rows (quiet run; summary row still logged)'}")

    print("\n=== BURBANK SANITY REPORT ===")
    print("Schema CONFIRMED from live capture; timestamps are true UTC (ISO 'Z').")
    print("STILL UNVERIFIED: stability of record_id and displayID across polls -- captured, not trusted.")
    print(f"summary rows always logged -> {args.summary_history}")
    print(f"affected={affected} of total={total} customers; outage_records={n}")
    for r in rows:
        print(f"  displayID={r['displayID']} record_id={r['record_id']} affected={r['current_affected']} "
              f"start={r['outage_start_utc']} etr={r['published_etr_utc']} "
              f"loc=({r['latitude']},{r['longitude']}) polygon={r['has_polygon']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
