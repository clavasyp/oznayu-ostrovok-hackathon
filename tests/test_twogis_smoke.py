import http.client
import io
import json
import ssl
import sys
import traceback
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest

from connectors.twogis import (
    DEFAULT_CACHE,
    NoRedirect,
    TwoGISClient,
    TwoGISConnector,
    TwoGISError,
    fingerprint,
)
from pipeline.check_twogis import load_key, main, run_checks

DEPARTURE = datetime.fromisoformat("2026-10-28T12:00:00+03:00")


def response(method, endpoint, query, body):
    if "rubric/search" in endpoint:
        return {"result": {"items": [{"id": "162", "name": "Рестораны"}]}}
    if "3.0/items" in endpoint:
        return {
            "result": {
                "total": 2,
                "items": [
                    {"id": "1", "point": {"lat": 55.7, "lon": 37.6}, "rubrics": [{"id": "162"}]}
                ],
            }
        }
    if "public_transport" in endpoint:
        return [
            {
                "id": "1",
                "route_id": "r",
                "pedestrian": False,
                "transport": ["metro"],
                "total_duration": 600,
                "transfer_count": 1,
                "crossing_count": 0,
            }
        ]
    if "get_dist_matrix" in endpoint:
        return {
            "routes": [
                {"source_id": 0, "target_id": 1, "status": "OK", "distance": 400, "duration": 300}
            ]
        }
    return {"status": "OK", "result": [{"total_distance": 400, "total_duration": 300}]}


def test_env_file_reads_only_key_and_environment_has_priority(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TWOGIS_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text('OTHER_SECRET=not-read\nexport TWOGIS_API_KEY="local-demo" # comment\n')
    assert load_key(path) == ("local-demo", "env_file")
    assert load_key(tmp_path / "missing.env") == ("", None)
    # Пустая переменная окружения не перекрывает ключ из файла.
    monkeypatch.setenv("TWOGIS_API_KEY", "")
    assert load_key(path) == ("local-demo", "env_file")
    monkeypatch.setenv("TWOGIS_API_KEY", "local-demo")
    assert load_key(path) == ("local-demo", "environment")
    assert capsys.readouterr().err == ""
    # Разные значения: берётся окружение, в stderr — какой источник, без значений.
    monkeypatch.setenv("TWOGIS_API_KEY", "environment-demo")
    assert load_key(path) == ("environment-demo", "environment")
    err = capsys.readouterr().err
    assert "окружения" in err and "local-demo" not in err and "environment-demo" not in err


def test_env_file_is_not_executed(tmp_path, monkeypatch):
    monkeypatch.delenv("TWOGIS_API_KEY", raising=False)
    path = tmp_path / ".env"
    marker = tmp_path / "must-not-exist"
    path.write_text(f'TWOGIS_API_KEY="$(touch {marker})"\n')
    assert load_key(path)[0].startswith("$(touch")
    assert not marker.exists()


def test_default_cache_is_persistent_and_ignored():
    root = Path(__file__).resolve().parents[1]
    assert DEFAULT_CACHE == root / "data" / "twogis_cache"
    assert "data/twogis_cache/" in (root / ".gitignore").read_text().splitlines()


def test_plan_never_reads_credentials_or_connects(tmp_path, monkeypatch):
    import pipeline.check_twogis as smoke

    monkeypatch.setattr(smoke, "load_key", lambda *args: pytest.fail("credential read"))
    report = tmp_path / "report.json"
    assert main(["--cache", str(tmp_path / "cache"), "--report", str(report)]) == 0
    data = json.loads(report.read_text())
    assert data["status"] == "planned" and data["network_attempts"] == 0
    assert len(data["cases"]) == 5 and not (tmp_path / "cache").exists()


def test_five_services_normalization_cache_replay_and_budget(tmp_path):
    client = TwoGISClient(tmp_path, key="test-credential", transport=response)
    report = run_checks(client, DEPARTURE)
    assert report["status"] == "passed"
    assert report["network_attempts"] == 5 and report["http_responses"] == 5
    assert all(case["cache_replay_verified"] for case in report["cases"])
    assert report["budget_reserved_this_month"] == {
        "categories": 1,
        "places": 1,
        "routing": 2,
        "matrix": 1,
    }
    assert "test-credential" not in json.dumps(report)
    assert report["cases"][1]["summary"]["collection_complete"] is False
    assert report["cases"][3]["summary"]["samples"][0]["transfers"] == 1
    again = run_checks(client, DEPARTURE)
    assert again["status"] == "passed" and again["network_attempts"] == 0
    assert again["budget_reserved_this_month"] == report["budget_reserved_this_month"]


def test_failed_host_is_not_retried_by_other_cases(tmp_path):
    def fail(*args):
        raise TwoGISError("timeout", kind="network_timeout")

    report = run_checks(TwoGISClient(tmp_path, key="test-credential", transport=fail), DEPARTURE)
    assert report["status"] == "blocked" and report["http_responses"] == 0
    assert report["network_attempts"] == 2
    assert [case["status"] for case in report["cases"]] == [
        "failed",
        "skipped",
        "failed",
        "skipped",
        "skipped",
    ]
    assert report["budget_reserved_this_month"] == {"categories": 1, "routing": 1}


def test_access_denied_is_reported_separately_from_network_failure(tmp_path):
    client = TwoGISClient(
        tmp_path, key="test-credential", transport=lambda *args: {"meta": {"code": 403}}
    )
    report = run_checks(client, DEPARTURE)
    assert report["status"] == "blocked" and report["http_responses"] == 5
    assert all(case["api_status"] == 403 for case in report["cases"])
    assert report["blocked_hosts"] == []


def test_naive_departure_and_report_overwriting_key_are_rejected(tmp_path):
    for extra in (
        ["--departure-at", "2026-10-28T12:00:00"],
        ["--env-file", str(tmp_path / "report.json")],
    ):
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--cache",
                    str(tmp_path / "cache"),
                    "--report",
                    str(tmp_path / "report.json"),
                    *extra,
                ]
            )
        assert exc.value.code == 2
    assert not (tmp_path / "report.json").exists()


def test_cache_does_not_prove_access_for_a_different_key(tmp_path):
    calls = []

    def transport(*args):
        calls.append(1)
        return response(*args)

    params = {"q": "кафе", "region_id": 32}
    a = TwoGISClient(tmp_path, key="account-a", transport=transport)
    b = TwoGISClient(tmp_path, key="account-b", transport=transport)
    record = a.request("categories", params)
    assert record["source"]["account_fingerprint"] == fingerprint("account-a")
    # Кеш общий: другой ключ и сборщик без ключа читают ответ без нового расхода.
    assert b.request("categories", params) == record and len(calls) == 1
    offline = TwoGISClient(tmp_path, key="", transport=lambda *a: pytest.fail("network"))
    assert offline.request("categories", params) == record
    assert a.cache_path("categories", params) == b.cache_path("categories", params)
    # Но проверка доступа не засчитывает ответ другого ключа и не тратит лимит молча.
    run_checks(a, DEPARTURE)
    calls.clear()
    other = run_checks(b, DEPARTURE)
    assert other["status"] == "from_other_key" and not calls
    assert {case["status"] for case in other["cases"]} == {"from_other_key"}
    assert other["budget_reserved_this_month"] == {}
    # С явным флагом — свежие запросы новым ключом; запись в кеше получает его отпечаток.
    again = run_checks(b, DEPARTURE, recheck_other_key=True)
    assert again["status"] == "passed" and again["network_attempts"] == 5 and len(calls) == 5
    assert all(case["from_cache"] is False for case in again["cases"])
    assert again["cases"][0]["source"]["account_fingerprint"] == fingerprint("account-b")
    assert "account-a" not in json.dumps(other) and "account-b" not in json.dumps(again)


def test_credential_in_request_is_rejected_before_cache_or_reservation(tmp_path):
    cache = tmp_path / "cache"
    client = TwoGISClient(
        cache, key="test-credential", transport=lambda *args: pytest.fail("network")
    )
    with pytest.raises(ValueError, match="credential"):
        client.request("categories", {"q": "test-credential"})
    assert not cache.exists()


def test_timeout_exception_never_echoes_request_key(tmp_path, monkeypatch):
    class Fail:
        def open(self, *args, **kwargs):
            raise urllib.error.URLError(TimeoutError("test-credential"))

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Fail())
    with pytest.raises(TwoGISError) as exc:
        TwoGISClient(tmp_path, key="test-credential").request("categories", {"q": "кафе"})
    assert exc.value.kind == "network_timeout"
    assert "test-credential" not in str(exc.value)


def test_connector_dataset_ref_matches_cache_record(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("TWOGIS_API_KEY", "test-credential")
    monkeypatch.setattr(TwoGISClient, "_http", staticmethod(response))
    context = SimpleNamespace(
        params={"twogis_requests": [{"action": "categories", "params": {"q": "Рестораны"}}]}
    )
    (ref,) = TwoGISConnector().fetch(context, tmp_path)
    assert ref.path.is_file()


def test_matrix_failed_payload_is_not_a_successful_check(tmp_path):
    def transport(method, endpoint, query, body):
        if "get_dist_matrix" in endpoint:
            return {"routes": [{"source_id": 0, "target_id": 1, "status": "FAIL"}]}
        return response(method, endpoint, query, body)

    report = run_checks(
        TwoGISClient(tmp_path, key="test-credential", transport=transport), DEPARTURE
    )
    assert report["status"] == "partial"
    assert report["cases"][-1]["error_kind"] == "response_validation"


class Opener:
    """Подмена urllib: open() бросает исключение или отдаёт ответ с заданным телом."""

    def __init__(self, behaviour):
        self.behaviour = behaviour

    def open(self, request, timeout=None):
        return self.behaviour(request)


class Response(io.BytesIO):
    status = 200

    def __init__(self, body=b"", error=None):
        super().__init__(body)
        self.error = error

    def read(self, *args):
        if self.error:
            raise self.error
        return super().read(*args)


def test_http_error_url_with_key_is_not_kept_in_exception_chain(tmp_path, monkeypatch):
    body, seen, key = io.BytesIO(b"{}"), [], "-".join(("live", "looking", "key"))

    def forbidden(request):
        seen.append(urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, body))
        raise seen[-1]

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener(forbidden))
    with pytest.raises(TwoGISError) as caught:
        TwoGISClient(tmp_path, key=key).request("categories", {"q": "кафе"})
    assert "key=" + key in seen[0].url  # реальный путь утечки — URL запроса
    error = caught.value
    assert error.kind == "http" and error.http_status == 403
    assert error.__context__ is None and error.__cause__ is None and body.closed
    assert key not in "".join(traceback.format_exception(error))


def raise_in_open(error):
    def behaviour(request):
        raise error

    return behaviour


@pytest.mark.parametrize(
    ("behaviour", "kind"),
    [
        (raise_in_open(http.client.RemoteDisconnected("closed without response")), "network"),
        (raise_in_open(ConnectionResetError(54, "Connection reset by peer")), "network"),
        (lambda r: Response(error=http.client.IncompleteRead(b'{"result":', 490)), "network"),
        (lambda r: Response(error=ConnectionResetError(54, "reset")), "network"),
        (lambda r: Response(error=ssl.SSLError(1, "[SSL] bad record mac")), "tls"),
        (lambda r: Response(error=TimeoutError("The read operation timed out")), "network_timeout"),
        (
            raise_in_open(
                urllib.error.URLError(
                    TimeoutError("_ssl.c:1011: The handshake operation timed out")
                )
            ),
            "tls_timeout",
        ),
        (raise_in_open(urllib.error.URLError(TimeoutError("timed out"))), "network_timeout"),
        (lambda r: Response(b'{"a": "\xff"}'), "invalid_response"),
        (lambda r: Response(b"<html>captive portal</html>"), "invalid_response"),
    ],
)
def test_raw_failures_after_sending_are_classified(tmp_path, monkeypatch, behaviour, kind):
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener(behaviour))
    with pytest.raises(TwoGISError) as caught:
        TwoGISClient(tmp_path, key="test-credential").request("categories", {"q": "кафе"})
    assert caught.value.kind == kind and caught.value.__context__ is None
    assert caught.value.http_status == (200 if kind == "invalid_response" else None)
    assert "test-credential" not in str(caught.value)


def test_dropped_connection_still_writes_report_and_stops_host(tmp_path, monkeypatch):
    def dropped(request):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener(dropped))
    report = run_checks(TwoGISClient(tmp_path, key="test-credential"), DEPARTURE)
    assert report["status"] == "blocked" and report["network_attempts"] == 2
    assert [(c["status"], c.get("error_kind")) for c in report["cases"]] == [
        ("failed", "network"),
        ("skipped", None),
        ("failed", "network"),
        ("skipped", None),
        ("skipped", None),
    ]
    assert report["blocked_hosts"] == ["catalog.api.2gis.com", "routing.api.2gis.com"]
    assert "network" in report["cases"][0]["error"]
    assert "test-credential" not in json.dumps(report)


def test_unexpected_error_is_reported_without_key(tmp_path):
    def transport(method, endpoint, query, body):
        if "rubric/search" in endpoint:
            raise LookupError("boom https://x/?key=" + query["key"] + " " + query["key"])
        return response(method, endpoint, query, body)

    report = run_checks(
        TwoGISClient(tmp_path, key="test-credential", transport=transport), DEPARTURE
    )
    assert report["status"] == "partial" and report["blocked_hosts"] == []
    first = report["cases"][0]
    assert first["status"] == "failed" and first["error_kind"] == "unexpected"
    assert first["error"].startswith("LookupError: boom")
    assert "test-credential" not in json.dumps(report)


def test_refusal_reasons_have_distinct_kinds(tmp_path):
    client = TwoGISClient(tmp_path, key="test-credential", limit=1, transport=response)
    transit = run_checks(client, DEPARTURE)["cases"][3]
    assert transit["error_kind"] == "budget" and "budget exhausted" in transit["error"]
    with pytest.raises(TwoGISError) as caught:
        client.request("matrix_async", {"points": [{}, {}], "sources": [0], "targets": [1]})
    assert caught.value.kind == "subscription_required"
    path = client.cache_path("categories", {"q": "Рестораны", "region_id": 32, "page_size": 5})
    record = json.loads(path.read_text())
    path.write_text(json.dumps(record | {"request": {"action": "categories", "params": {}}}))
    with pytest.raises(TwoGISError) as caught:
        client.request("categories", {"q": "Рестораны", "region_id": 32, "page_size": 5})
    assert caught.value.kind == "cache_mismatch"
    fp = io.BytesIO(b"")
    with pytest.raises(TwoGISError) as caught:
        NoRedirect().redirect_request(None, fp, 302, "Found", {}, "https://example.invalid/")
    assert caught.value.kind == "redirect" and caught.value.http_status == 302 and fp.closed


def test_execute_reads_key_file_writes_report_without_key(tmp_path, monkeypatch):
    key = "real-looking-key-123"
    calls = []

    def echo(self, method, endpoint, query, body):
        calls.append(endpoint)
        out = response(method, endpoint, query, body)
        if isinstance(out, dict):
            out |= {"echo": "https://x/?key=" + query["key"], "meta": {"code": 200, "key": key}}
        return out

    monkeypatch.setattr(TwoGISClient, "_http", echo)
    monkeypatch.setenv("TWOGIS_API_KEY", "")  # пустое окружение не перекрывает файл
    env = tmp_path / ".env"
    env.write_text(f'TWOGIS_API_KEY="{key}"\n')
    cache, report = tmp_path / "cache", tmp_path / "report.json"
    argv = ["--execute", "--env-file", str(env), "--cache", str(cache), "--report", str(report)]
    assert main(argv) == 0 and len(calls) == 5
    text = report.read_text()
    data = json.loads(text)
    assert data["status"] == "passed" and data["key_source"] == "env_file"
    assert data["key_tier"] == "demo_key" and "mode" not in data
    assert key not in text
    assert not any(key in path.read_text() for path in cache.iterdir() if path.is_file())


def test_execute_refuses_to_write_report_with_key(tmp_path, monkeypatch):
    import pipeline.check_twogis as smoke

    monkeypatch.setenv("TWOGIS_API_KEY", "test-credential")
    monkeypatch.setattr(
        smoke, "run_checks", lambda *args, **kwargs: {"status": "passed", "x": "test-credential"}
    )
    report = tmp_path / "report.json"
    with pytest.raises(RuntimeError, match="redaction"):
        main(["--execute", "--cache", str(tmp_path / "cache"), "--report", str(report)])
    assert not report.exists()


def test_collector_reads_env_file_only_for_execute(tmp_path, monkeypatch):
    import pipeline.collect_twogis as collector

    seen = []
    monkeypatch.setattr(collector, "collect", lambda *args, **kwargs: seen.append(kwargs) or {})
    monkeypatch.delenv("TWOGIS_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("TWOGIS_API_KEY=file-credential\n")
    base = ["collect_twogis", "--pack", "p", "--rubrics", "r", "--out", "o", "--env-file", str(env)]
    monkeypatch.setattr(sys, "argv", base)
    collector.main()
    monkeypatch.setattr(sys, "argv", [*base, "--execute"])
    collector.main()
    assert [kwargs["key"] for kwargs in seen] == [None, "file-credential"]
