# Step 1 Findings — SCE Live Outage Data Endpoint

**Date discovered:** 2026-07-31
**Method:** Headless Chromium (Playwright) load of the outage-status page + programmatic
ZIP search (92617/92660/92701), View-By toggle, and map pan/zoom, capturing all XHR/fetch.
Raw capture in `./capture/` (requests.jsonl, bodies/, page.html, summary.txt).

## Platform
**Esri ArcGIS** — self-hosted on **Esri Managed Cloud Services** (`*.esriemcs.com`),
using the ArcGIS Maps SDK for JS v4.34. **Not Kubra.** Not SCE's own `/v1/power/outage`
backend (that old path is dead, as expected).

## The endpoint that returns real outage records (THE ONE WE WANT)

```
https://sce-outage-ags.esriemcs.com/arcgis/rest/services/43/outage/MapServer/{LAYER}/query
```

- **Layer 0 = "Outages"** (unplanned / active outages)
- **Layer 1 = "Scheduled Outages"**

Both are standard ArcGIS FeatureService `/query` endpoints — fully parameterizable
(`where`, `outFields`, `returnGeometry`, `f=json`, `outSR`). GET, returns JSON.

### Confirmed sample response (layer 1, ZIP 92660 Newport Beach) — real data:
Fields returned per record (via `outFields`):

| Field | Meaning | Example |
|---|---|---|
| `IncidentId` / `OanNo` | outage IDs | 136317790 / 800553164 |
| `CountyName` | **county (tagged directly!)** | `ORANGE` |
| `CityName` | city | `NEWPORT BEACH` |
| `ZipcodeName` | ZIP | `92660` |
| `Circuit_Number` | **circuit name/number** | (null on scheduled; expected populated on unplanned) |
| `NoOfAffectedCust_Inci` | customers affected (exact int) | 3, 37, 7 |
| `MemoCauseCdDesc` | cause | `Cable Upgrade`, `Upgrading Equipment` |
| `OutageStartDateTime` | start (epoch ms) | 1785574800000 |
| `ERT` | estimated restoration time | `08/01/2026 12:00:00 PM` |
| `Status` | status | `ACTIVE` |
| `OutageType` | S=scheduled, etc. | `S` |
| `ProblemCode`, `MemoCauseCd`, `JobStatus`, `CrewStatusCd`, `IncidentType`, `PublishDate`, `LastChngDateTime`, `RO_Group_Numbers`, `Future_RO_Groups` | operational detail | |

**Resolution achieved: circuit-level schema, with county + city + ZIP + exact customer
counts + lat/lng available.** `CountyName='ORANGE'` means we can filter to Orange County
directly — no bounding-box guessing needed. The site's own query set `returnGeometry=false`,
but we can request `returnGeometry=true` to also get point lat/lng.

## Other endpoints seen (context, not our target)
- `tiles.sce-outage.esriemcs.com/.../43/outages|psps|reference/.../tile/{z}/{x}/{y}.pbf`
  — pre-baked **vector tiles** that just *draw* the map dots. Not tabular; ignore.
- `basemaps.sce-outage.esriemcs.com/...` — basemap vector tiles. Ignore.
- `sce-outage-ags.esriemcs.com/.../43/reference/MapServer/{4,5,6}/query` — reference
  layers: 4=High Fire Risk Areas, 5=Downstream Circuits, 6=SCE Territory. Not outages.
- `sce-outage-ags.esriemcs.com/.../43/outage_support/MapServer/2/query` — "Major Outages".
- `sce-outage-ags.esriemcs.com/.../43/lastupdate_time/MapServer/6/query` — feed timestamp.
- `.../43/psps/...` — Public Safety Power Shutoff layers (polygons).

## The exact query the site issued (per ZIP), URL-decoded
```
GET /arcgis/rest/services/43/outage/MapServer/1/query
  where = ZipcodeName = '92660' AND UPPER(CityName) = UPPER('Newport Beach')
          AND (OanNo is not null)
          AND ((OutageType = 'S') OR (JobStatus NOT IN('X','Z') AND ProblemCode NOT IN('13','19')))
          AND (UPPER(Status) = 'ACTIVE')
  outFields = ZipcodeName,CountyName,CityName,OutageStartDateTime,ERT,Status,JobStatus,
              MemoCauseCdDesc,OanNo,NoOfAffectedCust_Inci,IncidentType,IncidentId,
              LastChngDateTime,MemoCauseCd,PublishDate,RO_Group_Numbers,Future_RO_Groups,
              Circuit_Number,OutageType,ProblemCode,CrewStatusCd
  returnGeometry = false
  orderByFields = ZipcodeName,OanNo
  f = json
```

## Open question for Step 2 (validation)
- Site queries were ZIP+City scoped. For our purpose we can likely issue ONE broad query:
  `where=UPPER(CountyName)='ORANGE'` (+ status logic), `returnGeometry=true`, to pull all
  Orange County outages at once. Must confirm the layer allows an unscoped/`1=1`-style
  where without a browser session token, and whether there's a `resultRecordCount` cap
  (default 1000/2000 on ArcGIS) needing pagination.
- Confirm no auth cookie/token is required when called with plain `requests` (Step 2).
