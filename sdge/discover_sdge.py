"""Step 1 - Discover SDGE's live outage data endpoint (same method used for SCE/LADWP/PG&E)."""
import json, re, pathlib, traceback
from playwright.sync_api import sync_playwright

URL = "https://outage.sdge.com/"   # real map (Kubra StormCenter); the sdge.com path 403s/redirects here
ZIPS = ["92101", "92025", "91910"]   # downtown SD, Escondido, Chula Vista

OUT = pathlib.Path(__file__).parent / "capture"
BODIES = OUT / "bodies"
OUT.mkdir(parents=True, exist_ok=True); BODIES.mkdir(exist_ok=True)

NOISE = re.compile(r"(google|gtm|gtag|analytics|doubleclick|facebook|adobe|demdex|omtrdc|"
    r"quantum|optimizely|hotjar|newrelic|nr-data|cookielaw|onetrust|cookiebot|tealium|qualtrics|"
    r"\.png|\.jpg|\.jpeg|\.gif|\.svg|\.woff|\.woff2|\.ttf|\.css|\.ico|fonts\.|/fonts/)", re.I)
INTEREST = re.compile(r"(kubra|stormcenter|currentstate|arcgis|esri|featureserver|mapserver|"
    r"outage|thematic|cluster|/api/|geojson|query\?|/v\d+/|circuit|incident|/data/|summary|feed|graphql)", re.I)

captured, seen, counter = [], set(), [0]

def safe(u): return re.sub(r"[^A-Za-z0-9._-]", "_", u)[-120:]

def on_response(resp):
    try:
        req = resp.request; rtype = req.resource_type; url = resp.url
        if rtype not in ("xhr","fetch","document","other","script"): return
        if NOISE.search(url): return
        e = {"url":url,"method":req.method,"resource_type":rtype,"status":resp.status,
             "content_type":resp.headers.get("content-type",""),
             "interesting":bool(INTEREST.search(url)),"saved_body":None}
        ct = e["content_type"].lower()
        if ("json" in ct or e["interesting"]) and (url,req.method) not in seen:
            seen.add((url,req.method))
            try:
                body = resp.body()
                if body and len(body) < 6_000_000:
                    counter[0]+=1; fn=f"{counter[0]:03d}_{safe(url)}"
                    if not fn.endswith((".json",".txt")): fn+=".txt"
                    (BODIES/fn).write_bytes(body); e["saved_body"]=fn; e["body_len"]=len(body)
            except Exception as ex: e["body_error"]=str(ex)
        captured.append(e)
        print(f"{'**' if e['interesting'] else '  '} [{e['status']}] {req.method:4} {rtype:8} {url[:130]}")
    except Exception: traceback.print_exc()

def zip_search(page, z):
    for s in ["input[placeholder*='address' i]","input[placeholder*='zip' i]","input[placeholder*='search' i]",
              "input[placeholder*='city' i]","input[type='search']","input[aria-label*='search' i]","input[type='text']"]:
        try:
            loc = page.locator(s).first
            if loc.count() and loc.is_visible():
                loc.click(); loc.fill(""); loc.type(z, delay=60); page.wait_for_timeout(900); loc.press("Enter")
                print(f"   -> typed ZIP {z} into '{s}'"); page.wait_for_timeout(4500); return True
        except Exception: continue
    print(f"   -> no search input found for ZIP {z}"); return False

def pan_zoom(page):
    for s in ["canvas",".esri-view-surface",".leaflet-container",".mapboxgl-canvas","#map","div[class*='map' i]","iframe"]:
        try:
            loc = page.locator(s).first
            if loc.count() and loc.is_visible():
                box = loc.bounding_box()
                if not box: continue
                cx,cy = box["x"]+box["width"]/2, box["y"]+box["height"]/2
                page.mouse.move(cx,cy); page.mouse.down(); page.mouse.move(cx-100,cy-60,steps=8); page.mouse.up()
                page.wait_for_timeout(1200)
                for _ in range(3): page.mouse.wheel(0,-400); page.wait_for_timeout(1000)
                print(f"   -> panned/zoomed via '{s}'"); return True
        except Exception: continue
    print("   -> no interactable map surface found"); return False

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"), viewport={"width":1440,"height":1000})
        page = ctx.new_page(); page.on("response", on_response)
        print("=== Loading page ==="); page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        try: page.wait_for_load_state("networkidle", timeout=45000)
        except Exception: print("(networkidle timeout)")
        page.wait_for_timeout(6000)
        for label in ["Reject All","Decline","Necessary only","Accept All","Accept","I Agree","OK","Got it","Continue","Close"]:
            try:
                b = page.get_by_role("button", name=re.compile(label, re.I)).first
                if b.count() and b.is_visible(): b.click(); print(f"=== dismissed '{label}' ==="); page.wait_for_timeout(1500); break
            except Exception: continue
        print("\n=== frames ==="); [print("   frame:", fr.url) for fr in page.frames]
        print("\n=== pan/zoom ==="); pan_zoom(page)
        print("\n=== ZIP searches ==="); [zip_search(page, z) for z in ZIPS]
        page.wait_for_timeout(4000)
        try: (OUT/"page.html").write_text(page.content(), encoding="utf-8", errors="ignore")
        except Exception: pass
        browser.close()
    with (OUT/"requests.jsonl").open("w",encoding="utf-8") as f:
        for e in captured: f.write(json.dumps(e)+"\n")
    interesting = [e for e in captured if e["interesting"] or "json" in e["content_type"].lower()]
    interesting.sort(key=lambda e:(not e["interesting"], -(e.get("body_len") or 0)))
    lines=[f"Captured {len(captured)} responses; {len(interesting)} candidates.\n"]
    for e in interesting:
        lines.append(f"[{e['status']}] {e['method']} {e['resource_type']} ct={e['content_type']} len={e.get('body_len','?')} body={e['saved_body']}\n    {e['url']}\n")
    (OUT/"summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n\n=== SUMMARY (candidates) ==="); print("\n".join(lines[:50]))

if __name__ == "__main__": main()
