#!/usr/bin/env python3
"""Exact fixture equivalence for the memory representation, not empirical accuracy."""
import sys,copy,json
from pathlib import Path
import numpy as np
from shapely.geometry import LineString,Point
BASE=Path(__file__).resolve().parents[2];sys.path.insert(0,str(BASE/'scripts'))
import geo
from geo_memory_containers import PackedNodes,compact_record,graph_class

def row(typ,oid,geom,tags,nodes=None):return {'osm_type':typ,'osm_id':oid,'geometry_wkb':geom.wkb_hex,'tags':tags,'version':1,'timestamp':'2026-10-22T00:00:00Z','nodes':nodes}
nds=[[101,39.72,43.58],[102,39.721,43.58],[103,39.722,43.58],[104,39.723,43.58]]
records=[row('way',1,LineString([(n[1],n[2]) for n in nds]),{'highway':'residential','lit':'yes'},nds),row('way',2,LineString([(39.721,43.58),(39.721,43.581)]),{'highway':'steps'},[[102,39.721,43.58],[105,39.721,43.581]]),row('node',99,Point(39.723,43.58),{'amenity':'pharmacy','opening_hours':'24/7'}),row('way',3,LineString([(n[1],n[2]) for n in nds]),{'highway':'residential','lit':'no','name':'equal_length_later_way_must_not_replace_first'},nds),row('way',201,LineString([(39.721,43.579),(39.721,43.581)]),{'highway':'secondary','foot':'no'},[[201,39.721,43.579],[102,39.721,43.58],[202,39.721,43.581]]),row('node',102,Point(39.721,43.58),{'highway':'crossing','crossing':'traffic_signals'})]
manifest={'bbox':[39.15,43.35,40.5,44.05],'source_date':'2026-10-22T00:00:00Z','sha256':'synthetic-composite','input_files':[{'snapshot_file':'fixture-main','sha256':'fixture-main-sha'},{'snapshot_file':'fixture-node-supplement','sha256':'fixture-extra-sha'}]}
original_reader=geo.read_records
geo.read_records=lambda city:((copy.deepcopy(r),manifest) for r in records)
a=geo.Graph('sochi')
geo.read_records=lambda city:((compact_record(copy.deepcopy(r)),manifest) for r in records)
b=graph_class(geo.Graph)('sochi')
h={'hotel_id':'fixture','longitude':39.72,'latitude':43.58};target={'sid':'fixture:target','point':Point(*a.tr.transform(39.723,43.58)),'tags':{}}
ra=a.route(h,target,'fixture');rb=b.route(h,target,'fixture');checks={
 'node_ids_exact':a.nids==b.nids,
 'coordinate_arrays_bitwise_equal':np.array_equal(a.xy,b.xy),
 'sparse_graph_exact':(a.A!=b.A).nnz==0,
 'edge_keys_equal':set(a.edges)==set(b.edges),
 'edge_distances_bitwise_equal':all(a.edges[k]['length_m']==b.edges[k]['length_m'] for k in a.edges),
 'edge_source_ids_equal':all(a.edges[k]['sid']==b.edges[k]['sid'] for k in a.edges),
 'route_id_exact':ra['route_id']==rb['route_id'],
 'route_distance_bitwise_equal':ra['length_estimate_m']==rb['length_estimate_m'],
 'route_nodes_and_geometry_exact':ra['node_ids']==rb['node_ids'] and ra['coordinates']==rb['coordinates'],
 'source_metadata_intact':geo.osm_source(a.pois[0]['record'])==geo.osm_source(b.pois[0]['record']),
 'packed_nodes_roundtrip_exact':list(PackedNodes(nds))==list(map(tuple,nds)),
 'node_ids_remain_python_int':all(type(n) is int for n in b.nids+rb['node_ids']),
 'csr_tie_breaking_order_exact':np.array_equal(a.A.indices,b.A.indices) and np.array_equal(a.A.indptr,b.A.indptr),
 'equal_length_edge_first_source_preserved':all(b.edges[k]['sid']=='way/1' for k in b.edges if b.edges[k]['tags'].get('highway')=='residential'),
 'composite_manifest_exact':a.manifest==b.manifest==manifest,
 'steps_metrics_exact':geo.steps_metrics(ra['edges'])==geo.steps_metrics(rb['edges']),
 'crossing_metrics_exact':geo.crossing_metrics(ra,a)==geo.crossing_metrics(rb,b),
 'lighting_metrics_exact':geo.lighting_metrics(ra['edges'])==geo.lighting_metrics(rb['edges']),
}
def payload(graph,route):
    stair_target={'sid':'fixture:stairs','point':Point(*graph.tr.transform(39.721,43.581)),'tags':{}};stair=graph.route(h,stair_target,'fixture_stairs')
    return {'node_ids':graph.nids,'xy':graph.xy.tolist(),'csr_data':graph.A.data.tolist(),'csr_indices':graph.A.indices.tolist(),'csr_indptr':graph.A.indptr.tolist(),'edge_source_ids':{f'{u}-{v}':r['sid'] for (u,v),r in graph.edges.items()},'route':{k:route[k] for k in ['route_id','node_ids','coordinates','graph_length_m','length_estimate_m']},'steps':geo.steps_metrics(stair['edges']),'lighting':geo.lighting_metrics(stair['edges']),'crossings':geo.crossing_metrics(route,graph),'poi_source':geo.osm_source(graph.pois[0]['record'],graph.manifest),'manifest':graph.manifest}
original_payload=payload(a,ra);compact_payload=payload(b,rb);checks['saved_graph_and_outputs_byte_exact']=json.dumps(original_payload,sort_keys=True)==json.dumps(compact_payload,sort_keys=True)
(BASE/'qa/geo/fixture_original_graph.json').write_text(json.dumps(original_payload,sort_keys=True,indent=2)+'\n');(BASE/'qa/geo/fixture_compact_graph.json').write_text(json.dumps(compact_payload,sort_keys=True,indent=2)+'\n')
assert all(checks.values()),checks
(BASE/'qa/geo/memory_container_equivalence.json').write_text(json.dumps({'kind':'Representation-only exact fixture regression; no algorithm changes; not empirical accuracy','checks':checks,'passed':True},indent=2)+'\n');print(json.dumps(checks,indent=2));geo.read_records=original_reader
