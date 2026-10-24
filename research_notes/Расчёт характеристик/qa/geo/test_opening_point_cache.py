#!/usr/bin/env python3
"""Exact original point-state regression across all city countries/timezones.

Only the original optional next_change call is intercepted for this comparison,
so the known unbounded diagnostic cannot stall this test. Native point-state
parsing/evaluation, holiday logic and exceptions remain unchanged.
"""
import json,sys
from pathlib import Path
BASE=Path(__file__).resolve().parents[2];sys.path.insert(0,str(BASE/'scripts'))
import geo,opening_hours
from geo_opening_point_cache import evaluate,_state
NATIVE=opening_hours.OpeningHours
class NativePointWithoutOptionalChange:
    def __init__(self,*a,**kw):self.obj=NATIVE(*a,**kw)
    def is_open(self,*a,**kw):return self.obj.is_open(*a,**kw)
    def is_closed(self,*a,**kw):return self.obj.is_closed(*a,**kw)
    def next_change(self,*a,**kw):return None

def main():
    expressions=['24/7','Mo-Su 09:00-22:00','Fr 20:00-04:00','Mo-Su 22:00-05:00','Mo-Su 08:00-12:00,14:00-22:00','24/7; PH off','Mo-Su 08:00-23:00; SH off','notvalid(']
    rows=[]
    tagcases=[({'opening_hours':expr},False) for expr in expressions]+[({'opening_hours':'24/7'},True),({'opening_hours':'24/7','opening_hours:kitchen':'Mo-Su 09:00-21:00'},True),({'opening_hours:kitchen':'24/7','kitchen:opening_hours':'Mo-Su 09:00-21:00'},True),({},False)]
    for city in geo.CENTERS:
      for date in ['2026-11-14T02:00:00','2027-07-17T02:00:00']:
        for tags,kitchen in tagcases:
          try:
            opening_hours.OpeningHours=NativePointWithoutOptionalChange
            a=geo.opening_state({'tags':tags},date,city,kitchen)
          finally:opening_hours.OpeningHours=NATIVE
          b=evaluate({'tags':tags},date,city,kitchen,timezones=geo.TZ,countries=geo.COUNTRY)
          equal=all(a.get(k)==b.get(k) for k in ['state','expression','evaluated_local'])
          rows.append({'city_id':city,'timezone':geo.TZ[city],'country':geo.COUNTRY[city],'date':date,'tags':tags,'kitchen':kitchen,'old':a,'new':b,'point_state_expression_instant_exact':equal})
    # Same expression/date in another kitchen context must still have a distinct key.
    _state.cache_clear();evaluate({'tags':{'opening_hours':'24/7'}},'2026-11-14T02:00:00','sochi',False,timezones=geo.TZ,countries=geo.COUNTRY);evaluate({'tags':{'opening_hours:kitchen':'24/7'}},'2026-11-14T02:00:00','sochi',True,timezones=geo.TZ,countries=geo.COUNTRY);info=_state.cache_info();kitchen_key_separate=info.misses==2
    result={'cases':len(rows),'all_point_states_expressions_instants_exact':all(r['point_state_expression_instant_exact'] for r in rows),'kitchen_and_selected_tag_key_separate':kitchen_key_separate,'cache_key':['expression','date','city','timezone','country','kitchen','selected_tag_key'],'original_comparison_scope':'Original accepted opening_state with native is_open/is_closed/parsing unchanged; only optional next_change returns None to avoid known diagnostic stall.','rows':rows};result['passed']=result['all_point_states_expressions_instants_exact'] and kitchen_key_separate
    (BASE/'qa/geo/opening_point_cache_equivalence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print({k:v for k,v in result.items() if k!='rows'});assert result['passed']
if __name__=='__main__':main()
