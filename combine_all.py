"""
Combine every utility's scraped outage data into one unified CSV: Test_Danish_CSV.csv

Seven platforms with seven different schemas are normalized to a common set of columns.
Design notes (kept honest rather than silently coerced):
  * `id_stability` records what we actually VERIFIED about each utility's identifier, so a
    downstream user knows which ids can be used for duration tracking and which cannot.
  * Timestamps: each source is converted to true UTC only where its timezone was VERIFIED.
    Where a source ships a display-local string we keep it in *_local_raw and leave the UTC
    column blank rather than guessing.
  * `customers_affected_is_proxy` flags Anaheim, where there is no customer-count field and
    the affected-service-point count stands in for it.
  * Nothing is deduplicated: every row is one observation of one outage at one poll time,
    which is what makes this a time series.
"""

import csv, datetime as dt, json, pathlib, sys

ROOT = pathlib.Path(__file__).parent
OUTFILE = ROOT / "Test_Danish_CSV.csv"

COLUMNS = [
    "utility", "platform", "dataset", "outage_id", "id_stability",
    "customers_affected", "customers_affected_is_proxy",
    "cause", "circuit", "area", "county",
    "latitude", "longitude",
    "start_utc", "start_local_raw", "etr_utc", "etr_local_raw",
    "actual_end_utc", "status", "duration_minutes",
    "retrieved_utc", "source_file", "extra_json",
]


def rows_of(rel):
    p = ROOT / rel
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def blank():
    return {c: "" for c in COLUMNS}


def iso_utc(s):
    """Normalize an offset-aware ISO string (e.g. '...-07:00') to true UTC 'Z'."""
    if not s:
        return ""
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            return ""
        return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


out = []

# ---------------- SCE (ArcGIS) : two collection runs ----------------
for rel, dataset in [("la_oc_outages_history.csv", "sce_la_oc"),
                     ("orange_county_outages_history.csv", "sce_oc_baseline")]:
    for r in rows_of(rel):
        b = blank()
        b.update(
            utility="SCE", platform="ArcGIS (esriemcs)", dataset=dataset,
            outage_id=r.get("outage_id", ""),
            id_stability="STABLE (IncidentId; not stress-tested)",
            customers_affected=r.get("customers_affected", ""),
            customers_affected_is_proxy="false",
            cause=r.get("cause", ""),
            circuit=r.get("circuit_name", ""),          # always null in SCE's public feed
            area=r.get("city", ""), county=r.get("county", ""),
            latitude=r.get("latitude", ""), longitude=r.get("longitude", ""),
            start_utc=r.get("start_time_utc", ""),
            etr_local_raw=r.get("etr", ""),             # display string, tz unverified
            status=r.get("status", ""),
            retrieved_utc=r.get("retrieved_utc", ""), source_file=rel,
            extra_json=json.dumps({k: r.get(k) for k in
                                   ("feed", "outage_type", "crew_status", "result",
                                    "problem_code", "incident_type", "district_no",
                                    "sector_no", "zip", "oan_no") if r.get(k)}),
        )
        out.append(b)

# ---------------- LADWP (ArcGIS) ----------------
for r in rows_of("ladwp/ladwp_la_outages_history.csv"):
    b = blank()
    b.update(
        utility="LADWP", platform="ArcGIS Online", dataset="ladwp_la",
        outage_id=r.get("objectid_snapshot", ""),
        id_stability="UNSTABLE (OBJECTID rotates every refresh - do NOT track)",
        customers_affected=r.get("customers_out", ""), customers_affected_is_proxy="false",
        cause="",                                        # no cause field exists
        circuit="",                                      # no circuit field exists
        area=r.get("neighborhood", ""), county="LOS ANGELES",
        latitude=r.get("latitude", ""), longitude=r.get("longitude", ""),
        start_utc="",                                    # no start time in this feed
        etr_utc=r.get("etr_utc", ""), etr_local_raw=r.get("etr_display", ""),
        status=r.get("status", ""),
        retrieved_utc=r.get("retrieved_utc", ""),
        source_file="ladwp/ladwp_la_outages_history.csv",
        extra_json=json.dumps({k: r.get(k) for k in ("region", "outage_number") if r.get(k)}),
    )
    out.append(b)

# ---------------- SDGE (Kubra) ----------------
for r in rows_of("sdge/sdge_outages_history.csv"):
    b = blank()
    b.update(
        utility="SDGE", platform="Kubra StormCenter", dataset="sdge",
        outage_id=r.get("synthetic_id", ""),
        id_stability="SYNTHETIC (circuits+start_time; inc_id always null, '9-0' id positional)",
        customers_affected=r.get("customers_out", ""), customers_affected_is_proxy="false",
        cause=r.get("cause", ""), circuit=r.get("circuits", ""),
        area=r.get("communities", ""), county="SAN DIEGO",
        latitude=r.get("centroid_lat", ""), longitude=r.get("centroid_lng", ""),
        start_utc=r.get("start_utc", ""), etr_utc=r.get("etr_utc", ""),
        retrieved_utc=r.get("retrieved_utc", ""),
        source_file="sdge/sdge_outages_history.csv",
        extra_json=json.dumps({k: r.get(k) for k in
                               ("cause_detail", "crew_status", "n_out", "tile_quadkey",
                                "deployment_guid") if r.get(k)}),
    )
    out.append(b)

# ---------------- Glendale + Azusa (DataVoice) ----------------
for util, rel in [("Glendale", "glendale/glendale_outages_history.csv"),
                  ("Azusa", "azusa/azusa_outages_history.csv")]:
    for r in rows_of(rel):
        sub, fdr = r.get("substation", ""), r.get("feeder", "")
        b = blank()
        b.update(
            utility=util, platform="DataVoice OMS", dataset=util.lower(),
            outage_id=r.get("incident_id", ""),
            id_stability="STABLE (incident_id verified constant over 4+ polls)",
            customers_affected=r.get("consumers_affected", ""), customers_affected_is_proxy="false",
            cause="",                                    # DataVoice feed has NO cause field
            circuit=(f"sub {sub} / fdr {fdr}" if sub or fdr else ""),
            area="", county="LOS ANGELES",
            latitude=r.get("latitude", ""), longitude=r.get("longitude", ""),
            start_utc=r.get("start_utc", ""), start_local_raw=r.get("start_local_raw", ""),
            etr_utc=r.get("etr_utc", ""), etr_local_raw=r.get("etr_local_raw", ""),
            duration_minutes=r.get("duration_minutes", ""),
            retrieved_utc=r.get("retrieved_utc", ""), source_file=rel,
            extra_json=json.dumps({k: r.get(k) for k in
                                   ("substation", "feeder", "duration_raw", "outage_comment",
                                    "poly_vertices") if r.get(k)}),
        )
        out.append(b)

# ---------------- Burbank (POP) ----------------
for r in rows_of("burbank/burbank_outages_history.csv"):
    b = blank()
    b.update(
        utility="Burbank", platform="POP (self-hosted)", dataset="burbank",
        outage_id=r.get("displayID", ""),
        id_stability="STABLE (displayID + record_id constant over 3-4 polls)",
        customers_affected=r.get("current_affected", ""), customers_affected_is_proxy="false",
        cause="", circuit="",                            # no cause/circuit fields exist
        area=(r.get("areaName") if r.get("areaName") not in ("N/A",) else ""),
        county="LOS ANGELES",
        latitude=r.get("latitude", ""), longitude=r.get("longitude", ""),
        start_utc=r.get("outage_start_utc", ""),         # already true UTC (verified)
        etr_utc=r.get("published_etr_utc", ""),
        retrieved_utc=r.get("retrieved_utc", ""),
        source_file="burbank/burbank_outages_history.csv",
        extra_json=json.dumps({"record_id": r.get("record_id"),
                               "service_type": r.get("service_type"),
                               "has_polygon": r.get("has_polygon")}),
    )
    out.append(b)

# ---------------- Pasadena power (WebOutageViewer) : unpack nested JSON ----------------
for r in rows_of("pasadena/pasadena_power_outages_history.csv"):
    try:
        o = json.loads(r.get("all_fields_json") or "{}")
    except Exception:
        o = {}
    loc = o.get("OutageLocation") or {}
    b = blank()
    b.update(
        utility="Pasadena", platform="WebOutageViewer (custom)", dataset="pasadena_power",
        outage_id=o.get("OutageRecID", ""),
        id_stability="STABLE (OutageRecID constant over 4 captures)",
        customers_affected=o.get("CustomersOutNow", ""), customers_affected_is_proxy="false",
        cause=(o.get("VerifiedCause") or o.get("SuspectedCause") or ""),
        circuit=" / ".join(x for x in [o.get("Substation"), o.get("Feeder")] if x),
        area=o.get("OutageName", ""), county="LOS ANGELES",
        latitude=loc.get("Y", ""), longitude=loc.get("X", ""),
        start_utc=iso_utc(o.get("OutageStartTime")),     # ships explicit -07:00 offset
        start_local_raw=o.get("OutageStartTime", ""),
        etr_utc=iso_utc(o.get("EstimatedTime")), etr_local_raw=o.get("EstimatedTime", ""),
        actual_end_utc=iso_utc(o.get("OutageEndTime")),  # real restoration when populated
        status=o.get("Status") or "",
        retrieved_utc=r.get("retrieved_utc", ""),
        source_file="pasadena/pasadena_power_outages_history.csv",
        extra_json=json.dumps({k: o.get(k) for k in
                               ("CustomersRestored", "CustomersOutInitially", "Verified",
                                "CrewDispatched", "TroubledElement", "Notes", "District")
                               if o.get(k) not in (None, "")}),
    )
    out.append(b)

# ---------------- Anaheim (ASP.NET / ArcGIS front end) ----------------
for r in rows_of("anaheim/anaheim_outages_history.csv"):
    b = blank()
    b.update(
        utility="Anaheim", platform="ASP.NET .cshtml (ArcGIS front end)", dataset="anaheim",
        outage_id=r.get("incident_id", ""),
        id_stability="STABLE (ID constant over 3 polls; single incident observed)",
        customers_affected=r.get("affected_points", ""),
        customers_affected_is_proxy="true",              # point count, NOT a customer field
        cause="", circuit="",                            # neither field exists
        area="", county="ORANGE",
        latitude=r.get("centroid_lat", ""), longitude=r.get("centroid_lng", ""),
        start_utc=r.get("start_utc", ""), start_local_raw=r.get("start_local_raw", ""),
        etr_utc=r.get("etr_utc", ""), etr_local_raw=r.get("etr_local_raw", ""),
        status=r.get("type", ""),
        retrieved_utc=r.get("retrieved_utc", ""),
        source_file="anaheim/anaheim_outages_history.csv",
        extra_json=json.dumps({k: r.get(k) for k in
                               ("bbox_lat_min", "bbox_lng_min", "bbox_lat_max", "bbox_lng_max")
                               if r.get(k)}),
    )
    out.append(b)

# ---------------- write ----------------
with OUTFILE.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(out)

print(f"Wrote {OUTFILE}  ({len(out)} rows, {len(COLUMNS)} columns)\n")

# ---------------- summary ----------------
from collections import Counter, defaultdict
byu = Counter(r["utility"] for r in out)
print(f"{'utility':10} {'rows':>5} {'outages':>8} {'platform':34} {'cause':>6} {'circuit':>8} {'start':>6} {'lat/lng':>8}")
print("-" * 100)
for u in sorted(byu, key=lambda x: -byu[x]):
    rs = [r for r in out if r["utility"] == u]
    ids = {r["outage_id"] for r in rs if r["outage_id"]}
    def has(f): return f"{sum(1 for r in rs if r[f] not in ('', None))}/{len(rs)}"
    print(f"{u:10} {len(rs):>5} {len(ids):>8} {rs[0]['platform'][:34]:34} "
          f"{has('cause'):>6} {has('circuit'):>8} {has('start_utc'):>6} {has('latitude'):>8}")
print("-" * 100)
print(f"{'TOTAL':10} {len(out):>5} {len({(r['utility'], r['outage_id']) for r in out if r['outage_id']}):>8}")
print(f"\nrows with a real (non-proxy) customer count: "
      f"{sum(1 for r in out if r['customers_affected'] and r['customers_affected_is_proxy']=='false')}")
print(f"rows with an ACTUAL restoration timestamp:  {sum(1 for r in out if r['actual_end_utc'])}")
print(f"distinct causes present: {sorted({r['cause'] for r in out if r['cause']})[:8]}")
