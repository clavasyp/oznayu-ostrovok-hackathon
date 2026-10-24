"""Station-normal comparisons, explicitly different spatial supports.

This measures station minus reanalysis-cell disagreement, not calculator error
or a hotel's weather. Restrict audit to coastal Sochi and Adler cells.
"""
import csv,json
from common import BASE,read_jsonl,hotels

def main():
    ref=json.loads((BASE/'validation/station_reference.json').read_text());rows=[];seen=set()
    hh={h['hotel_id']:h for h in hotels('sochi')}
    for r in read_jsonl(BASE/'results/environment/sochi_facts.jsonl'):
        fid=r['feature_id'];p=r['params']
        if fid not in {'C01','C02'} or r['status']!='computed':continue
        if (p['grid_latitude'],p['grid_longitude']) not in {(43.5,39.75),(43.5,40.0)}:continue
        key=(fid,p['grid_latitude'],p['grid_longitude'],p['month'])
        if key in seen:continue
        seen.add(key);code='5' if fid=='C01' else '2';month='july' if p['month']==7 else 'november'
        station=ref['values'][code][month];model=r['value']['mean']
        rows.append({'case_id':f'STATION_{fid}_{p["grid_latitude"]}_{p["grid_longitude"]}_{p["month"]}',
            'hotel_id':r['hotel_id'],'hotel_name':hh[r['hotel_id']]['name'],'feature_id':fid,
            'model_value':model,'reference_value':station,'difference_model_minus_station':model-station,
            'absolute_difference':abs(model-station),'unit':r['unit'],'month':p['month'],
            'model_grid_lat':p['grid_latitude'],'model_grid_lon':p['grid_longitude'],
            'station_id':'37099','period':'1991-2020','station_height_m':142,
            'reference_url':'https://www.nodc.noaa.gov/archive/arc0216/0253808/4.4/data/0-data/Region-6-WMO-Normals-9120/RussianFederation/CSV/Sotchi_37099.csv',
            'reference_kind':'independent station observations; different spatial support and potentially daily boundaries',
            'annotator':'разметка Codex','canonical_gold_eligible':False,
            'measurement_kind':'representativeness_difference_not_implementation_error',
            'human_review_state':'pending','fact_id':r['fact_id']})
    out=BASE/'validation'
    with (out/'station_comparison.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    summary={fid:{'cases':len(rr:=[r for r in rows if r['feature_id']==fid]),
                  'unique_station_month_reference_values':2,
                  'mean_absolute_station_cell_difference':sum(r['absolute_difference']for r in rr)/len(rr),
                  'maximum_absolute_station_cell_difference':max(r['absolute_difference']for r in rr),
                  'unit':rr[0]['unit']}for fid in ['C01','C02']}
    (out/'station_summary.json').write_text(json.dumps({'summary':summary,
        'limitation':'Four cell-month cases per feature share one station and two monthly references. Not four independent station samples; not pure model error and not field validation of hotel climate.'},ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
