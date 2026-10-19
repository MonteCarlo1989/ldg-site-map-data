# LDG Site Map Data

Python GIS acquisition for one industrial site in Perry, Taylor County, Florida, with a minimal React status page.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the Python Flask API
- `pnpm --filter @workspace/ldg-site-map-data run dev` — run the status page
- `uv run python gis/acquire.py` — regenerate repository-root `data/`
- `pnpm --filter @workspace/api-server run build` — perform acquisition at build time
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- No database or API credentials are required.

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- Service: Python Flask; Gunicorn in production
- GIS: requests, Shapely, pyproj, Pillow
- Status page: React + Vite
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `gis/` — acquisition, ArcGIS transport, text-safe output, Python API; see `gis/README.md`
- `data/` — generated outputs, ignored by Git
- `lib/api-spec/openapi.yaml` — status and run contracts
- `artifacts/ldg-site-map-data/` — minimal status page

## Architecture decisions

- Keep output as local files in `data/`, not a database or cloud archive: another process reads these files directly.
- Do not add maps or other product UI. The user explicitly wants only files, manifest, log and rerun status.
- Never fill service failures with example or substitute data. The only allowed imagery fallback is the one specified by the user.
- NFHL fallback order is FEMA with verified TLS 1.2/browser headers, Living Atlas, verified Florida agency mirrors, then the current MSC county product. Never use flood-area polygons as panels, BFE lines or LOMRs.
- Targeted NFHL recovery must preserve unrelated file bytes, modification times, and dataset manifest entries. Append audit logs and new request ledgers rather than erasing earlier attempts.
- Full Florida inset layers use their own extent, not the site's vicinity, so the entire state remains available.

## Product

Acquires FEMA, NWI, NHD, TIGER, NAIP and USGS basemap data. Provides downloadable text-safe GeoJSON and base64 parts, raster georeferencing, manifest provenance and full request logs. GET or POST `/run` starts a background rerun.

## User preferences

The downstream consumer can read only text files of at most 100 KB. Output parts are capped at 90,000 bytes. Do not simplify SITE or CONTEXT vector geometry; use 50 ground meters for VICINITY and full-state inset layers.

## Gotchas

- Reruns regenerate the application-owned `data/` folder; the UI warns before replacing existing files.
- FEMA layer 14 is currently Cross-Sections; Base Flood Elevations resolves to layer 16. Record this correction in the manifest.
- ArcGIS catalogs determine layer IDs and allowed image sizes; do not assume those are stable.
- Logs and request ledgers are also partitioned to respect text size limits.
- `uv run python gis/acquire.py --nfhl-only` retries only FEMA outputs in place. See `gis/README.md` for source validation and empty-result handling.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
