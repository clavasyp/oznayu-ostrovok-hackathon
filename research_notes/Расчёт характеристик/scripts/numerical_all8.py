"""Eight-city independent arithmetic audit of original saved inputs.

Checks multi-location API pointers and projected building distances in each city.
This is not independent-source truth, a labelled hotel sample, or physical accuracy.
"""
import json,statistics,math
from collections import Counter
from pathlib import Path
from pyproj import Transformer,Geod
from shapely import wkb
from shapely.ops import transform,nearest_points
from common import BASE,V4,CITY_V4,read_jsonl,write_jsonl,hotels,now

CITIES='sochi moscow spb kazan kaliningrad minsk istanbul dubai'.split()

def main():
    results=[];skips=[];geod=Geod(ellps='WGS84')
    for city in CITIES:
        # One result per feature/grid/month, not one independent sample per hotel.
        selected={}
        for r in read_jsonl(BASE/f'results/environment/{city}_facts.jsonl'):
            if r['feature_id'] in {'C01','C02','C03'} and r['status']=='computed':
                p=r['params'];selected.setdefault((r['feature_id'],p['grid_latitude'],p['grid_longitude'],p['month']),r)
        cache={}
        for key,r in selected.items():
            annual=[];source_years=[];fid=r['feature_id']
            for src in r['sources']:
                path=Path(src['snapshot_file'])
                if path not in cache:cache[path]=json.loads(path.read_text())
                doc=cache[path]
                if isinstance(doc,list):doc=doc[int(src['json_pointer'].strip('/'))]
                d=doc['daily'];days=len(d['time']);month=r['params']['month']
                assert abs(doc['latitude']-r['params']['grid_latitude'])<1e-4
                assert abs(doc['longitude']-r['params']['grid_longitude'])<1e-4
                assert days==(31 if month==7 else 30)
                assert all(int(x[5:7])==month for x in d['time'])
                years={int(x[:4]) for x in d['time']};assert len(years)==1
                source_years.extend(years)
                if fid=='C01':v=statistics.mean(d['temperature_2m_mean'])
                elif fid=='C02':v=sum(x>=1 for x in d['precipitation_sum'])
                else:v=sum(x>30 for x in d['temperature_2m_max'])
                annual.append(v)
            assert sorted(source_years)==list(range(1991,2021))
            reference=statistics.mean(annual);delta=abs(reference-r['value']['mean'])
            results.append({'city_id':city,'feature_id':fid,'fact_id':r['fact_id'],
                'grid_month':list(key[1:]),'value':r['value']['mean'],'arithmetic_reference':reference,
                'absolute_difference':delta,'n_years':len(annual),'pass':len(annual)==30 and delta<1e-9,
                'kind':'same daily API data, independent reducer; not physical gold'})
        del cache
        sample=[r for r in read_jsonl(BASE/f'results/geo/{city}_facts.jsonl')
                if r['feature_id']=='N02' and r['status']=='computed'][:10]
        wanted={r['subject_id'] for r in sample}|{r['value']['target_id'] for r in sample};shapes={}
        key=CITY_V4.get(city,city)
        files=[BASE/'cohort/osm_sochi_extended.jsonl'] if city=='sochi' else [V4/f'raw/osm_{key}.jsonl',V4/f'raw/osm_{key}_closed_roads.jsonl']
        for path in files:
            if not path.exists():continue
            for rr in read_jsonl(path):
                sid=f"{rr['osm_type']}/{rr['osm_id']}"
                if sid in wanted:shapes[sid]=wkb.loads(rr['geometry_wkb'],hex=True)
        if not sample:skips.append({'city_id':city,'feature_id':'N02','reason':'no accepted building-distance output'})
        hh={h['hotel_id']:h for h in hotels(city)}
        for r in sample:
            hid=r['hotel_id'];lon=hh[hid]['longitude'];lat=hh[hid]['latitude']
            zone=int(math.floor((lon+180)/6))+1;epsg=(32600 if lat>=0 else 32700)+zone
            to_utm=Transformer.from_crs(4326,epsg,always_xy=True).transform
            inv=Transformer.from_crs(epsg,4326,always_xy=True).transform
            a,b=nearest_points(transform(to_utm,shapes[r['subject_id']]),transform(to_utm,shapes[r['value']['target_id']]))
            a,b=transform(inv,a),transform(inv,b)
            reference=geod.inv(a.x,a.y,b.x,b.y)[2];delta=abs(reference-r['value']['distance_m'])
            results.append({'city_id':city,'feature_id':'N02','fact_id':r['fact_id'],
                'value':r['value']['distance_m'],'arithmetic_reference':reference,
                'absolute_difference':delta,'tolerance_m':1,'projection_EPSG':epsg,'pass':delta<1,
                'kind':'same OSM geometry, independent UTM nearest endpoints plus ellipsoidal distance; not physical gold'})
    out=BASE/'validation';write_jsonl(out/'numerical_all8_checks.jsonl',results)
    summary={'at':now(),'checks':len(results),'passed':sum(r['pass'] for r in results),
        'by_city':dict(Counter(r['city_id'] for r in results)),
        'by_feature':dict(Counter(r['feature_id'] for r in results)),
        'maximum_N02_difference_m':max((r['absolute_difference'] for r in results if r['feature_id']=='N02'),default=None),
        'failed':[r for r in results if not r['pass']],'skips':skips,
        'scope':'Arithmetic reproducibility on the same original inputs. Not independent-source gold, source completeness, feature precision/recall or physical distance error.'}
    (out/'numerical_all8_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))
    if summary['failed']:raise SystemExit(1)

if __name__=='__main__':main()
