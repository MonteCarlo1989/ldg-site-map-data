"""Ordered, audited NFHL recovery. No mirror is treated as a different layer type."""
import json
import re
import traceback
import threading
from datetime import datetime, timezone
from shapely.geometry import box
from arcgis import now
from config import FEMA, EXTENTS
from nfhl_county import CountyProduct
from output import normalized

ALTERNATE = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer"
REDUCED = "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Flood_Hazard_Reduced_Set_gdb/FeatureServer/0"
FULL = "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Flood_Hazard_Areas/FeatureServer"
AGOL = "https://www.arcgis.com/sharing/rest"
LAYERS = {
    "nfhl_flood_zones": dict(layer=28, extent="CONTEXT", archive="S_FLD_HAZ_AR",
                            names=["Flood Hazard Zones", "S_FLD_HAZ_AR", "Flood Hazard Areas"],
                            fields=["FLD_ZONE", "ZONE_SUBTY", "SFHA_TF", "STATIC_BFE",
                                    "DEPTH", "DFIRM_ID", "SOURCE_CIT"]),
    "nfhl_firm_panels": dict(layer=3, extent="SITE", archive="S_FIRM_PAN",
                            names=["FIRM Panels", "S_FIRM_PAN"],
                            fields=["FIRM_PAN", "EFF_DATE", "PANEL_TYP"]),
    "nfhl_bfe": dict(layer=16, extent="CONTEXT", archive="S_BFE",
                     names=["Base Flood Elevations", "S_BFE"], fields=["ELEV"]),
    "nfhl_lomr": dict(layer=1, extent="SITE", archive="S_LOMR",
                      names=["LOMRs", "LOMR", "S_LOMR"],
                      fields=["LOMR_ID", "EFF_DATE", "CASE_NO", "STATUS"]),
}


def key(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def effective_date(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    return value


class NFHLFallback:
    def __init__(self, client, out):
        self.client, self.out = client, out
        self.county = CountyProduct(client)
        self.state_candidates = None
        self.state_error = None
        self.run_lock = threading.RLock()

    def verified_item(self, item):
        owner = item.get("owner")
        if not owner or not item.get("url"):
            return None
        try:
            profile = self.client.metadata(AGOL + "/community/users/" + owner)
            org_id = profile.get("orgId")
            if not org_id:
                return None
            org = self.client.metadata(AGOL + "/portals/" + org_id)
            organization = str(org.get("name", ""))
            approved = ("florida division of emergency management",
                        "florida department of environmental protection",
                        "florida geographic information office")
            if not any(name in organization.lower() for name in approved):
                return None
            return dict(item_id=item["id"], owner=owner, owner_org_id=org_id,
                        owner_organization=organization, item_title=item.get("title"),
                        service_url=item["url"].rstrip("/"))
        except Exception:
            self.out.log(f"OWNER VERIFICATION FAILED item={item.get('id')} owner={owner}\n"
                         + traceback.format_exc())
            return None

    def query(self, url, dataset):
        spec = LAYERS[dataset]
        metadata = self.client.metadata(url)
        returned_fields = [f["name"] for f in metadata.get("fields", [])]
        if dataset == "nfhl_flood_zones" and "fld_zone" not in [f.lower() for f in returned_fields]:
            raise ValueError("Not a flood-hazard-area layer: no FLD_ZONE field")
        features, params, requests = self.client.features(
            url, EXTENTS[spec["extent"]], ["*"])
        observed = {k for f in features for k in (f.get("properties") or {})}
        returned_fields = sorted(set(returned_fields) | observed)
        return features, returned_fields, dict(
            source_url=url + "/query", request_parameters=params,
            http_status=requests[-1]["http_status"] if requests else None,
            layer_name=metadata.get("name"), layer_id=metadata.get("id"))

    def service_layers(self, service, dataset, richer=False):
        root = self.client.metadata(service)
        spec = LAYERS[dataset]
        matches = []
        for layer in root.get("layers", []):
            url = service + "/" + str(layer["id"])
            match_name = key(layer["name"]) in {key(n) for n in spec["names"]}
            try:
                meta = self.client.metadata(url)
            except Exception:
                self.out.log("LAYER PROBE FAILED " + url + "\n" + traceback.format_exc())
                continue
            fields = {f["name"].upper() for f in meta.get("fields", [])}
            geometry = meta.get("geometryType", "")
            if dataset == "nfhl_flood_zones":
                eligible = "FLD_ZONE" in fields and "Polygon" in geometry
                if richer:
                    eligible = eligible and "STATIC_BFE" in fields
            elif dataset == "nfhl_firm_panels":
                eligible = (match_name or "FIRM_PAN" in fields) and "Polygon" in geometry
            elif dataset == "nfhl_bfe":
                eligible = (match_name or "ELEV" in fields) and "Polyline" in geometry
            else:
                eligible = (match_name or "LOMR_ID" in fields) and "Polygon" in geometry
            if eligible:
                matches.append(url)
        return matches

    def authorized_state_items(self):
        if self.state_candidates is not None:
            if self.state_error:
                raise ValueError(self.state_error)
            return self.state_candidates
        self.state_candidates = []
        try:
            start = 1
            while start != -1:
                result, _ = self.client.request(AGOL + "/search", dict(
                    f="json", q='type:"Feature Service" AND (NFHL OR "Flood Hazard") '
                                 'AND (Florida OR FDEP OR FDEM OR FGIO)',
                    num=100, start=start))
                for item in result.get("results", []):
                    if item.get("access") != "public" or item.get("type") != "Feature Service":
                        continue
                    # Verify ownership from the organization, not item keywords.
                    source = self.verified_item(item)
                    if source is None:
                        continue
                    self.state_candidates.append(source)
                    self.out.log("VERIFIED FLORIDA MIRROR " + json.dumps(source))
                next_start = result.get("nextStart", -1)
                if next_start != -1 and next_start <= start:
                    raise ValueError("ArcGIS Online search pagination did not advance")
                start = next_start
            return self.state_candidates
        except Exception as exc:
            self.state_error = str(exc)
            raise

    def prepare(self, dataset, features, returned_fields):
        """Canonical requested columns, nulls for unavailable fields; exact clipping."""
        spec = LAYERS[dataset]
        source_names = {f.upper(): f for f in returned_fields}
        missing = [f for f in spec["fields"] if f not in source_names]
        result = []
        for feature in features:
            properties = feature.get("properties") or {}
            actual = {k.upper(): v for k, v in properties.items()}
            f = dict(feature, properties={field: actual.get(field) for field in spec["fields"]})
            clipped = normalized(f, box(*EXTENTS[spec["extent"]]), simplify=False)
            if clipped:
                result.append(clipped)
        return result, missing

    def recover(self, dataset):
        spec = LAYERS[dataset]
        entry = dict(id=dataset, kind="vector", extent=spec["extent"], timestamp=now(),
                     fields=spec["fields"], crs="EPSG:4326", bbox=EXTENTS[spec["extent"]],
                     simplify_meters=0, files=[], attempts=[], source_url=None,
                     http_status=None, request_parameters=None)
        empty_sources = []

        def attempt(stage, source, operation):
            record = dict(stage=stage, source_url=source, timestamp=now())
            entry["attempts"].append(record)
            try:
                features, fields, provenance = operation()
                prepared, missing = self.prepare(dataset, features, fields)
                record.update(returned_fields=fields, source_feature_count=len(features),
                              intersecting_feature_count=len(prepared))
                if not prepared:
                    record.update(status="empty", reason="No features intersect the required extent")
                    empty_sources.append((stage, fields, provenance, missing))
                    return False
                files, count, subdivided = self.out.vectors(
                    dataset, prepared, EXTENTS[spec["extent"]], simplify=False)
                # Remove obsolete parts of this NFHL dataset only, after successful serialization.
                new_names = {f["name"] for f in files}
                from config import DATA
                for path in DATA.glob(dataset + ".*"):
                    if path.name not in new_names:
                        path.unlink()
                entry.update(provenance, status="success", selected_source=stage,
                             returned_fields=fields, missing_fields=missing,
                             missing_field_policy="Unavailable requested columns are present with null values.",
                             source_feature_count=len(features), feature_count=count,
                             spatially_subdivided_features=subdivided, files=files)
                record["status"] = "success"
                if dataset == "nfhl_flood_zones":
                    entry["FLD_ZONE"] = sorted({str(f["properties"]["FLD_ZONE"]) for f in prepared
                                                 if f["properties"]["FLD_ZONE"] is not None})
                if dataset == "nfhl_firm_panels":
                    panels = {}
                    for f in prepared:
                        p = f["properties"]
                        panels[(p["FIRM_PAN"], p["EFF_DATE"])] = dict(
                            FIRM_PAN=p["FIRM_PAN"], EFF_DATE=p["EFF_DATE"],
                            effective_date_utc=effective_date(p["EFF_DATE"]))
                    entry["firm_panels"] = list(panels.values())
                if dataset == "nfhl_bfe":
                    entry["note"] = "FEMA BFE is layer 16. Layer 14 is Cross-Sections, not BFE."
                return True
            except Exception as exc:
                record.update(status="failed", error=str(exc)[:2000],
                              full_error_location="log.txt and log.part*.txt")
                self.out.log(f"NFHL FALLBACK FAILED {dataset} {stage} {source}\n{traceback.format_exc()}")
                return False

        # Known, previously validated FEMA layer IDs avoid a failing root lookup.
        for service in (FEMA, ALTERNATE):
            url = f"{service}/{spec['layer']}"
            if attempt("1 FEMA TLS1.2 HTTP/1.1", url, lambda u=url: self.query(u, dataset)):
                return entry

        if dataset == "nfhl_flood_zones":
            if attempt("2 Living Atlas reduced", REDUCED, lambda: self.query(REDUCED, dataset)):
                return entry
        else:
            entry["attempts"].append(dict(stage="2 Living Atlas reduced", source_url=REDUCED,
                                         status="not_applicable",
                                         reason="Flood-hazard-area polygons cannot substitute for " + dataset))

        def living_atlas():
            layers = self.service_layers(FULL, dataset, richer=True)
            if not layers:
                raise ValueError("Fuller Living Atlas service has no suitable layer")
            for layer in layers:
                data = self.query(layer, dataset)
                if self.prepare(dataset, data[0], data[1])[0]:
                    return data
            return data
        if attempt("2 Living Atlas full", FULL, living_atlas):
            return entry

        def state_mirror():
            items = self.authorized_state_items()
            for item in items:
                source = item["service_url"]
                try:
                    if re.search(r"/(?:FeatureServer|MapServer)/\d+$", source):
                        layers = [source]
                    else:
                        layers = self.service_layers(source, dataset)
                    for layer in layers:
                        metadata = self.client.metadata(layer)
                        fields = {f["name"].upper() for f in metadata.get("fields", [])}
                        # Never reinterpret flood polygons as panels, lines or LOMRs.
                        required = {"nfhl_flood_zones": "FLD_ZONE", "nfhl_firm_panels": "FIRM_PAN",
                                    "nfhl_bfe": "ELEV", "nfhl_lomr": "LOMR_ID"}[dataset]
                        if required not in fields:
                            continue
                        self.out.log(f"STATE MIRROR QUERY item={item['item_id']} URL={layer}")
                        features, returned_fields, provenance = self.query(layer, dataset)
                        if self.prepare(dataset, features, returned_fields)[0]:
                            return features, returned_fields, dict(provenance, **item)
                except Exception:
                    self.out.log("STATE MIRROR FAILED " + json.dumps(item) + "\n" + traceback.format_exc())
            raise ValueError("No public mirror from a verified Florida agency returned suitable features")
        if attempt("3 Verified Florida agency mirror", AGOL + "/search", state_mirror):
            return entry

        if attempt("4 MSC NFHL Data-County", "https://msc.fema.gov/portal/advanceSearch",
                   lambda: self.county.read(spec["archive"], EXTENTS[spec["extent"]])):
            return entry
        if empty_sources:
            # Zero features from a successfully completed real query is data,
            # not a service error. Only accept it after trying the entire chain.
            stage, fields, provenance, missing = empty_sources[0]
            files, count, _ = self.out.vectors(dataset, [], EXTENTS[spec["extent"]], simplify=False)
            from config import DATA
            for path in DATA.glob(dataset + ".*"):
                if path.name not in {f["name"] for f in files}:
                    path.unlink()
            entry.update(provenance, status="success", selected_source=stage,
                         returned_fields=fields, missing_fields=missing, files=files,
                         source_feature_count=0, feature_count=count, confirmed_empty=True,
                         no_fallback_returned_features=True, all_fallback_stages_exhausted=True,
                         empty_result_note="A completed real source query returned zero intersecting features. "
                                           "All later fallbacks were attempted; no features were fabricated.")
            return entry
        entry.update(status="failed", feature_count=0,
                     error="All four fallback stages failed or returned no suitable features for this dataset.",
                     all_fallback_stages_exhausted=True)
        return entry
