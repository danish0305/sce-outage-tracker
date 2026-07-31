# SCE Outage Tracker — Los Angeles + Orange County

Polls **Southern California Edison (SCE)** live power-outage data every 15 minutes,
filters it to **Orange County** and **Los Angeles County**, and writes CSVs. A GitHub
Actions workflow runs the scrape on a schedule and commits the updated CSVs back to this repo.

## What it collects

Two independent scrapers hit the same public ArcGIS service family (Esri, self-hosted on
Esri Managed Cloud Services — **no API key or login required**):

| Scraper | Source layers | Geometry | Output |
|---|---|---|---|
| `sce_oc_outages.py` | `.../43/outage/MapServer/0` (active), `/1` (scheduled) | Point | `la_oc_outages_latest.csv`, `la_oc_outages_history.csv` |
| `sce_oc_psps.py` | `.../43/psps/MapServer/0` (under consideration), `/1` (active) | Polygon | `la_oc_psps_latest.csv`, `la_oc_psps_history.csv` |

### Endpoint

```
https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/outage/MapServer/{0,1}/query
https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/psps/MapServer/{0,1}/query
```

Queried as standard ArcGIS FeatureServer `/query` calls:
`where=UPPER(CountyName) IN ('ORANGE','LOS ANGELES')` (PSPS uses the field `COUNTY`),
`returnGeometry=true`, `outSR=4326`, `f=json`, with `resultOffset` pagination
(page size = the service's 2000-row `maxRecordCount`).

## Outage CSV schema (point layers)

`outage_id`, `oan_no`, `feed` (active/scheduled), `status`, `outage_type`,
`customers_affected`, `cause`, `circuit_name`, `city`, `county`, `zip`,
`latitude`, `longitude`, `start_time_utc`, `etr`, `crew_status`, `result`,
`problem_code`, `incident_type`, `district_no`, `sector_no`, `last_change`,
`publish_date`, `source_layer`, `retrieved_utc`.

## PSPS CSV schema (polygon layers)

Includes `psps_status`, `event_name`/`event_status`, `circuit_name`, `county`,
customer breakdown (`customers_total`, `cust_residential`, `cust_essential`,
`cust_major`, `cust_med_baseline`, `cust_med_baseline_critical`), `start_utc`/`end_utc`/`ert`,
`date_deenergized_utc`, a computed `centroid_lat`/`centroid_lng` + bounding box, and the full
polygon boundary as `geometry_rings_json`. PSPS events only exist during fire-weather /
high-wind conditions, so these CSVs are usually header-only — an **empty file is a correct
result, not a failure.**

## Resolution actually achieved

- ✅ Per-outage **point** location (lat/lng), plus city / ZIP / county tags
- ✅ **Exact** customers-affected counts, cause, start time, ETR, crew-status text
- ❌ **Not circuit-level for outages:** `Circuit_Number` exists in the schema but SCE leaves
  it null in the public outage feed. (PSPS polygons *do* carry `CIRCUIT_NAME`.)

## ⚠️ Coverage gaps — SCE territory only

This data covers only where **SCE** is the electric utility. Areas served by separate
**municipal utilities are excluded by definition**, notably:

- **City of Los Angeles** → LADWP (Los Angeles Dept. of Water & Power)
- **Anaheim** → Anaheim Public Utilities
- **Pasadena** → Pasadena Water & Power · **Glendale** → Glendale Water & Power ·
  **Burbank** → Burbank Water & Power · **Azusa** → Azusa Light & Water · **Vernon**

Caveat on `city`: `CityName` is a **postal/place label, not proof of provider**. A
municipal-utility city name can still appear as a small number of rows because SCE serves
fringe / unincorporated parcels that share that place name — those rows are SCE customers,
**not** the municipal utility's territory. Do not read them as coverage of that city's utility.

## Run locally

```bash
pip install -r requirements.txt
python sce_oc_outages.py --counties "ORANGE,LOS ANGELES" \
  --out la_oc_outages_latest.csv --history la_oc_outages_history.csv
python sce_oc_psps.py --counties "ORANGE,LOS ANGELES" \
  --out la_oc_psps_latest.csv --history la_oc_psps_history.csv
```

Omit `--counties` to get the Orange-County-only default. Use `--page-size N` to stress-test
pagination.

## Automation

`.github/workflows/scrape.yml` runs every 15 minutes (`*/15 * * * *`, **UTC**) and commits
updated CSVs back as `github-actions[bot]`. GitHub Actions cron is **best-effort** — runs are
often delayed several minutes and can be skipped under load; combined with SCE's own note that
reported outages can take up to 30 minutes to appear, treat this as near-real-time, not exact.

## Repo files

- `sce_oc_outages.py`, `sce_oc_psps.py` — the scrapers (portable; stdlib + `requests`)
- `discover_endpoint.py`, `validate_endpoint.py`, `STEP1_FINDINGS.md` — how the endpoint was
  discovered/validated (discovery needs `playwright`, not required for scraping)
- `.github/workflows/scrape.yml` — the scheduled job
