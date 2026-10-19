"""Fixed acquisition scope. All distances are ground distances, not Web Mercator."""
from pathlib import Path
from pyproj import Geod, Transformer
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LIMIT = 90_000
VERTICES = [
    (-83.59547573379061, 30.10202652463319),
    (-83.58741837790987, 30.10216575269676),
    (-83.58749839138977, 30.09916296420653),
    (-83.58827086758606, 30.099172246352474),
    (-83.58826013875, 30.098652444838205),
    (-83.58751984906189, 30.09866172703209),
    (-83.58757455775374, 30.08789746326437),
    (-83.59204848239058, 30.08787889685443),
    (-83.59178230268613, 30.09140518187869),
    (-83.5962991426672, 30.091358767493862),
    (-83.59602880017819, 30.09501632051197),
    (-83.59584640996518, 30.09500703797581),
    (-83.59575401884945, 30.10086613884424),
    (-83.59508883101375, 30.10086613884424),
    (-83.59511028868587, 30.101720077905693),
    (-83.59550725562008, 30.10170151409152),
]
SITE = Polygon(VERTICES)
GEOD = Geod(ellps="WGS84")
WEB = Transformer.from_crs(4326, 3857, always_xy=True)
# Local equidistant projection gives genuine 50 m simplification tolerances.
LOCAL_CRS = "+proj=aeqd +lat_0=30.095 +lon_0=-83.592 +datum=WGS84 +units=m"
TO_LOCAL = Transformer.from_crs(4326, LOCAL_CRS, always_xy=True).transform
FROM_LOCAL = Transformer.from_crs(LOCAL_CRS, 4326, always_xy=True).transform


def padded(miles):
    w, s, e, n = SITE.bounds
    m = miles * 1609.344
    # Use the most expansive cardinal offset at either bounding-box edge.
    return [
        min(GEOD.fwd(w, y, 270, m)[0] for y in (s, n)),
        GEOD.fwd((w + e) / 2, s, 180, m)[1],
        max(GEOD.fwd(e, y, 90, m)[0] for y in (s, n)),
        GEOD.fwd((w + e) / 2, n, 0, m)[1],
    ]


EXTENTS = {k: padded(v) for k, v in
           (("SITE", .5), ("CONTEXT", 3), ("VICINITY", 60))}
# Inset intentionally covers the full state, not the vicinity of the site.
EXTENTS["FLORIDA"] = [-88.0, 24.0, -79.5, 31.1]
FEMA = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
NWI = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer"
NHD = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"
TIGER = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
NAIP = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer"
IMAGERY = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer"
TOPO = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer"
