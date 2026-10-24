#!/usr/bin/env python3
"""Small streaming marine candidates for cache prewarming, never final facts.

Reads only cached beach/coastline geometries, uses the accepted marine gate and
selects nearest qualified polygon per hotel. Final walk-selected protocol may
select another polygon; the environment owner must wait for final target SHA.
"""
import json,time,argparse
from pathlib import Path
from shapely import wkb,STRtree
from shapely.geometry import Point
from shapely.ops import transform
from pyproj import Transformer,CRS
from common import BASE,V4,CITY_V4,read_jsonl,write_jsonl,hotels,sha256,now
from geo import CENTERS,marine_beach_classification,osm_source
def run(city):
    start=time.perf_counter();ck=CITY_V4.get(city,city);manifest=json.loads((V4/'manifests'/f'osm_{ck}_complete.json').read_text());lon,lat=CENTERS[city];tr=Transformer.from_crs(4326,CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m'),always_xy=True);coast=[];beaches=[];seen=set()
    for path in [V4/'raw'/f'osm_{ck}.jsonl',V4/'raw'/f'osm_{ck}_closed_roads.jsonl']:
      if not path.exists():continue
      with path.open() as f:
        for line in f:
          if '"beach"' not in line and '"coastline"' not in line:continue
          r=json.loads(line);kind=r['tags'].get('natural')
          if kind not in {'beach','coastline'}:continue
          sid=f"{r['osm_type']}/{r['osm_id']}";key=(sid,bool(r.get('nodes')))
          if key in seen:continue
          seen.add(key);g=wkb.loads(r['geometry_wkb'],hex=True);pg=transform(tr.transform,g);rr={'record':r,'geometry':pg,'point':pg.representative_point(),'sid':sid,'tags':r['tags'],'source_geometry':g}
          (beaches if kind=='beach' else coast).append(rr)
    tree=STRtree([r['geometry'] for r in coast]) if coast else None;qualified=[]
    for b in beaches:
        c=marine_beach_classification(b,coast,tree,manifest)
        if c['accepted'] and b['tags'].get('access') not in {'private','no','customers'}:qualified.append((b,c))
    out=[]
    for h in hotels(city):
        if not qualified:continue
        p=Point(*tr.transform(h['longitude'],h['latitude']));b,c=min(qualified,key=lambda bc:p.distance(bc[0]['geometry']));g=b['source_geometry'];q=g.representative_point();out.append({'hotel_id':h['hotel_id'],'city_id':city,'beach_id':b['sid'],'beach_name':b['tags'].get('name'),'beach_tags':b['tags'],'beach_kind':'sea','marine_classification':c,'beach_geometry_wkb':g.wkb_hex,'beach_reference_lon_lat':[q.x,q.y],'route_id':None,'entry_lon_lat':None,'confirmed_public_entrance':False,'source':osm_source(b['record'],manifest),'selection_protocol':'prewarm_nonfinal_candidates','selection_method':'nearest_marine_polygon_fallback','selection_distance_m':p.distance(b['geometry']),'prewarm_only':True,'not_for_publishing_feature_facts':'Final walk-selected/supplemental target SHA must be supplied separately.'})
    path=BASE/'qa/geo'/f'{city}_prewarm_marine_candidate_targets.jsonl';write_jsonl(path,out);result={'city_id':city,'created_at':now(),'file':str(path),'sha256':sha256(path),'rows':len(out),'qualified_polygons':len(qualified),'coastline_segments':len(coast),'selection_protocol':'prewarm_nonfinal_candidates','no_network':True,'seconds':time.perf_counter()-start};path.with_suffix('.manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--city',required=True,choices=['spb','kaliningrad','istanbul','dubai']);run(p.parse_args().city)
