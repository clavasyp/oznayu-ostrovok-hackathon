"""V9 B/C/E calculators. Inputs: common.cohort, cached WorldCover; explicit Open-Meteo ERA5.

Pure imports. CLI: fetch --city sochi; compute --city sochi; test.
Only fetch performs HTTP; it contacts archive-api.open-meteo.com and official docs,
never Ostrovok or a photo CDN. Climate output is July and November, 1991–2020.
"""
from __future__ import annotations
import argparse,calendar,csv,hashlib,json,math,statistics,sys,time
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
from functools import lru_cache
import numpy as np
import requests,rasterio,shapely
from pyproj import Transformer,Geod
from rasterio.windows import from_bounds
from rasterio.warp import reproject,Resampling
from common import BASE,V4,TRIPS,attempts,catalogue,fact,hotels,now,sha256,write_jsonl

ENV=BASE/'environment'; RAW=ENV/'raw'; OUT=BASE/'results/environment'; QA=BASE/'qa/environment'
FEATURES=[f'{p}{i:02}' for p,n in [('B',5),('C',4),('E',4)] for i in range(1,n+1)]
MONTHS=(7,11); YEARS=range(1991,2021); VEG=(10,20,30,40,90,95,100)
UTC_OFFSET={'sochi':3,'moscow':3,'spb':3,'kazan':3,'kaliningrad':2,'minsk':3,'istanbul':3,'dubai':4}
GEOD=Geod(ellps='WGS84')

@lru_cache(maxsize=32)
def input_sha(path):
    dig=hashlib.sha256()
    with Path(path).open('rb')as f:
      while block:=f.read(4*1024*1024):dig.update(block)
    return dig.hexdigest()

def marine_native_coordinate(api_lat,api_lon):
    # Pinned public importer defines API grid origin at nativeCF axis+halfcell.
    # Preserve both supports: never silently treat a reported APIpoint as nativeCFpoint.
    iy=round((api_lat+80-1/24)*12);ix=round((api_lon+180-1/24)*12)
    lat=float(np.float32(-80+iy/12));lon=float(np.float32(-180+ix/12))
    tile=V4/'raw/raster_tiles'/f'ESA_WorldCover_10m_2021_v200_N{math.floor(lat/3)*3:02}E{math.floor(lon/3)*3:03}_Map.tif'
    wc=None
    if tile.exists():
      with rasterio.open(tile)as ds:wc=int(next(ds.sample([(lon,lat)]))[0])
    return {'native_cf_latitude':lat,'native_cf_longitude':lon,'native_latitude_index':iy,'native_longitude_index':ix,
        'api_reported_latitude':api_lat,'api_reported_longitude':api_lon,'api_to_native_offset_deg':1/24,
        'native_coordinate_worldcover2021_class':wc,
        'wet_cell_evidence':'OpenMeteo finite native6h values; pinnedimporter buildssea mask fromthetao non_fill; independentWorldCover2021class80 where available',
        'support_limitation':'APIreportedcoordinatesoffsethalfgridfromnativeCFcoordinates; documentedsourceaudit, exactAPIdeploymentcommitnotexposed'}

def marine_request(city,points,year,month,label):
    params={'latitude':','.join(str(p[0])for p in points),'longitude':','.join(str(p[1])for p in points),
       'start_date':f'{year}-{month:02}-01','end_date':f'{year}-{month:02}-{calendar.monthrange(year,month)[1]}',
       'hourly':'sea_surface_temperature','models':'meteofrance_currents','cell_selection':'sea','timezone':'UTC'}
    key=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()[:18]
    dest=RAW/f'marine_{city}_{year}{month:02}_{label}_{key}.json';meta=dest.with_suffix('.meta.json')
    if dest.exists()and meta.exists()and json.loads(meta.read_text()).get('http_status')==200:
        data=json.loads(dest.read_text());return(data if isinstance(data,list)else[data]),json.loads(meta.read_text())
    tick=time.perf_counter();requested=now();r=requests.get('https://marine-api.open-meteo.com/v1/marine',params=params,timeout=60)
    dest.write_bytes(r.content);rec={'requested_at':requested,'finished_at':now(),'url':r.url,'http_status':r.status_code,
       'bytes':len(r.content),'sha256':sha256(dest),'path':str(dest),'params':params,'n_locations':len(points),
       'conservative_weight_estimate':len(points)*max(1,calendar.monthrange(year,month)[1]/14),
       'license':'CC BY4.0; D22hackathonfreeAPI; productionpaidplan/selfhost required','elapsed_s':time.perf_counter()-tick}
    dump(meta,rec)
    with (ENV/'http_ledger.jsonl').open('a')as f:f.write(json.dumps(rec,ensure_ascii=False)+'\n')
    r.raise_for_status();data=r.json()
    pause=max(1,rec['conservative_weight_estimate']/7)-(time.perf_counter()-tick)
    if pause>0:time.sleep(pause)
    return(data if isinstance(data,list)else[data]),rec

def fetch_marine(city,target_path=None,cache_only=False):
    target_path=Path(target_path).resolve()if target_path else BASE/f'results/geo/{city}_beach_targets.jsonl'
    if not target_path.exists():raise RuntimeError('Beach selection input not ready')
    targets=[json.loads(s)for s in target_path.open()if s.strip()]
    # A beach beside a river or lake is not a sea target. Selection must carry
    # the geo owner's coastline-based marineclassification, not just natural=beach.
    beaches={r['beach_id']:r for r in targets if r.get('beach_geometry_wkb')and r.get('beach_kind')=='sea'
        and r.get('marine_classification',{}).get('accepted')is True}
    mapping_path=ENV/f'marine_mapping_{city}{"_provisional" if cache_only else ""}.json'
    if not beaches:
        dump(mapping_path,{'city_id':city,'beaches':{},'source_beach_targets_sha256':sha256(target_path),
            'source_beach_targets_path':str(target_path),'source_beach_targets_n':len(targets),'runtime_s':0,
            'cache_only':cache_only,'reason':'no selected beach geometry with accepted marine coastline classification'});return
    order=sorted(beaches);points=[]
    for bid in order:
      p=shapely.from_wkb(bytes.fromhex(beaches[bid]['beach_geometry_wkb'])).representative_point();points.append((p.y,p.x))
    oldpath=mapping_path
    if not oldpath.exists()and not cache_only:oldpath=ENV/f'marine_mapping_{city}_provisional.json'
    old=json.loads(oldpath.read_text())if oldpath.exists()else{}
    started=time.perf_counter();response,metadata=marine_request(city,points,2025,7,'beach_probe')
    mapping={}
    for bid,requested,d in zip(order,points,response):
      vv=d.get('hourly',{}).get('sea_surface_temperature',[]);valid=sum(v is not None for v in vv)
      lon,lat=float(d['longitude']),float(d['latitude']);distance=GEOD.inv(requested[1],requested[0],lon,lat)[2]
      mapping[bid]={'beach_id':bid,'beach_name':beaches[bid].get('beach_name'),'requested_beach_latitude':requested[0],
        'requested_beach_longitude':requested[1],'model_latitude':lat,'model_longitude':lon,'beach_to_cell_m':distance,
        'probe_valid_hours':valid,'probe_expected_hours':len(vv),'eligible':valid==len(vv)and valid>0 and distance<30000,
        'source':beaches[bid]['source'],'probe_source':metadata,'confirmed_public_entrance':False,
        'selection':'OSMselectedbeachpolygonrepresentativepoint→OpenMeteoseacell; not a verifiedbeachentrance'}
      native=marine_native_coordinate(lat,lon);mapping[bid].update(native)
      mapping[bid]['beach_to_native_cf_m']=GEOD.inv(requested[1],requested[0],native['native_cf_longitude'],native['native_cf_latitude'])[2]
      mapping[bid]['eligible']=mapping[bid]['eligible']and mapping[bid]['beach_to_native_cf_m']<=30000
      if native['native_coordinate_worldcover2021_class']not in (None,0,80):
        mapping[bid]['eligible']=False
        mapping[bid]['rejected_initial_cell']={k:mapping[bid][k]for k in ('model_latitude','model_longitude','native_cf_latitude','native_cf_longitude','native_coordinate_worldcover2021_class','probe_source')}
        mapping[bid]['rejected_initial_cell']['reason']='Finite model wetmask conflicts with classified land at decoded native CF coordinate.'
    # An API finite wetmask can include a coarse coastal cell whose native centre
    # falls on mapped land. Search actual native water centres before giving up;
    # the beach polygon remains the reference and distance is explicitly bounded.
    candidates={};candidate_order={}
    for bid,m in mapping.items():
      if 'rejected_initial_cell'not in m:continue
      choices=[]
      rx=math.ceil(30000/(111000*math.cos(math.radians(m['requested_beach_latitude']))/12))+2
      for iy in range(m['native_latitude_index']-6,m['native_latitude_index']+7):
       for ix in range(m['native_longitude_index']-rx,m['native_longitude_index']+rx+1):
        api=(-80+(iy+.5)/12,-180+(ix+.5)/12);native=marine_native_coordinate(*api)
        distance=GEOD.inv(m['requested_beach_longitude'],m['requested_beach_latitude'],native['native_cf_longitude'],native['native_cf_latitude'])[2]
        if distance<=30000 and native['native_coordinate_worldcover2021_class']==80:
          choices.append((distance,api));candidates[api]=native
      candidate_order[bid]=sorted(choices)
    if candidates:
      pp=sorted(candidates);dd,mm=marine_request(city,pp,2025,7,'wet_validation_probe')
      candidate_responses={p:d for p,d in zip(pp,dd)}
      for bid,choices in candidate_order.items():
       m=mapping[bid];eligible=[]
       for distance,api in choices:
        d=candidate_responses[api];vv=d.get('hourly',{}).get('sea_surface_temperature',[])
        lat,lon=float(d['latitude']),float(d['longitude']);native=marine_native_coordinate(lat,lon)
        actual_distance=GEOD.inv(m['requested_beach_longitude'],m['requested_beach_latitude'],native['native_cf_longitude'],native['native_cf_latitude'])[2]
        if native['native_coordinate_worldcover2021_class']!=80 or actual_distance>30000 or not vv or any(v is None or not math.isfinite(v)for v in vv):continue
        eligible.append((actual_distance,api,d,native))
       if eligible:
        actual_distance,api,d,native=min(eligible,key=lambda row:(row[0],row[1]));lat,lon=float(d['latitude']),float(d['longitude']);vv=d['hourly']['sea_surface_temperature']
        m.update(native);m.update({'model_latitude':lat,'model_longitude':lon,'eligible':True,
            'beach_to_cell_m':GEOD.inv(m['requested_beach_longitude'],m['requested_beach_latitude'],lon,lat)[2],
            'beach_to_native_cf_m':actual_distance,'probe_valid_hours':len(vv),'probe_expected_hours':len(vv),'probe_source':mm,
            'wet_cell_selection_method':'nearest candidate native water centre within30km: WorldCover2021class80 plus finite thetao; initial model-wet landpoint rejected',
            'wet_cell_candidates_tested':len(choices)})
    cells=sorted(set((m['model_latitude'],m['model_longitude'])for m in mapping.values()if m['eligible']))
    if cells:
      for year in (2023,2024,2025):
       for month in MONTHS:
        cached=set()
        for oldfile in RAW.glob(f'marine_{city}_{year}{month:02}_cells_*.json'):
          if oldfile.name.endswith('.meta.json'):continue
          oldmeta=oldfile.with_suffix('.meta.json')
          if not oldmeta.exists()or json.loads(oldmeta.read_text()).get('http_status')!=200:continue
          olddata=json.loads(oldfile.read_text());olddata=olddata if isinstance(olddata,list)else[olddata]
          cached.update((float(d['latitude']),float(d['longitude']))for d in olddata)
        missing=sorted(set(cells)-cached)
        if missing:marine_request(city,missing,year,month,'cells')
    runtime=time.perf_counter()-started
    dump(mapping_path,{'city_id':city,'beaches':mapping,'unique_wet_cells':cells,'cache_only':cache_only,
       'runtime_s':runtime,'runtime_total_measured_s':old.get('runtime_total_measured_s',old.get('runtime_s',0))+runtime,
       'source_beach_targets_path':str(target_path),'source_beach_targets_sha256':sha256(target_path),'source_beach_targets_n':len(targets),
       'selection_protocols':sorted(set(r.get('selection_protocol','legacy_walk_selected')for r in targets)),'period':[2023,2025],
       'scope':'OpenMeteoarchivedCMEMSGLO12potentialthetaoatNetCDFdepth0.49402499198913574m;3yearsnotclimatenorm;modelversionhomogeneitynotguaranteed'})
    print('Marine',city,len(beaches),'beaches',len(cells),'wet cells',flush=True)

def marine_for_hotel(h,month):
    city=h['city_id'];mp=ENV/f'marine_mapping_{city}.json'
    if not mp.exists():return None,'Selected beach/model mapping not computed.'
    mapping=json.loads(mp.read_text())
    tp=Path(mapping.get('source_beach_targets_path',str(BASE/f'results/geo/{city}_beach_targets.jsonl')))
    if not tp.exists():return None,'Selected beach/model mapping input file unavailable.'
    if mapping.get('source_beach_targets_sha256')!=sha256(tp):return None,'Selected beach input changed; marine mapping must be recomputed against current frozen targets.'
    targets={r['hotel_id']:r for r in map(json.loads,tp.open())};target=targets.get(h['hotel_id'])
    if not target:return None,'No selected beach with sufficient geometry in route/OSM input.'
    if target.get('beach_kind')!='sea'or target.get('marine_classification',{}).get('accepted')is not True:return None,'Selected target has no accepted marine coastline classification; river/lake/unknown beach cannot supply marine temperature.'
    m=mapping['beaches'].get(target['beach_id'])
    if not m or not m['eligible']:return None,'No complete finite sea-cell probe within30km of selectedbeach.'
    cell=(m['model_latitude'],m['model_longitude']);annual=[];sources=[];counts={}
    for year in (2023,2024,2025):
      found=None
      for path in RAW.glob(f'marine_{city}_{year}{month:02}_cells_*.json'):
        if path.name.endswith('.meta.json'):continue
        meta=json.loads(path.with_suffix('.meta.json').read_text());dd=json.loads(path.read_text());dd=dd if isinstance(dd,list)else[dd]
        for idx,d in enumerate(dd):
          if (float(d['latitude']),float(d['longitude']))!=cell:continue
          # Use native6-hour steps; avoid counting hourlyHermiteinterpolationas24 independentobservations/day.
          hourly=d['hourly'];vv=[v for t,v in zip(hourly['time'],hourly['sea_surface_temperature'])if int(t[11:13])%6==0]
          expected=calendar.monthrange(year,month)[1]*4;valid=[v for v in vv if v is not None and math.isfinite(float(v))]
          counts[str(year)]={'valid_native_steps':len(valid),'expected_native_steps':expected}
          if len(valid)!=expected:return None,'Incomplete native6hour month: '+json.dumps(counts)
          annual.append(statistics.mean(valid));found=True
          sources.append({'provider':'Open-MeteoMarineAPI','url':meta['url'],'snapshot_file':str(path),'sha256':meta['sha256'],
           'json_pointer':f'/{idx}'if isinstance(json.loads(path.read_text()),list)else'','fetched_at':meta['requested_at'],
           'model_selector':'meteofrance_currents','underlying_model':'MERCATOR GLO12;CMEMS GLOBAL_ANALYSISFORECAST_PHY_001_024',
           'variable':'thetao exposedas sea_surface_temperature','depth_m':.49402499198913574,'license':'CC BY4.0',
           'api_entitlement':'D22freeAPIhackathon; productionpaidplan/selfhostrequired'})
          break
        if found:break
      if not found:return None,f'No authorized Open-Meteo complete{year}-{month:02} cachedseries.'
    value={'mean_C':statistics.mean(annual),'stddev_across_years_C':statistics.pstdev(annual),
      'annual_monthly_means_C':dict(zip(['2023','2024','2025'],annual)),'n_years':3,'native_6hour_completeness':counts}
    return {'value':value,'sources':sources,'mapping':m,'target_selection':{
        'selection_protocol':target.get('selection_protocol','legacy_walk_selected'),
        'selection_method':target.get('selection_method','walk_selected_marine'),
        'selection_distance_m':target.get('selection_distance_m'),
        'target_source_path':str(tp),'target_source_sha256':mapping['source_beach_targets_sha256'],
        'access_claim':'No confirmed publicaccess or hotel affiliation inferred, selection supplies marine modelcontext only.'}},None

def dump(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')

def climate_cell(h):
    # ERA5 native regular 0.25 degree grid, nearest; ties use Python round-even.
    return round(h['latitude']*4)/4,round(h['longitude']*4)/4

def climate_params(city,hs,year,month):
    cells=sorted(set(climate_cell(h) for h in hs));offset=UTC_OFFSET[city]
    return {'latitude':','.join(str(x[0])for x in cells),'longitude':','.join(str(x[1])for x in cells),
      'start_date':f'{year}-{month:02}-01','end_date':f'{year}-{month:02}-{calendar.monthrange(year,month)[1]}',
      'daily':'temperature_2m_mean,temperature_2m_max,precipitation_sum','models':'era5',
      'timezone':f'Etc/GMT-{offset}','cell_selection':'nearest','elevation':','.join(['nan']*len(cells))}

def fetch_climate(city):
    hs=hotels(city);cells=sorted(set(climate_cell(h) for h in hs));log=ENV/'http_ledger.jsonl';RAW.mkdir(parents=True,exist_ok=True)
    start=time.perf_counter();counts=Counter();conservative_weight=0
    session=requests.Session();session.headers['User-Agent']='HotelFeatureResearch/9.1 (non-commercial hackathon, cached once)'
    for year in YEARS:
      for month in MONTHS:
        params=climate_params(city,hs,year,month);key=hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()[:18]
        dest=RAW/f'era5_{city}_{year}{month:02}_{key}.json';meta=dest.with_suffix('.meta.json')
        if dest.exists() and meta.exists() and json.loads(meta.read_text()).get('http_status')==200:
            counts['cache_hit']+=1;continue
        days=calendar.monthrange(year,month)[1];weight=len(cells)*max(1,days/14);conservative_weight+=weight
        tick=time.perf_counter();requested=now()
        try:
            r=session.get('https://archive-api.open-meteo.com/v1/archive',params=params,timeout=60)
            dest.write_bytes(r.content)
            rec={'requested_at':requested,'finished_at':now(),'url':r.url,'http_status':r.status_code,'bytes':len(r.content),
                 'sha256':sha256(dest),'path':str(dest),'params':params,'n_locations':len(cells),
                 'conservative_weight_estimate':weight,'published_weight_estimate':len(cells)*days/14*.3,
                 'license':'CC BY 4.0 data; free API non-commercial hackathon D22 only; production Professional/self-host',
                 'elapsed_s':time.perf_counter()-tick}
            dump(meta,rec)
            with log.open('a')as f:f.write(json.dumps(rec,ensure_ascii=False)+'\n')
            counts[f'http_{r.status_code}']+=1
            if r.status_code in (401,403,429):
                print('STOP quota/authorization',r.status_code,r.text[:300],flush=True);break
            r.raise_for_status()
        except Exception as e:
            counts['failure']+=1;print(type(e).__name__,str(e)[:200],flush=True)
        # <=420 conservative weighted calls/minute, well below published600.
        pause=max(1.0,weight/7)-(time.perf_counter()-tick)
        if pause>0:time.sleep(pause)
      else:continue
      break
    result={'city_id':city,'at':now(),'runtime_s':time.perf_counter()-start,'hotel_count':len(hs),'cells':cells,
       'counts':dict(counts),'new_conservative_weight_estimate':conservative_weight,'months':list(MONTHS),
       'period':[1991,2020],'model':'ERA5 0.25 degree','timezone_policy':'fixed current local standard offset; no historical DST; explicitly retained',
       'temperature_downscaling':False}
    dump(ENV/f'fetch_{city}.json',result);print(json.dumps(result,ensure_ascii=False),flush=True)

def load_climate(city):
    records=defaultdict(list);provenance=defaultdict(list)
    for path in sorted(RAW.glob(f'era5_{city}_*.json')):
      if path.name.endswith('.meta.json'):continue
      meta_path=path.with_suffix('.meta.json')
      if not meta_path.exists():continue
      meta=json.loads(meta_path.read_text())
      if meta.get('http_status')!=200:continue
      data=json.loads(path.read_text());data=data if isinstance(data,list)else[data]
      for j,d in enumerate(data):
        cell=(float(d['latitude']),float(d['longitude']));daily=d['daily']
        for i,date in enumerate(daily['time']):
          records[cell].append({'date':date,'mean':daily['temperature_2m_mean'][i],'max':daily['temperature_2m_max'][i],
             'rain':daily['precipitation_sum'][i]})
        provenance[cell].append({'provider':'Open-Meteo Historical Weather API','url':meta['url'],'snapshot_file':str(path),
           'sha256':meta['sha256'],'json_pointer':f'/{j}'if isinstance(json.loads(path.read_text()),list)else'',
           'fetched_at':meta['requested_at'],'source_version':'ERA5 via Open-Meteo API; upstream release ID not exposed',
           'model':'ERA5','resolution_degrees':.25,'grid_latitude':d['latitude'],'grid_longitude':d['longitude'],
           'grid_elevation_m':d.get('elevation'),'timezone':d['timezone'],'utc_offset_seconds':d['utc_offset_seconds'],
           'license':'CC BY 4.0','api_entitlement':'D22 non-commercial hackathon; production Professional/self-host required'})
    return records,provenance

def climate_aggregate(rows,feature,month):
    field={'C01':'mean','C02':'rain','C03':'max'}[feature];annual=[];days=0;invalid=[]
    for year in YEARS:
      rr=[r for r in rows if r['date'].startswith(f'{year}-{month:02}-')]
      expected=calendar.monthrange(year,month)[1]
      unique={r['date']:r for r in rr}
      valid=[r[field]for r in unique.values()if r[field]is not None and math.isfinite(float(r[field]))]
      if len(unique)!=expected or len(valid)!=expected:invalid.append({'year':year,'expected':expected,'valid':len(valid)});continue
      days+=len(valid)
      annual.append(statistics.mean(valid) if feature=='C01' else sum(v>=1 for v in valid)if feature=='C02'else sum(v>30 for v in valid))
    if invalid or len(annual)!=30:return None,{'missing_years':invalid,'valid_year_count':len(annual),'valid_days':days}
    return {'mean':statistics.mean(annual),'stddev_across_years':statistics.pstdev(annual),
       'minimum_annual_value':min(annual),'maximum_annual_value':max(annual),'n_years':30,'valid_days':days,
       'annual_values':dict(zip(map(str,YEARS),annual))},None

def green_fraction(h):
    """Area weighted raster intersections in a hotel-centred AEQD projection.
    Land denominator excludes permanent water80 and missing0, not snow/ice70.
    2021 epoch stays historical. No current greenness or access claim.
    """
    lat,lon=h['latitude'],h['longitude'];tilelat=math.floor(lat/3)*3;tilelon=math.floor(lon/3)*3
    name=f'ESA_WorldCover_10m_2021_v200_N{tilelat:02}E{tilelon:03}_Map.tif';path=V4/'raw/raster_tiles'/name
    if not path.exists():return None,{'reason':'required WorldCover tile not cached','tile':name}
    projector=Transformer.from_crs('EPSG:4326',f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m',always_xy=True)
    radius=300.;circle=shapely.Point(0,0).buffer(radius,quad_segs=64);full_area=circle.area
    used_paths=[path]
    with rasterio.open(path)as ds:
      dx=330/(111000*math.cos(math.radians(lat)));dy=330/111000
      win=from_bounds(lon-dx,lat-dy,lon+dx,lat+dy,ds.transform).round_offsets().round_lengths()
      values=ds.read(1,window=win,boundless=True,fill_value=0);t=ds.window_transform(win);height,width=values.shape
      # Tile edges are not true no-data: use an adjacent already-cached tile
      # when the300m buffer crosses a3degreeWorldCover boundary. No HTTPfallback.
      for ay in range(math.floor((lat-dy)/3)*3,math.floor((lat+dy)/3)*3+1,3):
       for ax in range(math.floor((lon-dx)/3)*3,math.floor((lon+dx)/3)*3+1,3):
        adjacent=V4/'raw/raster_tiles'/f'ESA_WorldCover_10m_2021_v200_N{ay:02}E{ax:03}_Map.tif'
        if adjacent==path or not adjacent.exists():continue
        fill=np.zeros_like(values)
        with rasterio.open(adjacent)as extra:
          reproject(source=rasterio.band(extra,1),destination=fill,src_transform=extra.transform,src_crs=extra.crs,
              dst_transform=t,dst_crs=ds.crs,src_nodata=0,dst_nodata=0,resampling=Resampling.nearest)
        values=np.where(values==0,fill,values);used_paths.append(adjacent)
      yy,xx=np.indices((height+1,width+1));xs=t.c+xx*t.a;ys=t.f+yy*t.e
      xm,ym=projector.transform(xs,ys)
      vertices=np.stack([np.stack([xm[:-1,:-1],ym[:-1,:-1]],axis=-1),np.stack([xm[:-1,1:],ym[:-1,1:]],axis=-1),
          np.stack([xm[1:,1:],ym[1:,1:]],axis=-1),np.stack([xm[1:,:-1],ym[1:,:-1]],axis=-1)],axis=-2).reshape(-1,4,2)
      polygons=shapely.polygons(vertices);areas=shapely.area(shapely.intersection(polygons,circle));vals=values.reshape(-1)
      valid=np.isin(vals,[10,20,30,40,50,60,70,80,90,95,100]);water=vals==80;land=valid&~water;veg=np.isin(vals,VEG)
      valid_area=float(areas[valid].sum());land_area=float(areas[land].sum());veg_area=float(areas[land&veg].sum());water_area=float(areas[water].sum())
      coverage=valid_area/full_area
      if coverage<.99:return None,{'reason':'WorldCover valid area below99%','valid_fraction':coverage,'tile':name}
      if land_area<=0:return None,{'reason':'no classified land in300m buffer','water_area_m2':water_area,'tile':name}
      result={'vegetation_percent_land':100*veg_area/land_area,'vegetation_area_m2':veg_area,'land_area_m2':land_area,
        'water_area_m2':water_area,'missing_area_m2':max(0,full_area-valid_area),'buffer_area_m2':full_area,
        'valid_fraction':min(1.,coverage),'vegetation_percent_total_buffer_diagnostic':100*veg_area/full_area,
        'vegetation_classes':list(VEG),'excluded_water_class':80,'missing_class':0,'epoch':2021,
        'radius_m':300,'tile':name,'input_tiles':[p.name for p in used_paths],
        'raster_resolution_degrees':abs(ds.transform.a),'pixel_intersections':int((areas>0).sum()),
        'area_method':'pixel polygon intersections with256-segment300m circle in hotel-centred WGS84 AEQD; land denominator'}
    source={'provider':'ESA WorldCover','url':f'https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/{name}',
        'snapshot_file':str(path),'sha256':input_sha(str(path)),
        'source_version':'WorldCover2021v200','observed_at':None,'observation_epoch':2021,'license':'CC BY 4.0','resolution_nominal_m':10,
        'cached_input_origin':'V4 audit; read only; source fetch timestamp not recorded in original tile metadata'}
    source['additional_sources']=[dict(source,url=f'https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/{p.name}',
        snapshot_file=str(p),sha256=input_sha(str(p)))for p in used_paths[1:]]
    return result,source

def build_city(city):
    hs=hotels(city);cr,cp=load_climate(city);allfacts=[];runtimes={};start=time.perf_counter();cat=catalogue()
    t=time.perf_counter()
    for h in hs:
      value,source=green_fraction(h)
      rr={'E03-R1':'No newer licensed, dated, full-legend land-cover with common land mask cached for this hotel buffer.'}
      if value:
        allfacts.append(fact(h,'E03',value=value['vegetation_percent_land'],status='computed',unit='%',
          recipe_id='E03-R2',sources=[source]+source.get('additional_sources',[]),params={'radius_m':300,'epoch':2021,'vegetation_classes':list(VEG),'denominator':'classified land area excluding permanent water80'},
          scope_note='Historical2021 land-cover fraction, not current vegetation, park access, shade or hotel grounds.',
          observed_at=None,valid_to=None,refresh_after='2027-10-24T00:00:00+00:00',recipe_attempts=attempts('E03',rr,'E03-R2'),components=value,
          confidence={'level':'medium','reason':'FullvalidWorldCover raster intersection;2021classificationatnominal10m, no2026stabilityverified.'}))
      else:
        rr['E03-R2']=source['reason'];allfacts.append(fact(h,'E03',unknown_reason=source['reason'],recipe_attempts=attempts('E03',rr),params={'radius_m':300,'epoch':2021},components=source))
    runtimes['E']=time.perf_counter()-t;t=time.perf_counter()
    for h in hs:
      cell=climate_cell(h);rows=cr.get(cell,[])
      for fid in ('C01','C02','C03'):
       for month in MONTHS:
        params={'month':month,'period_start':'1991-01-01','period_end':'2020-12-31','model':'ERA5',
            'grid_latitude':cell[0],'grid_longitude':cell[1],'grid_resolution_deg':.25,
            'hotel_to_grid_m':GEOD.inv(h['longitude'],h['latitude'],cell[1],cell[0])[2],
            'model_grid_elevation_m':cp.get(cell,[{}])[0].get('grid_elevation_m'),
            'timezone':f'Etc/GMT-{UTC_OFFSET[city]}','daily_boundary_policy':'fixed local standard offset, no historicalDST',
            'temperature_downscaling':False,'trip_start':'2026-11-14'if month==11 else'2027-07-15'}
        if fid=='C02':params.update({'threshold_mm_per_day':1,'comparison':'>='})
        if fid=='C03':params.update({'threshold_C':30,'comparison':'>'})
        value,error=climate_aggregate(rows,fid,month)
        rr={fid+'-R1':'D22: direct Copernicus CDS is control-only; selected authorized Open-Meteo path for primary climate data.'}
        sources=[p for p in cp.get(cell,[])if f'-{month:02}-'in json.loads(Path(p['snapshot_file']).with_suffix('.meta.json').read_text())['params']['start_date']]
        if value:
          allfacts.append(fact(h,fid,value=value,status='computed',unit='°C'if fid=='C01'else'days/month',recipe_id=fid+'-R2',sources=sources,
           params=params,observed_at=None,valid_to=None,refresh_after='2027-10-24T00:00:00+00:00',recipe_attempts=attempts(fid,rr,fid+'-R2'),
           scope_note='Historical reanalysis monthly distribution across30completeyears; not forecast for2026/2027; nearest0.25degreegridair, nohotel-levelterrainadjustment.',
           confidence={'level':'medium','reason':'30/30complete months; explicitERA5; coarsecoastal/mountaingrid and0.1C APIrounding remain.'}))
        else:
          rr[fid+'-R2']='No complete30year month series in authorized cache: '+json.dumps(error)
          allfacts.append(fact(h,fid,unknown_reason='incomplete_authorized_1991_2020_month_series',params=params,sources=sources,recipe_attempts=attempts(fid,rr),components=error))
    runtimes['C']=time.perf_counter()-t;t=time.perf_counter()
    for h in hs:
      coastal=city in {'sochi','spb','istanbul','dubai','kaliningrad'}
      for fid in ('B01','B02','B03','B04','B05','C04','E01','E02','E04'):
        if fid.startswith('B') and not coastal:
          allfacts.append(fact(h,fid,status='not_applicable',unit=cat[fid]['unit'],params={'selection':'marine beach'},
              scope_note='City has no sea coast; inland lake/river beaches are a different selection.'))
          continue
        params={'trips':TRIPS}
        if fid=='B02':
          for month in MONTHS:
            result,error=marine_for_hotel(h,month)
            pp={'month':month,'period_start':'2023-01-01','period_end':'2025-12-31','depth_m':.49402499198913574,
                'variable':'sea_water_potential_temperature','model':'MERCATOR GLO12','sampling':'native6hourUTCsteps',
                'archive':'Open-MeteoMarine meteofrance_currents (MFSST)','trip_start':'2026-11-14'if month==11 else'2027-07-15'}
            rr={'B02-R1':'D22 excludes directCopernicus asprimary; R2OpenMeteo authorized samevariable/depth explicit2023–2025period evaluated.'}
            if result:
              m=result['mapping'];native=marine_native_coordinate(m['model_latitude'],m['model_longitude']);m.update(native)
              pp.update({'beach_id':m['beach_id'],'grid_latitude':native['native_cf_latitude'],'grid_longitude':native['native_cf_longitude'],
                  'api_reported_latitude':m['model_latitude'],'api_reported_longitude':m['model_longitude'],
                  'beach_to_api_point_m':m['beach_to_cell_m'],
                  'beach_to_native_cf_m':GEOD.inv(m['requested_beach_longitude'],m['requested_beach_latitude'],native['native_cf_longitude'],native['native_cf_latitude'])[2],
                  'native_cf_worldcover2021_class':native['native_coordinate_worldcover2021_class'],'wet_cell_evidence':native['wet_cell_evidence']})
              pp.update(result['target_selection'])
              if m.get('rejected_initial_cell'):
                pp['wet_cell_selection_method']=m.get('wet_cell_selection_method','No validated replacement for initial landpoint')
                pp['initial_wet_cell_rejection']={k:v for k,v in m['rejected_initial_cell'].items()if k!='probe_source'}
              allfacts.append(fact(h,'B02',value=result['value'],status='estimate',unit='°C',params=pp,
                recipe_id='B02-R2',sources=result['sources']+[m['source']],observed_at=None,valid_to=None,
                refresh_after='2027-10-24T00:00:00+00:00',recipe_attempts=attempts('B02',rr,'B02-R2'),
                scope_note='3yeararchivedmodelthetaoat0.494m; notclimatenorm,beachthermometer,skinSSTor2026/2027forecast. Importer/depthaudit storedenvironment/raw; APIdeploymentcommitnotexposed.',
                confidence={'level':'low','reason':'D18short3yeararchive;potentialthetao/depthverifiedviaupstreamNetCDFandpinnedimporter; coarsecoastalwetcell, sourceversionchanges andAPIrounding.'},
                audit_files=['environment/raw/marine_netcdf_depth_metadata.json','environment/raw/marine_netcdf_coordinate_metadata.json','environment/raw/MfWaveDownload.swift','environment/raw/ForecastapiController.swift'],
                components={'marine_spatial_support':native},algorithm_version='v9.2_marine_context'))
            else:
              rr['B02-R2']=error;allfacts.append(fact(h,'B02',unknown_reason=error,unit='°C',params=pp,recipe_attempts=attempts('B02',rr)))
          continue
        if fid=='B01':reason='No dated official material descriptor with matched named beach segment/extent; hotel marketing sand/pebble claims are not equivalent official data.'
        elif fid=='B03':reason='Both requested trips beyond the16day marine forecast horizon at2026-10-24; no valid modelrun/lead/wetcell for requested dates.'
        elif fid=='B04':reason='No matched authorized latest beach flag/status feed withissuer andobservationtime; future2026/2027status cannot be inferred fromhistoricalorweather.'
        elif fid=='B05':reason='No published authorized labprotocol matchedtohotel-selected beachsamplepoint withsamplingdate/method/units.'
        elif fid=='C04':reason='Requested trips beyond16day forecast horizon; no valid ensembleprobabilityrun. Historical rainy-dayfrequency is not rainprobabilityforecast.'
        elif fid=='E01':reason='Requested trips beyondOpenMeteoairqualityAPImaximum7day horizon(default5days); no valid CAMSmodelrun for requested dates. Cached2026-10-23PM2.5 not transferable.'
        elif fid=='E02':reason='D22 selects Open-Meteo asprimaryairsource; thisAPI exposesCAMSmodelPM2.5, no eligible stationmeasurements withstation/QC/timeaveraging. Directofficial/OpenAQstationpaths notapprovedunderD22; CAMSmodelE01 cannotreplaceobservation.'
        else:reason='No corrected BlackMarble monthlyA3 radiance plusQA/cloud/snow validpixel cached; no sufficient correctedA2 dailyseries. UncorrectedA1/otherVIIRS products rejected as different estimand.'
        rr={r['recipe_id']:reason for r in cat[fid]['recipes']}
        if fid=='B01':rr['B01-R2']='No dated hotelierphoto for the selected namedbeachsegment withverifiedextent; cachedOTAmarketingphotos are claims, not licensed datedsegmentinspection.'
        if fid=='B03':rr['B03-R1']='D22 excludes directCopernicusforecast asprimary; requested dates beyondavailableforecast anyway.'
        if fid=='B04':rr['B04-R2']='No hotelier-supplied timestamped officialflag/photo forselectedbeach; genericseason/news not a fresh beach_id status.'
        if fid=='B05':rr['B05-R2']='No hotelier-supplied verifiableofficiallabprotocol withsamplepoint/date/method; clean-watermarketing rejected.'
        if fid=='E01':rr['E01-R1']='D22 excludes directCAMS asprimary; OpenMeteoCAMS pathR2 examined fordatecoverage.'
        if fid=='E02':rr['E02-R2']='OpenAQdirectstationprimary notapprovedunderD22; providerlicense/APIauthorization/freshnearestsensor have notbeenestablished. No OpenMeteostation equivalent.'
        if fid=='E04':
          rr['E04-R1']='No corrected monthlyBlackMarbleA3 radiance+QA/cloud/snow productpixel cached forselectedmonth.'
          rr['E04-R2']='No corrected dailyBlackMarbleA2 QA-passing series sufficienttoreproduce the samemonthlyestimand; otherVIIRS/nightimageswrongproduct.'
        allfacts.append(fact(h,fid,unknown_reason=reason,unit=cat[fid]['unit'],params=params,recipe_attempts=attempts(fid,rr),
           scope_note='Unknown is an input/semantic/time gate, not false. Hotelier questions cannot replace model/archive dependencies.'))
    runtimes['B_and_other_gates']=time.perf_counter()-t
    dest=OUT/f'{city}_facts.jsonl';write_jsonl(dest,allfacts)
    by=defaultdict(Counter)
    for f in allfacts:by[f['feature_id']][f['status']]+=1
    summary={'city_id':city,'hotel_count':len(hs),'fact_rows':len(allfacts),'feature_ids':FEATURES,'status_by_feature':{k:dict(v)for k,v in by.items()},
       'runtime_s':time.perf_counter()-start,'family_runtime_s':runtimes,'result_bytes':dest.stat().st_size,'result_sha256':sha256(dest),
       'source_fetch_runtime_s':json.loads((ENV/f'fetch_{city}.json').read_text()).get('runtime_s')if(ENV/f'fetch_{city}.json').exists()else None,
       'marine_source_fetch_runtime_s':json.loads((ENV/f'marine_mapping_{city}.json').read_text()).get('runtime_total_measured_s',json.loads((ENV/f'marine_mapping_{city}.json').read_text()).get('runtime_s'))if(ENV/f'marine_mapping_{city}.json').exists()else None,
       'note':'C01–03andB02eachhave2monthcontexts. Allotherfeatures1rowperhotel. No newOstrovokHTTP.'}
    dump(OUT/f'{city}_summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)

def tests():
    # Boundary operators, completeness, no missing-as-zero, exact yearly aggregation.
    rows=[]
    for y in YEARS:
      for d in range(1,32):rows.append({'date':f'{y}-07-{d:02}','mean':20.,'max':30. if d%2 else 31.,'rain':1. if d%2 else .999})
    v,e=climate_aggregate(rows,'C01',7);assert e is None and v['mean']==20 and v['valid_days']==930
    v,e=climate_aggregate(rows,'C02',7);assert v['mean']==16
    v,e=climate_aggregate(rows,'C03',7);assert v['mean']==15
    rows[0]['mean']=None;v,e=climate_aggregate(rows,'C01',7);assert v is None and e['valid_year_count']==29
    # Vector raster polygon sanity: geometry intersection correctly excludes waterdenominator.
    areas=np.array([1.,3.,6.]);classes=np.array([10,50,80]);land=(classes!=0)&(classes!=80)
    assert 100*areas[np.isin(classes,VEG)&land].sum()/areas[land].sum()==25
    p=marine_native_coordinate(43.625,39.708344)
    assert p['native_latitude_index']==1483 and p['native_longitude_index']==2636
    assert abs(p['native_cf_latitude']-43.583333333333336)<1e-5 and p['native_coordinate_worldcover2021_class']==80
    v,e=green_fraction({'latitude':42.0001,'longitude':41.8})
    assert v is not None and v['valid_fraction']>.999 and len(v['input_tiles'])==2
    dump(QA/'unit_tests.json',{'at':now(),'status':'passed','checks':['C01meancomplete30years','C02>=1mmboundary','C03>30Cboundary','missingdailyrejectsmonth','landdenominatorexcludeswater','marineAPItoCFgridindexroundtrip','marineCFcandidateindependentWorldCoverwatercheck','cachedtileedge_mosaic_not_false_missing']})
    print('passed8meaningfulcontractchecks')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['fetch','marine','compute','test']);p.add_argument('--city',default='sochi');p.add_argument('--marine-targets');p.add_argument('--cache-only',action='store_true');a=p.parse_args()
    {'fetch':lambda:fetch_climate(a.city),'marine':lambda:fetch_marine(a.city,a.marine_targets,a.cache_only),'compute':lambda:build_city(a.city),'test':tests}[a.action]()
