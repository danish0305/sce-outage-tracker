"""Step 2b - determine exactly what headers PG&E endpoints require."""
import requests

ADDR = "https://ewapi.cloudapi.pge.com/single-address-outages?v=2&address=94102"
OUTAGES = "https://ewapi.cloudapi.pge.com/outages?v=2"
PCT = "https://pgealerts-downloads.alerts.pge.com/current_outages/outage-percentages-nonsync.json"
ORIGIN = "https://pgealerts.alerts.pge.com"

def test(url, headers, label):
    s = requests.Session()
    try:
        r = s.get(url, headers=headers, timeout=30)
    except Exception as e:
        print(f"[ERR] {label}: {e}"); return
    setck = 'set-cookie' in {k.lower() for k in r.headers}
    print(f"\n[{r.status_code}] {label}  ({len(r.content)}B, ct={r.headers.get('content-type','')}, Set-Cookie={setck}, cookies={dict(s.cookies)})")
    print("   body[:180]:", r.text[:180].replace("\n"," "))

print("========== single-address-outages ==========")
test(ADDR, {"User-Agent":"probe"}, "bare UA")
test(ADDR, {"User-Agent":"Mozilla/5.0", "Origin":ORIGIN, "Referer":ORIGIN+"/"}, "UA+Origin+Referer")

print("\n========== bulk /outages ==========")
test(OUTAGES, {"User-Agent":"probe"}, "bare UA")
test(OUTAGES, {"User-Agent":"Mozilla/5.0", "Origin":ORIGIN, "Referer":ORIGIN+"/"}, "UA+Origin+Referer")

print("\n========== county percentages json ==========")
test(PCT, {"User-Agent":"probe"}, "bare UA")
test(PCT, {"User-Agent":"Mozilla/5.0", "Origin":ORIGIN, "Referer":ORIGIN+"/"}, "UA+Origin+Referer")
