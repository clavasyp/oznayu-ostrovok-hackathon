"""Малый проверочный прогон 2ГИС: без --execute только план, ключ не попадает в отчёт.

Пять запросов, Matrix 1×1, общий месячный бюджет и повтор из кеша. Сбой соединения
останавливает остальные проверки того же хоста, ошибки не превращаются в факты.
Ответ из кеша подтверждает доступ, только если получен тем же ключом (отпечаток в source);
ответ другого ключа — статус from_other_key без расхода, повтор — по --recheck-other-key.
Отчёт пишется всегда: непредвиденная ошибка проверки — error_kind "unexpected".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from connectors.twogis import (
    DEFAULT_CACHE,
    ENDPOINTS,
    TwoGISClient,
    TwoGISError,
    fingerprint,
    load_key,
    place_rows,
    scrub,
    transit_rows,
    units,
)

VERSION = "twogis-smoke.2"
NETWORK_ERRORS = {"network", "network_timeout", "tls", "tls_timeout", "dns"}
POINTS = [{"lat": 55.7647, "lon": 37.6388}, {"lat": 55.7452, "lon": 37.6032}]


def error_text(exc, key):
    """Текст ошибки для отчёта: без значения ключа и без key= в URL."""
    text = str(exc) if isinstance(exc, TwoGISError) else f"{type(exc).__name__}: {exc}"
    return scrub(text.replace(key, "[redacted]") if key else text)


def cases(departure: datetime):
    if departure.utcoffset() is None:
        raise ValueError("Departure requires timezone")
    return [
        ("categories", {"q": "Рестораны", "region_id": 32, "page_size": 5}),
        (
            "places",
            {
                "q": "ресторан",
                "point": "37.6388,55.7647",
                "radius": 1000,
                "type": "branch",
                "page_size": 3,
                "fields": "items.point,items.rubrics,items.schedule",
            },
        ),
        (
            "routing",
            {
                "points": [point | {"type": "walking"} for point in POINTS],
                "transport": "walking",
                "route_mode": "fastest",
                "output": "summary",
                "alternative": 0,
                "locale": "ru",
            },
        ),
        (
            "transit",
            {
                "source": {"point": POINTS[0]},
                "target": {"point": POINTS[1]},
                "transport": ["metro"],
                "start_time": int(departure.timestamp()),
                "enable_schedule": True,
                "locale": "ru",
            },
        ),
        (
            "matrix",
            {
                "points": POINTS,
                "sources": [0],
                "targets": [1],
                "transport": "walking",
                "start_time": departure.isoformat(),
            },
        ),
    ]


def metric(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def summarize(action, record, rubric_mapping):
    payload = record["response"]
    if action == "categories":
        items = payload["result"]["items"]
        if not all(isinstance(item.get("id"), str | int) and item.get("name") for item in items):
            raise ValueError("Invalid category schema")
        for item in items:
            if item["name"].strip().casefold() == "рестораны":
                rubric_mapping[str(item["id"])] = ["restaurant"]
        return {"items": len(items), "restaurant_rubric_ids": sorted(rubric_mapping)}, "passed"
    if action == "places":
        result = payload["result"]
        collection = {
            "items": result["items"],
            "sources": [record["source"]],
            "complete": result.get("total") is not None and len(result["items"]) >= result["total"],
        }
        rows = place_rows(collection, "moscow", rubric_mapping)
        if len(rows) != len(result["items"]):
            raise ValueError("Place coordinates missing or invalid")
        return {
            "normalized_places": len(rows),
            "collection_complete": collection["complete"],
            "rubric_mapping_verified": bool(rubric_mapping),
        }, "passed"
    if action == "transit":
        if not isinstance(payload, list):
            if payload == {"status": "no_route"}:
                return {"normalized_transit_rows": 0}, "no_route"
            raise ValueError("Invalid public transport response")
        rows = transit_rows(record, city="moscow", source_id="smoke:a", target_id="smoke:b")
        if any(
            not metric(row["duration_s"])
            or type(row["transfers"]) is not int
            or row["transfers"] < 0
            for row in rows
        ):
            raise ValueError("Invalid transport metric")
        return {
            "normalized_transit_rows": len(rows),
            "departure_at": datetime.fromtimestamp(
                record["request"]["params"]["start_time"], UTC
            ).isoformat(),
            "samples": [
                {key: row[key] for key in ("duration_s", "transfers", "profile")}
                for row in rows[:2]
            ],
        }, "passed" if rows else "no_route"
    if payload == {"status": "no_route"}:
        return {"routes": 0}, "no_route"
    if action == "routing":
        if payload.get("status") in {
            "ROUTE_NOT_FOUND",
            "ROUTE_DOES_NOT_EXISTS",
            "ATTRACT_FAIL",
            "POINT_EXCLUDED",
        }:
            return {"routes": 0}, "no_route"
        if payload.get("status") != "OK" or not isinstance(payload.get("result"), list):
            raise ValueError("Invalid routing response")
        rows = payload["result"]
        if any(
            not metric(row.get("total_distance")) or not metric(row.get("total_duration"))
            for row in rows
        ):
            raise ValueError("Invalid routing metric")
        return {"routes": len(rows)}, "passed" if rows else "no_route"
    rows = payload["routes"]
    if not isinstance(rows, list) or any(
        row.get("source_id") != 0 or row.get("target_id") != 1 for row in rows
    ):
        raise ValueError("Matrix pair mismatch")
    good = [row for row in rows if row.get("status") == "OK"]
    if any(not metric(row.get("distance")) or not metric(row.get("duration")) for row in good):
        raise ValueError("Invalid matrix metric")
    if any(
        row.get("status")
        not in {
            "OK",
            "ROUTE_NOT_FOUND",
            "ROUTE_DOES_NOT_EXISTS",
            "ATTRACT_FAIL",
            "POINT_EXCLUDED",
            "PLATFORMS_NOT_FOUND",
        }
        for row in rows
    ):
        raise ValueError("Matrix backend failed")
    return {"pairs": len(rows), "successful_pairs": len(good)}, "passed" if good else "no_route"


def run_checks(client, departure, *, recheck_other_key=False):
    report = {
        "version": VERSION,
        "built_at": datetime.now(UTC).isoformat(),
        "key_tier": "subscription" if client.subscription else "demo_key",
        "commercial_ok": False,
        "cases": [],
        "network_attempts": 0,
        "http_responses": 0,
        "published_to_packs": False,
    }
    blocked_hosts, rubric_mapping = set(), {}
    account = fingerprint(client.key)
    transport = client.transport

    def counted(*args):
        report["network_attempts"] += 1
        try:
            result = transport(*args)
        except TwoGISError as exc:
            if exc.http_status:
                report["http_responses"] += 1
            raise
        report["http_responses"] += 1
        return result

    client.transport = counted
    try:
        for action, params in cases(departure):
            host = urlsplit(ENDPOINTS[action][1]).hostname
            item = {"action": action, "request": params, "maximum_units": units(action, params)}
            report["cases"].append(item)
            if host in blocked_hosts:
                item.update(status="skipped", reason="same_host_connection_failed")
                continue
            before = report["network_attempts"]
            try:
                result = client.request(action, params)
                if (
                    report["network_attempts"] == before
                    and result["source"].get("account_fingerprint") != account
                ):
                    # Ответ другого ключа не доказывает доступ этого; без флага не тратим лимит.
                    if not recheck_other_key:
                        item.update(status="from_other_key", reason="cached_with_another_key")
                        continue
                    result = client.request(action, params, refresh=True)
                summary, status = summarize(action, result, rubric_mapping)
                after = report["network_attempts"]
                replay = client.request(action, params)
                if replay != result or report["network_attempts"] != after:
                    raise ValueError("Cache replay made another request")
                item.update(
                    status=status,
                    summary=summary,
                    source=result["source"],
                    from_cache=after == before,
                    cache_replay_verified=True,
                )
            except TwoGISError as exc:
                item.update(
                    status="failed",
                    error_kind=exc.kind,
                    error=error_text(exc, client.key),
                    http_status=exc.http_status,
                    api_status=exc.api_status,
                )
                if exc.kind in NETWORK_ERRORS:
                    blocked_hosts.add(host)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                item.update(
                    status="failed",
                    error_kind="response_validation",
                    error=error_text(exc, client.key),
                )
            except Exception as exc:
                # Последний рубеж: отчёт пишется при любой ошибке, хост не помечается.
                item.update(
                    status="failed", error_kind="unexpected", error=error_text(exc, client.key)
                )
    finally:
        client.transport = transport
    outcomes = Counter(item["status"] for item in report["cases"])
    good = outcomes["passed"] + outcomes["no_route"]
    report["status"] = (
        "passed"
        if good == len(report["cases"])
        else "partial"
        if good
        else "from_other_key"
        if outcomes["from_other_key"] == len(report["cases"])
        else "blocked"
    )
    report["blocked_hosts"] = sorted(blocked_hosts)
    report["budget_reserved_this_month"] = client.budget_usage()
    report["code_sha256"] = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in (
            ("pipeline/check_twogis.py", Path(__file__)),
            ("connectors/twogis.py", Path(__file__).parents[1] / "connectors/twogis.py"),
        )
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--departure-at", type=datetime.fromisoformat)
    parser.add_argument(
        "--recheck-other-key",
        action="store_true",
        help="повторить запросы, ответ на которые в кеше получен другим ключом (с расходом)",
    )
    args = parser.parse_args(argv)
    departure = args.departure_at or (
        datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=1)
    ).replace(hour=12, minute=0, second=0, microsecond=0)
    try:
        requests = cases(departure)
    except ValueError:
        parser.error("--departure-at требует часовой пояс")
    if args.report.resolve() == args.env_file.resolve() or args.report.resolve().is_relative_to(
        args.cache.resolve()
    ):
        parser.error("отчёт должен находиться вне файла ключа и кеша")
    if not args.execute:
        report = {
            "version": VERSION,
            "status": "planned",
            "network_attempts": 0,
            "cases": [
                {"action": action, "params": params, "maximum_units": units(action, params)}
                for action, params in requests
            ],
        }
    else:
        try:
            key, key_source = load_key(args.env_file)
        except (OSError, ValueError):
            parser.error("не удалось прочитать TWOGIS_API_KEY из файла окружения")
        if not key:
            parser.error("TWOGIS_API_KEY не задан в окружении или --env-file")
        report = run_checks(
            TwoGISClient(args.cache, key=key), departure, recheck_other_key=args.recheck_other_key
        )
        report["key_source"] = key_source
        if key in json.dumps(report, ensure_ascii=False):
            raise RuntimeError("Credential redaction check failed; report not written")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report["status"], "network_attempts": report["network_attempts"]}))
    return 0 if report["status"] in {"passed", "planned"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
