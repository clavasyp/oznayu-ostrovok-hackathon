#!/usr/bin/env python3
"""Run the accepted immutable geo.py with a disclosed missing-node input supplement.

This wrapper adds pure barrier/access nodes from the same original PBF, leaving
the accepted calculator file unchanged. Sochi execution is deliberately forbidden.
"""
import argparse,json,sys,hashlib,time,gc
from pathlib import Path
from common import BASE,V4,CITY_V4,read_jsonl,write_jsonl,sha256,now
import geo
from geo_memory_containers import compact_record,graph_class
from geo_opening_point_cache import evaluate as cached_opening_state
EXPECTED='e575c51e28cbc3905eadb4e4d628005b9fad99031301a741d0477c80e73473ad'
def main():
    p=argparse.ArgumentParser();p.add_argument('--city',required=True,choices=['moscow','spb','kazan','kaliningrad','minsk','istanbul','dubai']);city=p.parse_args().city;start=time.perf_counter()
    path=Path(geo.__file__);actual=sha256(path)
    if actual!=EXPECTED:raise RuntimeError('Accepted geo.py SHA changed: '+actual)
    supplement=BASE/'cohort'/f'osm_{city}_pure_restriction_nodes.jsonl';mp=supplement.with_suffix('.manifest.json')
    if not supplement.exists() or not mp.exists():raise RuntimeError('Missing completed node supplement')
    manifest=json.loads(mp.read_text());original=geo.read_records
    joinfile=BASE/'results/facades'/f'{city}_building_matches.jsonl'
    if not joinfile.exists():raise RuntimeError('Final facade building matches must exist before geo calculation')
    with Path(manifest['source_file']).open('rb') as f:manifest['source_pbf_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    manifest['source_pbf_sha256_verification']='Streamed actual cached PBF before this city run; no network.'
    mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    ck=CITY_V4.get(city,city);original_manifest=json.loads((V4/'manifests'/f'osm_{ck}_complete.json').read_text())
    original_file=V4/'raw'/f'osm_{ck}.jsonl';closed_file=V4/'raw'/f'osm_{ck}_closed_roads.jsonl'
    inputs=[{'snapshot_file':str(original_file),'sha256':original_manifest['sha256']}]
    if closed_file.exists():inputs.append({'snapshot_file':str(closed_file),'sha256':sha256(closed_file)})
    inputs.append({'snapshot_file':str(supplement),'sha256':manifest['sha256']})
    composite={**original_manifest,'sha256':hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest(),'sha256_kind':'composite SHA256 of ordered input path+SHA manifests, not file hash','original_main_sha256':original_manifest['sha256'],'input_files':inputs,'node_supplement_manifest':manifest}
    def records(c):
        for r,m in original(c):yield compact_record(r),composite if c==city else m
        if c==city:
            for r in read_jsonl(supplement):yield compact_record(r),composite
    geo.read_records=records
    # Graph dictionaries and arrays form no cycles. Refcounting still frees them;
    # suppress repeated generation-2 scans of millions of acyclic objects while
    # a city is computed. A one-city process exits immediately afterwards.
    gc.disable()
    geo.Graph=graph_class(geo.Graph)
    geo.opening_state=lambda poi,date,city,kitchen=False:cached_opening_state(poi,date,city,kitchen,timezones=geo.TZ,countries=geo.COUNTRY)
    selected_routes={};original_route=geo.Graph.route
    def captured_route(self,hotel,target,kind):
        r=original_route(self,hotel,target,kind)
        if r.get('ok'):
            # Reuse exact returned path, not a second route or an inferred polyline.
            selected_routes[r['route_id']]={'type':'Feature','geometry':{'type':'LineString','coordinates':r['coordinates']} if len(r['coordinates'])>1 else {'type':'Point','coordinates':r['coordinates'][0]},'properties':{'city_id':city,'hotel_id':hotel['hotel_id'],'route_id':r['route_id'],'kind':kind,'target_id':r['target_id'],'target_name':r.get('target_name'),'graph_length_m':r['graph_length_m'],'length_estimate_m':r['length_estimate_m'],'hotel_snap_m':r['hotel_snap_m'],'target_snap_m':r['target_snap_m'],'node_ids':r['node_ids'],'edge_way_ids':[e['sid'] for e in r['edges']],'recipe_id':(kind+'-R2') if kind in {'L01','L02','L03','F02','P04','R04'} else 'F01-R2' if kind=='playground' else 'W01-R2' if kind=='beach' else 'W02-R2' if kind=='stop' else 'W03-R2' if kind=='center' else None,'source':{'provider':'OpenStreetMap','dataset_version':composite.get('source_date'),'composite_dataset_sha256':composite['sha256'],'input_files':inputs,'license':'ODbL1.0','license_url':'https://www.openstreetmap.org/copyright'},'route_status':'Research estimate; hotel and target connectors remain unverified','capture_method_version':'V9-exact-returned-route-capture-1'}}
        return r
    geo.Graph.route=captured_route
    result=geo.run(city);result['accepted_calculator_sha256']=actual;result['runner_sha256']=sha256(__file__);result['cyclic_gc_during_graph_compute']='disabled; reference counting active; single-city process exit frees resources';result['memory_representation']={'version':'V9-representation-only-1','source_file':str(BASE/'scripts/geo_memory_containers.py'),'sha256':sha256(BASE/'scripts/geo_memory_containers.py'),'exact_equivalence_test':str(BASE/'qa/geo/memory_container_equivalence.json'),'note':'Same accepted Graph.__init__, math.dist and routing methods; compact source node triples/shared edge metadata/interned text; no coordinate or source-selection changes.'};result['extra_node_input']=manifest;result['extra_node_input_reason']='Original V4 feature keys omit pure barrier/access/foot/locked nodes; recover known restrictions before route computation; no hypothetical access assumed.'
    saved=BASE/'results/geo'/f'{city}_all_selected_routes.jsonl';write_jsonl(saved,selected_routes.values());result['all_selected_routes']={'file':str(saved),'sha256':sha256(saved),'routes':len(selected_routes),'capture_method_version':'V9-exact-returned-route-capture-1'};result['result_bytes']=sum(p.stat().st_size for p in (BASE/'results/geo').glob(city+'_*jsonl'))
    result['hours_evaluation_extension']={'version':'V9-local-instant-hours-cache-1','sha256':sha256(BASE/'scripts/geo_opening_point_cache.py'),'scope':'Same requested-instant is_open/is_closed, country calendar and timezone. Optional unbounded next_change search omitted with explicit null/reason; no feature-state inference changed.'}
    factsfile=BASE/'results/geo'/f'{city}_facts.jsonl';facts=list(read_jsonl(factsfile))
    for row in facts:
        if isinstance(row.get('value'),dict) and row['value'].get('route_id'):
            row['depends_on']=[*row.get('depends_on',[]),{'kind':'effective_input_manifest','file':str(BASE/'results/geo'/f'{city}_run.json'),'sha256':composite['sha256'],'sha256_kind':composite['sha256_kind'],'node_supplement_sha256':manifest['sha256']}]
    write_jsonl(factsfile,facts);result['result_bytes']=sum(p.stat().st_size for p in (BASE/'results/geo').glob(city+'_*jsonl'));result['pipeline_seconds_including_capture_and_auxiliary_serialization']=time.perf_counter()-start;result['node_input_prepare_seconds']=manifest['seconds']
    path=BASE/'results/geo'/f'{city}_run.json';path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    if sha256(geo.__file__)!=actual:raise RuntimeError('Accepted calculator changed while city was computing')
    (BASE/'qa/geo'/f'{city}_calculation_inputs.json').write_text(json.dumps({'city_id':city,'recorded_at':now(),'accepted_geo_sha256':actual,'runner_sha256':sha256(__file__),'supplement':manifest,'building_matches_sha256':sha256(joinfile)},ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()
