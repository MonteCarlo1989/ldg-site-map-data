"""ArcGIS transport with audit logging, retries and complete pagination."""
import json
import threading
import time
import traceback
from urllib.parse import urlparse
from datetime import datetime, timezone
import requests


def now():
    return datetime.now(timezone.utc).isoformat()


class ArcGIS:
    def __init__(self, output, session=None, timeout=(15, 75), headers=None):
        self.output = output
        self.session = session or requests.Session()
        self.timeout = timeout
        self.headers = headers or {"User-Agent": "LDG-Site-Map-Data/1.0"}
        self.requests = []
        self.cache = {}
        self.lock = threading.RLock()
        self.host_locks = {}
        self.metadata_locks = {}

    def request(self, url, params=None, binary=False, method="GET"):
        # Avoid concurrent bursts against one public service, while different
        # agencies still download concurrently.
        with self.lock:
            host_lock = self.host_locks.setdefault(urlparse(url).netloc, threading.RLock())
        with host_lock:
            return self._request(url, params, binary, method)

    def _request(self, url, params=None, binary=False, method="GET"):
        params = params or {}
        last = None
        for attempt in range(1, 4):
            record = dict(url=url, parameters=params, timestamp=now(),
                          attempt=attempt, http_status=None, method=method)
            full_url = requests.Request("GET", url, params=params).prepare().url
            self.output.log(f"REQUEST {method} {attempt}/3 {full_url}")
            response = None
            try:
                response = self.session.request(
                    method, url, params=params if method == "GET" else None,
                    data=params if method != "GET" else None, timeout=self.timeout,
                    headers=self.headers)
                record["http_status"] = response.status_code
                record["response_url"] = response.url
                response.raise_for_status()
                if binary == "text":
                    result = response.text
                elif binary == "zip":
                    if not response.content.startswith(b"PK"):
                        raise ValueError("Expected ZIP; response: " + response.text)
                    result = response.content
                elif binary:
                    if not response.content.startswith(b"\xff\xd8"):
                        raise ValueError("Expected JPEG; response: " + response.text)
                    result = response.content
                else:
                    result = response.json()
                    if "error" in result:
                        raise ValueError(json.dumps(result["error"], ensure_ascii=False))
                record["status"] = "success"
                self.output.log(f"RESPONSE {response.status_code} {full_url}")
                if response.history:
                    record["redirects"] = [r.url for r in response.history] + [response.url]
                    self.output.log("REDIRECTS " + json.dumps(record["redirects"]))
                with self.lock:
                    self.requests.append(record)
                return result, record
            except Exception as exc:
                last = exc
                record.update(status="failed", error=str(exc)[:8000],
                              full_error_location="log.txt and log.part*.txt")
                with self.lock:
                    self.requests.append(record)
                body = response.text if response is not None else ""
                self.output.log(f"ERROR {full_url}\n{traceback.format_exc()}\n{body}")
                if attempt < 3:
                    time.sleep(attempt * 2)
        raise RuntimeError(f"{url} failed after three attempts: {last}")

    def metadata(self, url):
        with self.lock:
            metadata_lock = self.metadata_locks.setdefault(url, threading.RLock())
        with metadata_lock:
            return self._metadata(url)

    def _metadata(self, url):
        with self.lock:
            cached = self.cache.get(url)
        if cached is not None:
            if isinstance(cached, Exception):
                raise cached
            return cached
        try:
            obj, _ = self.request(url, {"f": "json"})
        except Exception as exc:
            with self.lock:
                self.cache[url] = exc
            raise
        with self.lock:
            self.cache[url] = obj
        return obj

    def layer(self, service, names):
        layers = self.metadata(service).get("layers", [])
        for name in names:
            for layer in layers:
                if layer["name"].strip().lower() == name.lower():
                    return layer["id"]
        raise ValueError(f"No layer {names} in {service}; available: {layers}")

    def features(self, url, bbox, fields, where="1=1"):
        meta = self.metadata(url)
        available = {f["name"].lower(): f["name"] for f in meta.get("fields", [])}
        wildcard = fields == ["*"]
        missing = [] if wildcard else [f for f in fields if f.lower() not in available]
        if missing:
            raise ValueError(f"Requested fields absent in {url}: {missing}")
        fields = ["*"] if wildcard else [available[f.lower()] for f in fields]
        oid = next((f["name"] for f in meta["fields"]
                    if f["type"] == "esriFieldTypeOID"), None)
        if oid and oid not in fields and not wildcard:
            fields.append(oid)
        params = dict(f="geojson", where=where, outFields=",".join(fields),
                      geometry=",".join(map(str, bbox)), geometryType="esriGeometryEnvelope",
                      inSR=4326, outSR=4326, spatialRel="esriSpatialRelIntersects",
                      returnGeometry="true")
        count = min(meta.get("maxRecordCount", 1000), 1000)
        features = []
        requests_used = []
        supports_paging = meta.get("advancedQueryCapabilities", {}).get("supportsPagination", False)
        if supports_paging:
            if oid:
                params["orderByFields"] = oid
            offset = 0
            seen_pages = set()
            while True:
                page_params = dict(params, resultOffset=offset, resultRecordCount=count)
                page, record = self.request(url + "/query", page_params)
                requests_used.append(record)
                batch = page.get("features")
                if batch is None:
                    raise ValueError(f"Not a GeoJSON FeatureCollection: {page}")
                signature = json.dumps(batch, sort_keys=True)
                if batch and signature in seen_pages:
                    raise ValueError("Service repeated a page; refusing an incomplete result")
                seen_pages.add(signature)
                features.extend(batch)
                exceeded = page.get("exceededTransferLimit",
                                    page.get("properties", {}).get("exceededTransferLimit", False))
                if not exceeded and len(batch) < count:
                    break
                if not batch:
                    if exceeded:
                        raise ValueError("Transfer limit exceeded with an empty page")
                    break
                offset += len(batch)
        else:
            # Object-ID enumeration is not transfer-limited on ArcGIS query services.
            id_params = dict(params, f="json", returnIdsOnly="true", returnGeometry="false")
            ids, record = self.request(url + "/query", id_params)
            requests_used.append(record)
            if ids.get("exceededTransferLimit"):
                raise ValueError("Object-ID enumeration was truncated")
            if "objectIds" not in ids:
                raise ValueError(f"Missing objectIds: {ids}")
            object_ids = sorted(ids["objectIds"] or [])

            def fetch_ids(chunk):
                p = dict(params, objectIds=",".join(map(str, chunk)))
                page, rec = self.request(url + "/query", p)
                requests_used.append(rec)
                if page.get("exceededTransferLimit") or page.get("properties", {}).get("exceededTransferLimit"):
                    if len(chunk) <= 1:
                        raise ValueError("Single-feature transfer limit exceeded")
                    middle = len(chunk) // 2
                    return fetch_ids(chunk[:middle]) + fetch_ids(chunk[middle:])
                if "features" not in page:
                    raise ValueError(f"Missing features: {page}")
                return page["features"]

            for start in range(0, len(object_ids), count):
                features.extend(fetch_ids(object_ids[start:start + count]))
        return features, params, requests_used
