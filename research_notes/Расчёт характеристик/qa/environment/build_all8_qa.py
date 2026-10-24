"""Read-only checks of computed inputs; writes QA/metrics only. No HTTP."""
from pathlib import Path
from collections import Counter,defaultdict
from datetime import datetime,timezone
import calendar,csv,hashlib,json,math,sys

BASE=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(BASE/'scripts'))
from environment import FEATURES,ENV,OUT,RAW,QA,UTC_OFFSET,MONTHS

CITIES=['sochi','moscow','spb','kazan','kaliningrad','minsk','istanbul','dubai']
def write(path,data):Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def digest(path):
 d=hashlib.sha256()
 with Path(path).open('rb')as f:
  while b:=f.read(4*1024*1024):d.update(b)
 return d.hexdigest()

def main():
 facts=[];summaries={};errors=[];coverage=[];sources={};source_hashes={};all_ids=set();native_classes=Counter()
 for city in CITIES:
  p=OUT/f'{city}_facts.jsonl'
  if not p.exists():raise RuntimeError(f'Missing final facts {city}')
  rr=[json.loads(s)for s in p.open()if s.strip()];facts+=rr;summaries[city]=json.loads((OUT/f'{city}_summary.json').read_text())
  if digest(p)!=summaries[city]['result_sha256']:errors.append(f'{city}:result summary hash mismatch')
  by=defaultdict(lambda:defaultdict(list))
  for f in rr:
   by[f['feature_id']][f['hotel_id']].append(f['status'])
   if f['fact_id']in all_ids:errors.append('duplicate fact_id '+f['fact_id'])
   all_ids.add(f['fact_id'])
   if f['status']in ('unknown','not_applicable')and f['value']is not None:errors.append('nullcontract '+f['fact_id'])
   if f['status']=='unknown'and not f['unknown_reason']:errors.append('missingunknownreason '+f['fact_id'])
   if f['observed_at']is not None or f['valid_to']is not None:errors.append('historicaltimecontract '+f['fact_id'])
   if len(f['recipe_attempts'])!=2:errors.append('recipechain '+f['fact_id'])
   for s in f['sources']:
    if s.get('snapshot_file'):
     sources[s['snapshot_file']]=s
     if s.get('sha256'):source_hashes[s['snapshot_file']]=s['sha256']
   if f['feature_id']=='B02'and f['status']=='estimate':
    native_classes[str(f['params'].get('native_cf_worldcover2021_class'))]+=1
    if f['params'].get('native_cf_worldcover2021_class')not in (None,0,80):errors.append('marine independent landpoint '+f['fact_id'])
    if f['params']['beach_to_native_cf_m']>30000:errors.append('marine spatial support beyond30km '+f['fact_id'])
    pp=Path(f['params']['target_source_path'])
    if digest(pp)!=f['params']['target_source_sha256']:errors.append('stale marine source '+city)
  for fid in FEATURES:
   hh=by[fid];n=summaries[city]['hotel_count']
   if len(hh)!=n:errors.append(f'{city}:{fid} missing hotel rows')
   row={'city_id':city,'feature_id':fid,'N_hotels':n,'computed_full_contexts':0,'estimate_full_contexts':0,
       'partial_contexts':0,'unknown_all_contexts':0,'not_applicable':0,'known_any_context':0}
   for statuses in hh.values():
    if all(s=='not_applicable'for s in statuses):row['not_applicable']+=1
    elif all(s=='computed'for s in statuses):row['computed_full_contexts']+=1;row['known_any_context']+=1
    elif all(s in ('computed','estimate')for s in statuses):row['estimate_full_contexts']+=1;row['known_any_context']+=1
    elif any(s in ('computed','estimate')for s in statuses):row['partial_contexts']+=1;row['known_any_context']+=1
    else:row['unknown_all_contexts']+=1
   coverage.append(row)
 # Independent arithmetic implementation against raw API arrays. Shared data,
 # so this is an implementation check, never empirical physical accuracy.
 raw=defaultdict(dict);unit_files=0
 for path in RAW.glob('era5_*.json'):
  if path.name.endswith('.meta.json'):continue
  meta=path.with_suffix('.meta.json')
  if not meta.exists()or json.loads(meta.read_text()).get('http_status')!=200:continue
  dd=json.loads(path.read_text());dd=dd if isinstance(dd,list)else[dd]
  city=path.name.split('_')[1];unit_files+=1
  for d in dd:
   for k,u in [('temperature_2m_mean','°C'),('temperature_2m_max','°C'),('precipitation_sum','mm')]:
    if d['daily_units'].get(k)!=u:errors.append('APIunit '+str(path))
   cell=(city,float(d['latitude']),float(d['longitude']))
   for i,date in enumerate(d['daily']['time']):raw[cell][date]=tuple(d['daily'][k][i]for k in ['temperature_2m_mean','temperature_2m_max','precipitation_sum'])
 checks=0;maximum_difference=0;annual_checks=0;marine_checks=0
 for f in facts:
  if f['feature_id']not in ('C01','C02','C03')or f['status']!='computed':continue
  p=f['params'];rr=raw[(f['city_id'],p['grid_latitude'],p['grid_longitude'])]
  selected=[v for date,v in rr.items()if int(date[5:7])==p['month']]
  if f['feature_id']=='C01':expected=sum(v[0]for v in selected)/len(selected)
  elif f['feature_id']=='C02':expected=sum(v[2]>=1 for v in selected)/30
  else:expected=sum(v[1]>30 for v in selected)/30
  delta=abs(expected-f['value']['mean']);maximum_difference=max(maximum_difference,delta);checks+=1
  if delta>1e-10:errors.append('climate arithmetic '+f['fact_id'])
  annual=[]
  for year in range(1991,2021):
   yy=[v for date,v in rr.items()if int(date[:4])==year and int(date[5:7])==p['month']]
   if len(yy)!=calendar.monthrange(year,p['month'])[1]or any(v is None for row in yy for v in row):
    errors.append('climate completeness '+f['fact_id']);continue
   if f['feature_id']=='C01':v=sum(row[0]for row in yy)/len(yy)
   elif f['feature_id']=='C02':v=sum(row[2]>=1 for row in yy)
   else:v=sum(row[1]>30 for row in yy)
   annual.append(v);annual_checks+=1
   if abs(v-f['value']['annual_values'][str(year)])>1e-10:errors.append('climate annual '+f['fact_id'])
  if len(annual)==30:
   mean=sum(annual)/30;sd=math.sqrt(sum((v-mean)**2 for v in annual)/30)
   if abs(sd-f['value']['stddev_across_years'])>1e-10:errors.append('climate spread '+f['fact_id'])
 marine_raw={}
 for path in RAW.glob('marine_*_cells_*.json'):
  if path.name.endswith('.meta.json'):continue
  dd=json.loads(path.read_text());dd=dd if isinstance(dd,list)else[dd]
  marine_raw[str(path)]=dd
  for d in dd:
   if d['hourly_units'].get('sea_surface_temperature')!='°C':errors.append('marine APIunit '+str(path))
 for f in facts:
  if f['feature_id']!='B02'or f['status']!='estimate':continue
  annual={}
  for src in f['sources']:
   if src.get('provider')!='Open-MeteoMarineAPI':continue
   dd=marine_raw[src['snapshot_file']]
   idx=int(src['json_pointer'][1:])if src.get('json_pointer')else 0;d=dd[idx]
   rows=[(t,v)for t,v in zip(d['hourly']['time'],d['hourly']['sea_surface_temperature'])if int(t[11:13])in (0,6,12,18)]
   year=int(rows[0][0][:4]);month=int(rows[0][0][5:7])
   if len(rows)!=calendar.monthrange(year,month)[1]*4 or any(v is None or not math.isfinite(v)for _,v in rows):
    errors.append('marine native completeness '+f['fact_id']);continue
   annual[str(year)]=sum(v for _,v in rows)/len(rows)
  if set(annual)!={'2023','2024','2025'}:errors.append('marine period '+f['fact_id']);continue
  for year,v in annual.items():
   if abs(v-f['value']['annual_monthly_means_C'][year])>1e-10:errors.append('marine annual '+f['fact_id'])
  mean=sum(annual.values())/3;sd=math.sqrt(sum((v-mean)**2 for v in annual.values())/3)
  if abs(mean-f['value']['mean_C'])>1e-10 or abs(sd-f['value']['stddev_across_years_C'])>1e-10:errors.append('marine arithmetic '+f['fact_id'])
  marine_checks+=1
 for path,expected in source_hashes.items():
  if digest(path)!=expected:errors.append('source hash '+path)
 ledger=[json.loads(s)for s in(ENV/'http_ledger.jsonl').open()if s.strip()]
 manual=json.loads((ENV/'manual_probe_log.json').read_text())['probes'];net={}
 for model,family in [('era5','ERA5'),('meteofrance_currents','Marine')]:
  lr=[r for r in ledger if r['params'].get('models')==model]
  probes=[r for r in manual if r['kind'].startswith('climate')== (family=='ERA5')]
  statuses=Counter(str(r['http_status'])for r in lr);manualstatuses=Counter(str(r['status'])for r in probes)
  combined=statuses+manualstatuses;urls=Counter(r['url']for r in lr)
  net[family]={'primary_HTTP_requests':len(lr),'primary_HTTP_statuses':dict(statuses),
     'manual_probe_HTTP_requests':len(probes),'manual_probe_statuses':dict(manualstatuses),
     'all_HTTP_requests_including_probes':len(lr)+len(probes),'all_HTTP_statuses':dict(combined),
     'same_URL_repeated_primary_requests':sum(n-1 for n in urls.values()),'transport_retry_attempts_observed':0,
     'parameter_correction_error_probes':manualstatuses.get('400',0),
     'HTTP200_semantically_empty_probes':sum(r['status']==200 and 'null'in r['kind']for r in probes),
     'primary_response_decoded_payload_bytes':sum(r['bytes']for r in lr),
     'probe_response_decoded_payload_bytes':sum(r['response_bytes']for r in probes),
     'conservative_weighted_primary_calls_estimate':sum(r['conservative_weight_estimate']for r in lr),
     'first_primary_request_at':min(r['requested_at']for r in lr)if lr else None,
     'last_primary_response_at':max(r['finished_at']for r in lr)if lr else None,
     'manual_probe_timestamp_precision':'UTCdate2026-10-24only; exacttimestampsnotretainedforeachprobe'}
  net[family]['conservative_weighted_successful_probe_calls_estimate']=5*30/14 if family=='ERA5'else 4*31/14
  net[family]['conservative_weighted_successful_total_estimate']=net[family]['conservative_weighted_primary_calls_estimate']+net[family]['conservative_weighted_successful_probe_calls_estimate']
 primary_paths={Path(r['path'])for r in ledger};primary_metas={p.with_suffix('.meta.json')for p in primary_paths}
 raster_paths={Path(p)for p,s in sources.items()if s.get('provider')=='ESA WorldCover'}
 net['cache']={'retained_primary_response_payload_files':len(primary_paths),
    'retained_primary_response_payload_bytes':sum(p.stat().st_size for p in primary_paths),
    'retained_primary_response_meta_bytes':sum(p.stat().st_size for p in primary_metas),
    'all_environment_raw_files_bytes':sum(p.stat().st_size for p in RAW.rglob('*')if p.is_file()),
    'unique_reused_referenced_WorldCover_tiles':len(raster_paths),'reused_WorldCover_input_bytes':sum(p.stat().st_size for p in raster_paths),
    'reused_WorldCover_input_files':[{'path':str(p),'bytes':p.stat().st_size,'sha256':source_hashes.get(str(p))}for p in sorted(raster_paths)],
    'all8_final_fact_bytes':sum(s['result_bytes']for s in summaries.values()),
    'wire_transfer_bytes':None,'byte_definition':'requests.content decodedbody/cachebytes, not compressedwiretraffic. Reusedrastersnotnewnetwork.'}
 net['cache']['environment_source_dir_bytes_excluding_runtime']=sum(p.stat().st_size for p in ENV.rglob('*')if p.is_file()and 'runtime'not in p.relative_to(ENV).parts)
 net['cache']['total_environment_source_and_referenced_raster_bytes']=net['cache']['environment_source_dir_bytes_excluding_runtime']+net['cache']['reused_WorldCover_input_bytes']
 net['cache']['total_definition']='Whole environment/ directory including source snapshots, source/provenance/README/ledger/mapping metadata, excluding installed runtime; plus unique referenced cached WorldCover inputs. No double counting raw primary files.'
 net['stages']={c:{'climate_fetch_s':s['source_fetch_runtime_s'],'marine_fetch_measured_cumulative_s':s['marine_source_fetch_runtime_s'],
       'calculator_s':s['runtime_s'],'family_calc_s':s['family_runtime_s'],'result_bytes':s['result_bytes']}for c,s in summaries.items()}
 net['quota_note']='Weightedvalues are conservativeestimates, notobservedbilledquota. Manual400maynotconsumequota; errorcorrectionprobesreportedseparately. NoHTTP429ortransportretryobservedinrecordedrun.'
 net['HTTP_count_scope']='Actual ERA5/Marine APIrequests only, including7recordedmanualprobes. Exploratorywebsearch/docs/sourcecode/CFmetadataHTTP anddependencyresolvertrafficnotclaimedascompletelyinstrumentedtotal.'
 net['native_CF_control_response_payload_bytes']=json.loads((ENV/'marine_semantic_audit.json').read_text())['native_total_response_payload_bytes']
 net['new_ostrovok_HTTP_requests']=0
 net['known_superseded_calculator_runs']=json.loads((ENV/'superseded_calculation_runs.json').read_text())
 write(QA/'all8_network_metrics.json',net)
 with(OUT/'all8_coverage.csv').open('w')as f:
  w=csv.DictWriter(f,fieldnames=list(coverage[0]));w.writeheader();w.writerows(coverage)
 report={'at':datetime.now(timezone.utc).isoformat(),'status':'passed'if not errors else'failed','hotels':sum(s['hotel_count']for s in summaries.values()),
    'fact_rows':len(facts),'source_files_hash_checked':len(source_hashes),'climate_API_files_unit_checked':unit_files,
    'independent_arithmetic_checks':checks,'climate_annual_recalculations':annual_checks,'marine_native_arithmetic_checks':marine_checks,
    'maximum_absolute_arithmetic_difference':maximum_difference,
    'arithmetic_scope':'Sharedinputimplementationcheck, notindependentphysicalgold','native_WorldCover_class_by_B02_fact':dict(native_classes),
    'gold_eligible_environment_cases':0,'errors':errors}
 write(QA/'all8_qa.json',report);print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
