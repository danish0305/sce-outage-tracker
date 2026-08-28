"""
Azusa Light & Water (DataVoice) — lifecycle poll.

Polls every 3 min for ~30 min. Records every raw response. If an outage appears, captures the
full payload and tracks incident_id / substation / feeder / consumers_affected across polls so
we can (a) verify ID stability independently of Glendale and (b) confirm the schema matches
Glendale field-for-field rather than assuming it does.
"""
import datetime as dt, json, pathlib, time
import requests

URL = "https://www.outageentry.com/Outage/ajax/ajaxShellOut.php"
FORM = {"action": "get", "client": "azusa", "target": "cfa_device_markers",
        "serviceIndex": "1", "port": "", "includePrecictions": ""}
OUT = pathlib.Path(__file__).parent / "capture"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "poll_azusa.jsonl"

# Glendale's confirmed field set, for an exact comparison.
GLENDALE_FIELDS = {"substation", "feeder", "incident_id", "alias", "outage_comment",
                   "estimated_restore_time", "formatted_ert", "start_date", "duration",
                   "consumers_affected", "lon", "lat", "opt_code", "poly"}

S = requests.Session(); S.headers.update({"User-Agent": "azusa-lifecycle-poll"})
N, GAP = 10, 180
hist = {}

for i in range(N):
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {"ts": ts}
    try:
        r = S.post(URL, data=FORM, timeout=30)
        j = r.json()
        ms = [m for k, v in j.items() if isinstance(v, dict) and isinstance(v.get("markers"), list)
              for m in v["markers"]]
        rec.update(status=r.status_code, result=j.get("result"), bytes=len(r.content),
                   n_outages=len(ms))
        if ms:
            rec["markers"] = [{k: (f"<{len(v)} pts>" if k == "poly" else v) for k, v in m.items()}
                              for m in ms]
            fields = set(ms[0].keys())
            rec["fields"] = sorted(fields)
            rec["fields_match_glendale"] = (fields == GLENDALE_FIELDS)
            rec["fields_extra"] = sorted(fields - GLENDALE_FIELDS)
            rec["fields_missing"] = sorted(GLENDALE_FIELDS - fields)
            (OUT / f"azusa_live_{ts.replace(':','').replace('-','')}.json").write_text(
                json.dumps(j, indent=1), encoding="utf-8")
            for m in ms:
                key = f"sub{m.get('substation')}/fdr{m.get('feeder')}@{m.get('start_date')}"
                hist.setdefault(key, []).append(
                    (ts, m.get("incident_id"), m.get("consumers_affected"), m.get("duration")))
    except Exception as e:
        rec["error"] = str(e)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"[{ts}] result={rec.get('result')} outages={rec.get('n_outages','ERR')}"
          + (f"  FIELDS_MATCH_GLENDALE={rec.get('fields_match_glendale')}" if rec.get("n_outages") else ""))
    if i < N - 1:
        time.sleep(GAP)

print("\n=== per-outage tracking ===")
if not hist:
    print("  No outages appeared during the window (endpoint healthy throughout).")
for k, seq in hist.items():
    print(f"\n {k}  (seen {len(seq)}x)")
    print(f"   incident_id : {sorted({s[1] or 'None' for s in seq})}  "
          f"-> {'STABLE' if len({s[1] for s in seq}) == 1 else 'CHANGED'}")
    print(f"   customers   : {[s[2] for s in seq]}")
    print(f"   duration    : {[s[3] for s in seq]}")
print("\nDONE")
