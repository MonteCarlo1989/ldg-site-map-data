"""Text-safe serialization. No geometry simplification is done in this module."""
import base64
import json
import threading
from shapely.geometry import box, mapping, shape
from shapely.ops import transform
from shapely import make_valid
from config import DATA, LIMIT, TO_LOCAL, FROM_LOCAL
from arcgis import now


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def rounded(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (tuple, list)):
        return [rounded(x) for x in value]
    if isinstance(value, dict):
        return {k: rounded(v) for k, v in value.items()}
    return value


def collection(features):
    return {"type": "FeatureCollection", "features": features}


def normalized(feature, clip, simplify=False):
    if not feature.get("geometry"):
        raise ValueError("Source feature has no geometry")
    geom = shape(feature["geometry"])
    if not geom.is_valid:
        geom = make_valid(geom)
    if simplify:
        geom = transform(FROM_LOCAL, transform(TO_LOCAL, geom).simplify(50, preserve_topology=True))
    geom = geom.intersection(clip)
    if geom.is_empty:
        return None
    result = dict(feature)
    result["geometry"] = rounded(mapping(geom))
    return result


def fragments(feature, depth=0):
    """Spatially subdivide a feature that alone cannot fit. Never drop vertices.

    Output features retain their source properties and source ID, so a consumer
    can union fragments. This is necessary even for a one-feature state outline.
    """
    if len(encode(collection([feature]))) < LIMIT:
        return [feature]
    if depth > 24:
        raise ValueError("Cannot fit a single feature under the text limit")
    geom = shape(feature["geometry"])
    w, s, e, n = geom.bounds
    if e - w >= n - s:
        mid = (w + e) / 2
        cells = [box(w - 1e-8, s - 1e-8, mid, n + 1e-8),
                 box(mid, s - 1e-8, e + 1e-8, n + 1e-8)]
    else:
        mid = (s + n) / 2
        cells = [box(w - 1e-8, s - 1e-8, e + 1e-8, mid),
                 box(w - 1e-8, mid, e + 1e-8, n + 1e-8)]
    output = []
    for cell in cells:
        part = geom.intersection(cell)
        if part.is_empty:
            continue
        f = dict(feature, geometry=rounded(mapping(part)))
        f["properties"] = dict(feature.get("properties") or {},
                               _ldg_fragment=True,
                               _ldg_source_id=feature.get("id", feature.get("properties", {}).get("OBJECTID")))
        output.extend(fragments(f, depth + 1))
    return output


class Output:
    def __init__(self, append=False):
        DATA.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        log_files = [DATA / "log.txt"] + sorted(DATA.glob("log.part*.txt"))
        self.lines = "".join(p.read_text() for p in log_files if p.exists()) if append else ""

    def write(self, name, content):
        if isinstance(content, str):
            content = content.encode()
        temp = DATA / (name + ".tmp")
        temp.write_bytes(content)
        temp.replace(DATA / name)
        return {"name": name, "size": len(content)}

    def json_parts(self, stem, items, key):
        groups, group = [], []
        for item in items:
            if len(encode({key: group + [item]})) >= LIMIT:
                if not group:
                    raise ValueError(f"One {key} record exceeds {LIMIT} bytes")
                groups.append(group)
                group = []
            group.append(item)
        if group or not groups:
            groups.append(group)
        return [self.write(f"{stem}.part{i:02d}.json", encode({key: g}))
                for i, g in enumerate(groups, 1)]

    def log(self, message):
        with self.lock:
            self.lines += f"{now()} {message}\n"
            raw = self.lines.encode()
            pos, i = 0, 1
            while pos < len(raw):
                end = min(pos + LIMIT, len(raw))
                while end < len(raw) and raw[end] & 0xC0 == 0x80:
                    end -= 1
                name = "log.txt" if i == 1 else f"log.part{i:04d}.txt"
                self.write(name, raw[pos:end])
                pos, i = end, i + 1

    def vectors(self, stem, features, bbox, simplify=False):
        clip = box(*bbox)
        output, oversized = [], 0
        for feature in features:
            f = normalized(feature, clip, simplify)
            if f:
                parts = fragments(f)
                oversized += int(len(parts) > 1)
                output.extend(parts)
        groups, group, size = [], [], len(encode(collection([])))
        for feature in output:
            b = len(encode(feature)) + (1 if group else 0)
            if size + b >= LIMIT:
                groups.append(group)
                group, size = [], len(encode(collection([])))
                b = len(encode(feature))
            group.append(feature)
            size += b
        if group or not groups:
            groups.append(group)
        files = []
        for i, group in enumerate(groups, 1):
            name = f"{stem}.geojson" if len(groups) == 1 else f"{stem}.part{i:02d}.geojson"
            files.append(dict(self.write(name, encode(collection(group))), feature_count=len(group)))
        return files, len(output), oversized

    def raster(self, stem, jpeg, info):
        files = [self.write(stem + ".jpg", jpeg)]
        raw = base64.b64encode(jpeg)
        parts = [raw[i:i + LIMIT] for i in range(0, len(raw), LIMIT)]
        files.extend(self.write(f"{stem}.part{i:03d}.b64", part) for i, part in enumerate(parts, 1))
        sidecar = dict(info, part_count=len(parts), total_bytes=len(jpeg),
                       base64_bytes=len(raw), encoding="base64",
                       parts=[f["name"] for f in files if f["name"].endswith(".b64")],
                       reconstruction="Concatenate base64 parts in listed order, then decode.")
        files.append(self.write(stem + ".json", encode(sidecar)))
        return files
