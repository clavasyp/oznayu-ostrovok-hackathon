#!/usr/bin/env python3
"""Вежливый сбор публичных страниц отелей ostrovok.ru: Москва и Большой Сочи (D24, D36).

Основа — collector.py из v5 (research_notes/Данные Островка и набор городов/site_data/scripts).
Отличия: свой каталог данных, индекс уже собранного в памяти (докачка без повторов),
редирект на выдачу (устаревший URL) не открываем, сводка по каждой странице.

Правила: один поток, не чаще 1 запроса в 1,1 с; без входа в аккаунт, прокси и обхода защиты;
при 401/403 или странице проверки — остановка с файлом STOP.json.

Режимы:
  collect.py <data_dir> sitemap            — список URL Москвы и Большого Сочи из sitemap
  collect.py <data_dir> pages [--limit N]  — страницы отелей по списку (с докачкой)
  collect.py <data_dir> status             — сколько собрано

Завершение: manifests/DONE.json (все адреса из urls.csv пройдены) или PARTIAL.json (прогон закончился,
но не всё пройдено). Отсутствие RUNNING.lock само по себе не значит, что сбор завершён.
"""
import argparse, csv, datetime as dt, gzip, hashlib, json, re, signal, sys, time
from email.utils import parsedate_to_datetime
from pathlib import Path
import xml.etree.ElementTree as ET
import requests

UA = 'OznayuHackathonCatalog/1.0 (Ostrovok hackathon team; organizers permission; max 1 request/second)'
MIN_GAP = 1.1
SITEMAP_INDEX = 'https://ostrovok.ru/sitemap.xml'
# Токены города в URL /hotel/<страна>/<город>/mid<id>/<slug>/ (v5: all_url_city_tokens.json).
CITY_TOKENS = {
    'moscow': {'moscow', 'moscow_skolkovo_neighborhood'},
    'sochi': {'sochi', 'adler', 'sirius', 'krasnaya_polyana', 'krasnopolyanskoe', 'lazarevskoe', 'loo',
              'dagomys', 'khosta', 'esto-sadok', 'vardane', 'golovinka', 'veseloe', 'polyana',
              'big_sochi_(sochi_and_vicinity)_kudepsta_neighborhood'},
}
V5_SITEMAP = Path('/Users/turgov/Хакатон/research_notes/Данные Островка и набор городов/site_data/csv/sitemap_hotels.csv')
URL_RE = re.compile(r'^https://ostrovok\.ru/hotel/([^/]+)/([^/]+)/mid(\d+)/([^/?]+)/?$')
CHALLENGE = (b'<title>Captcha', b'<title>Just a moment...', b'cf-chl-bypass', b'smartcaptcha')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Client:
    def __init__(self, root: Path):
        self.root = root
        self.man = root / 'manifests'
        self.man.mkdir(parents=True, exist_ok=True)
        self.ledger = self.man / 'http.jsonl'
        self.stop = self.man / 'STOP.json'
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': UA, 'Accept-Language': 'ru-RU,ru;q=0.9'})
        self.last = 0.0

    def get(self, url, kind, read_timeout=80, fatal=True):
        if self.stop.exists():
            raise SystemExit('STOP.json есть — сбор остановлен: ' + self.stop.read_text()[:300])
        rec = None
        for attempt in range(4):
            wait = MIN_GAP - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            started = now()
            tic = time.monotonic()
            body = b''
            try:
                resp = self.session.get(url, timeout=(20, read_timeout), allow_redirects=False)
                body = resp.content
                rec = {'url': url, 'kind': kind, 'started_at': started, 'status': resp.status_code,
                       'elapsed_s': round(time.monotonic() - tic, 3), 'bytes': len(body),
                       'sha256': hashlib.sha256(body).hexdigest(), 'attempt': attempt + 1,
                       'location': resp.headers.get('Location'), 'retry_after': resp.headers.get('Retry-After')}
            except requests.RequestException as exc:
                rec = {'url': url, 'kind': kind, 'started_at': started, 'status': None,
                       'elapsed_s': round(time.monotonic() - tic, 3), 'error': str(exc), 'attempt': attempt + 1}
            with self.ledger.open('a') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            status = rec.get('status')
            if status in (401, 403):
                self.stop.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
                raise SystemExit(f'{status} на {url} — остановлено')
            if status == 200 and any(m in body[:20000] for m in CHALLENGE):
                self.stop.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
                raise SystemExit(f'Страница проверки на {url} — остановлено')
            if status == 429:
                ra = rec.get('retry_after') or ''
                try:
                    delay = float(ra) if ra.isdigit() else max(0, (parsedate_to_datetime(ra) - dt.datetime.now(dt.timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    delay = 60 * (attempt + 1)
                time.sleep(max(delay, 60))
                continue
            if status is None or status in (500, 502, 503, 504):
                time.sleep(10 * (attempt + 1))
                continue
            return body, rec
        if not fatal:
            return b'', rec
        self.stop.write_text(json.dumps(rec, ensure_ascii=False, indent=2))
        raise SystemExit(f'Устойчивая ошибка на {url} — остановлено')


def cmd_sitemap(c: Client, root: Path):
    body, rec = c.get(SITEMAP_INDEX, 'sitemap_index')
    parts = [e.text for e in ET.fromstring(body).iter() if e.tag.endswith('loc')]
    rows, seen, failed = [], set(), []

    def take(part):
        # Части sitemap по ~7,5 МБ и 50 тыс. URL отдаются медленно: длинный таймаут, сбой одной части не фатален.
        data, r = c.get(part, 'sitemap_part', read_timeout=300, fatal=False)
        if r.get('status') != 200 or not data:
            return False
        for node in ET.fromstring(data):
            loc = next((e.text for e in node if e.tag.endswith('loc')), None)
            m = URL_RE.match(loc or '')
            if not m:
                continue  # AMP-копии и прочее пропускаем
            country, token, mid, slug = m.groups()
            city = next((k for k, v in CITY_TOKENS.items() if token in v), None)
            if city and country == 'russia' and loc not in seen:
                seen.add(loc)
                rows.append({'city': city, 'url_token': token, 'mid_url': mid, 'slug': slug, 'url': loc, 'sitemap': part})
        return True

    for part in parts:
        if not take(part):
            failed.append(part)
    failed = [p for p in failed if not take(p)]  # вторая попытка в конце
    if failed and V5_SITEMAP.exists():
        # Пробел от не скачанных частей закрываем списком v5 от 23.10 (только базовые токены, которые v5 сохранял).
        for r in csv.DictReader(V5_SITEMAP.open()):
            if r['is_amp'] == '0' and r['city_key'] in CITY_TOKENS and r['country'] == 'russia' and r['url'] not in seen:
                seen.add(r['url'])
                rows.append({'city': r['city_key'], 'url_token': r['url_city'], 'mid_url': r['mid_url'], 'slug': r['slug'],
                             'url': r['url'], 'sitemap': 'v5:' + r['sitemap']})
    out = root / 'urls.csv'
    with out.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['city', 'url_token', 'mid_url', 'slug', 'url', 'sitemap'])
        w.writeheader()
        w.writerows(rows)
    by = {}
    for r in rows:
        by[(r['city'], r['url_token'])] = by.get((r['city'], r['url_token']), 0) + 1
    summary = {'fetched_at': now(), 'sitemap_parts': len(parts), 'failed_parts': failed, 'urls': len(rows),
               'by_city_token': {f'{k[0]}/{k[1]}': v for k, v in sorted(by.items())}}
    (root / 'manifests' / 'sitemap_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def summarize(data):
    """Короткая сводка страницы для статистики; полный __NEXT_DATA__ лежит рядом."""
    out = {}
    def walk(o, depth=0):
        if depth > 6 or not isinstance(o, dict):
            return
        for k in ('masterId', 'hid'):
            if k in o and k not in out and isinstance(o[k], (int, str)):
                out[k] = o[k]
        for v in o.values():
            if isinstance(v, dict):
                walk(v, depth + 1)
    walk(data)
    text = json.dumps(data, ensure_ascii=False)
    out['room_groups'] = text.count('"rgHash"')
    out['has_reviews'] = '"reviews"' in text
    return out


def cmd_pages(c: Client, root: Path, limit):
    urls = list(csv.DictReader((root / 'urls.csv').open()))
    done = set()
    pages_log = root / 'manifests' / 'pages.jsonl'
    if pages_log.exists():
        for line in pages_log.open():
            done.add(json.loads(line)['url'])
    todo = [u for u in urls if u['url'] not in done]
    if limit:
        todo = todo[:limit]
    print(f'всего {len(urls)}, уже собрано {len(done)}, в этом запуске {len(todo)}', flush=True)
    raw_dir, next_dir = root / 'raw', root / 'next'
    t0 = time.monotonic()
    for i, u in enumerate(todo, 1):
        url = u['url']
        body, rec = c.get(url, 'hotel')
        final_url, status = url, rec.get('status')
        if status in (301, 302, 307, 308) and rec.get('location'):
            dest = requests.compat.urljoin(url, rec['location'])
            if URL_RE.match(dest):  # переезд страницы отеля — открываем; редирект на выдачу = устаревший URL
                body, rec = c.get(dest, 'hotel_redirect')
                final_url, status = dest, rec.get('status')
        entry = {'url': url, 'city': u['city'], 'url_token': u['url_token'], 'mid_url': u['mid_url'],
                 'final_url': final_url, 'status': status, 'fetched_at': now(), 'location': rec.get('location')}
        if status == 200 and body:
            sha = rec['sha256']
            p = raw_dir / sha[:2] / f'{sha}.html.gz'
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                with gzip.open(p, 'wb', compresslevel=6) as f:
                    f.write(body)
            entry['raw_file'] = str(p.relative_to(root))
            entry['sha256'] = sha
            html = body.decode('utf-8', 'replace')
            m = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
            if m:
                data = json.loads(m.group(1))
                n = next_dir / sha[:2] / f'{sha}.json.gz'
                n.parent.mkdir(parents=True, exist_ok=True)
                if not n.exists():
                    with gzip.open(n, 'wt', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False)
                entry['next_file'] = str(n.relative_to(root))
                entry.update(summarize(data))
            else:
                entry['next_file'] = None
        with pages_log.open('a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        if i % 50 == 0 or i == len(todo):
            rate = i / (time.monotonic() - t0)
            left = (len(todo) - i) / rate / 3600 if rate else 0
            print(f'{now()} {i}/{len(todo)} ~{rate:.2f} стр/с, осталось ~{left:.1f} ч', flush=True)
    # Явный маркер завершения: RUNNING.lock снимается и при ошибке, поэтому по нему одному судить нельзя.
    finished = {e for e in (json.loads(x)['url'] for x in pages_log.open())} if pages_log.exists() else set()
    complete = all(u['url'] in finished for u in urls)
    (root / 'manifests' / ('DONE.json' if complete else 'PARTIAL.json')).write_text(json.dumps(
        {'finished_at': now(), 'urls': len(urls), 'pages_logged': len(finished), 'complete': complete,
         'limit': limit or None}, ensure_ascii=False, indent=2))


def cmd_status(root: Path):
    urls = list(csv.DictReader((root / 'urls.csv').open())) if (root / 'urls.csv').exists() else []
    st, by_city = {}, {}
    p = root / 'manifests' / 'pages.jsonl'
    if p.exists():
        for line in p.open():
            e = json.loads(line)
            st[e['status']] = st.get(e['status'], 0) + 1
            if e['status'] == 200 and e.get('next_file'):
                by_city[e['city']] = by_city.get(e['city'], 0) + 1
    print(json.dumps({'urls': len(urls), 'status': st, 'pages_with_next_data': by_city,
                      'stopped': (root / 'manifests' / 'STOP.json').exists()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('data_dir')
    ap.add_argument('mode', choices=['sitemap', 'pages', 'status'])
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    root = Path(a.data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if a.mode == 'status':
        cmd_status(root)
        sys.exit(0)
    lock = root / 'manifests' / 'RUNNING.lock'
    (root / 'manifests').mkdir(parents=True, exist_ok=True)
    if lock.exists():
        sys.exit(f'Уже идёт другой сбор (есть {lock}). Один поток — правило вежливости.')
    # Выключение ПК, закрытие терминала или kill шлют SIGTERM / SIGHUP — выходим через finally,
    # чтобы lock снялся и продолжение запускалось той же командой без ручной уборки.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))
    lock.write_text(now())
    try:
        client = Client(root)
        if a.mode == 'sitemap':
            cmd_sitemap(client, root)
        else:
            cmd_pages(client, root, a.limit)
    finally:
        lock.unlink(missing_ok=True)
