"""V9 physical facade/height fans and building history, entirely from cached inputs.

Usage: V4/.venv/bin/python scripts/facades.py --city sochi
Writes results/facades/{city}_{facts,building_facts,building_matches,fan,diagnostics}.jsonl
and {city}_summary.json. No network or writes on import. All window calculations
are quality-gated: hypothetical midpoint/floor grids are diagnostic components,
never a promise about an existing room. Reusable pure LOS/sun functions are tested
in qa/facades/test_scenes.py. OSM heights/levels and cached OTA fields are preserved
with their original semantics. DSM is never treated as ground terrain.
"""
from __future__ import annotations
import argparse,csv,gzip,json,math,re,sys,time,unicodedata
from collections import Counter,defaultdict
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
import numpy as np
import pandas as pd
from shapely import STRtree,wkb
from shapely.geometry import Point,LineString,Polygon,MultiPolygon
from shapely.geometry.polygon import orient
from shapely.ops import transform
from pyproj import CRS,Transformer

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/'vendor'))
import pvlib
from common import BASE,ROOT,V4,TZ,TRIPS,hotels,fact as common_fact,attempts,read_jsonl,write_jsonl,sha256,now

OUT=BASE/'results/facades'
FIDS=[f'V0{i}' for i in range(1,7)]+[f'S0{i}' for i in range(1,4)]+[f'H0{i}' for i in range(1,4)]
FANFIDS=FIDS[:9]
CYR=dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя', ['a','b','v','g','d','e','e','zh','z','i','i','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya']))
GENERIC={'ulitsa','ul','street','str','road','prospekt','avenue','pereulok','lane','naberezhnaya','embankment','shosse','highway','bulvar','boulevard','imeni','sokak','sokagi','caddesi','cad','sk','cd','mahallesi','district','d','dom','korpus','litera'}
CENTERS={'sochi':(39.7231,43.5855),'moscow':(37.6173,55.7558),'spb':(30.3158,59.9386),'kazan':(49.1064,55.7961),'kaliningrad':(20.5,54.71),'minsk':(27.56,53.9),'istanbul':(28.98,41.01),'dubai':(55.27,25.2)}

FACADES_VERSION='v9.2.facades'
def fact(*args,**kwargs):
    kwargs.setdefault('algorithm_version',FACADES_VERSION)
    return common_fact(*args,**kwargs)

def normalize_house_notation(text):
    """Preserve complete house suffix/corpus, including spaced Cyrillic letters.
    13 A == 13А, while 13 != 13А; 19/21 к.2 != 19/21.
    This is a lexical gate, not a substitute for building identity evidence.
    """
    n=norm(text)
    n=re.sub(r'(?<=\d)\s+(?:lit|litera)\s+([a-z])\b',r'\1',n)
    rx=r'(?P<base>\d+[a-z]?(?:/\d+[a-z]?)?)\s*(?P<kind>korpus|korp|kor|stroenie|str|k)(?:\s+(?P<code>[0-9]+[a-z]?|[a-z])\b|(?P<numcode>\d+[a-z]?)\b)'
    def sub(m):
        kind='str' if m['kind'] in ['str','stroenie'] else 'k'
        return m['base']+kind+(m['code'] or m['numcode'])
    n=re.sub(rx,sub,n)
    return re.sub(r'(?<=\d)\s+([a-z])\b(?!\s*\d)',r'\1',n)

def full_house_gate(house,address):
    hn=normalize_house_notation(house).replace(' ','');an=normalize_house_notation(address)
    return bool(hn and re.search(r'(?<![a-z0-9/])'+re.escape(hn)+r'(?![a-z0-9/])',an))

def dump(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n')

def norm(s):
    s=''.join(CYR.get(c,c) for c in str(s).casefold())
    s=''.join(c for c in unicodedata.normalize('NFKD',s) if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9/]+',' ',s).strip()

def phone(s):return ''.join(c for c in str(s or '') if c.isdigit())

def positive_number(s):
    if s is None:return None
    s=str(s).strip().replace(',','.')
    if not re.fullmatch(r'\d+(?:\.\d+)?(?:\s*m)?',s):return None
    try:v=float(s.replace('m','').strip());return v if 0<v<1000 else None
    except ValueError:return None

def integer_levels(t):
    n=positive_number(t.get('building:levels'))
    return int(n) if n is not None and n.is_integer() and n<=180 else None

def ota_object(h):
    d=json.loads(gzip.decompress(Path(h['snapshot_file']).read_bytes()))
    for token in h['json_pointer'].strip('/').split('/'):
        d=d[int(token)] if isinstance(d,list) else d[token]
    return d

def address_evidence(h,t):
    """Conservative transliterated exact house + core street tokens, corpus veto.
    Coordinates narrow candidates; they do not constitute address evidence.
    """
    street=t.get('addr:street') or t.get('addr:place');house=t.get('addr:housenumber')
    if not(street and house):return None
    st=[x for x in norm(street).split() if x not in GENERIC]
    hn=normalize_house_notation(house).replace(' ','')
    if not st or not hn:return None
    addresses=[h.get('address','')]
    override_path=BASE/'facades/external_match_evidence.json'
    override=json.loads(override_path.read_text()).get(str(h.get('hotel_id'))) if override_path.exists() else None
    if override:
        obj=ota_object(h) if h.get('has_room_snapshot') else {}
        if phone(obj.get('contacts',{}).get('phone'))==override['required_ota_phone']:
            addresses=[override['address']]
        else:override=None
    if h.get('has_room_snapshot'):
        reg=ota_object(h).get('facts',{}).get('register')
        if isinstance(reg,dict) and reg.get('address') and not override:addresses=[reg['address']]
    for a in addresses:
        an=normalize_house_notation(a);tokens=an.split()
        # Sufficient street support: all street core tokens, allowing common -iy/-yi aliases.
        canon=lambda z:z.replace('yy','y').replace('iy','y').replace('yi','y').replace('sch','shch')
        if not all(any(canon(s)==canon(x) for x in tokens) for s in st):continue
        # Do not accept the street's numerical token as the house number.
        if not full_house_gate(house,a):continue
        if hn in st and sum(x==hn for x in tokens)<2:continue
        return {'street':street,'housenumber':house,'ota_address_used':a,'rule':'exact_full_house_suffix_corpus_and_core_street','normalized_house':hn,'normalized_address':an,'external_match_source':(override['source_url'] if override else None)}
    return None

def osm_source(r,path):
    return {'provider':'OpenStreetMap','source_id':f"{r['osm_type']}/{r['osm_id']}",
       'url':f"https://www.openstreetmap.org/{r['osm_type']}/{r['osm_id']}",'version':r.get('version'),
       'object_edited_at':r.get('timestamp'),'snapshot_file':str(path),'license':'ODbL-1.0',
       'attribution':'© OpenStreetMap contributors','license_url':'https://www.openstreetmap.org/copyright',
       'physical_observation_at':None}

def dataset_path(city):
    if city=='sochi' and (BASE/'cohort/osm_sochi_extended.jsonl').exists():return BASE/'cohort/osm_sochi_extended.jsonl'
    return V4/'raw'/f"osm_{'saint_petersburg' if city=='spb' else city}.jsonl"

def load_scene(city):
    x0,y0=CENTERS[city];crs=CRS.from_proj4(f'+proj=aeqd +lat_0={y0} +lon_0={x0} +datum=WGS84 +units=m')
    tr=Transformer.from_crs(4326,crs,always_xy=True);inv=Transformer.from_crs(crs,4326,always_xy=True)
    path=dataset_path(city);records=[];polys=[];geo=[];sites=[];siteg=[];targets=[];counts=Counter()
    for r in read_jsonl(path):
        t=r['tags'];building=t.get('building') not in (None,'no') or t.get('building:part') not in (None,'no')
        hotel=t.get('tourism') in ('hotel','guest_house','hostel','apartment','motel','resort')
        target=t.get('natural') in ('peak','coastline','beach') or t.get('historic') is not None or t.get('leisure') in ('park','garden')
        if not(building or hotel or target):continue
        g=wkb.loads(r['geometry_wkb'],hex=True)
        if g.is_empty:continue
        if building and g.geom_type in ('Polygon','MultiPolygon'):
            if not g.is_valid:g=g.buffer(0)
            if not g.is_valid or g.is_empty:counts['invalid_building']+=1;continue
            records.append(r);geo.append(g);polys.append(transform(tr.transform,g));counts['buildings']+=1
            counts['levels']+=integer_levels(t) is not None;counts['metric_height']+=positive_number(t.get('height')) is not None
        if hotel and g.geom_type in ('Polygon','MultiPolygon'):
            sites.append(r);siteg.append(transform(tr.transform,g))
        if target:targets.append({'record':r,'geometry':g})
    return {'path':path,'records':records,'geoms':polys,'geogs':geo,'tree':STRtree(polys),'sites':sites,'sitegeoms':siteg,'sitetree':STRtree(siteg),'targets':targets,'counts':dict(counts),'project':tr,'inverse':inv,'crs':crs.to_string()}

def match_hotels(city,hh,scene):
    matches=[];diagnostics=[]
    for h in hh:
        point=Point(*scene['project'].transform(h['longitude'],h['latitude']));idxs=scene['tree'].query(point.buffer(80))
        accepted={};cand=[];ot=ota_object(h) if h.get('has_room_snapshot') else {};contact=phone(ot.get('contacts',{}).get('phone'))
        for i0 in idxs:
            i=int(i0);r=scene['records'][i];g=scene['geoms'][i];t=r['tags'];d=point.distance(g);ae=address_evidence(h,t)
            item={'osm_id':f"{r['osm_type']}/{r['osm_id']}",'distance_m':d,'contains_ota_point':g.covers(point),'address_evidence':ae,'building_levels':t.get('building:levels'),'height':t.get('height'),'name':t.get('name')}
            cand.append(item)
            if ae and (d<=40 or g.covers(point)):
                accepted[i]={'rule':'full_address_and_spatial_consistency','address':ae,'contains_ota_point':g.covers(point),'distance_m':d}
        # Strong named hotel site with phone agreement admits its addressed buildings,
        # maintaining many-to-many relation rather than choosing one nearest footprint.
        for si0 in scene['sitetree'].query(point.buffer(150)):
            si=int(si0);sr=scene['sites'][si];sg=scene['sitegeoms'][si];st=sr['tags']
            if not contact or contact!=phone(st.get('phone') or st.get('contact:phone')):continue
            if not sg.buffer(30).covers(point):continue
            for i0 in scene['tree'].query(sg):
                i=int(i0);r=scene['records'][i];g=scene['geoms'][i]
                ae=address_evidence(h,r['tags'])
                if not ae or not sg.covers(g.representative_point()):continue
                accepted[i]={'rule':'exact_public_phone_hotel_site_plus_building_address','address':ae,'site_osm_id':f"{sr['osm_type']}/{sr['osm_id']}",'site_phone':st.get('phone') or st.get('contact:phone'),'contains_ota_point':g.covers(point),'distance_m':point.distance(g)}
        # Do not promote a different entrance/corpus merely because of containing point.
        for i,ev in sorted(accepted.items(),key=lambda z:(z[1]['distance_m'],z[0])):
            r=scene['records'][i];sid=f"{r['osm_type']}/{r['osm_id']}"
            matches.append({'city_id':city,'hotel_id':str(h['hotel_id']),'name':h['name'],'osm_id':sid,'osm_type':r['osm_type'],'osm_numeric_id':r['osm_id'],'geometry_wkb':r['geometry_wkb'],'tags':r['tags'],'version':r.get('version'),'timestamp':r.get('timestamp'),'match_status':'accepted_research_address_relation','match_evidence':ev,'distance_m':ev['distance_m'],'source':osm_source(r,scene['path']),'source_cohort':h['sources'],'room_category_mapping':'unknown','all_hotel_buildings_census':False,'scene_index':i})
        diagnostics.append({'city_id':city,'hotel_id':h['hotel_id'],'name':h['name'],'address':h['address'],'accepted_n':len(accepted),'candidates':sorted(cand,key=lambda x:x['distance_m']),'unknown_reason':None if accepted else ('outside_cached_spatial_coverage_or_no_footprints' if not cand else 'no_exact_address_building_match'),'rule':'nearest_only_never_accepted'})
    return matches,diagnostics

def facade_segments(geom):
    """Every exterior segment, outward normal, including courtyard-facing sides.
    No building simplification: every nonzero segment remains explicit. Shared
    walls are flagged separately at each floor height, never removed universally.
    """
    polys=[geom] if geom.geom_type=='Polygon' else list(geom.geoms)
    sides=[]
    for pi,p in enumerate(polys):
        p=orient(p,sign=1)
        for ri,ring in enumerate([p.exterior,*p.interiors]):
            coords=list(ring.coords)
            for ei,(a,b) in enumerate(zip(coords,coords[1:])):
                dx=b[0]-a[0];dy=b[1]-a[1];length=math.hypot(dx,dy)
                if length<.01:continue
                # CCW exterior and CW interior both have building on left.
                nx,ny=dy/length,-dx/length
                x,y=(a[0]+b[0])/2,(a[1]+b[1])/2
                sides.append({'side_id':f'p{pi}:r{ri}:e{ei}','a':list(a),'b':list(b),'x':x,'y':y,'normal':[nx,ny],'azimuth_deg':math.degrees(math.atan2(nx,ny))%360,'length_m':length,'courtyard':ri>0})
    return sides

def height_interval(tags):
    height=positive_number(tags.get('height'));levels=integer_levels(tags)
    if height:return {'min_m':height,'max_m':height,'basis':'OSM height tag; source-reported metric, physical survey unverified','status':'observed_tag'}
    if levels:return {'min_m':2.4*levels,'max_m':4.5*levels,'basis':'explicit exploratory assumption 2.4–4.5m per aboveground level, not a validated uncertainty interval','status':'scenario_only'}
    return {'min_m':None,'max_m':None,'basis':'No permitted metric height or aboveground levels; no default floorcount assigned','status':'unknown'}

def ray_building_intersections(origin,azimuth_deg,obstacles,max_distance=1000):
    """Return footprint hits in metric plane. Height is evaluated separately."""
    a=math.radians(azimuth_deg);end=(origin[0]+max_distance*math.sin(a),origin[1]+max_distance*math.cos(a));line=LineString([origin,end]);hits=[]
    for obstacle in obstacles:
        g=obstacle['geometry']
        if not g.intersects(line):continue
        z=g.intersection(line);dist=Point(origin).distance(z)
        hits.append({'distance_m':dist,'height_min_m':obstacle.get('height_min_m',0),'height_max_m':obstacle.get('height_max_m',math.inf),'id':obstacle.get('id')})
    return sorted(hits,key=lambda r:r['distance_m'])

def los_interval(origin,observer_height_interval,target_xy,target_height_interval,obstacles):
    """2.5D line-of-sight trivalence for a completely specified metric scene.
    Relative heights must share a known datum. Unknown obstacle upper height
    prevents positive visibility, whereas a robustly high wall can prove false.
    This pure function does not infer scene completeness or ground datum.
    """
    dx,dy=target_xy[0]-origin[0],target_xy[1]-origin[1];dist=math.hypot(dx,dy)
    if dist<=0:return {'visible':None,'reason':'degenerate_target'}
    az=math.degrees(math.atan2(dx,dy))%360;hits=ray_building_intersections(origin,az,obstacles,dist)
    uncertain=[]
    for hit in hits:
        if hit['distance_m']>=dist:continue
        q=hit['distance_m']/dist
        lower=observer_height_interval[0]*(1-q)+target_height_interval[0]*q
        upper=observer_height_interval[1]*(1-q)+target_height_interval[1]*q
        if hit['height_min_m']>upper:return {'visible':False,'reason':'obstacle_above_entire_ray_interval','blocker':hit['id'],'distance_m':dist}
        if hit['height_max_m']>=lower:uncertain.append(hit['id'])
    return {'visible':None if uncertain else True,'reason':'uncertain_obstacle_height' if uncertain else 'clear_in_supplied_complete_scene','uncertain_obstacles':uncertain,'distance_m':dist}

def solar_geometry(lat,lon,day,tz,step_minutes=5):
    """NREL SPA solar position, IANA local time. Midpoint integration, <=5min grid.
    Geometric elevation (not refraction) for geometric direct sun. Astronomical
    sunset defined as center elevation -0.833deg only for last-Nmin selection.
    """
    start=pd.Timestamp(day,tz=tz);stop=start+pd.DateOffset(days=1)
    times=pd.date_range(start,stop,freq=f'{step_minutes}min',inclusive='left')+pd.Timedelta(minutes=step_minutes/2)
    pos=pvlib.solarposition.spa_python(times,lat,lon)
    return times,pos

def orientation_components(normal_azimuth_deg,times,pos,step_minutes=5,last_minutes=30):
    elev=pos['elevation'].to_numpy();az=pos['azimuth'].to_numpy();inc=np.cos(np.deg2rad(az-normal_azimuth_deg))*np.cos(np.deg2rad(elev));front=(inc>1e-10)&(elev>0)
    local_hours=times.hour.to_numpy()+times.minute.to_numpy()/60
    afternoon=(local_hours>=14)&(local_hours<18)
    # Explicit integration bound due to 5min discretisation, no shadow interpretation.
    upper=float(front.sum()*step_minutes/60);upper_pm=float((front&afternoon).sum()*step_minutes/60)
    after_noon=np.flatnonzero(local_hours>=12)
    sunset_candidates=after_noon[elev[after_noon]<=-0.833]
    si=int(sunset_candidates[0]) if len(sunset_candidates) else None
    if si is None:sv=None;sunset_az=None;sunset_time=None
    else:
        mask=(np.arange(len(elev))>=max(0,si-math.ceil(last_minutes/step_minutes)))&(np.arange(len(elev))<si)
        disk_front=np.cos(np.deg2rad(az-normal_azimuth_deg))>0
        sv=bool((disk_front&mask).any());sunset_az=float(az[si]);sunset_time=times[si].isoformat()
    return {'unobstructed_orientation_sun_hours_upper':upper,'unobstructed_14_18_hours_upper':upper_pm,'canonical_hours_interval':[0,min(24,upper+2*step_minutes/60)],'canonical_14_18_interval':[0,min(4,upper_pm+2*step_minutes/60)],'sunset_direction_in_front_hemisphere':sv,'sunset_azimuth_deg_approx':sunset_az,'sunset_local_time_grid':sunset_time,'integration_step_minutes':step_minutes,'orientation_grid_uncertainty_hours':2*step_minutes/60,'upper_bound_is_grid_approximation':True,'point_existing_window_confirmed':False,'shadow_scene_complete':False,'canonical_S01_computed':False,'canonical_V05_computed':False}

def same_datums_known(scene):return False

def per_hotel_facts(h,mm,scene):
    sources=h['sources'];rows=[];bf=[];ot=ota_object(h);ofa=ot.get('facts',{});matched_sources=[m['source'] for m in mm]
    for fid in FANFIDS:
        reasons={'default':'No verified existing window coordinates/eye height and category-to-facade mapping; source heights/scene and terrain datum do not pass complete 3D quality gate.'}
        if fid=='S03':reasons['default']+=' No admitted versioned monthly plane irradiance series/shadow mask.'
        if fid in ['V02','V03','V04']:reasons['default']+=' A specific versioned target/volume and query angular threshold have not been verified.'
        if not mm:reason='no_building_match'
        else:reason='no_verified_window_height_ground_datum_and_complete_obstacles'
        rows.append(fact(h,fid,status='unknown',unknown_reason=reason,sources=matched_sources or sources,params={'trips':TRIPS,'timezone':h['timezone'],'target_scope':'existing_window_not_hypothetical_facade_midpoint','fan_file':str(OUT/f"{h['city_id']}_fan.jsonl")},recipe_attempts=attempts(fid,reasons),scope_note='Hypothetical facade×physical ordinal grid is retained separately as diagnostic, not a fact about an actual room/window.'))
    for m in mm:
        t=m['tags'];sid=m['osm_id'];src=[m['source']];params={'building_match_evidence':m['match_evidence']}
        levels=integer_levels(t)
        if levels:
            skipped=int(positive_number(t.get('building:min_level')) or 0)
            value={'aboveground_levels':max(0,levels-skipped),'raw_building_levels':levels,'skipped_levels_below_raised_part':skipped,'counting_convention':'OSM building:levels includes ground floor and excludes basement and roof levels; mezzanine handling as mapped, not independently inspected','roof_levels':t.get('roof:levels'),'underground_levels':t.get('building:levels:underground'),'building_part':bool(t.get('building:part')),'not_room_floor_labels':True}
            bf.append(fact(h,'H03',value=value,status='observed',unit='aboveground_storeys',params=params,sources=src,recipe_id='H03-R2',subject_level='building_part' if t.get('building:part') else 'building',subject_id=sid,observed_at=None,valid_to=None,recipe_attempts=attempts('H03',{'H03-R1':'No matched current official cadastre record with permitted reuse and counting convention.'},'H03-R2'),confidence={'level':'medium','reason':'Address-linked OSM source value under OSM counting convention; no physical survey/date. Source edit time is not observation time.'},scope_note='Only the individually matched footprint. No all-buildings census or category mapping.'))
        # Raw building start_date is retained as a component in geometry_quality.
        # It is not emitted as H01, even estimate: construction completion is a
        # different event and must pass the same-semantic gate (D15).
    for fid in ['H01','H02','H03']:
        vals=[r for r in bf if r['feature_id']==fid]
        if len(mm)==1 and len(vals)==1 and fid=='H03':
            rows.append(vals[0])
        else:
            reason='no_building_match' if not mm else ('multiple_buildings_or_counting_scope_conflict' if len(mm)>1 and fid=='H03' else 'no_current_official_heritage_registry_match' if fid=='H02' else 'no_verified_construction_completion_event' if fid=='H01' else 'missing_aboveground_levels')
            rows.append(fact(h,fid,status='unknown',unknown_reason=reason,sources=matched_sources or sources,params={'candidate_building_ids':[m['osm_id'] for m in mm]},recipe_attempts=attempts(fid,{'default':reason}),scope_note='Hotel-wide result is withheld when object/meaning differs. Individual building facts/components remain available.'))
    return rows,bf

def compare_ota(h,mm):
    if not h.get('has_room_snapshot'):return []
    obj=ota_object(h);data=obj.get('facts',{});rows=[]
    common={'hotel_id':h['hotel_id'],'city_id':h['city_id'],'name':h['name'],'ota_url':h['hotel_url'],'snapshot':h['snapshot_file']}
    floors=data.get('floorsNumber')
    if floors:
        mapped={m['osm_id']:integer_levels(m['tags']) for m in mm}
        rows.append({**common,'feature_id':'H03','ota_value':floors,'other_value':mapped,'ota_pointer':h['json_pointer']+'/facts/floorsNumber','classification':'raw_numbers_agree_not_verified_counting_convention' if len(mapped)==1 and list(mapped.values())[0]==floors else 'different_building_scope_or_counting_convention_not_comparable' if mapped else 'no_building_match_or_levels','resolution':'Do not declare either source wrong without per-corpus scope/counting convention.'})
    year=data.get('yearBuilt')
    if year:
        yrs={m['osm_id']:m['tags'].get('start_date') for m in mm if m['tags'].get('start_date')}
        rows.append({**common,'feature_id':'H01','ota_value':year,'other_value':yrs,'ota_pointer':h['json_pointer']+'/facts/yearBuilt','classification':'construction_event_not_verified_start_date_is_candidate' if yrs else 'no_independent_construction_completion_event','resolution':'Opening, existence, reconstruction and completion are separate events; request building document.'})
    sea=[]
    for i,r in enumerate(obj.get('roomGroups',[])):
        name=r.get('nameStruct',{}).get('mainName','')
        if re.search(r'вид.{0,12}мор|sea.?view',name,re.I):sea.append({'index':i,'rgHash':r.get('rgHash'),'name':name})
    if sea:rows.append({**common,'feature_id':'V01','ota_value':sea,'other_value':None,'ota_pointer':h['json_pointer']+'/roomGroups','classification':'category_claim_vs_unmapped_window_fan_not_comparable','resolution':'Source sea-view category claim does not map to specific facade/window/height or guaranteed allocation.'})
    return rows

def write_fan_facts(city,fan,hh):
    byid={str(h['hotel_id']):h for h in hh};count=0
    path=OUT/f'{city}_fan_facts.jsonl.gz'
    with gzip.open(path,'wt',encoding='utf-8') as out:
        for cell in fan:
            h=byid[str(cell['hotel_id'])]
            for fid in FANFIDS:
                dates=[x['date'] for x in cell['components']['solar_orientation_bounds']]
                pp=[{'date':d} for d in dates] if fid in ['V05','S01','S02'] else [{'month':m} for m in sorted({x[:7] for x in dates})] if fid=='S03' else [{}]
                for p in pp:
                    p.update({'timezone':h['timezone'],'facade_id':cell['facade_id'],'physical_floor_ordinal':cell['physical_floor_ordinal'],'window_coordinates_verified':False,'scenario_point_lonlat':cell['window_scenario_point_lonlat'],'scenario_azimuth_deg':cell['facade_azimuth_deg'],'height_interval_m':cell['height_interval_m'],'height_interval_basis':cell['height_interval_basis']})
                    if fid=='S02':p['local_time_interval']=['14:00','18:00']
                    if fid=='V05':p['last_minutes_before_astronomical_sunset']=30
                    reasons={'default':'No verified existing window/sector/eye height, common ground datum, and complete dated obstacle/vegetation/overhang scene. Diagnostic facade midpoint is a scenario, not an actual window.'}
                    if fid=='S03':reasons['default']+=' No admitted monthly plane irradiance series.'
                    row=fact(h,fid,status='unknown',unknown_reason='no_verified_existing_window_height_and_complete_scene',params=p,sources=[cell['geometry_source']],subject_level='facade_physical_floor',subject_id=f"{cell['building_id']}:{cell['facade_id']}:floor{cell['physical_floor_ordinal']}",recipe_attempts=attempts(fid,reasons),scope_note='Physical ordinal is source-count based; local room floor label and actual window mapping unknown. Continuous geometry diagnostic components are in separate fan file.',depends_on=[{'file':str(OUT/f'{city}_fan.jsonl'),'building_id':cell['building_id'],'facade_id':cell['facade_id'],'physical_floor_ordinal':cell['physical_floor_ordinal']}])
                    out.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n');count+=1
    return count,path.stat().st_size

def target_components(hh,scene):
    """Discover actual cache targets, keeping distance/bearing separate from LOS."""
    from shapely.ops import nearest_points
    groups={fid:[] for fid in ['V02','V03','V04']}
    for obj in scene['targets']:
        t=obj['record']['tags'];fid='V02' if t.get('natural')=='peak' else 'V03' if t.get('historic') else 'V04' if t.get('leisure') in ['park','garden'] else None
        if fid and (fid!='V03' or t.get('name')):
            g=transform(scene['project'].transform,obj['geometry']);groups[fid].append((obj['record'],g))
    indexes={fid:STRtree([g for r,g in rr]) for fid,rr in groups.items() if rr}
    rows=[]
    for h in hh:
        p=Point(*scene['project'].transform(h['longitude'],h['latitude']))
        for fid,rr in groups.items():
            if not rr:
                rows.append({'hotel_id':h['hotel_id'],'feature_id':fid,'target':None,'reason':'No eligible target found in cached extent; not proof of absence'});continue
            i=int(indexes[fid].nearest(p));r,g=rr[i];q=nearest_points(p,g)[1];dx,dy=q.x-p.x,q.y-p.y
            rows.append({'hotel_id':h['hotel_id'],'feature_id':fid,'target':{'osm_id':f"{r['osm_type']}/{r['osm_id']}",'name':r['tags'].get('name'),'tags':r['tags'],'source':osm_source(r,scene['path']),'selection':'nearest eligible mapped target to OTA point in cached extent, not global nearest'},'distance_m':p.distance(g),'bearing_deg':math.degrees(math.atan2(dx,dy))%360,'canonical_view_value':None,'scope':'Target discovery and 2D geometry only; not window visibility, 3D volume or angular visible extent.'})
    return rows

def run(city):
    start=time.monotonic();hh=hotels(city);scene=load_scene(city);loaded=time.monotonic();mm,diag=match_hotels(city,hh,scene);matched=time.monotonic();byh=defaultdict(list)
    for m in mm:byh[m['hotel_id']].append(m)
    write_jsonl(OUT/f'{city}_building_matches.jsonl',mm);write_jsonl(OUT/f'{city}_diagnostics.jsonl',diag)
    write_jsonl(OUT/f'{city}_target_components.jsonl',target_components(hh,scene))
    allfacts=[];buildingfacts=[];fan=[];solar_cache={};stats=Counter();geometryrows=[];comparisons=[]
    # Dates include each date requested, treating end as checkout (nights before end).
    dates=[]
    for trip in TRIPS:
        d=date.fromisoformat(trip['start']);end=date.fromisoformat(trip['end'])
        while d<end:dates.append(d.isoformat());d+=timedelta(days=1)
    for h in hh:
        hm=byh[str(h['hotel_id'])];ff,bf=per_hotel_facts(h,hm,scene);allfacts.extend(ff);buildingfacts.extend(bf);comparisons.extend(compare_ota(h,hm))
        for m in hm:
            i=m['scene_index'];g=scene['geoms'][i];t=m['tags'];levels=integer_levels(t);sides=facade_segments(g);hi=height_interval(t)
            min_level=int(positive_number(t.get('building:min_level')) or 0)
            floor_ordinals=list(range(min_level+1,levels+1)) if levels and min_level<levels else [None]
            neighbors=[int(j) for j in scene['tree'].query(g.buffer(1000)) if int(j)!=i]
            neighbor_known=sum(positive_number(scene['records'][j]['tags'].get('height')) is not None for j in neighbors)
            nlevels=sum(integer_levels(scene['records'][j]['tags']) is not None for j in neighbors)
            geometryrows.append({'hotel_id':h['hotel_id'],'building_id':m['osm_id'],'sides':len(sides),'levels':levels,'height_interval':hi,'raw_start_date':t.get('start_date'),'start_date_completion_event_verified':False,'neighbors_1000m':len(neighbors),'neighbors_metric_heights':neighbor_known,'neighbors_levels':nlevels,'neighbor_metric_height_fraction':neighbor_known/len(neighbors) if neighbors else None,'full_scene_complete':False,'unknown_reasons':['existing_window_not_verified','common_ground_datum_unavailable','trees_overhangs_not_complete','neighbor_height_gaps'],'source':m['source']})
            for side in sides:
                wx,wy=side['x']+.05*side['normal'][0],side['y']+.05*side['normal'][1];lon,lat=scene['inverse'].transform(wx,wy)
                # Per-ray footprint distances are useful diagnostics, not V06 horizontal
                # opaque obstruction distances: vegetation/height may change first hit.
                obs=[{'id':f"{scene['records'][j]['osm_type']}/{scene['records'][j]['osm_id']}",'geometry':scene['geoms'][j],'height_min_m':positive_number(scene['records'][j]['tags'].get('height')) or 0,'height_max_m':positive_number(scene['records'][j]['tags'].get('height')) or math.inf} for j in neighbors]
                rayhits=[]; ray_profiles=[]
                for off in [-60,-30,0,30,60]:
                    hits=ray_building_intersections((wx,wy),(side['azimuth_deg']+off)%360,obs,1000)
                    ray_profiles.append((off,hits))
                    rayhits.append({'offset_deg':off,'first_mapped_footprint_distance_m':hits[0]['distance_m'] if hits else None,'first_id':hits[0]['id'] if hits else None,'no_hit_means_clear':False})
                sun=[]
                for day in dates:
                    # Position varies negligibly across one hotel; calculate at actual
                    # midpoint coordinates (cache only the same point/date).
                    key=(round(lat,6),round(lon,6),day)
                    if key not in solar_cache:solar_cache[key]=solar_geometry(lat,lon,day,h['timezone'])
                    times,pos=solar_cache[key];sun.append({'date':day,**orientation_components(side['azimuth_deg'],times,pos)})
                for ordinal in floor_ordinals:
                    # Numeric height interval is a scenario envelope, never physical
                    # measured floor elevations. Unknown count gets one unresolved grid.
                    if ordinal is not None:
                        zi=[2.4*(ordinal-1)+1.2,4.5*(ordinal-1)+1.8]
                    else:zi=[None,None]
                    # Height-conditioned first opaque-footprint interval under an
                    # explicit flat-ground scenario. Unknown heights can block;
                    # lack of a hit is not an infinity observation.
                    conditional=[]
                    for off,hits in ray_profiles:
                        possible=[r for r in hits if zi[0] is not None and r['height_max_m']>=zi[0]]
                        certain=[r for r in hits if zi[1] is not None and r['height_min_m']>zi[1]]
                        conditional.append({'offset_deg':off,'nearest_possible_m':possible[0]['distance_m'] if possible else None,'nearest_certain_opaque_m':certain[0]['distance_m'] if certain else None,'flat_ground_assumption':True,'vegetation_and_canopies_missing':True,'canonical_V06':False})
                    side_line=LineString([side['a'],side['b']])
                    shared=[]
                    for j in neighbors:
                        ng=scene['geoms'][j]
                        if ng.distance(side_line)>.1:continue
                        overlap=ng.boundary.intersection(side_line.buffer(.05)).length
                        if overlap>.8*side['length_m']:
                            shared.append({'osm_id':f"{scene['records'][j]['osm_type']}/{scene['records'][j]['osm_id']}",'neighbor_height':height_interval(scene['records'][j]['tags']),'overlap_length_m':overlap,'exclusion':'only overlapping verified vertical range; no all-floor exclusion'})
                    fan.append({'city_id':city,'hotel_id':h['hotel_id'],'building_id':m['osm_id'],'facade_id':side['side_id'],'physical_floor_ordinal':ordinal,'local_floor_label':None,'floor_basis':'OSM aboveground ordinal, ground=1; no local room-floor mapping' if ordinal else 'unknown_floor_count_no_invented_floors','height_interval_m':zi,'height_interval_basis':'exploratory floor-to-floor envelope 2.4–4.5m + eyeheight1.2–1.8m, not measured' if ordinal else 'no_verified_metric_window_height','facade_azimuth_deg':side['azimuth_deg'],'side_length_m':side['length_m'],'window_scenario_point_lonlat':[lon,lat],'actual_window_confirmed':False,'courtyard_facing':side['courtyard'],'geometry_source':m['source'],'canonical_facts':{fid:{'status':'unknown','value':None,'unknown_reason':'no_verified_existing_window_height_and_complete_scene'} for fid in FANFIDS},'components':{'footprint_rays':rayhits,'conditional_horizontal_obstacle_intervals':conditional,'shared_wall_candidates':shared,'solar_orientation_bounds':sun},'mapping_to_room_category':'unknown','scope':'hypothetical facade grid only; not hotel/room promise'})
                    stats['fan_cells']+=1
    write_jsonl(OUT/f'{city}_facts.jsonl',allfacts);write_jsonl(OUT/f'{city}_building_facts.jsonl',buildingfacts);write_jsonl(OUT/f'{city}_fan.jsonl',fan);write_jsonl(OUT/f'{city}_geometry_quality.jsonl',geometryrows)
    write_jsonl(OUT/f'{city}_ota_comparisons.jsonl',comparisons)
    fan_fact_count,fan_fact_bytes=write_fan_facts(city,fan,hh)
    coverage={fid:dict(Counter(r['status'] for r in allfacts if r['feature_id']==fid)) for fid in FIDS}
    summary={'city_id':city,'hotel_count':len(hh),'matched_hotels':sum(bool(x) for x in byh.values()),'matched_buildings':len(mm),'fan_cells':len(fan),'full_passport_fan_facts':fan_fact_count,'fan_facts_gzip_bytes':fan_fact_bytes,'dates':dates,'scene_source':str(scene['path']),'scene_source_sha256':sha256(scene['path']),'scene_inventory':scene['counts'],'canonical_feature_coverage':coverage,'standalone_building_fact_counts':dict(Counter(r['feature_id']+':'+r['status'] for r in buildingfacts)),'runtime_seconds':{'load':loaded-start,'matching':matched-loaded,'facades_history_solar':time.monotonic()-matched,'total':time.monotonic()-start},'output_bytes':{p.name:p.stat().st_size for p in OUT.glob(f'{city}_*.jsonl')},'software':{'facades_version':FACADES_VERSION,'source_algorithm_sha256':sha256(Path(__file__)),'pvlib':pvlib.__version__,'algorithm':'NREL SPA pvlib.solarposition.spa_python','solar_time_step_minutes':5},'rights':{'OSM':'ODbL1.0 accepted with attribution','GloBFP':'not used: conditional upstream commercial rights','GLO30':'not used as ground DTM; DSM semantic veto','OTA':'cached only no new requests'},'actual_existing_windows_verified':0,'no_network_requests':True,'limitations':['No existing physical windows verified; all V/S canonical facts unknown, geometric bounds are distinct components','No current legal heritage registry join','Construction-completion event not proven by raw start_date','Fan does not map categories to sides/floors; ask hotelier','Building match research quality; all-building census absent; exact address may identify a residential building rather than every hotel unit']}
    dump(OUT/f'{city}_summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--city',required=True,choices=list(CENTERS));a=p.parse_args();run(a.city)
