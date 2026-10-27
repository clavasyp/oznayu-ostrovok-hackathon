"""Официальные API 2ГИС, D37: ключ из окружения, кеш и постоянный месячный бюджет.

Коннектор запускается только сборщиком. Без ключа сеть не используется. Деморежим
ограничен проверками (20 единиц на сервис), массовый сбор требует subscription=True.
Резервируем расход ДО запроса: ошибка/таймаут не разрешают бесплатно повторить вызов.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import math
import os
import re
import shlex
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from agents.catalog.places import load_places
from core.packs import sha256_file
from core.plugins import CityContext, DatasetRef, register_connector

VERSION = "twogis.3"
# Постоянный каталог кеша и журнала расхода: общий для проверки и сборщика, вне git и образа.
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data" / "twogis_cache"
ENDPOINTS = {
    "places": ("GET", "https://catalog.api.2gis.com/3.0/items", "places"),
    "categories": ("GET", "https://catalog.api.2gis.com/2.0/catalog/rubric/search", "categories"),
    "routing": ("POST", "https://routing.api.2gis.com/routing/7.0.0/global", "routing"),
    "transit": ("POST", "https://routing.api.2gis.com/public_transport/2.0", "routing"),
    "matrix": ("POST", "https://routing.api.2gis.com/get_dist_matrix", "matrix"),
    "matrix_async": (
        "POST",
        "https://routing.api.2gis.com/async_matrix/create_task/get_dist_matrix",
        "matrix",
    ),
    "matrix_status": (
        "GET",
        "https://routing.api.2gis.com/async_matrix/result/get_dist_matrix/{task_id}",
        "matrix",
    ),
}


class TwoGISError(RuntimeError):
    """kind: dns, network, network_timeout, tls, tls_timeout, http, api_status, redirect,
    invalid_response, budget, subscription_required, cache_mismatch, configuration."""

    def __init__(self, message, *, kind="connector", http_status=None, api_status=None):
        super().__init__(message)
        self.kind, self.http_status, self.api_status = kind, http_status, api_status


def fingerprint(key: str) -> str:
    """Усечённый SHA256 ключа: имя журнала расхода и отметка, каким ключом получен ответ."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _key_from_file(env_file: Path | None) -> str:
    if not env_file or not env_file.is_file():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?TWOGIS_API_KEY\s*=(.*)", line)
        if match:
            values = shlex.split(match[1], comments=True)
            if len(values) > 1:
                raise ValueError("TWOGIS_API_KEY must be a single quoted or unquoted value")
            return values[0] if values else ""
    return ""


def load_key(env_file: Path | None) -> tuple[str, str | None]:
    """Ключ и его источник: environment, env_file или None.

    Непустое окружение имеет приоритет; пустое `TWOGIS_API_KEY=` файл не перекрывает.
    Из файла читается только TWOGIS_API_KEY, без shell eval. Если оба источника заданы
    и различаются, в stderr пишется, какой взят, — без значений.
    """
    environment = os.environ.get("TWOGIS_API_KEY", "")
    if environment:
        try:
            other = _key_from_file(env_file)
        except (OSError, ValueError):
            other = ""
        if other and other != environment:
            print(
                f"TWOGIS_API_KEY в окружении и в {env_file} различаются; взят ключ из окружения",
                file=sys.stderr,
            )
        return environment, "environment"
    value = _key_from_file(env_file)
    return value, "env_file" if value else None


def network_kind(reason) -> str:
    """Вид сетевого сбоя; зависание TLS-рукопожатия отделяем от таймаута TCP."""
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, TimeoutError):
        # CPython: «_ssl.c:…: The handshake operation timed out» — TCP принят, TLS нет.
        if "handshake operation timed out" in str(reason):
            return "tls_timeout"
        return "network_timeout"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    return "network"


def scrub(value):
    """Отзывы, фотографии и возможный отражённый API-ключ не сохраняются даже в кеше."""
    if isinstance(value, dict):
        return {
            k: scrub(v)
            for k, v in value.items()
            if not any(t in k.casefold() for t in ("review", "photo", "apikey", "api_key"))
            and k.casefold() != "key"
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"([?&]key=)[^&\s]+", r"\1[redacted]", value)
    return value


def units(action: str, params: dict) -> int:
    if action == "matrix_status":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", params.get("task_id", "")):
            raise ValueError("Invalid asynchronous matrix task ID")
        return 0
    if action.startswith("matrix"):
        points, sources, targets = (
            params.get("points", []),
            params.get("sources", []),
            params.get("targets", []),
        )
        if (
            not sources
            or not targets
            or any(type(i) is not int or not 0 <= i < len(points) for i in sources + targets)
        ):
            raise ValueError("Matrix requires valid nonempty source/target indices")
        if action == "matrix" and (len(sources) > 25 or len(targets) > 25):
            raise ValueError("Use asynchronous matrix above 25 sources or targets")
        return len(sources) * len(targets)
    if action == "routing":
        # Поддерживаем только одну пару за вызов: биллинг однозначен.
        if len(params.get("points", [])) != 2:
            raise ValueError("Routing smoke calls require exactly two points")
    if action == "transit":
        if (
            not params.get("source")
            or not params.get("target")
            or type(params.get("start_time")) is not int
        ):
            raise ValueError("Transit needs source, target and explicit Unix departure time")
    return 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise TwoGISError(
            "Unexpected API redirect; key not forwarded", kind="redirect", http_status=code
        )


class TwoGISClient:
    def __init__(
        self, cache: Path, *, key: str | None = None, subscription=False, limit=20, transport=None
    ):
        self.key = key if key is not None else os.environ.get("TWOGIS_API_KEY", "")
        self.cache, self.subscription, self.limit = Path(cache), subscription, limit
        if limit < 1 or (not subscription and limit > 20):
            raise ValueError("Demo connector checks are limited to 20 units per service/month")
        self.transport = transport or self._http

    def _http(self, method, endpoint, query, body):
        context = ssl.create_default_context(
            cafile="/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").is_file() else None
        )
        opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPSHandler(context=context)
        )
        request = urllib.request.Request(
            endpoint + "?" + urllib.parse.urlencode(query),
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        status = None
        # TwoGISError поднимается после блоков except: исходное исключение (у HTTPError в URL
        # есть ключ) не попадает в __context__.
        try:
            with opener.open(request, timeout=30) as response:
                status = response.status
                if status == 204:
                    return {"status": "no_route"}
                return json.load(response)
        except urllib.error.HTTPError as exc:
            kind, status = "http", exc.code
            exc.close()
        except urllib.error.URLError as exc:
            kind, status = network_kind(exc.reason), None
        except (OSError, http.client.HTTPException) as exc:
            # Сбой после отправки запроса: RemoteDisconnected, RST, IncompleteRead, SSLError.
            kind, status = network_kind(exc), None
        except (json.JSONDecodeError, UnicodeDecodeError):
            kind = "invalid_response"
        if kind == "http":
            message = f"2GIS HTTP {status}; inspect account/service access"
        elif kind == "invalid_response":
            message = "2GIS returned invalid JSON; budget reservation kept"
        else:
            message = f"2GIS connection failed ({kind}); budget reservation kept"
        raise TwoGISError(message, kind=kind, http_status=status)

    def cache_path(self, action, params):
        # Ключ в идентичность не входит: сохранённый ответ воспроизводится без ключа и
        # с новым ключом без повторного расхода. Каким ключом он получен — в source.
        identity = json.dumps([VERSION, action, params], sort_keys=True, separators=(",", ":"))
        return self.cache / (hashlib.sha256(identity.encode()).hexdigest() + ".json")

    def _reserve(self, service: str, count: int):
        self.cache.mkdir(parents=True, exist_ok=True)
        # Один lock и ledger на ключ: процессы/города не получают каждый отдельный лимит.
        account = fingerprint(self.key)
        path = self.cache / ("budget-" + account + ".json")
        with (self.cache / ("budget-" + account + ".lock")).open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = json.loads(path.read_text()) if path.exists() else {}
            month = datetime.now(UTC).strftime("%Y-%m")
            bucket = ledger.setdefault(month, {})
            consumed = bucket.get(service, 0)
            if consumed + count > self.limit:
                raise TwoGISError(f"Local monthly {service} budget exhausted", kind="budget")
            bucket[service] = consumed + count
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(ledger, sort_keys=True) + "\n")
            temporary.replace(path)

    def budget_usage(self):
        path = self.cache / ("budget-" + fingerprint(self.key) + ".json")
        ledger = json.loads(path.read_text()) if path.exists() else {}
        return ledger.get(datetime.now(UTC).strftime("%Y-%m"), {})

    def request(self, action: str, params: dict, *, refresh=False) -> dict:
        """refresh=True — запрос мимо кеша (с расходом); ответ заменяет запись в кеше."""
        if action not in ENDPOINTS or "key" in params:
            raise ValueError("Unsupported API action or key in request body")
        if self.key and self.key in json.dumps(params, ensure_ascii=False):
            raise ValueError("API credential must not appear in request parameters")
        cost = units(action, params)
        method, endpoint, service = ENDPOINTS[action]
        path = self.cache_path(action, params)
        if path.exists() and not refresh:
            cached = json.loads(path.read_text())
            if cached["request"] != {"action": action, "params": params}:
                raise TwoGISError("Cache request mismatch", kind="cache_mismatch")
            if action != "matrix_status" or cached["response"].get("status") in {
                "TASK_DONE",
                "TASK_CANCELED",
            }:
                return cached
        if not self.key:
            raise TwoGISError("TWOGIS_API_KEY is missing; no request made", kind="configuration")
        if not self.subscription and action in {"matrix_async", "matrix_status"}:
            raise TwoGISError(
                "Asynchronous bulk matrix requires subscription mode", kind="subscription_required"
            )
        self._reserve(service, cost)
        query = dict(params) if method == "GET" else {}
        if action == "matrix_status":
            endpoint = endpoint.format(task_id=query.pop("task_id"))
        query["key"] = self.key
        if action.startswith("matrix"):
            query["version"] = "2.0"
        payload = self.transport(method, endpoint, query, params if method == "POST" else None)
        code = (payload.get("meta") or {}).get("code", 200) if isinstance(payload, dict) else 200
        if code != 200:
            status = code if type(code) is int else None
            raise TwoGISError(
                f"2GIS API status {status}; response not published",
                kind="api_status",
                api_status=status,
            )
        # Ключ может быть отражён в любом строковом поле, а не только в URL.
        clean = json.loads(
            json.dumps(scrub(payload), ensure_ascii=False).replace(self.key, "[redacted]")
        )
        record = {
            "request": {"action": action, "params": params},
            "response": clean,
            "source": {
                "provider": "2gis",
                "url": endpoint,
                "fetched_at": datetime.now(UTC).isoformat(),
                "commercial_ok": False,
                "license": "D37 hackathon use; no commercial permission",
                "reserved_units": cost,
                "account_fingerprint": fingerprint(self.key),
                "cache_sanitized": True,
                "response_sha256": hashlib.sha256(
                    json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
                "algorithm_version": VERSION,
            },
        }
        self.cache.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        temporary.replace(path)
        return record

    def places(self, params: dict, *, max_pages=2) -> dict:
        if max_pages < 1 or (not self.subscription and max_pages > 2):
            raise ValueError("Demo permits at most two pages per check")
        items, total, sources = {}, None, []
        for page in range(1, max_pages + 1):
            result = self.request(
                "places",
                params
                | {
                    "page": page,
                    "page_size": 50,
                    "fields": "items.point,items.rubrics,items.schedule",
                },
            )
            rows = result["response"]["result"]
            total = rows.get("total")
            sources.append(result["source"])
            for item in rows.get("items", []):
                items[str(item["id"])] = item
            if total is not None and page * 50 >= total:
                break
            if not rows.get("items"):
                break
        return {
            "items": list(items.values()),
            "total_reported": total,
            "complete": total is not None and len(items) >= total,
            "sources": sources,
        }


def grid_plan(hotels: list[dict], *, size_m=2000) -> list[dict]:
    if not hotels or size_m <= 0:
        return []
    lat = (min(h["latitude"] for h in hotels) + max(h["latitude"] for h in hotels)) / 2
    xscale, yscale = 111320 * math.cos(math.radians(lat)), 111320
    cells = {}
    for h in hotels:
        cell = (
            math.floor(h["longitude"] * xscale / size_m),
            math.floor(h["latitude"] * yscale / size_m),
        )
        cells.setdefault(cell, []).append(h)
    result = []
    for (x, y), group in sorted(cells.items()):
        lon, lat = (x + 0.5) * size_m / xscale, (y + 0.5) * size_m / yscale
        farthest = 0
        for h in group:
            a, b = math.radians(lat), math.radians(h["latitude"])
            hav = (
                math.sin((b - a) / 2) ** 2
                + math.cos(a) * math.cos(b) * math.sin(math.radians(h["longitude"] - lon) / 2) ** 2
            )
            farthest = max(farthest, 6371008.8 * 2 * math.asin(min(1, math.sqrt(hav))))
        result.append(
            {"point": f"{lon:.7f},{lat:.7f}", "radius": math.ceil(1500 + farthest * 1.01 + 1)}
        )
    return result


def transit_rows(result: dict, *, city: str, source_id: str, target_id: str) -> list[dict]:
    """Сохраняет конкретный контекст отправления; pedestrian-only не становится метро."""
    request = result["request"]
    if request["action"] != "transit":
        raise ValueError("Expected public transport response")
    departure = datetime.fromtimestamp(request["params"]["start_time"], UTC).isoformat()
    routes = result["response"]
    rows = []
    for route in routes if isinstance(routes, list) else []:
        if (
            route.get("pedestrian")
            or not route.get("transport")
            or route.get("total_duration") is None
        ):
            continue
        if any(route.get(k) is None for k in ("transfer_count", "crossing_count")):
            continue
        context = [
            city,
            source_id,
            target_id,
            departure,
            request["params"].get("transport"),
            route.get("route_id"),
            route.get("id"),
            route.get("total_duration"),
            route.get("transfer_count"),
            route.get("crossing_count"),
        ]
        rows.append(
            {
                "id": hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:24],
                "city_id": city,
                "source_station_id": source_id,
                "target_station_id": target_id,
                "departure_at": departure,
                "profile": ",".join(request["params"]["transport"]),
                "duration_s": route["total_duration"],
                "transfers": route["transfer_count"] + route["crossing_count"],
                "source": result["source"]
                | {
                    "transfer_count": route["transfer_count"],
                    "crossing_count": route["crossing_count"],
                    "transport": route["transport"],
                    "status": "estimate",
                    "schedule_enabled": request["params"].get("enable_schedule", False),
                },
            }
        )
    return rows


def place_rows(collection: dict, city: str, rubric_categories: dict[str, list[str]]) -> list[dict]:
    """Только явные рубрики. Совпадение поисковой строки не доказывает кухню заведения."""
    known = load_places().categories
    if any(c not in known for cats in rubric_categories.values() for c in cats):
        raise ValueError("Unknown place category in rubric mapping")
    rows = []
    for item in collection["items"]:
        point = item.get("point") or {}
        lon, lat = point.get("lon"), point.get("lat")
        if not isinstance(lon, int | float) or not isinstance(lat, int | float):
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        categories = sorted(
            {c for r in item.get("rubrics", []) for c in rubric_categories.get(str(r["id"]), [])}
        )
        rows.append(
            {
                "id": "2gis:" + str(item["id"]),
                "city_id": city,
                "name": item.get("name"),
                "latitude": lat,
                "longitude": lon,
                "categories": categories,
                "tags": {
                    "rubrics": item.get("rubrics", []),
                    "schedule": item.get("schedule"),
                    "address": item.get("address_name"),
                },
                "source": {
                    "provider": "2gis",
                    "url": "https://catalog.api.2gis.com/3.0/items",
                    "pages": collection["sources"],
                    "commercial_ok": False,
                    "query_complete": collection["complete"],
                    "rubric_mapping": rubric_categories,
                },
            }
        )
    return rows


@register_connector
class TwoGISConnector:
    id = "twogis"
    source_id = "2gis"
    layers = ("places", "categories", "routing", "transit", "matrix")

    def available(self, ctx: CityContext) -> bool:
        return bool(os.environ.get("TWOGIS_API_KEY"))

    def fetch(self, ctx: CityContext, workdir: Path) -> list[DatasetRef]:
        client = TwoGISClient(workdir)
        result = []
        requests = ctx.params.get("twogis_requests", [])
        if len(requests) > 5:
            raise ValueError("Connector demo supports at most five smoke requests")
        for item in requests:
            response = client.request(item["action"], item["params"])
            path = client.cache_path(item["action"], item["params"])
            result.append(
                DatasetRef(
                    "2gis:" + item["action"],
                    path,
                    "2gis",
                    version=response["source"]["fetched_at"],
                    sha256=sha256_file(path),
                    license="D37; commercial_ok=false",
                )
            )
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=ENDPOINTS, required=True)
    parser.add_argument(
        "--request", type=Path, required=True, help="JSON parameters without API key"
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--execute", action="store_true", help="Without this flag only estimates cost"
    )
    parser.add_argument(
        "--subscription", action="store_true", help="Existing paid API subscription"
    )
    parser.add_argument("--monthly-unit-limit", type=int, default=20)
    args = parser.parse_args()
    params = json.loads(args.request.read_text())
    print(json.dumps({"action": args.action, "reserved_units": units(args.action, params)}))
    if args.execute:
        try:
            key, _ = load_key(args.env_file)
        except (OSError, ValueError):
            parser.error("не удалось прочитать TWOGIS_API_KEY из файла окружения")
        result = TwoGISClient(
            args.cache, key=key, subscription=args.subscription, limit=args.monthly_unit_limit
        ).request(args.action, params)
        print(json.dumps(result["source"], ensure_ascii=False))


if __name__ == "__main__":
    main()
