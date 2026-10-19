"""Offline regression checks; no external requests and no production outputs."""
import json
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np
import pyogrio.raw
from shapely.geometry import box, mapping
from shapely import to_wkb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import output
from output import Output
from arcgis import ArcGIS
from nfhl_transport import TLS12Adapter, client
from nfhl_fallback import NFHLFallback, FEMA, ALTERNATE, REDUCED, FULL, LAYERS
from nfhl_county import CountyProduct


def feature(properties=None):
    return dict(type="Feature", id=7, properties=properties or {"FLD_ZONE": "AE"},
                geometry=mapping(box(-83.595, 30.09, -83.59, 30.10)))


class NFHLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(output, "DATA", self.root),
                        patch.object(config, "DATA", self.root)]
        for p in self.patches:
            p.start()
        self.out = Output()
        self.client = Mock()
        self.recovery = NFHLFallback(self.client, self.out)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def query_data(self, properties=None):
        f = feature(properties)
        return [f], list(f["properties"]), dict(source_url="test-source/query",
                                               http_status=200, request_parameters={"outFields": "*"})

    def test_tls12_retains_certificate_checks(self):
        adapter = TLS12Adapter()
        self.assertEqual(adapter.context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(adapter.context.maximum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(adapter.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(adapter.context.check_hostname)
        self.assertGreater(len(adapter.context.get_ciphers()), 0)
        transport = client(self.out)
        self.assertEqual(transport.timeout, 60)
        self.assertIsInstance(transport.session.get_adapter(FEMA), TLS12Adapter)

    def test_first_source_success_stops_and_missing_columns_are_null(self):
        with patch.object(self.recovery, "query", return_value=self.query_data()) as query:
            result = self.recovery.recover("nfhl_flood_zones")
        self.assertEqual(query.call_count, 1)
        self.assertEqual(result["FLD_ZONE"], ["AE"])
        self.assertIn("STATIC_BFE", result["missing_fields"])
        props = json.loads((self.root / result["files"][0]["name"]).read_text())["features"][0]["properties"]
        self.assertIsNone(props["STATIC_BFE"])
        self.assertEqual(set(props), set(LAYERS["nfhl_flood_zones"]["fields"]))

    def test_alternate_path_precedes_mirrors(self):
        def query(url, dataset):
            if url.startswith(FEMA):
                raise ConnectionError("test primary failure")
            self.assertTrue(url.startswith(ALTERNATE))
            return self.query_data()
        with patch.object(self.recovery, "query", side_effect=query) as q:
            result = self.recovery.recover("nfhl_flood_zones")
        self.assertEqual(q.call_count, 2)
        self.assertEqual(result["attempts"][0]["status"], "failed")
        self.assertEqual(result["attempts"][1]["status"], "success")

    def test_reduced_mirror_is_queried_only_for_flood_areas(self):
        def query(url, dataset):
            if url == REDUCED:
                return self.query_data()
            raise ConnectionError("FEMA unavailable")
        with patch.object(self.recovery, "query", side_effect=query) as q:
            result = self.recovery.recover("nfhl_flood_zones")
        self.assertEqual(q.call_count, 3)
        self.assertEqual(result["selected_source"], "2 Living Atlas reduced")

    def test_all_stages_exhausted_preserves_existing_dataset_files(self):
        previous = self.root / "nfhl_bfe.geojson"
        previous.write_text('{"existing":"real prior data"}')
        with patch.object(self.recovery, "query", side_effect=ConnectionError("offline")) as q, \
             patch.object(self.recovery, "service_layers", return_value=[]), \
             patch.object(self.recovery, "authorized_state_items", return_value=[]), \
             patch.object(self.recovery.county, "read", side_effect=ValueError("no county data")):
            result = self.recovery.recover("nfhl_bfe")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["all_fallback_stages_exhausted"])
        self.assertEqual(previous.read_text(), '{"existing":"real prior data"}')
        self.assertNotIn(REDUCED, [c.args[0] for c in q.call_args_list])

    def test_panel_summary_dates_and_case_insensitive_fields(self):
        props = {"firm_pan": "12123C0350D", "eff_date": 1475020800000, "panel_typ": "county"}
        with patch.object(self.recovery, "query", return_value=self.query_data(props)):
            result = self.recovery.recover("nfhl_firm_panels")
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["firm_panels"][0]["FIRM_PAN"], "12123C0350D")
        self.assertTrue(result["firm_panels"][0]["effective_date_utc"].startswith("2016-09-28"))

    def test_verified_zero_features_are_saved_only_after_all_fallbacks(self):
        def query(url, dataset):
            if url.startswith(FEMA):
                return [], ["LOMR_ID"], dict(source_url=url + "/query", http_status=200)
            raise ConnectionError("offline")
        with patch.object(self.recovery, "query", side_effect=query), \
             patch.object(self.recovery, "service_layers", return_value=[]), \
             patch.object(self.recovery, "authorized_state_items", return_value=[]), \
             patch.object(self.recovery.county, "read", side_effect=ValueError("unavailable")) as county:
            result = self.recovery.recover("nfhl_lomr")
        county.assert_called_once()
        self.assertTrue(result["confirmed_empty"])
        self.assertTrue(result["all_fallback_stages_exhausted"])
        self.assertEqual(result["feature_count"], 0)
        data = json.loads((self.root / "nfhl_lomr.geojson").read_text())
        self.assertEqual(data, {"type": "FeatureCollection", "features": []})

    def test_append_log_keeps_original_prefix_and_text_caps(self):
        self.out.log("a" * 100000)
        original = self.out.lines
        appended = Output(append=True)
        appended.log("new acquisition")
        self.assertTrue(appended.lines.startswith(original))
        self.assertTrue(all(p.stat().st_size <= 90000 for p in self.root.glob("*.txt")))

    def test_wildcard_and_pagination(self):
        transport = ArcGIS(self.out)
        meta = dict(fields=[{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                            {"name": "FLD_ZONE", "type": "esriFieldTypeString"}],
                    maxRecordCount=1, advancedQueryCapabilities={"supportsPagination": True})
        pages = [
            (dict(type="FeatureCollection", features=[feature()], exceededTransferLimit=True),
             {"http_status": 200}),
            (dict(type="FeatureCollection", features=[]), {"http_status": 200}),
        ]
        with patch.object(transport, "metadata", return_value=meta), \
             patch.object(transport, "request", side_effect=pages) as request:
            features, params, _ = transport.features("https://test/0", config.EXTENTS["CONTEXT"], ["*"])
        self.assertEqual(len(features), 1)
        self.assertEqual(params["outFields"], "*")
        self.assertEqual(request.call_args_list[1].args[1]["resultOffset"], 1)

    def test_county_shapefile_read_and_reprojection(self):
        shp = self.root / "S_FLD_HAZ_AR.shp"
        pyogrio.raw.write(
            shp, geometry=np.array([to_wkb(box(-83.595, 30.09, -83.59, 30.10))], dtype=object),
            field_data=[np.array(["AE"], dtype=object)], fields=["FLD_ZONE"],
            driver="ESRI Shapefile", geometry_type="Polygon", crs="EPSG:4326")
        county = CountyProduct(self.client)
        county.loaded = True
        county.layers = [(shp, "S_FLD_HAZ_AR", "Polygon")]
        county.provenance = {"product_name": "test fixture"}
        features, fields, provenance = county.read("S_FLD_HAZ_AR", config.EXTENTS["CONTEXT"])
        self.assertEqual(len(features), 1)
        self.assertEqual(fields, ["FLD_ZONE"])
        self.assertEqual(features[0]["properties"]["FLD_ZONE"], "AE")
        self.assertEqual(provenance["archive_layer"], "S_FLD_HAZ_AR")


if __name__ == "__main__":
    unittest.main()
