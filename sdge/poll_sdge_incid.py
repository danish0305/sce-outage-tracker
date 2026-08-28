"""Poll SDGE/Kubra every 3 min (~24 min) to test inc_id / id / circuits stability.

Each poll: re-read currentState (Kubra rotates the deployment GUIDs on republish), fetch the
summary totals, then fetch the known outage quadkey tiles using the FRESH GUIDs, and record
every individual outage (desc.cluster == false). Outages are tracked by a content signature
(circuits + start_time) as ground truth, so we can see whether inc_id/id stay constant for the
SAME physical outage across refreshes.
"""
import json, time, datetime as dt, pathlib, requests

INST = "baefd37d-17f9-4bb7-befb-f2f2b38b8cb8"
VIEW = "031b05f7-e0fe-4bd0-98fa-a70f017e8a5e"
CURRENT = f"https://kubra.io/stormcenter/api/v1/stormcenters/{INST}/views/{VIEW}/currentState?preview=false"

# (qkh, quadkey) tile pairs that returned outage/cluster data during discovery.
TILES = [("302","023013203"),("202","023013202"),("122","023013221"),
         ("330","0230132033"),("312","0230132213"),
         # a few neighbors in case an outage shifts tiles
         ("203","023013202"),("221","023013221")]

OUT = pathlib.Path(__file__).parent / "capture" / "incid_poll.jsonl"
OUT.parent.mkdir(parents=True, exist_ok=True)
S = requests.Session(); S.headers.update({"User-Agent":"sdge-incid-poll"})

N, GAP = 8, 180

def poll_once():
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {"ts": ts}
    try:
        cs = S.get(CURRENT, timeout=30).json()
        tmpl = cs["data"]["cluster_interval_generation_data"]   # cluster-data/{qkh}/<cint>/<dep>
        dep = cs["data"]["interval_generation_data"]            # data/<dep>
        rec["deployment"] = dep
        rec["cluster_tmpl"] = tmpl
        rec["updatedAt"] = cs.get("updatedAt")
        # summary totals
        dep_guid = dep.split("/")[-1]
        summ = S.get(f"https://kubra.io/data/{dep_guid}/public/summary-1/data.json", timeout=30).json()
        tot = summ["summaryFileData"]["totals"][0]
        rec["summary_total_outages"] = tot.get("total_outages")
        rec["summary_cust_a"] = tot.get("total_cust_a", {}).get("val")
        # fetch outage tiles with fresh GUIDs
        seen = {}
        for qkh, qk in TILES:
            url = f"https://kubra.io/{tmpl.replace('{qkh}', qkh)}/public/cluster-1/{qk}.json"
            try:
                r = S.get(url, timeout=30)
                if r.status_code != 200:
                    continue
                for item in r.json().get("file_data", []):
                    d = item.get("desc", {}) or {}
                    if d.get("cluster") is True:
                        continue  # skip aggregations; want individual outages
                    sig = f"{d.get('circuits')}|{d.get('start_time')}"
                    seen[sig] = {
                        "id": item.get("id"), "inc_id": d.get("inc_id"),
                        "circuits": d.get("circuits"), "customersOut": d.get("customersOut"),
                        "start_time": d.get("start_time"), "etr": d.get("etr"),
                        "cause": (d.get("cause") or {}).get("orig"),
                        "communities": d.get("communities"),
                        "crew_status": d.get("crew_status"), "tile": qk,
                    }
            except Exception:
                continue
        rec["outages"] = list(seen.values())
        rec["n_individual"] = len(seen)
    except Exception as e:
        rec["error"] = str(e)
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"[{ts}] dep={rec.get('deployment','?')} total={rec.get('summary_total_outages','?')} individual={rec.get('n_individual','ERR')}")
    return rec

for i in range(N):
    poll_once()
    if i < N-1:
        time.sleep(GAP)
print("DONE polling.")
