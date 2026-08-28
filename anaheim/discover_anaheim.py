"""Step 1 - Anaheim (APU): capture requests incl. driving the 'Past Outages' date-range picker."""
import json, re, pathlib
from playwright.sync_api import sync_playwright

URL = "https://gis.anaheim.net/electricoutages/map.html"
OUT = pathlib.Path(__file__).parent / "capture"; BODIES = OUT / "bodies"
OUT.mkdir(parents=True, exist_ok=True); BODIES.mkdir(exist_ok=True)
captured = []

def on_response(resp):
    try:
        req = resp.request; url = resp.url
        if req.resource_type not in ("xhr", "fetch", "document", "script", "other"): return
        if any(k in url.lower() for k in ("googletag", "gtag", ".css", ".png", ".gif", ".woff")): return
        e = {"url": url, "method": req.method, "status": resp.status,
             "rtype": req.resource_type, "ct": resp.headers.get("content-type", "")}
        if any(k in url.lower() for k in ("cshtml", "rest/services", "incident")):
            try:
                b = resp.body()
                if b and len(b) < 3_000_000:
                    fn = re.sub(r"[^A-Za-z0-9._-]", "_", url)[-110:] + ".txt"
                    (BODIES / fn).write_bytes(b); e["saved"] = fn; e["len"] = len(b)
            except Exception: pass
        captured.append(e)
        print(f"  [{e['status']}] {req.method:4} {e['rtype']:8} {url[:140]}")
    except Exception: pass

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_context(viewport={"width": 1440, "height": 1000}).new_page()
    page.on("response", on_response)
    console = []
    page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))

    print("=== load ==="); page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    try: page.wait_for_load_state("networkidle", timeout=40000)
    except Exception: pass
    page.wait_for_timeout(7000)

    # Inspect the controls present
    print("\n=== radio buttons / date boxes present ===")
    for sel in ["input[type=radio]", "input[name*='date' i]", "#datebox1", "#datebox2",
                "[widgetid*='datebox' i]", "button", "#divDateError"]:
        try:
            n = page.locator(sel).count()
            if n: print(f"   {sel}: {n}")
        except Exception: pass
    try:
        print("   radio labels:", page.eval_on_selector_all(
            "input[type=radio]", "els => els.map(e => e.id + '/' + (e.value||''))"))
    except Exception: pass

    # Switch to "Past Outages" mode
    print("\n=== switching to Past Outages ===")
    for pat in ["Past", "Previous", "History"]:
        try:
            el = page.get_by_text(re.compile(pat, re.I)).first
            if el.count() and el.is_visible():
                el.click(); print(f"   clicked text '{pat}'"); page.wait_for_timeout(2000); break
        except Exception: continue
    try:
        rs = page.locator("input[type=radio]")
        if rs.count() > 1:
            rs.nth(1).check(force=True); print("   checked 2nd radio"); page.wait_for_timeout(1500)
    except Exception as e: print("   radio err:", e)

    # Fill the two dojo DateTextBoxes and submit
    print("\n=== filling dates (30-day range) and submitting ===")
    try:
        page.evaluate("""() => {
            const r = require;
            try {
              const registry = r('dijit/registry');
              const b1 = registry.byId('datebox1'), b2 = registry.byId('datebox2');
              const to = new Date(); const from = new Date(Date.now() - 30*86400000);
              if (b1 && b2) { b1.set('value', from); b2.set('value', to); return 'set via dijit'; }
              return 'dijit boxes not found';
            } catch(e) { return 'err ' + e.message; }
        }""")
        print("   dijit set attempted")
    except Exception as e: print("   evaluate err:", e)
    for pat in ["Show", "Submit", "Go", "Search", "Apply", "Update"]:
        try:
            bt = page.get_by_role("button", name=re.compile(pat, re.I)).first
            if bt.count() and bt.is_visible():
                bt.click(); print(f"   clicked button '{pat}'"); page.wait_for_timeout(6000); break
        except Exception: continue
    # Fallback: call getIncidents() directly in page context
    try:
        res = page.evaluate("() => { try { if (typeof mode !== 'undefined') { mode='previous'; } "
                            "if (typeof getIncidents === 'function') { getIncidents(); return 'called getIncidents'; } "
                            "return 'getIncidents not in scope'; } catch(e){ return 'err '+e.message; } }")
        print("   direct call:", res); page.wait_for_timeout(6000)
    except Exception as e: print("   direct call err:", e)

    page.wait_for_timeout(3000)
    print("\n=== console messages ===")
    for c in console[-25:]: print("   ", c[:180])
    b.close()

(OUT / "requests_browser.jsonl").write_text("\n".join(json.dumps(e) for e in captured), encoding="utf-8")
print("\n=== cshtml requests seen ===")
for e in captured:
    if "cshtml" in e["url"].lower():
        print(f"   [{e['status']}] {e['url'][:160]} len={e.get('len','?')} saved={e.get('saved')}")
