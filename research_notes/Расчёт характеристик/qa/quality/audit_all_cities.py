"""Low-memory independent per-city result QA for V9.

No full graph is loaded, no network is used, and calculator outputs are read only.
Only qa/quality is written. Missing completed components are pending, not success.
This is contract/provenance and numerical-consistency QA, not physical accuracy.
"""
import argparse,collections,hashlib,importlib.util,json,math,sys
from pathlib import Path
B=Path(__file__).resolve().parents[2];sys.path.insert(0,str(B/'scripts'))
from common import catalogue,hotels,read_jsonl,sha256,now
CITIES=['sochi','moscow','spb','kazan','kaliningrad','minsk','istanbul','dubai']
COAST={'sochi','spb','kaliningrad','istanbul','dubai'}
CORE_SHA='e575c51e28cbc3905eadb4e4d628005b9fad99031301a741d0477c80e73473ad'

def read(path):return list(read_jsonl(path))
def finite(v):
    if isinstance(v,float):return math.isfinite(v)
    if isinstance(v,dict):return all(finite(x)for x in v.values())
    if isinstance(v,list):return all(finite(x)for x in v)
    return True

def audit(city):
    checks=[];pending=[];inputs=[];cat=catalogue();hh=hotels(city);hids={str(h['hotel_id'])for h in hh}
    fids={fid for fid,f in cat.items()if f['recipes'][0]['data_kind']in {'А','Б'}}
    def check(name,ok,details=None):checks.append({'name':name,'passed':bool(ok),'details':details})
    paths={family:B/f'results/{family}/{city}_facts.jsonl'for family in ['geo','facades','environment']}
    for family,path in paths.items():
        if not path.exists():pending.append('Missing completed '+family+' facts: '+str(path))
    runpath=B/f'results/geo/{city}_run.json'
    if not runpath.exists():pending.append('Missing completed geo run manifest')
    if pending:return {'city_id':city,'at':now(),'status':'PENDING','pending':pending,'checks':[]}
    raw=[]
    for family,path in paths.items():
        rr=read(path);raw.extend(rr);inputs.append({'kind':family,'path':str(path),'sha256':sha256(path),'rows':len(rr)})
    bp=B/f'results/facades/{city}_building_facts.jsonl'
    if bp.exists():raw.extend(read(bp));inputs.append({'kind':'individual_buildings','path':str(bp),'sha256':sha256(bp)})
    unique={};conflicts=[]
    for r in raw:
        fid=r['fact_id']
        if fid in unique and r!=unique[fid]:conflicts.append(fid)
        unique[fid]=r
    rows=list(unique.values());pairs={(str(r['hotel_id']),r['feature_id'])for r in rows}
    check('cohort_and54features_complete',pairs=={(h,f)for h in hids for f in fids}and len(fids)==54,{'N_hotels':len(hh),'N_pairs':len(pairs)})
    check('no_conflicting_fact_ids',not conflicts,conflicts)
    check('finite_values_and_valid_statuses',all(finite(r)and r['status']in {'unknown','not_applicable','observed','computed','estimate','ostrovok_content'}for r in rows))
    check('unknown_reason_null_value',all(r['value']is None and r.get('unknown_reason')for r in rows if r['status']=='unknown'))
    check('known_value_sources_recipe',all(r['value']is not None and r['sources']and r['recipe_id']for r in rows if r['status']in {'computed','observed','estimate'}))
    check('all_canonical_recipes_have_trace',all({x['recipe_id']for x in r['recipe_attempts']}=={x['recipe_id']for x in cat[r['feature_id']]['recipes']}for r in rows))
    check('valid_result_stops_recipe_chain',all(all(x['state']=='not_attempted_after_valid_result'for x in r['recipe_attempts'][i+1:])for r in rows for i,a in enumerate(r['recipe_attempts'])if a['state']=='accepted'))
    matches=read(B/f'results/facades/{city}_building_matches.jsonl');matched={(str(r['hotel_id']),r['osm_id'])for r in matches}
    check('N02_N03_no_unmatched_point_proxy',all(r['status']!='estimate'and (r['status']!='computed'or (r['subject_level']in {'building','building_part'}and(str(r['hotel_id']),r['subject_id'])in matched))for r in rows if r['feature_id']in {'N02','N03'}))
    check('accepted_buildings_have_address_support',all(r['match_evidence']['rule']in {'full_address_and_spatial_consistency','exact_public_phone_hotel_site_plus_building_address'}for r in matches))
    check('H03_counting_convention_and_positive_scope',all(r['value'].get('counting_convention')and r['value'].get('aboveground_levels',0)>0 and r['value'].get('not_room_floor_labels')for r in rows if r['feature_id']=='H03'and r['status']=='observed'))
    check('no_window_geometry_diagnostic_as_canonical_value',all(r['status']=='unknown'for r in rows if r['feature_id']in {'V01','V02','V03','V04','V05','V06','S01','S02','S03'}))
    route_features={'W04','W05','W06','Q03'}
    check('route_subjects_for_route_attributes',all(r['subject_level']=='route'and r['subject_id']=='route:'+r['params']['route_id']for r in rows if r['feature_id']in route_features and r['params'].get('route_id')))
    check('W04_explicit_DSM_extension',all(r['recipe_id']=='W04-D18-DSM'and r.get('catalogue_recipe_not_satisfied')and r.get('hard_condition_eligible')is False and r['value'].get('ground_ascent_not_measured')for r in rows if r['feature_id']=='W04'and r['status']=='estimate'))
    q=[r for r in rows if r['feature_id']=='Q03'and r['status']=='computed']
    check('Q03_defined_denominator_and_partition',all(r['value'].get('total_graph_length_m',0)>0 and all(r['value'].get(k)is not None for k in ['known_lit_percent','known_unlit_percent','unknown_percent'])and abs(sum(r['value'][k]for k in ['known_lit_percent','known_unlit_percent','unknown_percent'])-100)<1e-7 for r in q))
    check('climate_complete30years_explicit_model',all(r['params'].get('model')=='ERA5'and r['params'].get('period_start')=='1991-01-01'and r['params'].get('period_end')=='2020-12-31'and r['params'].get('temperature_downscaling')is False and r['value'].get('n_years')==30 and len(r['sources'])==30 for r in rows if r['feature_id']in {'C01','C02','C03'}and r['status']=='computed'))
    check('green_landmask_historical_area_fraction',all(r['params'].get('epoch')==2021 and r['components'].get('excluded_water_class')==80 and r['components'].get('valid_fraction',0)>=.99 and abs(r['value']-100*r['components']['vegetation_area_m2']/r['components']['land_area_m2'])<1e-9 for r in rows if r['feature_id']=='E03'and r['status']=='computed'))
    run=json.loads(runpath.read_text());inputs.append({'kind':'geo_run','path':str(runpath),'sha256':sha256(runpath)})
    if city!='sochi':
        check('immutable_core_version_recorded',run.get('accepted_calculator_sha256')==CORE_SHA)
        check('actual_overrides_versioned',bool(run.get('runner_sha256'))and bool(run.get('memory_representation',{}).get('sha256'))and bool(run.get('hours_evaluation_extension',{}).get('sha256')))
        manifest=run['input_manifest'];files=manifest['input_files'];composite=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
        check('composite_source_hash_reproducible',manifest.get('sha256')==composite and manifest.get('sha256_kind')=='composite SHA256 of ordered input path+SHA manifests, not file hash')
        supplement=run.get('extra_node_input',{});sp=Path(supplement.get('snapshot_file','/unavailable'))
        check('restriction_supplement_hash_and_original_PBF_version',sp.is_file()and sha256(sp)==supplement.get('sha256')and bool(supplement.get('source_pbf_sha256'))and supplement.get('source_date')==manifest.get('source_date'))
    if city=='dubai':
        actual_adapter=Path(run['hours_evaluation_extension'].get('source_file',str(B/'scripts/geo_opening_point_cache.py')))
        spec=importlib.util.spec_from_file_location('qa_actual_dubai_hours_adapter',actual_adapter);adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter);evaluate=adapter.evaluate
        check('Dubai_actual_adapter_source_SHA_matches_run',sha256(actual_adapter)==run['hours_evaluation_extension']['sha256'])
        tz={'dubai':'Asia/Dubai'};countries={'dubai':'AE'};cases=[('24/7','open'),('Mo-Su 09:00-22:00','closed'),('Fr 20:00-04:00','open'),('24/7; PH off','unknown'),('Mo-Su 00:00-24:00; SH off','unknown')]
        evaluated=[{'expression':expression,'expected':expected,'actual':evaluate({'tags':{'opening_hours':expression}},'2026-11-14T02:00:00','dubai',timezones=tz,countries=countries)['state']}for expression,expected in cases]
        check('Dubai_unsupported_country_fallback_expected_states',all(x['actual']==x['expected']for x in evaluated),evaluated)
        check('Dubai_actual_run_not_legacy_AE_failure_adapter',run.get('hours_evaluation_extension',{}).get('sha256')!='a0b00092b42cde47da1dbae93ff4e4aa559274145b543b59ff129aa477aa169a')
    routespath=B/f'results/geo/{city}_all_selected_routes.jsonl'
    if not routespath.exists():
        pending.append('Exact all-selected-route traces missing (including POI routes)')
        routespath=B/f'results/geo/{city}_routes.jsonl'
    routes=read(routespath);routeids={r['properties']['route_id']for r in routes}
    trace_by_id={r['properties']['route_id']:r['properties']for r in routes}
    linked=[r for r in rows if r['status']in {'computed','observed','estimate'}and isinstance(r.get('value'),dict)and r['value'].get('route_id')in trace_by_id]
    check('published_route_lengths_match_exact_trace',all(('graph_length_m'not in r['value']or abs(r['value']['graph_length_m']-trace_by_id[r['value']['route_id']]['graph_length_m'])<1e-8)and('distance_m'not in r['value']or 'length_estimate_m'not in trace_by_id[r['value']['route_id']]or abs(r['value']['distance_m']-trace_by_id[r['value']['route_id']]['length_estimate_m'])<1e-8)for r in linked))
    if city!='sochi':
        box=run['extra_node_input']['bbox'];outside=[]
        for rt in routes:
            co=rt['geometry']['coordinates'];co=[co]if rt['geometry']['type']=='Point'else co
            if any(not(box[0]<=pt[0]<=box[2]and box[1]<=pt[1]<=box[3])for pt in co):outside.append(rt['properties']['route_id'])
        check('actual_routes_within_restriction_supplement_extent',not outside,outside)
    blocked={r['osm_node_id']for r in read(B/f'results/geo/{city}_blocked_nodes.jsonl')}
    # Independent inspection of the newly recovered pure-node tags. In Sochi
    # these three restrictions were absent from the old accepted blocked export.
    supplement_path=B/f'cohort/osm_{city}_pure_restriction_nodes.jsonl';supplement_blocked=set()
    if supplement_path.exists():
        for node in read_jsonl(supplement_path):
            t=node['tags'];public=t.get('foot')in {'yes','designated','permissive'}
            denied=t.get('foot')in {'no','private','use_sidepath'}or bool(t.get('foot:conditional')or t.get('access:conditional'))or(t.get('access')in {'no','private','customers','permit','destination'}and not public)or(t.get('locked')=='yes'and t.get('barrier')in {'gate','lift_gate','swing_gate','sliding_gate','wicket_gate','toll_booth','turnstile','full-height_turnstile','kissing_gate'}and not public)
            if denied:supplement_blocked.add(node['osm_id'])
        blocked.update(supplement_blocked)
    badroutes=[r['properties']['route_id']for r in routes if set(r['properties']['node_ids'])&blocked]
    check('saved_routes_avoid_explicit_node_prohibitions',not badroutes,{'routes':len(routes),'forbidden_route_ids':badroutes,'blocked_nodes':len(blocked),'independently_tag_checked_supplement_blocked_nodes':len(supplement_blocked)})
    required_routes={r['params'].get('route_id')or(r['value'].get('route_id')if isinstance(r['value'],dict)else None)for r in rows if r['status']in {'computed','observed','estimate'}}-{None}
    missing_routes=sorted(required_routes-routeids)
    if city=='sochi'and pending:
        pending.append('Published route references not yet covered by old trace: '+str(len(missing_routes)))
    else:check('every_published_route_has_exact_trace',not missing_routes,missing_routes)
    if city in COAST:
        mp=B/f'environment/marine_mapping_{city}.json'
        if not mp.exists():pending.append('Marine mapping not completed')
        else:
            mapping=json.loads(mp.read_text());tp=Path(mapping.get('source_beach_targets_path',str(B/f'results/geo/{city}_beach_targets.jsonl')))
            check('marine_mapping_matches_explicit_target_file',tp.exists()and sha256(tp)==mapping.get('source_beach_targets_sha256'))
            targets=read(tp);check('marine_targets_keep_coastline_gate',all(r.get('beach_kind')=='sea'and r['marine_classification'].get('accepted')and r['marine_classification']['coastline_distance_m']<=r['marine_classification']['tolerance_m']==25 for r in targets))
            check('fallback_marine_context_not_access_claim',all(r.get('route_id')is None and r.get('entry_candidate_id')is None and r.get('entry_lon_lat')is None and r.get('reference_target_is_accessible_beach_claim')is False for r in targets if r.get('selection_method')=='nearest_marine_polygon_fallback'))
            check('B02_explicit_thetao_depth_and_CF_support',all(r['params'].get('depth_m')==.49402499198913574 and r['params'].get('variable')=='sea_water_potential_temperature'and r['params'].get('period_start')=='2023-01-01'and r['params'].get('period_end')=='2025-12-31'and 'api_reported_latitude'in r['params']and 'grid_latitude'in r['params']for r in rows if r['feature_id']=='B02'and r['status']=='estimate'))
        check('B02_no_known_land_native_CF_point',all(r['params'].get('native_cf_worldcover2021_class')in {None,0,80}for r in rows if r['feature_id']=='B02'and r['status']=='estimate'),dict(collections.Counter(str(r['params'].get('native_cf_worldcover2021_class'))for r in rows if r['feature_id']=='B02'and r['status']=='estimate')))
        check('B02_exactly3_archive_year_sources',all(r['value'].get('n_years')==3 and len(r['value'].get('annual_monthly_means_C',{}))==3 and sum(s.get('provider')=='Open-MeteoMarineAPI'for s in r['sources'])==3 for r in rows if r['feature_id']=='B02'and r['status']=='estimate'))
    else:
        check('inland_marine_features_not_applicable',all(r['status']=='not_applicable'for r in rows if r['feature_id']in {'W01','B01','B02','B03','B04','B05'}))
    status='FAILED'if any(not c['passed']for c in checks)else 'PENDING'if pending else 'PASSED_WITH_PHYSICAL_ACCURACY_UNMEASURED'
    return {'city_id':city,'at':now(),'status':status,'N_hotels':len(hh),'hotel_feature_pairs':len(pairs),'fact_rows':len(rows),'checks':checks,'pending':pending,'input_files':inputs,'physical_gold_scope':'No independent full-feature physical gold is implied by these contract and provenance checks.'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--cities',nargs='+',choices=CITIES,default=CITIES);a=p.parse_args();summaries=[]
    for city in a.cities:
        r=audit(city);(B/f'qa/quality/{city}_all8_gates.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));summaries.append({k:r.get(k)for k in ['city_id','status','N_hotels','hotel_feature_pairs','pending']});print(json.dumps({'city_id':city,'status':r['status'],'checks':len(r['checks']),'failed':[c for c in r['checks']if not c['passed']],'pending':r['pending']},ensure_ascii=False),flush=True)
    (B/'qa/quality/all8_progress.json').write_text(json.dumps({'at':now(),'requested_cities':a.cities,'cities':summaries,'status':'PASSED'if len(a.cities)==8 and all(r['status']=='PASSED_WITH_PHYSICAL_ACCURACY_UNMEASURED'for r in summaries)else 'IN_PROGRESS'},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
