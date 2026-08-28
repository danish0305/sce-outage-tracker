"""Poll the LADWP outage layer every 3 min for ~27 min; log each snapshot to observe
how OUTAGE_RANK / COUNT_IN_RANK / COUNT_OTHERS / ETR_DATETIME behave over time."""
import json, time, datetime as dt, pathlib, requests

BASE = "https://services2.arcgis.com/v0bIBBLIiGigCimX/ArcGIS/rest/services/PowerOutages_Data/FeatureServer/0"
OUT = pathlib.Path(__file__).parent / "capture" / "poll_snapshots.jsonl"
OUT.parent.mkdir(parents=True, exist_ok=True)
S = requests.Session(); S.headers.update({"User-Agent": "ladwp-poller"})

N = 10          # snapshots
GAP = 180       # seconds between snapshots (~27 min total)

for i in range(N):
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = S.get(f"{BASE}/query", params={
            "where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json"}, timeout=40)
        feats = r.json().get("features", [])
        rec = {
            "ts": ts, "n": len(feats),
            "rows": [f["attributes"] for f in feats],
        }
    except Exception as e:
        rec = {"ts": ts, "error": str(e)}
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"[{ts}] snapshot {i+1}/{N}: n={rec.get('n','ERR')}")
    if i < N - 1:
        time.sleep(GAP)
print("DONE polling.")
