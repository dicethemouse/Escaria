#!/usr/bin/env python3
"""Build the Mapbox GeoJSON package for Aulonia.

Inputs
------
* Azgaar PackCells JSON
* Cells GeoJSON
* Rivers GeoJSON
* Routes GeoJSON (geometry)
* Markers GeoJSON
* Burgs CSV (authoritative settlement longitude / latitude)
* Routes CSV (authoritative route names)

Outputs
-------
* Aulonia Admin Areas[ <date>].geojson
* Aulonia Lakes[ <date>].geojson
* Aulonia Hydro Lines[ <date>].geojson
* Aulonia Routes Vector[ <date>].geojson
* Aulonia Settlements Labels[ <date>].geojson
* Aulonia POIs[ <date>].geojson
* Aulonia Vector Layers Manifest[ <date>].json
* Aulonia Vector Layers[ <date>].zip (unless --no-zip)

The six GeoJSON files are deliberately rendering-oriented. Long-form lore,
market inventories and other page metadata belong in the Jekyll collections.
Every rendered feature receives a stable ``entity_id`` matching the collection
builder, so Mapbox clicks can resolve directly to a Jekyll entity.

Dependencies
------------
    pip install shapely

Shapely is used only for polygon dissolves and lake label axes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Polygon, mapping, shape
    from shapely.ops import unary_union
    try:
        from shapely import make_valid
    except ImportError:  # pragma: no cover - Shapely < 2 fallback
        make_valid = None
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "This script requires Shapely. Install it with: pip install shapely"
    ) from exc

GENERATOR_VERSION = "1.0.0"

# Azgaar canvas -> geographic coordinates used by the exported Aulonia GeoJSONs.
DEFAULT_MAP_WIDTH = 1204.0
DEFAULT_MAP_HEIGHT = 753.0
DEFAULT_LON_MIN = -28.8
DEFAULT_LON_MAX = 28.8
DEFAULT_LAT_MAX = 45.1
DEFAULT_LAT_MIN = 9.1


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def normalize_unicode(value: str) -> str:
    """Repair UTF-16 surrogate pairs occasionally present in Azgaar JSON."""
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def clean_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_unicode(value)
    if isinstance(value, dict):
        return {str(k): clean_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json_value(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(clean_json_value(obj), fh, ensure_ascii=False, indent=2, allow_nan=False)
        fh.write("\n")


def load_csv_index(path: Path, id_field: str = "Id") -> Dict[int, Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or id_field not in reader.fieldnames:
            raise ValueError(f"{path}: required CSV column {id_field!r} not found")
        out: Dict[int, Dict[str, str]] = {}
        for line_no, row in enumerate(reader, start=2):
            raw = (row.get(id_field) or "").strip()
            if not raw:
                continue
            try:
                ident = int(raw)
            except ValueError as exc:
                raise ValueError(f"{path}:{line_no}: invalid {id_field} {raw!r}") from exc
            if ident in out:
                raise ValueError(f"{path}:{line_no}: duplicate {id_field} {ident}")
            out[ident] = {str(k): (v or "") for k, v in row.items()}
        return out


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def inum(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_number(value: float, digits: int = 6) -> float:
    value = round(float(value), digits)
    return 0.0 if value == -0.0 else value


def xy_to_lonlat(
    x: Any,
    y: Any,
    *,
    width: float,
    height: float,
    lon_min: float = DEFAULT_LON_MIN,
    lon_max: float = DEFAULT_LON_MAX,
    lat_min: float = DEFAULT_LAT_MIN,
    lat_max: float = DEFAULT_LAT_MAX,
) -> Optional[List[float]]:
    if x is None or y is None:
        return None
    x = float(x)
    y = float(y)
    lon = lon_min + x * ((lon_max - lon_min) / width)
    lat = lat_max - y * ((lat_max - lat_min) / height)
    return [clean_number(lon), clean_number(lat)]


def csv_lonlat(row: Mapping[str, str], *, path: Path, ident: int) -> List[float]:
    try:
        lon = float((row.get("Longitude") or "").strip())
        lat = float((row.get("Latitude") or "").strip())
    except ValueError as exc:
        raise ValueError(f"{path}: invalid Longitude/Latitude for burg ID {ident}") from exc
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"{path}: out-of-range coordinate for burg ID {ident}: {lon}, {lat}")
    return [clean_number(lon), clean_number(lat)]


def feature_collection(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def geo_feature(geometry: Mapping[str, Any], properties: Mapping[str, Any]) -> Dict[str, Any]:
    # Remove only None values; false/0 are intentional styling values.
    props = {k: v for k, v in properties.items() if v is not None}
    return {"type": "Feature", "geometry": dict(geometry), "properties": props}


def parse_marker_id(value: Any) -> int:
    if isinstance(value, int):
        return value
    match = re.search(r"(\d+)$", str(value or ""))
    if not match:
        raise ValueError(f"Cannot parse marker id from {value!r}")
    return int(match.group(1))


def parse_km(value: str) -> float:
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", value or "")
    return float(match.group(0).replace(",", ".")) if match else 0.0


def safe_polygonal(geom):
    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        try:
            geom = make_valid(geom) if make_valid else geom.buffer(0)
        except Exception:
            geom = geom.buffer(0)
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polygons = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
        if polygons:
            merged = unary_union(polygons)
            return merged if not merged.is_empty else None
    return None


def dissolve(geometries: Sequence[Any]):
    valid = [g for g in geometries if g is not None and not g.is_empty]
    if not valid:
        return None
    return safe_polygonal(unary_union(valid))


def longest_linestring(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "LineString":
        return geom
    if geom.geom_type == "MultiLineString":
        return max(geom.geoms, key=lambda g: g.length, default=None)
    if geom.geom_type == "GeometryCollection":
        lines = []
        for g in geom.geoms:
            if g.geom_type == "LineString":
                lines.append(g)
            elif g.geom_type == "MultiLineString":
                lines.extend(g.geoms)
        return max(lines, key=lambda g: g.length, default=None)
    return None


def lake_label_axis(poly):
    """Create a line along the polygon's major axis, clipped inside the lake."""
    poly = safe_polygonal(poly)
    if poly is None:
        return None
    # Use largest island if geometry is multipart.
    work = max(poly.geoms, key=lambda g: g.area) if poly.geom_type == "MultiPolygon" else poly
    rect = work.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    edges = []
    for a, b in zip(coords, coords[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        edges.append((math.hypot(dx, dy), dx, dy))
    if not edges:
        return None
    _, dx, dy = max(edges, key=lambda e: e[0])
    norm = math.hypot(dx, dy)
    if norm == 0:
        return None
    ux, uy = dx / norm, dy / norm
    minx, miny, maxx, maxy = work.bounds
    diag = math.hypot(maxx - minx, maxy - miny) or 0.01
    c = work.centroid
    raw = LineString([
        (c.x - ux * diag * 2.0, c.y - uy * diag * 2.0),
        (c.x + ux * diag * 2.0, c.y + uy * diag * 2.0),
    ])
    inset = work.buffer(-max(diag * 0.025, 1e-7))
    target = inset if not inset.is_empty else work
    clipped = longest_linestring(raw.intersection(target))
    if clipped is None or clipped.length <= 0:
        clipped = longest_linestring(raw.intersection(work))
    return clipped


def assign_fraction_ranks(
    rows: Sequence[Any],
    score,
    cuts: Sequence[float],
    ranks: Sequence[int],
) -> Dict[Any, int]:
    """Assign ranks by descending score and cumulative fractional cut points."""
    ordered = sorted(rows, key=score, reverse=True)
    n = len(ordered)
    result: Dict[Any, int] = {}
    for idx, row in enumerate(ordered):
        frac = (idx + 1) / n if n else 1.0
        rank = ranks[-1]
        for cut, candidate in zip(cuts, ranks):
            if frac <= cut:
                rank = candidate
                break
        result[row] = rank
    return result


def assign_market_levels(active_burgs: List[Dict[str, Any]], markets: List[Dict[str, Any]]) -> Dict[int, str]:
    """Derive four map-display market tiers from market catchment population.

    PackCells stores market membership and centerBurgId but no textual tier.
    We therefore derive stable display tiers by catchment population:
    top 40% international, next 10% major, next 15% regional, rest local.
    With the current 34 Aulonia markets this yields 14/3/5/12 tiers.
    """
    totals: Dict[int, float] = defaultdict(float)
    for burg in active_burgs:
        market_id = inum(burg.get("market"))
        if market_id:
            totals[market_id] += fnum(burg.get("population"))
    ids = [inum(m.get("i")) for m in markets if isinstance(m, dict) and inum(m.get("i"))]
    ordered = sorted(ids, key=lambda mid: totals.get(mid, 0.0), reverse=True)
    n = len(ordered)
    out: Dict[int, str] = {}
    for idx, mid in enumerate(ordered):
        frac = (idx + 1) / n if n else 1.0
        if frac <= 0.40:
            level = "international"
        elif frac <= 0.50:
            level = "major"
        elif frac <= 0.65:
            level = "regional"
        else:
            level = "local"
        out[mid] = level
    return out


def build(args: argparse.Namespace) -> Tuple[Dict[str, int], List[Path]]:
    pack = load_json(args.pack)
    cells_geo = load_json(args.cells)
    rivers_geo = load_json(args.rivers)
    routes_geo = load_json(args.routes)
    markers_geo = load_json(args.markers)
    burg_csv = load_csv_index(args.burgs_csv)
    route_csv = load_csv_index(args.route_names_csv)

    pdata = pack.get("cells") or {}
    info = pack.get("info") or {}
    width = fnum(info.get("width"), DEFAULT_MAP_WIDTH)
    height = fnum(info.get("height"), DEFAULT_MAP_HEIGHT)

    cells = pdata.get("cells") or []
    states = [x for x in (pdata.get("states") or []) if isinstance(x, dict) and inum(x.get("i"))]
    provinces = [x for x in (pdata.get("provinces") or []) if isinstance(x, dict) and inum(x.get("i"))]
    burgs = [x for x in (pdata.get("burgs") or []) if isinstance(x, dict) and inum(x.get("i"))]
    lake_features = [x for x in (pdata.get("features") or []) if isinstance(x, dict) and x.get("type") == "lake"]
    pack_rivers = {inum(x.get("i")): x for x in (pdata.get("rivers") or []) if isinstance(x, dict)}
    pack_markers = {inum(x.get("i")): x for x in (pdata.get("markers") or []) if isinstance(x, dict)}
    markets = [x for x in (pdata.get("markets") or []) if isinstance(x, dict) and inum(x.get("i"))]

    state_by_id = {inum(x.get("i")): x for x in states}
    province_by_id = {inum(x.get("i")): x for x in provinces}
    cell_by_id = {inum(x.get("i")): x for x in cells if isinstance(x, dict)}

    active_states = [s for s in states if not s.get("removed")]
    active_provinces = [p for p in provinces if not p.get("removed")]
    active_burgs = [b for b in burgs if not b.get("removed")]

    # Validate authoritative CSV coverage.
    missing_active_burgs = [inum(b.get("i")) for b in active_burgs if inum(b.get("i")) not in burg_csv]
    if missing_active_burgs:
        raise ValueError(f"Burgs CSV misses active burg IDs: {missing_active_burgs[:20]}")
    route_geo_by_id = {inum(f.get("properties", {}).get("id")): f for f in routes_geo.get("features", [])}
    missing_route_names = sorted(set(route_geo_by_id) - set(route_csv))
    if missing_route_names:
        raise ValueError(f"Routes CSV misses route IDs: {missing_route_names[:20]}")

    # Load all cell polygons once, and group them for dissolves.
    cell_shapes: Dict[int, Any] = {}
    state_shapes: Dict[int, List[Any]] = defaultdict(list)
    province_shapes: Dict[int, List[Any]] = defaultdict(list)
    lake_shapes: Dict[int, List[Any]] = defaultdict(list)

    for feat in cells_geo.get("features", []):
        props = feat.get("properties") or {}
        cid = inum(props.get("id"))
        try:
            geom = safe_polygonal(shape(feat.get("geometry")))
        except Exception:
            geom = None
        if geom is None:
            continue
        cell_shapes[cid] = geom
        sid = inum(props.get("state"))
        pid = inum(props.get("province"))
        if sid:
            state_shapes[sid].append(geom)
        if pid:
            province_shapes[pid].append(geom)
        pcell = cell_by_id.get(cid) or {}
        feature_id = inum(pcell.get("f"))
        if feature_id:
            lake_shapes[feature_id].append(geom)

    # ------------------------------------------------------------------
    # 1. Admin areas
    # ------------------------------------------------------------------
    admin_features: List[Dict[str, Any]] = []
    for s in active_states:
        sid = inum(s.get("i"))
        geom = dissolve(state_shapes.get(sid, []))
        if geom is None:
            continue
        props = {
            "id": sid,
            "entity_id": f"state:{sid}",
            "feature_class": "state",
            "name": s.get("name"),
            "full_name": s.get("fullName") or s.get("name"),
            "form": s.get("form"),
            "form_name": s.get("formName"),
            "capital_id": inum(s.get("capital")) or None,
            "culture_id": inum(s.get("culture")) or None,
            "area": fnum(s.get("area")),
            "color": s.get("color"),
            "rank": 1,
            "minzoom": 0,
        }
        admin_features.append(geo_feature(mapping(geom), props))

    for p in active_provinces:
        pid = inum(p.get("i"))
        geom = dissolve(province_shapes.get(pid, []))
        if geom is None:
            continue
        sid = inum(p.get("state"))
        props = {
            "id": pid,
            "entity_id": f"province:{pid}",
            "feature_class": "province",
            "name": p.get("name"),
            "full_name": p.get("fullName") or p.get("name"),
            "form_name": p.get("formName"),
            "state_id": sid or None,
            "state_name": (state_by_id.get(sid) or {}).get("name"),
            "capital_id": inum(p.get("burg")) or None,
            "area": fnum(p.get("area")),
            "color": p.get("color"),
            "rank": 2,
            "minzoom": 4,
        }
        admin_features.append(geo_feature(mapping(geom), props))

    # ------------------------------------------------------------------
    # 2. Lakes and lake label axes
    # ------------------------------------------------------------------
    lake_by_id = {inum(x.get("i")): x for x in lake_features}
    lake_rank_map = assign_fraction_ranks(
        list(lake_by_id),
        score=lambda lid: fnum(lake_by_id[lid].get("area")),
        cuts=[0.05, 0.20, 0.50, 1.0],
        ranks=[1, 2, 3, 4],
    )
    lake_minzoom = {1: 2, 2: 4, 3: 6, 4: 8}
    lake_geo_features: List[Dict[str, Any]] = []
    lake_label_features: List[Dict[str, Any]] = []

    for lake in lake_features:
        lid = inum(lake.get("i"))
        geom = dissolve(lake_shapes.get(lid, []))
        if geom is None:
            continue
        rank = lake_rank_map[lid]
        props = {
            "id": lid,
            "entity_id": f"lake:{lid}",
            "feature_class": "lake",
            "name": lake.get("name"),
            "lake_type": lake.get("group") or "lake",
            "area": fnum(lake.get("area")),
            "height": fnum(lake.get("height")),
            "flux": fnum(lake.get("flux")),
            "temperature": fnum(lake.get("temp")),
            "outlet_id": inum(lake.get("outlet")) or None,
            "rank": rank,
            "minzoom": lake_minzoom[rank],
        }
        lake_geo_features.append(geo_feature(mapping(geom), props))
        axis = lake_label_axis(geom)
        if axis is not None:
            lake_label_features.append(geo_feature(mapping(axis), {
                "id": lid,
                "entity_id": f"lake:{lid}",
                "feature_class": "lake_label",
                "name": lake.get("name"),
                "lake_type": lake.get("group") or "lake",
                "area": fnum(lake.get("area")),
                "rank": rank,
                "minzoom": lake_minzoom[rank],
            }))

    # ------------------------------------------------------------------
    # 3. Rivers / hydro lines
    # ------------------------------------------------------------------
    river_features_src = rivers_geo.get("features", [])
    river_rank_map = assign_fraction_ranks(
        list(range(len(river_features_src))),
        score=lambda idx: fnum((river_features_src[idx].get("properties") or {}).get("discharge")),
        cuts=[0.02, 0.10, 0.30, 0.60, 1.0],
        ranks=[1, 2, 3, 4, 5],
    )
    river_minzoom = {1: 2, 2: 4, 3: 5, 4: 7, 5: 9}
    hydro_features: List[Dict[str, Any]] = []
    for idx, feat in enumerate(river_features_src):
        src = feat.get("properties") or {}
        rid = inum(src.get("id"))
        pack_river = pack_rivers.get(rid) or {}
        rank = river_rank_map[idx]
        props = {
            "id": rid,
            "entity_id": f"river:{rid}",
            "feature_class": "river",
            "name": src.get("name") or pack_river.get("name"),
            "river_type": src.get("type") or pack_river.get("type") or "River",
            "source_id": inum(src.get("source")) or None,
            "mouth_id": inum(src.get("mouth")) or None,
            "parent_id": inum(src.get("parent")) or None,
            "basin_id": inum(src.get("basin")) or None,
            "discharge": fnum(src.get("discharge")),
            "width_factor": fnum(src.get("widthFactor")),
            "source_width": fnum(src.get("sourceWidth")),
            "rank": rank,
            "minzoom": river_minzoom[rank],
        }
        hydro_features.append(geo_feature(feat.get("geometry"), props))
    hydro_features.extend(lake_label_features)

    # ------------------------------------------------------------------
    # 4. Routes: GeoJSON geometry + CSV names
    # ------------------------------------------------------------------
    group_to_type = {"roads": "road", "trails": "trail", "searoutes": "sea_route"}
    group_rank_minzoom = {
        "roads": ({1: 4, 2: 5, 3: 6}, [1, 2, 3]),
        "searoutes": ({1: 5, 2: 6, 3: 7}, [1, 2, 3]),
        "trails": ({2: 7, 3: 8, 4: 9}, [2, 3, 4]),
    }
    route_rows_by_group: Dict[str, List[int]] = defaultdict(list)
    route_length: Dict[int, float] = {}
    for rid, row in route_csv.items():
        group = (row.get("Group") or "").strip()
        route_rows_by_group[group].append(rid)
        route_length[rid] = parse_km(row.get("Length") or "")

    route_rank: Dict[int, int] = {}
    for group, ids in route_rows_by_group.items():
        _, ranks = group_rank_minzoom.get(group, ({1: 7, 2: 8, 3: 9}, [1, 2, 3]))
        route_rank.update(assign_fraction_ranks(
            ids,
            score=lambda rid: route_length.get(rid, 0.0),
            cuts=[0.10, 0.35, 1.0],
            ranks=ranks,
        ))

    route_features: List[Dict[str, Any]] = []
    for feat in routes_geo.get("features", []):
        src = feat.get("properties") or {}
        rid = inum(src.get("id"))
        csvrow = route_csv.get(rid)
        if csvrow is None:
            raise ValueError(f"Route {rid} has geometry but no CSV row")
        group = (csvrow.get("Group") or src.get("group") or "route").strip()
        if src.get("group") and str(src.get("group")) != group:
            raise ValueError(f"Route {rid}: group mismatch GeoJSON={src.get('group')!r}, CSV={group!r}")
        rtype = group_to_type.get(group, group.rstrip("s") or "route")
        name = (csvrow.get("Route") or "").strip() or None
        rank = route_rank.get(rid, 3)
        minzoom_map = group_rank_minzoom.get(group, ({1: 7, 2: 8, 3: 9}, [1, 2, 3]))[0]
        props = {
            "id": rid,
            "entity_id": f"route:{rid}",
            "feature_class": "route",
            "route_type": rtype,
            "route_group": group,
            "name": name,
            "named": bool(name),
            "length_km": route_length.get(rid, 0.0),
            "rank": rank,
            "minzoom": minzoom_map.get(rank, 8),
        }
        route_features.append(geo_feature(feat.get("geometry"), props))

    # ------------------------------------------------------------------
    # 5. Settlements and admin labels
    # ------------------------------------------------------------------
    market_levels = assign_market_levels(active_burgs, markets)
    market_center_by_market = {inum(m.get("i")): inum(m.get("centerBurgId")) for m in markets}

    # Rank settlements with administrative/market overrides, then population.
    preliminary_rank: Dict[int, int] = {}
    leftover: List[Dict[str, Any]] = []
    for b in active_burgs:
        bid = inum(b.get("i"))
        market_id = inum(b.get("market"))
        is_center = market_center_by_market.get(market_id) == bid
        level = market_levels.get(market_id, "local")
        if b.get("capital"):
            preliminary_rank[bid] = 1
        elif is_center and level in ("international", "major"):
            preliminary_rank[bid] = 2
        elif is_center and level == "regional":
            preliminary_rank[bid] = 3
        else:
            leftover.append(b)

    leftover_ranks = assign_fraction_ranks(
        [inum(b.get("i")) for b in leftover],
        score=lambda bid: fnum(next(b for b in leftover if inum(b.get("i")) == bid).get("population")),
        cuts=[0.05, 0.20, 0.50, 1.0],
        ranks=[3, 4, 5, 6],
    ) if leftover else {}
    preliminary_rank.update(leftover_ranks)
    settlement_minzoom = {1: 2, 2: 4, 3: 5, 4: 6, 5: 7, 6: 9}

    settlement_features: List[Dict[str, Any]] = []
    for b in active_burgs:
        bid = inum(b.get("i"))
        csvrow = burg_csv[bid]
        csv_name = (csvrow.get("Burg") or "").strip()
        pack_name = str(b.get("name") or "")
        if csv_name and pack_name and csv_name != pack_name:
            raise ValueError(f"Burg {bid}: name mismatch PackCells={pack_name!r}, CSV={csv_name!r}")
        coord = csv_lonlat(csvrow, path=args.burgs_csv, ident=bid)
        market_id = inum(b.get("market"))
        rank = preliminary_rank.get(bid, 6)
        props = {
            "id": bid,
            "entity_id": f"settlement:{bid}",
            "feature_class": "settlement",
            "name": pack_name or csv_name,
            "settlement_type": b.get("type") or b.get("group"),
            "state_id": inum(b.get("state")) or None,
            "province_id": inum((cell_by_id.get(inum(b.get("cell"))) or {}).get("province")) or None,
            "population": fnum(b.get("population")),
            "capital": bool(b.get("capital")),
            "port": bool(b.get("port")),
            "market_center": market_center_by_market.get(market_id) == bid,
            "market_level": market_levels.get(market_id, "local"),
            "market_id": market_id or None,
            "rank": rank,
            "minzoom": settlement_minzoom[rank],
        }
        settlement_features.append(geo_feature({"type": "Point", "coordinates": coord}, props))

    # State labels from Azgaar poles.
    for s in active_states:
        sid = inum(s.get("i"))
        coord = xy_to_lonlat(s.get("pole", [None, None])[0], s.get("pole", [None, None])[1], width=width, height=height)
        if coord is None:
            continue
        settlement_features.append(geo_feature({"type": "Point", "coordinates": coord}, {
            "id": sid,
            "entity_id": f"state:{sid}",
            "feature_class": "state_label",
            "name": s.get("name"),
            "full_name": s.get("fullName") or s.get("name"),
            "rank": 1,
            "minzoom": 2,
            "maxzoom": 7,
        }))

    # Province labels from Azgaar poles.
    for p in active_provinces:
        pid = inum(p.get("i"))
        pole = p.get("pole") or [None, None]
        coord = xy_to_lonlat(pole[0], pole[1], width=width, height=height)
        if coord is None:
            continue
        sid = inum(p.get("state"))
        settlement_features.append(geo_feature({"type": "Point", "coordinates": coord}, {
            "id": pid,
            "entity_id": f"province:{pid}",
            "feature_class": "province_label",
            "name": p.get("name"),
            "full_name": p.get("fullName") or p.get("name"),
            "state_id": sid or None,
            "state_name": (state_by_id.get(sid) or {}).get("name"),
            "rank": 2,
            "minzoom": 5,
            "maxzoom": 10,
        }))

    # ------------------------------------------------------------------
    # 6. POIs: markers + military
    # ------------------------------------------------------------------
    poi_features: List[Dict[str, Any]] = []
    for feat in markers_geo.get("features", []):
        src = feat.get("properties") or {}
        mid = parse_marker_id(src.get("id"))
        pmarker = pack_markers.get(mid) or {}
        cid = inum(pmarker.get("cell"))
        pcell = cell_by_id.get(cid) or {}
        poi_features.append(geo_feature(feat.get("geometry"), {
            "id": mid,
            "entity_id": f"poi:marker:{mid}",
            "feature_class": "marker",
            "poi_type": src.get("type") or pmarker.get("type"),
            "name": src.get("name") or f"Marker {mid}",
            "icon": src.get("icon") or pmarker.get("icon"),
            "description": src.get("legend"),
            "state_id": inum(pcell.get("state")) or None,
            "province_id": inum(pcell.get("province")) or None,
            "rank": 2,
            "minzoom": 6,
        }))

    for s in active_states:
        sid = inum(s.get("i"))
        for unit in s.get("military") or []:
            if not isinstance(unit, dict):
                continue
            uid = inum(unit.get("i"))
            coord = xy_to_lonlat(unit.get("x"), unit.get("y"), width=width, height=height)
            if coord is None:
                continue
            is_fleet = bool(unit.get("n")) or "fleet" in (unit.get("u") or {})
            strength = inum(unit.get("a"))
            poi_features.append(geo_feature({"type": "Point", "coordinates": coord}, {
                "id": f"{sid}:{uid}",
                "entity_id": f"poi:military:{sid}:{uid}",
                "feature_class": "military",
                "poi_type": "fleet" if is_fleet else "regiment",
                "name": unit.get("name") or (f"Fleet {uid}" if is_fleet else f"Regiment {uid}"),
                "state_id": sid,
                "strength": strength,
                "icon": unit.get("icon"),
                "rank": 3,
                "minzoom": 7,
            }))

    # ------------------------------------------------------------------
    # Write package
    # ------------------------------------------------------------------
    tag = f" {args.date_tag}" if args.date_tag else ""
    prefix = args.prefix.strip() or "Aulonia"
    outputs = {
        f"{prefix} Admin Areas{tag}.geojson": feature_collection(admin_features),
        f"{prefix} Lakes{tag}.geojson": feature_collection(lake_geo_features),
        f"{prefix} Hydro Lines{tag}.geojson": feature_collection(hydro_features),
        f"{prefix} Routes Vector{tag}.geojson": feature_collection(route_features),
        f"{prefix} Settlements Labels{tag}.geojson": feature_collection(settlement_features),
        f"{prefix} POIs{tag}.geojson": feature_collection(poi_features),
    }
    written: List[Path] = []
    for name, data in outputs.items():
        path = args.output / name
        write_json(path, data)
        written.append(path)

    counts = {
        "states": len(active_states),
        "provinces": len(active_provinces),
        "lakes": len(lake_geo_features),
        "rivers": len(river_features_src),
        "lake_labels": len(lake_label_features),
        "routes": len(route_features),
        "settlements": len(active_burgs),
        "state_labels": len(active_states),
        "province_labels": len(active_provinces),
        "markers": sum(1 for f in poi_features if f["properties"].get("feature_class") == "marker"),
        "military": sum(1 for f in poi_features if f["properties"].get("feature_class") == "military"),
    }
    manifest = {
        "name": f"{prefix} Mapbox vector package",
        "generator": "build_aulonia_mapbox_geojson.py",
        "generator_version": GENERATOR_VERSION,
        "source_map": info.get("mapName") or prefix,
        "source_exported_at": info.get("exportedAt"),
        "counts": counts,
        "excluded_removed": {
            "settlements": len(burgs) - len(active_burgs),
            "provinces": len(provinces) - len(active_provinces),
        },
        "authoritative_sources": {
            "settlement_coordinates": args.burgs_csv.name,
            "route_names": args.route_names_csv.name,
            "route_geometry": args.routes.name,
            "river_geometry": args.rivers.name,
            "marker_geometry": args.markers.name,
        },
        "files": [p.name for p in written],
    }
    manifest_path = args.output / f"{prefix} Vector Layers Manifest{tag}.json"
    write_json(manifest_path, manifest)
    written.append(manifest_path)

    if not args.no_zip:
        zip_path = args.output / f"{prefix} Vector Layers{tag}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in written:
                zf.write(path, arcname=path.name)
        written.append(zip_path)

    return counts, written


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the six Mapbox-ready Aulonia GeoJSON layers from Azgaar exports."
    )
    parser.add_argument("--pack", required=True, type=Path, help="Aulonia PackCells JSON")
    parser.add_argument("--cells", required=True, type=Path, help="Aulonia Cells GeoJSON")
    parser.add_argument("--rivers", required=True, type=Path, help="Aulonia Rivers GeoJSON")
    parser.add_argument("--routes", required=True, type=Path, help="Aulonia Routes GeoJSON (geometry)")
    parser.add_argument("--markers", required=True, type=Path, help="Aulonia Markers GeoJSON")
    parser.add_argument("--burgs-csv", required=True, type=Path, help="Burgs CSV; authoritative settlement coordinates")
    parser.add_argument("--route-names-csv", required=True, type=Path, help="Routes CSV; authoritative route names")
    parser.add_argument("--output", type=Path, default=Path("."), help="Output directory (default: current directory)")
    parser.add_argument("--prefix", default="Aulonia", help="Output filename prefix (default: Aulonia)")
    parser.add_argument(
        "--date-tag",
        default="",
        help="Optional filename date/version tag, e.g. 2026-08-13. Empty = stable filenames.",
    )
    parser.add_argument("--no-zip", action="store_true", help="Do not create the convenience ZIP package")
    args = parser.parse_args(argv)
    for attr in ("pack", "cells", "rivers", "routes", "markers", "burgs_csv", "route_names_csv"):
        path = getattr(args, attr)
        if not path.is_file():
            parser.error(f"{attr}: file not found: {path}")
    args.output.mkdir(parents=True, exist_ok=True)
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = parse_args(argv)
        counts, written = build(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Mapbox package built successfully.")
    for key, value in counts.items():
        print(f"  {key:16s} {value:5d}")
    print("\nWritten files:")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
