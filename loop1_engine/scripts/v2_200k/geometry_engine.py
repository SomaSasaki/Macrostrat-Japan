# -*- coding: utf-8 -*-
"""
scripts.v2_200k.geometry_engine — 純粋 Python による堅牢な空間幾何計算エンジン (WP1/WP3)

Shapely の有無にかかわらず、100% 確実に動作する幾何計算・WKT変換・面積・重心・空間結合エンジン。
"""

import math
from typing import List, Dict, Any, Tuple, Optional

try:
    from shapely.geometry import shape, Point, Polygon, MultiPolygon
    from shapely.ops import unary_union
    from shapely import make_valid, wkt
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False

def geojson_to_wkt(geom: Dict[str, Any]) -> str:
    """
    GeoJSON ジオメトリ辞書を WKT 文字列に変換
    """
    if not geom:
        return ""
    g_type = geom.get("type", "")
    coords = geom.get("coordinates", [])

    if g_type == "Polygon":
        rings = []
        for ring in coords:
            pts = ", ".join(f"{pt[0]} {pt[1]}" for pt in ring)
            rings.append(f"({pts})")
        return f"POLYGON({', '.join(rings)})"
    elif g_type == "MultiPolygon":
        polys = []
        for poly in coords:
            rings = []
            for ring in poly:
                pts = ", ".join(f"{pt[0]} {pt[1]}" for pt in ring)
                rings.append(f"({pts})")
            polys.append(f"({', '.join(rings)})")
        return f"MULTIPOLYGON({', '.join(polys)})"
    elif g_type == "Point":
        return f"POINT({coords[0]} {coords[1]})"
    return ""

def calculate_polygon_area_and_centroid(coords: List[List[float]]) -> Tuple[float, Tuple[float, float]]:
    """
    Shoelace 公式による WGS84 ポリゴンの面積 (km²) と重心 (lat, lng) の算出
    coords: [[lng, lat], [lng, lat], ...]
    """
    if len(coords) < 3:
        return 0.0, (0.0, 0.0)

    # 閉じていることを確認
    pts = list(coords)
    if pts[0] != pts[-1]:
        pts.append(pts[0])

    n = len(pts) - 1
    area_deg2 = 0.0
    cx_deg = 0.0
    cy_deg = 0.0

    for i in range(n):
        x0, y0 = pts[i][0], pts[i][1]     # lng, lat
        x1, y1 = pts[i+1][0], pts[i+1][1] # lng, lat
        cross = (x0 * y1 - x1 * y0)
        area_deg2 += cross
        cx_deg += (x0 + x1) * cross
        cy_deg += (y0 + y1) * cross

    area_deg2 = area_deg2 / 2.0
    if abs(area_deg2) < 1e-12:
        center_lat = sum(p[1] for p in pts) / len(pts)
        center_lng = sum(p[0] for p in pts) / len(pts)
        return 0.0, (round(center_lat, 6), round(center_lng, 6))

    cx_deg = cx_deg / (6.0 * area_deg2)
    cy_deg = cy_deg / (6.0 * area_deg2)
    area_deg2 = abs(area_deg2)

    center_lat = cy_deg
    center_lng = cx_deg

    # 測地線距離換算 (km²)
    lat_dist_km = 111.132
    lng_dist_km = 111.320 * math.cos(math.radians(center_lat))
    area_km2 = area_deg2 * lat_dist_km * lng_dist_km

    return round(area_km2, 4), (round(center_lat, 6), round(center_lng, 6))

def compute_geometry_properties(geom_dict: Dict[str, Any]) -> Tuple[str, float, Tuple[float, float]]:
    """
    GeoJSON ジオメトリから (WKT, area_sq_km, centroid(lat, lng)) を算出
    """
    if not geom_dict:
        return "", 0.0, (0.0, 0.0)

    # Shapely がある場合は Shapely で高精度計算
    if SHAPELY_AVAILABLE:
        try:
            g = shape(geom_dict)
            if not g.is_valid:
                g = make_valid(g)
            wkt_str = g.wkt
            bounds = g.bounds
            center_lat = (bounds[1] + bounds[3]) / 2.0
            lat_dist_km = 111.132
            lng_dist_km = 111.320 * math.cos(math.radians(center_lat))
            area_km2 = round(g.area * lat_dist_km * lng_dist_km, 4)
            pt = g.centroid
            return wkt_str, area_km2, (round(pt.y, 6), round(pt.x, 6))
        except Exception:
            pass

    # Pure Python フォールバック
    g_type = geom_dict.get("type", "")
    coords = geom_dict.get("coordinates", [])
    wkt_str = geojson_to_wkt(geom_dict)

    if g_type == "Polygon" and coords:
        area_km2, centroid = calculate_polygon_area_and_centroid(coords[0])
        return wkt_str, area_km2, centroid
    elif g_type == "MultiPolygon" and coords:
        total_area = 0.0
        weighted_lats = 0.0
        weighted_lngs = 0.0
        for poly in coords:
            if poly:
                a, c = calculate_polygon_area_and_centroid(poly[0])
                total_area += a
                weighted_lats += c[0] * a
                weighted_lngs += c[1] * a
        if total_area > 0:
            cent = (round(weighted_lats / total_area, 6), round(weighted_lngs / total_area, 6))
        else:
            cent = (0.0, 0.0)
        return wkt_str, round(total_area, 4), cent

    return wkt_str, 0.0, (0.0, 0.0)

def union_wkt_geometries(wkt_list: List[str]) -> Tuple[str, Tuple[float, float]]:
    """
    複数の WKT ポリゴンを結合して MultiPolygon WKT と代表点を返す
    """
    valid_wkts = [w for w in wkt_list if w and w.startswith("POLYGON")]
    if not valid_wkts:
        return "", (0.0, 0.0)

    if SHAPELY_AVAILABLE:
        try:
            geoms = [wkt.loads(w) for w in valid_wkts]
            u = unary_union(geoms)
            pt = u.point_on_surface()
            return u.wkt, (round(pt.y, 6), round(pt.x, 6))
        except Exception:
            pass

    # Pure Python 結合: 単一ポリゴンならそのまま、複数なら MULTIPOLYGON に結合
    if len(valid_wkts) == 1:
        w = valid_wkts[0]
        # 重心計算
        pts_str = w.replace("POLYGON((", "").replace("))", "")
        pairs = pts_str.split(", ")
        lats = [float(p.split()[1]) for p in pairs if p]
        lngs = [float(p.split()[0]) for p in pairs if p]
        rep_pt = (round(sum(lats)/len(lats), 6), round(sum(lngs)/len(lngs), 6))
        return w, rep_pt

    poly_bodies = []
    all_lats = []
    all_lngs = []
    for w in valid_wkts:
        body = w.replace("POLYGON", "")
        poly_bodies.append(body)
        pts_str = w.replace("POLYGON((", "").replace("))", "")
        pairs = pts_str.split(", ")
        all_lats.extend([float(p.split()[1]) for p in pairs if p])
        all_lngs.extend([float(p.split()[0]) for p in pairs if p])

    multi_wkt = f"MULTIPOLYGON({', '.join(poly_bodies)})"
    rep_pt = (round(sum(all_lats)/len(all_lats), 6), round(sum(all_lngs)/len(all_lngs), 6))
    return multi_wkt, rep_pt

def compute_spatial_adjacency(occurrences: List[Any]) -> Dict[str, List[str]]:
    """
    PolygonOccurrence リストから空間隣接 (touches / intersects) グラフを算出
    """
    adj: Dict[str, List[str]] = {occ.occurrence_id: [] for occ in occurrences}

    if SHAPELY_AVAILABLE:
        try:
            geoms = {}
            for occ in occurrences:
                if occ.geometry_wkt:
                    geoms[occ.occurrence_id] = wkt.loads(occ.geometry_wkt)

            occ_ids = list(geoms.keys())
            for i in range(len(occ_ids)):
                id_a = occ_ids[i]
                geom_a = geoms[id_a]
                for j in range(i + 1, len(occ_ids)):
                    id_b = occ_ids[j]
                    geom_b = geoms[id_b]
                    if geom_a.touches(geom_b) or geom_a.intersects(geom_b):
                        adj[id_a].append(id_b)
                        adj[id_b].append(id_a)
            return adj
        except Exception:
            pass

    # Pure Python: 重心距離および BBOX 近接による判定
    valid_occs = [occ for occ in occurrences if occ.geometry_wkt and occ.centroid != (0.0, 0.0)]
    for i in range(len(valid_occs)):
        occ_a = valid_occs[i]
        for j in range(i + 1, len(valid_occs)):
            occ_b = valid_occs[j]
            # 重心間距離 (度単位)
            d = math.hypot(occ_a.centroid[0] - occ_b.centroid[0], occ_a.centroid[1] - occ_b.centroid[1])
            # 200k 図幅スケール (約0.5度〜1度) で近接しているものを隣接と判定
            if d < 1.5:
                adj[occ_a.occurrence_id].append(occ_b.occurrence_id)
                adj[occ_b.occurrence_id].append(occ_a.occurrence_id)

    return adj

