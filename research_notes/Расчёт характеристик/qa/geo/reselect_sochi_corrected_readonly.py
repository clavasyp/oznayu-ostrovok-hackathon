#!/usr/bin/env python3
"""Full read-only Sochi re-selection plus old-endpoint snap audit.

Publish only QA sandbox outputs. Exact native point-state hours are cached to
avoid the optional unbounded next_change search; no characteristic selection
uses that diagnostic. Every canonical output is hashed before and after.
"""
import collections
import gc
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
REAL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REAL / 'scripts'))
from common import read_jsonl, write_jsonl, sha256, hotels
import geo
from geo_memory_containers import compact_record, graph_class
from geo_opening_point_cache import evaluate

def semantic_value(value):
    if isinstance(value, dict):
        return {k: semantic_value(v) for k, v in value.items() if k not in {'next_change', 'next_change_reason', 'evaluation_method_version'}}
    if isinstance(value, list):
        return [semantic_value(v) for v in value]
    return value

def key(row):
    params = {k: v for k, v in row['params'].items() if k != 'quality_exclusion_evidence'}
    # Route parameters identify the dependent target, so retain those in values
    # comparison, while matching source observations by feature/trip/subject kind.
    if row['feature_id'] in {'W04', 'W05', 'W06', 'A01', 'Q03'}:
        params = {k: v for k, v in params.items() if k not in {'route_id', 'target_id', 'target_kind'}}
        subject = row['subject_level']
    else:
        subject = row['subject_id']
    return json.dumps([row['hotel_id'], row['feature_id'], subject, params], ensure_ascii=False, sort_keys=True)

def main():
    start = time.perf_counter()
    canonical = {str(path): sha256(path) for path in (REAL / 'results/geo').glob('sochi_*') if path.is_file()}
    mainfile = REAL / 'cohort/osm_sochi_extended.jsonl'
    original_manifest = json.loads((REAL / 'cohort/osm_sochi_extended_manifest.json').read_text())
    extra = REAL / 'cohort/osm_sochi_pure_restriction_nodes.jsonl'
    supplement = json.loads(extra.with_suffix('.manifest.json').read_text())
    inputs = [{'snapshot_file': str(mainfile), 'sha256': original_manifest['sha256']}, {'snapshot_file': str(extra), 'sha256': sha256(extra)}]
    composite = {**original_manifest, 'input_files': inputs,
                 'sha256': hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
                 'sha256_kind': 'composite SHA256 of ordered input path+SHA manifests, not file hash',
                 'pure_node_supplement': supplement, 'source_pbf_sha256': sha256(supplement['source_file'])}
    blocked = {row['osm_id'] for row in read_jsonl(extra) if geo.blocked_pedestrian_node(row['tags'])}
    def original_records(city):
        seen = set()
        for record in read_jsonl(mainfile):
            identity = (geo.osm_id(record), bool(record.get('nodes')))
            if identity not in seen:
                seen.add(identity)
                yield compact_record(record), original_manifest
    geo.read_records = original_records
    geo.Graph = graph_class(geo.Graph)
    gc.disable()
    original_graph = geo.Graph('sochi')
    affected, totals = [], collections.Counter()
    def inspect(pool, target_id, point, tags):
        totals[pool] += 1
        index, distance = original_graph.snap(point)
        node = original_graph.nids[index] if index is not None else None
        if node in blocked:
            affected.append({'pool': pool, 'target_id': target_id, 'old_snap_node': node, 'old_snap_distance_m': distance,
                             'tags': tags, 'point_projected': [point.x, point.y]})
    for h in hotels('sochi'):
        inspect('hotel_origin', h['hotel_id'], original_graph.project_hotel(h), {})
    for pool, records in [('all_POIs_including_unselected_candidates', original_graph.pois), ('all_transport_stops', original_graph.stops), ('all_marine_graph_contacts', original_graph.beach_targets())]:
        for record in records:
            inspect(pool, record['sid'], record['point'], record['tags'])
    nodes_actually_removed = sorted(blocked.intersection(original_graph.nids))
    old_audit_seconds = time.perf_counter() - start
    del original_graph
    gc.collect()
    def corrected_records(city):
        for record, manifest in original_records(city):
            yield record, composite
        for record in read_jsonl(extra):
            yield compact_record(record), composite
    geo.read_records = corrected_records
    sandbox = REAL / 'qa/geo/sochi_corrected_readonly'
    (sandbox / 'results').mkdir(parents=True, exist_ok=True)
    (sandbox / 'qa/geo').mkdir(parents=True, exist_ok=True)
    facade_link = sandbox / 'results/facades'
    if not facade_link.exists():
        facade_link.symlink_to(REAL / 'results/facades', target_is_directory=True)
    shutil.copy2(REAL / 'qa/geo/source_quality_exclusions.json', sandbox / 'qa/geo/source_quality_exclusions.json')
    geo.BASE = sandbox
    geo.opening_state = lambda poi, date, city, kitchen=False: evaluate(poi, date, city, kitchen, timezones=geo.TZ, countries=geo.COUNTRY)
    original_route = geo.Graph.route
    all_routes = {}
    def route(self, hotel, target, kind):
        result = original_route(self, hotel, target, kind)
        if result.get('ok'):
            all_routes[result['route_id']] = {'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': result['coordinates']} if len(result['coordinates']) > 1 else {'type': 'Point', 'coordinates': result['coordinates'][0]},
                                             'properties': {'route_id': result['route_id'], 'hotel_id': hotel['hotel_id'], 'kind': kind,
                                                            'target_id': result['target_id'], 'graph_length_m': result['graph_length_m'],
                                                            'length_estimate_m': result['length_estimate_m'], 'hotel_snap_m': result['hotel_snap_m'], 'target_snap_m': result['target_snap_m'],
                                                            'node_ids': result['node_ids'], 'edge_way_ids': [edge['sid'] for edge in result['edges']],
                                                            'source': {'provider': 'OpenStreetMap', 'composite_dataset_sha256': composite['sha256'], 'input_files': inputs, 'license': 'ODbL1.0'}}}
        return result
    geo.Graph.route = route
    selection_start = time.perf_counter()
    run = geo.run('sochi')
    reselect_seconds = time.perf_counter() - selection_start
    write_jsonl(sandbox / 'results/geo/sochi_all_selected_routes.jsonl', all_routes.values())
    corrected = {key(row): row for row in read_jsonl(sandbox / 'results/geo/sochi_facts.jsonl')}
    current = {key(row): row for row in read_jsonl(REAL / 'results/geo/sochi_facts.jsonl')}
    before = {key(row): row for row in read_jsonl(REAL / 'checkpoints/sochi_before_v9_3_F01_node_repair/sochi_facts.jsonl')}
    def compare(left):
        differences = []
        for identity in left.keys() | corrected.keys():
            old, new = left.get(identity), corrected.get(identity)
            if old is None or new is None:
                differences.append({'kind': 'row_presence_difference', 'key': identity, 'old': old, 'new': new})
                continue
            ov, nv = semantic_value(old.get('value')), semantic_value(new.get('value'))
            # F01 repair adds explicit diagnostics not present in the baseline
            # characteristic; compare its selected route/target/total value.
            if old['feature_id'] == 'F01' and isinstance(ov, dict) and isinstance(nv, dict):
                ov = {k: v for k, v in ov.items() if k not in {'graph_length_m', 'hotel_connector_gap_m', 'target_connector_gap_m'}}
                nv = {k: v for k, v in nv.items() if k not in {'graph_length_m', 'hotel_connector_gap_m', 'target_connector_gap_m'}}
            if old['status'] != new['status'] or ov != nv or old.get('unknown_reason') != new.get('unknown_reason'):
                differences.append({'hotel_id': old['hotel_id'], 'feature_id': old['feature_id'], 'params': old['params'], 'old_fact_id': old['fact_id'],
                                    'old_status': old['status'], 'new_status': new['status'], 'old_value': ov, 'new_value': nv,
                                    'old_unknown_reason': old.get('unknown_reason'), 'new_unknown_reason': new.get('unknown_reason')})
        return differences
    against_before, against_current = compare(before), compare(current)
    new_contacts = [{'route_id': rid, 'blocked_nodes': sorted(blocked.intersection(row['properties']['node_ids']))} for rid, row in all_routes.items() if blocked.intersection(row['properties']['node_ids'])]
    unchanged = all(sha256(path) == digest for path, digest in canonical.items())
    report = {'version': 'V9.3-Sochi-full-corrected-reselect-1', 'canonical_files_unchanged': unchanged,
              'old_snap_audit_pools_counts': dict(totals), 'new_blocked_nodes': sorted(blocked), 'removed_nodes_present_in_original_graph': nodes_actually_removed,
              'old_snap_affected_origins_or_candidates': affected, 'old_graph_and_snap_audit_seconds': old_audit_seconds,
              'all_89_hotels_corrected_full_reselect_seconds': reselect_seconds, 'corrected_full_run': run,
              'semantic_deltas_against_pre_repair_checkpoint': against_before, 'semantic_deltas_against_current_repaired_facts': against_current,
              'corrected_selected_routes': len(all_routes), 'corrected_routes_with_blocked_node_contacts': new_contacts,
              'comparison_scope': 'All 29 features/all89hotels/two requested periods; source hashes, calculated time, absolute quality-evidence sandbox path and optional next_change diagnostic are excluded. All actual characteristic values/status/reasons compared.',
              'network_requests': 0, 'total_seconds': time.perf_counter() - start,
              'source_sha256': {str(p): sha256(p) for p in [Path(__file__), REAL / 'scripts/geo.py', REAL / 'scripts/geo_memory_containers.py', REAL / 'scripts/geo_opening_point_cache.py']}}
    (REAL / 'qa/geo/sochi_full_corrected_reselect_audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in {'corrected_full_run', 'semantic_deltas_against_pre_repair_checkpoint', 'source_sha256'}}, ensure_ascii=False, indent=2))
    assert unchanged and not new_contacts

if __name__ == '__main__':
    main()
