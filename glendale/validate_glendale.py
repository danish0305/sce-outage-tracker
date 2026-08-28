"""Step 2 - Validate Glendale (DataVoice) endpoint with plain HTTP + short ID-stability poll."""
import json, time, datetime as dt, pathlib, requests

URL = "https://www.outageentry.com/Outage/ajax/ajaxShellOut.php"
FORM = {"action": "get", "client": "glendale", "target": "cfa_device_markers",
        "serviceIndex": "1", "port": "", "includePrecictions": ""}
OUT = pathlib.Path(__file__).parent / "capture"

def fetch(session, label=""):
    r = session.post(URL, data=FORM, timeout=30)
    setck = 'set-cookie' in {k.lower() for k in r.headers}
    print(f"\n[{r.status_code}] {label} ({len(r.content)}B, ct={r.headers.get('content-type','')}, "
          f"Set-Cookie={setck}, session_cookies={dict(session.cookies)})")
    try:
        return r.json()
    except Exception as e:
        print("  non-JSON:", r.text[:200]); return None

def markers(j):
    out = []
    if not j: return out
    for k, v in j.items():
        if isinstance(v, dict) and "markers" in v:
            out.extend(v["markers"])
    return out

S = requests.Session(); S.headers.update({"User-Agent": "glendale-validate"})
print("cookies before:", dict(S.cookies))
j = fetch(S, "plain POST, bare UA")
ms = markers(j)
print(f"  outages returned: {len(ms)}")
if ms:
    m = dict(ms[0]); npoly = len(m.get("poly") or []); m["poly"] = f"<{npoly} vertices>"
    print("  FIELDS:", list(m.keys()))
    print("  SAMPLE:", json.dumps(m, indent=1))
    print("\n  --- all outages (summary) ---")
    for x in ms:
        print(f"    inc={x.get('incident_id'):>9} sub={x.get('substation'):>3} feeder={x.get('feeder'):>3} "
              f"cust={x.get('consumers_affected'):>4} start={x.get('start_date')} dur={x.get('duration'):>12} "
              f"ert={x.get('estimated_restore_time')} lat/lon={x.get('lat')[:7]},{x.get('lon')[:9]} "
              f"poly={len(x.get('poly') or [])} comment={x.get('outage_comment')}")
    (OUT / "step2_sample.json").write_text(json.dumps(j, indent=1), encoding="utf-8")

# --- short ID stability poll: 4 polls x 90s ---
print("\n\n=== ID stability poll (4 x 90s) ===")
hist = {}
for i in range(4):
    ts = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    ms = markers(fetch(S, f"poll {i+1}"))
    for x in ms:
        key = f"sub{x.get('substation')}/fdr{x.get('feeder')}@{x.get('start_date')}"
        hist.setdefault(key, []).append((ts, x.get("incident_id"), x.get("consumers_affected"),
                                         x.get("duration"), x.get("estimated_restore_time")))
    print(f"  poll {i+1}: {len(ms)} outages")
    if i < 3: time.sleep(90)

print("\n--- per-outage tracking (keyed by substation/feeder/start) ---")
for key, seq in hist.items():
    print(f"\n {key}  (seen {len(seq)}x)")
    print(f"   incident_id values: {{{', '.join(sorted({s[1] or 'None' for s in seq}))}}}")
    print(f"   consumers_affected: {[s[2] for s in seq]}")
    print(f"   duration:           {[s[3] for s in seq]}")
    print(f"   ERT distinct:       {sorted({s[4] or 'None' for s in seq})}")
print("\ncookies after:", dict(S.cookies))
