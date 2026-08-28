"""Step 2 - Validate Anaheim (APU) endpoints with plain HTTP, incl. the 90-day history range."""
import datetime as dt, json, pathlib, requests
OUT = pathlib.Path(__file__).parent / "capture"; OUT.mkdir(parents=True, exist_ok=True)
BASE = "https://gis.anaheim.net/electricoutages/"
S = requests.Session(); S.headers.update({"User-Agent": "anaheim-validate"})

def show(label, r):
    setck = 'set-cookie' in {k.lower() for k in r.headers}
    print(f"\n[{r.status_code}] {label} ({len(r.content)}B, ct={r.headers.get('content-type','')}, "
          f"Set-Cookie={setck}, cookies={dict(S.cookies)})")
    return r

print("cookies before:", dict(S.cookies))

print("\n########## ACTIVE incidents ##########")
r = show("ActiveIncidents.cshtml", S.get(BASE + "ActiveIncidents.cshtml", timeout=30))
print("  body[:400]:", r.text[:400])
(OUT / "step2_active.json").write_text(r.text, encoding="utf-8")
try:
    act = r.json(); print(f"  parsed: {type(act).__name__}, len={len(act) if hasattr(act,'__len__') else '?'}")
except Exception as e:
    act = None; print("  (not JSON:", e, ")")

print("\n########## PREVIOUS incidents — 90-day range (max allowed) ##########")
today = dt.date.today()
d1 = (today - dt.timedelta(days=89)).strftime("%m/%d/%Y").lstrip("0").replace("/0", "/")
d2 = today.strftime("%m/%d/%Y").lstrip("0").replace("/0", "/")
print(f"  date1={d1}  date2={d2}")
r = show("PreviousIncidents.cshtml (90d)", S.get(BASE + "PreviousIncidents.cshtml",
                                                params={"date1": d1, "date2": d2}, timeout=60))
(OUT / "step2_previous_90d.json").write_text(r.text, encoding="utf-8")
try:
    prev = r.json()
except Exception as e:
    prev = None; print("  (not JSON:", e, ")"); print("  raw[:400]:", r.text[:400])

if isinstance(prev, list):
    print(f"  HISTORICAL RECORDS: {len(prev)}")
    if prev:
        print("  FIELDS:", sorted(prev[0].keys()))
        print("\n  --- first 6 records ---")
        for x in prev[:6]:
            print("   ", json.dumps(x)[:260])
        # date range actually covered + customer stats
        starts = [x.get("StartTime") for x in prev if x.get("StartTime")]
        ends = [x.get("EndTime") for x in prev if x.get("EndTime")]
        print(f"\n  StartTime samples: {starts[:3]}")
        print(f"  EndTime   samples: {ends[:3]}")
        print(f"  records WITH EndTime: {len(ends)}/{len(prev)}  <-- actual restoration?")
        cc = [x.get("CustomerCount") for x in prev if x.get("CustomerCount") is not None]
        print(f"  CustomerCount populated: {len(cc)}/{len(prev)}; sample {cc[:8]}")
        descs = {str(x.get("Description"))[:60] for x in prev}
        print(f"  distinct Description values ({len(descs)}): {list(descs)[:8]}")
        # any id-ish field?
        idish = [k for k in prev[0].keys() if "id" in k.lower()]
        print(f"  id-like fields: {idish}")

print("\n########## shorter range (7d) for comparison ##########")
d1b = (today - dt.timedelta(days=7)).strftime("%m/%d/%Y").lstrip("0").replace("/0", "/")
r = show("PreviousIncidents (7d)", S.get(BASE + "PreviousIncidents.cshtml",
                                        params={"date1": d1b, "date2": d2}, timeout=60))
try:
    p7 = r.json(); print(f"  records in last 7 days: {len(p7) if isinstance(p7,list) else '?'}")
except Exception as e:
    print("  (not JSON)", e)

print("\n########## over-90-day range (server-side limit check) ##########")
d1c = (today - dt.timedelta(days=200)).strftime("%m/%d/%Y").lstrip("0").replace("/0", "/")
r = show("PreviousIncidents (200d)", S.get(BASE + "PreviousIncidents.cshtml",
                                          params={"date1": d1c, "date2": d2}, timeout=90))
try:
    p200 = r.json()
    print(f"  records for 200-day request: {len(p200) if isinstance(p200,list) else '?'}"
          f"  -> {'server ALSO enforces no limit (client-side only)' if isinstance(p200,list) else '?'}")
except Exception as e:
    print("  (not JSON)", e, r.text[:200])
print("\ncookies after:", dict(S.cookies))
