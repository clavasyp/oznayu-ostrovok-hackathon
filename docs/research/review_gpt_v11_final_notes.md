# v11 final — рабочие заметки (28.10)

Финал на диске = main fb1 (sha fb1fce21…), принят root 25.10 18:49 МСК, DRAFT/HOLD. В репозитории лежит 2cca (25.10 14:18) — отличается только разделом listing (8b → e79) и абзацем D38/D39.

Хронология после 2cca:
- 25.10 16:13 МСК resume → listing до DNS Errno 8 (Москва p.172) 16:58 МСК → e79: 728 стр / 14 521 / 11 699 MID; previews M 3083 (2607 new) / S 8264 (7213 new).
- 26.10 12:33 resume «давай делай» → +95 стр → HTTP 500 QRATOR, Сириус p.197, 12:40 → dc38: 823 / 16 421 / 13 320; previews M 3597 = 3099 new + 498 refresh; S 9293 = 8080 new + 1213 refresh.
- 26.10: изолированный пересчёт компонентов Москвы на 7311 профилях — rc0, core QA принят, scene QA нет, не заморожен (4.1 ГБ). Сочи — прерван паузой (rc130, без manifest).
- 26.10 15:27 пауза пользователя («пк офнуть»).
- 27.10 15:19–15:21 МСК: короткий resume (process gate, performance2 кандидат, независимые тесты point-рецептов на крошечных синтетических данных) → обрыв без записи в журнал/чекпоинта.

Сверка чисел (сам):
- corpus/v4/metrics.json: 3032 атома, 91/2951 exact, 0/323 — ок; eval 200 + 325 + 137 NC — ок.
- sochi/curation/summary.json: 2596 / 2855 / 6497,5 мин / 63 540 пар — ок; shortlist.csv 63 540 строк.
- sochi building_items.jsonl: 1410 связей, 1278 отелей, levels у 714, все medium — ок.
- sochi last_returns.csv: 19:30 / 21:19 / 22:23 — ок.
- moscow acquisition_stats: 1113 внутр., еда 365, кухня 355, italian 107 — ок; maps_entities 1273; aero trips 7668; routes 400; links 94 260; curation 6353; building cands 4162 — ок.
- listing dc38 listing_summary: 823 / 16 421 / 13 320, STOP 500 Sirius p.197 — ок.

Находки:
- rating=0 при rating_count>0: Сочи 163 записи (пляжи и пр., напр. «Центральный пляж» 0 / 4890), Москва 21. Это буквальный ratingValue Яндекса → читать как unknown.
- Новые MID listing: APARTMENT 2549/3099 (82%) Москва, 6161/8080 (76%) Сочи.
- Синтетика 200: шаблоны «Нужен отель в Москва. Обязательно: …», 11 московских запросов про море/пляж; не естественный язык.
- 325 internal — отзывы (requiredness not_applicable), не запросы.
- atoms: 1350 разных predicate, 1002 единичных — словарь не нормализован; группировать по классам gaps_priority/taxonomy.
- Данные карт — публичный HTML Яндекса, license «D35_organizer_permission_public_maps», commercial_ok нет; D37 п.5 — только официальные API.
