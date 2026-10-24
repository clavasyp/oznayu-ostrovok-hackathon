#!/usr/bin/env python3
"""Supersede exactly two F01 passports after root-authorized node restriction audit.

Original complete calculation and its runtime remain historical. This repair
uses the separately measured corrected-graph delta and publishes a complete
trace index without recomputing unrelated characteristic values.
"""
import copy
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
from common import read_jsonl, write_jsonl, sha256, now

def main():
    start = time.perf_counter()
    version = 'V9.3-Sochi-F01-pure-node-repair-1'
    checkpoint = BASE / 'checkpoints/sochi_before_v9_3_F01_node_repair'
    if checkpoint.exists():
        raise RuntimeError('Repair checkpoint already exists; refuse replay without explicit review')
    checkpoint.mkdir(parents=True)
    originals = sorted(path for path in (BASE / 'results/geo').glob('sochi_*') if path.is_file())
    checkpoint_rows = []
    for path in originals:
        saved = checkpoint / path.name
        shutil.copy2(path, saved)
        checkpoint_rows.append({'original_file': str(path), 'checkpoint_file': str(saved), 'sha256': sha256(saved), 'bytes': saved.stat().st_size})
    (checkpoint / 'manifest.json').write_text(json.dumps({'created_at': now(), 'reason': 'Before authorized V9.3 correction of two F01 rows after omitted explicit access=no node audit.', 'files': checkpoint_rows}, ensure_ascii=False, indent=2) + '\n')
    delta = json.loads((BASE / 'qa/geo/sochi_restricted_poi_delta.json').read_text())
    replay = json.loads((BASE / 'qa/geo/sochi_additional_routes_replay_audit.json').read_text())
    if len(delta['cases']) != 1 or not delta['canonical_files_unchanged']:
        raise RuntimeError('Unexpected delta scope')
    case = delta['cases'][0]
    if case['hotel_id'] != '10595288' or case['feature_id'] != 'F01':
        raise RuntimeError('Unexpected hotel-feature repair target')
    code_files = [Path(__file__), BASE / 'qa/geo/audit_sochi_restricted_poi_delta.py', BASE / 'qa/geo/restore_sochi_poi_routes.py', BASE / 'scripts/geo.py', BASE / 'scripts/common.py', BASE / 'scripts/geo_memory_containers.py']
    code = {}
    for path in code_files:
        digest = sha256(path)
        archive = BASE / 'qa/geo/code_versions' / (digest + '.py')
        if not archive.exists():
            archive.write_bytes(path.read_bytes())
        code[str(path)] = {'sha256': digest, 'exact_archive_file': str(archive)}
    runfile = BASE / 'results/geo/sochi_run.json'
    run = json.loads(runfile.read_text())
    baseline_manifest = run['input_manifest']
    index = {}
    for route in read_jsonl(BASE / 'results/geo/sochi_routes.jsonl'):
        route = copy.deepcopy(route)
        props = route['properties']
        props.update({'source': {'provider': 'OpenStreetMap', 'dataset_sha256': baseline_manifest['sha256'],
                                 'snapshot_file': str(BASE / 'cohort/osm_sochi_extended.jsonl'),
                                 'dataset_version': baseline_manifest.get('source_date'), 'license': 'ODbL1.0'},
                      'trace_origin': 'Original accepted run captured path; no new blocked-node contact.'})
        props['recipe_id'] = {'beach': 'W01-R2', 'stop': 'W02-R2', 'center': 'W03-R2'}.get(props['kind'])
        if props['kind'] == 'mapped_dog_area_candidate':
            props['component_only_not_canonical_P03'] = True
        index[props['route_id']] = route
    for route in read_jsonl(BASE / 'qa/geo/sochi_additional_routes.jsonl'):
        if route['properties']['route_id'] == case['old_route_id']:
            continue
        route = copy.deepcopy(route)
        route['properties']['trace_origin'] = 'Exact replay of accepted graph; ID and saved total/graph distances match; no new blocked-node contact.'
        index[route['properties']['route_id']] = route
    for route in read_jsonl(BASE / 'qa/geo/sochi_restricted_poi_delta_routes.jsonl'):
        route = copy.deepcopy(route)
        route['properties'].update({'route_id': case['new_route_id'], 'target_id': case['new_target_id'],
                                    'graph_length_m': case['new_graph_length_m'], 'length_estimate_m': case['new_length_estimate_m'],
                                    'source': {'provider': 'OpenStreetMap', 'composite_dataset_sha256': delta['effective_input_manifest']['sha256'],
                                               'input_files': delta['effective_input_manifest']['input_files'], 'license': 'ODbL1.0'},
                                    'trace_origin': 'Exact returned path from corrected graph with pure restriction supplement; V9.3 F01 repair.',
                                    'capture_method_version': version, 'kind': 'playground'})
        index[case['new_route_id']] = route
    blocked = set(replay['newly_blocked_nodes_audited'])
    bad = [{'route_id': rid, 'contact_nodes': sorted(blocked.intersection(row['properties']['node_ids']))} for rid, row in index.items() if blocked.intersection(row['properties']['node_ids'])]
    if bad:
        raise RuntimeError('Final trace index still contains forbidden node contacts: ' + str(bad))
    tracefile = BASE / 'results/geo/sochi_all_selected_routes.jsonl'
    write_jsonl(tracefile, index.values())
    trace_sha = sha256(tracefile)
    factsfile = BASE / 'results/geo/sochi_facts.jsonl'
    old_lines = factsfile.read_text().splitlines(keepends=True)
    new_lines, changes = [], []
    targets = set(case['fact_ids'])
    for line in old_lines:
        row = json.loads(line)
        if row['fact_id'] not in targets:
            new_lines.append(line)  # Preserve every unrelated passport byte-for-byte.
            continue
        old = copy.deepcopy(row)
        if row['hotel_id'] != '10595288' or row['feature_id'] != 'F01' or row['value']['route_id'] != case['old_route_id']:
            raise RuntimeError('Passport identity mismatch')
        if case['new_route_ok']:
            row['value'].update({'distance_m': case['new_length_estimate_m'], 'route_id': case['new_route_id'],
                                 'poi_id': case['new_target_id'], 'graph_length_m': case['new_graph_length_m'],
                                 'hotel_connector_gap_m': case['new_hotel_connector_gap_m'],
                                 'target_connector_gap_m': case['new_target_connector_gap_m']})
            row['confidence'] = {'level': 'low', 'reason': 'Corrected OSM graph excludes all three omitted explicit restricted nodes. Same mapped playground; endpoint re-snapping changes the estimate. Hotel/target connector gaps and public entrance remain unverified.'}
        else:
            row.update({'status': 'unknown', 'value': None, 'recipe_id': None, 'unknown_reason': case['new_route_failure_reason']})
        row['algorithm_version'] = 'v9.3-F01-node-repair'
        row['calculated_at'] = now()
        row['sources'].append({'provider': 'OpenStreetMap', 'snapshot_file': str(BASE / 'cohort/osm_sochi_pure_restriction_nodes.jsonl'),
                               'dataset_sha256': delta['effective_input_manifest']['pure_node_supplement']['sha256'],
                               'source_pbf_sha256': delta['effective_input_manifest']['source_pbf_sha256'],
                               'dataset_version': delta['effective_input_manifest'].get('source_date'),
                               'url': 'https://www.openstreetmap.org/node/655271566', 'license': 'ODbL1.0',
                               'scope': 'Pure restriction nodes from the same cached PBF; complete effective graph input in depends_on.'})
        row['depends_on'] += [{'kind': 'effective_input_manifest', 'file': str(runfile), 'phase': version,
                               'sha256': delta['effective_input_manifest']['sha256'], 'sha256_kind': delta['effective_input_manifest']['sha256_kind'],
                               'node_supplement_sha256': delta['effective_input_manifest']['pure_node_supplement']['sha256']},
                              {'kind': 'route_trace_index', 'file': str(tracefile), 'sha256': trace_sha, 'route_id': case.get('new_route_id')}]
        row['repair'] = {'version': version, 'superseded_route_id': case['old_route_id'], 'excluded_contact_node': 655271566,
                         'previous_checkpoint': str(checkpoint), 'original_calculated_at': old['calculated_at']}
        new_lines.append(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
        changes.append({'fact_id': row['fact_id'], 'hotel_id': row['hotel_id'], 'feature_id': row['feature_id'], 'params': row['params'],
                        'old_value': old['value'], 'new_value': row['value'], 'old_status': old['status'], 'new_status': row['status']})
    if len(changes) != 2:
        raise RuntimeError('Expected exactly two seasonal F01 passports')
    factsfile.write_text(''.join(new_lines))
    unrelated_exact = all(a == b for a, b in zip(old_lines, new_lines) if json.loads(a)['fact_id'] not in targets)
    known_route_refs = {row['value']['route_id'] for row in read_jsonl(factsfile) if isinstance(row.get('value'), dict) and row['value'].get('route_id')}
    if not known_route_refs.issubset(index):
        raise RuntimeError('A selected known route has no trace in the final index')
    unchanged_other_files = all(sha256(row['original_file']) == row['sha256'] for row in checkpoint_rows if Path(row['original_file']).name not in {'sochi_facts.jsonl', 'sochi_run.json'})
    phase = {'version': version, 'repaired_at': now(), 'changed_passport_count': len(changes), 'changed_values': changes,
             'corrected_graph_effective_input_manifest': delta['effective_input_manifest'], 'code_versions': code,
             'original_run_total_seconds_preserved': run['total_seconds'], 'original_run_time_scope': 'Historical full accepted run before pure-node repair; not a rerun timing.',
             'corrected_graph_delta_calculation_seconds': delta['total_seconds'], 'repair_publication_seconds': time.perf_counter() - start,
             'repair_timing_scope': 'Separate measured corrected-graph candidate calculation plus checkpoint/passport/trace publication; no claim of a new full Sochi calculation.',
             'checkpoint': str(checkpoint), 'checkpoint_manifest_sha256': sha256(checkpoint / 'manifest.json'),
             'unrelated_passports_byte_identical': unrelated_exact, 'other_original_geo_files_unchanged': unchanged_other_files,
             'unaffected_replayed_POI_routes': 316, 'unaffected_original_captured_routes': 335,
             'final_trace_count': len(index), 'final_all_traces_new_restricted_node_contacts': bad,
             'replay_audit': str(BASE / 'qa/geo/sochi_additional_routes_replay_audit.json'),
             'corrected_graph_delta': str(BASE / 'qa/geo/sochi_restricted_poi_delta.json'), 'network_requests': 0}
    run['repair_phases'] = [*run.get('repair_phases', []), phase]
    run['initial_calculation_runtime_note'] = 'total_seconds and family_seconds describe original accepted full run. See repair_phases for the separate measured V9.3 repair.'
    run['all_selected_routes'] = {'file': str(tracefile), 'sha256': trace_sha, 'routes': len(index),
                                  'capture_method_version': 'V9.3-original-plus-exact-replay-plus-corrected-F01-index',
                                  'source_scope': '335 original traces +316 exact accepted graph replays +1 corrected graph route. Per-route source manifests retained.'}
    run['result_bytes_before_repair'] = run['result_bytes']
    run['result_bytes'] = sum(path.stat().st_size for path in (BASE / 'results/geo').glob('sochi_*jsonl'))
    runfile.write_text(json.dumps(run, ensure_ascii=False, indent=2) + '\n')
    report = {'version': version, 'checkpoint': str(checkpoint), 'changed_facts': changes,
              'facts_sha256': sha256(factsfile), 'run_sha256': sha256(runfile),
              'trace_index_sha256': trace_sha, 'trace_count': len(index),
              'unrelated_passports_byte_identical': unrelated_exact, 'other_original_geo_files_unchanged': unchanged_other_files,
              'final_combined_route_restriction_contacts': bad, 'known_route_references_all_saved': known_route_refs.issubset(index),
              'code_versions': code, 'total_publication_seconds': time.perf_counter() - start,
              'passed': unrelated_exact and unchanged_other_files and not bad}
    (BASE / 'qa/geo/sochi_F01_node_repair_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'code_versions'}, ensure_ascii=False, indent=2))
    assert report['passed']

if __name__ == '__main__':
    main()
