#!/usr/bin/env python3
"""Meaningful graph/access/time/unknown contract regressions, not accuracy labels."""
import json,sys
from pathlib import Path
BASE=Path(__file__).resolve().parents[2];sys.path.insert(0,str(BASE/'scripts'))
import geo
from common import attempts,fact,hotels
checks={}
checks['private_access_excluded']=not geo.allowed({'highway':'service','access':'private'})
checks['explicit_public_foot_override']=geo.allowed({'highway':'service','access':'private','foot':'yes'})
checks['motorway_excluded']=not geo.allowed({'highway':'motorway'})
checks['cycleway_without_foot_excluded']=not geo.allowed({'highway':'cycleway'})
checks['conditional_access_not_silently_accepted']=not geo.allowed({'highway':'footway','access:conditional':'yes @ (Mo-Fr)'})
es=[{'tags':{'highway':h},'length_m':10} for h in ['path','steps','steps','path','steps']]
checks['adjacent_steps_merged']=geo.steps_metrics(es)['known_step_sections']==2
li=geo.lighting_metrics([{'tags':{'lit':'yes'},'length_m':30},{'tags':{'lit':'no'},'length_m':20},{'tags':{},'length_m':50}])
checks['lighting_unknown_preserved']=li['known_lit_percent']==30 and li['unknown_percent']==50
checks['open_at_local_02']=geo.opening_state({'tags':{'opening_hours':'24/7'}},'2026-11-14T02:00:00','sochi')['state']=='open'
checks['closed_at_local_02']=geo.opening_state({'tags':{'opening_hours':'Mo-Su 09:00-22:00'}},'2026-11-14T02:00:00','sochi')['state']=='closed'
checks['kitchen_hours_not_bar_hours']=geo.opening_state({'tags':{'opening_hours':'24/7'}},'2026-11-14T02:00:00','sochi',True)['state']=='unknown'
h=hotels('sochi')[0];f=fact(h,'N04',value=False,status='computed',recipe_id='N04-R1')
checks['valid_false_stops_chain']=f['recipe_attempts'][1]['state']=='not_attempted_after_valid_result'
try:fact(h,'N04',status='unknown');checks['unknown_requires_reason']=False
except ValueError:checks['unknown_requires_reason']=True
assert all(checks.values()),checks
(BASE/'qa/geo/synthetic_tests.json').write_text(json.dumps({'run_at':geo.now(),'test_type':'Synthetic regression controls, not empirical accuracy','checks':checks,'passed':True},indent=2)+'\n')
print(json.dumps(checks,indent=2))
