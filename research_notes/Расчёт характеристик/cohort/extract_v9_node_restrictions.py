#!/usr/bin/env python3
"""Recover pure restriction/barrier nodes omitted by V4's feature-key selection.

Streams the already cached PBF with a node-only filter, without location indexes,
network calls or changes to original artifacts. Nodes having any V4 extraction key
are excluded here to avoid duplicate records when appended to that snapshot.
"""
import argparse,json,sys,time,hashlib
from pathlib import Path
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE/'scripts'))
from common import V4,CITY_V4,now
import osmium
from shapely.geometry import Point

V4KEYS={'tourism','building','building:part','highway','amenity','shop','natural','railway','public_transport','disused:tourism','abandoned:tourism','construction:tourism','leisure','healthcare','entrance'}
CONFIG={
 'sochi':('russia_southern-fed-district.osm.pbf',[39.15,43.35,40.5,44.05]),
 'moscow':('russia_central-fed-district.osm.pbf',[36.785,55.105,38.005,56.045]),
 'spb':('russia_northwestern-fed-district.osm.pbf',[29.385,59.585,30.815,60.265]),
 'kazan':('russia_volga-fed-district.osm.pbf',[48.785,55.595,49.415,56.015]),
 'kaliningrad':('russia_kaliningrad.osm.pbf',[20.285,54.605,20.665,54.835]),
 'minsk':('belarus.osm.pbf',[27.335,53.755,27.765,54.035]),
 'istanbul':('turkey.osm.pbf',[28.485,40.785,29.465,41.365]),
 'dubai':('gcc-states.osm.pbf',[54.835,24.735,55.665,25.415]),
}
def run(city):
    pbf,b=CONFIG[city];src=V4/'raw'/pbf;out=BASE/'cohort'/f'osm_{city}_pure_restriction_nodes.jsonl';start=time.perf_counter();n=0;reader=osmium.io.Reader(str(src));epoch=reader.header().get('osmosis_replication_timestamp');reader.close()
    existing_keys=V4KEYS if city!='sochi' else {'tourism','building','building:part','highway','amenity','shop','natural','railway','public_transport','leisure','healthcare','entrance','office','lit','barrier','landuse','man_made'}
    proc=osmium.FileProcessor(str(src)).with_filter(osmium.filter.EntityFilter(osmium.osm.NODE)).with_filter(osmium.filter.KeyFilter('barrier','access','foot','locked'))
    with out.open('w') as f:
      for obj in proc:
        if not obj.is_node() or not obj.location.valid():continue
        tags=dict(obj.tags)
        if any(k in tags for k in existing_keys):continue
        x,y=obj.location.lon,obj.location.lat
        if not b[0]<=x<=b[2] or not b[1]<=y<=b[3]:continue
        r={'osm_type':'node','osm_id':obj.id,'geometry_wkb':Point(x,y).wkb_hex,'tags':tags,'version':obj.version,'timestamp':str(obj.timestamp),'nodes':None}
        f.write(json.dumps(r,ensure_ascii=False)+'\n');n+=1
    m={'city_id':city,'source_file':str(src),'source_date':epoch,'snapshot_file':str(out),'bbox':b,'records':n,'bytes':out.stat().st_size,'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'seconds':time.perf_counter()-start,'built_at':now(),'license':'ODbL1.0','license_url':'https://www.openstreetmap.org/copyright','new_http_requests':0,'method':'Node-only PBF filter barrier/access/foot/locked; exclude all nodes selected by V4KEYS. No geometry/identity inference.'}
    out.with_suffix('.manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n');print(json.dumps(m,ensure_ascii=False),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--city',required=True,choices=list(CONFIG));run(p.parse_args().city)
