#!/usr/bin/env python3
"""Summarize actual measured outputs and archived-source provenance, not accuracy."""
import collections
import json
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
from common import read_jsonl, sha256
import geo

def main():
    queue = json.loads((BASE / 'qa/geo/hardware_queue_progress.json').read_text())
    stages = {row['city_id']: row['stage_seconds'] for row in queue['completed']}
    rows = []
    for city in ['moscow', 'spb', 'kazan', 'kaliningrad', 'minsk', 'istanbul', 'dubai']:
        runfile = BASE / 'results/geo' / f'{city}_run.json'
        run = json.loads(runfile.read_text())
        check = json.loads((BASE / 'qa/geo' / f'{city}_contract_checks.json').read_text())
        factsfile = BASE / 'results/geo' / f'{city}_facts.jsonl'
        facts = list(read_jsonl(factsfile))
        source_hashes = {'accepted_geo': run['accepted_calculator_sha256'], 'runner': run['runner_sha256'],
                         'memory': run['memory_representation']['sha256'], 'hours': run['hours_evaluation_extension']['sha256']}
        archive_checks = {name: sha256(BASE / 'qa/geo/code_versions' / (digest + '.py')) == digest for name, digest in source_hashes.items()}
        routefile = BASE / 'results/geo' / f'{city}_all_selected_routes.jsonl'
        routes = list(read_jsonl(routefile))
        integer_nodes = all(type(node) is int for route in routes for node in route['properties']['node_ids'])
        traces = all(route['properties']['source']['composite_dataset_sha256'] == run['input_manifest']['sha256'] for route in routes)
        features = {}
        for fid in geo.OWN:
            fs = [fact for fact in facts if fact['feature_id'] == fid]
            features[fid] = {'rows_by_status': dict(collections.Counter(fact['status'] for fact in fs)),
                             'hotel_denominator': 50,
                             'hotels_with_any_row_of_status': {status: len({fact['hotel_id'] for fact in fs if fact['status'] == status}) for status in sorted({fact['status'] for fact in fs})},
                             'warning': 'Hotel can have different statuses across scopes/parameters. Any estimated/computed row does not establish all-parameter coverage.'}
        marinefile = BASE / 'results/geo' / f'{city}_marine_targets_with_fallback.jsonl'
        row = {'city_id': city, 'hotels': run['hotels'], 'owned_feature_count': 29,
               'unique_hotel_feature_pairs': run['unique_hotel_feature_pairs'], 'fact_rows': len(facts),
               'statuses': dict(collections.Counter(fact['status'] for fact in facts)), 'feature_counts': features,
               'accepted_calculator_seconds': run['total_seconds'], 'graph_load_seconds': run['load_seconds'],
               'family_seconds': run['family_seconds'],
               'full_geo_pipeline_seconds': run['pipeline_seconds_including_capture_and_auxiliary_serialization'],
               'node_input_extraction_seconds': run['extra_node_input']['seconds'],
               'hardware_queue_stage_seconds': stages.get(city),
               'runtime_scope': 'Actual successful final run. Failed/replaced attempts are disclosed separately; wrapper includes source PBF hash/capture/serialization. Facade and input extraction stages are separate.',
               'graph_nodes': run['graph_nodes'], 'graph_edges': run['graph_edges'], 'blocked_nodes': run['blocked_pedestrian_nodes'],
               'result_bytes_current_all_city_jsonl': sum(path.stat().st_size for path in (BASE / 'results/geo').glob(city + '_*jsonl')),
               'facts_bytes': factsfile.stat().st_size, 'facts_sha256': sha256(factsfile),
               'selected_routes': len(routes), 'selected_routes_bytes': routefile.stat().st_size, 'selected_routes_sha256': sha256(routefile),
               'marine_targets_sha256': sha256(marinefile), 'source_hashes': source_hashes,
               'exact_source_archives_verified': archive_checks, 'all_route_node_ids_native_int': integer_nodes,
               'all_route_sources_match_effective_input_manifest': traces,
               'contract_checks_passed': check['passed'], 'contract_checks_count': len(check['checks']),
               'effective_input_manifest': run['input_manifest'], 'run_file': str(runfile), 'run_sha256': sha256(runfile)}
        if run.get('repair_phases'):
            row['repair_phases'] = run['repair_phases']
            row['runtime_scope_after_targeted_repairs'] = 'Full geo pipeline seconds remain the original successful full run; targeted repair phases have separately measured times and preserve unrelated values/routes.'
        if city == 'dubai':
            row['replacement_run_reason'] = 'V1 unsupported AE parser country prevented all declared hours states. V2 retries only expressions without PH/SH with country=None, retaining Asia/Dubai; PH/SH unknown.'
            row['hardware_queue_stage_seconds_note'] = 'Queue geo stage is preserved v1. Final AE-corrected geo timing is full_geo_pipeline_seconds; facade/node stages unchanged.'
        rows.append(row)
    result = {'cities': rows, 'hotels': sum(row['hotels'] for row in rows),
              'unique_hotel_feature_pairs': sum(row['unique_hotel_feature_pairs'] for row in rows),
              'passed': all(row['contract_checks_passed'] and all(row['exact_source_archives_verified'].values()) and row['all_route_node_ids_native_int'] and row['all_route_sources_match_effective_input_manifest'] for row in rows),
              'network_requests': 0,
              'scope': 'Seven-city deterministic output/provenance/contracts. Not empirical accuracy. Sochi accepted checkpoint and its supplemental read-only POI audit are separate.',
              'source_capture_note': 'Original V1 files ab27e873/a0b00092/101779db remained unchanged throughout all original seven city subprocesses; exact bytes archived before AE v2 work. V1 recorded end SHA; no concurrent edit occurred. Dubai V2 captures/archives code before imports and checks unchanged at end.',
              'failed_and_replaced_attempts': ['moscow_initial_attempt.json', 'moscow_gc_disabled_attempt.json', 'moscow_compact_pre_hours_attempt.json', 'dubai_before_AE_fix/manifest.json']}
    destination = BASE / 'qa/geo/seven_city_summary.json'
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'hotels': result['hotels'], 'unique_hotel_feature_pairs': result['unique_hotel_feature_pairs'], 'passed': result['passed'], 'rows': [{key: row[key] for key in ['city_id', 'fact_rows', 'statuses', 'full_geo_pipeline_seconds', 'result_bytes_current_all_city_jsonl', 'selected_routes', 'contract_checks_passed']} for row in rows]}, ensure_ascii=False, indent=2))
    assert result['passed']

if __name__ == '__main__':
    main()
