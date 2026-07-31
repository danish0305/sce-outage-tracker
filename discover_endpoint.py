"""
Step 1 - Discover SCE's live outage data endpoint.

Loads the SCE outage-status page in headless Chromium (Playwright), captures every
XHR/fetch/document network request + response body, then interacts with the page
(ZIP search, View By toggle, map pan/zoom) to trigger additional requests.

All captured traffic is written to ./capture/ for inspection:
  - requests.jsonl : one line per captured request (url, method, resource_type, status, ...)
  - bodies/        : response bodies for interesting (json-ish) responses
  - summary.txt    : a ranked, human-readable shortlist of candidate data endpoints
"""

import json
import re
import time
import pathlib
import traceback
from playwright.sync_api import sync_playwright

URL = "https://www.sce.com/outages-safety/outage-center/check-outage-status"
OC_ZIPS = ["92617", "92660", "92701"]  # Irvine, Newport Beach, Santa Ana

OUT = pathlib.Path(__file__).parent / "capture"
BODIES = OUT / "bodies"
OUT.mkdir(exist_ok=True)
BODIES.mkdir(exist_ok=True)

# Substrings that mark a request as noise we don't care about.
NOISE = re.compile(
    r"(google|gtm|gtag|analytics|doubleclick|facebook|adobe|demdex|"
    r"omtrdc|quantum|optimizely|hotjar|newrelic|nr-data|cookielaw|onetrust|"
    r"\.png|\.jpg|\.jpeg|\.gif|\.svg|\.woff|\.woff2|\.ttf|\.css|\.ico|"
    r"fonts\.|/fonts/)",
    re.I,
)
# Substrings that strongly suggest an outage/geo data endpoint.
INTEREST = re.compile(
    r"(kubra|stormcenter|arcgis|featureserver|mapserver|outage|currentstate|"
    r"thematic|cluster|/api/|geojson|query\?|/v\d+/|circuit|county|/data/)",
    re.I,
)

captured = []          # list of dicts
seen_urls = set()
body_counter = [0]


def safe_name(url: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._-]", "_", url)
    return n[-120:]


def on_response(response):
    try:
        req = response.request
        rtype = req.resource_type
        url = response.url
        if rtype not in ("xhr", "fetch", "document", "other", "script"):
            return
        if NOISE.search(url):
            return
        entry = {
            "url": url,
            "method": req.method,
            "resource_type": rtype,
            "status": response.status,
            "content_type": response.headers.get("content-type", ""),
            "interesting": bool(INTEREST.search(url)),
            "saved_body": None,
        }
        ct = entry["content_type"].lower()
        wants_body = ("json" in ct or "geo+json" in ct or entry["interesting"])
        if wants_body and (url, req.method) not in seen_urls:
            seen_urls.add((url, req.method))
            try:
                body = response.body()
                if body and len(body) < 6_000_000:
                    body_counter[0] += 1
                    fn = f"{body_counter[0]:03d}_{safe_name(url)}"
                    if not fn.endswith((".json", ".txt")):
                        fn += ".txt"
                    (BODIES / fn).write_bytes(body)
                    entry["saved_body"] = fn
                    entry["body_len"] = len(body)
            except Exception as e:
                entry["body_error"] = str(e)
        captured.append(entry)
        tag = "**" if entry["interesting"] else "  "
        print(f"{tag} [{entry['status']}] {req.method:4} {rtype:8} {url[:130]}")
    except Exception:
        traceback.print_exc()


def try_zip_search(page, zip_code):
    """Best-effort: find a search/address input, type a ZIP, submit."""
    selectors = [
        "input[type='search']",
        "input[placeholder*='address' i]",
        "input[placeholder*='zip' i]",
        "input[placeholder*='city' i]",
        "input[aria-label*='search' i]",
        "input[name*='search' i]",
        "input[type='text']",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click()
                loc.fill("")
                loc.type(zip_code, delay=60)
                page.wait_for_timeout(800)
                loc.press("Enter")
                print(f"   -> typed ZIP {zip_code} into '{sel}'")
                page.wait_for_timeout(4000)
                return True
        except Exception:
            continue
    print(f"   -> no usable search input found for ZIP {zip_code}")
    return False


def try_toggle_viewby(page):
    for text in ["City", "County"]:
        try:
            loc = page.get_by_text(text, exact=False).first
            if loc.count() and loc.is_visible():
                loc.click()
                print(f"   -> clicked View-By '{text}'")
                page.wait_for_timeout(2500)
        except Exception:
            continue


def pan_zoom_map(page):
    """Try to interact with an Esri/Leaflet map canvas near OC."""
    for sel in ["canvas", ".esri-view-surface", ".leaflet-container", "#map", "div[class*='map' i]"]:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                box = loc.bounding_box()
                if not box:
                    continue
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                # drag to pan
                page.mouse.move(cx, cy)
                page.mouse.down()
                page.mouse.move(cx - 120, cy - 60, steps=8)
                page.mouse.up()
                page.wait_for_timeout(1500)
                # zoom in a couple of times
                for _ in range(3):
                    page.mouse.wheel(0, -400)
                    page.wait_for_timeout(1200)
                print(f"   -> panned/zoomed map via '{sel}'")
                return True
        except Exception:
            continue
    print("   -> no interactable map surface found")
    return False


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/149.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 1000},
        )
        page = ctx.new_page()
        page.on("response", on_response)

        print("=== Loading page ===")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            print("(networkidle timeout - continuing)")
        page.wait_for_timeout(6000)

        # Dismiss cookie/consent banners if present (privacy-preserving: decline).
        for label in ["Reject All", "Decline", "Necessary only", "Accept All", "Accept", "I Agree", "OK"]:
            try:
                b = page.get_by_role("button", name=re.compile(label, re.I)).first
                if b.count() and b.is_visible():
                    b.click()
                    print(f"=== dismissed banner via '{label}' ===")
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

        print("\n=== Interaction: View By toggle ===")
        try_toggle_viewby(page)

        print("\n=== Interaction: map pan/zoom ===")
        pan_zoom_map(page)

        print("\n=== Interaction: ZIP searches ===")
        for z in OC_ZIPS:
            try_zip_search(page, z)

        page.wait_for_timeout(4000)

        # Dump full HTML too, for reference.
        try:
            (OUT / "page.html").write_text(page.content(), encoding="utf-8", errors="ignore")
        except Exception:
            pass

        browser.close()

    # Write logs
    with (OUT / "requests.jsonl").open("w", encoding="utf-8") as f:
        for e in captured:
            f.write(json.dumps(e) + "\n")

    # Ranked summary
    interesting = [e for e in captured if e["interesting"] or "json" in e["content_type"].lower()]
    interesting.sort(key=lambda e: (not e["interesting"], -(e.get("body_len") or 0)))
    lines = []
    lines.append(f"Captured {len(captured)} XHR/fetch/doc responses; "
                 f"{len(interesting)} candidate data responses.\n")
    for e in interesting:
        lines.append(f"[{e['status']}] {e['method']} {e['resource_type']} "
                     f"ct={e['content_type']} len={e.get('body_len','?')} "
                     f"body={e['saved_body']}\n    {e['url']}\n")
    (OUT / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n\n=== SUMMARY ===")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
