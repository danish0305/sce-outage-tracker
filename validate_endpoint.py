"""
Step 2 - Validate the discovered ArcGIS endpoint with plain HTTP (no browser).

Checks, for the outage MapServer layers 0 (Outages) and 1 (Scheduled Outages):
  1. Service/layer metadata reachable without any browser session/cookie/token?
     -> maxRecordCount, geometryType, field list.
  2. A broad OC query works: where=UPPER(CountyName)='ORANGE', returnGeometry=true.
  3. returnCountOnly to see how many OC records exist (vs maxRecordCount -> pagination?).
  4. Save raw sample responses to ./capture/step2_*.json.
"""

import json
import pathlib
import requests

BASE = "https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/outage/MapServer"
OUT = pathlib.Path(__file__).parent / "capture"
OUT.mkdir(exist_ok=True)

# A clean session with NO cookies carried over from any browser. Plain UA.
S = requests.Session()
S.headers.update({"User-Agent": "python-requests (SCE OC outage validation)"})

OC_WHERE = "UPPER(CountyName) = 'ORANGE'"

OUTFIELDS = ",".join([
    "ZipcodeName", "CountyName", "CityName", "OutageStartDateTime", "ERT", "Status",
    "JobStatus", "MemoCauseCdDesc", "OanNo", "NoOfAffectedCust_Inci", "IncidentType",
    "IncidentId", "LastChngDateTime", "MemoCauseCd", "PublishDate", "RO_Group_Numbers",
    "Future_RO_Groups", "Circuit_Number", "OutageType", "ProblemCode", "CrewStatusCd",
])


def get(url, params=None, label=""):
    r = S.get(url, params=params, timeout=40)
    ct = r.headers.get("content-type", "")
    print(f"\n--- {label} ---")
    print(f"GET {r.url}")
    print(f"HTTP {r.status_code} | content-type: {ct} | {len(r.content)} bytes")
    print(f"Set-Cookie present: {'set-cookie' in {k.lower() for k in r.headers}}")
    try:
        return r.json()
    except Exception:
        print("!! Non-JSON body (first 300 chars):")
        print(r.text[:300])
        return None


def layer_meta(layer):
    j = get(f"{BASE}/{layer}", params={"f": "json"}, label=f"Layer {layer} metadata")
    if not j:
        return
    print(f"  name={j.get('name')!r} geometryType={j.get('geometryType')!r} "
          f"maxRecordCount={j.get('maxRecordCount')}")
    caps = j.get("capabilities")
    print(f"  capabilities={caps!r}")
    fields = [f["name"] for f in j.get("fields", [])]
    print(f"  {len(fields)} fields: {fields}")
    (OUT / f"step2_layer{layer}_meta.json").write_text(
        json.dumps(j, indent=2), encoding="utf-8")


def oc_count(layer):
    j = get(f"{BASE}/{layer}/query",
            params={"where": OC_WHERE, "returnCountOnly": "true", "f": "json"},
            label=f"Layer {layer} Orange County COUNT")
    if j is not None:
        print(f"  Orange County record count: {j.get('count')}")
    return j.get("count") if j else None


def oc_query(layer):
    params = {
        "where": OC_WHERE,
        "outFields": OUTFIELDS,
        "returnGeometry": "true",
        "outSR": "4326",          # WGS84 lat/lng
        "f": "json",
    }
    j = get(f"{BASE}/{layer}/query", params=params,
            label=f"Layer {layer} Orange County FULL query (with geometry)")
    if not j:
        return
    feats = j.get("features", [])
    print(f"  features returned: {len(feats)} | exceededTransferLimit={j.get('exceededTransferLimit')}")
    if feats:
        f0 = feats[0]
        print(f"  sample geometry: {f0.get('geometry')}")
        a = f0.get("attributes", {})
        print(f"  sample city/county/circuit/cust: "
              f"{a.get('CityName')} / {a.get('CountyName')} / "
              f"{a.get('Circuit_Number')} / {a.get('NoOfAffectedCust_Inci')}")
        # distinct cities present
        cities = sorted({(f['attributes'].get('CityName') or '').strip() for f in feats})
        print(f"  distinct cities in result ({len(cities)}): {cities}")
    (OUT / f"step2_layer{layer}_oc_query.json").write_text(
        json.dumps(j, indent=2), encoding="utf-8")


if __name__ == "__main__":
    print("Cookies in session BEFORE any request:", dict(S.cookies))
    for layer in (0, 1):
        layer_meta(layer)
        oc_count(layer)
        oc_query(layer)
    print("\nCookies in session AFTER all requests:", dict(S.cookies))
    print("\nDONE. Raw samples saved to ./capture/step2_*.json")
