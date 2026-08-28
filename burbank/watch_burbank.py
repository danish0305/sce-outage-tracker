"""
Burbank (POP) outage watcher — one check-cycle per invocation.

Cheap path: GET PopOutageSummary (~228 B). If affectedCount == 0, log a heartbeat and exit.
The moment affectedCount > 0, immediately:
  1. capture the full PopOutage payload (raw, timestamped) -- banks the real schema
  2. run 4 follow-up polls 90 s apart, recording displayID / all identifier-ish fields per
     outage, so we can tell whether displayID is STABLE or rotates like SDGE's positional id
  3. capture PopOutageSummary alongside, and a UTC + Pacific clock stamp at capture time so
     the timezone of outageStepOffTime / publishedEtr can be settled by arithmetic

Everything lands in ./capture/watch/. Designed to be run repeatedly by a scheduler.
"""
import datetime as dt, json, pathlib, time
import requests

BASE = "https://pop.burbankwaterandpower.com/POP/model/"
SUMMARY = BASE + "PopOutageSummary?ServiceType=Electric"
OUTAGE = BASE + "PopOutage?ServiceType=Electric"
WATER_OUTAGE = BASE + "PopOutage?ServiceType=Water"

OUT = pathlib.Path(__file__).parent / "capture" / "watch"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "watch_log.txt"

S = requests.Session()
S.headers.update({"User-Agent": "burbank-pop-watcher/1.0 (+research)"})


def log(msg):
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}  {msg}"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def clocks():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        pac = now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except Exception:
        pac = "?"
    return {"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "pacific": pac}


def affected(summary):
    tot = 0
    if isinstance(summary, list):
        for row in summary:
            v = row.get("affectedCount")
            if isinstance(v, (int, float)):
                tot += v
    return tot


def ident_view(o):
    """Pull every plausibly-identifying / circuit-ish field from an outage record."""
    keys = [k for k in o.keys()
            if any(t in k.lower() for t in
                   ("id", "name", "circuit", "feeder", "substation", "device", "area", "etr",
                    "time", "count", "cause", "status", "crew"))]
    return {k: o.get(k) for k in keys}


def main():
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        r = S.get(SUMMARY, timeout=30)
        summary = r.json()
    except Exception as e:
        log(f"ERROR summary fetch: {e}")
        return 1

    n = affected(summary)
    if n <= 0:
        # also peek at water, purely informational
        log(f"quiet: affectedCount={n} (no outages)")
        return 0

    # ---- OUTAGE DETECTED: bank everything ----
    log(f"*** OUTAGE DETECTED: affectedCount={n} -- capturing full payload ***")
    (OUT / f"summary_{stamp}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    try:
        payload = S.get(OUTAGE, timeout=30).json()
    except Exception as e:
        log(f"ERROR PopOutage fetch: {e}")
        return 1

    cap = {"captured_at": clocks(), "summary": summary, "PopOutage": payload}
    (OUT / f"popoutage_FULL_{stamp}.json").write_text(json.dumps(cap, indent=1), encoding="utf-8")
    log(f"captured PopOutage: {len(payload) if isinstance(payload, list) else '?'} record(s)")

    if isinstance(payload, list) and payload:
        log(f"FIELD NAMES: {sorted(payload[0].keys())}")

    # ---- stability poll: 4 x 90s ----
    snaps = []
    for i in range(4):
        try:
            p = S.get(OUTAGE, timeout=30).json()
        except Exception as e:
            log(f"  poll {i+1} error: {e}")
            p = []
        snaps.append({"poll": i + 1, "clocks": clocks(),
                      "records": [ident_view(o) for o in p] if isinstance(p, list) else []})
        log(f"  stability poll {i+1}: {len(p) if isinstance(p, list) else '?'} record(s)")
        if i < 3:
            time.sleep(90)
    (OUT / f"stability_{stamp}.json").write_text(json.dumps(snaps, indent=1), encoding="utf-8")
    log("stability snapshots saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
