"""Combine auditable Codex cases while preserving evidence eligibility.

Does NOT convert source audits, numerical checks, or partial statements into
gold labels. Human review is pending for every case. No new network requests.
"""
import csv,json
from pathlib import Path
from common import BASE,read_jsonl,write_jsonl,hotels,catalogue,now

def truth(value):return str(value).lower() in {'true','1','yes'}

def main():
    inputs=[BASE/'facades/reference_cases.csv',BASE/'qa/geo/reference_cases.csv',
            BASE/'qa/environment/audit_cases.csv',BASE/'validation/station_comparison.csv']
    out=[];missing=[];hh={h['hotel_id']:h for h in hotels()}
    pred={}
    for p in (BASE/'integrated').glob('sochi_facts.jsonl'):
        for r in read_jsonl(p):pred.setdefault((r['hotel_id'],r['feature_id']),[]).append(r)
    for p in inputs:
        if not p.exists():missing.append(str(p));continue
        for r in csv.DictReader(p.open()):
            hid=r.get('hotel_id','');fid=r.get('feature_id','')
            if not hid or not fid:raise ValueError((p,r))
            gold=truth(r.get('canonical_gold_eligible',r.get('canonical_full_feature_gold',r.get('gold_eligible',False))))
            ref=r.get('reference_url',r.get('reference_source_url',''))
            out.append({'case_id':r['case_id'],'hotel_id':hid,'city_id':hh.get(hid,{}).get('city_id','sochi'),
                'hotel_name':hh.get(hid,{}).get('name',''),'feature_id':fid,'family':catalogue().get(fid,{}).get('family'),
                'annotation':'разметка Codex','human_verified':False,'human_review_state':'pending',
                'canonical_full_feature_gold':gold,
                'reference_url':ref,'reference_value':r.get('reference_value',r.get('reference_label')),
                'reference_scope':r.get('reference_scope',r.get('scope_and_limitations',r.get('scope_limit',r.get('reason','')))),
                'evaluation_kind':r.get('evaluation_kind',r.get('reference_kind',r.get('measurement_kind',r.get('label','source_audit')))),
                'source_case_file':str(p),'original_case':r,
                'current_prediction_fact_ids':[f['fact_id'] for f in pred.get((hid,fid),[])],
                'current_statuses':sorted({f['status'] for f in pred.get((hid,fid),[])}),
                'warning':'Pre-audit predictions in original_case remain frozen. Current linked results can contain repairs; do not score repaired same cases as held-out.'})
    ids=[r['case_id']for r in out];assert len(ids)==len(set(ids))
    scientific_units={}
    for r in out:
        orig=r['original_case'];key=r['case_id']
        if 'Sotchi_37099.csv' in r['reference_url'] and r['feature_id'] in {'C01','C02'}:
            month=int(orig['month']);rr=next((f for f in pred.get((r['hotel_id'],r['feature_id']),[]) if f['params'].get('month')==month),None)
            if rr:
                pp=rr['params'];key=json.dumps([r['feature_id'],pp['grid_latitude'],pp['grid_longitude'],month,r['reference_url']])
        r['scientific_case_key']=key
        r['duplicate_of_case_id']=scientific_units.get(key)
        scientific_units.setdefault(key,r['case_id'])
    dest=BASE/'validation';dest.mkdir(exist_ok=True);write_jsonl(dest/'cases.jsonl',out)
    fields=['case_id','hotel_id','city_id','hotel_name','feature_id','family','annotation','human_verified',
            'human_review_state','canonical_full_feature_gold','reference_url','reference_value','reference_scope',
            'evaluation_kind','source_case_file','original_case','current_prediction_fact_ids','current_statuses',
            'scientific_case_key','duplicate_of_case_id','warning']
    with (dest/'cases.csv').open('w')as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in out:w.writerow({k:json.dumps(v,ensure_ascii=False)if isinstance(v,(dict,list))else v for k,v in r.items()})
    required={'V01','V05','S01','N02','N04','W01','W05','L01','H03','E03','C01','B02'}
    summary={'at':now(),'case_count':len(out),'distinct_scientific_contexts':len(scientific_units),
        'duplicate_context_rows':len(out)-len(scientific_units),
        'canonical_full_feature_gold_count':sum(r['canonical_full_feature_gold']for r in out),
        'human_verified_count':0,'feature_ids':sorted({r['feature_id']for r in out}),
        'families_with_cases':sorted({r['family']for r in out if r['family']}),
        'missing_mandatory_features':sorted(required-{r['feature_id']for r in out}),'missing_input_panels':missing,
        'numeric_checks_excluded_from_reference_case_count':True,
        'acceptance_limitation':'Case count is audit workload, not a gold sample size. Full-feature precision/recall/distance error require independent same-scope references, currently missing for most contracts.'}
    (dest/'cases_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
