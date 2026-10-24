"""Offline integration/coverage audit for V9.

Usage: python integrate.py --cities sochi
Then, only after the Sochi checkpoint: --cities sochi moscow spb kazan
kaliningrad minsk istanbul dubai. Does not execute calculators or use HTTP.
"""
import argparse,csv,json,math
from collections import Counter,defaultdict
from pathlib import Path
from common import BASE,catalogue,hotels,read_jsonl,write_jsonl,sha256,now

VALID={'observed','computed','estimate','ostrovok_content','unknown','not_applicable'}
ORDER=['computed','observed','estimate','ostrovok_content','not_applicable','unknown']

def finite(x):
    if isinstance(x,float):return math.isfinite(x)
    if isinstance(x,dict):return all(finite(v) for v in x.values())
    if isinstance(x,list):return all(finite(v) for v in x)
    return True

def main(cities):
    cat=catalogue();fids={fid for fid,f in cat.items() if f['recipes'][0]['data_kind'] in {'А','Б'}}
    assert len(fids)==54
    hh=[h for h in hotels() if h['city_id'] in cities]; byhotel={h['hotel_id']:h for h in hh}
    rows=[];inputs=[];errors=[];warnings=[]
    for city in cities:
        for family in ['geo','facades','environment']:
            p=BASE/f'results/{family}/{city}_facts.jsonl'
            if not p.exists():raise FileNotFoundError(p)
            local=list(read_jsonl(p));rows.extend(local)
            inputs.append({'path':str(p),'sha256':sha256(p),'bytes':p.stat().st_size,'rows':len(local)})
        p=BASE/f'results/facades/{city}_building_facts.jsonl'
        if p.exists():
            local=list(read_jsonl(p));rows.extend(local)
            inputs.append({'path':str(p),'sha256':sha256(p),'bytes':p.stat().st_size,'rows':len(local)})
    # Building facts are also present in the per-hotel logical view for strict
    # single-building matches. Deduplicate identical exports, reject conflicting
    # payloads with the same fact identity.
    unique={};export_duplicates=0
    for r in rows:
        ident=r.get('fact_id')
        if ident in unique:
            if r==unique[ident]:export_duplicates+=1
            else:errors.append(['conflicting_export_fact_id',ident])
        else:unique[ident]=r
    rows=list(unique.values())
    ids=set();pairs=defaultdict(list)
    for r in rows:
        hid=str(r.get('hotel_id'));fid=r.get('feature_id');st=r.get('status')
        if hid not in byhotel:errors.append(['unknown_hotel',hid])
        if fid not in fids:errors.append(['non_54_feature',fid])
        if st not in VALID:errors.append(['status',hid,fid,st])
        if not finite(r):errors.append(['non_finite',hid,fid])
        if st=='unknown' and (not r.get('unknown_reason') or r.get('value') is not None):errors.append(['invalid_unknown',hid,fid])
        if st in {'computed','observed','estimate'} and r.get('value') is None:errors.append(['missing_value',hid,fid])
        if st in {'computed','observed','estimate'} and not r.get('sources'):errors.append(['missing_sources',hid,fid])
        if not r.get('recipe_attempts'):errors.append(['missing_recipe_trace',hid,fid])
        if st in {'computed','observed','estimate'} and not r.get('recipe_id'):errors.append(['missing_selected_recipe',hid,fid])
        if st in {'computed','observed','estimate'}:
            selected=r.get('recipe_id');canonical={x['recipe_id'] for x in cat.get(fid,{}).get('recipes',[])}
            if selected in canonical:
                if not any(a.get('recipe_id')==selected and a.get('state')=='accepted' for a in r.get('recipe_attempts',[])):
                    errors.append(['selected_recipe_not_accepted',hid,fid,selected])
            elif not (st=='estimate' and selected=='W04-D18-DSM' and not r.get('hard_condition_eligible',True)):
                errors.append(['unrecognized_recipe_extension',hid,fid,selected])
        if not r.get('calculated_at'):errors.append(['missing_calculated_at',hid,fid])
        ident=r.get('fact_id')
        if ident in ids:errors.append(['duplicate_fact_id',ident])
        ids.add(ident);pairs[(hid,fid)].append(r)
    for hid in byhotel:
        missing=fids-{fid for h,fid in pairs if h==hid}
        if missing:errors.append(['missing_hotel_features',hid,sorted(missing)])
    coverage=[];city_summary=[]
    for city in cities:
        ch=[h for h in hh if h['city_id']==city]; totals=Counter()
        for fid in sorted(fids):
            counts=Counter();rowcounts=Counter();all_issued_known=0
            for h in ch:
                rr=pairs[(h['hotel_id'],fid)];statuses={r['status'] for r in rr}
                best=next(s for s in ORDER if s in statuses);counts[best]+=1;rowcounts.update(r['status'] for r in rr)
                all_issued_known+=bool(rr) and not any(r['status'] in {'unknown','estimate','ostrovok_content'} for r in rr)
            totals.update(counts)
            coverage.append({'city_id':city,'feature_id':fid,'feature_name':cat[fid]['name'],'N_hotels':len(ch),
                **{f'{s}_n':counts[s] for s in ORDER},'all_issued_rows_resolved_n':all_issued_known,
                'fact_rows':sum(rowcounts.values()),'fact_row_status_counts':json.dumps(rowcounts,sort_keys=True),
                'denominator_note':'one hotel counted once by best admissible status at at least one stated subject/parameter; does not imply all buildings, windows, routes, dates or room categories are resolved'})
        city_summary.append({'city_id':city,'N_hotels':len(ch),'features_attempted':54,
            'features_with_computed_or_observed':sum(r['computed_n']+r['observed_n']>0 for r in coverage if r['city_id']==city),
            'features_with_estimate':sum(r['estimate_n']>0 for r in coverage if r['city_id']==city),
            'hotel_feature_pairs':len(ch)*54,'best_status_counts':dict(totals),
            'unknown_share':totals['unknown']/(len(ch)*54)})
    out=BASE/'integrated';out.mkdir(exist_ok=True)
    suffix='sochi' if cities==['sochi'] else 'all8'
    write_jsonl(out/f'{suffix}_facts.jsonl',rows)
    with (out/f'{suffix}_coverage.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(coverage[0]));w.writeheader();w.writerows(coverage)
    summary={'at':now(),'cities':cities,'cohort_sha256':sha256(BASE/'cohort/hotels.jsonl'),
             'fact_rows':len(rows),'identical_logical_export_duplicates_removed':export_duplicates,
             'hotel_feature_pairs':len(pairs),'N_hotels':len(hh),
             'city_summary':city_summary,'unknown_reasons':dict(Counter(r['unknown_reason'] for r in rows if r['status']=='unknown')),
             'input_files':inputs,'errors':errors,'warnings':warnings,'status':'PASSED' if not errors else 'FAILED',
             'coverage_semantics':'any stated subject/parameter, not whole hotel/category guarantee'}
    (out/f'{suffix}_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k not in ['input_files','unknown_reasons','errors']},ensure_ascii=False))
    if errors:print(json.dumps({'error_count':len(errors),'first_errors':errors[:5]},ensure_ascii=False))
    if errors:raise SystemExit(1)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--cities',nargs='+',required=True);main(p.parse_args().cities)
