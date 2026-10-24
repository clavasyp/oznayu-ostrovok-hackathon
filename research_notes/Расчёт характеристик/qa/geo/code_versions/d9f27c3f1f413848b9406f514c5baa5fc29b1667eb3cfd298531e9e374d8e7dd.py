#!/usr/bin/env python3
"""Separate characteristic deltas from nested source-manifest diagnostics."""
import json
import shutil
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
from common import read_jsonl, sha256
from reselect_sochi_corrected_readonly import key

def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in {'next_change', 'next_change_reason', 'evaluation_method_version',
                                                                    'dataset_sha256', 'source_pbf_sha256', 'composite_dataset_sha256', 'input_files', 'graph_contact_candidates'}}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value

def main():
    reportfile = BASE / 'qa/geo/sochi_full_corrected_reselect_audit.json'
    raw = reportfile.with_name('sochi_full_corrected_reselect_audit_including_provenance_differences.json')
    if not raw.exists():
        shutil.copy2(reportfile, raw)
    report = json.loads(raw.read_text())
    corrected = {key(row): row for row in read_jsonl(BASE / 'qa/geo/sochi_corrected_readonly/results/geo/sochi_facts.jsonl')}
    def compare(path):
        original = {key(row): row for row in read_jsonl(path)}
        changes = []
        for identity in original.keys() | corrected.keys():
            old, new = original.get(identity), corrected.get(identity)
            if old is None or new is None:
                changes.append({'kind': 'row_presence_difference', 'key': identity, 'old': old, 'new': new})
                continue
            ov, nv = normalized(old.get('value')), normalized(new.get('value'))
            if old['feature_id'] == 'F01' and isinstance(ov, dict) and isinstance(nv, dict):
                ov = {k: v for k, v in ov.items() if k not in {'graph_length_m', 'hotel_connector_gap_m', 'target_connector_gap_m'}}
                nv = {k: v for k, v in nv.items() if k not in {'graph_length_m', 'hotel_connector_gap_m', 'target_connector_gap_m'}}
            if old['status'] != new['status'] or ov != nv or old.get('unknown_reason') != new.get('unknown_reason'):
                changes.append({'hotel_id': old['hotel_id'], 'feature_id': old['feature_id'], 'params': old['params'], 'old_fact_id': old['fact_id'],
                                'old_status': old['status'], 'new_status': new['status'], 'old_value': ov, 'new_value': nv,
                                'old_unknown_reason': old.get('unknown_reason'), 'new_unknown_reason': new.get('unknown_reason')})
        return changes
    before = compare(BASE / 'checkpoints/sochi_before_v9_3_F01_node_repair/sochi_facts.jsonl')
    current = compare(BASE / 'results/geo/sochi_facts.jsonl')
    report['semantic_deltas_against_pre_repair_checkpoint'] = before
    report['semantic_deltas_against_current_repaired_facts'] = current
    report['comparison_scope'] = 'All actual characteristic values, route/target IDs, distances, statuses and unknown reasons; skip nested dataset SHA/input-file provenance, optional next_change, graph-contact candidate-count diagnostics, and explicit F01 connector diagnostics absent from the original calculator output. Physical target classification and coastline distance remain compared.'
    report['diagnostic_delta'] = {'removed_marine_contact_candidate': 'way/53012393#graph-node-655271566',
                                 'beach_id': 'way/53012393', 'old_graph_contacts': 24, 'corrected_graph_contacts': 23,
                                 'meaning': 'One unselected beach contact was removed; full re-selection confirms no W01 target/distance change for any of 89 hotels. Historical W01 passports retain the original graph candidate count.'}
    report['unfiltered_comparison_file'] = str(raw)
    report['comparison_method_version'] = 'V9.3-characteristic-versus-source-diagnostic-1'
    report['comparison_source_sha256'] = sha256(__file__)
    archives = {}
    for path in [Path(__file__), BASE / 'qa/geo/reselect_sochi_corrected_readonly.py', BASE / 'scripts/geo.py', BASE / 'scripts/geo_memory_containers.py', BASE / 'scripts/geo_opening_point_cache.py']:
        digest = sha256(path)
        archive = BASE / 'qa/geo/code_versions' / (digest + '.py')
        if not archive.exists():
            archive.write_bytes(path.read_bytes())
        archives[str(path)] = {'sha256': digest, 'exact_archive_file': str(archive)}
    report['code_version_archives'] = archives
    report['passed'] = report['canonical_files_unchanged'] and not current and len(before) == 2 and all(row['hotel_id'] == '10595288' and row['feature_id'] == 'F01' for row in before) and not report['corrected_routes_with_blocked_node_contacts']
    reportfile.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'before_deltas': before, 'current_deltas': current, 'old_snap_affected': report['old_snap_affected_origins_or_candidates'], 'diagnostic_delta': report['diagnostic_delta'], 'corrected_full_reselect_seconds': report['all_89_hotels_corrected_full_reselect_seconds']}, ensure_ascii=False, indent=2))
    assert report['passed']

if __name__ == '__main__':
    main()
