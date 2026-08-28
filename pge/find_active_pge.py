"""Query single-address-outages for ZIPs in affected counties to capture a populated
current_outage and reveal the real outage schema (cause/start/ETR/ID/restoration)."""
import json, pathlib, requests
S = requests.Session(); S.headers.update({"User-Agent":"pge-schema"})
BASE = "https://ewapi.cloudapi.pge.com/single-address-outages?v=2&address="
OUT = pathlib.Path(__file__).parent / "capture"

ZIPS = ["93901","93905","93906","93940","93955","93930","93927","95012",  # Monterey
        "95340","95348","93635","95301",                                   # Merced
        "95060","95062","95076","95403","95404","95709","94533","94565"]   # SantaCruz/Sonoma/ElDorado/Solano/CC

found = 0
for z in ZIPS:
    try:
        r = S.get(BASE + z, timeout=30); j = r.json()
    except Exception as e:
        print(f"{z}: ERR {e}"); continue
    res = j.get("results", [])
    active = []
    for pr in res:
        for det in (pr.get("sp_meter_transformer_details") or []):
            if det.get("current_outage"):
                active.append((pr, det))
    print(f"{z}: {r.status_code} results={len(res)} active_current_outage={len(active)} Set-Cookie={'set-cookie' in {k.lower() for k in r.headers}}")
    if active and found < 2:
        pr, det = active[0]
        co = det["current_outage"]
        print(f"\n  ===== ACTIVE OUTAGE @ {pr.get('prem_city')} {pr.get('prem_zip')} ({pr.get('prem_county')}) lat/lng={pr.get('prem_lat')},{pr.get('prem_long')} =====")
        print("  current_outage KEYS:", list(co.keys()))
        print("  current_outage FULL:", json.dumps(co, indent=1)[:2000])
        print("  power_restored_window:", det.get("power_restored_window"))
        (OUT / f"active_outage_{z}.json").write_text(json.dumps(j, indent=1), encoding="utf-8")
        found += 1

print(f"\nTotal ZIPs with an active current_outage captured: {found}")
