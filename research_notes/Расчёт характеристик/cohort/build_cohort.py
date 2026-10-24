#!/usr/bin/env python3
"""Freeze V9 hotel entities exclusively from existing V5 caches. No HTTP."""
import csv,gzip,hashlib,json,sys,collections
from pathlib import Path
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE/'scripts'))
from common import V5,now,write_jsonl,sha256

def subzone(h):
    l=h['location'];lat=l['latitude'];lon=l['longitude'];a=(l.get('address','')+' '+l.get('city','')).lower()
    if lon>40.05 or 'красная поляна' in a or 'эсто-садок' in a:return 'krasnaya_polyana'
    if lat>43.85:return 'lazarevskoe'
    if 'дагомыс' in a or (43.635<lat<43.685 and 39.63<lon<39.68):return 'dagomys'
    if 43.66<lat<43.74 and 39.53<lon<39.64:return 'loo_vardane'
    if 43.74<=lat<=43.85:return 'other_greater_sochi_golovinka'
    if l.get('city')=='Сириус' or (lat<43.43 and lon>39.926):return 'sirius'
    if lat<43.49:return 'adler'
    if lat<43.565:return 'khosta_matsesta'
    return 'center'

def main():
    old=json.loads((V5/'manifests/valid_sample_cohort.json').read_text());accepted={str(r['masterId']) for r in old['pages']}
    ledger=[json.loads(s) for s in (V5/'manifests/http.jsonl').read_text().splitlines()]
    byhash={r.get('sha256'):r for r in ledger if r.get('status')==200}
    urlbyfile={r.get('next_file'):r for r in (json.loads(s) for s in (V5/'manifests/sample_results.jsonl').read_text().splitlines())}
    output=[];seen=set();inventory=[]
    def add(h,city,fp,url,htmlsha,pointer,origin):
        mid=str(h.get('masterId',''));l=h.get('location',{})
        if not mid or mid=='None' or not h.get('name') or not l.get('latitude') or not l.get('longitude') or not l.get('address'):return False
        if mid in seen:return False
        b=gzip.decompress(fp.read_bytes());lr=byhash.get(htmlsha,{})
        source={'provider':'ostrovok_cached_v5','url':url,'snapshot_file':str(fp.resolve()),'json_pointer':pointer,'json_sha256':hashlib.sha256(b).hexdigest(),'html_sha256':htmlsha,'fetched_at':lr.get('fetched_at'),'license_status':'research_cache_only_pending_written_permission_no_new_requests'}
        row={'hotel_id':mid,'city_id':city,'name':h['name'],'latitude':l['latitude'],'longitude':l['longitude'],'address':l['address'],'source_city':l.get('city'),'ota_slug':h.get('id'),'hotel_kind':h.get('facts',{}).get('kind',h.get('kind')),'cohort_origin':origin,'sources':[source],'snapshot_file':source['snapshot_file'],'json_pointer':pointer,'hotel_url':url,'has_room_snapshot':origin!='cached_serp_supplement','timezone':{'kaliningrad':'Europe/Kaliningrad','dubai':'Asia/Dubai','minsk':'Europe/Minsk','istanbul':'Europe/Istanbul'}.get(city,'Europe/Moscow')}
        if city=='sochi':row['subzone']=subzone(h)
        output.append(row);seen.add(mid);return True
    for r in old['pages']:
        if r['city_key']=='batumi':continue
        fp=V5/r['next_file'];h=json.loads(gzip.decompress(fp.read_bytes()))['props']['pageProps']['hotel']
        add(h,r['city_key'],fp,r['url'],r['html_sha256'],'/props/pageProps/hotel','v5_accepted_50')
    for fp in sorted((V5/'raw').glob('*.next.json.gz')):
        pp=json.loads(gzip.decompress(fp.read_bytes())).get('props',{}).get('pageProps',{});h=pp.get('hotel',{});l=h.get('location',{});lat=l.get('latitude');lon=l.get('longitude')
        inventory.append({'file':str(fp),'hotel_masterId':h.get('masterId'),'has_serp':bool(pp.get('serpData')),'latitude':lat,'longitude':lon})
        if lat and lon and 43.35<lat<44.05 and 39.15<lon<40.5:
            rel=str(fp.relative_to(V5));r=urlbyfile.get(rel,{});add(h,'sochi',fp,r.get('canonical',r.get('url')),fp.name.split('.')[0],'/props/pageProps/hotel','cached_full_page_supplement')
    for r in csv.DictReader((V5/'csv/city_page_claims.csv').open()):
        if not any(z in r['url'] for z in ['/sochi/','/adler/']):continue
        fp=V5/r['next_file'];pp=json.loads(gzip.decompress(fp.read_bytes()))['props']['pageProps']
        for i,h in enumerate(pp['serpData']['hotels']):
            l=h.get('location',{});lat=l.get('latitude');lon=l.get('longitude')
            if lat and lon and 43.35<lat<44.05 and 39.15<lon<40.5:
                # URL to cached page, not a fabricated hotel-specific canonical.
                add(h,'sochi',fp,r['url'],fp.name.split('.')[0],f'/props/pageProps/serpData/hotels/{i}','cached_serp_supplement')
    output.sort(key=lambda h:(h['city_id'],h['hotel_id']));write_jsonl(BASE/'cohort/hotels.jsonl',output);write_jsonl(BASE/'cohort/cache_inventory.jsonl',inventory)
    cs=collections.Counter(h['city_id'] for h in output);zs=collections.Counter(h.get('subzone') for h in output if h['city_id']=='sochi')
    manifest={'built_at':now(),'input_cohort_sha256':sha256(V5/'manifests/valid_sample_cohort.json'),'input_cohort_id':old['cohort_id'],'city_counts':dict(cs),'sochi_origins':dict(collections.Counter(h['cohort_origin'] for h in output if h['city_id']=='sochi')),'sochi_subzones':dict(zs),'new_http_requests':0,'cache_next_files_inspected':len(inventory),'sample_attempt_records_inspected':sum(1 for _ in open(V5/'manifests/sample_results.jsonl')),'cohort_sha256':sha256(BASE/'cohort/hotels.jsonl'),'blocked_subzones':['dagomys'] if not zs['dagomys'] else [],'limitations':['SERP supplements have name/address/coordinates/masterId, but no full hotel/category snapshot. Their source pointer is the city cached card, not a made-up individual hotel URL.','D28 supplement is exhaustive over cached valid geographically matching records, not an operating-hotels census. Exact ~100 target cannot override no-new-OTA-request restriction.','Subzone tags use disclosed address/coordinate rules and are approximate geographic strata, not surveyed administrative boundaries.','No cached Dagomys entity satisfies hotel identity/address/coordinates checks; absence in cache is not absence of hotels.']}
    (BASE/'cohort/manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');print(json.dumps(manifest,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
