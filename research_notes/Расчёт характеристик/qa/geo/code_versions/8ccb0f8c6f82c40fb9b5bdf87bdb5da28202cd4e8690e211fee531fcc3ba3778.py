#!/usr/bin/env python3
"""Read-only replay of omitted Sochi POI geometries against accepted facts.

Never calls geo.run(), writes canonical facts, or adds supplemental nodes to the
accepted graph. Replayed paths are labelled as matching only after exact route
identity and distance checks. New-node contacts are an independent audit.
"""
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

EXPECTED = 'e575c51e28cbc3905eadb4e4d628005b9fad99031301a741d0477c80e73473ad'

def main():
    start = time.perf_counter()
    if sha256(geo.__file__) != EXPECTED:
        raise RuntimeError('Accepted graph source changed')
    frozen = [BASE / 'results/geo' / ('sochi_' + suffix) for suffix in
              ['facts.jsonl', 'routes.jsonl', 'run.json', 'beach_targets.jsonl', 'beach_classifications.jsonl']]
    hashes_before = {str(path): sha256(path) for path in frozen}
    code = {}
    for path in [Path(__file__), Path(geo.__file__), BASE / 'scripts/common.py', BASE / 'scripts/geo_memory_containers.py']:
        digest = sha256(path)
        archive = BASE / 'qa/geo/code_versions' / (digest + '.py')
        if not archive.exists():
            archive.write_bytes(path.read_bytes())
        code[str(path)] = {'sha256': digest, 'exact_archive_file': str(archive)}
    hs = {hotel['hotel_id']: hotel for hotel in hotels('sochi')}
    facts = list(read_jsonl(BASE / 'results/geo/sochi_facts.jsonl'))
    saved = {row['properties']['route_id'] for row in read_jsonl(BASE / 'results/geo/sochi_routes.jsonl')}
    pending = {}
    for fact in facts:
        value = fact.get('value')
        if not isinstance(value, dict) or not value.get('route_id') or value['route_id'] in saved:
            continue
        if not value.get('poi_id'):
            continue
        pending.setdefault(value['route_id'], []).append(fact)
    original_records = geo.read_records
    def records(city):
        for record, manifest in original_records(city):
            yield compact_record(record), manifest
    geo.read_records = records
    geo.Graph = graph_class(geo.Graph)
    gc.disable()
    graph = geo.Graph('sochi')
    load_seconds = time.perf_counter() - start
    original_manifest = json.loads((BASE / 'cohort/osm_sochi_extended_manifest.json').read_text())
    if graph.manifest['sha256'] != original_manifest['sha256']:
        raise RuntimeError('Replay graph effective manifest differs from accepted extended input')
    pois = {poi['sid']: poi for poi in graph.pois}
    audit = json.loads((BASE / 'qa/geo/sochi_pure_node_readonly_audit.json').read_text())
    newly_blocked = {row['osm_id'] for row in audit['blocked_node_records']}
    output, checks, contacts = [], [], []
    # Group hotel routes to retain the accepted graph's one-source shortest-path cache.
    groups = sorted(pending.items(), key=lambda pair: (str(pair[1][0]['hotel_id']), pair[0]))
    for route_id, refs in groups:
        fact = refs[0]
        value = fact['value']
        poi = pois.get(value['poi_id'])
        kind = 'playground' if fact['feature_id'] == 'F01' else fact['feature_id']
        route = graph.route(hs[fact['hotel_id']], poi, kind) if poi else {'ok': False, 'reason': 'target_missing_from_replay_graph'}
        identity = bool(route.get('ok') and route['route_id'] == route_id)
        total_exact = bool(identity and all(route['length_estimate_m'] == ref['value']['distance_m'] for ref in refs))
        graph_exact = bool(identity and all('graph_length_m' not in ref['value'] or route['graph_length_m'] == ref['value']['graph_length_m'] for ref in refs))
        passed = identity and total_exact and graph_exact
        check = {'route_id': route_id, 'hotel_id': fact['hotel_id'], 'feature_id': fact['feature_id'], 'target_id': value['poi_id'],
                 'fact_ids': [ref['fact_id'] for ref in refs], 'route_identity_exact': identity,
                 'all_saved_total_distances_exact': total_exact, 'all_saved_graph_distances_exact': graph_exact,
                 'passed': passed}
        if not passed:
            check.update({'returned_ok': route.get('ok'), 'reason': route.get('reason'),
                          'returned_route_id': route.get('route_id'), 'returned_length_estimate_m': route.get('length_estimate_m'),
                          'stored_distance_m': value.get('distance_m')})
            checks.append(check)
            continue
        touched = sorted(newly_blocked.intersection(route['node_ids']))
        if touched:
            contacts.append({**check, 'newly_blocked_node_ids': touched, 'edge_way_ids': [edge['sid'] for edge in route['edges']]})
        output.append({'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': route['coordinates']} if len(route['coordinates']) > 1 else {'type': 'Point', 'coordinates': route['coordinates'][0]},
                       'properties': {**check, 'kind': kind, 'graph_length_m': route['graph_length_m'],
                                      'length_estimate_m': route['length_estimate_m'], 'hotel_snap_m': route['hotel_snap_m'], 'target_snap_m': route['target_snap_m'],
                                      'node_ids': route['node_ids'], 'edge_way_ids': [edge['sid'] for edge in route['edges']],
                                      'recipe_ids': sorted({ref['recipe_id'] for ref in refs}),
                                      'source': {'provider': 'OpenStreetMap', 'dataset_sha256': graph.manifest['sha256'],
                                                 'snapshot_file': str(BASE / 'cohort/osm_sochi_extended.jsonl'),
                                                 'dataset_version': graph.manifest.get('source_date'), 'license': 'ODbL1.0'},
                                      'replay_method_version': 'V9-Sochi-missing-POI-exact-replay-1',
                                      'replay_label': 'Reconstructed from unchanged accepted graph; route ID and every saved graph/total distance match exactly. Not an originally captured path.',
                                      'newly_blocked_node_contact_ids': touched,
                                      'steps_metrics': geo.steps_metrics(route['edges']), 'lighting_metrics': geo.lighting_metrics(route['edges'])}})
        checks.append(check)
    destination = BASE / 'qa/geo/sochi_additional_routes.jsonl'
    write_jsonl(destination, output)
    hashes_after = {str(path): sha256(path) for path in frozen}
    result = {'method_version': 'V9-Sochi-missing-POI-exact-replay-1', 'read_only_canonical_files': True,
              'canonical_files_sha256_before': hashes_before, 'canonical_files_sha256_after': hashes_after,
              'canonical_files_unchanged': hashes_before == hashes_after,
              'graph_source_sha256': EXPECTED, 'source_code_archives': code,
              'effective_input_manifest': graph.manifest, 'new_nodes_added_to_replay_graph': False,
              'original_saved_routes': len(saved), 'missing_unique_poi_routes': len(pending),
              'reconstructed_routes_with_exact_saved_id_and_lengths': len(output),
              'failed_replays': [check for check in checks if not check['passed']],
              'replay_checks': checks, 'newly_blocked_nodes_audited': sorted(newly_blocked),
              'additional_routes_with_new_node_contacts': len(contacts), 'new_node_contacts': contacts,
              'additional_routes_file': str(destination), 'additional_routes_sha256': sha256(destination),
              'additional_routes_bytes': destination.stat().st_size, 'graph_load_seconds': load_seconds,
              'total_seconds': time.perf_counter() - start,
              'scope': 'Original graph POI path replay and restriction-contact audit. No canonical facts, selected targets, accepted report, or checkpoint changed.'}
    (BASE / 'qa/geo/sochi_additional_routes_replay_audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print({key: val for key, val in result.items() if key not in {'replay_checks', 'source_code_archives', 'effective_input_manifest', 'canonical_files_sha256_before', 'canonical_files_sha256_after'}})
    assert hashes_before == hashes_after
    assert not result['failed_replays']

if __name__ == '__main__':
    main()
