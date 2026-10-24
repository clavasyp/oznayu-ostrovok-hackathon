#!/usr/bin/env python3
"""V9 OSM distance, mapped-count, pedestrian-route and hours calculators.

Inputs: immutable V5/V7 cohort, cached V4 OSM/PBF/DSM/GTFS and V9 building joins.
Outputs: JSONL passports, route GeoJSON, diagnostics and measured runtimes. No HTTP.
Run with V4 .venv/bin/python geo.py --city sochi; imports have no side effects.
"""
from __future__ import annotations
import argparse,collections,csv,gzip,hashlib,json,math,re,time,zipfile,functools
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
from scipy.spatial import cKDTree
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra,connected_components
from shapely import wkb,STRtree
from shapely.geometry import Point,LineString,mapping
from shapely.ops import transform as gtransform
from pyproj import CRS,Transformer
import opening_hours
from common import BASE,V4,CITY_V4,TZ,TRIPS,hotels,read_jsonl,write_jsonl,fact,attempts,now,sha256,catalogue

OWN='N01 N02 N03 N04 W01 W02 W03 W04 W05 W06 T01 T02 T03 L01 L02 L03 F01 F02 P03 P04 A01 R04 R05 Q01 Q02 Q03 X01 X02 X03'.split()
BUSY={'trunk','trunk_link','primary','primary_link','secondary','secondary_link'}
WALK={'footway','path','pedestrian','steps','living_street','residential','service','unclassified','tertiary','tertiary_link','secondary','secondary_link','primary','primary_link','road','track'}
CENTERS={'sochi':(39.7231,43.5855),'moscow':(37.6176,55.7558),'spb':(30.3158,59.9391),'kazan':(49.1064,55.7989),'kaliningrad':(20.5109,54.7104),'minsk':(27.5618,53.9023),'istanbul':(28.9784,41.0082),'dubai':(55.2708,25.2048)}
COAST={'sochi','spb','kaliningrad','istanbul','dubai'}
COUNTRY={'sochi':'RU','moscow':'RU','spb':'RU','kazan':'RU','kaliningrad':'RU','minsk':'BY','istanbul':'TR','dubai':'AE'}

def osm_id(r):return f"{r['osm_type']}/{r['osm_id']}"
def osm_source(r,manifest=None):
    r=r.get('record',r)
    return {'provider':'OpenStreetMap','url':'https://www.openstreetmap.org/'+osm_id(r),'version':r.get('version'),'observed_at':r.get('timestamp'),'date_kind':'OSM edit, not physical inspection','license':'ODbL1.0','license_url':'https://www.openstreetmap.org/copyright','dataset_version':(manifest or {}).get('source_date'),'dataset_sha256':(manifest or {}).get('sha256')}
@functools.lru_cache(maxsize=64)
def dsm_source(tile):
    fp=V4/'raw/raster_tiles'/tile
    return {'provider':'Copernicus DEM GLO-30','version':'public GLO30 COG bucket; V4 cached snapshot2026-10-23 identified by tile SHA256; native acquisition epoch and ETag unknown','sha256':sha256(fp),'url':'https://copernicus-dem-30m.s3.amazonaws.com/'+tile.removesuffix('.tif')+'/'+tile,'snapshot_file':str(fp),'license':'Copernicus DEM Licence, free worldwide GLO30 use subject to required attribution/notices','license_url':'https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM','surface_model':'DSM, not DTM','physical_observation_at':None}
def allowed(t):
    hw=t.get('highway')
    if t.get('foot') in {'no','private','use_sidepath'}:return False
    if t.get('access') in {'no','private','customers','permit','destination'} and t.get('foot') not in {'yes','designated','permissive'}:return False
    if t.get('construction') or t.get('proposed'):return False
    if t.get('foot:conditional') or t.get('access:conditional'):return False
    return hw in WALK or (hw=='cycleway' and t.get('foot') in {'yes','designated','permissive'})
def read_records(city):
    ck=CITY_V4.get(city,city)
    if city=='sochi':paths=[BASE/'cohort/osm_sochi_extended.jsonl'];mp=BASE/'cohort/osm_sochi_extended_manifest.json'
    else:paths=[V4/'raw'/f'osm_{ck}.jsonl',V4/'raw'/f'osm_{ck}_closed_roads.jsonl'];mp=V4/'manifests'/f'osm_{ck}_complete.json'
    manifest=json.loads(mp.read_text()) if mp.exists() else {};seen=set()
    for path in paths:
      if not path.exists():continue
      for r in read_jsonl(path):
        key=(osm_id(r),bool(r.get('nodes')))
        if key in seen:continue
        seen.add(key);yield r,manifest

class Graph:
    def __init__(self,city):
        self.city=city;x,y=CENTERS[city];self.tr=Transformer.from_crs(4326,CRS.from_proj4(f'+proj=aeqd +lat_0={y} +lon_0={x} +datum=WGS84 +units=m'),always_xy=True);self.inv=Transformer.from_crs(self.tr.target_crs,4326,always_xy=True)
        self.records=[];self.roads=[];self.busy=[];self.rail=[];self.beaches=[];self.pois=[];self.stops=[];self.entrances=[];self.crossing_nodes={};nodes={};roadways=[];self.manifest={};self.coverage_bounds=None
        for r,m in read_records(city):
            self.manifest=m;t=r['tags'];hw=t.get('highway');sid=osm_id(r)
            relevant=hw or t.get('amenity') or t.get('shop') or t.get('leisure') or t.get('office') or t.get('healthcare') or t.get('railway') or t.get('public_transport') or t.get('natural')=='beach' or t.get('entrance')
            if not relevant:continue
            try:g=wkb.loads(r['geometry_wkb'],hex=True);pg=gtransform(self.tr.transform,g)
            except Exception:continue
            rr={'record':r,'geometry':pg,'point':pg.representative_point(),'sid':sid,'tags':t}
            if r['osm_type']=='node' and hw=='crossing':self.crossing_nodes[r['osm_id']]=rr
            if hw in BUSY and g.geom_type in {'LineString','MultiLineString'}:self.busy.append(rr)
            if t.get('railway') in {'rail','light_rail','tram'} and not any(k in t for k in ['disused','abandoned','construction','proposed']) and not any(k.startswith(('disused:','abandoned:','construction:','proposed:')) for k in t):self.rail.append(rr)
            if t.get('natural')=='beach':self.beaches.append(rr)
            if t.get('entrance') not in {None,'no','exit'}:self.entrances.append(rr)
            if t.get('amenity') or t.get('shop') or t.get('leisure') or t.get('healthcare') or t.get('office'):self.pois.append(rr)
            if hw=='bus_stop' or t.get('railway') in {'station','halt','tram_stop','subway_entrance'} or t.get('public_transport')=='platform':self.stops.append(rr)
            if allowed(t) and r.get('nodes'):
                nd=r['nodes'];roadways.append((nd,rr));nodes.update((n[0],(n[1],n[2])) for n in nd)
        self.nids=sorted(nodes);self.index={n:i for i,n in enumerate(self.nids)}
        xy=np.array([nodes[n] for n in self.nids]);xx,yy=self.tr.transform(xy[:,0],xy[:,1]);self.xy=np.column_stack([xx,yy]);self.tree=cKDTree(self.xy);self.edges={}
        for nd,r in roadways:
          for a,b in zip(nd,nd[1:]):
            u=self.index[a[0]];v=self.index[b[0]]
            if u==v:continue
            key=(min(u,v),max(u,v));d=math.dist(self.xy[u],self.xy[v]);old=self.edges.get(key)
            if old is None or d<old['length_m']:self.edges[key]={'length_m':d,**r}
        rows=[];cols=[];dist=[]
        for (u,v),r in self.edges.items():rows.extend([u,v]);cols.extend([v,u]);dist.extend([r['length_m']]*2)
        self.A=coo_matrix((dist,(rows,cols)),shape=(len(self.nids),len(self.nids))).tocsr();self._last_source=None;self._last_dist=None;self._last_pred=None
        self.busy_tree=STRtree([r['geometry'] for r in self.busy]) if self.busy else None
        self.rail_tree=STRtree([r['geometry'] for r in self.rail]) if self.rail else None
        self.poi_tree=STRtree([r['point'] for r in self.pois]) if self.pois else None
        if self.city=='sochi':self.coverage_bounds=self.manifest.get('bbox')
        else:
            # V4 cached extraction bounds are fixed and its first-vertex limitation is inherited.
            b={'moscow':[36.785,55.105,38.005,56.045],'spb':[29.385,59.585,30.815,60.265],'kazan':[48.785,55.595,49.415,56.015],'kaliningrad':[20.285,54.605,20.665,54.835],'minsk':[27.335,53.755,27.765,54.035],'istanbul':[28.485,40.785,29.465,41.365],'dubai':[54.835,24.735,55.665,25.415]};self.coverage_bounds=b[city]
    def project_hotel(self,h):return Point(*self.tr.transform(h['longitude'],h['latitude']))
    def covered(self,h):
        a,b,c,d=self.coverage_bounds;return a<h['longitude']<c and b<h['latitude']<d
    def snap(self,p,maxd=100):
        d,i=self.tree.query([p.x,p.y]);return (int(i),float(d)) if d<=maxd else (None,float(d))
    def route(self,h,target,kind):
        p=self.project_hotel(h);s,sd=self.snap(p);q=target['point'];e,ed=self.snap(q)
        if s is None or e is None:return {'ok':False,'reason':'hotel_or_target_snap_over_100m','hotel_snap_m':sd,'target_snap_m':ed,'target_id':target['sid']}
        if self._last_source!=s:
            self._last_dist,self._last_pred=dijkstra(self.A,directed=False,indices=s,return_predecessors=True,limit=180000);self._last_source=s
        ds,pred=self._last_dist,self._last_pred
        if not np.isfinite(ds[e]):return {'ok':False,'reason':'no_connected_path_in_cached_graph_not_proof_no_real_path','hotel_snap_m':sd,'target_snap_m':ed,'target_id':target['sid']}
        ids=[e]
        while ids[-1]!=s:
            j=int(pred[ids[-1]])
            if j<0:return {'ok':False,'reason':'predecessor_gap'}
            ids.append(j)
        ids.reverse();edges=[self.edges[(min(a,b),max(a,b))] for a,b in zip(ids,ids[1:])];coord=[self.inv.transform(*self.xy[i]) for i in ids]
        rid=hashlib.sha256(json.dumps([h['hotel_id'],target['sid'],kind,self.nids[s],self.nids[e]]).encode()).hexdigest()[:20]
        return {'ok':True,'route_id':rid,'target_id':target['sid'],'target_name':target['tags'].get('name'),'target_kind':kind,'graph_length_m':float(ds[e]),'length_estimate_m':float(ds[e])+sd+ed,'hotel_snap_m':sd,'target_snap_m':ed,'node_ids':[self.nids[i] for i in ids],'coordinates':coord,'edges':edges,'target':target,'origin_verified_entrance':False,'target_verified_entrance':target.get('verified_entrance',False),'legal_access_inspected':False}
    def choose(self,h,candidates,kind,max_candidates=5):
        # Evaluate all eligible candidate endpoint nodes against one shortest-path tree.
        # A fixed top-k by air distance can miss the nearest reachable path behind a river/fence.
        p=self.project_hotel(h);s,sd=self.snap(p)
        if s is None:return {'ok':False,'reason':'hotel_snap_over_100m','hotel_snap_m':sd,'candidates_total':len(candidates)}
        if not candidates:return {'ok':False,'reason':'no_eligible_mapped_targets','candidates_total':0}
        if self._last_source!=s:
            self._last_dist,self._last_pred=dijkstra(self.A,directed=False,indices=s,return_predecessors=True,limit=180000);self._last_source=s
        xy=np.asarray([[r['point'].x,r['point'].y] for r in candidates]);gaps,inds=self.tree.query(xy);costs=self._last_dist[inds]+gaps+sd;costs[gaps>100]=np.inf
        i=int(np.argmin(costs))
        if not np.isfinite(costs[i]):return {'ok':False,'reason':'no_connected_eligible_candidate_in_cached_graph','candidates_total':len(candidates),'endpoint_snap_over_100m':int(np.count_nonzero(gaps>100))}
        return self.route(h,candidates[i],kind)
    def beach_targets(self):
        out=[]
        for b in self.beaches:
            if b['tags'].get('access') in {'private','no','customers'}:continue
            geom=b['geometry'];center=geom.centroid;radius=max(math.hypot(geom.bounds[2]-geom.bounds[0],geom.bounds[3]-geom.bounds[1])/2,30)+30
            candidates=self.tree.query_ball_point([center.x,center.y],radius)
            candidates=[i for i in candidates if geom.distance(Point(self.xy[i]))<=25]
            if not candidates:continue
            # All graph contacts are retained; endpoints remain candidates, not public entrances.
            for i in candidates:
                out.append({**b,'point':Point(self.xy[i]),'sid':b['sid']+f'#graph-node-{self.nids[i]}','beach_id':b['sid'],'verified_entrance':False})
        return out

def steps_metrics(edges):
    n=0;active=False;length=0.;unknown=0.
    for e in edges:
        isstep=e['tags'].get('highway')=='steps'
        if isstep and not active:n+=1
        active=isstep
        if isstep:length+=e['length_m']
        else:unknown+=e['length_m']
    return {'known_step_sections':n,'mapped_steps_length_m':length,'non_step_edges_not_surveyed_for_unmapped_obstacles_m':unknown,'counting_convention':'Consecutive steps edges are one section even across way IDs; not individual risers; zero does not establish step-free route.'}
def lighting_metrics(edges):
    ds=collections.defaultdict(float)
    for e in edges:
        v=e['tags'].get('lit');key='yes' if v=='yes' else 'no' if v=='no' else 'unknown';ds[key]+=e['length_m']
    total=sum(ds.values());return {'known_lit_percent':100*ds['yes']/total if total else None,'known_unlit_percent':100*ds['no']/total if total else None,'unknown_percent':100*ds['unknown']/total if total else None,'total_graph_length_m':total,'date_kind':'OSM way edit date; working lamps not field inspected'}
def crossing_metrics(route,graph):
    if not graph.busy_tree:return {'known_crossings':0,'crossings':[],'unknown_grade_and_type':0}
    coords=route['coordinates'];line=gtransform(graph.tr.transform,LineString(coords)) if len(coords)>1 else Point(*graph.tr.transform(*coords[0]));pts=[]
    for i in graph.busy_tree.query(line):
        b=graph.busy[int(i)];inter=line.intersection(b['geometry'])
        gg=list(inter.geoms) if hasattr(inter,'geoms') else [inter]
        for p in gg:
            if p.geom_type!='Point':continue # Shared road segments are not crossings.
            if any(p.distance(x['point'])<4 for x in pts):continue
            pts.append({'point':p,'road':b})
    out=[]
    for r in pts:
        p=r['point'];b=r['road'];crossing=[];at_grade_possible=False
        for e in route['edges']:
            if e['geometry'].distance(p)>3:continue
            t=e['tags'];bt=b['tags']
            # Explicit unequal layers/bridge/tunnel do not form an at-grade crossing.
            if t.get('layer','0')!=bt.get('layer','0') and any(t.get(k) or bt.get(k) for k in ['bridge','tunnel']):continue
            at_grade_possible=True
            if t.get('highway')=='crossing' or t.get('footway')=='crossing' or t.get('crossing'):crossing.append(t.get('crossing',t.get('crossing:markings','mapped_crossing')))
        if not at_grade_possible:continue
        for n in route['node_ids']:
            q=graph.crossing_nodes.get(n)
            if q and q['point'].distance(p)<4:crossing.append(q['tags'].get('crossing','mapped_crossing_node'))
        out.append({'road_id':b['sid'],'highway':b['tags'].get('highway'),'crossing_type':crossing[0] if crossing else 'unknown','coordinates':graph.inv.transform(p.x,p.y),'grade_inspected':False,'known_mapped_crossing':bool(crossing)})
    return {'known_crossings':sum(r['known_mapped_crossing'] for r in out),'geometric_crossing_candidates':len(out),'crossings':out,'unknown_grade_and_type':sum(not r['known_mapped_crossing'] for r in out),'scope':'Known mapped crossings and additional geometric candidates separated; explicit unequal bridge/tunnel layers excluded; missing grade/type preserved unknown; not safety certification.'}

def opening_state(poi,date,city,kitchen=False):
    t=poi['tags'];keys=['opening_hours:kitchen','kitchen:opening_hours','food_service_hours'] if kitchen else ['opening_hours'];oh=next((t[k] for k in keys if t.get(k)),None)
    if not oh:return {'state':'unknown','reason':'no_kitchen_service_hours' if kitchen else 'no_opening_hours','expression':None}
    try:
        dt=datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(TZ[city]));o=opening_hours.OpeningHours(oh,country=COUNTRY[city],timezone=ZoneInfo(TZ[city]));state='open' if o.is_open(dt) else 'closed' if o.is_closed(dt) else 'unknown'
        return {'state':state,'expression':oh,'evaluated_local':dt.isoformat(),'reason':'OSM declared recurring schedule; future exceptions/physical operation require refresh','has_public_holiday_expression':bool(re.search(r'\b(PH|SH)\b',oh)),'next_change':str(o.next_change(dt))}
    except Exception as e:return {'state':'unknown','expression':oh,'reason':'parser_or_country_holiday_failure:'+str(e)[:160]}

def gtfs_dates(city):
    ck=CITY_V4.get(city,city);files=list((V4/'raw').glob(f'*gtfs*{ck}*.zip'));out=[]
    for path in files:
      try:
        with zipfile.ZipFile(path) as z:
            names=z.namelist();calname=next((n for n in names if n.endswith('calendar.txt')),None);exname=next((n for n in names if n.endswith('calendar_dates.txt')),None)
            cal=list(csv.DictReader(z.read(calname).decode('utf-8-sig').splitlines())) if calname else [];ex=list(csv.DictReader(z.read(exname).decode('utf-8-sig').splitlines())) if exname else []
            for trip in TRIPS:
                dt=datetime.fromisoformat(trip['arrival_local']);s=dt.strftime('%Y%m%d');weekday=dt.strftime('%A').lower();active={r['service_id'] for r in cal if r['start_date']<=s<=r['end_date'] and r.get(weekday)=='1'}
                for r in ex:
                    if r['date']==s:
                        if r['exception_type']=='1':active.add(r['service_id'])
                        elif r['exception_type']=='2':active.discard(r['service_id'])
                out.append({'file':str(path),'date':s,'active_services':len(active),'calendar_rows':len(cal),'exception_rows':len(ex),'calendar_min':min((r['start_date'] for r in cal),default=None),'calendar_max':max((r['end_date'] for r in cal),default=None),'license_entitlement_and_operator_completeness':'not asserted by ZIP presence'})
      except Exception as e:out.append({'file':str(path),'error':str(e)})
    return out

def dsm_ascent(route,graph):
    try:
      import rasterio
      from scipy.ndimage import median_filter
      if len(route['coordinates'])<2:return None
      line=gtransform(graph.tr.transform,LineString(route['coordinates']));positions=np.linspace(0,line.length,max(2,int(math.ceil(line.length/30))+1));points=[line.interpolate(float(d)) for d in positions];ll=[graph.inv.transform(p.x,p.y) for p in points];values=[None]*len(ll);used=[]
      for fp in (V4/'raw/raster_tiles').glob('Copernicus_DSM*.tif'):
        with rasterio.open(fp) as src:
          inds=[i for i,(x,y) in enumerate(ll) if src.bounds.left<x<src.bounds.right and src.bounds.bottom<y<src.bounds.top]
          if not inds:continue
          used.append(fp.name)
          for i,v in zip(inds,src.sample([ll[i] for i in inds],masked=True)):
            if not np.ma.is_masked(v[0]):values[i]=float(v[0])
      if any(v is None for v in values):return {'ok':False,'reason':'DSM_tile_or_nodata_gap','valid_samples':sum(v is not None for v in values),'samples':len(values)}
      a=np.asarray(values);sm=median_filter(a,size=3,mode='nearest');delta=np.diff(sm);total=float(np.maximum(delta,0).sum())
      return {'ok':True,'dsm_positive_dz_estimate_m':total,'sample_step_m':30,'samples':len(values),'filter':'3-sample running median; endpoint nearest','tiles':used,'ground_ascent_not_measured':True,'model':'CopernicusGLO30 DSM; building/vegetation surface, not DTM','error_interval_m':None,'error_reason':'No independent ground profile; no fabricated error interval.'}
    except Exception as e:return {'ok':False,'reason':'DSM_sampling_failed:'+str(e)[:120]}

def run(city):
    start=time.perf_counter();outdir=BASE/'results/geo';outdir.mkdir(parents=True,exist_ok=True);hs=hotels(city);graph=Graph(city);load_seconds=time.perf_counter()-start;facts=[];routes=[];poi_audit=[];targets_out=[];timings=collections.defaultdict(float);diag={'city_id':city,'hotels':len(hs),'graph_nodes':len(graph.nids),'graph_edges':len(graph.edges),'load_seconds':load_seconds,'input_manifest':graph.manifest,'no_new_http_requests':True,'gtfs_calendar_checks':gtfs_dates(city)}
    exclusions_file=BASE/'qa/geo/source_quality_exclusions.json';quality_exclusions=json.loads(exclusions_file.read_text()) if exclusions_file.exists() else []
    joinfile=BASE/'results/facades'/f'{city}_building_matches.jsonl';joins=collections.defaultdict(list)
    if joinfile.exists():
      for r in read_jsonl(joinfile):joins[str(r['hotel_id'])].append(r)
    beach=graph.beach_targets() if city in COAST else []
    reference_center=Point(*graph.tr.transform(*CENTERS[city]));_,component_labels=connected_components(graph.A,directed=False);sizes=np.bincount(component_labels);largest_component=int(np.argmax(sizes));center_pool=np.flatnonzero(component_labels==largest_component);offsets=np.linalg.norm(graph.xy[center_pool]-np.asarray([reference_center.x,reference_center.y]),axis=1);center_index=int(center_pool[int(np.argmin(offsets))]);center_gap=float(np.min(offsets))
    center={'sid':f'project:center:{city}:osm-node-{graph.nids[center_index]}','point':Point(graph.xy[center_index]),'tags':{'name':'Explicit mapped pedestrian reference node nearest project city-center point '+city},'verified_entrance':False}
    diag['center_target']={'osm_node_id':graph.nids[center_index],'coordinates':graph.inv.transform(*graph.xy[center_index]),'selection_reference_coordinates':CENTERS[city],'reference_to_node_m':float(center_gap),'selection_rule':'nearest node to reference center in largest connected mapped pedestrian network component','component_nodes':int(sizes[largest_component]),'meaning':'An explicit chosen point near the center, not an implicit administrative center or hotel entrance.'}
    for h in hs:
      p=graph.project_hotel(h);covered=graph.covered(h);t0=time.perf_counter()
      # N04 intentionally counts objects in the accepted snapshot, never all nightlife establishments.
      bars=[r for r in graph.pois if r['tags'].get('amenity') in {'bar','nightclub','music_venue'} or r['tags'].get('club')=='music']
      nearby=[r for r in bars if p.distance(r['geometry'])<=300];seen=set();dedup=[]
      for r in nearby:
        name=(r['tags'].get('name') or '').strip().casefold();dup=any(name and name==(q['tags'].get('name') or '').strip().casefold() and r['point'].distance(q['point'])<15 for q in dedup)
        if r['sid'] in seen or dup:continue
        seen.add(r['sid']);dedup.append(r)
      if covered:
        f=fact(h,'N04',value={'count':len(dedup),'objects':[{'id':r['sid'],'name':r['tags'].get('name'),'type':r['tags'].get('amenity'),'distance_m':p.distance(r['geometry']),'opening_hours':r['tags'].get('opening_hours')} for r in dedup],'completeness':'Objects mapped in this OSM extraction; not all real venues. Zero means none recorded.','unknown_hours_count':sum(not r['tags'].get('opening_hours') for r in dedup)},unit='count',status='computed',recipe_id='N04-R2',params={'radius_m':300,'types':['bar','nightclub','music_venue'],'distance_origin':'OTAhotelpoint','buffer_operator':'minimum geometry distance'},sources=[osm_source(r,graph.manifest) for r in dedup] or [{'provider':'OpenStreetMap','dataset_version':graph.manifest.get('source_date'),'dataset_sha256':graph.manifest.get('sha256'),'license':'ODbL1.0','url':'https://www.openstreetmap.org/copyright'}],scope_note='Source-scoped mapped-count, not acoustic level or indoor quiet.',recipe_attempts=attempts('N04',{'N04-R1':'No accepted official per-object nightlife inventory in cache.'},'N04-R2'))
      else:f=fact(h,'N04',unknown_reason='hotel_outside_cached_OSM_extent')
      facts.append(f)
      for fid,key,records,tree in [('N02','road_classes',graph.busy,graph.busy_tree),('N03','rail_classes',graph.rail,graph.rail_tree)]:
        jj=joins.get(h['hotel_id'],[]);accepted=[]
        for j in jj:
            # Common building-match protocol accepts flat geometry or candidate list.
            candidates=j.get('matches',j.get('accepted_matches',[j]))
            for b in candidates:
                if b.get('accepted') is False or b.get('match_status') in {'rejected','ambiguous','unmatched'}:continue
                gw=b.get('geometry_wkb4326') or b.get('geometry_wkb') or b.get('building_geometry_wkb')
                if gw:accepted.append(b)
        if accepted and tree:
          for b in accepted:
            bg=gtransform(graph.tr.transform,wkb.loads(b.get('geometry_wkb4326') or b.get('geometry_wkb') or b.get('building_geometry_wkb'),hex=True));i=int(tree.nearest(bg));r=records[i];dist=bg.distance(r['geometry']);sid=b.get('building_id',b.get('source_id',b.get('osm_id','unknown_building')))
            facts.append(fact(h,fid,value={'distance_m':dist,'target_id':r['sid'],'target_class':r['tags'].get('highway' if fid=='N02' else 'railway'),'target_name':r['tags'].get('name'),'building_id':sid},unit='m',status='computed',subject_level='building',subject_id=str(sid),recipe_id=fid+'-R2',params={key:sorted(BUSY) if fid=='N02' else ['rail','light_rail','tram'],'distance_origin':'accepted building footprint','target_geometry':'axis','rail_status_convention':'rail/light_rail/tram mapped active; disused/abandoned/construction/proposed excluded' if fid=='N03' else None},sources=[osm_source(r,graph.manifest),b.get('source',{'building_join':str(joinfile),'evidence':b.get('evidence')})],scope_note='One matched building; not all hotel buildings or window distance. N03 operational status is OSM mapped state, not inspected train frequency.',recipe_attempts=attempts(fid,{fid+'-R1':'No accepted official road/active-rail layer for this building in cache.'},fid+'-R2')))
        elif covered and tree:
            i=int(tree.nearest(p));r=records[i];facts.append(fact(h,fid,unknown_reason='verified_hotel_building_footprint_missing_or_ambiguous',params={key:sorted(BUSY) if fid=='N02' else ['rail','light_rail','tram']},component_diagnostics={'point_to_axis_distance_m':p.distance(r['geometry']),'target_id':r['sid'],'not_feature_value':'Point distance is not footprint distance.'}))
        else:facts.append(fact(h,fid,unknown_reason='source_extent_or_matching_source_geometry_unavailable'))
      facts.append(fact(h,'N01',unknown_reason='No licensed georeferenced accepted facade-height Lnight layer, complete emissions model, or independent acoustic validation in cached inputs; road distance is not dB.'))
      timings['N']+=time.perf_counter()-t0;t0=time.perf_counter()
      route_candidates={}
      if city in COAST:route_candidates['beach']=graph.choose(h,beach,'beach') if covered else {'ok':False,'reason':'hotel_outside_cached_OSM_extent'}
      route_candidates['stop']=graph.choose(h,graph.stops,'stop') if covered else {'ok':False,'reason':'hotel_outside_cached_OSM_extent'}
      route_candidates['center']=graph.route(h,center,'center') if covered else {'ok':False,'reason':'hotel_outside_cached_OSM_extent'}
      for fid,kind in [('W01','beach'),('W02','stop'),('W03','center')]:
        if kind=='beach' and city not in COAST:facts.append(fact(h,fid,status='not_applicable',params={'target':'marine_beach'},scope_note='Moscow/Kazan/Minsk inland; this parameter is marine beach, not river/lake recreation.'));continue
        r=route_candidates[kind]
        if not r['ok']:facts.append(fact(h,fid,unknown_reason=r['reason'],params={'target_kind':kind},component_diagnostics=r));continue
        facts.append(fact(h,fid,status='estimate',value={'distance_m':r['length_estimate_m'],'graph_length_m':r['graph_length_m'],'hotel_connector_gap_m':r['hotel_snap_m'],'target_connector_gap_m':r['target_snap_m'],'route_id':r['route_id'],'target_id':r['target_id'],'target_name':r['target_name'],'verified_hotel_entrance':False,'verified_target_entrance':r['target_verified_entrance']},unit='m',recipe_id=fid+'-R2',params={'target_kind':kind,'target_id':r['target_id'],'walking_graph':'OSM shared nodes; no joins at mere geometry crossing','private_or_conditional_access':'excluded','maximum_unverified_snap_m':100},scope_note='Research route estimate; actual legal hotel entrance and destination entrance need verification. Beach contact node is not a verified public beach gate. Default road foot-access rules need local QA. D18: excluded from mandatory constraints.',sources=[{'provider':'OpenStreetMap','dataset_version':graph.manifest.get('source_date'),'dataset_sha256':graph.manifest.get('sha256'),'license':'ODbL1.0','url':'https://www.openstreetmap.org/copyright'}],recipe_attempts=attempts(fid,{fid+'-R1':'No official surveyed continuous pedestrian graph/verified entrance pair.'},fid+'-R2'),hard_condition_eligible=False))
        routes.append({'type':'Feature','geometry':{'type':'LineString','coordinates':r['coordinates']} if len(r['coordinates'])>1 else mapping(Point(r['coordinates'][0])),'properties':{'hotel_id':h['hotel_id'],'city_id':city,'route_id':r['route_id'],'kind':kind,'target_id':r['target_id'],'graph_length_m':r['graph_length_m'],'hotel_snap_m':r['hotel_snap_m'],'target_snap_m':r['target_snap_m'],'node_ids':r['node_ids'],'edge_way_ids':[e['sid'] for e in r['edges']],'validity':'estimate, endpoints and default access not inspected'}})
      main=route_candidates.get('beach') if route_candidates.get('beach',{}).get('ok') else route_candidates['stop'] if route_candidates['stop'].get('ok') else route_candidates['center']
      if main.get('ok'):
        dsm=dsm_ascent(main,graph);sp=steps_metrics(main['edges']);li=lighting_metrics(main['edges']);cr=crossing_metrics(main,graph);params={'route_id':main['route_id'],'target_kind':main['target_kind'],'target_id':main['target_id']}
        for fid,value in [('W05',sp),('W06',cr),('Q03',li)]:facts.append(fact(h,fid,status='computed',value=value,unit='sections' if fid=='W05' else 'count' if fid=='W06' else '%',params=params,recipe_id=fid+'-R2',sources=[{'provider':'OpenStreetMap','dataset_version':graph.manifest.get('source_date'),'dataset_sha256':graph.manifest.get('sha256'),'url':'https://www.openstreetmap.org/copyright','license':'ODbL1.0'}],scope_note='Computed mapped attributes conditional on saved estimated route. Unknown unmapped obstacles/lighting retained; not a verified accessibility/safety guarantee.',recipe_attempts=attempts(fid,{fid+'-R1':'No full surveyed route obstacle/lighting inventory.'},fid+'-R2'),hard_condition_eligible=False))
        if dsm and dsm.get('ok'):
            dsmsources=[dsm_source(tile) for tile in dsm['tiles']]
            facts.append(fact(h,'W04',status='estimate',value=dsm,unit='m',params=params,recipe_id='W04-D18-DSM',sources=dsmsources,depends_on=[{'kind':'route','id':main['route_id']}]+[{'kind':'raster_tile','id':tile} for tile in dsm['tiles']],scope_note='Explicit D18 DSM surface estimate, outside mandatory conditions. Canonical ground-DTM recipe failed; no conversion of DSM to pavement elevations.',recipe_attempts=attempts('W04',{'W04-R1':'No ground DTM/engineering profile. Copernicus GLO30 is DSM, rejected as ground profile.','W04-R2':'No repeated surveyed or levelled route profile.'}),catalogue_recipe_not_satisfied=True,hard_condition_eligible=False))
        else:facts.append(fact(h,'W04',unknown_reason='Ground DTM/survey absent; DSM estimate unavailable',params=params,component_diagnostics=dsm))
        facts.append(fact(h,'A01',unknown_reason='No zero-gap surveyed constraints: hotel exit, kerbs, width, slope, surface, stairs and working lifts unverified. A route with no mapped steps is insufficient.',params={**params,'minimum_width_m':0.9,'maximum_incline_percent':5,'maximum_kerb_mm':20},component_diagnostics={'known_steps_sections':sp['known_step_sections'],'unverified_route':True}))
        if main['target_kind']=='beach':targets_out.append({'hotel_id':h['hotel_id'],'city_id':city,'beach_id':main['target']['beach_id'],'beach_name':main['target']['tags'].get('name'),'beach_tags':main['target']['tags'],'beach_geometry_wkb_projected':main['target']['geometry'].wkb_hex,'beach_geometry_wkb':gtransform(graph.inv.transform,main['target']['geometry']).wkb_hex,'route_id':main['route_id'],'entry_candidate_id':main['target_id'],'entry_lon_lat':main['coordinates'][-1],'confirmed_public_entrance':False,'source':osm_source(main['target']['record'],graph.manifest)})
      else:
        for fid in ['W04','W05','W06','A01','Q03']:facts.append(fact(h,fid,unknown_reason='No selected connected route in cached graph; no absence or accessibility conclusion.'))
      timings['W_A_Q03']+=time.perf_counter()-t0;t0=time.perf_counter()
      # Time-aware POI: evaluate true service hours, not generic near POI or bar hours.
      def match(t,fid):
        if fid=='L01':return t.get('amenity')=='pharmacy'
        if fid=='L02':return t.get('amenity') in {'restaurant','cafe','fast_food','food_court','bar','pub'}
        if fid=='L03':return t.get('shop')=='supermarket'
        if fid=='F02':return t.get('healthcare:speciality') in {'paediatrics','pediatrics'} and t.get('emergency')=='yes'
        if fid=='P04':return t.get('amenity')=='veterinary'
        if fid=='R04':return (t.get('office')=='coworking' or t.get('amenity')=='coworking_space') and any(t.get(k) in {'yes','public','day_pass'} for k in ['coworking:day_pass','day_pass','access'])
        return False
      for fid in ['L01','L02','L03','F02','P04','R04']:
        blocked={r['poi_id'] for r in quality_exclusions if r['feature_id']==fid}
        pool=[r for r in graph.pois if match(r['tags'],fid) and p.distance(r['point'])<30000 and r['sid'] not in blocked]
        for trip in TRIPS:
            date=trip['arrival_local'];states=[(r,opening_state(r,date,city,kitchen=fid=='L02')) for r in pool];opens=[r for r,s in states if s['state']=='open'];rt=graph.choose(h,opens,fid) if covered and opens else {'ok':False,'reason':'no_confirmed_open_service_hours_in_cached_source_or_extent'}
            params={'arrival_local':date,'timezone':TZ[city],'candidate_search_radius_m':30000,'service':fid,'kitchen_service_hours_required':fid=='L02','quality_exclusions':sorted(blocked),'quality_exclusion_evidence':str(exclusions_file) if blocked else None}
            if rt['ok']:
                target=rt['target'];st=next(s for r,s in states if r['sid']==target['sid']);value={'distance_m':rt['length_estimate_m'],'graph_length_m':rt['graph_length_m'],'route_id':rt['route_id'],'poi_id':target['sid'],'poi_name':target['tags'].get('name'),'opening_hours_evaluation':st,'known_open_candidates':len(opens),'unknown_hours_candidates':sum(s['state']=='unknown' for r,s in states),'nearest_scope':'nearest network route among all evaluated open eligible mapped candidates within 30km air radius; unknown-hours POIs may be nearer'}
                facts.append(fact(h,fid,status='estimate',value=value,unit='m',params=params,recipe_id=fid+'-R2',sources=[osm_source(target['record'],graph.manifest)],scope_note='Future declared recurring schedule plus research route estimate; not guaranteed physical opening. Refresh hours/holiday exceptions and verify entrances before hard constraint.',recipe_attempts=attempts(fid,{fid+'-R1':'No cached provider-confirmed specific-date service roster and surveyed graph.'},fid+'-R2'),refresh_after=(datetime.now(ZoneInfo('UTC'))+timedelta(days=1)).isoformat(),valid_to=None,hard_condition_eligible=False))
                poi_audit.append({'hotel_id':h['hotel_id'],'feature_id':fid,'city_id':city,'params':params,'poi_id':target['sid'],'poi_tags':target['tags'],'hours':st,'source':osm_source(target['record'],graph.manifest),'route_id':rt['route_id']})
            else:facts.append(fact(h,fid,unknown_reason=rt['reason'],params=params,component_diagnostics={'eligible_source_pois':len(pool),'open_source_pois':len(opens),'closed_source_pois':sum(s['state']=='closed' for r,s in states),'unknown_source_hours':sum(s['state']=='unknown' for r,s in states),'not_inferred':'No confirmed source candidate is not evidence no service exists.'}))
      for fid,kind,pool in [('F01','playground',[r for r in graph.pois if r['tags'].get('leisure')=='playground' and r['tags'].get('access') not in {'private','no','customers'}]),('P03','dog_area',[r for r in graph.pois if r['tags'].get('leisure')=='dog_park' and r['tags'].get('access') not in {'private','no','customers'}])]:
        rt=graph.choose(h,pool,kind) if covered else {'ok':False,'reason':'outside_cached_extent'}
        if rt['ok']:facts.append(fact(h,fid,status='estimate',value={'distance_m':rt['length_estimate_m'],'route_id':rt['route_id'],'poi_id':rt['target_id'],'poi_name':rt['target_name'],'access_tag':rt['target']['tags'].get('access'),'age_or_dog_restrictions':{k:v for k,v in rt['target']['tags'].items() if k in ['min_age','max_age','dog','dog:conditional','opening_hours','access','leash']}},unit='m',recipe_id=fid+'-R2',scope_note='Mapped designated target and route estimate; verify entrance/public access and age/leash/season rules. Ordinary green space is not substituted for designated dog area.',sources=[osm_source(rt['target']['record'],graph.manifest)],hard_condition_eligible=False))
        else:facts.append(fact(h,fid,unknown_reason='No eligible target/connected route in cached graph; absent mapping is not absence of playground/dog area.',component_diagnostics=rt))
      timings['L_F_P_R04']+=time.perf_counter()-t0;t0=time.perf_counter()
      for trip in TRIPS:
        for fid in ['T01','T02','T03']:
            checks=[r for r in diag['gtfs_calendar_checks'] if r.get('date')==trip['arrival_local'][:10].replace('-','')]
            facts.append(fact(h,fid,unknown_reason='No licensed complete current operator schedule/active service calendar and verified terminal-to-hotel itinerary for requested date. Zero active cached services is not no real transit.',params={'arrival_local':trip['arrival_local'],'timezone':TZ[city],'origin_kind':'airport_terminal' if fid=='T01' else 'railway_station' if fid=='T02' else 'airport_terminal','night_window_local':['01:00','03:00'] if fid=='T03' else None,'arrival_buffer_minutes':30 if fid=='T01' else 0},component_diagnostics={'gtfs_calendar_checks':checks},recipe_attempts=attempts(fid,{fid+'-R1':'Actual cached ZIP calendars inspected; no accepted full operator active itinerary for requested date; licence/operator completeness also required.',fid+'-R2':'No accepted normalized current primary-operator date-specific schedule in cached inputs.'})))
        for fid in ['X01','X02','X03']:
            facts.append(fact(h,fid,unknown_reason='No accepted dated, geocoded primary-source event/closure/construction inventory with coverage, status and licence for requested trip. Generic OSM construction tags do not provide permit geometry and active dates.',params={'start':trip['start'],'end':trip['end'],'timezone':TZ[city],'radius_m':1000 if fid=='X01' else None}))
      for fid,reason in [('R05','No licensed fresh OpenCellID export with operator/radio/last_seen and observed cell coordinates. OSM mast location or phone bars is another meaning.'),('Q01','No matched police-category numerator, resident population denominator, reporting period and statistical-boundary version.'),('Q02','No complete official geocoded injury-crash IDs/dates/coordinate completeness for route buffer and period. Selective news bulletins cannot provide full count.')]:facts.append(fact(h,fid,unknown_reason=reason,params={'route_buffer_m':100,'period_start':'2025-01-01','period_end':'2025-12-31'} if fid=='Q02' else {}))
      timings['T_R05_Q_X']+=time.perf_counter()-t0
    # Preserve the two requested seasonal contexts even for the same static graph route.
    # These are declared projections, not two independent physical observations.
    contextual=[];route_features={'W01','W02','W03','W04','W05','W06','A01','Q03','F01','P03'}
    for f in facts:
        if f['feature_id'] not in route_features:contextual.append(f);continue
        for trip in TRIPS:
            r={**f,'params':{**f['params'],'trip_start':trip['start'],'trip_end':trip['end'],'seasonal_route_revalidation':'not performed; snapshot projection only'}}
            identity=json.dumps([r['city_id'],r['hotel_id'],r['feature_id'],r['subject_level'],r['subject_id'],r['params']],sort_keys=True,ensure_ascii=False);r['fact_id']=hashlib.sha256(identity.encode()).hexdigest()[:24];contextual.append(r)
    facts=contextual
    write_jsonl(outdir/f'{city}_facts.jsonl',facts);write_jsonl(outdir/f'{city}_routes.jsonl',routes);write_jsonl(outdir/f'{city}_beach_targets.jsonl',targets_out);write_jsonl(outdir/f'{city}_poi_audit.jsonl',poi_audit)
    diag.update({'total_seconds':time.perf_counter()-start,'family_seconds':dict(timings),'facts':len(facts),'unique_hotel_feature_pairs':len({(r['hotel_id'],r['feature_id']) for r in facts}),'statuses':dict(collections.Counter(r['status'] for r in facts)),'result_bytes':sum(p.stat().st_size for p in outdir.glob(city+'_*jsonl')),'feature_counts':{fid:dict(collections.Counter(r['status'] for r in facts if r['feature_id']==fid)) for fid in OWN},'graph_license':'ODbL1.0; attribution/share-alike obligations; source ages and extent retained'})
    (outdir/f'{city}_run.json').write_text(json.dumps(diag,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:diag[k] for k in ['city_id','hotels','facts','statuses','total_seconds','result_bytes']},ensure_ascii=False))
    return diag

def main():
    p=argparse.ArgumentParser();p.add_argument('--city',required=True,choices=list(CENTERS));a=p.parse_args();run(a.city)
if __name__=='__main__':main()
