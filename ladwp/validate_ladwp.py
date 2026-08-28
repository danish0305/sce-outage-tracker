"""Step 2 - Validate LADWP outage endpoint with plain HTTP (no browser)."""
import json, pathlib, requests

BASE = "https://services2.arcgis.com/v0bIBBLIiGigCimX/ArcGIS/rest/services/PowerOutages_Data/FeatureServer/0"
OUT = pathlib.Path(__file__).parent / "capture"
OUT.mkdir(parents=True, exist_ok=True)

S = requests.Session()
S.headers.update({"User-Agent": "python-requests (LADWP outage validation)"})

def get(url, params, label):
    r = S.get(url, params=params, timeout=40)
    setck = 'set-cookie' in {k.lower() for k in r.headers}
    print(f"\n--- {label} ---")
    print(f"GET {r.url}")
    print(f"HTTP {r.status_code} | ct={r.headers.get('content-type','')} | {len(r.content)}B | Set-Cookie={setck} | session_cookies={dict(S.cookies)}")
    return r

print("cookies before:", dict(S.cookies))

# 1) total count
j = get(f"{BASE}/query", {"where": "1=1", "returnCountOnly": "true", "f": "json"}, "COUNT (all)").json()
print("  total active-outage features:", j.get("count"))

# 2) full sample with geometry in lat/lng
r = get(f"{BASE}/query", {
    "where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": "4326", "f": "json",
}, "FULL query (outFields=*, geometry, WGS84)")
j = r.json()
(OUT / "step2_sample.json").write_text(json.dumps(j, indent=2), encoding="utf-8")
feats = j.get("features", [])
print("  features returned:", len(feats), "| exceededTransferLimit:", j.get("exceededTransferLimit"))
if feats:
    print("  --- first 3 records (attributes + geometry) ---")
    for f in feats[:3]:
        print("   ", json.dumps(f, ensure_ascii=False))
    # geographic scope: distinct cities
    cities = sorted({(f["attributes"].get("CITY_NAM") or "").strip() for f in feats})
    print(f"\n  distinct CITY_NAM ({len(cities)}):")
    print("   ", cities)
    # lat/lng bounds to see if Owens Valley (~36-37N) mixes with LA (~34N)
    lats = [f["attributes"].get("CENTROID_LAT") for f in feats if f["attributes"].get("CENTROID_LAT")]
    lngs = [f["attributes"].get("CENTROID_LNG") for f in feats if f["attributes"].get("CENTROID_LNG")]
    if lats:
        print(f"  lat range: {min(lats):.3f} .. {max(lats):.3f} | lng range: {min(lngs):.3f} .. {max(lngs):.3f}")
        print("  (LA Basin ~33.7-34.3N; Owens Valley ~36.5-37.5N — wide lat range => both regions mixed)")
