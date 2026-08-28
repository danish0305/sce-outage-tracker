"""Step 2 - Validate Pasadena endpoints with plain HTTP (direct + via WP proxy)."""
import json, pathlib, requests
OUT = pathlib.Path(__file__).parent / "capture"

POWER_DIRECT = "http://204.89.9.88/api/weboutageviewer/get_live_data/"
PROXY = "https://pwp.cityofpasadena.net/wp-content/themes/water_and_power/php/get.php?url="
WATER = ("https://services2.arcgis.com/zNjnZafDYCAJAbN0/arcgis/rest/services/"
         "WaterOutageFeatures_PROD_VIEW/FeatureServer/0")

def show(label, r, session):
    setck = 'set-cookie' in {k.lower() for k in r.headers}
    print(f"\n[{r.status_code}] {label} ({len(r.content)}B, ct={r.headers.get('content-type','')}, "
          f"Set-Cookie={setck}, cookies={dict(session.cookies)})")
    print("   body[:300]:", r.text[:300].replace("\n", " "))

S = requests.Session(); S.headers.update({"User-Agent": "pasadena-validate"})
print("cookies before:", dict(S.cookies))

print("\n########## POWER — direct to raw IP (no browser, no proxy) ##########")
try:
    r = S.get(POWER_DIRECT, timeout=25); show("power DIRECT", r, S)
    (OUT / "step2_power_direct.json").write_text(r.text, encoding="utf-8")
except Exception as e:
    print("  DIRECT failed:", e)

print("\n########## POWER — via WordPress proxy ##########")
try:
    import urllib.parse
    r = S.get(PROXY + urllib.parse.quote(POWER_DIRECT, safe=""), timeout=25); show("power via proxy", r, S)
except Exception as e:
    print("  proxy failed:", e)

print("\n########## WATER — ArcGIS layer metadata ##########")
j = S.get(f"{WATER}?f=json", timeout=30).json()
print("  name:", j.get("name"), "| geometryType:", j.get("geometryType"),
      "| maxRecordCount:", j.get("maxRecordCount"), "| capabilities:", j.get("capabilities"))
print("  fields:", [f["name"] for f in j.get("fields", [])])

print("\n########## WATER — counts & status breakdown ##########")
for where, lbl in [("1=1", "ALL records"), ("Status='Open'", "Status=Open"),
                   ("Status='Closed'", "Status=Closed")]:
    try:
        c = S.get(f"{WATER}/query", params={"where": where, "returnCountOnly": "true", "f": "json"},
                  timeout=30).json()
        print(f"  {lbl:16}: {c.get('count')}")
    except Exception as e:
        print(f"  {lbl}: ERR {e}")

print("\n########## WATER — full sample in WGS84 ##########")
r = S.get(f"{WATER}/query", params={"where": "1=1", "outFields": "*", "returnGeometry": "true",
                                    "outSR": "4326", "f": "json"}, timeout=30)
show("water query", r, S)
d = r.json()
(OUT / "step2_water_sample.json").write_text(json.dumps(d, indent=1), encoding="utf-8")
for ft in d.get("features", []):
    a = ft["attributes"]; g = ft.get("geometry", {})
    print(f"   OBJECTID={a.get('OBJECTID')} Status={a.get('Status')!r} cust={a.get('CustomersAffected')} "
          f"ETR={a.get('EstimatedTimeofReturn')} GlobalID={a.get('GlobalID')}")
    print(f"      Location={a.get('Location')!r} Notes={a.get('Notes')!r} lat/lng={g.get('y')},{g.get('x')}")
print("\ncookies after:", dict(S.cookies))
