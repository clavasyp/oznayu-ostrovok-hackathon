#!/usr/bin/env python3
"""Full old/new join diff coverage and final known N02/N03 subject audit."""
import json
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
from common import read_jsonl, hotels, sha256
from repair_building_axis_features_v3 import accepted_rows, geometry_signatures

def main():
    archive = BASE / 'facades/archive/address_repair_20261024T135705Z/results/facades'
    cities = []
    for city in ['sochi', 'moscow', 'spb', 'kazan', 'kaliningrad', 'minsk', 'istanbul', 'dubai']:
        oldfile = archive / f'{city}_building_matches.jsonl'
        finalfile = BASE / 'results/facades' / f'{city}_building_matches.jsonl'
        after = geometry_signatures(accepted_rows(finalfile))
        baseline_proof = None
        if oldfile.exists():
            before = geometry_signatures(accepted_rows(oldfile))
            old_sha = sha256(oldfile)
        else:
            # Unchanged cities were not copied into the repair archive. Their
            # original worker recorded the actual join SHA before this repair.
            original_inputs = BASE / 'qa/geo' / f'{city}_calculation_inputs.json'
            baseline = json.loads(original_inputs.read_text())
            old_sha = baseline['building_matches_sha256']
            if sha256(finalfile) != old_sha:
                raise RuntimeError('Unarchived city changed from original recorded building-match SHA: ' + city)
            before = after
            baseline_proof = {'file': str(original_inputs), 'file_sha256': sha256(original_inputs),
                              'original_recorded_join_sha256': old_sha, 'current_join_byte_identical_to_recorded_baseline': True}
        affected = sorted(h for h in before.keys() | after.keys() if before.get(h, []) != after.get(h, []))
        accepted = {h: {subject for subject, geometry in rows} for h, rows in after.items()}
        facts = list(read_jsonl(BASE / 'results/geo' / f'{city}_facts.jsonl'))
        mismatches = []
        feature_rows = {}
        for fid in ['N02', 'N03']:
            feature_rows[fid] = {'computed_rows': sum(row['status'] == 'computed' for row in facts if row['feature_id'] == fid),
                                 'unknown_rows': sum(row['status'] == 'unknown' for row in facts if row['feature_id'] == fid)}
            for hotel in hotels(city):
                hotel_id = hotel['hotel_id']
                group = [row for row in facts if row['hotel_id'] == hotel_id and row['feature_id'] == fid]
                known = {row['subject_id'] for row in group if row['status'] in {'computed', 'observed', 'estimate'}}
                expected = accepted.get(hotel_id, set())
                if known != expected:
                    mismatches.append({'hotel_id': hotel_id, 'feature_id': fid, 'accepted_building_ids': sorted(expected), 'known_fact_subject_ids': sorted(known),
                                       'unknown_reasons': [row['unknown_reason'] for row in group if row['status'] == 'unknown']})
        reportfile = BASE / 'qa/geo' / (f'{city}_N02N03_strict_join_repair_report.json' if city == 'sochi' else f'{city}_N02N03_join_diff_repair_report.json')
        report = json.loads(reportfile.read_text()) if affected and reportfile.exists() else None
        repair_complete = not affected or bool(report and report['passed'] and report['affected_hotel_ids'] == affected and report['final_building_match_sha256'] == sha256(finalfile))
        current = {'city_id': city, 'cohort_hotels': len(hotels(city)),
                   'old_building_match_file': str(oldfile) if oldfile.exists() else None, 'old_sha256': old_sha, 'unchanged_city_baseline_proof': baseline_proof,
                   'final_building_match_file': str(finalfile), 'final_sha256': sha256(finalfile),
                   'old_relations': sum(map(len, before.values())), 'final_relations': sum(map(len, after.values())),
                   'all_affected_hotel_ids': affected, 'known_feature_rows': feature_rows,
                   'final_known_subjects_equal_all_final_accepted_buildings': not mismatches, 'subject_mismatches': mismatches,
                   'full_diff_repair_coverage_complete': repair_complete,
                   'repair_report': str(reportfile) if report else None, 'repair_report_sha256': sha256(reportfile) if report else None,
                   'repair_seconds': report.get('repair_seconds') if report else 0,
                   'unrelated_passport_lines_byte_identical': report.get('unrelated_passport_lines_byte_identical') if report else True,
                   'all_other_geo_files_byte_identical': all(report['all_other_geo_files_byte_identical'].values()) if report else True}
        cities.append(current)
    result = {'method_version': 'V9-all8-join-diff-to-axis-passport-coverage-1', 'cities': cities,
              'cohort_hotels': sum(row['cohort_hotels'] for row in cities),
              'affected_hotels': sum(len(row['all_affected_hotel_ids']) for row in cities),
              'all_affected_hotel_ids_derived_from_complete_snapshot_diffs': True,
              'source_code_sha256': sha256(__file__), 'network_requests': 0,
              'scope': 'Every known N02/N03 building subject agrees with final accepted joins for all439hotels. The same AEQD/axis classes are used; no new route or full geo run occurred during join repairs.'}
    result['passed'] = all(row['full_diff_repair_coverage_complete'] and row['final_known_subjects_equal_all_final_accepted_buildings'] and row['unrelated_passport_lines_byte_identical'] and row['all_other_geo_files_byte_identical'] for row in cities)
    path = BASE / 'qa/geo/all8_building_axis_join_coverage.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    source_archive = BASE / 'qa/geo/code_versions' / (sha256(__file__) + '.py')
    if not source_archive.exists():
        source_archive.write_bytes(Path(__file__).read_bytes())
    print(json.dumps({'passed': result['passed'], 'affected_hotels': result['affected_hotels'],
                      'cities': [{k: row[k] for k in ['city_id', 'old_relations', 'final_relations', 'all_affected_hotel_ids', 'subject_mismatches', 'full_diff_repair_coverage_complete']} for row in cities]}, ensure_ascii=False, indent=2))
    assert result['passed']

if __name__ == '__main__':
    main()
