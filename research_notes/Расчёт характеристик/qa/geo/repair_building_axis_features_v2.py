#!/usr/bin/env python3
"""Targeted N02/N03 repair after final strict full-address building joins.

Read only busy-road and active-mapped rail geometries. Never construct Graph,
route, or call geo.run(). Preserve every unrelated passport line and every route
artifact byte-for-byte. Publication requires an explicit final join-file SHA.
"""
import argparse
import collections
import copy
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
from common import V4, CITY_V4, read_jsonl, hotels, sha256, fact, attempts, now
import geo
from pyproj import CRS, Transformer
from shapely import wkb, STRtree
from shapely.geometry import Point
from shapely.ops import transform

EXPECTED_CORE = 'e575c51e28cbc3905eadb4e4d628005b9fad99031301a741d0477c80e73473ad'
VERSION = 'V9-N02N03-building-join-diff-repair-2'
def accepted_rows(path):
    groups = collections.defaultdict(list)
    for row in read_jsonl(path):
        for building in row.get('matches', row.get('accepted_matches', [row])):
            if building.get('accepted') is False or building.get('match_status') in {'rejected', 'ambiguous', 'unmatched'}:
                continue
            geometry = building.get('geometry_wkb4326') or building.get('geometry_wkb') or building.get('building_geometry_wkb')
            if geometry:
                groups[str(row['hotel_id'])].append(building)
    return groups

def geometry_signatures(groups):
    return {hotel: sorted((str(b.get('building_id', b.get('source_id', b.get('osm_id', 'unknown_building')))), b.get('geometry_wkb4326') or b.get('geometry_wkb') or b.get('building_geometry_wkb')) for b in rows) for hotel, rows in groups.items()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--city', required=True, choices=sorted(geo.CENTERS))
    parser.add_argument('--expected-matches-sha', required=True)
    parser.add_argument('--previous-matches-file', required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    start = time.perf_counter()
    city = args.city
    if sha256(geo.__file__) != EXPECTED_CORE:
        raise RuntimeError('Accepted calculator source changed')
    joinfile = BASE / 'results/facades' / f'{city}_building_matches.jsonl'
    if sha256(joinfile) != args.expected_matches_sha:
        raise RuntimeError('Final facade building-match SHA mismatch; wait for FINAL')
    previous_file = Path(args.previous_matches_file).resolve()
    previous_sha = sha256(previous_file)
    joins = accepted_rows(joinfile)
    previous_joins = accepted_rows(previous_file)
    old_signatures, new_signatures = geometry_signatures(previous_joins), geometry_signatures(joins)
    cohort_ids = {h['hotel_id'] for h in hotels(city)}
    affected = {hotel for hotel in old_signatures.keys() | new_signatures.keys() if old_signatures.get(hotel, []) != new_signatures.get(hotel, []) and hotel in cohort_ids}
    if not affected:
        raise RuntimeError('No accepted building identity/geometry changes between old and FINAL snapshots; no repair required')
    factsfile = BASE / 'results/geo' / f'{city}_facts.jsonl'
    runfile = BASE / 'results/geo' / f'{city}_run.json'
    old_lines = factsfile.read_text().splitlines(keepends=True)
    old_facts = [json.loads(line) for line in old_lines]
    old_groups = collections.defaultdict(list)
    for row in old_facts:
        if row['hotel_id'] in affected and row['feature_id'] in {'N02', 'N03'}:
            old_groups[(row['hotel_id'], row['feature_id'])].append(row)
    if len(old_groups) != 2 * len(affected):
        raise RuntimeError('Missing affected hotel-feature rows')
    files_before = {str(path): sha256(path) for path in (BASE / 'results/geo').glob(city + '_*') if path.is_file()}
    code = {}
    for source in [Path(__file__), Path(geo.__file__), BASE / 'scripts/common.py', BASE / 'scripts/facades.py']:
        digest = sha256(source)
        archive = BASE / 'qa/geo/code_versions' / (digest + '.py')
        if not archive.exists():
            archive.write_bytes(source.read_bytes())
        code[str(source)] = {'sha256': digest, 'exact_archive_file': str(archive)}
    x, y = geo.CENTERS[city]
    tr = Transformer.from_crs(4326, CRS.from_proj4(f'+proj=aeqd +lat_0={y} +lon_0={x} +datum=WGS84 +units=m'), always_xy=True)
    layers = {'N02': [], 'N03': []}
    observed_manifest = None
    record_count = 0
    load_start = time.perf_counter()
    for record, manifest in geo.read_records(city):
        record_count += 1
        observed_manifest = manifest
        tags = record['tags']
        road = tags.get('highway') in geo.BUSY
        rail = tags.get('railway') in {'rail', 'light_rail', 'tram'} and not any(k in tags for k in ['disused', 'abandoned', 'construction', 'proposed']) and not any(k.startswith(('disused:', 'abandoned:', 'construction:', 'proposed:')) for k in tags)
        if not road and not rail:
            continue
        try:
            geometry = wkb.loads(record['geometry_wkb'], hex=True)
            projected = transform(tr.transform, geometry)
        except Exception:
            continue
        item = {'record': record, 'geometry': projected, 'sid': geo.osm_id(record), 'tags': tags}
        if road and geometry.geom_type in {'LineString', 'MultiLineString'}:
            layers['N02'].append(item)
        if rail:
            layers['N03'].append(item)
    load_seconds = time.perf_counter() - load_start
    trees = {fid: STRtree([item['geometry'] for item in layer]) if layer else None for fid, layer in layers.items()}
    if city == 'sochi':
        input_paths = [BASE / 'cohort/osm_sochi_extended.jsonl']
    else:
        ck = CITY_V4.get(city, city)
        input_paths = [V4 / 'raw' / f'osm_{ck}.jsonl', V4 / 'raw' / f'osm_{ck}_closed_roads.jsonl']
        input_paths = [path for path in input_paths if path.exists()]
    input_files = [{'snapshot_file': str(path), 'sha256': sha256(path)} for path in input_paths]
    source_manifest = {**observed_manifest, 'sha256': hashlib.sha256(json.dumps(input_files, sort_keys=True).encode()).hexdigest(),
                       'sha256_kind': 'composite SHA256 of ordered road/rail input path+SHA manifests, not file hash',
                       'input_files': input_files, 'scope': 'N02/N03 axis layers only. No pedestrian graph/node supplement is required for footprint-to-axis distance.'}
    hs = {hotel['hotel_id']: hotel for hotel in hotels(city)}
    new_groups = {}
    recalculated = []
    for hotel_id in sorted(affected):
        h = hs[hotel_id]
        for fid in ['N02', 'N03']:
            key = 'road_classes' if fid == 'N02' else 'rail_classes'
            rows = []
            if joins[hotel_id] and trees[fid]:
                for building in joins[hotel_id]:
                    footprint = transform(tr.transform, wkb.loads(building.get('geometry_wkb4326') or building.get('geometry_wkb') or building.get('building_geometry_wkb'), hex=True))
                    axis = layers[fid][int(trees[fid].nearest(footprint))]
                    subject = building.get('building_id', building.get('source_id', building.get('osm_id', 'unknown_building')))
                    params = {key: sorted(geo.BUSY) if fid == 'N02' else ['rail', 'light_rail', 'tram'],
                              'distance_origin': 'accepted building footprint', 'target_geometry': 'axis',
                              'rail_status_convention': 'rail/light_rail/tram mapped active; disused/abandoned/construction/proposed excluded' if fid == 'N03' else None}
                    row = fact(h, fid, value={'distance_m': footprint.distance(axis['geometry']), 'target_id': axis['sid'],
                                             'target_class': axis['tags'].get('highway' if fid == 'N02' else 'railway'),
                                             'target_name': axis['tags'].get('name'), 'building_id': subject}, unit='m', status='computed',
                               subject_level='building', subject_id=str(subject), recipe_id=fid + '-R2', params=params,
                               sources=[geo.osm_source(axis, source_manifest), building.get('source', {'building_join': str(joinfile)})],
                               scope_note='One strictly matched full-address building; not all hotel buildings or a window distance. Rail status is mapped active state, not inspected frequency.',
                               confidence={'level': 'medium', 'reason': 'Accepted strict full-address/corpus building relation plus the same city AEQD nearest eligible axis calculation. Independent physical accuracy is not established.'},
                               recipe_attempts=attempts(fid, {fid + '-R1': 'No accepted official road/active-rail layer for this building in cache.'}, fid + '-R2'))
                    old = next((old for old in old_groups[(hotel_id, fid)] if old['subject_id'] == str(subject)), None)
                    recalculated.append({'hotel_id': hotel_id, 'feature_id': fid, 'subject_id': str(subject),
                                         'retained_existing_subject': old is not None,
                                         'same_value_as_existing_subject': old is not None and old['value'] == row['value'],
                                         'geometry_changed_in_final_join': dict(old_signatures.get(hotel_id, [])).get(str(subject)) != dict(new_signatures.get(hotel_id, [])).get(str(subject)),
                                         'old_value': old['value'] if old else None, 'new_value': row['value']})
                    rows.append(row)
            else:
                params = {key: sorted(geo.BUSY) if fid == 'N02' else ['rail', 'light_rail', 'tram']}
                diagnostic = {'rejected_prior_subject_ids': [old['subject_id'] for old in old_groups[(hotel_id, fid)]],
                              'cohort_address': h['address'], 'final_building_match_count': len(joins[hotel_id]),
                              'meaning': 'The final strict address/corpus gate does not establish a hotel footprint. A hotel point is not substituted for a footprint.'}
                if trees[fid]:
                    point = Point(*tr.transform(h['longitude'], h['latitude']))
                    axis = layers[fid][int(trees[fid].nearest(point))]
                    diagnostic.update({'point_to_axis_distance_m': point.distance(axis['geometry']), 'target_id': axis['sid'], 'not_feature_value': 'Point distance is not footprint distance.'})
                reason = 'verified_hotel_building_footprint_rejected_or_unconfirmed_by_strict_full_address_corpus_gate' if not joins[hotel_id] else 'matching_axis_layer_unavailable'
                rows.append(fact(h, fid, unknown_reason=reason, params=params, component_diagnostics=diagnostic,
                                 recipe_attempts=attempts(fid, {fid + '-R1': 'No accepted official road/active-rail and validated hotel-footprint pair.',
                                                               fid + '-R2': 'Final full-address/corpus gate has no accepted hotel footprint; prior base-address building cannot establish this requested corpus.'})))
            for row in rows:
                row['algorithm_version'] = 'v9-N02N03-join-diff-repair-2'
                row['depends_on'] = [*row.get('depends_on', []), {'kind': 'final_building_match_snapshot', 'file': str(joinfile), 'sha256': args.expected_matches_sha},
                                     {'kind': 'axis_layer_input_manifest', 'sha256': source_manifest['sha256'], 'sha256_kind': source_manifest['sha256_kind'], 'input_files': input_files}]
                row['repair'] = {'version': VERSION, 'reason': 'Strict full-address/corpus parsing changes accepted footprint identities; repair both false-parent matches and prior false-negative valid matches.', 'recomputed_without_graph': True}
            new_groups[(hotel_id, fid)] = rows
    unexpected_surviving_value_changes = [row for row in recalculated if row['retained_existing_subject'] and not row['geometry_changed_in_final_join'] and not row['same_value_as_existing_subject']]
    if unexpected_surviving_value_changes:
        raise RuntimeError('Surviving footprint axis value changed unexpectedly: ' + json.dumps(unexpected_surviving_value_changes, ensure_ascii=False))
    if sha256(joinfile) != args.expected_matches_sha:
        raise RuntimeError('Final building matches changed during repair preparation')
    for source, info in code.items():
        if sha256(source) != info['sha256']:
            raise RuntimeError('Source file changed during repair preparation: ' + source)
    new_lines, inserted = [], set()
    for line, old in zip(old_lines, old_facts):
        group = (old['hotel_id'], old['feature_id'])
        if group not in new_groups:
            new_lines.append(line)
        elif group not in inserted:
            new_lines.extend(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n' for row in new_groups[group])
            inserted.add(group)
    new_facts = [json.loads(line) for line in new_lines]
    changed = []
    for group in sorted(new_groups):
        old_map = {row['subject_id']: row for row in old_groups[group]}
        new_map = {row['subject_id']: row for row in new_groups[group]}
        changed.append({'hotel_id': group[0], 'feature_id': group[1],
                        'removed_subjects': [{'subject_id': subject, 'fact_id': row['fact_id'], 'value': row['value'], 'status': row['status']} for subject, row in old_map.items() if subject not in new_map],
                        'new_subjects': [{'subject_id': subject, 'fact_id': row['fact_id'], 'value': row['value'], 'status': row['status'], 'unknown_reason': row.get('unknown_reason')} for subject, row in new_map.items() if subject not in old_map],
                        'retained_subject_ids': sorted(old_map.keys() & new_map.keys())})
    draft = BASE / 'qa/geo' / f'{city}_N02N03_join_diff_repair_draft.jsonl'
    with draft.open('w') as f:
        f.writelines(new_lines)
    report = {'version': VERSION, 'city_id': city, 'prepared_at': now(), 'published': args.publish,
              'affected_hotel_ids': sorted(affected), 'previous_building_match_file': str(previous_file), 'previous_building_match_sha256': previous_sha,
              'affected_selection_rule': 'All cohort hotels whose accepted building ID+geometry sets differ between previous and FINAL snapshots; not a hardcoded hotel list.', 'final_building_match_file': str(joinfile), 'final_building_match_sha256': args.expected_matches_sha,
              'new_match_count_by_hotel': {hotel: len(joins[hotel]) for hotel in affected},
              'old_facts': len(old_facts), 'new_facts': len(new_facts), 'changes': changed, 'recomputed_surviving_subjects': recalculated,
              'input_records_streamed': record_count, 'axis_geometry_counts': {fid: len(layer) for fid, layer in layers.items()},
              'axis_load_seconds': load_seconds, 'effective_axis_input_manifest': source_manifest,
              'full_geo_run_or_graph_executed': False, 'source_code_versions': code, 'draft_file': str(draft),
              'old_geo_file_sha256': files_before, 'network_requests': 0}
    if args.publish:
        checkpoint = BASE / 'checkpoints' / f'{city}_before_N02N03_join_diff_{args.expected_matches_sha[:12]}'
        if checkpoint.exists():
            raise RuntimeError('Repair checkpoint already exists; refuse repeated publication')
        checkpoint.mkdir(parents=True)
        for path in [factsfile, runfile]:
            shutil.copy2(path, checkpoint / path.name)
        (checkpoint / 'manifest.json').write_text(json.dumps({'created_at': now(), 'geo_file_sha256_before': files_before, 'copied': [factsfile.name, runfile.name], 'reason': VERSION}, ensure_ascii=False, indent=2) + '\n')
        factsfile.write_text(''.join(new_lines))
        # Preserve unrelated passports including Sochi v9.3 F01 byte-for-byte.
        old_unrelated = [line for line, row in zip(old_lines, old_facts) if (row['hotel_id'], row['feature_id']) not in new_groups]
        new_unrelated = [line for line, row in zip(new_lines, new_facts) if (row['hotel_id'], row['feature_id']) not in new_groups]
        unchanged_files = {path: sha256(path) == digest for path, digest in files_before.items() if path not in {str(factsfile), str(runfile)}}
        assert old_unrelated == new_unrelated and all(unchanged_files.values())
        run = json.loads(runfile.read_text())
        report.update({'checkpoint': str(checkpoint), 'unrelated_passport_lines_byte_identical': old_unrelated == new_unrelated,
                       'all_other_geo_files_byte_identical': unchanged_files, 'facts_sha256': sha256(factsfile),
                       'repair_seconds': time.perf_counter() - start})
        phase = {k: v for k, v in report.items() if k not in {'old_geo_file_sha256', 'draft_file'}}
        phase['runtime_scope'] = 'Separate targeted N02/N03 axis-layer load/recompute/publication. Historical full-run time and previous F01 repair phases are preserved.'
        run['repair_phases'] = [*run.get('repair_phases', []), phase]
        run['facts'] = len(new_facts)
        run['statuses'] = dict(collections.Counter(row['status'] for row in new_facts))
        run['unique_hotel_feature_pairs'] = len({(row['hotel_id'], row['feature_id']) for row in new_facts})
        run['feature_counts'] = {fid: dict(collections.Counter(row['status'] for row in new_facts if row['feature_id'] == fid)) for fid in geo.OWN}
        run['result_bytes'] = sum(path.stat().st_size for path in (BASE / 'results/geo').glob(city + '_*jsonl'))
        runfile.write_text(json.dumps(run, ensure_ascii=False, indent=2) + '\n')
        report['run_sha256'] = sha256(runfile)
        report['passed'] = old_unrelated == new_unrelated and all(unchanged_files.values()) and sha256(joinfile) == args.expected_matches_sha and sha256(previous_file) == previous_sha
    else:
        report['preparation_seconds'] = time.perf_counter() - start
    destination = BASE / 'qa/geo' / f'{city}_N02N03_join_diff_repair_report.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in {'source_code_versions', 'old_geo_file_sha256', 'effective_axis_input_manifest', 'all_other_geo_files_byte_identical'}}, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
