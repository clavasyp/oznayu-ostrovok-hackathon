"""Stream all V9 facade/floor passports; no large scene or HTTP is loaded.

Structural/source validation only. A complete fan does not establish real windows.
"""
import gzip,json
from collections import Counter
from common import BASE,sha256,now

CITIES='sochi moscow spb kazan kaliningrad minsk istanbul dubai'.split()

def main():
    results=[];errors=[]
    for city in CITIES:
        p=BASE/f'results/facades/{city}_fan_facts.jsonl.gz'
        summary=json.loads((p.parent/f'{city}_summary.json').read_text())
        ids=set();counts=Counter();n=0;local=[]
        with gzip.open(p,'rt') as fh:
            for line in fh:
                r=json.loads(line);n+=1;fid=r['feature_id'];counts[fid]+=1
                if r['fact_id'] in ids:local.append(['duplicate_fact_id',r['fact_id']])
                ids.add(r['fact_id'])
                if r['city_id']!=city:local.append(['wrong_city',r['fact_id']])
                if r['status']!='unknown' or r['value'] is not None or not r.get('unknown_reason'):
                    local.append(['unsupported_actual_window_claim',r['fact_id']])
                if not r.get('recipe_attempts') or not r.get('sources') or not r.get('calculated_at'):
                    local.append(['missing_passport_fields',r['fact_id']])
                if r['subject_level']!='facade_physical_floor':local.append(['subject_scope',r['fact_id'],r['subject_level']])
        if n!=summary['full_passport_fan_facts'] or n!=summary['fan_cells']*25:
            local.append(['summary_count_mismatch',n,summary['full_passport_fan_facts'],summary['fan_cells']])
        errors.extend([[city,*e] for e in local])
        results.append({'city_id':city,'passports':n,'unique_ids':len(ids),'features':dict(counts),
            'fan_cells':summary['fan_cells'],'file':str(p),'sha256':sha256(p),'bytes':p.stat().st_size,
            'passed':not local})
    output={'at':now(),'passed':not errors,'cities':results,'errors':errors,
            'total_passports':sum(r['passports'] for r in results),
            'scope':'All issued compressed fan passports checked. Structural reproducibility, not physical accuracy or evidence that an existing window is at a synthetic facade point.'}
    dest=BASE/'qa/root/all8_fan_audit.json';dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':output['passed'],'total_passports':output['total_passports'],'errors':errors[:5]}))
    if errors:raise SystemExit(1)

if __name__=='__main__':main()
