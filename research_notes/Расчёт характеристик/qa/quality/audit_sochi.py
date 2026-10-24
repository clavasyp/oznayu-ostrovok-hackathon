"""Independent V9 Sochi result gates. Writes only qa/quality, never calculator outputs.

This is source/contract QA and repair regression, not independent physical gold.
Run with the existing V4 Python environment after all Sochi calculators settle.
"""
from pathlib import Path
import json,sys,hashlib,collections
from datetime import datetime
B=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(B/'scripts'))
from common import catalogue,hotels,read_jsonl,sha256,now,TRIPS

def main():
    checks=[]
    def check(name,ok,details=None):checks.append({'name':name,'passed':bool(ok),'details':details})
    hs=hotels('sochi');hids={str(h['hotel_id'])for h in hs};cat=catalogue()
    fids={fid for fid,f in cat.items()if f['recipes'][0]['data_kind']in {'А','Б'}}
    rows=[];inputs=[]
    for family in ['geo','facades','environment']:
        p=B/f'results/{family}/sochi_facts.jsonl';rr=list(read_jsonl(p));rows.extend(rr)
        inputs.append({'path':str(p),'sha256':sha256(p),'rows':len(rr)})
    p=B/'results/facades/sochi_building_facts.jsonl';rows.extend(read_jsonl(p));inputs.append({'path':str(p),'sha256':sha256(p)})
    unique={};conflict=[]
    for r in rows:
        if r['fact_id']in unique and r!=unique[r['fact_id']]:conflict.append(r['fact_id'])
        unique[r['fact_id']]=r
    rows=list(unique.values());pairs={(str(r['hotel_id']),r['feature_id'])for r in rows}
    check('54_catalogue_features_attempted_for_all89',len(fids)==54 and len(hs)==89 and pairs=={(h,f)for h in hids for f in fids})
    check('no_conflicting_fact_identity',not conflict,conflict)
    check('unknown_has_reason_and_null_value',all(r.get('unknown_reason')and r['value']is None for r in rows if r['status']=='unknown'))
    check('known_has_value_source_recipe',all(r['value']is not None and r['sources']and r['recipe_id']for r in rows if r['status']in {'observed','computed','estimate'}))
    check('every_canonical_recipe_has_trace',all({a['recipe_id']for a in r['recipe_attempts']}=={a['recipe_id']for a in cat[r['feature_id']]['recipes']}for r in rows))
    check('accepted_stops_later_recipes_including_false',all(not any(a['state']=='accepted'for a in r['recipe_attempts'][i+1:])and all(a['state']=='not_attempted_after_valid_result'for a in r['recipe_attempts'][i+1:])for r in rows for i,a in enumerate(r['recipe_attempts'])if a['state']=='accepted'))
    check('trip_arrivals_are_saturdays_at02local',all(datetime.fromisoformat(t['arrival_local']).weekday()==5 and datetime.fromisoformat(t['arrival_local']).hour==2 for t in TRIPS),TRIPS)
    check('no_canonical_window_result_from_orientation_only',all(r['status']=='unknown'for r in rows if r['feature_id']in {'V01','V02','V03','V04','V05','V06','S01','S02','S03'}))
    matches=list(read_jsonl(B/'results/facades/sochi_building_matches.jsonl'));joinids={(str(r['hotel_id']),r['osm_id'])for r in matches}
    check('N02_N03_building_subject_has_accepted_address_join',all((str(r['hotel_id']),r['subject_id'])in joinids and r['subject_level']=='building'for r in rows if r['feature_id']in {'N02','N03'}and r['status']=='computed'))
    check('building_join_never_nearest_only',all(r.get('match_evidence',{}).get('rule')in {'full_address_and_spatial_consistency','exact_public_phone_hotel_site_plus_building_address'}for r in matches))
    check('H03_keeps_counting_convention',all(r['value'].get('counting_convention')and r['value'].get('not_room_floor_labels')and r['subject_level']in {'building','building_part'}for r in rows if r['feature_id']=='H03'and r['status']=='observed'))
    check('W04_DSM_only_explicit_D18_extension',all(r['recipe_id']=='W04-D18-DSM'and r.get('catalogue_recipe_not_satisfied')and r.get('hard_condition_eligible')is False and r['value'].get('ground_ascent_not_measured')for r in rows if r['feature_id']=='W04'and r['status']=='estimate'))
    check('P03_no_dogpark_only_canonical_claim',all(r['status']=='unknown'for r in rows if r['feature_id']=='P03'))
    q=[r for r in rows if r['feature_id']=='Q03'and r['status']=='computed']
    check('Q03_computed_requires_nonzero_denominator_and_fractions',all(r['value'].get('total_graph_length_m',0)>0 and all(r['value'].get(k)is not None for k in ['known_lit_percent','known_unlit_percent','unknown_percent'])for r in q))
    check('Q03_known_unlit_unknown_sum100',all(abs(sum(r['value'][k]for k in ['known_lit_percent','known_unlit_percent','unknown_percent'])-100)<1e-7 for r in q if r['value'].get('total_graph_length_m',0)>0))
    ef=[r for r in rows if r['feature_id']=='E03'and r['status']=='computed']
    check('E03_historical_land_mask_area_weighted',all(r['params'].get('epoch')==2021 and r['components']['excluded_water_class']==80 and r['components']['valid_fraction']>=.99 and 'pixel polygon intersections'in r['components']['area_method'] and abs(r['value']-100*r['components']['vegetation_area_m2']/r['components']['land_area_m2'])<1e-9 for r in ef))
    cc=[r for r in rows if r['feature_id']in {'C01','C02','C03'}and r['status']=='computed']
    check('C01_C03_full30year_ERA5_no_downscale',all(r['params'].get('period_start')=='1991-01-01'and r['params'].get('period_end')=='2020-12-31'and r['params'].get('model')=='ERA5'and r['params'].get('temperature_downscaling')is False and r['value']['n_years']==30 and len(r['sources'])==30 for r in cc))
    check('C02_C03_threshold_operators_explicit',all(r['params'].get('threshold_mm_per_day')==1 and r['params'].get('comparison')=='>='for r in cc if r['feature_id']=='C02')and all(r['params'].get('threshold_C')==30 and r['params'].get('comparison')=='>'for r in cc if r['feature_id']=='C03'))
    targets_path=B/'results/geo/sochi_beach_targets.jsonl';targets=list(read_jsonl(targets_path));mapping=json.loads((B/'environment/marine_mapping_sochi.json').read_text())
    check('marine_mapping_matches_frozen_target_hash',mapping.get('source_beach_targets_sha256')==sha256(targets_path),{'mapping_hash':mapping.get('source_beach_targets_sha256'),'actual_hash':sha256(targets_path)})
    check('all_selected_beaches_have_geometric_marine_gate',all(r.get('beach_kind')=='sea'and r.get('marine_classification',{}).get('accepted')and r['marine_classification']['coastline_distance_m']<=r['marine_classification']['tolerance_m']==25 and r['marine_classification']['coastline_source'].get('version')is not None for r in targets))
    blocked={r['osm_node_id']for r in read_jsonl(B/'results/geo/sochi_blocked_nodes.jsonl')};routes=list(read_jsonl(B/'results/geo/sochi_routes.jsonl'))
    check('all_saved_routes_avoid_explicitly_blocked_nodes',all(not(set(r['properties']['node_ids'])&blocked)for r in routes),{'routes':len(routes),'blocked_nodes':len(blocked)})
    bf=[r for r in rows if r['feature_id']=='B02'and r['status']=='estimate']
    check('B02_depth_model_shortperiod_and_CF_support_explicit',all(r['params'].get('depth_m')==.49402499198913574 and r['params'].get('variable')=='sea_water_potential_temperature'and r['params'].get('period_start')=='2023-01-01'and r['params'].get('period_end')=='2025-12-31'and 'api_reported_latitude'in r['params']and 'grid_latitude'in r['params']and r['components']['marine_spatial_support'].get('api_to_native_offset_deg')==1/24 for r in bf))
    output={'at':now(),'scope':'Independent source/contract QA plus repair regressions; not physical hotel accuracy.','passed':all(c['passed']for c in checks),'checks':checks,'input_files':inputs,'fact_rows_deduplicated':len(rows),'hotel_feature_pairs':len(pairs),'marine_target_sha256':sha256(targets_path)}
    dest=B/'qa/quality/result_gates.json';dest.write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps({'passed':output['passed'],'checks':len(checks),'failed':[c for c in checks if not c['passed']],'fact_rows':len(rows)},ensure_ascii=False))
    if not output['passed']:raise SystemExit(1)
if __name__=='__main__':main()
