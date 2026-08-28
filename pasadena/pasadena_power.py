"""
Pasadena Water & Power — POWER side scraper (WebOutageViewer).

Endpoint (validated: works directly, no WordPress proxy needed, no auth/cookies):
  GET http://204.89.9.88/api/weboutageviewer/get_live_data/
Envelope observed during discovery (while empty):
  {"Outages": [], "Trucks": [], "Areas": null, "CallSummary": null}

FRAGILITY NOTE: this is plain HTTP on a bare IP with no hostname/TLS. It can change address
without notice. The site itself proxies it via
  https://pwp.cityofpasadena.net/wp-content/themes/water_and_power/php/get.php?url=<encoded>
so we fall back to that proxy automatically if the direct IP call fails.

Snapshot semantics: writes an envelope row EVERY run even when empty, so we get a real time
series of Outages/Trucks/Areas/CallSummary counts. Whenever any section is non-empty, the FULL
raw JSON is archived to capture/live/ -- that payload is the only way to learn the per-outage
field names (unknown so far, since Outages was empty during discovery).

UNVERIFIED (flagged): per-outage field names, ID stability, and timestamp timezone. Nothing is
parsed or converted; raw values are preserved verbatim alongside UTC+Pacific capture clocks.
"""

import argparse, csv, datetime as dt, json, pathlib, sys, time, urllib.parse
import requests

DIRECT = "http://204.89.9.88/api/weboutageviewer/get_live_data/"
PROXY = "https://pwp.cityofpasadena.net/wp-content/themes/water_and_power/php/get.php?url="

ENVELOPE_COLUMNS = ["utility", "source", "outages_count", "trucks_count", "areas_count",
                    "callsummary_present", "raw_bytes", "retrieved_utc", "retrieved_pacific"]
OUTAGE_COLUMNS = ["utility", "source", "all_fields_json", "retrieved_utc", "retrieved_pacific"]


def clocks():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        pac = now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        pac = ""
    return now.strftime("%Y-%m-%dT%H:%M:%SZ"), pac


def fetch(session, timeout=25, retries=2):
    """Try the direct raw-IP endpoint, then the WordPress proxy."""
    last = None
    for attempt in range(retries):
        try:
            r = session.get(DIRECT, timeout=timeout); r.raise_for_status()
            return r.json(), "direct_ip", len(r.content)
        except (requests.RequestException, ValueError) as e:
            last = e; time.sleep(1.0 * (attempt + 1))
    try:
        r = session.get(PROXY + urllib.parse.quote(DIRECT, safe=""), timeout=timeout)
        r.raise_for_status()
        return r.json(), "wp_proxy", len(r.content)
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"both direct ({last}) and proxy ({e}) failed")


def count(v):
    return len(v) if isinstance(v, list) else (0 if v is None else 1)


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
    ap.add_argument("--out", default=str(here / "pasadena_power_outages_latest.csv"))
    ap.add_argument("--history", default=str(here / "pasadena_power_outages_history.csv"))
    ap.add_argument("--envelope-history", default=str(here / "pasadena_power_envelope_history.csv"))
    args = ap.parse_args()

    utc, pac = clocks()
    s = requests.Session(); s.headers.update({"User-Agent": "LA-muni-outage-tracker/1.0 (+research)"})
    try:
        data, source, nbytes = fetch(s)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2

    outages = data.get("Outages") or []
    trucks = data.get("Trucks") or []
    areas = data.get("Areas")
    callsum = data.get("CallSummary")

    print(f"Retrieved {utc} | Pasadena WebOutageViewer via {source} | "
          f"Outages={count(outages)} Trucks={count(trucks)} Areas={count(areas)} "
          f"CallSummary={'yes' if callsum else 'null'}")

    append_csv([{"utility": "pasadena_power", "source": source,
                 "outages_count": count(outages), "trucks_count": count(trucks),
                 "areas_count": count(areas),
                 "callsummary_present": bool(callsum), "raw_bytes": nbytes,
                 "retrieved_utc": utc, "retrieved_pacific": pac}],
               args.envelope_history, ENVELOPE_COLUMNS)

    # Archive full raw payload whenever anything at all is populated.
    if outages or trucks or areas or callsum:
        live = here / "capture" / "live"; live.mkdir(parents=True, exist_ok=True)
        stamp = utc.replace(":", "").replace("-", "")
        (live / f"weboutageviewer_{stamp}.json").write_text(
            json.dumps({"captured": {"utc": utc, "pacific": pac}, "source": source,
                        "data": data}, indent=1), encoding="utf-8")
        print("  *** non-empty payload -- raw JSON archived")
        if outages:
            print(f"  *** OUTAGE FIELDS: {sorted(outages[0].keys())}")

    rows = [{"utility": "pasadena_power", "source": source, "all_fields_json": json.dumps(o),
             "retrieved_utc": utc, "retrieved_pacific": pac} for o in outages]
    print(f"Wrote {write_csv(rows, args.out, OUTAGE_COLUMNS)}")
    h = append_csv(rows, args.history, OUTAGE_COLUMNS)
    print(f"History: {'appended '+str(len(rows))+' outage rows' if h else 'no outage rows (quiet run; envelope row still logged)'}")

    print("\n=== PASADENA (power) SANITY REPORT ===")
    print("NOTE: per-outage schema, ID stability, and timezone are UNVERIFIED (Outages was")
    print("      empty during discovery). Raw values preserved verbatim; nothing converted.")
    print(f"envelope rows always logged -> {args.envelope_history}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
