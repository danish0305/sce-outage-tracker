"""
Azusa Light & Water (DataVoice) watcher — one check-cycle per invocation.

Cheap: a single POST (~356 B when quiet). Logs a heartbeat when quiet. The moment an outage
appears it:
  1. archives the full raw payload (banks the schema),
  2. compares the field set against Glendale's confirmed DataVoice schema (verify, not assume),
  3. runs 4 follow-up polls 90 s apart recording incident_id / substation / feeder /
     consumers_affected / duration, so ID stability is measured independently of Glendale.
Designed to be run repeatedly by a scheduler.
"""
import datetime as dt, json, pathlib, time
import requests

URL = "https://www.outageentry.com/Outage/ajax/ajaxShellOut.php"
FORM = {"action": "get", "client": "azusa", "target": "cfa_device_markers",
        "serviceIndex": "1", "port": "", "includePrecictions": ""}
GLENDALE_FIELDS = {"substation", "feeder", "incident_id", "alias", "outage_comment",
                   "estimated_restore_time", "formatted_ert", "start_date", "duration",
                   "consumers_affected", "lon", "lat", "opt_code", "poly"}

OUT = pathlib.Path(__file__).parent / "capture" / "watch"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "watch_log.txt"
S = requests.Session(); S.headers.update({"User-Agent": "azusa-watcher/1.0 (+research)"})


def log(msg):
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts}  {msg}\n")
    print(f"{ts}  {msg}")


def markers(j):
    return [m for k, v in j.items() if isinstance(v, dict) and isinstance(v.get("markers"), list)
            for m in v["markers"]]


def clocks():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        pac = now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        pac = ""
    return now.strftime("%Y-%m-%dT%H:%M:%SZ"), pac


def main():
    try:
        j = S.post(URL, data=FORM, timeout=30).json()
    except Exception as e:
        log(f"ERROR: {e}")
        return 1
    ms = markers(j)
    if not ms:
        log(f"quiet: result={j.get('result')} outages=0")
        return 0

    utc, pac = clocks()
    stamp = utc.replace(":", "").replace("-", "")
    log(f"*** AZUSA OUTAGE DETECTED: {len(ms)} outage(s) -- capturing ***")
    (OUT / f"azusa_live_{stamp}.json").write_text(
        json.dumps({"captured": {"utc": utc, "pacific": pac}, "payload": j}, indent=1),
        encoding="utf-8")

    fields = set(ms[0].keys())
    log(f"FIELDS: {sorted(fields)}")
    log(f"schema matches Glendale exactly: {fields == GLENDALE_FIELDS}"
        + (f" | extra={sorted(fields - GLENDALE_FIELDS)} missing={sorted(GLENDALE_FIELDS - fields)}"
           if fields != GLENDALE_FIELDS else ""))
    for m in ms:
        log(f"  inc={m.get('incident_id')} sub={m.get('substation')} fdr={m.get('feeder')} "
            f"cust={m.get('consumers_affected')} start={m.get('start_date')} "
            f"dur={m.get('duration')} ert={m.get('estimated_restore_time')} "
            f"poly={len(m.get('poly') or [])}")

    snaps = []
    for i in range(4):
        try:
            mm = markers(S.post(URL, data=FORM, timeout=30).json())
        except Exception as e:
            log(f"  poll {i+1} error: {e}"); mm = []
        snaps.append({"poll": i + 1, "clocks": clocks(),
                      "records": [{k: m.get(k) for k in
                                   ("incident_id", "substation", "feeder",
                                    "consumers_affected", "duration",
                                    "estimated_restore_time", "start_date")} for m in mm]})
        log(f"  stability poll {i+1}: {len(mm)} outage(s) ids="
            f"{[m.get('incident_id') for m in mm]}")
        if i < 3:
            time.sleep(90)
    (OUT / f"azusa_stability_{stamp}.json").write_text(json.dumps(snaps, indent=1), encoding="utf-8")
    log("stability snapshots saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
