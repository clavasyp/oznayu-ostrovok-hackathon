"""Offline V9 runtime/size accounting. Run after all eight city outputs exist.

Shared timing groups are never allocated to individual families or summed again.
The output is measured process elapsed time, not machine-independent throughput.
"""
import csv,json
from collections import Counter,defaultdict
from common import BASE,catalogue,read_jsonl,now,sha256

CITIES='sochi moscow spb kazan kaliningrad minsk istanbul dubai'.split()
GROUPS={
 'V':['facades_history_solar'],'S':['facades_history_solar'],'H':['facades_history_solar'],
 'N':['N'],'W':['W_A_Q03'],'A':['W_A_Q03'],
 'L':['L_F_P_R04'],'F':['L_F_P_R04'],'P':['L_F_P_R04'],
 'R':['L_F_P_R04','T_R05_Q_X'],'T':['T_R05_Q_X'],
 'Q':['W_A_Q03','T_R05_Q_X'],'X':['T_R05_Q_X'],
 'B':['B_and_other_gates'],'C':['C'],'E':['E'],'M':[]}
SHARED={'facades_history_solar','W_A_Q03','L_F_P_R04','T_R05_Q_X','B_and_other_gates'}

def main():
    cat=catalogue(); counts=defaultdict(Counter); sizes=Counter()
    source=BASE/'integrated/all8_facts.jsonl'
    for line in source.open():
        r=json.loads(line); key=(r['city_id'],r['feature_id'][0])
        counts[key][r['status']]+=1;sizes[key]+=len(line.encode())
    cities=[];families=[];module_inputs=[]
    for city in CITIES:
        paths={name:BASE/f'results/{name}/{city}_{suffix}.json' for name,suffix in
               [('facades','summary'),('geo','run'),('environment','summary')]}
        modules={k:json.loads(p.read_text()) for k,p in paths.items()}
        module_inputs.extend({'path':str(p),'sha256':sha256(p)} for p in paths.values())
        f,g,e=(modules[k] for k in ['facades','geo','environment'])
        groups={**g['family_seconds'],**e['family_runtime_s'],
                'facades_history_solar':f['runtime_seconds']['facades_history_solar']}
        physical={name:sum(p.stat().st_size for p in (BASE/'results'/name).glob(city+'_*') if p.is_file())
                  for name in modules}
        module_seconds={'facades':f['runtime_seconds']['total'],
                        'geo':g.get('pipeline_seconds_including_capture_and_auxiliary_serialization',g['total_seconds']),
                        'environment':e['runtime_s']}
        repairs=g.get('repair_phases',[])
        cities.append({'city_id':city,'hotels':f['hotel_count'],
            'calculator_process_elapsed_sum_s':sum(module_seconds.values()),
            'facades_s':module_seconds['facades'],'geo_s':module_seconds['geo'],
            'geo_core_s':g['total_seconds'],
            'restriction_node_prepare_s':g.get('node_input_prepare_seconds'),
            'geo_correction_compute_s':sum(x.get('corrected_graph_delta_calculation_seconds',0) for x in repairs),
            'geo_correction_publish_s':sum(x.get('repair_publication_seconds',0) for x in repairs),
            'geo_correction_validation_s':sum(x.get('full_reselection_validation',{}).get('total_readonly_validation_seconds',0) for x in repairs),
            'geo_building_join_repair_s':sum(x.get('repair_seconds',0) for x in repairs),
            'environment_s':module_seconds['environment'],
            'era5_fetch_s':e.get('source_fetch_runtime_s'),
            'marine_fetch_s':e.get('marine_source_fetch_runtime_s'),
            'physical_result_bytes':sum(physical.values()),
            'facades_result_bytes':physical['facades'],'geo_result_bytes':physical['geo'],
            'environment_result_bytes':physical['environment'],
            'main_fact_rows':sum(sum(counts[(city,fam)].values()) for fam in GROUPS),
            'fan_cells':f['fan_cells'],'fan_passports':f['full_passport_fan_facts'],
            'fan_gzip_bytes':f['fan_facts_gzip_bytes'],
            'matched_hotels':f['matched_hotels'],'matched_buildings':f['matched_buildings']})
        for fam,keys in GROUPS.items():
            families.append({'city_id':city,'family':fam,
                'family_name':next(x['family'] for fid,x in cat.items() if fid[0]==fam),
                '54_feature_count':sum(fid[0]==fam and x['recipes'][0]['data_kind'] in {'А','Б'} for fid,x in cat.items()),
                'main_fact_rows':sum(counts[(city,fam)].values()),
                'main_logical_jsonl_bytes':sizes[(city,fam)],
                'status_counts':json.dumps(counts[(city,fam)],sort_keys=True),
                'measured_group_times_s':json.dumps({k:groups[k] for k in keys},sort_keys=True),
                'individual_family_seconds':groups[keys[0]] if len(keys)==1 and keys[0] not in SHARED else None,
                'time_scope':'not calculated: hotelier-only family' if not keys else
                    'shared group timing, not separately measured or additive by family' if any(k in SHARED for k in keys) else 'individual family',
                'fan_bytes_note':'Uncompressed main logical rows only; compressed fan belongs to shared V/S city artifact and is counted once in city physical bytes.'})
    out=BASE/'scale';out.mkdir(exist_ok=True)
    for name,rows in [('city_metrics',cities),('family_metrics',families)]:
        with (out/(name+'.csv')).open('w') as fh:
            w=csv.DictWriter(fh,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    interrupted=[]
    for p in sorted((BASE/'qa/geo').glob('*_attempt.json')):
        interrupted.append({'path':str(p),'sha256':sha256(p),'record':json.loads(p.read_text())})
    result={'at':now(),'cities':cities,'city_count':len(cities),'family_rows':len(families),
        'physical_result_bytes':sum(r['physical_result_bytes'] for r in cities),
        'fan_passports':sum(r['fan_passports'] for r in cities),
        'process_elapsed_sum_s':sum(r['calculator_process_elapsed_sum_s'] for r in cities),
        'input_files':module_inputs,'integrated_source_sha256':sha256(source),
        'interrupted_attempt_manifests':interrupted,
        'limitations':['Input rasters/PBF/API cache not included in result bytes.',
            'Integrated views duplicate logical results and are not included in physical result bytes.',
            'Group timings include multiple families and must not be summed across family rows.',
            'First source extraction, network fetch, human review and total end-to-end time are separate.',
            'Historical Sochi core elapsed time is preserved; later correction/publication/reselection phases are separate columns, not hidden inside its original total.',
            'Targeted N02/N03 building-join repairs have their own geo_building_join_repair_s column; they do not rebuild the graph and are not added to historical geo process totals.',
            'This is a local-machine measurement; Docker <=10min remains unmeasured.']}
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['city_count','family_rows','physical_result_bytes','fan_passports','process_elapsed_sum_s']}))

if __name__=='__main__':main()
