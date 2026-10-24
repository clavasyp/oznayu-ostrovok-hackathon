#!/usr/bin/env python3
"""Decouple a physical marine reference polygon from pedestrian connectivity.

Preserves existing walk-selected marine beaches; only missing hotel targets receive
the closest explicitly marine-qualified non-private mapped polygon. It is not a
claim that this beach is accessible, affiliated with the hotel, or open in season.
Writes supplemental outputs only, including for the frozen historical Sochi run.
"""
import argparse,json,time
from pathlib import Path
from shapely import wkb
from shapely.geometry import Point
from shapely.ops import transform as transform_geometry
from pyproj import Transformer,CRS
from common import BASE,V4,CITY_V4,hotels,read_jsonl,write_jsonl,sha256,now
from geo import CENTERS,COAST
def run(city):
    start=time.perf_counter();d=BASE/'results/geo';source=d/f'{city}_beach_targets.jsonl';classification_file=d/f'{city}_beach_classifications.jsonl';before={r['hotel_id']:r for r in read_jsonl(source)};classes={r['beach_id']:r for r in read_jsonl(classification_file) if r.get('accepted') and r.get('access') not in {'private','no','customers'}}
    lon,lat=CENTERS[city];tr=Transformer.from_crs(4326,CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m'),always_xy=True);geometries={}
    paths=[BASE/'cohort/osm_sochi_extended.jsonl'] if city=='sochi' else [V4/'raw'/f'osm_{CITY_V4.get(city,city)}.jsonl',V4/'raw'/f'osm_{CITY_V4.get(city,city)}_closed_roads.jsonl']
    if classes:
      for path in paths:
        if not path.exists():continue
        with path.open() as f:
          for line in f:
            if '"beach"' not in line:continue
            r=json.loads(line)
            if r['tags'].get('natural')!='beach':continue
            sid=f"{r['osm_type']}/{r['osm_id']}"
            if sid not in classes or sid in geometries:continue
            g=wkb.loads(r['geometry_wkb'],hex=True);geometries[sid]={'geometry':g,'projected':transform_geometry(tr.transform,g),'record':r}
    out=[];delta=[]
    for h in hotels(city):
        if h['hotel_id'] in before:
            previous=before[h['hotel_id']];g=wkb.loads(previous['beach_geometry_wkb'],hex=True);p=Point(*tr.transform(h['longitude'],h['latitude']));distance=p.distance(transform_geometry(tr.transform,g));q=g.representative_point()
            out.append({**previous,'selection_method':'walk_selected_marine','selection_distance_m':distance,'selection_point_to_polygon_distance_m':distance,'selection_protocol':'v9.2_marine_context','beach_reference_lon_lat':[q.x,q.y],'selection_rule':'preserve accepted walk-selected marine polygon; selection_distance_m is direct hotelpoint-to-polygon distance, not the walking route length; B02 uses polygon only, no claim of public entrance or access','marine_reference_purpose':'Physical water-cell reference independent of legal beach access','reference_target_is_accessible_beach_claim':False});continue
        if city not in COAST or not geometries:continue
        p=Point(*tr.transform(h['longitude'],h['latitude']));sid=min(geometries,key=lambda sid:p.distance(geometries[sid]['projected']));b=geometries[sid];g=b['geometry'];q=g.representative_point();c=classes[sid]
        r={'hotel_id':h['hotel_id'],'city_id':city,'beach_id':sid,'beach_name':b['record']['tags'].get('name'),'beach_tags':b['record']['tags'],'beach_kind':'sea','marine_classification':c,'beach_geometry_wkb':g.wkb_hex,'beach_geometry_wkb_projected':b['projected'].wkb_hex,'beach_reference_lon_lat':[q.x,q.y],'route_id':None,'entry_candidate_id':None,'entry_lon_lat':None,'confirmed_public_entrance':False,'source':c['beach_source'],'selection_rule':'nearest accepted marine-adjacent non-private mapped polygon by minimum 2D distance from OTA hotel point in city AEQD; route connectivity not required','selection_point_to_polygon_distance_m':p.distance(b['projected']),'selection_origin':'OTA hotel point; not matched building or pedestrian entrance','marine_reference_purpose':'Physical water-cell reference only; pedestrian reachability, public admission and seasonal opening remain unknown','reference_target_is_accessible_beach_claim':False}
        r.update({'selection_method':'nearest_marine_polygon_fallback','selection_distance_m':r['selection_point_to_polygon_distance_m'],'selection_protocol':'v9.2_marine_context'})
        out.append(r);delta.append({'hotel_id':h['hotel_id'],'hotel_name':h['name'],'beach_id':sid,'marine_classification':c,'selection_distance_m':r['selection_point_to_polygon_distance_m'],'selection_point_to_polygon_distance_m':r['selection_point_to_polygon_distance_m'],'route_id':None,'entry_lon_lat':None})
    target=d/f'{city}_marine_targets_with_fallback.jsonl';write_jsonl(target,out);m={'city_id':city,'built_at':now(),'selection_rule_version':'V9-marine-reference-target-1','input_selected_targets_sha256':sha256(source),'input_classifications_sha256':sha256(classification_file),'result_sha256':sha256(target),'result_file':str(target),'script_sha256':sha256(__file__),'hotel_denominator':len(hotels(city)),'walk_selected_preserved':len(before),'new_physical_reference_only_targets':len(delta),'result_targets':len(out),'delta':delta,'network_requests':0,'seconds':time.perf_counter()-start,'no_canonical_artifacts_changed':True,'limitation':'This supplemental target is not a legal walking route or hotel partner beach. Current OSM private/no/customers tags are excluded, missing admission/season policy remains unknown.'};(BASE/'qa/geo'/f'{city}_marine_targets_with_fallback_manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n');print(json.dumps(m,ensure_ascii=False),flush=True);return m
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--city',required=True,choices=list(CENTERS));run(p.parse_args().city)
