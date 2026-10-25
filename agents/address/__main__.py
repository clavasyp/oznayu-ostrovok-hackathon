"""Адреса города: разбор адресов отелей и кандидаты «отель → здание» для проверки (D33).

    python -m agents.address --city moscow                       # только разбор адресов
    python -m agents.address --city moscow --buildings FILE.jsonl.gz --out candidates.jsonl
    python -m agents.address --city moscow --llm                 # модель — стоит денег

Сеть не нужна (кроме `--llm`). Здания — в формате пакета (`buildings.jsonl.gz`, packs/FORMAT.md).
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

from agents.address.llm import normalize
from agents.address.match import BuildingIndex, load_buildings, propose

PACKS = Path(__file__).resolve().parents[2] / "packs"


def load_hotels(city_id: str, packs: Path = PACKS) -> list[dict]:
    with gzip.open(packs / city_id / "data" / "hotels.jsonl.gz", "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def v9_links(city_id: str, packs: Path = PACKS) -> dict[str, set[str]]:
    """Связи «отель → здание» из расчёта v9 (по фактам пакета) — для сверки, не как правда."""
    links: dict[str, set[str]] = defaultdict(set)
    path = packs / city_id / "data" / "facts.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            fact = json.loads(line)
            value = fact.get("value")
            if isinstance(value, dict) and value.get("building_id"):
                links[fact["hotel_id"]].add(value["building_id"])
            if fact.get("subject_level") == "building":
                links[fact["hotel_id"]].add(fact["subject_id"])
    return links


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agents.address")
    ap.add_argument("--city", default="moscow")
    ap.add_argument("--buildings", type=Path, help="здания OSM с метками addr:* (jsonl / jsonl.gz)")
    ap.add_argument("--out", type=Path, help="куда записать кандидаты curation_item (jsonl)")
    ap.add_argument("--llm", action="store_true", help="модель для неразобранных адресов")
    ap.add_argument("--packs", type=Path, default=PACKS)
    args = ap.parse_args(argv)

    hotels = load_hotels(args.city, args.packs)
    parsed = {h["id"]: normalize(h.get("address") or "", use_llm=args.llm) for h in hotels}
    complete = [hid for hid, (a, _, _) in parsed.items() if a.complete]
    print(f"{args.city}: адрес разобран полностью у {len(complete)} из {len(hotels)} отелей")
    for h in hotels:
        a, by, notes = parsed[h["id"]]
        if not a.complete:
            print(
                f"  не разобран: {h['id']} «{h.get('address')}»"
                + (f" ({'; '.join(notes)})" if notes else "")
            )
        elif by == "llm":
            print(f"  моделью: {h['id']} «{h.get('address')}» → {a.text()}")
    if not args.buildings:
        print("Зданий нет (--buildings): кандидаты не строятся.")
        return 0

    links = v9_links(args.city, args.packs)
    index = BuildingIndex(load_buildings(args.buildings))
    items = []
    for h in hotels:
        addr, by, _ = parsed[h["id"]]
        proposed_by = "llm+script:agents.address" if by == "llm" else "script:agents.address"
        items.append(
            propose(
                h, index, address=addr, proposed_by=proposed_by, v9_links=links.get(h["id"], ())
            )
        )
    by_conf = Counter(i["proposal"]["confidence"] for i in items)
    print("Кандидаты:", ", ".join(f"{k} — {v}" for k, v in sorted(by_conf.items())))
    agree = [i for i in items if links.get(i["subject_id"])]
    same = [i for i in agree if i["proposal"]["building_id"] in links[i["subject_id"]]]
    print(f"Сверка с v9: связь была у {len(agree)}, наш кандидат совпал у {len(same)}")
    if args.out:
        with args.out.open("w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"Записано: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
