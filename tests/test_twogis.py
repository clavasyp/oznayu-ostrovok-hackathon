import json

import pytest

from connectors.twogis import TwoGISClient, TwoGISError, grid_plan, place_rows, transit_rows, units


def test_missing_key_never_calls_network(tmp_path):
    client = TwoGISClient(tmp_path, key="", transport=lambda *a: pytest.fail("network"))
    with pytest.raises(TwoGISError, match="missing"):
        client.request("categories", {"q": "кафе", "region_id": 32})
    assert not list(tmp_path.glob("budget-*"))


def test_cache_redacts_key_photos_reviews_and_does_not_spend_twice(tmp_path):
    calls = []

    def transport(*args):
        calls.append(args)
        return {
            "meta": {"code": 200},
            "result": {
                "items": [{"id": "1", "photos": ["x"], "reviews": ["y"]}],
                "echo": "secret-value",
            },
        }

    client = TwoGISClient(tmp_path, key="secret-value", limit=1, transport=transport)
    a = client.request("categories", {"q": "кафе"})
    assert a == client.request("categories", {"q": "кафе"})
    assert len(calls) == 1
    text = "".join(p.read_text() for p in tmp_path.glob("*.json"))
    assert "secret-value" not in text and "photos" not in text and "reviews" not in text
    with pytest.raises(TwoGISError, match="budget"):
        client.request("categories", {"q": "ресторан"})
    assert len(calls) == 1


def test_matrix_pairs_and_failed_calls_keep_reservation(tmp_path):
    params = {"points": [{"lat": 1, "lon": 1}] * 4, "sources": [0, 1], "targets": [2, 3]}
    assert units("matrix", params) == 4
    client = TwoGISClient(
        tmp_path, key="test", limit=3, transport=lambda *a: pytest.fail("network")
    )
    with pytest.raises(TwoGISError, match="budget"):
        client.request("matrix", params)
    failing = TwoGISClient(
        tmp_path, key="other", limit=1, transport=lambda *a: {"meta": {"code": 403}}
    )
    with pytest.raises(TwoGISError, match="status 403"):
        failing.request("places", {"q": "test"})
    with pytest.raises(TwoGISError, match="budget"):
        failing.request("places", {"q": "test"})


def test_large_matrix_uses_async_and_demo_cannot_start_bulk(tmp_path):
    params = {"points": [{"lat": 1, "lon": 1}] * 27, "sources": list(range(26)), "targets": [26]}
    with pytest.raises(ValueError, match="asynchronous"):
        units("matrix", params)
    assert units("matrix_async", params) == 26
    with pytest.raises(TwoGISError, match="subscription"):
        TwoGISClient(tmp_path, key="test").request("matrix_async", params)


def test_pagination_deduplicates_and_reports_truncation(tmp_path):
    def transport(method, endpoint, query, body):
        return {
            "result": {
                "total": 101,
                "items": [
                    {"id": str(i)}
                    for i in range((int(query["page"]) - 1) * 50, int(query["page"]) * 50)
                ],
            }
        }

    result = TwoGISClient(tmp_path, key="test", transport=transport).places({"q": "кафе"})
    assert len(result["items"]) == 100 and result["complete"] is False
    assert len(result["sources"]) == 2


def test_transit_context_and_transfers_are_not_invented():
    result = {
        "request": {
            "action": "transit",
            "params": {
                "source": {},
                "target": {},
                "start_time": 1794614400,
                "transport": ["metro"],
                "enable_schedule": True,
            },
        },
        "source": {"provider": "2gis", "commercial_ok": False},
        "response": [
            {
                "id": 1,
                "route_id": "r",
                "pedestrian": False,
                "transport": ["metro"],
                "total_duration": 600,
                "transfer_count": 1,
                "crossing_count": 2,
            },
            {"id": 2, "pedestrian": True, "total_duration": 300},
            {"id": 3, "pedestrian": False, "transport": ["metro"], "total_duration": 600},
        ],
    }
    rows = transit_rows(result, city="moscow", source_id="a", target_id="b")
    assert len(rows) == 1 and rows[0]["duration_s"] == 600 and rows[0]["transfers"] == 3
    assert rows[0]["departure_at"].endswith("+00:00") and rows[0]["profile"] == "metro"
    assert rows == transit_rows(result, city="moscow", source_id="a", target_id="b")
    with pytest.raises(ValueError, match="explicit"):
        units("transit", {"source": {"point": {}}, "target": {"point": {}}, "transport": ["metro"]})


def test_rubric_mapping_does_not_infer_cuisine_from_name():
    collection = {
        "items": [
            {
                "id": "1",
                "name": "Italian restaurant",
                "point": {"lat": 55.7, "lon": 37.6},
                "rubrics": [{"id": "real-id"}],
            }
        ],
        "sources": [],
        "complete": False,
    }
    assert place_rows(collection, "moscow", {})[0]["categories"] == []
    row = place_rows(collection, "moscow", {"real-id": ["italian"]})[0]
    assert row["categories"] == ["italian"] and row["source"]["commercial_ok"] is False
    assert "reviews" not in json.dumps(row)


def test_grid_is_deterministic_and_avoids_one_query_per_hotel():
    hotels = [{"longitude": 37.6, "latitude": 55.7}] * 10
    assert grid_plan(hotels) == grid_plan(list(reversed(hotels)))
    assert len(grid_plan(hotels)) == 1 and grid_plan(hotels)[0]["radius"] > 1500


def test_async_poll_refreshes_transient_status_but_caches_finished_result(tmp_path):
    calls = []

    def transport(*args):
        calls.append(args)
        return {
            "task_id": "task123",
            "status": "TASK_CREATED" if len(calls) == 1 else "TASK_DONE",
            "result_link": "https://routing.api.2gis.com/result",
        }

    client = TwoGISClient(tmp_path, key="test", subscription=True, transport=transport)
    params = {"task_id": "task123"}
    assert units("matrix_status", params) == 0
    assert client.request("matrix_status", params)["response"]["status"] == "TASK_CREATED"
    done = client.request("matrix_status", params)
    assert done["response"]["status"] == "TASK_DONE"
    assert client.request("matrix_status", params) == done and len(calls) == 2
    with pytest.raises(ValueError, match="task ID"):
        units("matrix_status", {"task_id": "../evil"})
