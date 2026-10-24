"""Shared V9 data protocol. Pure imports: no network, no writes on import."""
from __future__ import annotations
import hashlib,json,functools
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
BASE=Path(__file__).resolve().parents[1]
COHORT=BASE/'cohort/hotels.jsonl'
V4=ROOT/'research_notes/Аудит данных 8 городов'
V5=ROOT/'research_notes/Данные Островка и набор городов/site_data'
CATALOG=ROOT/'research_notes/Каталог характеристик отелей и номеров/feature_catalog.json'
CITY_V4={'spb':'saint_petersburg'}
TZ={'sochi':'Europe/Moscow','moscow':'Europe/Moscow','spb':'Europe/Moscow','kazan':'Europe/Moscow','kaliningrad':'Europe/Kaliningrad','minsk':'Europe/Minsk','istanbul':'Europe/Istanbul','dubai':'Asia/Dubai'}
TRIPS=[{'start':'2026-11-14','end':'2026-11-17','arrival_local':'2026-11-14T02:00:00'},{'start':'2027-07-15','end':'2027-07-18','arrival_local':'2027-07-17T02:00:00'}]

def now(): return datetime.now(timezone.utc).isoformat()
def read_jsonl(path):
    with Path(path).open() as f:
        for line in f:
            if line.strip(): yield json.loads(line)
def write_jsonl(path,rows):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w') as f:
        for row in rows: f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
def hotels(city=None):return [h for h in read_jsonl(COHORT) if city is None or h['city_id']==city]
@functools.lru_cache(maxsize=1)
def catalogue():return {f['id']:f for f in json.loads(CATALOG.read_text())['features']}
def sha256(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def attempts(feature_id, reasons=None, success_recipe=None):
    """Every recipe is explicitly accepted/rejected; valid false is accepted."""
    reasons=reasons or {}
    rr=[];done=False
    for r in catalogue()[feature_id]['recipes']:
        rid=r['recipe_id']
        if done: state='not_attempted_after_valid_result'; reason='A previous recipe produced a valid value, including false.'
        elif rid==success_recipe:state='accepted';reason='Mandatory inputs and scope checked by calculator.';done=True
        else:state='rejected';reason=reasons.get(rid,reasons.get('default','Mandatory source fields or object-level coverage not available; source-scoped components retained separately.'))
        rr.append({'recipe_id':rid,'data_kind':r['data_kind'],'state':state,'reason':reason})
    return rr

def fact(hotel,feature_id,*,value=None,status='unknown',unit=None,params=None,unknown_reason=None,
         sources=None,recipe_id=None,confidence=None,subject_level='hotel',subject_id=None,
         scope_note=None,recipe_attempts=None,observed_at=None,valid_to=None,depends_on=None,**extra):
    if status not in {'observed','computed','estimate','ostrovok_content','unknown','not_applicable'}:raise ValueError(status)
    if status=='unknown' and not unknown_reason:raise ValueError('unknown_reason required')
    if status in {'unknown','not_applicable'} and value is not None:raise ValueError('unknown and not_applicable require null value')
    params=params or {};subject_id=subject_id or f"hotel:{hotel['hotel_id']}"
    identity=json.dumps([hotel['city_id'],hotel['hotel_id'],feature_id,subject_level,subject_id,params],sort_keys=True,ensure_ascii=False)
    out={'fact_id':hashlib.sha256(identity.encode()).hexdigest()[:24],'hotel_id':str(hotel['hotel_id']),
         'city_id':hotel['city_id'],'subject_level':subject_level,'subject_id':subject_id,'feature_id':feature_id,
         'params':params,'value':value,'unit':unit if unit is not None else catalogue()[feature_id].get('unit'),'status':status,'unknown_reason':unknown_reason,
         'sources':sources or [],'recipe_id':recipe_id,'algorithm_version':'v9.1','confidence':confidence or {'level':'unknown' if status=='unknown' else 'low' if status=='estimate' else 'medium','reason':unknown_reason or scope_note or 'Source-scoped deterministic result; original source timestamps and unverified dependencies are retained in the passport.'},
         'observed_at':observed_at,'calculated_at':now(),'valid_to':valid_to,'scope_note':scope_note,
         'recipe_attempts':recipe_attempts or attempts(feature_id,reasons={'default':unknown_reason} if unknown_reason else None,success_recipe=recipe_id),
         'depends_on':depends_on or []}
    out.update(extra);return out
