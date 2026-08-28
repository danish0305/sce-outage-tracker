"""Step 2 - probe candidate PG&E bulk outage feeds with plain HTTP (no browser)."""
import json, requests
S = requests.Session(); S.headers.update({"User-Agent":"pge-probe","Accept":"application/json"})

CANDIDATES = [
    "https://ewapi.cloudapi.pge.com/outages",
    "https://ewapi.cloudapi.pge.com/outages?v=2",
    "https://ewapi.cloudapi.pge.com/current-outages?v=2",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/outages.json",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/PGE_outages.json",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/outage_info.json",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/current_outages.json",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/outages-nonsync.json",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/outage-list-nonsync.json",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/PGE_full_outage_details.json",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/outageMap.json",
    "https://pgealerts-downloads.alerts.pge.com/current_outages/outage_details.json",
]

def probe(url):
    try:
        r = S.get(url, timeout=30)
    except Exception as e:
        print(f"[ERR] {url}\n     {e}"); return None
    setck = 'set-cookie' in {k.lower() for k in r.headers}
    ct = r.headers.get("content-type","")
    print(f"[{r.status_code}] {len(r.content):>8}B ct={ct[:32]:32} Set-Cookie={setck}  {url}")
    if r.status_code == 200 and ("json" in ct.lower() or r.text[:1] in "[{"):
        try:
            j = r.json()
            if isinstance(j, list):
                print(f"        -> JSON list[{len(j)}]; first keys: {list(j[0].keys()) if j else '[]'}")
                return (url, j)
            if isinstance(j, dict):
                print(f"        -> JSON dict; top keys: {list(j.keys())[:15]}")
                for k,v in j.items():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        print(f"           list field '{k}'[{len(v)}] first keys: {list(v[0].keys())}")
                return (url, j)
        except Exception as e:
            print(f"        (json parse failed: {e})")
    return None

print("cookies before:", dict(S.cookies))
hits = [probe(u) for u in CANDIDATES]
print("\ncookies after:", dict(S.cookies))
