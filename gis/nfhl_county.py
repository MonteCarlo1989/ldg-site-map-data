"""Find the current county NFHL product from MSC, not a guessed download URL."""
import io
import math
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode
import numpy as np
import pyogrio
from pyproj import CRS, Transformer
from shapely.geometry import mapping
from shapely import from_wkb
from shapely.ops import transform

MSC = "https://msc.fema.gov/portal/advanceSearch"


def scalar(value):
    if value is None:
        return None
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else np.datetime_as_string(value, unit="ms")
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


class CountyProduct:
    def __init__(self, client):
        self.client = client
        self.loaded = False
        self.error = None
        self.temp = None
        self.layers = []
        self.provenance = {}

    def load(self):
        if self.loaded:
            if self.error:
                raise RuntimeError(self.error)
            return
        self.loaded = True
        try:
            # Establish the MSC session, then use the actual advanced-search form.
            self.client.request(MSC, {"state": "12", "county": "12123"}, binary="text")
            counties, _ = self.client.request(MSC, {"getCounty": "12"})
            county = next(c for c in counties if "TAYLOR" in c["label"].upper())
            if str(county["value"]) not in ("123", "12123"):
                raise ValueError(f"Unexpected Taylor County identifier: {county}")
            params = dict(selstate="12", selcounty=county["value"], selcommunity="none",
                          txtjurisdictionname="", jurisdictionkey="", jurisdictionvalue="",
                          searchedCid="", searchedDateStart="", searchedDateEnd="",
                          txtproductid="", txtstartdate="", txtenddate="", method="search")
            previous_header = self.client.session.headers.get("X-Requested-With")
            self.client.session.headers["X-Requested-With"] = "XMLHttpRequest"
            try:
                products, _ = self.client.request(MSC, params, method="POST")
            finally:
                if previous_header is None:
                    self.client.session.headers.pop("X-Requested-With", None)
                else:
                    self.client.session.headers["X-Requested-With"] = previous_header
            choices = products.get("EFFECTIVE", {}).get("NFHL_COUNTY_DATA", [])
            choices = [p for p in choices if str(p.get("product_NAME", "")).upper().startswith("12123C")]
            if not choices:
                raise ValueError("MSC returned no current NFHL Data-County product for 12123C")
            # Only EFFECTIVE products are considered. The current published row
            # determines product IDs, type/subtype and effective date.
            def effective_date(product):
                value = product.get("product_EFFECTIVE_DATE_STRING", "")
                for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %I:%M:%S %p"):
                    try:
                        return datetime.strptime(value, fmt)
                    except ValueError:
                        pass
                return datetime.min
            product = max(choices, key=effective_date)
            download_params = dict(productTypeID=product["product_TYPE_ID"],
                                   productSubTypeID=product["product_SUBTYPE_ID"],
                                   productID=product["product_NAME"])
            download = "https://msc.fema.gov/portal/downloadProduct"
            url = download + "?" + urlencode(download_params)
            self.provenance = dict(product_name=product["product_NAME"],
                                   product_effective_date=product.get("product_EFFECTIVE_DATE_STRING"),
                                   product_description=product.get("product_DESCRIPTION"),
                                   download_url=url, source_url=url, search_parameters=params,
                                   request_parameters=download_params)
            self.client.output.log("MSC PRODUCT " + str(self.provenance))
            archive, record = self.client.request(download, download_params, binary="zip")
            self.provenance.update(http_status=record["http_status"],
                                   resolved_download_url=record.get("response_url"))
            self.temp = tempfile.TemporaryDirectory(prefix="ldg-nfhl-county-")
            root = Path(self.temp.name)
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                # Guard against path traversal and zip bombs in external archives.
                if sum(i.file_size for i in z.infolist()) > 4_000_000_000:
                    raise ValueError("County archive exceeds 4 GB uncompressed safety limit")
                for info in z.infolist():
                    path = (root / info.filename).resolve()
                    if not path.is_relative_to(root.resolve()):
                        raise ValueError("Unsafe archive member: " + info.filename)
                z.extractall(root)
            sources = list(root.rglob("*.gdb")) + list(root.rglob("*.shp"))
            for source in sources:
                for name, geometry_type in pyogrio.list_layers(source):
                    self.layers.append((source, str(name), str(geometry_type)))
            if not self.layers:
                raise ValueError("Downloaded county ZIP contains no readable geodatabase or shapefiles")
        except Exception as exc:
            self.error = str(exc)
            raise

    def read(self, expected_name, bbox):
        self.load()
        candidates = [l for l in self.layers
                      if l[1].split(".")[-1].upper() == expected_name.upper()]
        if not candidates:
            raise ValueError(f"County product lacks {expected_name}; available: {[l[1] for l in self.layers]}")
        source, layer, _ = candidates[0]
        info = pyogrio.read_info(source, layer=layer)
        if not info.get("crs"):
            raise ValueError("County layer has no declared coordinate reference system")
        to_source = Transformer.from_crs(4326, info["crs"], always_xy=True)
        source_bbox = to_source.transform_bounds(*bbox, densify_pts=21)
        meta, fids, geometries, columns = pyogrio.raw.read(
            source, layer=layer, bbox=source_bbox, return_fids=True, force_2d=True,
            datetime_as_string=True)
        to_wgs = Transformer.from_crs(CRS(meta["crs"]), 4326, always_xy=True).transform
        features = []
        for i, wkb in enumerate(geometries):
            if wkb is None:
                continue
            geom = transform(to_wgs, from_wkb(wkb))
            props = {str(field): scalar(values[i]) for field, values in zip(meta["fields"], columns)}
            features.append(dict(type="Feature", id=int(fids[i]), properties=props,
                                 geometry=mapping(geom)))
        return features, list(map(str, meta["fields"])), dict(self.provenance, archive_layer=layer)

    def close(self):
        if self.temp:
            self.temp.cleanup()
