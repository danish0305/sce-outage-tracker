"""Vernon (VPU) — real browser attempt. Plain HTTP gets 403 on subpages; test whether a real
headless Chromium is also blocked, and if it loads, hunt for any outage map / data endpoint."""
import json, re, pathlib
from playwright.sync_api import sync_playwright

OUT = pathlib.Path(__file__).parent / "capture"
OUT.mkdir(parents=True, exist_ok=True)

START = "https://www.cityofvernonca.gov/government/public-utilities"
SUBPAGES = [
    "https://www.cityofvernonca.gov/government/public-utilities/electric-services",
    "https://www.cityofvernonca.gov/how-do-i/report/outage",
]
captured = []

def on_response(resp):
    try:
        u = resp.url
        if any(k in u.lower() for k in ("outage", "kubra", "arcgis", "esri", "datavoice",
                                        "outageentry", "stormcenter", "/api/", "geojson", "gis.")):
            captured.append({"url": u, "status": resp.status,
                             "ct": resp.headers.get("content-type", "")})
            print(f"  ** [{resp.status}] {u[:150]}")
    except Exception:
        pass

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"), viewport={"width": 1440, "height": 1000},
          locale="en-US")
    page = ctx.new_page(); page.on("response", on_response)

    print("=== load Public Utilities page in real browser ===")
    r = page.goto(START, wait_until="domcontentloaded", timeout=60000)
    print(f"  status={r.status if r else '?'}  url={page.url}")
    try: page.wait_for_load_state("networkidle", timeout=30000)
    except Exception: pass
    page.wait_for_timeout(4000)
    print(f"  title={page.title()!r}")

    # Find every link mentioning outage
    links = page.eval_on_selector_all("a", """els => els.map(e => ({t:(e.innerText||'').trim(), h:e.href}))
        .filter(x => /outage|electric|power|map/i.test(x.t) || /outage|gis|map/i.test(x.h))""")
    print(f"\n=== {len(links)} candidate links found on page ===")
    for l in links[:30]:
        print(f"   {l['t'][:45]!r:48} -> {l['h'][:110]}")
    (OUT / "browser_links.json").write_text(json.dumps(links, indent=1), encoding="utf-8")

    # Try the subpages that 403'd on plain HTTP
    for u in SUBPAGES:
        print(f"\n=== browser load: {u} ===")
        try:
            r = page.goto(u, wait_until="domcontentloaded", timeout=45000)
            print(f"  status={r.status if r else '?'}  title={page.title()!r}")
            if r and r.status == 200:
                try: page.wait_for_load_state("networkidle", timeout=25000)
                except Exception: pass
                page.wait_for_timeout(3000)
                txt = page.inner_text("body")[:4000]
                hits = [m.start() for m in re.finditer(r"outage", txt, re.I)]
                print(f"  'outage' mentions in body text: {len(hits)}")
                for h in hits[:6]:
                    print("    ...", " ".join(txt[max(0,h-110):h+110].split()), "...")
                subl = page.eval_on_selector_all("a", """els => els.map(e=>({t:(e.innerText||'').trim(),h:e.href}))
                    .filter(x => /outage|map|gis|report/i.test(x.t+' '+x.h))""")
                for l in subl[:20]:
                    print(f"    link {l['t'][:40]!r:43} -> {l['h'][:110]}")
                (OUT / ("body_" + re.sub(r"\W+","_",u)[-60:] + ".txt")).write_text(txt, encoding="utf-8")
        except Exception as e:
            print(f"  ERROR: {str(e)[:140]}")

    b.close()

(OUT / "browser_outage_requests.json").write_text(json.dumps(captured, indent=1), encoding="utf-8")
print(f"\n=== outage/gis-ish network requests captured: {len(captured)} ===")
for c in captured: print("   ", c)
