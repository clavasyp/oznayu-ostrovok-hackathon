"""Сверка уже собранного D26 с исходным JSON; parse_page повторно не вызывается.

Задания группируются на диске по snapshot_file: каждый нужный JSON распаковывается
один раз. Проверяется и полнота новых категорий/фактов, не только существование pointer.
HTML-хеш сверяется с журналом: самого HTML этот аудитор не проверяет.
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import zlib
from collections import defaultdict
from pathlib import Path

from connectors.ostrovok_site import HOTEL_POINTER, pet_claims, room_claims
from core.packs import iter_jsonl, sha256_file
from pipeline.audit_packs import aware_date, safe_path


def clean(value):
    return (
        value.strip()
        if isinstance(value, str) and value.strip() and value.strip().upper() != "UNSPECIFIED"
        else None
    )


def resolve_pointer(document, pointer):
    """RFC 6901, включая ~0/~1; отрицательный индекс не является JSON pointer."""
    if pointer == "":
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("invalid pointer")
    node = document
    for component in pointer[1:].split("/"):
        if any(not part or part[0] not in "01" for part in component.split("~")[1:]):
            raise ValueError("invalid escape")
        key = component.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            if not key.isascii() or not key.isdigit() or (len(key) > 1 and key[0] == "0"):
                raise ValueError("invalid array index")
            node = node[int(key)]
        else:
            node = node[key]
    return node


def expectations(raw):
    """Ожидаемые утверждения из явных полей первоисточника, без построения Fact.

    Правила меток и фраз — те же функции, что у коннектора (`room_claims`, `pet_claims`):
    аудит сверяет собранное с JSON страницы, а не повторяет правила вторым списком."""
    hid = str(raw["masterId"])
    rooms, facts = {}, {}
    for index, group in enumerate(raw.get("roomGroups") or []):
        rid = hid + ":" + clean(group.get("rgHash"))
        base = f"{HOTEL_POINTER}/roomGroups/{index}"
        rooms[rid] = (base, group)
        tags = group.get("roomAmenityData") or []
        bedding = clean((group.get("nameStruct") or {}).get("beddingType"))
        for claim in room_claims(base, tags, bedding):
            facts[(claim.feature_id, rid, claim.pointer)] = (claim.value, False)
    for claim in pet_claims(raw, hid):
        facts[(claim.feature_id, "hotel:" + hid, claim.pointer)] = (claim.value, claim.hard)
    return rooms, facts


class SnapshotAudit:
    def __init__(self, root, conn, issues):
        roots = [root] if isinstance(root, Path) else list(root)
        self.roots = {path.name: path for path in roots}
        if len(self.roots) != len(roots):
            raise ValueError("Snapshot dataset names must be unique")
        self.conn, self.issues = conn, issues
        self.index = defaultdict(list)
        self.index_sha = {}
        self.catalog_hotels = set()
        self.unverified_references = 0
        for path in roots:
            self.index_sha[path.name] = sha256_file(path / "manifests/pages.jsonl")
            for entry in iter_jsonl(path / "manifests/pages.jsonl"):
                if entry.get("next_file") and entry.get("status") == 200:
                    self.index[(path.name, entry["next_file"])].append(entry)
        conn.execute(
            "CREATE TABLE snapshot_jobs "
            "(dataset TEXT, file TEXT, kind TEXT, payload TEXT, origin TEXT)"
        )

    def enqueue(self, kind, row, where):
        if kind == "hotels":
            self.catalog_hotels.add(row["id"])
        if kind in {"hotels", "room_types"}:
            source = row.get("source") or {}
            if source.get("provider") != "ostrovok.ru" or not source.get("snapshot_file"):
                return
        elif kind == "facts" and str(row.get("algorithm_version")).startswith("ostrovok-site."):
            # Пустой или неполный паспорт уже записан ошибкой в PackAudit.check; факт без
            # пригодного паспорта не сверяется, а честно попадёт в snapshot_fact_missing.
            sources = row.get("sources") or []
            source = sources[0] if sources and isinstance(sources[0], dict) else {}
            if source.get("provider") != "ostrovok.ru" or not source.get("snapshot_file"):
                return
        else:
            return
        dataset = source["dataset_version"]
        if dataset not in self.roots:
            self.unverified_references += 1
            self.issues.add("snapshot_dataset_not_supplied", where, field=dataset)
            return
        self.conn.execute(
            "INSERT INTO snapshot_jobs VALUES (?,?,?,?,?)",
            (dataset, source["snapshot_file"], kind, json.dumps(row, ensure_ascii=False), where),
        )

    def check(self, kind, row, source, document, digest, rooms, expected):
        if source.get("import_scope") not in {None, "room_categories_only"}:
            raise ValueError("Unknown import scope")
        raw = document["props"]["pageProps"]["hotel"]
        target = resolve_pointer(document, source["json_pointer"])
        if source["dataset_sha256"] != digest:
            raise ValueError("JSON hash mismatch")
        matching = any(
            source["url"] == (entry.get("final_url") or entry["url"])
            and source["html_sha256"] == entry["sha256"]
            and aware_date(source["fetched_at"]) == aware_date(entry["fetched_at"])
            for entry in self.index[(source["dataset_version"], source["snapshot_file"])]
        )
        if not matching:
            raise ValueError("snapshot index mismatch")
        hid = str(raw["masterId"])
        if kind == "hotels":
            if row["id"] != hid or source["json_pointer"] != HOTEL_POINTER:
                raise ValueError("hotel identity mismatch")
            expected_hotel = {
                "name": clean(raw["name"]),
                "address": clean(raw["location"]["address"]),
                "latitude": raw["location"]["latitude"],
                "longitude": raw["location"]["longitude"],
            }
            if any(row.get(key) != value for key, value in expected_hotel.items()):
                raise ValueError("hotel content mismatch")
            stars = (raw.get("facts") or {}).get("starRating")
            expected_stars = stars if type(stars) in {int, float} and stars != 0 else None
            if row.get("stars") != expected_stars or row.get("kind") != clean(
                (raw.get("facts") or {}).get("kind")
            ):
                raise ValueError("hotel classification mismatch")
        elif kind == "room_types":
            pointer, group = rooms[row["id"]]
            if (
                row["hotel_id"] != hid
                or pointer != source["json_pointer"]
                or row["rg_hash"] != clean(group["rgHash"])
            ):
                raise ValueError("room identity mismatch")
            name = clean((group.get("nameStruct") or {}).get("mainName")) or clean(
                group.get("name")
            )
            if row["name"] != name or row["amenities"] != (group.get("roomAmenityData") or []):
                raise ValueError("room content mismatch")
        else:
            key = (row["feature_id"], row["subject_id"], source["json_pointer"])
            value, hard = expected[key]
            if (
                row["hotel_id"] != hid
                or row.get("value") != value
                or row["status"] != ("unknown" if value is None else "ostrovok_content")
            ):
                raise ValueError("D26 value/status mismatch")
            if row.get("hard_condition_eligible") is not hard:
                raise ValueError("D26 guarantee mismatch")
            if aware_date(row["observed_at"]) != aware_date(source["fetched_at"]):
                raise ValueError("observation time mismatch")
            if (
                "source_value" in source
                and (target.strip() if isinstance(target, str) else target)
                != source["source_value"]
            ):
                raise ValueError("source value mismatch")

    def validate(self):
        result = {
            "status": "passed",
            "index_sha256_by_dataset": self.index_sha,
            "index_sha256": next(iter(self.index_sha.values()))
            if len(self.index_sha) == 1
            else None,
            "json_files": 0,
            "references": 0,
            "records": {},
            "html_verification": "hash_compared_to_index_only",
            "unreferenced_snapshot_pages": "not_checked",
            "unverified_references": self.unverified_references,
            "category_only_pages": 0,
        }
        errors_before = sum(self.issues.errors.values())
        self.conn.execute("CREATE INDEX snapshot_jobs_file ON snapshot_jobs(dataset,file)")
        cursor = self.conn.execute(
            "SELECT dataset,file,kind,payload,origin FROM snapshot_jobs ORDER BY dataset,file"
        )
        for (dataset, name), jobs in itertools.groupby(cursor, key=lambda job: job[:2]):
            document, digest = None, None
            try:
                path = safe_path(self.roots[dataset], name)
                with gzip.open(path, "rb") as source:
                    payload = source.read(32 * 1024 * 1024 + 1)
                if len(payload) > 32 * 1024 * 1024:
                    raise ValueError("snapshot JSON larger than 32 MiB")
                digest = hashlib.sha256(payload).hexdigest()
                document = json.loads(payload)
                rooms, expected = expectations(document["props"]["pageProps"]["hotel"])
                result["json_files"] += 1
            except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, zlib.error):
                self.issues.add("snapshot_document_unreadable", name)
                document = None
            seen_rooms, seen_facts, seen_hotel = set(), set(), False
            scopes = set()
            for _, _, kind, payload, where in jobs:
                result["references"] += 1
                result["records"][kind] = result["records"].get(kind, 0) + 1
                if document is None:
                    continue
                row = json.loads(payload)
                source = row["sources"][0] if kind == "facts" else row["source"]
                scopes.add(source.get("import_scope"))
                try:
                    self.check(kind, row, source, document, digest, rooms, expected)
                    if kind == "facts":
                        key = (row["feature_id"], row["subject_id"], source["json_pointer"])
                        if key in seen_facts:
                            self.issues.add("snapshot_duplicate_fact_reference", where)
                        seen_facts.add(key)
                    elif kind == "room_types":
                        seen_rooms.add(row["id"])
                    else:
                        seen_hotel = True
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                    self.issues.add("snapshot_record_mismatch", where)
            if document is not None:
                for key in set(expected) - seen_facts:
                    self.issues.add("snapshot_fact_missing", name, field=key[0])
                for _ in set(rooms) - seen_rooms:
                    self.issues.add("snapshot_room_missing", name)
                category_only = (
                    scopes == {"room_categories_only"}
                    and str(document["props"]["pageProps"]["hotel"]["masterId"])
                    in self.catalog_hotels
                )
                if category_only:
                    result["category_only_pages"] += 1
                if not seen_hotel and not category_only:
                    self.issues.add("snapshot_hotel_missing", name)
        if sum(self.issues.errors.values()) != errors_before or self.unverified_references:
            result["status"] = "failed"
        elif not result["references"]:
            result["status"] = "not_applicable"
        return result
