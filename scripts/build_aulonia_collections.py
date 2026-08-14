#!/usr/bin/env python3
"""Build seven Jekyll collections from Azgaar Aulonia exports.

Settlement coordinates are sourced from the Burgs CSV (Latitude/Longitude),
and route names are sourced from the Routes CSV.

Collections created under OUTPUT_ROOT:
  _states, _provinces, _settlements, _rivers, _lakes, _routes, _pois

The script intentionally does NOT create a search index. The Jekyll collections
are meant to be the canonical source from which a later Liquid-generated search
index can be built.

Generated fields live between AULONIA AUTO markers inside YAML front matter.
On re-run, only that generated block is replaced; manual front-matter fields and
Markdown content outside the block are preserved.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Tuple

GENERATOR_VERSION = "1.1.0"
AUTO_BEGIN = "# BEGIN AULONIA AUTO-GENERATED DATA"
AUTO_END = "# END AULONIA AUTO-GENERATED DATA"

COLLECTIONS = {
    "state": "_states",
    "province": "_provinces",
    "settlement": "_settlements",
    "river": "_rivers",
    "lake": "_lakes",
    "route": "_routes",
    "poi": "_pois",
}

SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
CAMEL_1_RE = re.compile(r"(.)([A-Z][a-z]+)")
CAMEL_2_RE = re.compile(r"([a-z0-9])([A-Z])")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_csv_by_id(path: Path) -> Dict[int, Dict[str, str]]:
    """Load an Azgaar CSV export keyed by its integer Id column."""
    rows: Dict[int, Dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "Id" not in reader.fieldnames:
            raise ValueError(f"CSV has no 'Id' column: {path}")
        for row in reader:
            raw_id = (row.get("Id") or "").strip()
            if not raw_id:
                continue
            try:
                ident = int(raw_id)
            except ValueError as exc:
                raise ValueError(f"Invalid Id {raw_id!r} in {path}") from exc
            rows[ident] = {str(k): (v if v is not None else "") for k, v in row.items()}
    return rows


def csv_float(row: Optional[Mapping[str, str]], key: str) -> Optional[float]:
    if not row:
        return None
    raw = (row.get(key) or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def is_dict_record(value: Any) -> bool:
    return isinstance(value, dict)


def snake_case(name: str) -> str:
    name = name.replace("-", "_").replace(" ", "_")
    name = CAMEL_1_RE.sub(r"\1_\2", name)
    name = CAMEL_2_RE.sub(r"\1_\2", name)
    return name.lower()


def deep_snake(value: Any) -> Any:
    if isinstance(value, dict):
        return {snake_case(str(k)): deep_snake(v) for k, v in value.items()}
    if isinstance(value, list):
        return [deep_snake(v) for v in value]
    return value


def normalize_unicode(value: str) -> str:
    """Repair UTF-16 surrogate pairs occasionally present in Azgaar JSON text."""
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def finite_number(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def clean_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_unicode(value)
    if isinstance(value, dict):
        return {str(k): clean_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_json_value(v) for v in value]
    return finite_number(value)


def yaml_scalar(value: Any) -> str:
    value = finite_number(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return json.dumps(normalize_unicode(str(value)), ensure_ascii=False)


def yaml_key(key: Any) -> str:
    key = str(key)
    if SAFE_KEY_RE.match(key):
        return key
    return json.dumps(key, ensure_ascii=False)


def yaml_lines(value: Any, indent: int = 0) -> List[str]:
    """Small dependency-free YAML emitter for JSON-compatible values.

    Strings are always JSON-quoted. This produces conservative YAML that
    Jekyll/Psych can parse while keeping nested structures readable.
    """
    value = clean_json_value(value)
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [pad + "{}"]
        lines: List[str] = []
        for k, v in value.items():
            key = yaml_key(k)
            if isinstance(v, dict):
                if v:
                    lines.append(f"{pad}{key}:")
                    lines.extend(yaml_lines(v, indent + 2))
                else:
                    lines.append(f"{pad}{key}: {{}}")
            elif isinstance(v, list):
                if v:
                    lines.append(f"{pad}{key}:")
                    lines.extend(yaml_lines(v, indent + 2))
                else:
                    lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {yaml_scalar(v)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [pad + "[]"]
        lines = []
        for item in value:
            if isinstance(item, dict):
                if not item:
                    lines.append(pad + "- {}")
                else:
                    lines.append(pad + "-")
                    lines.extend(yaml_lines(item, indent + 2))
            elif isinstance(item, list):
                if not item:
                    lines.append(pad + "- []")
                else:
                    lines.append(pad + "-")
                    lines.extend(yaml_lines(item, indent + 2))
            else:
                lines.append(pad + "- " + yaml_scalar(item))
        return lines
    return [pad + yaml_scalar(value)]


def dump_yaml_mapping(mapping: Mapping[str, Any]) -> str:
    return "\n".join(yaml_lines(dict(mapping)))


def iter_geometry_coords(coords: Any) -> Iterator[Tuple[float, float]]:
    if (
        isinstance(coords, (list, tuple))
        and len(coords) >= 2
        and isinstance(coords[0], (int, float))
        and isinstance(coords[1], (int, float))
    ):
        yield float(coords[0]), float(coords[1])
        return
    if isinstance(coords, (list, tuple)):
        for child in coords:
            yield from iter_geometry_coords(child)


def geometry_bbox(geometry: Mapping[str, Any]) -> Optional[List[float]]:
    pts = list(iter_geometry_coords(geometry.get("coordinates")))
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [round(min(xs), 6), round(min(ys), 6), round(max(xs), 6), round(max(ys), 6)]


def bbox_union(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> Optional[List[float]]:
    if a is None:
        return list(b) if b is not None else None
    if b is None:
        return list(a)
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def bbox_center(bbox: Optional[Sequence[float]]) -> Optional[List[float]]:
    if not bbox:
        return None
    return [round((bbox[0] + bbox[2]) / 2, 6), round((bbox[1] + bbox[3]) / 2, 6)]


def representative_coord(geometry: Mapping[str, Any]) -> Optional[List[float]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Point" and isinstance(coords, list) and len(coords) >= 2:
        return [round(float(coords[0]), 6), round(float(coords[1]), 6)]
    if gtype == "LineString" and isinstance(coords, list) and coords:
        p = coords[len(coords) // 2]
        if isinstance(p, list) and len(p) >= 2:
            return [round(float(p[0]), 6), round(float(p[1]), 6)]
    return bbox_center(geometry_bbox(geometry))


def point_bbox(coord: Optional[Sequence[float]]) -> Optional[List[float]]:
    if not coord:
        return None
    return [coord[0], coord[1], coord[0], coord[1]]


def fit_affine_xy_to_lonlat(markers_geojson: Mapping[str, Any]) -> Tuple[float, float, float, float]:
    """Fit lon=a+b*x and lat=c+d*y from marker GeoJSON x/y properties.

    Azgaar's marker export includes both original map coordinates and GeoJSON
    coordinates, making this transform self-describing and avoiding hard-coded
    Aulonia bounds.
    """
    samples = []
    for feature in markers_geojson.get("features", []):
        props = feature.get("properties") or {}
        geom = feature.get("geometry") or {}
        xy = (props.get("x"), props.get("y"))
        ll = geom.get("coordinates")
        if (
            isinstance(xy[0], (int, float))
            and isinstance(xy[1], (int, float))
            and geom.get("type") == "Point"
            and isinstance(ll, list)
            and len(ll) >= 2
        ):
            samples.append((float(xy[0]), float(xy[1]), float(ll[0]), float(ll[1])))
    if len(samples) < 2:
        raise ValueError("Need at least two marker points with x/y and GeoJSON coordinates to infer coordinate transform")

    def regression(xs: List[float], ys: List[float]) -> Tuple[float, float]:
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom == 0:
            raise ValueError("Cannot infer affine coordinate transform from degenerate marker samples")
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
        intercept = my - slope * mx
        return intercept, slope

    lon_a, lon_b = regression([s[0] for s in samples], [s[2] for s in samples])
    lat_a, lat_b = regression([s[1] for s in samples], [s[3] for s in samples])
    return lon_a, lon_b, lat_a, lat_b


def make_xy_transform(params: Tuple[float, float, float, float]):
    lon_a, lon_b, lat_a, lat_b = params

    def transform(x: Any, y: Any) -> Optional[List[float]]:
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None
        return [round(lon_a + lon_b * float(x), 6), round(lat_a + lat_b * float(y), 6)]

    return transform


def index_dict_records(records: Iterable[Any], key: str = "i") -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for record in records:
        if isinstance(record, dict) and isinstance(record.get(key), int):
            out[int(record[key])] = record
    return out


def names_for_ids(ids: Iterable[int], index: Mapping[int, Mapping[str, Any]]) -> List[str]:
    names = []
    for ident in ids:
        obj = index.get(int(ident))
        if obj and obj.get("name"):
            names.append(str(obj["name"]))
    return names


def nonzero_sorted(values: Iterable[Any]) -> List[int]:
    out = set()
    for v in values:
        if isinstance(v, int) and v != 0:
            out.add(v)
    return sorted(out)


def status_for(record: Mapping[str, Any]) -> str:
    return "removed" if bool(record.get("removed")) else "active"


def slugish_type(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or fallback


def existing_manual_parts(path: Path) -> Tuple[str, str]:
    """Return (manual_front_matter, body) from an existing generated file."""
    if not path.exists():
        return "", ""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text.rstrip() + "\n"
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return "", text.rstrip() + "\n"
    front = lines[1:end]
    body = "\n".join(lines[end + 1 :]).lstrip("\n")

    manual: List[str] = []
    in_auto = False
    for line in front:
        if line.strip() == AUTO_BEGIN:
            in_auto = True
            continue
        if line.strip() == AUTO_END:
            in_auto = False
            continue
        if not in_auto:
            manual.append(line)
    # remove leading/trailing blank lines to avoid growth on each run
    while manual and not manual[0].strip():
        manual.pop(0)
    while manual and not manual[-1].strip():
        manual.pop()
    return "\n".join(manual), body.rstrip() + ("\n" if body.strip() else "")


def default_manual_frontmatter() -> str:
    return "aliases: []\nsearch_terms: []\nsummary: \"\""


def default_body(category: str) -> str:
    headings = {
        "state": ["Beschreibung", "Regierung und Gesellschaft", "Geschichte"],
        "province": ["Beschreibung", "Verwaltung", "Geschichte"],
        "settlement": ["Beschreibung", "Geschichte", "Besonderheiten"],
        "river": ["Beschreibung", "Verlauf", "Geschichte"],
        "lake": ["Beschreibung", "Geographie", "Geschichte"],
        "route": ["Beschreibung", "Verlauf", "Geschichte"],
        "poi": ["Beschreibung", "Geschichte"],
    }
    return "\n\n".join(f"## {h}\n\n" for h in headings[category]).rstrip() + "\n"


def write_document(path: Path, generated: Mapping[str, Any], category: str, dry_run: bool = False) -> None:
    manual, body = existing_manual_parts(path)
    if not manual:
        manual = default_manual_frontmatter()
    if not body.strip():
        body = default_body(category)
    auto_yaml = dump_yaml_mapping(generated)
    text = (
        "---\n"
        f"{AUTO_BEGIN}\n"
        f"{auto_yaml}\n"
        f"{AUTO_END}\n"
        f"{manual.rstrip()}\n"
        "---\n\n"
        f"{body.lstrip()}"
    )
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def common_doc(
    *,
    title: str,
    entity_id: str,
    source_id: Any,
    category: str,
    subtype: str,
    status: str,
    coordinates: Optional[Sequence[float]],
    bbox: Optional[Sequence[float]],
    map_zoom: float,
) -> Dict[str, Any]:
    return {
        "title": title,
        "entity_id": entity_id,
        "source_id": source_id,
        "category": category,
        "subtype": subtype,
        "status": status,
        "searchable": status == "active",
        "coordinates": list(coordinates) if coordinates else None,
        "bbox": list(bbox) if bbox else None,
        "map_zoom": map_zoom,
        "generated_by": f"build_aulonia_collections.py {GENERATOR_VERSION}",
    }


def merge_extras(doc: MutableMapping[str, Any], raw: Mapping[str, Any], exclude: Iterable[str]) -> None:
    excluded = set(exclude)
    extras = {k: v for k, v in raw.items() if k not in excluded}
    for key, value in deep_snake(extras).items():
        if key not in doc:
            doc[key] = value


def market_payload(
    market_id: Any,
    markets: Mapping[int, Mapping[str, Any]],
    goods: Mapping[int, Mapping[str, Any]],
    burgs: Mapping[int, Mapping[str, Any]],
    settlement_id: int,
) -> Optional[Dict[str, Any]]:
    if not isinstance(market_id, int) or market_id == 0:
        return None
    market = markets.get(market_id)
    if not market:
        return {"id": market_id}
    center_id = market.get("centerBurgId")
    center = burgs.get(center_id) if isinstance(center_id, int) else None
    result: Dict[str, Any] = {
        "id": market_id,
        "center": center_id == settlement_id,
        "center_settlement_id": center_id,
        "center_settlement_name": center.get("name") if center else None,
        "color": market.get("color"),
        "goods": [],
    }
    price_fields = {
        "value_gp", "price_gp", "base_price_gp", "regional_multiplier", "currency",
        "legacy_value", "legacy_price", "price_reference"
    }
    for good_key, quote in (market.get("goods") or {}).items():
        try:
            good_id = int(good_key)
        except (TypeError, ValueError):
            continue
        catalog = goods.get(good_id, {})
        item: Dict[str, Any] = {
            "id": good_id,
            "name": catalog.get("name"),
            "unit": catalog.get("unit"),
            "tags": catalog.get("tags", []),
            "icon": catalog.get("icon"),
            "color": catalog.get("color"),
            "value": catalog.get("value"),
            "stock": quote.get("stock") if isinstance(quote, dict) else None,
            "price": quote.get("price") if isinstance(quote, dict) else None,
        }
        for field in price_fields:
            if field in catalog:
                item[field] = catalog[field]
            if isinstance(quote, dict) and field in quote:
                item[field] = quote[field]
        result["goods"].append(item)
    return result


def build(args: argparse.Namespace) -> Dict[str, int]:
    pack = load_json(args.pack)
    cells_geo = load_json(args.cells)
    rivers_geo = load_json(args.rivers)
    routes_geo = load_json(args.routes)
    markers_geo = load_json(args.markers)
    burgs_csv = load_csv_by_id(args.burgs_csv)
    route_names_csv = load_csv_by_id(args.routes_csv)

    pack_data = pack.get("cells") or {}
    info = pack.get("info") or {}

    cells = index_dict_records(pack_data.get("cells", []))
    burgs = index_dict_records(pack_data.get("burgs", []))
    states = index_dict_records(pack_data.get("states", []))
    provinces = index_dict_records(pack_data.get("provinces", []))
    rivers = index_dict_records(pack_data.get("rivers", []))
    markers = index_dict_records(pack_data.get("markers", []))
    routes = index_dict_records(pack_data.get("routes", []))
    markets = index_dict_records(pack_data.get("markets", []))
    goods = index_dict_records(pack_data.get("goods", []))
    cultures = index_dict_records(pack_data.get("cultures", []))
    religions = index_dict_records(pack_data.get("religions", []))
    biomes = index_dict_records(pack_data.get("biomes", []))
    features = index_dict_records(pack_data.get("features", []))

    xy_to_lonlat = make_xy_transform(fit_affine_xy_to_lonlat(markers_geo))

    # GeoJSON indices and per-cell geometry summaries.
    cell_geo_by_id: Dict[int, Mapping[str, Any]] = {}
    cell_bbox_by_id: Dict[int, List[float]] = {}
    state_bbox: Dict[int, List[float]] = {}
    province_bbox: Dict[int, List[float]] = {}
    for feature in cells_geo.get("features", []):
        props = feature.get("properties") or {}
        ident = props.get("id")
        if not isinstance(ident, int):
            continue
        cell_geo_by_id[ident] = feature
        fb = geometry_bbox(feature.get("geometry") or {})
        if fb:
            cell_bbox_by_id[ident] = fb
            sid = props.get("state")
            pid = props.get("province")
            if isinstance(sid, int) and sid:
                state_bbox[sid] = bbox_union(state_bbox.get(sid), fb)  # type: ignore[assignment]
            if isinstance(pid, int) and pid:
                province_bbox[pid] = bbox_union(province_bbox.get(pid), fb)  # type: ignore[assignment]

    river_geo_by_id = {
        int(f.get("properties", {}).get("id")): f
        for f in rivers_geo.get("features", [])
        if isinstance(f.get("properties", {}).get("id"), int)
    }
    route_geo_by_id = {
        int(f.get("properties", {}).get("id")): f
        for f in routes_geo.get("features", [])
        if isinstance(f.get("properties", {}).get("id"), int)
    }
    marker_geo_by_id: Dict[int, Mapping[str, Any]] = {}
    for f in markers_geo.get("features", []):
        props = f.get("properties") or {}
        raw_id = props.get("id")
        ident: Optional[int] = None
        if isinstance(raw_id, int):
            ident = raw_id
        elif isinstance(raw_id, str):
            m = re.search(r"(\d+)$", raw_id)
            if m:
                ident = int(m.group(1))
        if ident is not None:
            marker_geo_by_id[ident] = f

    # Map feature id -> member pack cell ids, used for lake bbox.
    feature_cells: Dict[int, List[int]] = {}
    for cid, cell in cells.items():
        fid = cell.get("f")
        if isinstance(fid, int) and fid:
            feature_cells.setdefault(fid, []).append(cid)

    output_root: Path = args.output
    for dirname in COLLECTIONS.values():
        if not args.dry_run:
            (output_root / dirname).mkdir(parents=True, exist_ok=True)

    written: Dict[str, int] = {v: 0 for v in COLLECTIONS.values()}
    expected_paths: set[Path] = set()

    # --- States ---
    for sid, state in sorted(states.items()):
        if sid == 0:
            continue
        status = status_for(state)
        if args.exclude_removed and status == "removed":
            continue
        coord = None
        pole = state.get("pole")
        if isinstance(pole, list) and len(pole) >= 2:
            coord = xy_to_lonlat(pole[0], pole[1])
        bbox = state_bbox.get(sid)
        title = str(state.get("name") or f"State {sid}")
        form = state.get("form") or state.get("formName") or "state"
        doc = common_doc(
            title=title, entity_id=f"state:{sid}", source_id=sid, category="state",
            subtype=slugish_type(form, "state"), status=status,
            coordinates=coord or bbox_center(bbox), bbox=bbox, map_zoom=5,
        )
        capital_id = state.get("capital") if isinstance(state.get("capital"), int) else None
        culture_id = state.get("culture") if isinstance(state.get("culture"), int) else None
        province_ids = nonzero_sorted(state.get("provinces") or [])
        neighbor_ids = nonzero_sorted(state.get("neighbors") or [])
        military_refs = []
        for unit in state.get("military") or []:
            if isinstance(unit, dict) and isinstance(unit.get("i"), int):
                military_refs.append(f"poi:military:{sid}:{unit['i']}")
        doc.update({
            "full_name": state.get("fullName"),
            "form": state.get("form"),
            "form_name": state.get("formName"),
            "capital_id": capital_id,
            "capital_name": burgs.get(capital_id, {}).get("name") if capital_id is not None else None,
            "culture_id": culture_id,
            "culture_name": cultures.get(culture_id, {}).get("name") if culture_id is not None else None,
            "province_ids": province_ids,
            "province_names": names_for_ids(province_ids, provinces),
            "neighbor_state_ids": neighbor_ids,
            "neighbor_state_names": names_for_ids(neighbor_ids, states),
            "military_entity_ids": military_refs,
        })
        merge_extras(doc, state, {
            "i", "name", "fullName", "form", "formName", "capital", "culture", "pole",
            "provinces", "neighbors", "military", "removed"
        })
        path = output_root / "_states" / f"{sid}.md"
        expected_paths.add(path)
        write_document(path, doc, "state", args.dry_run)
        written["_states"] += 1

    # --- Provinces ---
    for pid, province in sorted(provinces.items()):
        status = status_for(province)
        if args.exclude_removed and status == "removed":
            continue
        pole = province.get("pole")
        coord = xy_to_lonlat(pole[0], pole[1]) if isinstance(pole, list) and len(pole) >= 2 else None
        bbox = province_bbox.get(pid)
        title = str(province.get("name") or f"Province {pid}")
        subtype = slugish_type(province.get("formName"), "province")
        doc = common_doc(
            title=title, entity_id=f"province:{pid}", source_id=pid, category="province",
            subtype=subtype, status=status, coordinates=coord or bbox_center(bbox), bbox=bbox, map_zoom=8,
        )
        sid = province.get("state") if isinstance(province.get("state"), int) else None
        burg_id = province.get("burg") if isinstance(province.get("burg"), int) else None
        settlement_ids = nonzero_sorted(province.get("burgs") or [])
        doc.update({
            "full_name": province.get("fullName"),
            "form_name": province.get("formName"),
            "state_id": sid,
            "state_name": states.get(sid, {}).get("name") if sid is not None else None,
            "capital_id": burg_id,
            "capital_name": burgs.get(burg_id, {}).get("name") if burg_id is not None else None,
            "settlement_ids": settlement_ids,
            "settlement_names": names_for_ids(settlement_ids, burgs),
        })
        merge_extras(doc, province, {
            "i", "name", "fullName", "formName", "state", "burg", "burgs", "pole", "removed"
        })
        path = output_root / "_provinces" / f"{pid}.md"
        expected_paths.add(path)
        write_document(path, doc, "province", args.dry_run)
        written["_provinces"] += 1

    # --- Settlements ---
    for bid, burg in sorted(burgs.items()):
        status = status_for(burg)
        if args.exclude_removed and status == "removed":
            continue
        burg_csv = burgs_csv.get(bid)
        lon = csv_float(burg_csv, "Longitude")
        lat = csv_float(burg_csv, "Latitude")
        coord = [lon, lat] if lon is not None and lat is not None else None
        cell_id = burg.get("cell") if isinstance(burg.get("cell"), int) else None
        cell = cells.get(cell_id, {}) if cell_id is not None else {}
        cell_geo = cell_geo_by_id.get(cell_id, {}) if cell_id is not None else {}
        cell_props = cell_geo.get("properties") or {}
        sid = burg.get("state") if isinstance(burg.get("state"), int) else cell.get("state")
        pid = cell.get("province") if isinstance(cell.get("province"), int) else cell_props.get("province")
        culture_id = burg.get("culture") if isinstance(burg.get("culture"), int) else cell.get("culture")
        religion_id = cell.get("religion") if isinstance(cell.get("religion"), int) else cell_props.get("religion")
        biome_id = cell.get("biome") if isinstance(cell.get("biome"), int) else cell_props.get("biome")
        title = str(burg.get("name") or f"Settlement {bid}")
        capital = bool(burg.get("capital"))
        subtype = "capital" if capital else slugish_type(burg.get("type"), "settlement")
        pop = burg.get("population")
        zoom = 8 if capital else (9 if isinstance(pop, (int, float)) and pop >= 20 else 11)
        doc = common_doc(
            title=title, entity_id=f"settlement:{bid}", source_id=bid, category="settlement",
            subtype=subtype, status=status, coordinates=coord, bbox=point_bbox(coord), map_zoom=zoom,
        )
        doc.update({
            "cell_id": cell_id,
            "feature_id": burg.get("feature"),
            "state_id": sid,
            "state_name": states.get(sid, {}).get("name") if isinstance(sid, int) else None,
            "province_id": pid if isinstance(pid, int) and pid else None,
            "province_name": provinces.get(pid, {}).get("name") if isinstance(pid, int) and pid else None,
            "culture_id": culture_id if isinstance(culture_id, int) and culture_id else None,
            "culture_name": cultures.get(culture_id, {}).get("name") if isinstance(culture_id, int) else None,
            "religion_id": religion_id if isinstance(religion_id, int) and religion_id else None,
            "religion_name": religions.get(religion_id, {}).get("name") if isinstance(religion_id, int) else None,
            "biome_id": biome_id if isinstance(biome_id, int) else None,
            "biome_name": biomes.get(biome_id, {}).get("name") if isinstance(biome_id, int) else None,
            "height_m": cell_props.get("height"),
            "coordinate_source": "burgs_csv" if coord is not None else None,
            "settlement_type": burg.get("type"),
            "population": pop,
            "capital": capital,
            "port": bool(burg.get("port")),
            "market": market_payload(burg.get("market"), markets, goods, burgs, bid),
        })
        merge_extras(doc, burg, {
            "i", "name", "x", "y", "cell", "feature", "state", "culture", "type", "population",
            "capital", "port", "market", "removed"
        })
        path = output_root / "_settlements" / f"{bid}.md"
        expected_paths.add(path)
        write_document(path, doc, "settlement", args.dry_run)
        written["_settlements"] += 1

    # --- Rivers ---
    for rid, river in sorted(rivers.items()):
        status = status_for(river)
        if args.exclude_removed and status == "removed":
            continue
        feature = river_geo_by_id.get(rid)
        geometry = (feature or {}).get("geometry") or {}
        bbox = geometry_bbox(geometry)
        coord = representative_coord(geometry)
        traversal_cells = [c for c in river.get("cells") or [] if isinstance(c, int)]
        state_ids = nonzero_sorted(cells.get(c, {}).get("state") for c in traversal_cells)
        province_ids = nonzero_sorted(cells.get(c, {}).get("province") for c in traversal_cells)
        title = str(river.get("name") or (feature or {}).get("properties", {}).get("name") or f"River {rid}")
        discharge = river.get("discharge")
        zoom = 7 if isinstance(discharge, (int, float)) and discharge >= 500 else 9
        doc = common_doc(
            title=title, entity_id=f"river:{rid}", source_id=rid, category="river",
            subtype=slugish_type(river.get("type"), "river"), status=status,
            coordinates=coord, bbox=bbox, map_zoom=zoom,
        )
        doc.update({
            "river_type": river.get("type"),
            "source_cell_id": river.get("source"),
            "mouth_cell_id": river.get("mouth"),
            "parent_river_id": river.get("parent"),
            "basin_id": river.get("basin"),
            "state_ids": state_ids,
            "state_names": names_for_ids(state_ids, states),
            "province_ids": province_ids,
            "province_names": names_for_ids(province_ids, provinces),
            "geometry_type": geometry.get("type") or "LineString",
        })
        merge_extras(doc, river, {
            "i", "name", "type", "source", "mouth", "parent", "basin", "cells", "removed"
        })
        path = output_root / "_rivers" / f"{rid}.md"
        expected_paths.add(path)
        write_document(path, doc, "river", args.dry_run)
        written["_rivers"] += 1

    # --- Lakes (PackCells features of type lake, geometry summarized from member cells) ---
    for lake_id, lake in sorted(features.items()):
        if lake.get("type") != "lake":
            continue
        status = status_for(lake)
        if args.exclude_removed and status == "removed":
            continue
        member_cells = feature_cells.get(lake_id, [])
        bbox: Optional[List[float]] = None
        for cid in member_cells:
            bbox = bbox_union(bbox, cell_bbox_by_id.get(cid))
        shoreline = [c for c in lake.get("shoreline") or [] if isinstance(c, int)]
        state_ids = nonzero_sorted(cells.get(c, {}).get("state") for c in shoreline)
        province_ids = nonzero_sorted(cells.get(c, {}).get("province") for c in shoreline)
        title = str(lake.get("name") or f"Lake {lake_id}")
        area = lake.get("area")
        zoom = 7 if isinstance(area, (int, float)) and area >= 100 else 9
        coord = bbox_center(bbox)
        doc = common_doc(
            title=title, entity_id=f"lake:{lake_id}", source_id=lake_id, category="lake",
            subtype=slugish_type(lake.get("group"), "lake"), status=status,
            coordinates=coord, bbox=bbox, map_zoom=zoom,
        )
        inlet_ids = nonzero_sorted(lake.get("inlets") or [])
        outlet_id = lake.get("outlet") if isinstance(lake.get("outlet"), int) and lake.get("outlet") else None
        doc.update({
            "lake_type": lake.get("group"),
            "state_ids": state_ids,
            "state_names": names_for_ids(state_ids, states),
            "province_ids": province_ids,
            "province_names": names_for_ids(province_ids, provinces),
            "inlet_river_ids": inlet_ids,
            "inlet_river_names": names_for_ids(inlet_ids, rivers),
            "outlet_river_id": outlet_id,
            "outlet_river_name": rivers.get(outlet_id, {}).get("name") if outlet_id is not None else None,
            "member_cell_count": len(member_cells),
            "geometry_type": "MultiPolygon",
        })
        merge_extras(doc, lake, {
            "i", "name", "type", "group", "vertices", "shoreline", "inlets", "outlet", "firstCell", "removed"
        })
        path = output_root / "_lakes" / f"{lake_id}.md"
        expected_paths.add(path)
        write_document(path, doc, "lake", args.dry_run)
        written["_lakes"] += 1

    # --- Routes ---
    group_to_subtype = {"roads": "road", "trails": "trail", "searoutes": "sea_route"}
    group_to_title = {"roads": "Road", "trails": "Trail", "searoutes": "Sea Route"}
    for route_id, route in sorted(routes.items()):
        status = status_for(route)
        if args.exclude_removed and status == "removed":
            continue
        feature = route_geo_by_id.get(route_id)
        props = (feature or {}).get("properties") or {}
        geometry = (feature or {}).get("geometry") or {}
        group = route.get("group") or props.get("group") or "route"
        subtype = group_to_subtype.get(str(group), slugish_type(group, "route"))
        route_csv = route_names_csv.get(route_id)
        csv_name = (route_csv.get("Route") or "").strip() if route_csv else ""
        actual_name = csv_name or route.get("name") or props.get("name")
        title = str(actual_name or f"{group_to_title.get(str(group), 'Route')} {route_id}")
        bbox = geometry_bbox(geometry)
        coord = representative_coord(geometry)
        traversal_cells = []
        for p in route.get("points") or []:
            if isinstance(p, list) and len(p) >= 3 and isinstance(p[2], int):
                traversal_cells.append(p[2])
        state_ids = nonzero_sorted(cells.get(c, {}).get("state") for c in traversal_cells)
        province_ids = nonzero_sorted(cells.get(c, {}).get("province") for c in traversal_cells)
        doc = common_doc(
            title=title, entity_id=f"route:{route_id}", source_id=route_id, category="route",
            subtype=subtype, status=status, coordinates=coord, bbox=bbox, map_zoom=10,
        )
        doc.update({
            "route_type": subtype,
            "route_group": group,
            "named": bool(actual_name),
            "name_source": "routes_csv" if csv_name else ("packcells" if route.get("name") else ("geojson" if props.get("name") else None)),
            "length": (route_csv.get("Length") or "").strip() if route_csv else None,
            "state_ids": state_ids,
            "state_names": names_for_ids(state_ids, states),
            "province_ids": province_ids,
            "province_names": names_for_ids(province_ids, provinces),
            "geometry_type": geometry.get("type") or "LineString",
            "geometry_vertex_count": sum(1 for _ in iter_geometry_coords(geometry.get("coordinates"))),
        })
        merge_extras(doc, route, {"i", "name", "group", "points", "removed"})
        # GeoJSON may contain future non-geometry properties not present in PackCells.
        geo_extras = {k: v for k, v in props.items() if k not in {"id", "name", "group"}}
        for k, v in deep_snake(geo_extras).items():
            doc.setdefault(k, v)
        path = output_root / "_routes" / f"{route_id}.md"
        expected_paths.add(path)
        write_document(path, doc, "route", args.dry_run)
        written["_routes"] += 1

    # --- POI markers ---
    for marker_id, marker in sorted(markers.items()):
        status = status_for(marker)
        if args.exclude_removed and status == "removed":
            continue
        feature = marker_geo_by_id.get(marker_id)
        props = (feature or {}).get("properties") or {}
        geometry = (feature or {}).get("geometry") or {}
        coord = representative_coord(geometry) or xy_to_lonlat(marker.get("x"), marker.get("y"))
        cell_id = marker.get("cell") if isinstance(marker.get("cell"), int) else None
        cell = cells.get(cell_id, {}) if cell_id is not None else {}
        sid = cell.get("state") if isinstance(cell.get("state"), int) and cell.get("state") else None
        pid = cell.get("province") if isinstance(cell.get("province"), int) and cell.get("province") else None
        culture_id = cell.get("culture") if isinstance(cell.get("culture"), int) and cell.get("culture") else None
        religion_id = cell.get("religion") if isinstance(cell.get("religion"), int) and cell.get("religion") else None
        biome_id = cell.get("biome") if isinstance(cell.get("biome"), int) else None
        marker_type = marker.get("type") or props.get("type") or "marker"
        title = str(props.get("name") or marker.get("name") or f"POI {marker_id}")
        doc = common_doc(
            title=title, entity_id=f"poi:marker:{marker_id}", source_id=f"marker{marker_id}", category="poi",
            subtype=slugish_type(marker_type, "marker"), status=status,
            coordinates=coord, bbox=point_bbox(coord), map_zoom=11,
        )
        cell_props = (cell_geo_by_id.get(cell_id, {}) or {}).get("properties") or {}
        doc.update({
            "poi_group": "marker",
            "poi_type": marker_type,
            "icon": props.get("icon") or marker.get("icon"),
            "description": props.get("legend") or marker.get("legend"),
            "cell_id": cell_id,
            "state_id": sid,
            "state_name": states.get(sid, {}).get("name") if sid is not None else None,
            "province_id": pid,
            "province_name": provinces.get(pid, {}).get("name") if pid is not None else None,
            "culture_id": culture_id,
            "culture_name": cultures.get(culture_id, {}).get("name") if culture_id is not None else None,
            "religion_id": religion_id,
            "religion_name": religions.get(religion_id, {}).get("name") if religion_id is not None else None,
            "biome_id": biome_id,
            "biome_name": biomes.get(biome_id, {}).get("name") if biome_id is not None else None,
            "height_m": cell_props.get("height"),
        })
        merge_extras(doc, marker, {"i", "name", "type", "icon", "legend", "x", "y", "cell", "removed"})
        geo_extras = {k: v for k, v in props.items() if k not in {"id", "name", "type", "icon", "legend", "x", "y"}}
        for k, v in deep_snake(geo_extras).items():
            doc.setdefault(k, v)
        path = output_root / "_pois" / f"marker-{marker_id}.md"
        expected_paths.add(path)
        write_document(path, doc, "poi", args.dry_run)
        written["_pois"] += 1

    # --- POI military units from state.military ---
    for sid, state in sorted(states.items()):
        if sid == 0:
            continue
        for unit in state.get("military") or []:
            if not isinstance(unit, dict) or not isinstance(unit.get("i"), int):
                continue
            uid = int(unit["i"])
            status = status_for(unit)
            if args.exclude_removed and status == "removed":
                continue
            subtype = "fleet" if bool(unit.get("n")) or "fleet" in (unit.get("u") or {}) else "regiment"
            coord = xy_to_lonlat(unit.get("x"), unit.get("y"))
            cell_id = unit.get("cell") if isinstance(unit.get("cell"), int) else None
            cell = cells.get(cell_id, {}) if cell_id is not None else {}
            pid = cell.get("province") if isinstance(cell.get("province"), int) and cell.get("province") else None
            title = str(unit.get("name") or f"{subtype.title()} {sid}-{uid}")
            doc = common_doc(
                title=title, entity_id=f"poi:military:{sid}:{uid}", source_id=f"{sid}:{uid}", category="poi",
                subtype=subtype, status=status, coordinates=coord, bbox=point_bbox(coord), map_zoom=10,
            )
            doc.update({
                "poi_group": "military",
                "poi_type": subtype,
                "state_id": sid,
                "state_name": state.get("name"),
                "province_id": pid,
                "province_name": provinces.get(pid, {}).get("name") if pid is not None else None,
                "cell_id": cell_id,
                "strength": unit.get("a"),
                "icon": unit.get("icon"),
                "composition": deep_snake(unit.get("u") or {}),
            })
            merge_extras(doc, unit, {"i", "name", "state", "x", "y", "bx", "by", "cell", "a", "icon", "u", "n", "removed"})
            path = output_root / "_pois" / f"military-{sid}-{uid}.md"
            expected_paths.add(path)
            write_document(path, doc, "poi", args.dry_run)
            written["_pois"] += 1

    if args.prune and not args.dry_run:
        for dirname in COLLECTIONS.values():
            folder = output_root / dirname
            if not folder.exists():
                continue
            for path in folder.glob("*.md"):
                if path not in expected_paths:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    if AUTO_BEGIN in text and AUTO_END in text:
                        path.unlink()

    return written


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Aulonia Jekyll collections from PackCells and Azgaar GeoJSON exports."
    )
    parser.add_argument("--pack", required=True, type=Path, help="Aulonia PackCells JSON")
    parser.add_argument("--cells", required=True, type=Path, help="Aulonia Cells GeoJSON")
    parser.add_argument("--rivers", required=True, type=Path, help="Aulonia Rivers GeoJSON")
    parser.add_argument("--routes", required=True, type=Path, help="Aulonia Routes GeoJSON")
    parser.add_argument("--markers", required=True, type=Path, help="Aulonia Markers GeoJSON")
    parser.add_argument("--burgs-csv", required=True, type=Path, help="Azgaar Burgs CSV; authoritative settlement Latitude/Longitude source")
    parser.add_argument("--routes-csv", required=True, type=Path, help="Azgaar Routes CSV; authoritative route name source")
    parser.add_argument("--output", required=True, type=Path, help="Jekyll repository root")
    parser.add_argument("--exclude-removed", action="store_true", help="Do not write records marked removed")
    parser.add_argument("--prune", action="store_true", help="Delete stale auto-generated Markdown files from the seven collections")
    parser.add_argument("--dry-run", action="store_true", help="Validate/read sources and report counts without writing files")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    for attr in ("pack", "cells", "rivers", "routes", "markers", "burgs_csv", "routes_csv"):
        path = getattr(args, attr)
        if not path.is_file():
            print(f"ERROR: {attr} file not found: {path}", file=sys.stderr)
            return 2
    counts = build(args)
    total = sum(counts.values())
    print("Aulonia Jekyll collections built successfully." if not args.dry_run else "Aulonia sources validated successfully (dry run).")
    for dirname in ("_states", "_provinces", "_settlements", "_rivers", "_lakes", "_routes", "_pois"):
        print(f"  {dirname:14s} {counts.get(dirname, 0):5d}")
    print(f"  {'TOTAL':14s} {total:5d}")
    print("Search index: not generated (by design; build it later from Jekyll collections).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
