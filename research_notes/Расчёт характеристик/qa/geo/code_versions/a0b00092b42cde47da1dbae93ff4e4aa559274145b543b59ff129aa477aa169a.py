"""Exact requested-instant hours evaluation, with bounded optional work.

The characteristic requires a state at the requested local instant. It does not
require finding a future change through an unbounded holiday/calendar iterator.
Keep that diagnostic null and cache identical expressions/date/city results.
"""
import re,functools
from datetime import datetime
from zoneinfo import ZoneInfo
import opening_hours

@functools.lru_cache(maxsize=32768)
def _state(expression,date,city,timezone,country,kitchen,selected_tag_key):
    try:
        dt=datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(timezone))
        o=opening_hours.OpeningHours(expression,country=country,timezone=ZoneInfo(timezone))
        state='open' if o.is_open(dt) else 'closed' if o.is_closed(dt) else 'unknown'
        return {'state':state,'expression':expression,'evaluated_local':dt.isoformat(),'reason':'OSM declared recurring schedule; future exceptions/physical operation require refresh','has_public_holiday_expression':bool(re.search(r'\b(PH|SH)\b',expression)),'next_change':None,'next_change_reason':'Optional future-change search not required for the feature and omitted; requested-instant state was evaluated exactly.','evaluation_method_version':'V9-local-instant-hours-cache-1'}
    except Exception as e:return {'state':'unknown','expression':expression,'reason':'parser_or_country_holiday_failure:'+str(e)[:160]}

def evaluate(poi,date,city,kitchen=False,*,timezones,countries):
    t=poi['tags'];keys=['opening_hours:kitchen','kitchen:opening_hours','food_service_hours'] if kitchen else ['opening_hours'];selected=next((k for k in keys if t.get(k)),None);expression=t[selected] if selected else None
    if not expression:return {'state':'unknown','reason':'no_kitchen_service_hours' if kitchen else 'no_opening_hours','expression':None}
    # Return a new dict so callers cannot mutate a cache entry shared across hotels.
    return dict(_state(expression,date,city,timezones[city],countries[city],bool(kitchen),selected))
