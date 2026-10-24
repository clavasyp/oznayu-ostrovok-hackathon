#!/usr/bin/env python3
"""Check the report against frozen inputs; does not compute hotel features."""
import argparse,csv,hashlib,json,re
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

BASE=Path(__file__).resolve().parents[2]
ROOT=BASE.parents[1]
CITIES=['sochi','moscow','spb','kazan','kaliningrad','minsk','istanbul','dubai']

def sha(path):
    with Path(path).open('rb')as f:return hashlib.file_digest(f,'sha256').hexdigest()

def rows(path):
    with Path(path).open()as f:
        for line in f:
            if line.strip():yield json.loads(line)

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--author-reviewed',action='store_true');args=parser.parse_args()
    p=ROOT/'reports/Расчёт характеристик на реальных отелях.md';text=p.read_text()
    summary=json.loads((BASE/'integrated/all8_summary.json').read_text())
    with(BASE/'integrated/all8_coverage.csv').open()as f:coverage=list(csv.DictReader(f))
    cv={(r['city_id'],r['feature_id']):r for r in coverage};N={r['city_id']:r['N_hotels']for r in summary['city_summary']}
    checks={};matrix_errors=[];seen=[]
    for line in text.splitlines():
        m=re.match(r'^\| ([A-Z]\d\d) · ',line)
        if not m:continue
        fid=m.group(1);seen.append(fid);cells=[s.strip()for s in line.split('|')[2:-1]]
        if len(cells)!=8:matrix_errors.append([fid,'not8cities']);continue
        for city,cell in zip(CITIES,cells):
            try:actual=[int(x)for x in cell.split('·')]
            except ValueError:matrix_errors.append([fid,city,'format']);continue
            r=cv[city,fid];expected=[int(r['computed_n'])+int(r['observed_n']),int(r['estimate_n']),int(r['unknown_n']),int(r['not_applicable_n'])]
            if actual!=expected or sum(actual)!=N[city]:matrix_errors.append([fid,city,actual,expected])
    checks['matrix54x8_exact']=len(seen)==len(set(seen))==54 and not matrix_errors
    prefill=Counter(r['feature_id']for r in rows(BASE/'hotelier/prefill.jsonl')if r['claims'])
    question_rows=re.findall(r'^\| ([A-Z]\d\d) \| (\d+)/(\d+) \|',text,re.M)
    checks['26_prefill_rows_exact']=len(question_rows)==26 and all(int(a)==prefill[fid]and int(n)==summary['N_hotels']for fid,a,n in question_rows)
    cited=re.findall(r'паспорт [A-Z]\d\d · ([0-9a-f]+)',text);cited_set=set(cited);found={};forbidden_known=[]
    for f in rows(BASE/'integrated/all8_facts.jsonl'):
        if f['fact_id']in cited_set:found[f['fact_id']]=f
        if f['status']in {'computed','observed','estimate'}and (f['hotel_id'],f['subject_id'])in {('7725078','way/808856239'),('8085655','way/80180198'),('7719161','way/134871120')}:
            forbidden_known.append(f['fact_id'])
    checks['21_example_passports_valid']=len(cited)==21 and cited_set==set(found)
    checks['example_cities_cover_all8']={f['city_id']for f in found.values()}==set(CITIES)
    checks['no_known_false_parent_facts']=not forbidden_known
    local=re.findall(r'\]\(<([^>]+)>\)',text);missing=[x for x in local if not Path(re.sub(r':\d+$','',x)).exists()]
    checks['local_links_exist']=not missing
    checks['21_maps']=len(re.findall(r'\[карта\]',text))==21
    checks['7_sections']=all(f'## {i}.'in text for i in range(1,8))
    checks['7_decisions']=len(re.findall(r'^\d\. \[вывод\]',text,re.M))==7
    checks['not_DRAFT_HOLD']='Рабочая авторская версия'not in text and 'Рабочий черновик'not in text and 'HOLD'not in text and 'DRAFT'not in text
    totals=Counter()
    for r in summary['city_summary']:totals.update(r['best_status_counts'])
    checks['headline_counts_exact']=all(str(totals[k])in text.split('## 1.')[0]for k in ['computed','observed','estimate','unknown','not_applicable'])
    checks['gold_limits_explicit']='0 полных независимых gold'in text and '0 человеческих проверок'in text and 'ошибка маршрута в метрах не измерены'in text
    checks['Q03_denominator_explicit']='Знаменатель Q03 — длина нанесённой графовой полилинии'in text and 'весь путь освещён'in text
    checks['waterbody_inference_explicit']='Это вывод Codex для проверки'in text and 'региональный контекст'in text
    checks['P03_R2_preserved']='официальный реестр сверх R2 не вводится'in text
    checks['no_Mantera_transfer']='Официальная страница лобби подтверждает'not in text
    checks['strict_address_repair_explicit']='176 связей у 163 отелей'in text and '43 отелей пяти городов'in text and '139 прежних − 4 отклонённые + 41 новая'in text
    with(BASE/'qa/writer/ota_comparisons_all8.csv').open()as f:comparisons=list(csv.DictReader(f))
    nc={fid:Counter(r['numeric_comparison']for r in comparisons if r['feature_id']==fid)for fid in ['H01','H03']}
    checks['OTA_numeric_operands_classified']=len(comparisons)==99 and nc['H03']==Counter({'missing_independent_numeric_operand':33,'raw_numeric_difference_or_multiple_scope':6,'raw_numeric_agreement':1})and nc['H01']==Counter({'missing_independent_numeric_operand':40,'raw_numeric_difference_or_multiple_scope':2,'raw_numeric_agreement':1})
    checks['actual_runner_links']='run_geo_v9_other_city_compact.py'in text and 'run_geo_v9_other_city_compact_v2.py'in text and 'scripts/run_geo_v9_other_city.py'not in text
    freeze=json.loads((BASE/'qa/writer/all8_source_freeze.json').read_text());changed=[r['path']for r in freeze['files']if sha(r['path'])!=r['sha256']]
    checks['writer_source_freeze_exact']=not changed and freeze['report_sha256']==sha(p)
    checks['root_integration_passed']=summary['status']=='PASSED'
    result={'at':datetime.now(timezone.utc).isoformat(),'report':str(p),'sha256':sha(p),'bytes':p.stat().st_size,
        'lines':len(text.splitlines()),'words':len(text.split()),'lead_words':len(text.split('## 1.')[0].split()),
        'checks':checks,'matrix_errors':matrix_errors,'missing_local_links':missing,'changed_frozen_sources':changed,
        'forbidden_known_fact_ids':forbidden_known,'source_freeze_sha256':sha(BASE/'qa/writer/all8_source_freeze.json'),
        'author_full_review':args.author_reviewed,'status':'PASSED'if all(checks.values())and args.author_reviewed else'FAILED_OR_REVIEW_PENDING',
        'scope':'Report consistency and source provenance only; no physical feature accuracy claimed.'}
    (BASE/'qa/writer/all8_author_checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    q=BASE/'qa/writer/all8_report_qa.json';meta=json.loads(q.read_text());meta.update({'author_full_review':'PASSED'if args.author_reviewed else'pending','author_checks':str(BASE/'qa/writer/all8_author_checks.json'),'author_checks_sha256':sha(BASE/'qa/writer/all8_author_checks.json'),'source_freeze':str(BASE/'qa/writer/all8_source_freeze.json'),'source_freeze_sha256':result['source_freeze_sha256'],'status':result['status'],'root_review':'required'});q.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not all(checks.values()):raise SystemExit(1)

if __name__=='__main__':main()
