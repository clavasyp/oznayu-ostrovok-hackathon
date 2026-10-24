"""Independent arithmetic checks on saved V9 outputs, not field ground truth.

N02: a different projection plus ellipsoidal distance of nearest endpoints.
C01/C02/C03: re-aggregate original daily responses without calculator helpers.
No HTTP, no writes to calculator outputs or old research directories.
"""
import json,statistics
from collections import defaultdict
from pathlib import Path
from pyproj import CRS,Transformer,Geod
from shapely import wkb
from shapely.ops import transform,nearest_points
from common import BASE,read_jsonl,write_jsonl,now

def main():
    results=[]
    gf=BASE/'results/geo/sochi_facts.jsonl'; ef=BASE/'results/environment/sochi_facts.jsonl'
    geo=list(read_jsonl(gf)); env=list(read_jsonl(ef))
    all_n=[r for r in geo if r['feature_id']=='N02' and r['status']=='computed']
    nr=all_n[:10]
    # The report's multi-building flagship deserves an explicit check of every
    # matched Radisson body, beyond the deterministic first-ten audit panel.
    used={r['fact_id'] for r in nr}
    nr.extend(r for r in all_n if r['hotel_id']=='13736470' and r['fact_id'] not in used)
    wanted={r['subject_id'] for r in nr}|{r['value']['target_id'] for r in nr}; shapes={}
    for r in read_jsonl(BASE/'cohort/osm_sochi_extended.jsonl'):
        key=f"{r['osm_type']}/{r['osm_id']}"
        if key in wanted:shapes[key]=wkb.loads(bytes.fromhex(r['geometry_wkb']))
    to_utm=Transformer.from_crs('EPSG:4326','EPSG:32637',always_xy=True).transform
    from_utm=Transformer.from_crs('EPSG:32637','EPSG:4326',always_xy=True).transform
    geod=Geod(ellps='WGS84')
    for r in nr:
        a,b=nearest_points(transform(to_utm,shapes[r['subject_id']]),transform(to_utm,shapes[r['value']['target_id']]))
        a,b=transform(from_utm,a),transform(from_utm,b)
        check=geod.inv(a.x,a.y,b.x,b.y)[2];delta=abs(check-r['value']['distance_m'])
        results.append({'case_id':'numeric_'+r['fact_id'],'feature_id':'N02','hotel_id':r['hotel_id'],
                        'production_value_m':r['value']['distance_m'],'independent_arithmetic_m':check,
                        'absolute_difference_m':delta,'tolerance_m':1.0,'pass':delta<1.0,
                        'reference_kind':'same OSM input, different projection and ellipsoidal check; NOT independent-source gold',
                        'fact_id':r['fact_id']})
    # Deduplicate hotel outputs sharing the same grid, feature and month.
    selected={}
    for r in env:
        if r['feature_id'] in {'C01','C02','C03'} and r['status']=='computed':
            p=r['params'];selected.setdefault((r['feature_id'],p['grid_latitude'],p['grid_longitude'],p['month']),r)
    raw_cache={}
    for key,r in selected.items():
        annual=[]
        for src in r['sources']:
            path=Path(src['snapshot_file'])
            if path not in raw_cache:raw_cache[path]=json.loads(path.read_text())
            doc=raw_cache[path];pointer=src.get('json_pointer','')
            if isinstance(doc,list):doc=doc[int(pointer.strip('/') or 0)]
            daily=doc['daily'];fid=r['feature_id']
            if fid=='C01':value=statistics.mean(daily['temperature_2m_mean'])
            elif fid=='C02':value=sum(x>=r['params']['threshold_mm_per_day'] for x in daily['precipitation_sum'])
            else:
                threshold=r['params'].get('threshold_c',r['params'].get('threshold_C',r['params'].get('threshold_tmax_C',30)))
                value=sum(x>threshold for x in daily['temperature_2m_max'])
            annual.append(value)
        check=statistics.mean(annual);delta=abs(check-r['value']['mean'])
        results.append({'case_id':'numeric_'+r['fact_id'],'feature_id':r['feature_id'],'hotel_id':r['hotel_id'],
                        'month':r['params']['month'],'grid':[r['params']['grid_latitude'],r['params']['grid_longitude']],
                        'production_value':r['value']['mean'],'independent_arithmetic':check,
                        'absolute_difference':delta,'n_years':len(annual),'pass':delta<1e-9 and len(annual)==30,
                        'reference_kind':'same API daily input, independent reducer; NOT independent-source gold',
                        'fact_id':r['fact_id']})
    out=BASE/'validation';out.mkdir(exist_ok=True);write_jsonl(out/'numerical_checks.jsonl',results)
    summary={'at':now(),'checks':len(results),'passed':sum(r['pass'] for r in results),
             'failed':[r for r in results if not r['pass']],
             'interpretation':'Arithmetic and numerical reproducibility only. Does not measure physical accuracy, recall, or source completeness.'}
    (out/'numerical_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False))
    if summary['failed']:raise SystemExit(1)

if __name__=='__main__':main()
