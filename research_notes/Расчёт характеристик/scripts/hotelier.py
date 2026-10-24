"""Generate 26 catalogue questions and source-scoped OTA prefill, offline only.

Input: frozen V9 cohort, canonical feature catalogue, frozen V7 claim ledger.
Output: hotelier/questions_26.csv and hotelier/prefill.jsonl, including typed
cached V5 room fields and already filtered V7 claims. Claims remain
claims: no fabricated answer, category guarantee, simulation or completed fact.
"""
from pathlib import Path
import csv
import json
import gzip,hashlib
from collections import defaultdict
from common import BASE, ROOT, catalogue, hotels, write_jsonl, sha256

FIELDS = {
    'S04': 'room_type_id,variant_ids,hvac_type,individual_control,operating_season,restrictions',
    'S05': 'room_type_id,variant_ids,window_ids,opaque_coverage,edge_gaps,evidence_date',
    'N05': 'room_ids,LAeq_dBA,interval,measurement_protocol,windows_state,hvac_state,instrument',
    'N06': 'room_type_id,room_ids,shared_wall_with_lift,assignment_guarantee',
    'T04': 'parking_owner,reservation,vehicle_dimensions,fee,currency,charging_basis,access_hours',
    'T05': 'charger_id,connector,power_kW,working_at,external_access,reservation,fee',
    'L04': 'arrival_local,front_desk_hours,self_checkin,prior_arrangement,contact_deadline,fee',
    'L05': 'service_date,breakfast_start,breakfast_box,order_deadline,fee',
    'F03': 'child_age,weight_limit,cot_dimensions,fee,currency,reservation_guarantee',
    'F04': 'room_type_pair,room_ids,internal_connecting_door,guaranteed_pair,booking_method',
    'F05': 'pool_id,minimum_age,depth_m,hours,season,supervision_rules',
    'P01': 'species,weight_kg,number,room_type_ids,restricted_areas,prior_approval',
    'P02': 'species,weight_kg,number,dates,fee_amount,currency,fee_basis,deposit,refund_rules',
    'A02': 'entrance_id,clear_width_mm,threshold_mm,route_to_lobby,measurement_date',
    'A03': 'lift_id,door_width_mm,cabin_width_mm,cabin_depth_mm,served_physical_floors',
    'A04': 'room_type_id,variant_ids,shower_threshold_mm,measurement_date',
    'A05': 'room_type_id,variant_ids,door_clear_widths_mm,bathroom_width_mm,measurement_date',
    'R01': 'room_ids,download_Mbps,upload_Mbps,time_samples,protocol,measurement_date',
    'R02': 'room_ids,latency_ms,jitter_ms,loss_percent,endpoint,protocol,time_samples',
    'R03': 'room_type_id,variant_ids,desk_clear_width_mm,desk_depth_mm,desk_height_mm,socket_distance_mm',
    'M01': 'room_type_id,variant_ids,bed_configuration,bed_dimensions_mm,split_possible,assignment_guarantee',
    'M02': 'room_type_id,variant_ids,private_balcony,private_terrace,area_m2,safety,assignment_guarantee',
    'M03': 'room_type_id,variant_ids,private_kitchenette,appliances,cooking_allowed,fee',
    'M04': 'room_type_id,variant_ids,in_room_kettle,on_request,fee,guarantee',
    'M05': 'room_type_id,variant_ids,safe_inner_width_mm,safe_depth_mm,safe_height_mm,access,fee',
    'M06': 'booking_id,dates,occupancy,mandatory_fee_items,currency,fee_basis,deposit_items,refund_rules',
}

def main():
    cat=catalogue(); selected={k:v for k,v in cat.items() if v['recipes'][0]['data_kind']=='В'}
    assert len(selected)==26 and set(FIELDS)==set(selected)
    cohort={str(h['hotel_id']):h for h in hotels()}; claims=defaultdict(list)
    source=ROOT/'research_notes/Обогащение данных об отелях/experiment/claims_all_frozen.jsonl'
    for line in source.open():
        c=json.loads(line); key=(str(c['hotel_masterId']),c['feature_id'])
        if key[0] in cohort and key[1] in selected and c.get('effective_atom_eligible') and not c.get('conflict_blocked'):
            claims[key].append(c)
    # Reuse the typed V5 room-group fields which were outside V7's extraction
    # scope. A bedding label or appliance tag is useful prefill, not a guarantee
    # of private equipment, dimensions, measured network speed or assignment.
    equipment={
        'M03':{'kitchen','kitchenette','microwave','fridge','kitchen-stuff','stove','oven','dishwasher'},
        'M04':{'kettle'},'M05':{'safe'},'R03':{'desk'},
        'R01':{'high-speed-internet-access','internet-access','wifi-in-room'},
    }
    structured_claim_count=0;structured_hotels=0
    for hid,h in cohort.items():
        if not h.get('has_room_snapshot'):continue
        raw=gzip.decompress(Path(h['snapshot_file']).read_bytes());doc=json.loads(raw)
        obj=doc['props']['pageProps']['hotel']
        assert str(obj['masterId'])==hid
        raw_hash=hashlib.sha256(raw).hexdigest()
        expected=h['sources'][0].get('json_sha256')
        if expected:assert raw_hash==expected
        got=False
        for i,rg in enumerate(obj.get('roomGroups',[])):
            base=f'/props/pageProps/hotel/roomGroups/{i}'
            group_id=rg.get('rgHash') or f'{hid}:snapshot_group:{i}'
            candidates=[]
            bedding=rg.get('nameStruct',{}).get('beddingType')
            if isinstance(bedding,str) and bedding.strip():
                candidates.append(('M01','published_bedding_text',bedding,base+'/nameStruct/beddingType'))
            for j,tag in enumerate(rg.get('roomAmenityData',[])):
                for fid,tags in equipment.items():
                    if tag in tags:candidates.append((fid,'published_room_equipment_tag',tag,base+f'/roomAmenityData/{j}'))
            for fid,atom,value,pointer in candidates:
                quote=json.dumps(value,ensure_ascii=False)
                ident=hashlib.sha256(json.dumps([hid,fid,pointer,value],ensure_ascii=False).encode()).hexdigest()[:24]
                claims[(hid,fid)].append({'claim_id':'ota9_'+ident,'atom_id':atom,'typed_value':value,
                    'subject_type':'room_group','source_subject_id':str(group_id),'quote':quote,
                    'source_url':h['hotel_url'],'json_pointer':pointer,'source_hash':raw_hash,
                    'fetched_at':h['sources'][0].get('fetched_at'),'origin':'v5_cached_typed_room_field',
                    'scope_limitation':'Published room-group label only. No all-variant guarantee, private-kitchen inference, safe dimensions, measured speed or socket/desk dimensions.'})
                structured_claim_count+=1;got=True
        structured_hotels+=got
    out=BASE/'hotelier';out.mkdir(exist_ok=True)
    rows=[]; prefill=[]
    for fid,f in selected.items():
        supported=[h for h in cohort if claims.get((h,fid))]
        rows.append({'feature_id':fid,'name':f['name'],'scope':f['scope'],
                     'prefill_hotels_n':len(supported),'cohort_N':len(cohort),
                     'prefill_scope':'source-scoped V7 eligible atoms plus cached V5 typed room-group fields; incomplete contract, no transfer to all variants or measured quantities',
                     'free_text_question':f['fallback_question'],'parsed_fields':FIELDS[fid],
                     'computed_prefill':'No calculator can establish the hotelier-only contract; route/facade diagnostics may motivate a question.'})
        for hid in cohort:
            cs=claims.get((hid,fid),[])
            prefill.append({'hotel_id':hid,'city_id':cohort[hid]['city_id'],'feature_id':fid,
                            'question':f['fallback_question'],'parsed_fields':FIELDS[fid].split(','),
                            'answer_text':None,'is_simulated':False,'answer_status':'not_asked',
                            'claims':[{'claim_id':c['claim_id'],'atom_id':c['atom_id'],
                                       'value':c['typed_value'],'subject_type':c['subject_type'],
                                       'source_subject_id':c['source_subject_id'],'quote':c['quote'],
                                       'source_url':c['source_url'],'json_pointer':c['json_pointer'],
                                       'source_hash':c['source_hash'],'fetched_at':c['fetched_at'],
                                       'status':'ostrovok_content','full_contract_eligible':False,
                                       'origin':c.get('origin','v7_eligible_atom'),
                                       'scope_limitation':c.get('scope_limitation'),
                                       'hard_condition_eligible':False} for c in cs]})
    with (out/'questions_26.csv').open('w') as fp:
        w=csv.DictWriter(fp,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    write_jsonl(out/'prefill.jsonl',prefill)
    (out/'manifest.json').write_text(json.dumps({'questions':len(rows),'hotel_feature_pairs':len(prefill),
        'pairs_with_prefill':sum(bool(x['claims']) for x in prefill),'actual_answers':0,
        'new_network_requests':0,'v5_structured_claim_count':structured_claim_count,
        'v5_structured_hotels':structured_hotels,
        'claim_source_sha256':sha256(source),'cohort_sha256':sha256(BASE/'cohort/hotels.jsonl'),
        'limitation':'No new research or hotel contact. Existing claims are prefill, not physical verification.'},ensure_ascii=False,indent=2))
    print(json.dumps({'questions':len(rows),'prefill_pairs':sum(bool(x['claims']) for x in prefill)},ensure_ascii=False))

if __name__=='__main__': main()
