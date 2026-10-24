#!/usr/bin/env python3
"""Render the V9 eight-city report from accepted outputs, never run calculators.

Without --frozen writes an explicit progress draft, without incomplete-city
numbers. --frozen requires all eight integrated outputs and measured scale/QA.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from write_sochi_report import BASE, ROOT, link, table

CITIES = ['sochi','moscow','spb','kazan','kaliningrad','minsk','istanbul','dubai']
NAMES = dict(zip(CITIES,['Большой Сочи','Москва','Петербург','Казань','Калининград','Минск','Стамбул','Дубай']))
OUT = ROOT / 'reports/Расчёт характеристик на реальных отелях.md'
QA = BASE / 'qa/writer'
NEEDED = {'N02','N04','W01','W02','W03','W04','W05','L01','L03','F01','P03','P04','E03','C01','B02','H03'}


def jsonfile(rel):
    return json.loads((BASE/rel).read_text())


def jsonlines(path):
    with Path(path).open() as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def csvrows(rel):
    with (BASE/rel).open() as handle:
        return list(csv.DictReader(handle))


def dump(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def polish_prose(text):
    """Space prose quantities without modifying exact hotel names/table data."""
    result=[]
    for line in text.splitlines():
        if line.startswith(('[факт]','[вывод]'))or re.match(r'^\d\. \[вывод\]',line):
            line=re.sub(r'(?<=\d)(?=[А-Яа-яЁё])(?![А-ЯЁ]\b)',' ',line)
            line=re.sub(r'(?<=[А-Яа-яЁё])(?=\d)',' ',line)
            line=re.sub(r'\b(MID|WMO|WorldCover)(?=\d)',r'\1 ',line)
            for old,new in {'часовыхпоясов':'часовых поясов','nativeCF':'native CF','actualMID':'фактический MID','coreSHA':'SHA исходного калькулятора','room-group labels':'подписи групп номеров','prooflinks':'ссылки для проверки','pinned-исходниками':'закреплёнными исходниками'}.items():
                line=line.replace(old,new)
        result.append(line)
    return '\n'.join(result)+'\n'


def progress_draft():
    summary=jsonfile('integrated/sochi_summary.json')['city_summary'][0]
    parts=['# Расчёт характеристик на реальных отелях',
        '**Рабочий черновик V9: общий расчёт восьми городов ещё не принят. Числа незавершённых городов не публикуются.**',
        f"[факт] Принята контрольная точка Сочи: {summary['N_hotels']} объектов, все 54 цепочки попытаны, {summary['features_with_computed_or_observed']} характеристик имеют хотя бы один `computed/observed`, {summary['features_with_estimate']} — оценки. Сводка остальных семи будет добавлена после итоговой интеграции и QA. " + f"[Актуальный отчёт Сочи](<{ROOT/'reports/Расчёт характеристик — Сочи.md'}>)",
        table(['Город','Входная когорта','Состояние общего результата'],[[NAMES[c],89 if c=='sochi' else 50,'Принят отдельно; ждёт общей интеграции' if c=='sochi' else 'Вычисление и проверка продолжаются']for c in CITIES]),
        '## 1. Выборка, даты и источники\n\n[факт] Входы: 439 actual MID, Большой Сочи 89 и семь городов по 50; Дагомыс остаётся пробелом кеша. Поездки 14–17.11.2026 и 15–18.07.2027; ночные сервисы — суббота 02:00 в местном часовом поясе. Новых запросов к Островку нет.',
        '## 2. Матрица 54 характеристик и цепочки рецептов\n\n[вывод] Здесь будет одна матрица 54×8: расчёт/источник, оценка, неизвестно, неприменимо. Неприменимость морского контекста у внутренних городов не означает отсутствие любых пляжей. Фасадный веер без проверенных окон не превратится в установленный вид.',
        '## 3. Разметка и метрики качества\n\n[факт] Сочинская разметка Codex: 34 строки / 30 различных контекстов, 0 полных независимых gold и 0 человеческих проверок. Арифметическая проверка восьми городов будет опубликована отдельно и не будет названа физической точностью.',
        '## 4. Расхождения с карточкой\n\n[вывод] Будут сопоставлены все сохранённые городские таблицы с проверкой области, метода и даты. Unknown не опровергает название категории, а числа разных корпусов не выбираются голосованием источников.',
        '## 5. 26 вопросов отельеру\n\n[факт] Вход — V7 и типизированные поля кеша V5. Подпись кровати не гарантирует конфигурацию, оборудование не доказывает приватную кухню, тег связи не даёт Мбит/с. Полных подтверждённых контрактов и внешних ответов нет.',
        '## 6. Семь сценариев\n\n[вывод] Финальная версия выберет по три реальных случая для заката/тишины/моря, семьи, питомца, ночного приезда, удалёнки, маломобильности и пляжного сезона. Отель выбирается из фактической когорты; обязательные свойства не выдумываются для показа.',
        '## 7. Стоимость, размер и перенос в ядро\n\n[факт] Московские прерванные попытки будут раскрыты отдельно от успешного времени: первая286с с профилем cyclic GC/swap; вторая≥553с с установленным блокером Rust OpeningHours.next_change уже после построения графа; ещё один короткий запуск остановлен для применения исправления. Нельзя приписывать всё время второй попытки доказанной memory-thrash. Runner сохраняет точные проверки открытия на обе даты, кеширует их и пропускает только необязательный поиск следующей смены часов. Группы W/A/Q и V/S/H не распределяются вымышленно по семействам. Docker≤10мин и стоимость закрытия полевых пробелов не измерены.\n\n'+link('Методика и правильный runner семи городов','README.md')]
    OUT.write_text('\n\n'.join(parts)+'\n')
    dump(QA/'all8_report_qa.json',{'at':datetime.now(timezone.utc).isoformat(),'report':str(OUT),'status':'DRAFT','sha256':digest(OUT),'no_incomplete_city_results_claimed':True,'root_final_data_GO_required':True})
    print(json.dumps({'status':'DRAFT','file':str(OUT),'sha256':digest(OUT)},ensure_ascii=False))


def load_inputs():
    required=['integrated/all8_summary.json','integrated/all8_coverage.csv','integrated/all8_facts.jsonl',
        'scale/summary.json','scale/city_metrics.csv','scale/family_metrics.csv',
        'validation/numerical_all8_summary.json','qa/root/all8_fan_audit.json','qa/quality/all8_review.json']
    missing=[str(BASE/p)for p in required if not(BASE/p).exists()]
    if missing:
        raise RuntimeError('All-city data not accepted/complete: '+', '.join(missing))
    summary=jsonfile(required[0])
    if summary['status']!='PASSED' or set(summary['cities'])!=set(CITIES):
        raise RuntimeError('Need a PASSED integration of exactly all eight cities')
    coverage=csvrows('integrated/all8_coverage.csv')
    if len(coverage)!=432 or len({(r['city_id'],r['feature_id'])for r in coverage})!=432:
        raise RuntimeError('Coverage must contain 54×8 unique rows')
    hotels={str(h['hotel_id']):h for h in jsonlines(BASE/'cohort/hotels.jsonl') if h['city_id']in CITIES}
    # Reduced records avoid loading repeated 30-source climate passports into
    # memory while the hardware worker finishes other cities.
    facts={}
    for f in jsonlines(BASE/'integrated/all8_facts.jsonl'):
        if f['feature_id']not in NEEDED:
            continue
        key=(f['hotel_id'],f['feature_id'],f['params'].get('month'))
        previous=facts.get(key)
        if previous and previous['status']in {'computed','observed','estimate'}:
            continue
        value=f.get('value')
        if isinstance(value,dict):
            value={k:v for k,v in value.items() if not isinstance(v,(dict,list))}
        facts[key]={k:f[k]for k in ['fact_id','hotel_id','city_id','feature_id','status','subject_level','subject_id','params']}
        facts[key]['value']=value
        facts[key]['source_url']=next((s['url']for s in f.get('sources',[])if s.get('url')and 'openstreetmap.org/copyright'not in s['url']),None)
        facts[key]['unknown_reason']=f.get('unknown_reason')
    numerical=jsonfile('validation/numerical_all8_summary.json')
    fan=jsonfile('qa/root/all8_fan_audit.json')
    if numerical['failed'] or not fan['passed']:
        raise RuntimeError('Numerical/fan QA is not passed')
    quality=jsonfile('qa/quality/all8_review.json')
    if not quality['status'].startswith('PASSED') or quality['summary']['hotels']!=len(hotels):
        raise RuntimeError('Final independent technical quality review is not passed')
    return summary,coverage,hotels,facts,numerical,fan


def render_final():
    summary,coverage,hotels,facts,numerical,fan=load_inputs()
    QA.mkdir(parents=True,exist_ok=True)
    bycity={r['city_id']:r for r in summary['city_summary']}
    N=summary['N_hotels']; pairs=summary['hotel_feature_pairs']
    totals=Counter()
    for c in CITIES:
        totals.update(bycity[c]['best_status_counts'])
    cov={(r['city_id'],r['feature_id']):r for r in coverage}
    feature_names={r['feature_id']:r['feature_name']for r in coverage}
    questions=csvrows('hotelier/questions_26.csv')
    prefill=list(jsonlines(BASE/'hotelier/prefill.jsonl'))
    pref_counts=Counter(r['feature_id']for r in prefill if r['claims'])
    pref_pairs=sum(pref_counts.values())
    cityscale=csvrows('scale/city_metrics.csv');familyscale=csvrows('scale/family_metrics.csv')
    scale=jsonfile('scale/summary.json')
    station=jsonfile('validation/station_summary.json')['summary']
    labels=jsonfile('validation/cases_summary.json')
    facades={c:jsonfile(f'results/facades/{c}_summary.json')for c in CITIES}
    quality=jsonfile('qa/quality/all8_review.json')
    network=jsonfile('qa/environment/all8_network_metrics.json')
    wet=jsonfile('qa/environment/coastal_wetcell_validation.json')
    repair=jsonfile('qa/geo/sochi_F01_node_repair_report.json')
    repair_phase=next(p for p in jsonfile('results/geo/sochi_run.json')['repair_phases']if 'F01'in p['version'])
    facade_review=jsonfile('qa/facades/final8_review.json')
    join_review=jsonfile('qa/geo/all8_building_axis_join_coverage.json')
    q03_scope=jsonfile('qa/quality/Q03_graph_denominator_scope.json')

    def get(hid,fid,month=None):
        return facts.get((str(hid),fid,month))

    def metric(hid,fid,key=None,unit='',month=None,digits=1):
        f=get(hid,fid,month);label=fid+{7:' (июль)',11:' (ноябрь)'}.get(month,'')
        if not f:
            return label+': неизвестно'
        if f['status']=='not_applicable':
            return label+': неприменимо к заданному морскому контексту'
        if f['status']=='unknown':
            return label+': неизвестно'
        if fid in {'N02','N03','H03'} and f['subject_level']in {'building','building_part'}:
            label+=' ['+f['subject_id']+']'
        v=f['value']
        if key:
            v=v.get(key)if isinstance(v,dict)else None
        if v is None:
            return label+': значение не установлено'
        if isinstance(v,(int,float)):
            v=f'{v:.{digits}f}'if digits else str(round(v))
        st={'computed':'расчёт','observed':'по источнику','estimate':'оценка'}.get(f['status'],f['status'])
        return f'{label}: {v}{unit} ({st})'

    def pick(city,fid,key=None,month=None,largest=False,exclude=()):
        pool=[h for h in hotels.values()if h['city_id']==city and h['hotel_id']not in exclude]
        def rank(h):
            f=get(h['hotel_id'],fid,month)
            if not f or f['status']not in {'computed','observed','estimate'}:
                return (1,0,h['hotel_id'])
            v=f['value'].get(key)if key and isinstance(f['value'],dict)else f['value']
            if not isinstance(v,(int,float)):
                v=0
            return(0,-v if largest else v,h['hotel_id'])
        return sorted(pool,key=rank)[0]['hotel_id']

    used_citations=[]
    def example(hid,values,question,source_feature,month=None):
        h=hotels[str(hid)];f=get(hid,source_feature,month)
        if f:
            proof=link(f"паспорт {source_feature} · {f['fact_id']}",'integrated/all8_facts.jsonl');used_citations.append(f['fact_id'])
            if f['source_url']:
                proof+=f"; [первоисточник]({f['source_url']})"
        else:
            proof=link('факты и причины неизвестности','integrated/all8_facts.jsonl')
        proof+=f"; [карта](https://www.openstreetmap.org/?mlat={h['latitude']:.6f}&mlon={h['longitude']:.6f}#map=17/{h['latitude']:.6f}/{h['longitude']:.6f})"
        route=get(hid,'W05')
        if source_feature=='W05'and route:
            kind=route['params'].get('target_kind')
            values.append('Цель этого маршрута: '+{'beach':'выбранный пляж','stop':'выбранная остановка','center':'заданная точка центра'}.get(kind,str(kind)))
            route_feature={'beach':'W01','stop':'W02','center':'W03'}.get(kind)
            if route_feature:
                values.append(metric(hid,route_feature,'distance_m',' м'))
        return [f"{NAMES[h['city_id']]} · {h['name']} · MID {hid}",'[факт] '+'; '.join(values),'[вывод] '+question,proof]

    lead_table=table(['Город, N','54 цепочки','Есть расчёт/источник, ID','Есть оценка, ID','Неизвестно, пары','Неприменимо, пары'],[[f"{NAMES[c]} · {bycity[c]['N_hotels']}",bycity[c]['features_attempted'],bycity[c]['features_with_computed_or_observed'],bycity[c]['features_with_estimate'],f"{bycity[c]['best_status_counts'].get('unknown',0)}/{bycity[c]['hotel_feature_pairs']}",f"{bycity[c]['best_status_counts'].get('not_applicable',0)}/{bycity[c]['hotel_feature_pairs']}"]for c in CITIES])
    matrix=table(['ID / характеристика']+[f"{NAMES[c]}, N={bycity[c]['N_hotels']}"for c in CITIES],[[fid+' · '+feature_names[fid]]+['·'.join(str(int(cov[c,fid][k]))for k in ['computed_n','estimate_n','unknown_n','not_applicable_n'])if not int(cov[c,fid]['observed_n'])else '·'.join(str(int(cov[c,fid][k])+int(cov[c,fid]['observed_n'])if k=='computed_n'else int(cov[c,fid][k]))for k in ['computed_n','estimate_n','unknown_n','not_applicable_n'])for c in CITIES]for fid in sorted(feature_names)])
    cohort_counts=Counter(h['city_id']for h in hotels.values())
    timezone_rows=[]
    for c in CITIES:
        h=next(h for h in hotels.values()if h['city_id']==c);tz=h['timezone']
        offsets=[datetime.fromisoformat(day+'T02:00:00').replace(tzinfo=ZoneInfo(tz)).strftime('%z')for day in ['2026-11-14','2027-07-17']]
        timezone_rows.append([NAMES[c],cohort_counts[c],tz,offsets[0]+' / '+offsets[1]])
    matches=table(['Город','Связаны объекты/когорта','Связей отель→корпус','Ячеек веера','Полных паспортов веера'],[[NAMES[c],f"{facades[c]['matched_hotels']}/{cohort_counts[c]}",facades[c]['matched_buildings'],facades[c]['fan_cells'],facades[c]['full_passport_fan_facts']]for c in CITIES])
    questiontable=table(['ID','Предзаполнение V5+V7, n/N','Свободный вопрос','Поля ответа'],[[r['feature_id'],f"{pref_counts[r['feature_id']]}/{N}",r['free_text_question'],r['parsed_fields']]for r in questions])

    # All saved comparisons remain accessible; selection below is deliberately
    # for diagnostic variety, not an estimated error rate of the OTA catalogue.
    comparisons=[]
    for c in CITIES:
        p=BASE/f'results/facades/{c}_ota_comparisons.jsonl'
        if p.exists():
            comparisons.extend(jsonlines(p))
    def numeric_comparison(row):
        if row['feature_id']not in {'H01','H03'}:
            return 'category_claim_without_window_mapping'
        other=row.get('other_value')
        operands=list(other.values())if isinstance(other,dict)else [other]
        try:
            nums=[float(v)for v in operands if v is not None]
            ota=float(row['ota_value'])
        except (TypeError,ValueError):
            nums=[]
        if not nums:
            return 'missing_independent_numeric_operand'
        return 'raw_numeric_agreement'if all(v==ota for v in nums)else 'raw_numeric_difference_or_multiple_scope'
    for r in comparisons:
        r['numeric_comparison']=numeric_comparison(r)
    numeric_counts={fid:Counter(r['numeric_comparison']for r in comparisons if r['feature_id']==fid)for fid in ['H01','H03']}
    with(QA/'ota_comparisons_all8.csv').open('w')as handle:
        fields=['city_id','hotel_id','name','feature_id','ota_value','other_value','classification','numeric_comparison','resolution','ota_url','ota_pointer','snapshot']
        w=csv.DictWriter(handle,fieldnames=fields);w.writeheader()
        for r in comparisons:
            w.writerow({k:json.dumps(r.get(k),ensure_ascii=False)if isinstance(r.get(k),(dict,list))else r.get(k)for k in fields})
    classes=Counter(r['classification']for r in comparisons)
    comp_by_city=Counter(r['city_id']for r in comparisons)
    selected=[]
    for c in CITIES:
        local=[r for r in comparisons if r['city_id']==c]
        for fid in ['H03','V01','H01']:
            candidates=[r for r in local if r['feature_id']==fid]
            if candidates:
                selected.append(candidates[0])
    comp_rows=[]
    for r in selected:
        def compact(v):
            if v is None:
                return 'Неизвестно'
            text=json.dumps(v,ensure_ascii=False)
            if isinstance(v,list):
                return f'{len(v)} опубликованных вариантов; полные значения в CSV'
            return text if len(text)<220 else text[:217]+'…'
        resolution={
            'different_building_scope_or_counting_convention_not_comparable':'Не определены общий корпус/система счёта. Нельзя объявить источник неверным без покорпусного уточнения.',
            'category_claim_vs_unmapped_window_fan_not_comparable':'Категория не привязана к окну/фасаду/этажу. Unknown не опровергает заявление.',
            'no_independent_construction_completion_event':'Нет подтверждённого события завершения строительства; нужен документ корпуса.',
            'construction_event_not_verified_start_date_is_candidate':'Событие start_date не проверено; открытие, реконструкция и завершение различаются.',
            'no_building_match_or_levels':'Нет принятой связи с контуром/этажности; сопоставимое значение неизвестно.',
            'raw_numbers_agree_not_verified_counting_convention':'Сырые числа совпадают, но конвенция карточки не подтверждена.'
        }.get(r['classification'],r['resolution'])
        labels_rus={
            'missing_independent_numeric_operand':'Нет независимого числового значения',
            'raw_numeric_agreement':'Сырые числа совпали; метод не подтверждён',
            'raw_numeric_difference_or_multiple_scope':'Сырые числа различаются / несколько корпусов',
            'category_claim_without_window_mapping':'Категория без привязки к окну'
        }
        missing=r['numeric_comparison']=='missing_independent_numeric_operand'
        if missing:
            resolution='Независимого числового значения нет; расхождение чисел не установлено. '+('Нужен документ о завершении строительства.'if r['feature_id']=='H01'else 'Нужны принятая связь с корпусом и его этажность.')
        comp_rows.append([NAMES[r['city_id']]+' · '+r['name']+' · '+r['feature_id'],compact(r.get('ota_value')),'Неизвестно'if missing else compact(r.get('other_value')),labels_rus[r['numeric_comparison']],resolution])
    comp_table=table(['Объект / поле','Островок','Наш слой','Классификация','Что решает'],comp_rows)

    scenarios=[]
    ids=['13736470',pick('istanbul','W01','distance_m'),pick('dubai','W01','distance_m')]
    scenarios.append(('Тихий номер с видом на закат недалеко от моря',[example(h,[metric(h,'W01','distance_m',' м'),metric(h,'N02','distance_m',' м'),metric(h,'N04','count',digits=0),'V01/V05/S01/N05 неизвестны'],'Привязать категорию к корпусу, стороне, этажу и окнам; запросить измерение ночного звука. Расстояние до дороги и число нанесённых баров не отвечают за тишину.','W01')for h in ids]))
    ids=['11313836',pick('moscow','F01','distance_m'),pick('spb','F01','distance_m')]
    scenarios.append(('Семья',[example(h,[metric(h,'F01','distance_m',' м'),metric(h,'W02','distance_m',' м'),metric(h,'E03',unit='%')],'Подтвердить доступ/возрастные правила площадки, кроватку F03 и конфигурацию/гарантию кроватей M01; F02 требует именно детского неотложного приёма.','F01')for h in ids]))
    ids=['10012084',pick('kazan','P04','distance_m'),pick('minsk','P04','distance_m')]
    scenarios.append(('Питомец',[example(h,[metric(h,'P03','distance_m',' м'),metric(h,'P04','distance_m',' м')],'Уточнить животное, массу, количество и категорию P01, полную доплату/депозит P02. Для P03 нужны обозначенная площадка, вход, доступ и правила; обычная зелень не заменяет разрешение.','P04')for h in ids]))
    ids=['10012084',pick('kaliningrad','L01','distance_m'),pick('dubai','L03','distance_m')]
    scenarios.append(('Ночной приезд, суббота 02:00',[example(h,[metric(h,'L01','distance_m',' м'),metric(h,'L03','distance_m',' м'),'L02/T01/T02/T03: проверять отдельный полный контракт'],'Подтвердить ключ/самозаезд L04 на нужную местную дату. Часы кухни не следуют из часов бара, а близкая остановка не означает ночной рейс.','L01')for h in ids]))
    ids=['10575679',pick('moscow','N04','count',largest=True),pick('istanbul','N02','distance_m')]
    scenarios.append(('Удалённая работа',[example(h,[metric(h,'E03',unit='%'),metric(h,'C01','mean',' °C',month=7),metric(h,'N04','count',digits=0)],'Запросить тесты R01/R02 у стола утром/вечером, размеры/розетку R03, управление охлаждением S04 и размещение без стены лифтовой шахты N06. Тег интернета не даёт Мбит/с.','E03')for h in ids]))
    ids=['9986868',pick('spb','W05','known_step_sections',largest=True),pick('kazan','W05','known_step_sections',largest=True)]
    scenarios.append(('Маломобильный гость',[example(h,[metric(h,'W05','known_step_sections',digits=0),metric(h,'W04','dsm_positive_dz_estimate_m',' м'),'A01 неизвестно'],'Нужен непрерывный обследованный путь под ограничения гостя: вход, бордюры, ширины, покрытия, уклон и рабочие лифты. Ноль нанесённых лестниц и DSM-оценка не подтверждают доступность.','W05')for h in ids]))
    ids=['13736470',pick('istanbul','B02','mean_C',month=7),pick('dubai','B02','mean_C',month=7)]
    scenarios.append(('Пляж и сезон',[example(h,[metric(h,'B02','mean_C',' °C',month=7),metric(h,'B02','mean_C',' °C',month=11),metric(h,'W01','distance_m',' м')],'Морская модель — контекст, не разрешение купаться. Уточнить B01/B04/B05 и реальный вход; B03/C04/E01 пересчитываются в доступном прогнозном горизонте.','B02',7)for h in ids]))
    scenario_text='\n\n'.join('### '+title+'\n\n'+table(['Реальный объект','Значения / статусы','Следующий вопрос / граница','Доказательство / карта'],rows)for title,rows in scenarios)

    runtime=table(['Город','V/S/H, с','Гео runner / core, с','B/C/E, с','ERA5 / marine, с','Файлы результатов, Б'],[[NAMES[r['city_id']],f"{float(r['facades_s']):.3f}",f"{float(r['geo_s']):.3f} / {float(r['geo_core_s']):.3f}",f"{float(r['environment_s']):.3f}",f"{float(r['era5_fetch_s']or 0):.3f} / {float(r['marine_fetch_s']or 0):.3f}",r['physical_result_bytes']]for r in cityscale])
    join_runtime=table(['Город','Затронуто отелей','Связей до → после','Исправление N02/N03, с'],[[NAMES[r['city_id']],len(r['all_affected_hotel_ids']),str(r['old_relations'])+' → '+str(r['final_relations']),f"{float(next(s['geo_building_join_repair_s']for s in cityscale if s['city_id']==r['city_id'])):.3f}"]for r in join_review['cities']if r['all_affected_hotel_ids']])
    groupnames=[('N','N'),('W_A_Q03','W/A/Q03'),('L_F_P_R04','L/F/P/R04'),('T_R05_Q_X','T/R05/Q01–02/X'),('facades_history_solar','V/S/H: общая обработка'),('C','C'),('E','E'),('B_and_other_gates','B и проверки')]
    groups={}
    for row in familyscale:
        for group,seconds in json.loads(row['measured_group_times_s']).items():
            groups[row['city_id'],group]=seconds
    grouptable=table(['Измеренная группа']+[NAMES[c]for c in CITIES],[[name]+[f'{groups[c,key]:.3f}'for c in CITIES]for key,name in groupnames])
    attempt_rows=[]
    for path in sorted((BASE/'qa/geo').glob('*attempt.json')):
        attempt=json.loads(path.read_text())
        elapsed=attempt.get('elapsed_seconds_at_termination_observed')
        shown=str(elapsed)+' с'if elapsed is not None else '≥'+str(attempt.get('elapsed_seconds_observed_lower_bound','не измерено'))+' с'
        if path.name=='moscow_initial_attempt.json':
            diagnosis='Профиль CPython: full-generation cyclic GC; наблюдались swap/footprint≈5,1 ГБ на 8 ГБ'
        elif path.name=='moscow_gc_disabled_attempt.json':
            diagnosis='Native-профиль: граф построен, блокер Rust OpeningHours.next_change; гипотеза memory-thrash как главной причины уточнена'
        elif path.name=='moscow_compact_pre_hours_attempt.json':
            diagnosis='Намеренно остановлен, чтобы убрать установленный необязательный next_change до ещё одного долгого прогона'
        else:
            diagnosis=attempt.get('established_profile_blocker',attempt.get('note',attempt.get('status','См. manifest')))
        attempt_name={'moscow_initial_attempt.json':'Москва: первая попытка','moscow_gc_disabled_attempt.json':'Москва: с отключённым GC','moscow_compact_pre_hours_attempt.json':'Москва: compact до исправления часов'}.get(path.name,attempt.get('attempt',path.stem))
        attempt_rows.append([attempt_name,shown,diagnosis,link('manifest','qa/geo/'+path.name)])
    attempt_table=table(['Прерванная попытка','Наблюдавшееся время','Подтверждённая причина / граница','Источник'],attempt_rows)
    network_table=table(['API','Основные HTTP / 200','Пробы HTTP / статусы','Всего HTTP','Оценка взвешенных успешных вызовов'],[[kind,f"{network[kind]['primary_HTTP_requests']} / {network[kind]['primary_HTTP_statuses'].get('200',0)}",str(network[kind]['manual_probe_HTTP_requests'])+' / '+json.dumps(network[kind]['manual_probe_statuses'],ensure_ascii=False),network[kind]['all_HTTP_requests_including_probes'],f"{network[kind]['conservative_weighted_successful_total_estimate']:.3f}"]for kind in ['ERA5','Marine']])
    cohort_manifest=jsonfile('cohort/manifest.json')
    no_room=sum(not h.get('has_room_snapshot')for h in hotels.values())
    known_features=len({r['feature_id']for r in coverage if int(r['computed_n'])+int(r['observed_n'])})
    est_features=len({r['feature_id']for r in coverage if int(r['estimate_n'])})
    report=f'''# Расчёт характеристик на реальных отелях

Итоговый вычислительный отчёт V9 · 24 октября 2026 года · 8 городов, каталог 80 характеристик / 17 семейств. Основание — [задание V9](<{ROOT/'handoff_chatgpt_v9_compute.md'}>) и [сверка с треком](<{ROOT/'docs/research/track_alignment.md'}>). Полная техническая методика и команды — {link('README V9','README.md')}.

[факт] **На {N} реальных объектах выполнены цепочки всех 54 самостоятельно получаемых характеристик — {pairs} пар «отель × характеристика».** В сумме {totals['computed']} пар имеют хотя бы один `computed`, {totals['observed']} — `observed`, {totals['estimate']} — только оценку, {totals['unknown']} ({100*totals['unknown']/pairs:.2f}%) — неизвестны, {totals['not_applicable']} — неприменимы к заданным параметрам. Расчёты или факты источника найдены хотя бы где-то у {known_features}/54 ID; оценки есть у {est_features}/54 ID. Счётчики описывают наличие статуса, а не полноту контракта. Паспорт относится конкретной сущности/дате/цели, а не всем номерам отеля.

{lead_table}

[факт] **Вид и солнце V01–V06/S01–S03 пока не доказаны ни в одном существующем окне.** Построено {fan['total_passports']} полных паспортов веера, но неподтверждённая высота окна и неполная сцена препятствий сохраняют `unknown`. Новые относительно карточки результаты — геометрия корпусов/дорог/ж/д, нанесённые ночные объекты, маршруты и их компоненты, растительность, исторический климат и морской модельный контекст. Оценки не выполняют обязательные условия.

[факт] Разметка Codex остаётся **сочинской**: {labels['case_count']} строки / {labels['distinct_scientific_contexts']} разных контекстов, 0 полных независимых gold и 0 человеческих проверок. Физическая precision/recall и ошибка маршрута в метрах не измерены. В двух целевых проверках компонента текущих часов TP=1, FP=1, precision=50%, recall неизвестен. Отдельно {numerical['passed']}/{numerical['checks']} проверок арифметики восьми городов прошли: это те же входы, другая реализация, не физическая точность.

[факт] Главные случаи для показа: Radisson с разными корпусами и неизвестной привязкой категории; «Архитектор» с ошибочными часами аптеки и исправленным маршрутом; реальные многогородские примеры в семи сценариях раздела 6. Этажность/год/морские названия сравниваются только после проверки области и метода. У текущих расчётов остаются открытые требования: Дагомыс, проверенные окна/входы, независимый gold и свежие полные расписания. Время запуска Docker ≤10 минут здесь не измерялось.

## 1. Выборка, параметры и допустимые источники

[факт] Когорта содержит фактические `masterId`, адреса, координаты, URL/JSON-pointer и хеш каждого сохранённого источника. Исходные V5 по 50 на город сохранены; в Сочи добавлены 5 полных кешированных страниц и 34 SERP-карточки. {no_room}/{N} SERP-дополнений не имеют полного снимка категорий. Дагомыс отсутствует в допустимом кеше: это не отсутствие отелей, а незакрытый пункт D28 при запрете новых запросов к Островку. Подзоны Сочи: центр25, Адлер30, Сириус12, Красная Поляна4, Лазаревское5, Лоо/Вардане6, Хоста/Мацеста6, Головинка1; правила приблизительного районирования раскрыты в манифесте. Батуми не добавлялся. {link('Когорта','cohort/hotels.jsonl')}, {link('манифест','cohort/manifest.json')}.

{table(['Город','N','Часовой пояс','Смещение двух суббот 02:00'],timezone_rows)}

[факт] Поездки — **14–17.11.2026** и **15–18.07.2027**. Ночные сервисы — 14.11.2026 и 17.07.2027 в 02:00 местного времени. Солнечный веер использует дни пребывания без дня выезда. Климат — июль/ноябрь 1991–2020; море — июль/ноябрь 2023–2025. Статические маршруты/часы, спроецированные на две даты, не являются двумя наблюдениями сезонной доступности.

[факт] Переиспользованы сохранённые PBF, OSM-срезы, WorldCover/GLO-30 и OTA-снимки. Для Сочи переизвлечён весь нужный bbox из прежнего PBF ЮФО. В остальных городах runner дополняет старый V4-срез чистыми узлами `barrier/access/foot/locked` из того же PBF: иначе запреты у узлов терялись. В manifest явно различаются SHA файла и составной SHA упорядоченного списка входов. Границы/дата карты не гарантируют полную инвентаризацию.

[факт] Источники проходят D14/D22: [OSM ODbL](https://www.openstreetmap.org/copyright), [WorldCover CC BY 4.0](https://esa-worldcover.org/en/data-access), [Open-Meteo Historical](https://open-meteo.com/en/docs/historical-weather-api) и [Marine](https://open-meteo.com/en/docs/marine-weather-api) с согласованным некоммерческим доступом; production — платный план/свой сервер. GloBFP с условными upstream-правами не пропущен, GLO-30 не назван DTM. Запрещённые заданием поставщики не использованы. Новых HTTP к Островку — 0. Приведённые внешние фото/страницы — исследовательские prooflinks, права на тиражирование фотографий не заявлены.

## 2. Матрица 54×8, рецепты и паспорта

[факт] В каждой ячейке — **Р·О·?·НП**, числители четырёх статусов над N из заголовка: Р = `computed+observed`, О = `estimate`, ? = `unknown`, НП = `not_applicable`. Например, `10·20·19·1` при N=50 означает 10/50, 20/50, 19/50, 1/50. Один MID учитывается один раз по лучшему допустимому статусу **хотя бы одного** указанного корпуса/параметра. Это не покрытие всех зданий/окон/категорий; строгая колонка `all_issued_rows_resolved_n` сохранена отдельно. НП у внутренних городов относится только к заданному морскому контексту, а не утверждает, что в городе нет речных/озёрных пляжей. {link('Полный CSV','integrated/all8_coverage.csv')}.

{matrix}

[факт] **Цепочка рецептов.** Все 54 ID определены первым рецептом А/Б канонического каталога; остальные 26 В остаются анкетой. В паспорте записан каждый отказ: права, поле, покрытие, качество или другой смысл. Валидное `false` останавливает цепочку; два пригодных противоречащих источника образуют конфликт. `unknown` не заменён «нет». У каждого факта есть субъект, параметры, значение/единица, статус, источник/версия/хеш, рецепт, уверенность/причина, дата расчёта, известные даты источника и реальная валидность. Дата правки карты не объявляется физическим осмотром, `valid_to=null` не означает вечную истинность. {link('Интегрированные паспорта','integrated/all8_facts.jsonl')}.

[факт] **V/S/H.** Полный адрес и геометрическая согласованность сопоставляют объект с контуром; ближайший контур без достаточной идентичности не принимается. Корпус, литера, дробь сохраняются. Это исследовательские связи, не полная натурная перепись корпусов. H03 — надземный счёт источника OSM, включая первый надземный уровень, без подвала/крыши; не номер этажа категории. `start_date` без события завершения не закрывает H01, а H02 требует актуальный официальный статус. Веер сегмент фасада × ординальный этаж хранит все отказы V01–V06/S01–S03. NREL SPA и сценарная LOS-геометрия дают компоненты, но без проверенного окна/вертикального нуля/DTM/препятствий не доказывают вид или часы солнца. Категории к вееру не привязаны; фраза «море с пятого этажа» пока недоказуема.

[факт] Итоговая строгая проверка адресов дала {facade_review['totals']['relation_rows']} связей у {facade_review['totals']['matched_hotels']} отелей: 139 прежних − 4 отклонённые + 41 новая подтверждённая по принятому правилу. Это связи отель→корпус; различных OSM-контуров {facade_review['final_distinct_osm_building_ids']}. В основном экспорте H03 известен у {facade_review['totals']['main_H03_observed']} отелей. Удалены ложные родительские дома 1 вместо 1Б («Парадиз», Сочи), 2 вместо 2А («Мечта», Сочи), 13 вместо 13А («Альбертина», Калининград); у отеля «Москва» в Калининграде корпус 2 нельзя заменить родителем без корпуса. Дополнительный аудит допустимых кешированных кандидатов восстановил связи с полными адресами, включая транслитерацию. N02/N03 пересчитаны у {join_review['affected_hotels']} отелей пяти городов; наборы известных корпусных субъектов совпали с окончательными связями для всех 439 объектов. Остальные 27 геохарактеристик, все маршруты и восемь файлов среды побайтно прежние. Снимки до исправления сохранены. Это исправление идентичности и регрессия на тех же данных, не измерение физической точности. {link('Фасадный аудит','qa/facades/final8_review.json')}, {link('полнота зависимого пересчёта','qa/geo/all8_building_axis_join_coverage.json')}.

{matches}

[факт] **N.** N02 — расстояние конкретного принятого корпуса до оси дорог `primary/primary_link/secondary/secondary_link/trunk/trunk_link`; N03 — до нанесённой активной ж/д. N04 — количество учтённых типов в радиусе300м с полнотой слоя. Ни один не является Lnight или тишиной в номере; N01 остаётся неизвестным без пригодной карты/калиброванной модели. Ноль нанесённых заведений не означает отсутствие всех заведений.

[факт] **W/A/Q03/F/P/L/R.** Граф соединяется общими OSM-узлами, не любым пересечением линий; запреты проверяются и на рёбрах, и на узлах. Входы/снапы/сезонные ворота не обследованы, поэтому длины маршрутов — оценки. W05 считает известные лестничные участки, W06 разделяет нанесённые переходы и геометрических кандидатов, Q03 возвращает доли освещённого/неосвещённого/неизвестного; нулевая длина графа даёт неизвестный Q03. Их субъект — конкретный маршрут. W04 отклоняет канонические DTM/обследование и выдаёт отдельную D18 DSM-оценку `W04-D18-DSM`, без выдуманного интервала ошибки. Ноль лестниц и низкий DSM-подъём не закрывают A01.

[факт] **Знаменатель Q03 — длина нанесённой графовой полилинии**, без непроверенных коннекторов отеля/цели. У {len(q03_scope['cases'])} строк «100% нанесённого графа освещено» имеются ненулевые исключённые коннекторы. Это {len({(r['city'],r['hotel_id'],r['route_id'])for r in q03_scope['cases']})} различных маршрутов, повторённых для двух дат. Нельзя говорить «весь путь освещён» или использовать Q03 как проверку безопасности; работоспособность фонарей и коннекторы не обследованы. {link('Аудит знаменателя','qa/quality/Q03_graph_denominator_scope.json')}.

[факт] P03 R2 допускает явно обозначенную OSM-площадку для собак при подтверждении входа, доступа, правил животного/поводка/сезона; обязательный официальный реестр сверх R2 не вводится. Тег хаски-центра/обычного парка недостаточен. L01/L03/P04 разбирают регулярные часы в местном времени, но будущие исключения/физическое открытие неизвестны. L02 требует часы кухни, F02 — детский неотложный приём, R04 — доступ внешнего посетителя. T01–T03 не следуют из остановки или старого GTFS: ноль активных записей кеша не доказывает отсутствие реального транспорта.

[факт] Для Дубая первоначальный parser отклонял код страны AE даже у простого24/7. Отдельный закреплённый адаптерv2 допускает `country=None` только для выражений без PH/SH, сохраняя `Asia/Dubai`; выражения с календарными праздниками не упрощаются. После проверки ожидаемых состояний274строки перешли `unknown→estimate`, прежний вариант сохранён. Это исправление семантики парсера, не подтверждение фактического открытия в будущем. {link('Независимая техническая рецензия','qa/quality/all8_review.json')}.

[факт] **B/C/E.** ERA5 C01–C03: нативная сетка0,25°, без высотной коррекции к отелю, 30 полных июлей/ноябрей1991–2020; средняя температура на 2 м, дни с осадками≥1мм, Tmax>30°C. Фиксированные нынешние местные смещения не воспроизводят исторический DST. E03 заново рассчитан от OTA-координаты: площадь классов10/20/30/40/90/95/100 в300м, знаменатель — классифицированная суша без воды80/nodata; это WorldCover2021, не текущая зелень/доступный парк/тень.

[факт] B02 — `thetao` MERCATOR GLO12 на глубине0,494025м, исходный шаг 6 часов по UTC, 2023–2025, `estimate`; не климатическая норма, градусник на пляже или прогноз. Семантика проверена pinned-исходниками импортера и CF NetCDF; API и nativeCF центры сдвинуты на половину ячейки, обе координаты сохранены. Единый `v9.2_marine_context` сначала берёт walk-selected морской полигон, иначе ближайший прошедший геометрический фильтр морской полигон по прямой только для модельного контекста. При резервном выборе маршрут/вход неизвестны; доступ, пригодность купаться и связь с отелем не утверждаются. Это не запасной рецепт W01.

[факт] Пространственная QA отклонила {wet['known_land_rejections']} исходных nativeCF-точек модели, попавших в наземные классы WorldCover: Стамбул1точка/19отелей и Дубай10точек/23отеля. Для тех же пляжных полигонов выбраны новые конечные модельные значения в классе80 «вода»; у Rumeli Kavağı новая nativeCF-ячейка примерно7,289км от пляжа. Все {quality['summary']['B02_estimate_rows']} финальных B02-строк имеют эту дополнительную проверку воды. Совпадение класса80 подтверждает только пространственный контроль, **не температуру, безопасность купания и не независимый gold**. Датировка берегов модели и WorldCover2021 может различаться. {link('Отклонённые точки и замены','qa/environment/coastal_wetcell_validation.json')}.

[вывод] У Калининграда выбранный сохранённый полигон `way/423147709` по координатам относится к Калининградскому/Вислинскому заливу. Это вывод Codex для проверки, не официальный эталон точного пляжа. [МЧС](https://39.mchs.gov.ru/glavnoe-upravlenie/harakteristika-subekta) описывает отделение залива Балтийской косой, [ИОРАН](https://atlantic.ocean.ru/index.php/interesnoe-v-nauke/item/277-vozmozhnost-peretoka-vod-v-sisteme-kaliningradskij-zaliv-reka-pregolya-kurshskij-zaliv) — прибрежные лагуны; эти первичные страницы дают региональный контекст, а не проверяют координату, доступ или thetao. Результат не следует подписывать температурой/путём к открытому курортному берегу Зеленоградска. Фильтр coastline≤25м сам не различает открытую акваторию и лагуну. {link('Разбор области водоёма','validation/kaliningrad_waterbody_scope.json')}.

[факт] B03/C04/E01 не дают прогноз на поездки за горизонтом модели; исторические средние не подставляются. B01/B04/B05 требуют материала конкретного участка, уполномоченного статуса и лабораторного протокола. CAMS не заменяет станционное измерение E02; недатированный JPEG не заменяет corrected BlackMarble E04. Для Q01/Q02/R05/X01–X03 не приняты городской индекс, выборочные новости, вышка OSM или недатированная стройка. Полные отказы и параметры — в фактах; методика — {link('среда','environment/README.md')}, {link('фасады','facades/README.md')}.

## 3. Размеченная выборка и измеренные границы качества

[факт] Разметка Codex: {labels['case_count']} строки, {labels['distinct_scientific_contexts']} разных контекстов, {labels['duplicate_context_rows']} повтора C01; **только Сочи**. Все обязательные ID V9 присутствуют, охвачено13семейств; A/R/X/M не получили независимых полных эталонов. Человеческих проверок0, полных независимых gold0. Изначальные 8 фасадных,10 гео,8 среды и8 станционных строк не превращаются в независимые30наблюдений точности. Для ручной проверки подготовлены {link('единый CSV всех случаев','validation/cases.csv')} и {link('сводка разметки','validation/cases_summary.json')}; исходные панели: {link('фасады','facades/reference_cases.csv')}, {link('гео','qa/geo/reference_cases.csv')}, {link('среда','qa/environment/audit_cases.csv')}, {link('станции','validation/station_comparison.csv')}.

{table(['Проверка','Результат','Что это измеряет'],[
['Полный физический контракт V/S/маршрутов/моря/растительности','Precision/recall/ошибка маршрута в метрах не измерены','Нет независимого эталона той же сущности, даты, метода и полной области'],
['Компонент текущих часов L01/P04','TP1,FP1; precision50%; recall неизвестен','Два намеренно выбранных разных оператора; не репрезентативная выборка будущих маршрутов'],
['C01:4 cell-month / одна станция / два месяца',f"Среднее абсолютное различие {station['C01']['mean_absolute_station_cell_difference']:.3f}°C, максимум {station['C01']['maximum_absolute_station_cell_difference']:.3f}°C",'Станция против сетки: репрезентативность/опора/определение температуры; не чистая ошибка кода'],
['C02:4 cell-month / та же станция',f"Среднее абсолютное различие {station['C02']['mean_absolute_station_cell_difference']:.3f} дня, максимум {station['C02']['maximum_absolute_station_cell_difference']:.3f}",'Опора, суточные границы и модель; не четыре независимые станции'],
['Арифметика восьми городов',f"{numerical['passed']}/{numerical['checks']} прошли; максимум N02 {numerical['maximum_N02_difference_m']:.6f}м",'Те же геометрии OSM/суточные API, независимые проекция/reducer; не physical distance error'],
['Полные паспорта веера',f"{fan['total_passports']} проверены",'Структурная QA, не подтверждение существующего окна'],
['Самостоятельные неизвестные пары',f"{totals['unknown']}/{pairs} = {100*totals['unknown']/pairs:.2f}%",'Хотя бы один субъект/набор параметров; не гарантии всех номеров'],
['Анкета',f"26/80=32,5%; {26*N}/{26*N} требуют ответа, prefill у {pref_pairs}",'Базовая доля контрактов отельера, без автоматического прибавления всех самостоятельных unknown'],
])}

[факт] [Норма станции Сочи WMO37099 1991–2020](https://www.nodc.noaa.gov/archive/arc0216/0253808/4.4/data/0-data/Region-6-WMO-Normals-9120/RussianFederation/CSV/Sotchi_37099.csv) относится к142м и одной станции; сопоставление с ячейками не делает норму температурой каждого отеля. Четыре повторных контекста C01 дедуплицированы в счётчике30. B02 metadata/код подтверждают смысл/глубину, а не независимую температуру; WorldCover не имеет независимой датированной полной разметки300м. У расстояний `error_m` не заполнено без обследованного независимого пути.

[факт] Ошибка текущего компонента часов: [«Столетов», Конституции24А](https://stoletov.ru/pharmacies/doktor-stoletov-sochi-konstitutsii-sssr-24a-sochi-ulitsa-konstitutsii-sssr-24a-2017/) сообщает08–21 против OSM24/7. Кандидат заблокирован; у «Архитектора» маршрут к другой аптеке417,51м вместо341,05м. [Ветеринарный оператор Тепличная18А](https://www.centervet-sochi.com/) подтверждает24h только текущего компонента. Повтор исправленного случая не является held-out тестом. Исправления речного пляжа/морской цели,37 первоначальных route ID через запрещённые узлы и Q03 на нулевом пути — регрессии реализации, не рост измеренной физической точности.

[факт] Финальная независимая техническая рецензия прошла {quality['summary']['result_gates_passed']}/{quality['summary']['result_gates']} проверок контрактов/источников, отдельно {quality['summary']['strict_address_repair_gates_passed']}/{quality['summary']['strict_address_repair_gates']} проверок строгого адресного исправления и {quality['summary']['exact_saved_route_traces']} сохранённых точных трасс без контакта с известными запрещёнными узлами. На этапе Сочи v9.3 полный повторный выбор89×29 изменил только2сезонных F01-паспорта MID10595288:583,884→501,764м; на том этапе4557остальных геопаспортов были побайтно прежними. Последующий адресный аудит отдельно изменил N02/N03, как описано выше; W01/морские цели неизменны. Все652сочинские трассы совпали с исправленным полным расчётом по геометрии/узлам/рёбрам/длине. Статус рецензии прямо сохраняет «physical accuracy unmeasured». {link('Итог рецензии','qa/quality/all8_review.json')}, {link('исправление узла F01','qa/geo/sochi_F01_node_repair_report.json')}.

[вывод] Воспроизводимость и покрытие измерены отдельно от пока незакрытой независимой физической точности. Для значимой precision/recall нужны полные положительные/отрицательные эталоны, для маршрутов — один и тот же обследованный вход/путь; для V/S — окно, сектор, дата и препятствия. Нужен новый ранее не использованный контрольный набор после исправлений. Неизвестность может закрываться открытыми данными/обследованием/отельером; автоматически считать каждый unknown отправленным вопросом нельзя. Отправленных вопросов0.

## 4. Противоречия и несопоставимые заявления Островка

[факт] Просмотрены сохранённые сравнения всех восьми городов: {len(comparisons)} строк; по городам: {', '.join(NAMES[c]+' '+str(comp_by_city[c])for c in CITIES)}. Полный исходный scope, значения, указатели и снимки сведены в {link('CSV всех сравнений','qa/writer/ota_comparisons_all8.csv')}; исходная классификация сохранена, отдельно проверено наличие числового операнда. Из 40 строк H03 у {numeric_counts['H03']['missing_independent_numeric_operand']} нет независимого числа этажей, у {numeric_counts['H03']['raw_numeric_difference_or_multiple_scope']} сырые числа различаются или имеют несколько корпусных областей, у {numeric_counts['H03']['raw_numeric_agreement']} совпали. Две исходные метки различия области при `levels=null` включены в неизвестные, а не в числовые расхождения. Из 43 строк H01 у {numeric_counts['H01']['missing_independent_numeric_operand']} нет независимого события/года завершения, у {numeric_counts['H01']['raw_numeric_difference_or_multiple_scope']} сырые годы различаются, у {numeric_counts['H01']['raw_numeric_agreement']} совпали; событие `start_date` всё равно не подтверждено как завершение. Ещё 16 строк V01 сравнивают заявление категории с непривязанным окном. Это диагностические строки, не доля ошибок всего каталога и не доказанные противоречия Островку.

{comp_table}

[факт] Число этажей карточки может описывать другой корпус/конвенцию; H03 OSM — конкретный надземный счёт. `yearBuilt`/`start_date` могут обозначать открытие, реконструкцию или существование, а H01 требует завершения строительства корпуса. Название морской категории — заявление; `unknown` веера его не опровергает. Различия этих типов пока нельзя разрешать победой источника по уверенности.

[факт] У Radisson `floorsNumber8` против четырёх корпусов9/8/8/7 — не доказанная ложь. Островок POI «Арфа»362м и наш W01≈155,2м относятся разным целям/неуказанному методу: это не измеренная ошибка OTA. У Фрегата POI13м и маршрут≈248,5м также не одна величина. Кто прав, можно установить только после общей сущности/входа/метода/даты. Единственный здесь опровергнутый текущий компонент часов относится OSM/оператору, не карточке OTA.

## 5. 26 характеристик отельера

[факт] Таблица считает {N} объектов по наличию хотя бы одного опубликованного атома Островка V5/V7. Всего {pref_pairs}/{26*N} анкетных пар имеют частичное предзаполнение, полных подтверждённых контрактов0, внешних ответов0. Типизированные room-group labels имеют actualMID, JSON-pointer, хеш и дату кеша. Подпись кровати M01 не гарантирует назначение; холодильник/посуда M03 не доказывают приватную кухню; тег связи R01 не является измерением Мбит/с; стол/сейф не даёт размеров. Вычислители добавляют причину/субъект вопроса, не измерение комнаты. {link('Предзаполнение с цитатами','hotelier/prefill.jsonl')}, {link('все вопросы CSV','hotelier/questions_26.csv')}.

{questiontable}

[факт] Из расчёта возникают адресные дополнительные вопросы: категория `rgHash`→корпус/фасад/ординальный этаж/окна; реальный вход и сезонные ворота; непрерывный обследованный путь под ограничения гостя; правила собачьей площадки; конкретные часы кухни; события/перекрытия на нужные даты; документ завершения строительства. Ответ сохраняется свободным текстом, затем раскладывается в поля с единицами, областью, сроком и проверками. «Море рядом», «хороший Wi-Fi», «есть лифт» недостаточны для V01/R01/A03. Отправок/имитации ответов в этой работе не было.

## 6. Семь сценариев: по три реальных проверяемых случая

[факт] Это выборка материалов для объяснения и ручного аудита, **не рейтинг полностью подходящих гостиниц**. Для разнообразия выбраны разные города и объекты из фактической когорты; критерий выбора — доступное значение нужного калькулятора или показательный unknown. Каждый пример сохраняет `fact_id`, первоисточник и карту. Повтор отеля в другом сценарии допустим. Неприменимость морского контекста не выдаётся за отсутствие любых пляжей, а неизвестная услуга не становится отрицанием.

{scenario_text}

[вывод] В поиске обязательное условие проходит при доказанном «да» полного контракта; оценка остаётся мягким условием D18. Неизвестность показывает конкретный пробел и формирует вопрос. Новая характеристика из текста должна сначала получить смысл, область, единицу, рецепты и проверки: LLM не производит значение. Кнопка бронирования — заключительная ссылка с условиями; центр результата здесь — новые воспроизводимые характеристики и их достоверность.

## 7. Локальное время, размер, стоимость закрытия пробелов и масштаб

{runtime}

[факт] Это успешные измеренные процессы на текущей машине; `geo runner` включает захват точных путей/дополнительную сериализацию, `core` — внутренний расчёт. ERA5 — измеренная фаза сбора, marine — накопленные измеренные морские фазы; эти столбцы не являются временем одного запроса или полным сроком исследования. Подготовка узлов ограничений и первичное извлечение отдельно в {link('city_metrics.csv','scale/city_metrics.csv')}. Сумма успешных процессов: {scale['process_elapsed_sum_s']:.3f}с; она не включает всё исследование, начальные попытки, сетевые стадии и человеческое ревью.

[факт] Строка геоветви Сочи сохраняет исторический полный прогон до v9.3. Исправление запрещённого узла F01 измерялось отдельно: дельта {repair_phase['corrected_graph_delta_calculation_seconds']:.3f}с, публикация {repair_phase['repair_publication_seconds']:.3f}с, полная контрольная перевыборка/аудит {repair['full_corrected_reselection_validation']['total_readonly_validation_seconds']:.3f}с, в том числе сама перевыборка {repair['full_corrected_reselection_validation']['full_corrected_reselection_seconds']:.3f}с. Последующее исправление N02/N03 по адресам также измерено отдельно:

{join_runtime}

[факт] Эти фазы исправления и контроля не включены в сумму {scale['process_elapsed_sum_s']:.3f}с и не объявлены новым полным производственным прогоном города. Времена фасадной ветви в первой таблице относятся к окончательным выходам; геоветвь сохраняет исторический полный расчёт с явными последующими дельтами.

{grouptable}

[факт] Совместные группы времени не распределялись вымышленно по семействам и не суммируются повторно: V/S/H, W/A/Q03, L/F/P/R04, T/R05/Q/X. У M все признаки анкетные; собственного вычислительного времени нет. Число строк и логические JSONL-байты по каждому городу×семейству — {link('family_metrics.csv','scale/family_metrics.csv')}. Небольшое время проверки недоступного рецепта не является производительностью работающего транспортного/акустического калькулятора.

{attempt_table}

[факт] Успешный runner использует `gc.disable()` при работающем подсчёте ссылок, более компактное представление графа и кеш точечного разбора часов. Версия принятого `geo.py` сохраняется; overrides runner/backend явно версионированы и входят в provenance. Отсутствие изменений coreSHA не означает отсутствие дополнений runner. Представление проверено19точными fixture-контролями; точечные часы —192проверками8городов×2дат×12контекстов с PH/SH/overnight/split/parser/kitchen против прежнего разбора. Различных часовых поясов5; отдельные проверки AE2 не смешаны с этими192. Точное `is_open/is_closed` сохраняется; исключён только необязательный дорогой поиск будущей смены часов `next_change`, он `null` с причиной. Эти проверки не являются полевой точностью. Тяжёлые фасадные/графовые процессы чередовались на ноутбуке8ГБ. Прерванные попытки отделены от успешного времени; второй попытке не приписывается недоказанная memory-thrash после того, как профиль указал другой блокер. {link('Compact runner','scripts/run_geo_v9_other_city_compact.py')}; для Дубая фактически применён {link('compact_v2 / AE2','scripts/run_geo_v9_other_city_compact_v2.py')}.

{network_table}

[факт] Фактических ERA5/Marine API HTTP — {sum(network[k]['all_HTTP_requests_including_probes']for k in ['ERA5','Marine'])}: основные {sum(network[k]['primary_HTTP_requests']for k in ['ERA5','Marine'])} и7ручных проб;536ответов200 и2ошибки400 при исправлении параметров. Транспортных retry и429в журнале нет. Взвешенные вызовы — консервативная оценка нагрузки, **не счёт или фактически списанная квота**. Одна успешная морская проба была семантически пустой; HTTP200 сам не означает годный ряд. Эти числа охватывают только инструментированные API, не весь web/docs/код/CFmetadata HTTP и трафик установки зависимостей. {link('Полный журнал нагрузки/кеша','qa/environment/all8_network_metrics.json')}.

[факт] Кеш основных ответов — {network['cache']['retained_primary_response_payload_files']} файлов, {network['cache']['retained_primary_response_payload_bytes']}Б decoded payload + {network['cache']['retained_primary_response_meta_bytes']}Б metadata; это не wire-байты. Существующие 9 тайлов WorldCover ({network['cache']['reused_WorldCover_input_bytes']}Б) переиспользованы, не скачаны повторно. Весь каталог среды без installed runtime — {network['cache']['environment_source_dir_bytes_excluding_runtime']}Б; с этими уникальными растрами — {network['cache']['total_environment_source_and_referenced_raster_bytes']}Б без повторного сложения вложенного raw. Ранние development-прогоны не полностью инструментированы; известные сохранённые промежуточные запуски перечислены в network metrics, не выданы за весь срок исследования.

[факт] Физические файлы результатов восьми городов — {scale['physical_result_bytes']}Б; полных паспортов веера — {scale['fan_passports']}. Сюда не входят исходные PBF/растр/API-кеш и дублирующий интегрированный вид. Интегрированные основные/корпусные факты — {summary['fact_rows']} строк, {(BASE/'integrated/all8_facts.jsonl').stat().st_size}Б; сжатый веер учитывается один раз в городских физических файлах. Большой JSONL с повторными источниками — формат автономного аудита, в БД источники/зависимости нормализуются. {link('Манифест масштаба','scale/summary.json')}.

[вывод] D6: пакет города строится заранее — разрешённые входы/версии → вычисления → QA → компактные факты/геометрия/источники. При старте жюри нужны проверка хешей, загрузка готовой базы и API на порту 8080 с `/health`, без скачивания19ГБ, API-походов и построения полного графа. **Docker compose up --build≤10мин здесь не измерялся**: V9 создал исследовательские модули, не приложение. В core нужны адаптеры string ID→справочники bigint, `algorithm_version`→`algo_version`, `calculated_at`→`computed_at`, отдельные confidence/причина, пары фасад/этаж и нормализованные source/fact_dependency. Неизвестные и отказанные рецепты сохраняются.

{table(['Пробел','Что требуется','Измеренная цена / срок'],[
['Дагомыс / недостающие категории','Разрешённые полные исходники с MID/адресом/координатами/категориями','Не измерены; новых запросов к Островку0'],
['Вид и солнце','Окна, вертикальный ноль, DTM, препятствия, категория→размещение и независимые фото/осмотр','Стоимость обследования не измерена'],
['Маршруты / маломобильность','Реальные входы, ворота, покрытия, ширины, бордюры, уклон, ступени и работающие лифты','Стоимость/человеко-часы не измерены'],
['Часы / транспорт / события','Свежие полные расписания/исключения/даты/геометрии под своими лицензиями','Цена полноты и договора не измерена'],
['Шум / вода / станционный воздух / безопасность','Собственные допустимые карты/замеры/протоколы/числитель и знаменатель','Тарифы и стоимость полевых работ не измерены'],
['Будущие прогнозы','Пересчёт в горизонте с модельным run/lead и обновление статуса пляжа','Production поD22: платный план/свой сервер, счёт не получен'],
['Независимая точность','Ручная проверка 30 контекстов, gold ключевых контрактов и новый held-out набор','0 полных gold сейчас; денежная сумма не выдумана'],
])}

### Решения для команды

1. [вывод] Переносить в ядро факты вместе с паспортами, областью/параметрами, зависимостями и цепочкой отказов; 54 попытки не называть54готовыми характеристиками.
2. [вывод] Сохранить8городов и80/17; готовые городские пакеты отделить от пользовательского поиска и загрузки при старте.
3. [вывод] Не допускать V/S к обязательным условиям до проверенных окон/высот/препятствий и размещения категорий; веер остаётся исследовательской основой.
4. [вывод] Маршруты/будущие часы/DSM/короткий морской ряд оставлять оценками; A01 требует непрерывного обследования, B02 не подтверждает доступ к пляжу.
5. [вывод] Закрыть независимый gold и человеческую перепроверку до заявлений физической precision/recall; арифметика и регрессии остаются отдельными метриками.
6. [вывод] Разбирать конфликты по общей сущности/методу/дате и задавать адресные вопросы с предзаполнением V5/V7, не решать голосованием уверенности.
7. [вывод] Измерить реальную контейнерную сборку/инициализацию≤10мин с подготовленными данными; не включать сетевой сбор/тяжёлые графы в запуск жюри и не скрывать неудачные попытки.
'''
    report=polish_prose(report)
    OUT.write_text(report)
    freeze_paths=['README.md','integrated/all8_facts.jsonl','integrated/all8_summary.json','integrated/all8_coverage.csv','cohort/hotels.jsonl','cohort/manifest.json','scale/summary.json','scale/city_metrics.csv','scale/family_metrics.csv','validation/numerical_all8_summary.json','validation/cases_summary.json','validation/cases.csv','validation/station_summary.json','hotelier/manifest.json','hotelier/prefill.jsonl','hotelier/questions_26.csv','qa/root/all8_fan_audit.json','qa/quality/all8_review.json','qa/environment/all8_network_metrics.json','qa/environment/coastal_wetcell_validation.json','validation/kaliningrad_waterbody_scope.json','qa/quality/Q03_graph_denominator_scope.json','qa/geo/sochi_F01_node_repair_report.json','qa/geo/sochi_full_corrected_reselect_audit.json','scripts/write_all8_report.py','scripts/write_sochi_report.py']
    freeze_paths.extend(['qa/geo/opening_point_cache_equivalence.json','qa/geo/opening_point_cache_v2_checks.json','qa/facades/final8_review.json','qa/geo/all8_building_axis_join_coverage.json','qa/geo/building_axis_repair_manifest.json','scripts/run_geo_v9_other_city_compact.py','scripts/run_geo_v9_other_city_compact_v2.py','qa/writer/ota_comparisons_all8.csv'])
    freeze_paths.extend(['qa/geo/sochi_N02N03_strict_join_repair_report.json']+[f'qa/geo/{c}_N02N03_join_diff_repair_report.json'for c in ['moscow','spb','kazan','kaliningrad']])
    for city in CITIES:
        freeze_paths.extend([f'results/facades/{city}_summary.json',f'results/geo/{city}_run.json',f'results/environment/{city}_summary.json',f'results/facades/{city}_ota_comparisons.jsonl'])
    freeze_paths.extend(str(p.relative_to(BASE))for p in(BASE/'qa/geo').glob('*attempt.json'))
    freeze_paths.extend(['qa/writer/check_all8_report.py','qa/writer/check_sochi_report.py'])
    freeze_files={BASE/p for p in freeze_paths}
    freeze_files.update(Path(re.sub(r':\d+$','',p))for p in re.findall(r'\]\(<([^>]+)>\)',report)if p.startswith('/'))
    dump(QA/'all8_source_freeze.json',{'at':datetime.now(timezone.utc).isoformat(),'purpose':'Exact writer inputs and linked local evidence; upstream source datasets remain pinned through the accepted calculator manifests.','report_sha256':digest(OUT),'files':[{'path':str(p),'sha256':digest(p),'bytes':p.stat().st_size}for p in sorted(freeze_files)if p.is_file()]})
    dump(QA/'all8_report_qa.json',{'at':datetime.now(timezone.utc).isoformat(),'report':str(OUT),'sha256':digest(OUT),'bytes':OUT.stat().st_size,'status':'AUTHOR_REVIEW_PENDING','input_snapshot_frozen':True,'input_summary_sha256':digest(BASE/'integrated/all8_summary.json'),'coverage_rows':len(coverage),'hotelier_rows':len(questions),'scenario_count':len(scenarios),'examples_per_scenario':[len(r)for _,r in scenarios],'example_fact_ids':used_citations,'comparison_rows':len(comparisons),'root_review':'required','input_files':[{'path':str(BASE/p),'sha256':digest(BASE/p)}for p in ['integrated/all8_summary.json','integrated/all8_coverage.csv','scale/summary.json','validation/numerical_all8_summary.json','validation/cases_summary.json','hotelier/manifest.json','qa/root/all8_fan_audit.json','qa/quality/all8_review.json','qa/environment/all8_network_metrics.json','qa/environment/coastal_wetcell_validation.json','validation/kaliningrad_waterbody_scope.json','qa/quality/Q03_graph_denominator_scope.json','qa/geo/sochi_F01_node_repair_report.json']]})
    print(json.dumps({'file':str(OUT),'sha256':digest(OUT),'bytes':OUT.stat().st_size,'status':'AUTHOR_REVIEW_PENDING'},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen',action='store_true',help='Use only complete root-accepted eight-city inputs.')
    args=parser.parse_args()
    if args.frozen:
        render_final()
    else:
        progress_draft()


if __name__=='__main__':
    main()
