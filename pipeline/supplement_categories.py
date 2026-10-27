"""Дополнение отсутствующих категорий из отдельного датированного снимка.

Не меняет координаты/имена отелей и старые геофакты. Только точный masterId,
полный roomGroups и отели, у которых ещё нет категорий. Пустой список не заполняется
догадкой. Исходные packs/ не заменяются: результат публикуется отдельной командой.

У дополненного отеля меняются ещё две вещи: ссылка на Островок, если прежняя ведёт не на его
страницу (нет `mid` или другой `mid`), — на адрес страницы снимка с пометкой, откуда и когда
она взята; предзаполнение вопросов отельеру — заявлениями страницы (`pipeline.hotelier_questions`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path

from connectors.ostrovok_site import parse_page
from core.packs import PackFile, PackSource, _layers_version, iter_jsonl, sha256_file, verify
from pipeline.build_packs import DERIVED_URL, coverage, new_output
from pipeline.build_v9_packs import write_jsonl_gz
from pipeline.hotelier_questions import build_questions

# .2: ссылка на страницу отеля и предзаполнение вопросов у дополненных отелей; версия пакета —
# с хешем файлов-слоёв (`core.packs._layers_version`)
VERSION = "category-supplement.2"
TARGETS = ("13736470", "10012084", "9986868", "11313836", "6458292", "8736052")
SCOPE = "room_categories_only"


def position_delta(a, b):
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a["longitude"])
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(value)))


def rows(pack, manifest, kind):
    for entry in manifest.files:
        if entry.kind == kind:
            yield from iter_jsonl(pack / entry.path)


def page_url(hotel_id, hotel_source, page_source, snapshot_name):
    """Ссылка на страницу отеля из снимка, если прежняя ведёт не туда (нет `mid` или другой
    `mid`, как у части отелей v9) или собрана без проверки (`build_packs.derived_page_url`).
    Только адрес страницы с ответом 200 и `mid` этого отеля."""
    old = hotel_source.get("url") or ""
    new = page_source["url"]
    mine = f"/mid{hotel_id}/"
    derived = hotel_source.get("url_method") == DERIVED_URL
    if (mine in old and not derived) or mine not in new:
        return None
    kept = {k: v for k, v in hotel_source.items() if k != "url_method"}
    return kept | {
        "url": new,
        "url_previous": hotel_source.get("url_previous") or old or None,
        "url_from": snapshot_name,
        "url_fetched_at": page_source["fetched_at"],
    }


def rewrite(folder, manifest, kind, records):
    """Переписать файлы вида `kind` одним файлом (прежние имена сохраняются, если файл один)."""
    entries = [entry for entry in manifest.files if entry.kind == kind and not entry.layer]
    if len(entries) != 1:
        raise ValueError(f"Expected one {kind} file to rewrite")
    entry = entries[0]
    write_jsonl_gz(folder / entry.path, records)
    entry.rows = len(records)
    entry.bytes = (folder / entry.path).stat().st_size
    entry.sha256 = sha256_file(folder / entry.path)


def supplement(base, snapshot, out, hotel_ids=TARGETS, *, built_at=None):
    out = new_output(out, base, snapshot)
    out.mkdir(parents=True, exist_ok=True)
    targets = set(hotel_ids)
    pages = {}
    for entry in iter_jsonl(snapshot / "manifests/pages.jsonl"):
        if entry.get("status") != 200 or not entry.get("next_file"):
            continue
        # Журнал — подсказка. Итоговая идентичность проверяется по JSON parse_page.
        page = parse_page(snapshot, entry)
        hid = page.hotels[0].id
        if hid not in targets:
            if str(entry.get("mid_url")) in targets:
                raise ValueError("Snapshot masterId differs from requested identity")
            continue
        if hid in pages:
            raise ValueError("Duplicate snapshot page for target masterId")
        pages[hid] = page
    result = {
        "version": VERSION,
        "snapshot_version": snapshot.name,
        "index_sha256": sha256_file(snapshot / "manifests/pages.jsonl"),
        "hotels": {},
        "cities": {},
    }
    known = set()
    for path in sorted(base.glob("*/manifest.json")):
        pack = path.parent
        manifest = verify(pack)
        hotels = list(rows(pack, manifest, "hotels"))
        selected = [h for h in hotels if h["id"] in targets]
        if not selected:
            continue
        rooms = list(rows(pack, manifest, "room_types"))
        counts = {}
        for room in rooms:
            counts[room["hotel_id"]] = counts.get(room["hotel_id"], 0) + 1
        added_rooms, added_facts, urls = [], [], {}
        for hotel in selected:
            hid = hotel["id"]
            known.add(hid)
            row = {
                "city_id": manifest.city_id,
                "existing_categories": counts.get(hid, 0),
                "added_categories": 0,
                "added_facts": 0,
            }
            result["hotels"][hid] = row
            if counts.get(hid):
                row["status"] = "already_present"
                continue
            if hid not in pages:
                row["status"] = "snapshot_page_missing"
                continue
            page = pages[hid]
            if page.hotels[0].city_id != manifest.city_id:
                raise ValueError("Snapshot city differs from catalog city")
            delta = position_delta(hotel, page.hotels[0])
            if delta > 50:
                raise ValueError(
                    "Hotel moved more than 50m; category-only supplement requires review"
                )
            row["position_delta_m"] = round(delta, 3)
            row["source"] = page.hotels[0].source
            if not page.room_types:
                row["status"] = "room_groups_empty"
                continue
            for room in page.room_types:
                raw = room.model_dump(mode="json")
                raw["source"] |= {"import_scope": SCOPE}
                added_rooms.append(raw)
            for fact in page.facts:
                raw = fact.model_dump(mode="json", exclude_none=True)
                raw["sources"] = [source | {"import_scope": SCOPE} for source in raw["sources"]]
                added_facts.append(raw)
            row.update(
                status="added", added_categories=len(page.room_types), added_facts=len(page.facts)
            )
            if updated := page_url(hid, hotel.get("source") or {}, row["source"], snapshot.name):
                urls[hid] = updated
                row["url_updated"] = updated["url"]
        if not added_rooms:
            continue
        folder = out / manifest.city_id
        shutil.copytree(pack, folder)
        for kind, records in (("room_types", added_rooms), ("facts", added_facts)):
            records.sort(key=lambda r: r.get("id", r.get("fact_id")))
            relative = f"data/{kind}_{snapshot.name}.jsonl.gz"
            if (folder / relative).exists():
                raise ValueError("Supplement file already exists")
            write_jsonl_gz(folder / relative, records)
            manifest.files.append(
                PackFile(
                    path=relative,
                    kind=kind,
                    rows=len(records),
                    bytes=(folder / relative).stat().st_size,
                    sha256=sha256_file(folder / relative),
                )
            )
        if urls:
            hotels = [h | {"source": urls[h["id"]]} if h["id"] in urls else h for h in hotels]
            rewrite(folder, manifest, "hotels", sorted(hotels, key=lambda h: h["id"]))
        questions = list(rows(pack, manifest, "questions"))
        if questions:
            added = {room["hotel_id"] for room in added_rooms}
            refreshed, _ = build_questions(
                [], (f for f in added_facts if f["hotel_id"] in added), questions
            )
            rewrite(folder, manifest, "questions", refreshed)
        # Статусы D26 не считаются computed; знаменатель и прочие факты сохраняются.
        templates = list(rows(pack, verify(pack), "coverage"))
        covered = coverage(hotels, rows(folder, manifest, "facts"), templates)
        for entry in manifest.files:
            if entry.kind == "coverage":
                write_jsonl_gz(folder / entry.path, covered)
                entry.rows, entry.bytes, entry.sha256 = (
                    len(covered),
                    (folder / entry.path).stat().st_size,
                    sha256_file(folder / entry.path),
                )
        manifest.sources.append(
            PackSource(
                id="ostrovok_category_supplement",
                provider="ostrovok.ru",
                release=snapshot.name,
                sha256=result["index_sha256"],
                fetched_at=max(
                    datetime.fromisoformat(room["source"]["fetched_at"]) for room in added_rooms
                ),
            )
        )
        manifest.built_at = built_at or datetime.now(UTC)
        if manifest.built_at.utcoffset() is None:
            raise ValueError("Build timestamp requires timezone")
        manifest.code_version = VERSION
        identity = "".join(entry.sha256 for entry in manifest.files if not entry.layer)
        manifest.pack_version = VERSION + "-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
        manifest.pack_version = _layers_version(manifest)
        manifest.notes = (
            manifest.notes or ""
        ) + f" Категории дополнены из {snapshot.name}; координаты и геофакты сохранены."
        (folder / "manifest.json").write_text(
            manifest.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8"
        )
        verify(folder)
        result["cities"][manifest.city_id] = {
            "pack_version": manifest.pack_version,
            "added_categories": len(added_rooms),
            "added_facts": len(added_facts),
        }
    if targets - known:
        raise ValueError("Requested masterId not found in base catalog")
    (out / "category_supplement_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-packs", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hotel-id", action="append")
    args = parser.parse_args()
    print(
        json.dumps(
            supplement(args.base_packs, args.snapshot, args.out, args.hotel_id or TARGETS)[
                "cities"
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
