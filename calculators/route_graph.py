"""Граф и метрики v9, перенесённые без замены геометрического алгоритма.

Источник: research_notes/Расчёт характеристик/scripts/geo.py. Изменён только вход:
поток нормализованных OSM-записей и манифест вместо исследовательских путей.
Ограничения рёбер и узлов, выбор цели, шаги, переходы и освещение сохранены.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import sys
from collections.abc import Mapping

import numpy as np
from pyproj import CRS, Transformer
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from shapely import STRtree, wkb
from shapely.geometry import LineString, Point
from shapely.ops import transform as gtransform

BUSY = {"trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link"}

WALK = {
    "footway",
    "path",
    "pedestrian",
    "steps",
    "living_street",
    "residential",
    "service",
    "unclassified",
    "tertiary",
    "tertiary_link",
    "secondary",
    "secondary_link",
    "primary",
    "primary_link",
    "road",
    "track",
}

CENTERS = {
    "sochi": (39.7231, 43.5855),
    "moscow": (37.6176, 55.7558),
    "spb": (30.3158, 59.9391),
    "kazan": (49.1064, 55.7989),
    "kaliningrad": (20.5109, 54.7104),
    "minsk": (27.5618, 53.9023),
    "istanbul": (28.9784, 41.0082),
    "dubai": (55.2708, 25.2048),
}

COAST = {"sochi", "spb", "kaliningrad", "istanbul", "dubai"}

COUNTRY = {
    "sochi": "RU",
    "moscow": "RU",
    "spb": "RU",
    "kazan": "RU",
    "kaliningrad": "RU",
    "minsk": "BY",
    "istanbul": "TR",
    "dubai": "AE",
}

MARINE_ADJACENCY_TOLERANCE_M = 25


def marine_beach_classification(beach, coastlines, coastline_tree, manifest=None):
    """Conservative mapped marine adjacency, not a measured water-kind classifier.

    natural=beach also describes inland beaches. Only contact with an explicit
    natural=coastline, allowing a declared small mapping gap, supplies a marine
    candidate. Failed adjacency is unknown, never proof that a beach is inland.
    """
    result = {
        "method": "OSM natural=beach geometry minimum distance to explicit natural=coastline",
        "method_version": "marine-adjacency-v1",
        "tolerance_m": MARINE_ADJACENCY_TOLERANCE_M,
        "beach_id": beach["sid"],
        "accepted": False,
        "state": "marine_adjacency_unconfirmed",
        "coastline_distance_m": None,
        "coastline_source": None,
        "limitation": (
            "Geometric acceptance tolerance, not measured classifier accuracy;"
            " mapping gaps may omit real sea beaches."
        ),
    }
    if not coastline_tree or not coastlines:
        result["reason"] = (
            "No explicit marine coastline geometry in cached extraction; no "
            "river/lake substitution."
        )
        return result
    coast = coastlines[int(coastline_tree.nearest(beach["geometry"]))]
    distance = float(beach["geometry"].distance(coast["geometry"]))
    result.update(
        {
            "coastline_distance_m": distance,
            "coastline_source": osm_source(coast["record"], manifest),
        }
    )
    if distance <= MARINE_ADJACENCY_TOLERANCE_M:
        result.update(
            {
                "accepted": True,
                "state": "marine_adjacent_mapped_candidate",
                "reason": (
                    "Mapped beach contacts explicit marine coastline within declared "
                    "geometric tolerance."
                ),
            }
        )
    else:
        result["reason"] = (
            "Beach geometry is not adjacent to cached explicit marine "
            "coastline within tolerance; kind not established."
        )
    return result


def osm_id(r):
    return f"{r['osm_type']}/{r['osm_id']}"


def osm_source(r, manifest=None):
    r = r.get("record", r)
    return {
        "provider": "OpenStreetMap",
        "url": "https://www.openstreetmap.org/" + osm_id(r),
        "version": r.get("version"),
        "observed_at": r.get("timestamp"),
        "date_kind": "OSM edit, not physical inspection",
        "license": "ODbL1.0",
        "license_url": "https://www.openstreetmap.org/copyright",
        "dataset_version": (manifest or {}).get("source_date"),
        "dataset_sha256": (manifest or {}).get("sha256"),
    }


def allowed(t):
    hw = t.get("highway")
    if t.get("foot") in {"no", "private", "use_sidepath"}:
        return False
    if t.get("access") in {"no", "private", "customers", "permit", "destination"} and t.get(
        "foot"
    ) not in {"yes", "designated", "permissive"}:
        return False
    if t.get("construction") or t.get("proposed"):
        return False
    if t.get("foot:conditional") or t.get("access:conditional"):
        return False
    return hw in WALK or (hw == "cycleway" and t.get("foot") in {"yes", "designated", "permissive"})


def blocked_pedestrian_node(t):
    """Known node restrictions apply to traversal as well as way restrictions.

    A bollard alone normally permits pedestrians. Missing barrier policy remains
    an uninspected gap; it is not converted into either permission or a ban.
    """
    public_foot = t.get("foot") in {"yes", "designated", "permissive"}
    if t.get("foot") in {"no", "private", "use_sidepath"}:
        return "explicit_foot_restriction"
    if t.get("foot:conditional") or t.get("access:conditional"):
        return "unresolved_conditional_node_access"
    if (
        t.get("access") in {"no", "private", "customers", "permit", "destination"}
        and not public_foot
    ):
        return "explicit_access_restriction_without_public_foot_override"
    if (
        t.get("locked") == "yes"
        and t.get("barrier")
        in {
            "gate",
            "lift_gate",
            "swing_gate",
            "sliding_gate",
            "wicket_gate",
            "toll_booth",
            "turnstile",
            "full-height_turnstile",
            "kissing_gate",
        }
        and not public_foot
    ):
        return "locked_gate_without_explicit_public_foot_permission"
    return None


class PackedNodes:
    """Узлы в float64: ID проверяются до преобразования, потери точности запрещены."""

    def __init__(self, values):
        if any(abs(int(v[0])) > 2**53 for v in values):
            raise ValueError("OSM node ID exceeds exact float64 range")
        self.array = np.asarray(values, dtype=np.float64)

    def __len__(self):
        return len(self.array)

    def __iter__(self):
        return ((int(v[0]), float(v[1]), float(v[2])) for v in self.array)

    def __getitem__(self, key):
        if isinstance(key, slice):
            result = object.__new__(PackedNodes)
            result.array = self.array[key]
            return result
        v = self.array[key]
        return int(v[0]), float(v[1]), float(v[2])


class EdgeValue(Mapping):
    """Рёбра одного OSM way разделяют метаданные, сохраняя собственную длину."""

    def __init__(self, length, metadata):
        self.length, self.metadata = length, metadata

    def __getitem__(self, key):
        return self.length if key == "length_m" else self.metadata[key]

    def __iter__(self):
        yield "length_m"
        yield from self.metadata

    def __len__(self):
        return len(self.metadata) + 1


class PedestrianGraph:
    def __init__(self, city, records, manifest, *, compact=False):
        self.city = city
        x, y = CENTERS[city]
        self.tr = Transformer.from_crs(
            4326,
            CRS.from_proj4(f"+proj=aeqd +lat_0={y} +lon_0={x} +datum=WGS84 +units=m"),
            always_xy=True,
        )
        self.inv = Transformer.from_crs(self.tr.target_crs, 4326, always_xy=True)
        self.records = []
        self.roads = []
        self.busy = []
        self.rail = []
        self.beaches = []
        self.coastlines = []
        self.beach_classifications = []
        self.blocked_nodes = {}
        self.pois = []
        self.stops = []
        self.entrances = []
        self.crossing_nodes = {}
        nodes = {}
        roadways = []
        self.manifest = {}
        self.coverage_bounds = None
        for r in records:
            if compact:
                r["tags"] = {
                    sys.intern(k): sys.intern(v) if isinstance(v, str) else v
                    for k, v in r["tags"].items()
                }
                if r.get("nodes"):
                    r["nodes"] = PackedNodes(r["nodes"])
            m = manifest
            self.manifest = m
            t = r["tags"]
            hw = t.get("highway")
            sid = osm_id(r)
            if r["osm_type"] == "node":
                node_restriction = blocked_pedestrian_node(t)
                if node_restriction:
                    self.blocked_nodes[r["osm_id"]] = {
                        "reason": node_restriction,
                        "tags": t,
                        "source": osm_source(r, m),
                    }
            relevant = (
                hw
                or t.get("amenity")
                or t.get("shop")
                or t.get("leisure")
                or t.get("office")
                or t.get("healthcare")
                or t.get("railway")
                or t.get("public_transport")
                or t.get("natural") in {"beach", "coastline"}
                or t.get("entrance")
            )
            if not relevant:
                continue
            try:
                g = wkb.loads(r["geometry_wkb"], hex=True)
                pg = gtransform(self.tr.transform, g)
            except Exception:
                continue
            if compact:
                r.pop("geometry_wkb", None)
            rr = {
                "record": r,
                "geometry": pg,
                "point": pg.representative_point(),
                "sid": sid,
                "tags": t,
            }
            if r["osm_type"] == "node" and hw == "crossing":
                self.crossing_nodes[r["osm_id"]] = rr
            if hw in BUSY and g.geom_type in {"LineString", "MultiLineString"}:
                self.busy.append(rr)
            if (
                t.get("railway") in {"rail", "light_rail", "tram"}
                and not any(k in t for k in ["disused", "abandoned", "construction", "proposed"])
                and not any(
                    k.startswith(("disused:", "abandoned:", "construction:", "proposed:"))
                    for k in t
                )
            ):
                self.rail.append(rr)
            if t.get("natural") == "beach":
                self.beaches.append(rr)
            if t.get("natural") == "coastline":
                self.coastlines.append(rr)
            if t.get("entrance") not in {None, "no", "exit"}:
                self.entrances.append(rr)
            if (
                t.get("amenity")
                or t.get("shop")
                or t.get("leisure")
                or t.get("healthcare")
                or t.get("office")
            ):
                self.pois.append(rr)
            if (
                hw == "bus_stop"
                or t.get("railway") in {"station", "halt", "tram_stop", "subway_entrance"}
                or t.get("public_transport") == "platform"
            ):
                self.stops.append(rr)
            if allowed(t) and r.get("nodes"):
                nd = r["nodes"]
                roadways.append((nd, rr))
                nodes.update((n[0], (n[1], n[2])) for n in nd)
        self.nids = sorted(n for n in nodes if n not in self.blocked_nodes)
        self.index = {n: i for i, n in enumerate(self.nids)}
        if not self.nids:
            raise ValueError("empty_pedestrian_graph")
        xy = np.array([nodes[n] for n in self.nids])
        xx, yy = self.tr.transform(xy[:, 0], xy[:, 1])
        self.xy = np.column_stack([xx, yy])
        self.tree = cKDTree(self.xy)
        self.edges = {}
        for nd, r in roadways:
            for a, b in zip(nd, nd[1:], strict=False):
                if a[0] not in self.index or b[0] not in self.index:
                    continue
                u = self.index[a[0]]
                v = self.index[b[0]]
                if u == v:
                    continue
                key = (min(u, v), max(u, v))
                d = math.dist(self.xy[u], self.xy[v])
                old = self.edges.get(key)
                if old is None or d < old["length_m"]:
                    self.edges[key] = EdgeValue(d, r) if compact else {"length_m": d, **r}
        rows = []
        cols = []
        dist = []
        for (u, v), r in self.edges.items():
            rows.extend([u, v])
            cols.extend([v, u])
            dist.extend([r["length_m"]] * 2)
        self.A = coo_matrix((dist, (rows, cols)), shape=(len(self.nids), len(self.nids))).tocsr()
        self._last_source = None
        self._last_dist = None
        self._last_pred = None
        self.busy_tree = STRtree([r["geometry"] for r in self.busy]) if self.busy else None
        self.rail_tree = STRtree([r["geometry"] for r in self.rail]) if self.rail else None
        self.poi_tree = STRtree([r["point"] for r in self.pois]) if self.pois else None
        self.coastline_tree = (
            STRtree([r["geometry"] for r in self.coastlines]) if self.coastlines else None
        )
        self.coverage_bounds = manifest["bbox"]

    def project_hotel(self, h):
        return Point(*self.tr.transform(h["longitude"], h["latitude"]))

    def covered(self, h):
        a, b, c, d = self.coverage_bounds
        return a < h["longitude"] < c and b < h["latitude"] < d

    def snap(self, p, maxd=100):
        d, i = self.tree.query([p.x, p.y])
        return (int(i), float(d)) if d <= maxd else (None, float(d))

    def route(self, h, target, kind):
        p = self.project_hotel(h)
        s, sd = self.snap(p)
        q = target["point"]
        e, ed = self.snap(q)
        if s is None or e is None:
            return {
                "ok": False,
                "reason": "hotel_or_target_snap_over_100m",
                "hotel_snap_m": sd,
                "target_snap_m": ed,
                "target_id": target["sid"],
            }
        if self._last_source != s:
            self._last_dist, self._last_pred = dijkstra(
                self.A, directed=False, indices=s, return_predecessors=True, limit=180000
            )
            self._last_source = s
        ds, pred = self._last_dist, self._last_pred
        if not np.isfinite(ds[e]):
            return {
                "ok": False,
                "reason": "no_connected_path_in_cached_graph_not_proof_no_real_path",
                "hotel_snap_m": sd,
                "target_snap_m": ed,
                "target_id": target["sid"],
            }
        ids = [e]
        while ids[-1] != s:
            j = int(pred[ids[-1]])
            if j < 0:
                return {"ok": False, "reason": "predecessor_gap"}
            ids.append(j)
        ids.reverse()
        edges = [self.edges[(min(a, b), max(a, b))] for a, b in zip(ids, ids[1:], strict=False)]
        coord = [self.inv.transform(*self.xy[i]) for i in ids]
        rid = hashlib.sha256(
            json.dumps([h["hotel_id"], target["sid"], kind, self.nids[s], self.nids[e]]).encode()
        ).hexdigest()[:20]
        return {
            "ok": True,
            "route_id": rid,
            "target_id": target["sid"],
            "target_name": target["tags"].get("name"),
            "target_kind": kind,
            "graph_length_m": float(ds[e]),
            "length_estimate_m": float(ds[e]) + sd + ed,
            "hotel_snap_m": sd,
            "target_snap_m": ed,
            "node_ids": [self.nids[i] for i in ids],
            "coordinates": coord,
            "edges": edges,
            "target": target,
            "origin_verified_entrance": False,
            "target_verified_entrance": target.get("verified_entrance", False),
            "legal_access_inspected": False,
        }

    def choose(self, h, candidates, kind, max_candidates=5):
        # Evaluate all eligible candidate endpoint nodes against one shortest-path tree.
        # A fixed top-k by air distance can miss the nearest reachable path behind a river/fence.
        p = self.project_hotel(h)
        s, sd = self.snap(p)
        if s is None:
            return {
                "ok": False,
                "reason": "hotel_snap_over_100m",
                "hotel_snap_m": sd,
                "candidates_total": len(candidates),
            }
        if not candidates:
            return {"ok": False, "reason": "no_eligible_mapped_targets", "candidates_total": 0}
        if self._last_source != s:
            self._last_dist, self._last_pred = dijkstra(
                self.A, directed=False, indices=s, return_predecessors=True, limit=180000
            )
            self._last_source = s
        xy = np.asarray([[r["point"].x, r["point"].y] for r in candidates])
        gaps, inds = self.tree.query(xy)
        costs = self._last_dist[inds] + gaps + sd
        costs[gaps > 100] = np.inf
        i = int(np.argmin(costs))
        if not np.isfinite(costs[i]):
            return {
                "ok": False,
                "reason": "no_connected_eligible_candidate_in_cached_graph",
                "candidates_total": len(candidates),
                "endpoint_snap_over_100m": int(np.count_nonzero(gaps > 100)),
            }
        return self.route(h, candidates[i], kind)

    def beach_targets(self):
        out = []
        for b in self.beaches:
            classification = marine_beach_classification(
                b, self.coastlines, self.coastline_tree, self.manifest
            )
            classification.update(
                {
                    "beach_name": b["tags"].get("name"),
                    "beach_source": osm_source(b["record"], self.manifest),
                    "access": b["tags"].get("access"),
                    "graph_contact_candidates": 0,
                }
            )
            self.beach_classifications.append(classification)
            if not classification["accepted"] or b["tags"].get("access") in {
                "private",
                "no",
                "customers",
            }:
                continue
            geom = b["geometry"]
            center = geom.centroid
            radius = (
                max(
                    math.hypot(geom.bounds[2] - geom.bounds[0], geom.bounds[3] - geom.bounds[1])
                    / 2,
                    30,
                )
                + 30
            )
            candidates = self.tree.query_ball_point([center.x, center.y], radius)
            candidates = [i for i in candidates if geom.distance(Point(self.xy[i])) <= 25]
            classification["graph_contact_candidates"] = len(candidates)
            if not candidates:
                continue
            # All graph contacts are retained; endpoints remain candidates, not public entrances.
            for i in candidates:
                out.append(
                    {
                        **b,
                        "point": Point(self.xy[i]),
                        "sid": b["sid"] + f"#graph-node-{self.nids[i]}",
                        "beach_id": b["sid"],
                        "verified_entrance": False,
                        "beach_kind": "sea",
                        "marine_classification": classification,
                    }
                )
        return out


def steps_metrics(edges):
    n = 0
    active = False
    length = 0.0
    unknown = 0.0
    for e in edges:
        isstep = e["tags"].get("highway") == "steps"
        if isstep and not active:
            n += 1
        active = isstep
        if isstep:
            length += e["length_m"]
        else:
            unknown += e["length_m"]
    return {
        "known_step_sections": n,
        "mapped_steps_length_m": length,
        "non_step_edges_not_surveyed_for_unmapped_obstacles_m": unknown,
        "counting_convention": (
            "Consecutive steps edges are one section even across way IDs; not "
            "individual risers; zero does not establish step-free route."
        ),
    }


def lighting_metrics(edges):
    ds = collections.defaultdict(float)
    for e in edges:
        v = e["tags"].get("lit")
        key = "yes" if v == "yes" else "no" if v == "no" else "unknown"
        ds[key] += e["length_m"]
    total = sum(ds.values())
    return {
        "known_lit_percent": 100 * ds["yes"] / total if total else None,
        "known_unlit_percent": 100 * ds["no"] / total if total else None,
        "unknown_percent": 100 * ds["unknown"] / total if total else None,
        "total_graph_length_m": total,
        "date_kind": "OSM way edit date; working lamps not field inspected",
    }


def crossing_metrics(route, graph):
    if not graph.busy_tree:
        return {"known_crossings": 0, "crossings": [], "unknown_grade_and_type": 0}
    coords = route["coordinates"]
    line = (
        gtransform(graph.tr.transform, LineString(coords))
        if len(coords) > 1
        else Point(*graph.tr.transform(*coords[0]))
    )
    pts = []
    for i in graph.busy_tree.query(line):
        b = graph.busy[int(i)]
        inter = line.intersection(b["geometry"])
        gg = list(inter.geoms) if hasattr(inter, "geoms") else [inter]
        for p in gg:
            if p.geom_type != "Point":
                continue  # Shared road segments are not crossings.
            if any(p.distance(x["point"]) < 4 for x in pts):
                continue
            pts.append({"point": p, "road": b})
    out = []
    for r in pts:
        p = r["point"]
        b = r["road"]
        crossing = []
        at_grade_possible = False
        for e in route["edges"]:
            if e["geometry"].distance(p) > 3:
                continue
            t = e["tags"]
            bt = b["tags"]
            # Explicit unequal layers/bridge/tunnel do not form an at-grade crossing.
            if t.get("layer", "0") != bt.get("layer", "0") and any(
                t.get(k) or bt.get(k) for k in ["bridge", "tunnel"]
            ):
                continue
            at_grade_possible = True
            if (
                t.get("highway") == "crossing"
                or t.get("footway") == "crossing"
                or t.get("crossing")
            ):
                crossing.append(t.get("crossing", t.get("crossing:markings", "mapped_crossing")))
        if not at_grade_possible:
            continue
        for n in route["node_ids"]:
            q = graph.crossing_nodes.get(n)
            if q and q["point"].distance(p) < 4:
                crossing.append(q["tags"].get("crossing", "mapped_crossing_node"))
        out.append(
            {
                "road_id": b["sid"],
                "highway": b["tags"].get("highway"),
                "crossing_type": crossing[0] if crossing else "unknown",
                "coordinates": graph.inv.transform(p.x, p.y),
                "grade_inspected": False,
                "known_mapped_crossing": bool(crossing),
            }
        )
    return {
        "known_crossings": sum(r["known_mapped_crossing"] for r in out),
        "geometric_crossing_candidates": len(out),
        "crossings": out,
        "unknown_grade_and_type": sum(not r["known_mapped_crossing"] for r in out),
        "scope": (
            "Known mapped crossings and additional geometric candidates "
            "separated; explicit unequal bridge/tunnel layers excluded; "
            "missing grade/type preserved unknown; not safety certification."
        ),
    }
