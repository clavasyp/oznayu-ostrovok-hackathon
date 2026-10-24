"""Verify V9 facade output contracts, without asserting physical accuracy."""
import collections,gzip,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import facades as f
city=sys.argv[1] if len(sys.argv)>1 else 'sochi';p=f.OUT
hh=f.hotels(city);ids={h['hotel_id'] for h in hh};main=list(f.read_jsonl(p/f'{city}_facts.jsonl'));fan=list(f.read_jsonl(p/f'{city}_fan.jsonl'));mm=list(f.read_jsonl(p/f'{city}_building_matches.jsonl'));summary=json.loads((p/f'{city}_summary.json').read_text());by=collections.defaultdict(list)
for m in mm:by[m['hotel_id']].append(m)
checks=[]
def ck(name,value):
 checks.append({'name':name,'passed':bool(value)});assert value,name
ck('one_main_record_per_hotel_feature',len(main)==len(hh)*len(f.FIDS) and len({(r['hotel_id'],r['feature_id']) for r in main})==len(main))
ck('main_fact_identity_unique',len({r['fact_id'] for r in main})==len(main))
ck('cohort_membership',all(r['hotel_id'] in ids for r in main))
ck('unknown_has_reason_and_null_value',all(r['unknown_reason'] and r['value'] is None for r in main if r['status']=='unknown'))
ck('all_recipes_accounted',all(len(r['recipe_attempts'])==2 for r in main))
ck('all_v_s_canonical_unverified_window_gated',all(r['status']=='unknown' for r in main if r['feature_id'] in f.FANFIDS))
ck('history_observed_single_matched_building',all(len(by[r['hotel_id']])==1 and r['subject_level'] in ['building','building_part'] for r in main if r['status']=='observed'))
ck('nearest_only_never_accepted',all(m['match_evidence']['rule']!='nearest' and m['match_evidence']['address'] for m in mm))
ck('matched_summary_not_defaultdict_inflation',summary['matched_hotels']==len({m['hotel_id'] for m in mm}))
ck('fan_no_existing_window_fabrication',all(c['actual_window_confirmed'] is False and c['mapping_to_room_category']=='unknown' for c in fan))
ck('fan_unknown_floor_not_fabricated',all(c['height_interval_m']==[None,None] for c in fan if c['physical_floor_ordinal'] is None))
ck('fan_canonical_values_null',all(all(v['status']=='unknown' and v['value'] is None for v in c['canonical_facts'].values()) for c in fan))
ck('solar_components_not_canonical_hours',all(all(not s['canonical_S01_computed'] and not s['canonical_V05_computed'] for s in c['components']['solar_orientation_bounds']) for c in fan))
# Stream full passport set to avoid retaining repeated source data in memory.
n=0;factids=set();required={'fact_id','city_id','hotel_id','feature_id','subject_level','subject_id','params','value','unit','status','unknown_reason','sources','recipe_id','confidence','observed_at','calculated_at','valid_to','recipe_attempts'}
with gzip.open(p/f'{city}_fan_facts.jsonl.gz','rt') as fp:
 for line in fp:
  r=json.loads(line);n+=1;factids.add(r['fact_id'])
  assert required<=set(r),required-set(r)
  assert r['status']=='unknown' and r['value'] is None
ck('full_passport_fan_n',n==summary['full_passport_fan_facts']==len(fan)*25)
ck('full_passport_fan_identity_unique',len(factids)==n)
ck('full_passport_fan_dates_not_checkout',all(len(c['components']['solar_orientation_bounds'])==6 for c in fan))
f.dump(f.BASE/f'qa/facades/{city}_output_checks.json',{'city':city,'passed':all(x['passed'] for x in checks),'checks':checks,'main_facts':len(main),'full_passport_fan_facts':n,'scope':'Format, exact input lineage and semantic gates; not real-hotel accuracy.'})
print(city,len(checks),'output checks passed',n,'fan fact passports')
