#!/usr/bin/env python3
"""Read-only selected-POI delta with the omitted pure restriction nodes restored."""
import gc
import hashlib
import json
import sys
import time
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
from common import read_jsonl, write_jsonl, sha256, hotels
import geo
from geo_memory_containers import compact_record, graph_class

def main():
    start = time.perf_counter()
    replay = json.loads((BASE / 'qa/geo/sochi_additional_routes_replay_audit.json').read_text())
    canonical = {str(path): sha256(path) for path in (BASE / 'results/geo').glob('sochi_*') if path.is_file()}
    main_manifest = json.loads((BASE / 'cohort/osm_sochi_extended_manifest.json').read_text())
    extra_file = BASE / 'cohort/osm_sochi_pure_restriction_nodes.jsonl'
    extra_manifest = json.loads(extra_file.with_suffix('.manifest.json').read_text())
    inputs = [{'snapshot_file': str(BASE / 'cohort/osm_sochi_extended.jsonl'), 'sha256': main_manifest['sha256']},
              {'snapshot_file': str(extra_file), 'sha256': sha256(extra_file)}]
    composite = {**main_manifest, 'sha256': hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
                 'sha256_kind': 'composite SHA256 of ordered input path+SHA manifests, not file hash', 'input_files': inputs,
                 'pure_node_supplement': extra_manifest,
                 'source_pbf_sha256': sha256(extra_manifest['source_file'])}
    original = geo.read_records
    def records(city):
        for record, manifest in original(city):
            yield compact_record(record), composite
        for record in read_jsonl(extra_file):
            yield compact_record(record), composite
    geo.read_records = records
    geo.Graph = graph_class(geo.Graph)
    gc.disable()
    graph = geo.Graph('sochi')
    hs = {h['hotel_id']: h for h in hotels('sochi')}
    old_routes = {r['properties']['route_id']: r for r in read_jsonl(BASE / 'qa/geo/sochi_additional_routes.jsonl')}
    cases, output = [], []
    for contact in replay['new_node_contacts']:
        h = hs[contact['hotel_id']]
        if contact['feature_id'] != 'F01':
            raise RuntimeError('Unexpected POI feature; implement its exact candidate selection before use')
        pool = [r for r in graph.pois if r['tags'].get('leisure') == 'playground' and r['tags'].get('access') not in {'private', 'no', 'customers'}]
        new = graph.choose(h, pool, 'playground')
        old = old_routes[contact['route_id']]['properties']
        case = {'hotel_id': h['hotel_id'], 'hotel_name': h['name'], 'feature_id': contact['feature_id'],
                'fact_ids': contact['fact_ids'], 'old_route_id': contact['route_id'], 'old_target_id': contact['target_id'],
                'old_length_estimate_m': old['length_estimate_m'], 'old_graph_length_m': old['graph_length_m'],
                'omitted_blocked_node_ids': contact['newly_blocked_node_ids'], 'new_route_ok': new['ok']}
        if new['ok']:
            case.update({'new_route_id': new['route_id'], 'new_target_id': new['target_id'],
                         'new_length_estimate_m': new['length_estimate_m'], 'new_graph_length_m': new['graph_length_m'],
                         'new_hotel_connector_gap_m': new['hotel_snap_m'], 'new_target_connector_gap_m': new['target_snap_m'],
                         'distance_delta_m': new['length_estimate_m'] - old['length_estimate_m'],
                         'new_path_has_any_new_blocked_node': bool(set(new['node_ids']).intersection(replay['newly_blocked_nodes_audited']))})
            output.append({'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': new['coordinates']} if len(new['coordinates']) > 1 else {'type': 'Point', 'coordinates': new['coordinates'][0]},
                           'properties': {**case, 'node_ids': new['node_ids'], 'edge_way_ids': [edge['sid'] for edge in new['edges']],
                                          'recipe_id': 'F01-R2', 'effective_input_manifest': composite,
                                          'scope': 'Read-only candidate correction with restored explicit node restrictions; canonical Sochi facts not overwritten. Endpoints remain unverified.'}})
        else:
            case['new_route_failure_reason'] = new.get('reason')
        cases.append(case)
    path = BASE / 'qa/geo/sochi_restricted_poi_delta_routes.jsonl'
    write_jsonl(path, output)
    unchanged = all(sha256(path) == digest for path, digest in canonical.items())
    report = {'method_version': 'V9-Sochi-pure-node-POI-delta-1', 'canonical_files_unchanged': unchanged,
              'cases': cases, 'effective_input_manifest': composite, 'routes_file': str(path), 'routes_sha256': sha256(path),
              'source_code_sha256': {str(p): sha256(p) for p in [Path(__file__), Path(geo.__file__), BASE / 'scripts/geo_memory_containers.py']},
              'total_seconds': time.perf_counter() - start, 'network_requests': 0,
              'decision': 'Root must decide superseding checkpoint; this read-only audit does not update accepted facts.'}
    (BASE / 'qa/geo/sochi_restricted_poi_delta.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'effective_input_manifest'}, ensure_ascii=False, indent=2))
    assert unchanged

if __name__ == '__main__':
    main()
