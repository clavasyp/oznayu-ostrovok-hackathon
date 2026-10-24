#!/usr/bin/env python3
"""Attach final read-only full-reselection proof to the narrow Sochi repair."""
import json
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
from common import read_jsonl, sha256

def main():
    currentfile = BASE / 'results/geo/sochi_all_selected_routes.jsonl'
    correctedfile = BASE / 'qa/geo/sochi_corrected_readonly/results/geo/sochi_all_selected_routes.jsonl'
    current = {r['properties']['route_id']: r for r in read_jsonl(currentfile)}
    corrected = {r['properties']['route_id']: r for r in read_jsonl(correctedfile)}
    differences = []
    for rid in current.keys() | corrected.keys():
        a, b = current.get(rid), corrected.get(rid)
        if a is None or b is None:
            differences.append({'route_id': rid, 'issue': 'route_presence'})
            continue
        issues = [field for field in ['node_ids', 'edge_way_ids', 'graph_length_m'] if a['properties'][field] != b['properties'][field]]
        if a['geometry'] != b['geometry']:
            issues.append('geometry')
        if issues:
            differences.append({'route_id': rid, 'issues': issues})
    auditfile = BASE / 'qa/geo/sochi_full_corrected_reselect_audit.json'
    audit = json.loads(auditfile.read_text())
    audit['final_published_trace_equivalence'] = {'current_index_file': str(currentfile), 'current_index_sha256': sha256(currentfile),
                                                'corrected_full_reselection_file': str(correctedfile), 'corrected_full_reselection_sha256': sha256(correctedfile),
                                                'route_count': len(current), 'compared': ['exact route IDs', 'node_ids', 'edge_way_ids', 'graph_length_m', 'geometry'],
                                                'differences': differences, 'passed': not differences and len(current) == 652}
    audit['passed'] &= audit['final_published_trace_equivalence']['passed']
    auditfile.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    assert audit['passed'] and not audit['semantic_deltas_against_current_repaired_facts']
    digest = sha256(__file__)
    archive = BASE / 'qa/geo/code_versions' / (digest + '.py')
    if not archive.exists():
        archive.write_bytes(Path(__file__).read_bytes())
    runfile = BASE / 'results/geo/sochi_run.json'
    run = json.loads(runfile.read_text())
    run['repair_phases'][-1]['full_reselection_validation'] = {'audit_file': str(auditfile), 'sha256': sha256(auditfile),
                                                            'all_89_hotels_all_29_features_reselected': True,
                                                            'full_corrected_reselection_seconds': audit['all_89_hotels_corrected_full_reselect_seconds'],
                                                            'old_graph_and_all_candidate_snap_audit_seconds': audit['old_graph_and_snap_audit_seconds'],
                                                            'total_readonly_validation_seconds': audit['total_seconds'],
                                                            'additional_characteristic_deltas_after_two_F01_repairs': 0,
                                                            'all_652_trace_geometries_node_ids_edge_ids_graph_lengths_exact': True,
                                                            'all_origins_and_candidates_snap_audited': audit['old_snap_audit_pools_counts'],
                                                            'removed_unselected_beach_contact_diagnostic': audit['diagnostic_delta'],
                                                            'source_code_sha256': digest, 'exact_archive_file': str(archive)}
    runfile.write_text(json.dumps(run, ensure_ascii=False, indent=2) + '\n')
    reportfile = BASE / 'qa/geo/sochi_F01_node_repair_report.json'
    report = json.loads(reportfile.read_text())
    report['run_sha256'] = sha256(runfile)
    report['full_corrected_reselection_validation'] = run['repair_phases'][-1]['full_reselection_validation']
    report['passed'] &= audit['passed']
    reportfile.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'facts_sha256': report['facts_sha256'], 'final_run_sha256': report['run_sha256'],
                      'trace_index_sha256': sha256(currentfile), 'full_validation_audit_sha256': sha256(auditfile),
                      'all_652_routes_exact': not differences, 'additional_semantic_deltas': len(audit['semantic_deltas_against_current_repaired_facts'])}, ensure_ascii=False))

if __name__ == '__main__':
    main()
