# Входы расчётов (D45)

Из чего собраны пакеты Москвы и Большого Сочи: всё, что читают сборщики `pipeline/`, — здесь
или в релизе GitHub. Пакеты (`packs/`) — результат, эти файлы — вход: пересборка на них даёт те же
`fact_id`. В образ Docker `data/` не попадает (`.dockerignore`), жюри они не нужны.

## В репозитории (`data/inputs/`)

| Папка / файл | Что | Каким шагом читается | Источник, лицензия |
|---|---|---|---|
| `era5/` | климат месяца по ячейкам 0,25° для 8 городов (кеш ответов) | `pipeline.build_environment --era5` | ERA5 через Open-Meteo, CC BY 4.0 |
| `era5_points_sochi/` | климат в точках 0,05° у отелей Сочи | `pipeline.build_environment --points` | то же |
| `districts_moscow/` | границы 132 районов и поселений Москвы (admin_level 8) | `pipeline.build_districts apply`, `pipeline.add_hotel` | OSM / Geofabrik ЦФО 22.10.2026, ODbL 1.0 |
| `heritage/` | объекты реестра культурного наследия Москвы и Сочи | `pipeline.build_heritage --raw` | Wikidata, CC0 |
| `beach_tags.json` | покрытие пляжей по тегам OSM | `pipeline.build_beach_surface` | OSM, ODbL 1.0 |
| `copdem_tileList.txt` | список листов GLO-30 (где суша без рельефа) | `pipeline.build_view_sun` | Copernicus DEM |
| `ostrovok_pages_2026-10-29/` | страница Palmira Art (её нет в снимке 24.10) в формате снимка | `pipeline.add_hotel stage` | ostrovok.ru, сбор разрешён организаторами |

## В релизе [`data-raw-2026-10-29`](https://github.com/clavasyp/oznayu-ostrovok-hackathon/releases/tag/data-raw-2026-10-29)

Файлы больше 100 МБ git не принимает, поэтому большие входы — релизом:
- срезы OSM ЦФО и ЮФО (`*.osm.pbf`);
- сцены зданий и деревьев для окон (`osm_*_scene*.jsonl.gz`);
- высоты 3D-GloBFP;
- листы рельефа GLO-30 и покрова WorldCover.

Загрузка и сверка:

```bash
gh release download data-raw-2026-10-29 -R clavasyp/oznayu-ostrovok-hackathon -D data/raw
cd data/raw && shasum -a 256 -c SHA256SUMS.txt
```

`data/raw/` git не отслеживает. Сцены и GloBFP перед расчётом распаковать (`gunzip -k`). Пути
к ним — `CITIES` в `pipeline/build_view_sun.py`. Страницы Островка 24.10 — релиз
`data-ostrovok-2026-10-24` (`data/ostrovok_catalog/README.md`).

Не входят: ответы API 2ГИС (условия 2ГИС, D37), отзывы (D37), данные Яндекса v11.
