"""Meaningful synthetic LOS/facade and independent solar implementation checks."""
import json,sys,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import facades as f
from shapely.geometry import box,Point
from astral.sun import azimuth,elevation

checks=[]
def check(name,ok,**details):
    checks.append({'name':name,'passed':bool(ok),**details})
    assert ok,(name,details)

ss=f.facade_segments(box(0,0,10,10))
check('four_metric_facades',len(ss)==4)
check('outward_normals',all(not box(0,0,10,10).covers(Point(s['x']+.1*s['normal'][0],s['y']+.1*s['normal'][1])) for s in ss))
check('cardinal_azimuths',sorted(round(s['azimuth_deg']) for s in ss)==[0,90,180,270])
wall={'id':'wall','geometry':box(4,-1,6,1),'height_min_m':9,'height_max_m':9}
r=f.los_interval((0,0),(2,2),(10,0),(2,2),[wall]);check('robust_wall_false',r['visible'] is False,result=r)
wall['height_min_m']=0;wall['height_max_m']=1
r=f.los_interval((0,0),(2,2),(10,0),(2,2),[wall]);check('low_wall_true_in_complete_scene',r['visible'] is True,result=r)
wall['height_max_m']=math.inf
r=f.los_interval((0,0),(2,2),(10,0),(2,2),[wall]);check('unknown_wall_not_transparent',r['visible'] is None,result=r)
wall['height_min_m']=1;wall['height_max_m']=9
r=f.los_interval((0,0),(2,2),(10,0),(2,2),[wall]);check('height_interval_unknown',r['visible'] is None,result=r)
wall['height_min_m']=9;wall['height_max_m']=9
r=f.los_interval((0,0),(20,20),(10,0),(20,20),[wall]);check('higher_floor_clear',r['visible'] is True,result=r)
r=f.los_interval((5,0),(2,2),(10,0),(2,2),[wall]);check('origin_inside_other_obstacle_not_transparent',r['visible'] is False,result=r)
check('invalid_fractional_levels_not_floors',f.integer_levels({'building:levels':'1.5'}) is None)
check('no_height_no_fabricated_count',f.height_interval({})['max_m'] is None)
for day in ['2026-11-14','2027-07-15']:
    times,pos=f.solar_geometry(43.394641876,39.976051331,day,'Europe/Moscow')
    for azi in [0,90,180,270]:
        c=f.orientation_components(azi,times,pos)
        check(f'orientation_bounds_nonnegative_{day}_{azi}',0<=c['unobstructed_14_18_hours_upper']<=4 and 0<=c['unobstructed_orientation_sun_hours_upper']<=24)
    # Astral and SPA are independent code paths, same astronomical parameters.
    from astral import Observer
    observer=Observer(43.394641876,39.976051331,0)
    for i in [96,144,192]:
        dt=times[i].to_pydatetime();a=azimuth(observer,dt);e=elevation(observer,dt,with_refraction=False)
        da=abs((a-float(pos.iloc[i]['azimuth'])+180)%360-180);de=abs(e-float(pos.iloc[i]['elevation']))
        check(f'pvlib_astral_agreement_{day}_{i}',da<.1 and de<.1,azimuth_difference_deg=da,elevation_difference_deg=de,scope='astronomy implementation agreement; not ground-truth window sunshine')
    north=f.orientation_components(0,times,pos)
    if day=='2026-11-14':check('winter_north_plane_orientation_zero',north['unobstructed_orientation_sun_hours_upper']==0)

f.dump(f.BASE/'qa/facades/synthetic_scene_checks.json',{'checks':checks,'passed':all(x['passed'] for x in checks),'n':len(checks),'scope':'Synthetic algorithm checks and independent astronomical implementation agreement, not real-world hotel accuracy.'})
print(len(checks),'checks passed')
