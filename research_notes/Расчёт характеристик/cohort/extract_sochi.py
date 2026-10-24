#!/usr/bin/env python3
"""Extract full D28 cached south-federal PBF to V9 only; no download."""
import json,sys,time,hashlib
from pathlib import Path
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE/'scripts'))
from common import V4,now
import osmium
from shapely import wkb
from shapely.geometry import Point,box
KEYS=['tourism','building','building:part','highway','amenity','shop','natural','railway','public_transport','leisure','healthcare','entrance','office','lit','barrier','landuse','man_made']
def main():
    p=V4/'raw/russia_southern-fed-district.osm.pbf';out=BASE/'cohort/osm_sochi_extended.jsonl';bounds=(39.15,43.35,40.5,44.05);bbox=box(*bounds);fac=osmium.geom.WKBFactory();start=time.monotonic();count=errors=0
    reader=osmium.io.Reader(str(p));source_date=reader.header().get('osmosis_replication_timestamp');reader.close()
    proc=osmium.FileProcessor(str(p)).with_locations('sparse_mem_array').with_areas(osmium.filter.KeyFilter(*KEYS)).with_filter(osmium.filter.KeyFilter(*KEYS))
    with out.open('w') as f:
      for obj in proc:
        try:
          if obj.is_relation():continue
          if obj.is_node():
            if not obj.location.valid():continue
            x,y=obj.location.lon,obj.location.lat
          elif obj.is_area():
            ring=next(iter(obj.outer_rings()),None)
            if ring is None or not ring[0].location.valid():continue
            x,y=ring[0].lon,ring[0].lat
          else:
            if obj.is_closed() and 'highway' not in obj.tags:continue
            if not obj.nodes or not obj.nodes[0].location.valid():continue
            x,y=obj.nodes[0].lon,obj.nodes[0].lat
          # Same first-vertex limitation as V4, explicitly recorded below.
          if not bounds[0]<=x<=bounds[2] or not bounds[1]<=y<=bounds[3]:continue
          tags=dict(obj.tags)
          if obj.is_node():g=Point(x,y);typ='node';oid=obj.id
          elif obj.is_area():g=wkb.loads(fac.create_multipolygon(obj),hex=True);typ='way' if obj.from_way() else 'relation';oid=obj.orig_id()
          else:g=wkb.loads(fac.create_linestring(obj),hex=True);typ='way';oid=obj.id
          if g.is_empty or not g.intersects(bbox):continue
          nds=[[n.ref,n.lon,n.lat] for n in obj.nodes if n.location.valid()] if obj.is_way() and 'highway' in tags else None
          r={'osm_type':typ,'osm_id':oid,'geometry_wkb':g.wkb_hex,'tags':tags,'version':obj.version,'timestamp':str(obj.timestamp),'nodes':nds}
          f.write(json.dumps(r,ensure_ascii=False)+'\n');count+=1
        except Exception:errors+=1
    m={'built_at':now(),'source_file':str(p),'source_date':source_date,'bbox':bounds,'rows':count,'geometry_errors_region':errors,'seconds':time.monotonic()-start,'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'bytes':out.stat().st_size,'license':'ODbL1.0','license_url':'https://www.openstreetmap.org/copyright','new_network_requests':0,'limitations':'First vertex candidate filter can miss large polygons crossing bbox with starting vertex outside; ordinary local roads/buildings within bbox covered; not complete official municipal inventory.'}
    (BASE/'cohort/osm_sochi_extended_manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n');print(json.dumps(m,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
