#!/usr/bin/env python3
"""Build a small auditable Codex annotation panel, explicitly separating components/gaps.

No synthetic control or self-OSM trace is called independent ground truth.
"""
import json,csv,hashlib,sys
from pathlib import Path
BASE=Path(__file__).resolve().parents[2];sys.path.insert(0,str(BASE/'scripts'))
from common import read_jsonl,write_jsonl,now,hotels

def main():
    q=BASE/'qa/geo';facts=list(read_jsonl(BASE/'results/geo/sochi_facts.jsonl'));hs={h['hotel_id']:h for h in hotels('sochi')};rows=[]
    def pick(mid,fid):return next(r for r in facts if r['hotel_id']==mid and r['feature_id']==fid)
    def add(n,mid,fid,r,kind,refurl,reference_label,note,independent=False,gold=False,component=False):
        rows.append({'case_id':'geo-sochi-'+str(n).zfill(2),'annotation':'Разметка Codex','human_verified':False,'hotel_id':mid,'city_id':'sochi','hotel_name':hs[mid]['name'],'feature_id':fid,'prediction_fact_id':r['fact_id'],'prediction_status':r['status'],'prediction':r['value'],'params':r['params'],'evaluation_kind':kind,'reference_source_url':refurl,'reference_label':reference_label,'independent_reference':independent,'canonical_full_feature_gold':gold,'component_only':component,'error_m':None,'scope_and_limitations':note,'annotated_at':now()})
    pre=list(read_jsonl(q/'predictions_pre_source_audit.jsonl'))[0]
    add(1,'10012084','L01',pre,'independent_current_schedule_component',json.loads((q/'stoletov_konstitutsii24a_source.json').read_text())['url'],False,'Pre-audit OSM24/7 at node5736969027 versus current operator08:00–21:00. Identity verified by node inside address24A building51167675. This falsifies current recurring-open02 component, not a gold full route or an observation of future14Nov2026/17Jul2027. Candidate blocked; rerouting is a repair on same case, not held-out accuracy.',True,False,True)
    r=pick('10012084','P04')
    add(2,'10012084','P04',r,'independent_current_schedule_component','https://www.centervet-sochi.com/',True,'Primary operator lists Teplichnaya18A around the clock. OSMnode11016638319 matches website/email/phone and lies insideTepliсhnaya18Aс1 building731859545. Supports current declared24h component only; 6.8km network route has no independent length gold and future exceptions unmeasured.',True,False,True)
    add(3,'10012084','N04',pick('10012084','N04'),'insufficient_independent_inventory','https://bandidobar.com/',None,'Count is mapped-source total, not an exhaustive real-world count. Primary brand lists three branch addresses, but the historical OSMBandido point lacks full address; no branch identity resolution. PrimaryHTTP returned402; search-indexextract is a lead, not accepted count gold.',True)
    add(4,'13736470','W01',pick('13736470','W01'),'insufficient_independent_route_reference','https://www.openstreetmap.org/way/240616876',None,'Saved shortest mapped route ends at a graph contact with beach polygon; public entrance and actual hotel entrance unverified. OSMgeometry trace is the same source as prediction and cannot supply independent distance error.',False)
    add(5,'10012084','W05',pick('10012084','W05'),'same_source_trace_only','https://www.openstreetmap.org/',None,'Known merged steps sections on exact saved route; team can inspect edge_way_ids in sochi_routes.jsonl. No independent dated field/photo route survey, so no precision/recall or physical step-free claim.',False)
    add(6,'13736470','W05',pick('13736470','W05'),'same_source_trace_only','https://www.openstreetmap.org/way/240616876',None,'Zero known mapped steps does not mean no physical stairs or kerbs. All remaining route length is uninspected for missing obstacles; independent reference is missing.',False)
    add(7,'13736470','N02',pick('13736470','N02'),'independent_algorithm_agreement_pending','https://www.openstreetmap.org/way/257675020',None,'Projected nearest footprint-axis distance is reproducible; a geodesic algorithm can check numerical agreement using same source geometry. Such agreement is not independent ground truth of footprint/property identity or current road placement.',False)
    add(8,'13736470','Q03',pick('13736470','Q03'),'insufficient_lighting_reference','https://www.openstreetmap.org/way/240616876',None,'Known-lit0%, unknown100% on saved route means no explicit light declaration in graph, not dark or unsafe. No independent working-light inventory or dated night survey.',False)
    add(9,'8740393','F01',pick('8740393','F01'),'insufficient_same_scope_reference','https://akvaloo.ru/',None,'Hotel-owned family facilities on official property site do not independently validate the selected public offsite playground entrance/access/network distance. Preserve scope difference.',True)
    add(10,'8740393','T03',pick('8740393','T03'),'insufficient_future_schedule_reference','https://www.sochi-tk.ru/routes.html',None,'Current official route/operator list supports provider discovery, but not complete active calendars, night itinerary and transfers for14Nov2026/17Jul2027. Missing accepted feed does not establish false.',True)
    write_jsonl(q/'reference_cases.jsonl',rows)
    with (q/'reference_cases.csv').open('w') as f:
        keys=list(rows[0]);w=csv.DictWriter(f,fieldnames=keys);w.writeheader()
        for r in rows:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in r.items()})
    (q/'reference_case_metrics.json').write_text(json.dumps({'annotation':'Разметка Codex','cases':len(rows),'full_feature_independent_gold':0,'current_declared_hours_component_cases':2,'current_hours_component_TP':1,'current_hours_component_FP':1,'component_positive_precision':0.5,'component_positive_precision_scope':'Two deliberately selected different-provider current-schedule cases, not representative fullfeature accuracy; future dates/route distances not gold.','component_recall':None,'component_recall_reason':'No complete negative/positive panel; recall unmeasured.','full_feature_distance_error_m':None,'repair_cases_are_not_heldout':True,'remaining_cases_are_gaps_or_recipe_agreement_only':8},ensure_ascii=False,indent=2)+'\n')
    print('Wrote10cases;2current-hourscomponents,0independentfullfeaturegold.')
if __name__=='__main__':main()
