#!/usr/bin/env python3
"""One hardware worker: sequential unchanged facades -> QA -> nodes -> geo -> QA.

Never runs Sochi or recomputes the already accepted Moscow facade stage.
Each child exits before the next large scene/graph is loaded on this 8GB host.
"""
import json,hashlib,subprocess,time,sys
from pathlib import Path
from common import BASE,V4,ROOT,now,sha256
PYTHON=V4/'.venv/bin/python'
EXPECTED={'scripts/facades.py':'7509c3ae212510319ff387b98c383a14435a54a4a31e0cd0689236320527099d','qa/facades/check_outputs.py':'c67953253025886ed64b344f6e4ddff9c580aeb132d0c8281bbac1e33c1669ca','scripts/geo.py':'e575c51e28cbc3905eadb4e4d628005b9fad99031301a741d0477c80e73473ad'}
CITIES=['spb','kazan','kaliningrad','minsk','istanbul','dubai']
def main():
    for rel,digest in EXPECTED.items():
        if sha256(BASE/rel)!=digest:raise RuntimeError('Owned source changed before execution: '+rel)
    progress={'started_at':now(),'stage':'starting','city_id':None,'completed':[],'source_sha256':EXPECTED,'execution_policy':'One heavy child process at a time; no Sochi and no Moscow facade recomputation.'};pp=BASE/'qa/geo/hardware_queue_progress.json'
    def save():pp.write_text(json.dumps(progress,ensure_ascii=False,indent=2)+'\n')
    def stage(city,name,script,args):
        for rel,digest in EXPECTED.items():
            if sha256(BASE/rel)!=digest:raise RuntimeError('Frozen source changed: '+rel)
        progress.update(city_id=city,stage=name,updated_at=now());save();print(json.dumps({'event':'stage_started','city_id':city,'stage':name,'at':now()}),flush=True);start=time.perf_counter();log=BASE/'qa/geo'/f'{city}_{name}.log'
        with log.open('w') as f:r=subprocess.run([str(PYTHON),str(BASE/script),*args],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
        seconds=time.perf_counter()-start
        if r.returncode:
            progress.update(stage='failed',failed_stage=name,returncode=r.returncode,log_file=str(log),updated_at=now());save();raise RuntimeError(f'{city}/{name} failed: '+str(log))
        print(json.dumps({'event':'stage_done','city_id':city,'stage':name,'seconds':seconds,'log_file':str(log)},ensure_ascii=False),flush=True)
        return seconds
    for city in CITIES:
        times={}
        times['facades']=stage(city,'facades','scripts/facades.py',['--city',city]);times['facades_qa']=stage(city,'facades_qa','qa/facades/check_outputs.py',[city]);times['node_supplement']=stage(city,'node_supplement','cohort/extract_v9_node_restrictions.py',['--city',city]);times['geo']=stage(city,'geo','scripts/run_geo_v9_other_city_compact.py',['--city',city]);times['geo_qa']=stage(city,'geo_qa','qa/geo/check_city_outputs.py',['--city',city]);times['marine_reference_targets']=stage(city,'marine_reference_targets','scripts/build_marine_reference_targets.py',['--city',city])
        event={'event':'city_complete','city_id':city,'at':now(),'stage_seconds':times,'geo_run_file':str(BASE/'results/geo'/f'{city}_run.json'),'walk_targets_file':str(BASE/'results/geo'/f'{city}_beach_targets.jsonl'),'walk_targets_sha256':sha256(BASE/'results/geo'/f'{city}_beach_targets.jsonl'),'marine_reference_file':str(BASE/'results/geo'/f'{city}_marine_targets_with_fallback.jsonl'),'marine_reference_sha256':sha256(BASE/'results/geo'/f'{city}_marine_targets_with_fallback.jsonl')};progress['completed'].append(event);progress.update(stage='city_complete',updated_at=now());save();print(json.dumps(event,ensure_ascii=False),flush=True)
        with (ROOT/'codex_memory/progress_v9_geo.md').open('a') as f:f.write('\n'+now()+' — '+city+' complete: facades+QA, nodes, geo+QA, supplementalmarine. MarineSHA '+event['marine_reference_sha256']+'. Heavy stages sequential; seehardware_queue_progress.json for actualtimings.\n')
    progress.update(stage='complete',completed_at=now());save();print(json.dumps({'event':'queue_complete','cities':CITIES,'at':now()}),flush=True)
if __name__=='__main__':main()
