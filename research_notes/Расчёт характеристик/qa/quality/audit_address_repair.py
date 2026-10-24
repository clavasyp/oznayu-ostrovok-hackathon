"""Independent final strict-address repair QA, without loading a city graph.

Writes only qa/quality. The lexical fixtures/replay validate rules and lineage;
none of them constitute physical hotel-to-building match accuracy or gold.
"""
import ast,collections,hashlib,importlib.util,json,sys
from pathlib import Path
B=Path(__file__).resolve().parents[2];sys.path.insert(0,str(B/'scripts'))
import facades
from common import hotels,read_jsonl,sha256,now
CITIES=['sochi','moscow','spb','kazan','kaliningrad','minsk','istanbul','dubai']
REPORTS={'sochi':'sochi_N02N03_strict_join_repair_report.json',**{c:c+'_N02N03_join_diff_repair_report.json'for c in ['kaliningrad','moscow','spb','kazan']}}
EXPECTED_REMOVED={('sochi','7725078','way/808856239'),('sochi','8085655','way/80180198'),('kaliningrad','7719161','way/134871120'),('kaliningrad','13647475','way/39386693')}

def read(p):return list(read_jsonl(p))
def linehashes(p):return [hashlib.sha256(x).hexdigest()for x in p.read_bytes().splitlines(keepends=True)]
def main():
    checks=[]
    def check(name,ok,details=None):checks.append({'name':name,'passed':bool(ok),'details':details})
    finalpath=B/'qa/facades/final8_review.json';final=json.loads(finalpath.read_text());actual_core=sha256(B/'scripts/facades.py')
    check('final_facades_algorithm_SHA',actual_core==final['new_core_sha256'])
    testpath=B/'qa/facades/test_address_gate_v92.py';tree=ast.parse(testpath.read_text());pairs=next(ast.literal_eval(node.value)for node in tree.body if isinstance(node,ast.Assign)and any(isinstance(t,ast.Name)and t.id=='pairs'for t in node.targets));fixture_rows=[]
    for house,address,expected in pairs:
        actual=facades.full_house_gate(house,address);fixture_rows.append({'house':house,'address':address,'expected':expected,'actual':actual,'passed':actual==expected})
    check('seventeen_literal_house_suffix_corpus_fixtures',len(fixture_rows)==17 and all(x['passed']for x in fixture_rows),fixture_rows)
    # The helper may find the street's number; the complete address matcher must reject it.
    lexical_hotel={'hotel_id':'QA-street-number','address':'улица 8 Марта 13','has_room_snapshot':False}
    check('complete_matcher_rejects_street_number_as_house',facades.address_evidence(lexical_hotel,{'addr:street':'улица 8 Марта','addr:housenumber':'8'})is None)
    legacy_sha=final['old_core_sha256'];legacy_path=B/f'qa/geo/code_versions/{legacy_sha}.py';check('historical_v1_source_archive_hash',sha256(legacy_path)==legacy_sha);spec=importlib.util.spec_from_file_location('qa_facades_historical_v1',legacy_path);legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
    hh={str(h['hotel_id']):h for h in hotels()};all_current=[];accepted_by_city={};address_replays=[];unreviewed=[];removed=[];added=[];affected=set()
    declared_current={(r['city_id'],str(r['hotel_id']),r['building_id'])for r in final['all_current_relation_scope_audit']}
    declared_old={(r['city_id'],str(r['hotel_id']),r['building_id'])for r in final['pre_repair_audit_rows']}
    for city in CITIES:
        p=B/f'results/facades/{city}_building_matches.jsonl';rows=read(p);accepted_by_city[city]=collections.defaultdict(set)
        for r in rows:
            key=(city,str(r['hotel_id']),r['osm_id']);all_current.append(key);accepted_by_city[city][str(r['hotel_id'])].add(r['osm_id'])
            ev=facades.address_evidence(hh[str(r['hotel_id'])],r['tags']);stored=r['match_evidence']['address'];legacy_exact=False;metadata_only=False
            if ev!=stored:
                legacy_ev=legacy.address_evidence(hh[str(r['hotel_id'])],r['tags']);legacy_exact=legacy_ev==stored;changed_keys={k for k in set(stored)|set(ev or {})if stored.get(k)!=(ev or {}).get(k)};metadata_only=bool(ev)and changed_keys<={'rule','normalized_house','normalized_address'}
            address_replays.append({'city_id':city,'hotel_id':str(r['hotel_id']),'building_id':r['osm_id'],'source_house':r['tags'].get('addr:housenumber'),'source_street':r['tags'].get('addr:street',r['tags'].get('addr:place')),'ota_address_used':stored['ota_address_used'],'source_id_version':r.get('version'),'full_match_reproduced':bool(ev),'stored_address_evidence_exact':ev==stored,'legacy_v1_exact':legacy_exact,'legacy_metadata_only':metadata_only,'stored_lineage_reproduced':ev==stored or(legacy_exact and metadata_only),'match_rule':r['match_evidence']['rule']})
        summary=next(s for s in final['city_summary']if s['city_id']==city)
        check(city+'_matches_fact_fan_hashes_final',sha256(p)==summary['matches_sha256']and sha256(B/f'results/facades/{city}_facts.jsonl')==summary['main_facts_sha256']and sha256(B/f'results/facades/{city}_fan_facts.jsonl.gz')==summary['fan_gzip_sha256'])
        # Check the complete per-hotel N02/N03 subject sets, not only known rows.
        gf=read(B/f'results/geo/{city}_facts.jsonl')
        problems=[]
        for h in hotels(city):
            hid=str(h['hotel_id']);expected=accepted_by_city[city].get(hid,set())
            for fid in ['N02','N03']:
                rowsf=[r for r in gf if str(r['hotel_id'])==hid and r['feature_id']==fid]
                subjects={r['subject_id']for r in rowsf if r['status']=='computed'}
                if subjects!=expected:problems.append({'hotel_id':hid,'feature_id':fid,'expected':sorted(expected),'actual':sorted(subjects)})
                if not expected and not all(r['status']=='unknown'for r in rowsf):problems.append({'hotel_id':hid,'feature_id':fid,'problem':'No accepted building must leave canonical feature unknown'})
        check(city+'_N02_N03_subject_sets_exact_FINAL_buildings',not problems,problems)
        if city in final['repair_deltas']:
            delta=final['repair_deltas'][city];old=read(Path(delta['old_matches_path']));oldset={(str(r['hotel_id']),r['osm_id'])for r in old};newset={(str(r['hotel_id']),r['osm_id'])for r in rows};derived_removed=oldset-newset;derived_added=newset-oldset
            removed.extend((city,*r)for r in derived_removed);added.extend((city,*r)for r in derived_added);derived_affected={h for h,s in derived_removed|derived_added};affected.update((city,h)for h in derived_affected)
            check(city+'_complete_snapshot_diff_matches_author',derived_removed==set(map(tuple,delta['removed']))and derived_added==set(map(tuple,delta['added']))and derived_affected==set(delta['affected_hotel_ids']))
        if city in REPORTS:
            reportpath=B/'qa/geo'/REPORTS[city];repair=json.loads(reportpath.read_text());checkpoint=Path(repair['checkpoint']);oldpath=checkpoint/f'{city}_facts.jsonl'
            if not oldpath.exists():oldpath=checkpoint/'results/geo'/f'{city}_facts.jsonl'
            oldlines=[line for line in oldpath.read_bytes().splitlines(keepends=True)if json.loads(line)['feature_id']not in {'N02','N03'}];newlines=[line for line in(B/f'results/geo/{city}_facts.jsonl').read_bytes().splitlines(keepends=True)if json.loads(line)['feature_id']not in {'N02','N03'}]
            check(city+'_unrelated_geo_passports_byte_unchanged',oldlines==newlines)
            check(city+'_repair_uses_current_building_snapshot',repair['published']and repair['passed']and repair['final_building_match_sha256']==sha256(p)and repair['facts_sha256']==sha256(B/f'results/geo/{city}_facts.jsonl')and repair['run_sha256']==sha256(B/f'results/geo/{city}_run.json'))
            other=repair['all_other_geo_files_byte_identical'];check(city+'_other_geo_output_hashes_preserved',bool(other)and(all(other.values())if isinstance(other,dict)else bool(other)),other)
            # Check current files against the own previous accepted phase fingerprint,
            # which independently proves route and environment immutability.
    currentset=set(all_current)
    check('all176relations_have_full_address_replay',len(all_current)==176 and all(r['full_match_reproduced']and r['stored_lineage_reproduced']for r in address_replays))
    check('full_current_candidate_audit_exact_accepted_set',currentset==declared_current and len(final['all_current_relation_scope_audit'])==176)
    check('complete_original139candidate_audit_present',len(final['pre_repair_audit_rows'])==139 and len(declared_old)==139 and len(final['pre_repair_gate_regression_audit'])==139)
    check('all_four_false_or_uncertain_parent_relations_removed',set(removed)==EXPECTED_REMOVED and all(r not in currentset for r in removed),sorted(removed))
    check('repair_relation_accounting139minus4plus41equals176',len(added)==41 and 139-4+len(added)==len(all_current)and len(affected)==43,{'removed':len(removed),'added':len(added),'affected_hotels':len(affected)})
    additional={(r['city_id'],str(r['hotel_id']),r['building_id'])for r in final['additional_candidate_gate_audit']}
    expected_additional={r for r in added if r[0]not in {'sochi','kaliningrad'}}
    check('six_other_city_additional_candidate_audit_complete',additional==expected_additional and len(additional)==30)
    previous=json.loads((B/'qa/quality/history/all8_review_before_address_repair.json').read_text());fingerprints={v['path']:v['sha256']for v in previous['frozen_inputs']};unchanged=[]
    for city in CITIES:
        for p in [B/f'results/environment/{city}_facts.jsonl',B/f'results/geo/{city}_all_selected_routes.jsonl']:
            unchanged.append({'city_id':city,'path':str(p),'passed':sha256(p)==fingerprints[str(p)]})
    check('all8_environment_and_exact_route_traces_unchanged',all(r['passed']for r in unchanged),unchanged)
    nonecomparisons=[]
    for city in CITIES:
        for row in read(B/f'results/facades/{city}_ota_comparisons.jsonl'):
            if row['feature_id']=='H03'and row.get('other_value')and all(v is None for v in row['other_value'].values()):nonecomparisons.append(row)
    check('H03_missing_levels_never_number_agreement_or_proved_conflict',all(row['classification']!='raw_numbers_agree_not_verified_counting_convention'and 'proved'not in row['classification']for row in nonecomparisons),[{'city_id':r['city_id'],'hotel_id':r['hotel_id'],'classification':r['classification'],'other_value':r['other_value']}for r in nonecomparisons])
    out={'at':now(),'passed':all(c['passed']for c in checks),'scope':'Strict lexical address rule regression plus all176current source-address relations and dependent subject-set lineage. This does not establish physical hotel-to-building match accuracy.','checks':checks,'current_address_replays':address_replays,'removed_relations':sorted(removed),'added_relations':sorted(added),'affected_hotel_pairs':sorted(affected),'new_facades_SHA256':actual_core,'final_facades_review_SHA256':sha256(finalpath),'physical_gold_added':0}
    (B/'qa/quality/address_repair_review.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps({'passed':out['passed'],'gates':len(checks),'failed':[c for c in checks if not c['passed']],'current_relations':len(all_current),'affected_hotels':len(affected)},ensure_ascii=False))
    if not out['passed']:raise SystemExit(1)
if __name__=='__main__':main()
