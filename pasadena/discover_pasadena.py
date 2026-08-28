"""Step 1 - Discover Pasadena Water & Power outage endpoint (WordPress + Google Maps front end)."""
import json, re, pathlib, traceback
from playwright.sync_api import sync_playwright

URL = "https://pwp.cityofpasadena.net/outage-map/"
ZIPS = ["91101", "91104", "91106"]

OUT = pathlib.Path(__file__).parent / "capture"
BODIES = OUT / "bodies"
OUT.mkdir(parents=True, exist_ok=True); BODIES.mkdir(exist_ok=True)

NOISE = re.compile(r"(google-analytics|gtm|gtag|doubleclick|facebook|adobe|demdex|omtrdc|"
    r"quantum|optimizely|hotjar|newrelic|cookielaw|onetrust|cookiebot|tealium|addtoany|"
    r"\.png|\.jpg|\.jpeg|\.gif|\.svg|\.woff|\.woff2|\.ttf|\.css|\.ico|fonts\.|/fonts/)", re.I)
INTEREST = re.compile(r"(outage|/api/|geojson|\.json|\.php|\.ashx|\.asmx|xml|circuit|incident|"
    r"data|marker|kml|rest|wp-json|admin-ajax|arcgis|esri|kubra)", re.I)

captured, seen, counter = [], set(), [0]
def safe(u): return re.sub(r"[^A-Za-z0-9._-]", "_", u)[-120:]

def on_response(resp):
    try:
        req=resp.request; rtype=req.resource_type; url=resp.url
        if rtype not in ("xhr","fetch","document","other","script"): return
        if NOISE.search(url): return
        e={"url":url,"method":req.method,"resource_type":rtype,"status":resp.status,
           "content_type":resp.headers.get("content-type",""),
           "interesting":bool(INTEREST.search(url)),"saved_body":None,
           "post_data":(req.post_data or "")[:600]}
        ct=e["content_type"].lower()
        if ("json" in ct or "xml" in ct or e["interesting"]) and (url,req.method) not in seen:
            seen.add((url,req.method))
            try:
                body=resp.body()
                if body and len(body)<6_000_000:
                    counter[0]+=1; fn=f"{counter[0]:03d}_{safe(url)}"
                    if not fn.endswith((".json",".txt",".xml")): fn+=".txt"
                    (BODIES/fn).write_bytes(body); e["saved_body"]=fn; e["body_len"]=len(body)
            except Exception as ex: e["body_error"]=str(ex)
        captured.append(e)
        print(f"{'**' if e['interesting'] else '  '} [{e['status']}] {req.method:4} {rtype:8} {url[:135]}")
    except Exception: traceback.print_exc()

def main():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        ctx=b.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"), viewport={"width":1440,"height":1000})
        page=ctx.new_page(); page.on("response", on_response)
        print("=== Loading ==="); page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        try: page.wait_for_load_state("networkidle", timeout=45000)
        except Exception: print("(networkidle timeout)")
        page.wait_for_timeout(8000)
        for label in ["Accept","Agree","OK","Close","Got it"]:
            try:
                bt=page.get_by_role("button", name=re.compile(label, re.I)).first
                if bt.count() and bt.is_visible(): bt.click(); page.wait_for_timeout(1200); break
            except Exception: continue
        print("\n=== frames ==="); [print("   frame:", f.url) for f in page.frames]
        for s in ["canvas",".gm-style","#map","div[class*='map' i]","iframe"]:
            try:
                loc=page.locator(s).first
                if loc.count() and loc.is_visible():
                    bb=loc.bounding_box()
                    if not bb: continue
                    cx,cy=bb["x"]+bb["width"]/2, bb["y"]+bb["height"]/2
                    page.mouse.move(cx,cy); page.mouse.down(); page.mouse.move(cx-90,cy-50,steps=6); page.mouse.up()
                    page.wait_for_timeout(1500)
                    for _ in range(3): page.mouse.wheel(0,-400); page.wait_for_timeout(1200)
                    print(f"   -> panned/zoomed '{s}'"); break
            except Exception: continue
        for z in ZIPS:
            done=False
            for s in ["input[type='search']","input[placeholder*='zip' i]","input[placeholder*='address' i]","input[type='text']"]:
                try:
                    loc=page.locator(s).first
                    if loc.count() and loc.is_visible():
                        loc.click(); loc.fill(""); loc.type(z, delay=60); page.wait_for_timeout(800); loc.press("Enter")
                        print(f"   -> typed {z} into '{s}'"); page.wait_for_timeout(4000); done=True; break
                except Exception: continue
            if not done: print(f"   -> no input for {z}")
        page.wait_for_timeout(3000)
        try: (OUT/"page.html").write_text(page.content(), encoding="utf-8", errors="ignore")
        except Exception: pass
        b.close()
    with (OUT/"requests.jsonl").open("w",encoding="utf-8") as fh:
        for e in captured: fh.write(json.dumps(e)+"\n")
    cands=[e for e in captured if e["interesting"] or "json" in e["content_type"].lower() or "xml" in e["content_type"].lower()]
    cands.sort(key=lambda e:(not e["interesting"], -(e.get("body_len") or 0)))
    lines=[f"Captured {len(captured)}; {len(cands)} candidates.\n"]
    for e in cands:
        lines.append(f"[{e['status']}] {e['method']} {e['resource_type']} ct={e['content_type']} len={e.get('body_len','?')} body={e['saved_body']}\n    {e['url']}\n"+(f"    POST: {e['post_data']}\n" if e['post_data'] else ""))
    (OUT/"summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n=== SUMMARY ==="); print("\n".join(lines[:45]))

if __name__=="__main__": main()
