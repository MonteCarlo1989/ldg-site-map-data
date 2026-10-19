#!/usr/bin/env python3
"""Run once: python gis/acquire.py. Network failures are reported, never filled in."""
import fcntl
import io
import json
import math
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from PIL import Image
from shapely.geometry import mapping
from config import (ROOT, DATA, LIMIT, SITE, EXTENTS, WEB, FEMA, NWI, NHD,
                    TIGER, NAIP, IMAGERY, TOPO)
from arcgis import ArcGIS, now
from output import Output, encode, rounded, collection

LOCK = ROOT / ".acquisition.lock"


def is_running():
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def vector_job(client, out, spec):
    name, service, layers, extent, fields, where, simplify = spec
    if name.startswith("nfhl_"):
        from nfhl_fallback import NFHLFallback
        with client.lock:
            if not hasattr(client, "nfhl_fallback"):
                client.nfhl_fallback = NFHLFallback(client, out)
            fallback = client.nfhl_fallback
        with fallback.run_lock:
            return fallback.recover(name)
    entry = dict(id=name, kind="vector", extent=extent, timestamp=now(),
                 source_url=service, fields=fields, where=where,
                 request_parameters=None,
                 crs="EPSG:4326", bbox=EXTENTS[extent],
                 simplify_meters=50 if simplify else 0, files=[])
    try:
        features, sources, parameters, records = [], [], [], []
        for candidates in layers:
            layer = client.layer(service, candidates)
            url = f"{service}/{layer}"
            batch, params, used = client.features(url, EXTENTS[extent], fields, where)
            # Layer IDs distinguish otherwise colliding ArcGIS feature IDs.
            for feature in batch:
                if "id" in feature:
                    feature["id"] = f"{layer}:{feature['id']}"
            features.extend(batch)
            sources.append(url + "/query")
            parameters.append(params)
            records.extend(used)
        entry.update(source_url=sources, request_parameters=parameters,
                     http_status=records[-1]["http_status"] if records else None,
                     source_feature_count=len(features))
        files, count, subdivided = out.vectors(name, features, EXTENTS[extent], simplify)
        entry.update(status="success", files=files, feature_count=count,
                     spatially_subdivided_features=subdivided)
        if subdivided:
            entry["note"] = ("Features too large for one 90 KB FeatureCollection were spatially "
                             "subdivided without simplification; union by _ldg_source_id.")
        if name == "nfhl_flood_zones":
            entry["FLD_ZONE"] = sorted({str(f["properties"]["FLD_ZONE"]) for f in features
                                         if f["properties"].get("FLD_ZONE") is not None})
        if name == "nfhl_firm_panels":
            panels = {}
            for f in features:
                p = f["properties"]
                date = p.get("EFF_DATE")
                iso = (datetime.fromtimestamp(date / 1000, timezone.utc).isoformat()
                       if isinstance(date, (int, float)) else date)
                panels[p.get("FIRM_PAN")] = dict(FIRM_PAN=p.get("FIRM_PAN"),
                                                EFF_DATE=date, effective_date_utc=iso)
            entry["firm_panels"] = list(panels.values())
        if name == "nfhl_bfe":
            entry["note"] = "Live FEMA metadata: requested layer 14 is Cross-Sections; BFE is layer 16."
    except Exception as exc:
        related = [r for r in client.requests if r["url"].startswith(service)]
        last = related[-1] if related else {}
        entry.update(status="failed", error=str(exc), http_status=last.get("http_status"),
                     failed_request=dict(url=last.get("url"), parameters=last.get("parameters")))
        out.log(f"LAYER FAILED {name}\n{traceback.format_exc()}")
        # Failed layers are not exposed as apparently complete output.
        for path in DATA.glob(name + ".*"):
            path.unlink()
        entry["files"] = []
    return entry


def raster_job(client, out, name, extent, resolution, aerial=False):
    entry = dict(id=name, kind="raster", extent=extent, timestamp=now(), files=[],
                 target_ground_resolution_m=resolution, attempts=[])
    w, s, e, n = EXTENTS[extent]
    xmin, ymin = WEB.transform(w, s)
    xmax, ymax = WEB.transform(e, n)
    bbox = [xmin, ymin, xmax, ymax]
    web_resolution = resolution / math.cos(math.radians((s + n) / 2))
    for service in ([NAIP, IMAGERY] if aerial else [TOPO]):
        endpoint = service + ("/exportImage" if service.endswith("ImageServer") else "/export")
        try:
            meta = client.metadata(service)
            max_width = min(4096, meta.get("maxImageWidth", 4096))
            max_height = min(4096, meta.get("maxImageHeight", 4096))
            scale = max(web_resolution, (xmax - xmin) / max_width, (ymax - ymin) / max_height)
            width = min(max_width, max(1, math.ceil((xmax - xmin) / scale)))
            height = min(max_height, max(1, math.ceil((ymax - ymin) / scale)))
            params = dict(f="json", bbox=",".join(map(str, bbox)), bboxSR=3857,
                          imageSR=3857, size=f"{width},{height}", format="jpg")
            if service.endswith("ImageServer"):
                params.update(interpolation="RSP_BilinearInterpolation", adjustAspectRatio="true")
            else:
                params["transparent"] = "false"
            entry["attempts"].append(dict(source_url=endpoint, request_parameters=params))
            result, record = client.request(endpoint, params)
            if not result.get("href") or not result.get("extent"):
                raise ValueError(f"Export response has no image URL or georeferencing: {result}")
            jpeg, image_record = client.request(result["href"], binary=True)
            with Image.open(io.BytesIO(jpeg)) as image:
                actual_width, actual_height = image.size
                if image.format != "JPEG":
                    raise ValueError("Export is not a JPEG")
            ext = result["extent"]
            sr = ext.get("spatialReference", {})
            if (sr.get("latestWkid") or sr.get("wkid")) not in (3857, 102100, 102113):
                raise ValueError(f"Unexpected raster SR: {sr}")
            if (result.get("width", actual_width), result.get("height", actual_height)) != (actual_width, actual_height):
                raise ValueError("Export dimensions disagree with downloaded JPEG")
            exact = [ext[k] for k in ("xmin", "ymin", "xmax", "ymax")]
            pixel_x = (exact[2] - exact[0]) / actual_width
            pixel_y = -(exact[3] - exact[1]) / actual_height
            info = dict(bbox=exact, spatial_reference="EPSG:3857",
                        service_spatial_reference=sr, width=actual_width, height=actual_height,
                        source_url=endpoint, image_url=result["href"], request_parameters=params,
                        world_file=[pixel_x, 0, 0, pixel_y, exact[0] + pixel_x / 2,
                                    exact[3] + pixel_y / 2])
            files = out.raster(name, jpeg, info)
            entry.update(status="success", source_url=endpoint, request_parameters=params,
                         http_status=image_record["http_status"], export_http_status=record["http_status"],
                         bbox=exact, spatial_reference="EPSG:3857", width=actual_width,
                         height=actual_height, image_bytes=len(jpeg), files=files)
            break
        except Exception as exc:
            entry["attempts"].append(dict(source_url=endpoint, status="failed", error=str(exc)))
            out.log(f"RASTER SOURCE FAILED {name} {endpoint}\n{traceback.format_exc()}")
    else:
        entry.update(status="failed", error="All permitted raster sources failed", http_status=None)
    return entry


def specs():
    transport = TIGER + "/Transportation/MapServer"
    places = TIGER + "/Places_CouSub_ConCity_SubMCD/MapServer"
    states = TIGER + "/State_County/MapServer"
    roads = ["NAME", "MTFCC", "RTTYP"]
    return [
        ("nfhl_flood_zones", FEMA, [["Flood Hazard Zones", "S_FLD_HAZ_AR"]], "CONTEXT",
         ["FLD_ZONE", "ZONE_SUBTY", "SFHA_TF", "STATIC_BFE", "DEPTH", "DFIRM_ID", "SOURCE_CIT"], "1=1", False),
        ("nfhl_firm_panels", FEMA, [["FIRM Panels"]], "SITE", ["FIRM_PAN", "EFF_DATE", "PANEL_TYP"], "1=1", False),
        ("nfhl_bfe", FEMA, [["Base Flood Elevations", "S_BFE"]], "CONTEXT", ["ELEV"], "1=1", False),
        ("nfhl_lomr", FEMA, [["LOMRs", "LOMR"]], "SITE",
         ["LOMR_ID", "EFF_DATE", "CASE_NO", "STATUS"], "1=1", False),
        ("nwi_wetlands", NWI, [["Wetlands"]], "CONTEXT", ["ATTRIBUTE", "WETLAND_TYPE", "ACRES"], "1=1", False),
        ("nhd_flowline", NHD, [["Flowline - Large Scale", "NHDFlowline"]], "CONTEXT",
         ["gnis_name", "ftype", "fcode"], "1=1", False),
        ("nhd_waterbody", NHD, [["Waterbody - Large Scale", "NHDWaterbody"]], "CONTEXT",
         ["gnis_name", "ftype", "fcode"], "1=1", False),
        ("nhd_area", NHD, [["Area - Large Scale", "NHDArea"]], "CONTEXT",
         ["gnis_name", "ftype", "fcode"], "1=1", False),
        ("roads_context", transport, [["Primary Roads"], ["Secondary Roads 72_1k scale"], ["Local Roads"]],
         "CONTEXT", roads, "1=1", False),
        ("roads_vicinity", transport, [["Primary Roads"], ["Secondary Roads 72_1k scale"]],
         "VICINITY", roads, "MTFCC IN ('S1100','S1200')", True),
        ("railroads_context", transport, [["Railroads"]], "CONTEXT", ["NAME", "MTFCC"], "1=1", False),
        ("perry_place", places, [["Incorporated Places"]], "CONTEXT", ["NAME", "GEOID"],
         "STATE='12' AND BASENAME='Perry'", False),
        ("places_vicinity", places, [["Incorporated Places"]], "VICINITY", ["NAME", "GEOID"], "1=1", True),
        ("florida_counties", states, [["Counties"]], "FLORIDA", ["NAME", "GEOID"], "STATE='12'", True),
        ("florida_state", states, [["States"]], "FLORIDA", ["NAME", "GEOID"], "STATE='12'", True),
    ]


def write_manifest(out, manifest, client, final=False):
    # Full provenance is recorded once per dataset, inherited by each of its files.
    # This prevents hundreds of raster parts from duplicating identical parameters.
    manifest["updated_at"] = now()
    if final:
        requests_files = out.json_parts("requests", client.requests, "requests")
        manifest["request_ledger"] = [f["name"] for f in requests_files]
    file_rows = []
    owners = {f["name"]: e["id"] for e in manifest["datasets"] for f in e.get("files", [])}
    for path in sorted(DATA.iterdir()):
        if not path.is_file() or path.suffix == ".tmp" or path.name.startswith("manifest"):
            continue
        file_rows.append(dict(name=path.name, size=path.stat().st_size,
                              dataset=owners.get(path.name, "acquisition_audit")))
    manifest["files"] = file_rows
    manifest["audit_provenance"] = dict(
        id="acquisition_audit", source_url=None, request_parameters=None,
        http_status=None, timestamp=manifest["updated_at"], kind="generated audit",
    )
    manifest["provenance_convention"] = (
        "Each files[].dataset references datasets[].id or audit_provenance.id, which supplies source_url, "
        "request_parameters, timestamp, HTTP status, feature_count or image dimensions. "
        "Every network attempt including failed HTTP statuses is in request_ledger. "
        "Audit files are generated locally, source_url=null and http_status=null.")
    # Metadata files are text-safe too; exceptionally large catalogs use valid JSON parts.
    if len(encode(manifest)) >= LIMIT:
        catalogs = out.json_parts("manifest.files", file_rows, "files")
        manifest["file_catalog_parts"] = [f["name"] for f in catalogs]
        manifest["files"] = catalogs
    manifest["self"] = {"name": "manifest.json", "size": 0, "source_url": None,
                        "http_status": None}
    for _ in range(5):
        size = len(encode(manifest))
        if manifest["self"]["size"] == size:
            break
        manifest["self"]["size"] = size
    if len(encode(manifest)) >= LIMIT:
        raise ValueError("Manifest exceeds text limit even after catalog partitioning")
    out.write("manifest.json", encode(manifest))


def run():
    DATA.mkdir(exist_ok=True)
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # Only this application's output directory is regenerated.
        for path in DATA.iterdir():
            if path.is_file() and (path.suffix in (".geojson", ".json", ".b64", ".jpg", ".txt", ".tmp")):
                path.unlink()
        out = Output()
        from nfhl_transport import client as nfhl_client
        client = nfhl_client(out)
        manifest = dict(application="LDG Site Map Data", started_at=now(), status="running",
                        text_part_limit_bytes=LIMIT, vector_crs="EPSG:4326",
                        extents=EXTENTS, extent_padding_miles={"SITE": .5, "CONTEXT": 3, "VICINITY": 60},
                        extent_method="WGS84 geodesic cardinal offsets from site bounding box",
                        datasets=[], notes=[
                            "No SITE or CONTEXT geometry simplification; only six-decimal coordinate rounding.",
                            "VICINITY and full-state inset geometry simplified at 50 ground meters in local AEQD.",
                            "Full Florida inset uses a separate FLORIDA extent to avoid clipping it to the vicinity.",
                            "Source features too large for one text file are spatially subdivided, never simplified.",
                            "JPEGs are binary originals; .b64 parts are the text-consumer representation.",
                            "log.txt is the first log segment; subsequent log.partNNNN.txt files continue it.",
                        ])
        subject = dict(type="Feature", properties={"name": "Subject site"},
                       geometry=rounded(mapping(SITE)))
        subject_file = out.write("subject_site.geojson", encode(collection([subject])))
        manifest["datasets"].append(dict(id="subject_site", kind="vector", status="success",
                                        source_url="user-supplied polygon", request_parameters=None,
                                        http_status=None, timestamp=now(), feature_count=1,
                                        files=[subject_file]))
        out.log("Acquisition started; remote failures will be retained in the manifest.")
        write_manifest(out, manifest, client)
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                jobs = [executor.submit(vector_job, client, out, spec) for spec in specs()]
                jobs += [
                    executor.submit(raster_job, client, out, "naip_site", "SITE", 1, True),
                    executor.submit(raster_job, client, out, "naip_context", "CONTEXT", 4, True),
                    executor.submit(raster_job, client, out, "topo_vicinity", "VICINITY", 60),
                    executor.submit(raster_job, client, out, "topo_context", "CONTEXT", 4),
                ]
                for future in as_completed(jobs):
                    entry = future.result()
                    manifest["datasets"].append(entry)
                    print(f"{entry['id']}: {entry['status']}", flush=True)
                    write_manifest(out, manifest, client)
            failures = sum(e["status"] == "failed" for e in manifest["datasets"])
            manifest.update(status="partial" if failures else "complete",
                            failed_datasets=failures, finished_at=now())
            out.log(f"Acquisition finished; {failures} dataset failures.")
        except Exception:
            manifest.update(status="failed", finished_at=now(), error=traceback.format_exc())
            out.log(traceback.format_exc())
        if hasattr(client, "nfhl_fallback"):
            client.nfhl_fallback.county.close()
        write_manifest(out, manifest, client, final=True)
        return 0 if manifest["status"] in ("complete", "partial") else 1


if __name__ == "__main__":
    import sys
    if "--nfhl-only" in sys.argv:
        from nfhl_refresh import refresh
        raise SystemExit(refresh())
    raise SystemExit(run())
