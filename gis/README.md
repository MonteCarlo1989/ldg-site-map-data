# LDG Site Map Data

Python is the data acquisition implementation. The React app is only a status page.

## Run

From the repository root:

```sh
uv sync
uv run python gis/acquire.py
```

To retry **only** the four NFHL datasets without regenerating other data:

```sh
uv run python gis/acquire.py --nfhl-only
```

This mode updates the existing manifest, appends logs and adds new request-ledger
parts without replacing old ledgers. It verifies both checksums and modification
times of unrelated outputs, and preserves their dataset entries unchanged. A
failed retry retains an existing successful NFHL output, explicitly marked as a
failed refresh; it never presents that old output as newly downloaded.

The acquisition is also part of the API build command. Individual remote failures
do not abort other layers; the final status is `partial` and failed attempts remain
in the request ledger and log. `/run` (GET or POST) and `/api/run` (POST) start a
background rerun, returning HTTP 202. Concurrent runs are prevented by an OS lock.
`/api/status` provides status, complete manifest text, concatenated logs and files.
The page at `/` lists downloads and displays the manifest and log.

All output is in the repository-root `data/` folder. Reruns regenerate this
application-owned folder. Download needed results before rerunning. Local output
is intended for this workspace's downstream file consumer; it is not a persistent
cloud archive.

## Text consumer

- Every text file is at most 90,000 bytes (stricter than 100 KB).
- GeoJSON files are complete FeatureCollections in EPSG:4326. Load all numbered
  parts for a layer, in order. Empty successful layers have an empty collection.
- Six-decimal rounding is applied only to geometry coordinates, not attributes.
- No SITE or CONTEXT simplification. VICINITY and full-Florida inset data use a
  50-meter tolerance in a local equidistant projection.
- A single source feature can itself exceed the file limit. Such a feature is
  subdivided spatially, without simplification, into separately serializable
  features. `_ldg_source_id` and `_ldg_fragment` identify them for reconstruction.
  A layer's manifest records both source and output feature counts.
- JPEGs are the binary originals and are intentionally exempt from the text-file
  limit. Concatenate the `.b64` parts in the sidecar's listed order and decode
  base64 to recover the exact JPEG. Each sidecar records returned bbox, actual
  dimensions, SR and six world-file values (A, D, B, E, C, F; pixel-center origin).
- `manifest.json` lists datasets, inherited file provenance, sizes and summary
  metadata. `requests.partNN.json` gives every actual attempt, exact parameters,
  UTC timestamp, HTTP status and errors. The manifest's `request_ledger` lists
  those files. Large file catalogs, if needed, are in `file_catalog_parts`.
- `log.txt` is segment one. Append `log.partNNNN.txt` in filename order for the
  complete untruncated log. UTF-8 characters are never split across log files.

## Live service differences

FEMA's live layer 14 is Cross-Sections, not S_BFE. BFE uses the previously verified
layer 16; this correction is in its manifest. NFHL layer IDs are 28, 3, 16 and 1.
NHD uses the large-scale flowline,
waterbody and area layers. TIGER all-roads combines primary, secondary and local
roads; incorporated places use the actual Places_CouSub_ConCity_SubMCD service.
The Perry query uses `BASENAME='Perry' AND STATE='12'`, because NAME includes the
legal place suffix. Full-state inset outputs have a separate FLORIDA clip extent.
NAIP dimensions honor the smaller of the advertised service limit and 4096.

## NFHL fallback chain

For each dataset, continue until a suitable source returns intersecting features:

1. Standard FEMA endpoint, then `/gis/nfhl/rest/services/public/NFHL/MapServer`.
   Use a desktop browser User-Agent, HTTP/1.1, verified TLS exactly 1.2 with default
   ciphers, and a 60-second requests timeout. Certificate checks remain enabled.
2. Living Atlas reduced flood-area service, then probe the fuller NFHL service.
   The reduced service is only applicable to flood polygons, not panels/BFE/LOMRs.
3. Public ArcGIS Online feature services found by NFHL/Flood Hazard and Florida
   searches, restricted to owners whose organization identifies FDEM, FDEP or FGIO.
   Item ID, service URL and verified owning organization are logged and recorded.
4. The MSC advanced-search form for Florida/Taylor County: select the current
   EFFECTIVE NFHL Data-County product for 12123C; use its actual type/subtype/name
   to obtain the ZIP. GDAL/pyogrio reads S_FLD_HAZ_AR, S_FIRM_PAN, S_BFE and S_LOMR
   from extracted FileGDB/shapefiles in a temporary directory. The manifest records
   product name, effective date, download URL and archive layer.

An empty result does not stop the chain. All failures/empty sources are recorded.
If no fallback returns features but an authoritative query completed successfully
with zero intersecting features, save a valid empty FeatureCollection and mark
`confirmed_empty` in the manifest. This is distinct from every source failing:
all-failure datasets remain explicitly failed, with no invented output.
Source field names are recorded verbatim, requested output columns are canonical,
and missing requested fields are present with null values. No polygon mirror is
ever reinterpreted as a different FEMA layer type.

Offline regression checks: `uv run python -m unittest discover -s gis/tests -v`.
