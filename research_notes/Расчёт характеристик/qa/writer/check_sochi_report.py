#!/usr/bin/env python3
"""Verify the Sochi prose against its final inputs; no feature calculation."""
import argparse,csv,hashlib,json,re
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

BASE=Path(__file__).resolve().parents[2]
ROOT=BASE.parents[1]

def sha(path):
    with Path(path).open('rb')as f:return hashlib.file_digest(f,'sha256').hexdigest()

def jl(path):
    with Path(path).open()as f:
        for line in f:
            if line.strip():yield json.loads(line)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--author-reviewed',action='store_true');args=p.parse_args()
    report=ROOT/'reports/Расчёт характеристик — Сочи.md';text=report.read_text()
    summary=json.loads((BASE/'integrated/sochi_summary.json').read_text())
    with(BASE/'integrated/sochi_coverage.csv').open()as f:coverage={r['feature_id']:r for r in csv.DictReader(f)}
    checks={};errors=[];seen=[]
    for line in text.splitlines():
        m=re.match(r'^\| ([A-Z]\d\d) \| [^|]+ \| (\d+)/89 \| (\d+)/89 \| (\d+)/89 \|$',line)
        if not m:continue
        fid,a,b,c=m.groups();seen.append(fid);r=coverage[fid]
        expected=[int(r['computed_n'])+int(r['observed_n']),int(r['estimate_n']),int(r['unknown_n'])]
        if [int(a),int(b),int(c)]!=expected:errors.append([fid,a,b,c,expected])
    checks['54_coverage_rows_exact']=len(seen)==len(set(seen))==54 and not errors
    prefill=Counter(r['feature_id']for r in jl(BASE/'hotelier/prefill.jsonl')if r['city_id']=='sochi'and r['claims'])
    questions=re.findall(r'^\| ([A-Z]\d\d) \| (\d+)/89 \|',text,re.M)
    checks['26_prefill_rows_exact']=len(questions)==26 and all(int(n)==prefill[fid]for fid,n in questions)and sum(prefill.values())==260
    citations=re.findall(r'паспорт [A-Z]\d\d · ([0-9a-f]+)',text)
    factids={f['fact_id']for f in jl(BASE/'integrated/sochi_facts.jsonl')}
    checks['21_example_passports_exist']=len(citations)==21 and set(citations)<=factids
    checks['21_maps']=text.count('[карта]')==21
    checks['7_sections']=all(f'## {i}.'in text for i in range(1,8))
    checks['7_decisions']=len(re.findall(r'^\d\. \[вывод\]',text,re.M))==7
    local=re.findall(r'\]\(<([^>]+)>\)',text);missing=[v for v in local if not Path(re.sub(r':\d+$','',v)).exists()]
    checks['local_links_exist']=not missing
    checks['not_DRAFT_HOLD']='DRAFT'not in text and 'HOLD'not in text
    checks['address_repair_explicit']='редакция v9.4'in text and '66 связей'in text and '46/89'in text and 'N02/N03'in text
    checks['Q03_scope_explicit']='нанесённой графовой полилинии'in text
    checks['marine_scope_preserved']='0,494025'in text and 'вход'in text and '86'in text
    checks['root_integration_passed']=summary['status']=='PASSED'
    quality=json.loads((BASE/'qa/quality/all8_review.json').read_text())
    checks['final_quality_passed']=quality['status'].startswith('PASSED')
    checkpoint=BASE/'checkpoints/sochi_accepted/Расчёт характеристик — Сочи.md'
    checks['accepted_original_unchanged']=sha(checkpoint)=='7853c3b2209d52dd1481f1d035a5848485b173ce8ea427601112b79438a30a1d'
    inputs=['integrated/sochi_facts.jsonl','integrated/sochi_summary.json','integrated/sochi_coverage.csv','results/facades/sochi_summary.json','results/geo/sochi_run.json','results/environment/sochi_summary.json','cohort/hotels.jsonl','cohort/manifest.json','hotelier/questions_26.csv','hotelier/prefill.jsonl','qa/facades/final8_review.json','qa/geo/sochi_F01_node_repair_report.json','qa/geo/sochi_full_corrected_reselect_audit.json','qa/geo/sochi_N02N03_strict_join_repair_report.json','qa/quality/all8_review.json','scripts/write_sochi_report.py','qa/writer/check_sochi_report.py']
    freeze_files={BASE/v for v in inputs}
    freeze_files.update(Path(re.sub(r':\d+$','',v))for v in local if v.startswith('/'))
    freeze={'at':datetime.now(timezone.utc).isoformat(),'report_sha256':sha(report),'files':[{'path':str(v),'sha256':sha(v),'bytes':v.stat().st_size}for v in sorted(freeze_files)if v.is_file()]}
    fp=BASE/'qa/writer/sochi_source_freeze.json';fp.write_text(json.dumps(freeze,ensure_ascii=False,indent=2)+'\n')
    result={'at':datetime.now(timezone.utc).isoformat(),'report':str(report),'sha256':sha(report),'bytes':report.stat().st_size,'lines':len(text.splitlines()),'words':len(text.split()),'checks':checks,'coverage_errors':errors,'missing_links':missing,'source_freeze':str(fp),'source_freeze_sha256':sha(fp),'author_full_review':args.author_reviewed,'status':'PASSED'if all(checks.values())and args.author_reviewed else 'FAILED_OR_REVIEW_PENDING','root_review':'required'}
    (BASE/'qa/writer/sochi_author_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    qp=BASE/'qa/writer/sochi_report_qa.json';meta=json.loads(qp.read_text());meta.update(result);qp.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not all(checks.values()):raise SystemExit(1)

if __name__=='__main__':main()
