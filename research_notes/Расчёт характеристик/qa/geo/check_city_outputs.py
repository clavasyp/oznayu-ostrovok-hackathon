#!/usr/bin/env python3
"""Check output contracts after a successful city computation, without recomputing."""
import argparse,json,sys,collections,math
from pathlib import Path
BASE=Path(__file__).resolve().parents[2];sys.path.insert(0,str(BASE/'scripts'))
from common import hotels,read_jsonl,sha256,now
OWN='N01 N02 N03 N04 W01 W02 W03 W04 W05 W06 T01 T02 T03 L01 L02 L03 F01 F02 P03 P04 A01 R04 R05 Q01 Q02 Q03 X01 X02 X03'.split()
def main(city):
    p=BASE/'results/geo';file=p/f'{city}_facts.jsonl';rows=list(read_jsonl(file));hs=hotels(city);allroutes=p/f'{city}_all_selected_routes.jsonl';routes={r['properties']['route_id']:r for r in read_jsonl(allroutes)} if allroutes.exists() else {r['properties']['route_id']:r for r in read_jsonl(p/f'{city}_routes.jsonl')};known=[r for r in rows if r['status'] in {'computed','estimate','observed'}];expected={(h['hotel_id'],fid) for h in hs for fid in OWN};pairs={(r['hotel_id'],r['feature_id']) for r in rows};routefacts=[r for r in known if isinstance(r['value'],dict) and r['value'].get('route_id')];qs=[r for r in rows if r['feature_id']=='Q03' and r['status']=='computed'];wo=[r for r in known if r['feature_id']=='W01'];checks={
      'all_expected_hotel_feature_pairs':pairs==expected,
      'unique_fact_ids':len({r['fact_id'] for r in rows})==len(rows),
      'unknown_has_reason':all(r.get('unknown_reason') and r['value'] is None for r in rows if r['status']=='unknown'),
      'known_sources_recipe':all(r.get('sources') and r.get('recipe_id') for r in known),
      'every_recipe_trace_present':all(r.get('recipe_attempts') for r in rows),
      'all_selected_route_ids_saved':all(r['value']['route_id'] in routes for r in routefacts),
      'DSM_only_explicit_estimate_extension':all(r['status']=='estimate' and r['recipe_id']=='W04-D18-DSM' and r.get('catalogue_recipe_not_satisfied') and all(a['state']=='rejected' for a in r['recipe_attempts']) for r in known if r['feature_id']=='W04'),
      'marine_not_inland_beach_substitution':all(r['value'].get('marine_classification',{}).get('accepted') is True for r in wo),
      'zero_lighting_denominator_not_computed':all(r['value'].get('total_graph_length_m',0)>0 for r in qs),
      'lighting_percent_partition':all(abs(sum(r['value'][k] for k in ['known_lit_percent','known_unlit_percent','unknown_percent'])-100)<1e-7 for r in qs),
      'route_subjects_for_route_measurements':all(r['subject_level']=='route' and r['subject_id']=='route:'+r['params']['route_id'] for r in known if r['feature_id'] in {'W04','W05','W06','Q03'}),
      'no_fake_future_hours_validity':all(r.get('valid_to') is None and r.get('refresh_after') for r in known if r['feature_id'] in {'L01','L02','L03','F02','P04','R04'}),
      'N02N03_building_scope':all(r['subject_level']=='building' and r['value'].get('building_id') for r in known if r['feature_id'] in {'N02','N03'}),
      'P03_canonical_not_filled_without_required_policy':all(r['status']=='unknown' for r in rows if r['feature_id']=='P03'),
    }
    result={'city_id':city,'checked_at':now(),'facts_sha256':sha256(file),'checks':checks,'passed':all(checks.values()),'hotel_denominator':len(hs),'owned_feature_count':len(OWN),'hotel_feature_pairs':len(pairs),'fact_rows':len(rows),'route_fact_rows':len(routefacts),'unique_saved_selected_routes':len(routes),'statuses':dict(collections.Counter(r['status'] for r in rows)),'check_kind':'Contract/regression checks, not measured empirical accuracy'};(BASE/'qa/geo'/f'{city}_contract_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False,indent=2));assert result['passed']
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--city',required=True);main(p.parse_args().city)
