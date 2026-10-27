"""Готовые слои D34/D39. На запросе нет скачивания данных и построения графа."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from core.geo_store import NEARBY_RADIUS_M, GeoStore, place_categories

router = APIRouter(prefix="/api")


def geo(request: Request, hotel_id: str | None = None) -> GeoStore:
    store = request.app.state.store
    if getattr(store, "stub", True):
        raise HTTPException(503, "Геослои доступны после загрузки пакетов в режиме db")
    if hotel_id is not None and store.hotel(hotel_id) is None:
        raise HTTPException(404, "Отель не найден")
    return GeoStore(store.engine)


@router.get("/hotels/{hotel_id}/geometry")
def geometry(request: Request, hotel_id: str):
    return geo(request, hotel_id).geometry(hotel_id)


@router.get("/hotels/{hotel_id}/nearby-places")
def nearby(
    request: Request,
    hotel_id: str,
    category: str | None = None,
    radius_m: float = Query(NEARBY_RADIUS_M, gt=0, le=NEARBY_RADIUS_M),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    store = geo(request, hotel_id)
    allowed = place_categories()
    if category is not None and category not in allowed:
        # Ошибка клиента — не «мест нет»: иначе опечатка выглядит как отсутствие данных.
        raise HTTPException(
            422,
            f"Неизвестная категория места «{category}». Допустимые: {', '.join(allowed)}. "
            "Id словаря мест (agents/catalog/places.json) или характеристики каталога: "
            "L01 — аптека, L03 — супермаркет и т. д.",
        )
    return store.nearby(hotel_id, category, radius_m, limit, offset)


@router.get("/hotels/{hotel_id}/stations")
def stations(request: Request, hotel_id: str):
    return geo(request, hotel_id).stations(hotel_id)


@router.get("/cities/{city_id}/transit-times")
def transit(
    request: Request,
    city_id: str,
    source_station_id: str,
    target_station_id: str,
    departure_at: datetime,
    profile: str = "metro",
):
    if departure_at.tzinfo is None:
        raise HTTPException(422, "Дата отправления должна содержать часовой пояс")
    store = geo(request)
    if city_id not in {c["city_id"] for c in request.app.state.store.cities()}:
        raise HTTPException(404, "Город не найден")
    return store.transit(city_id, source_station_id, target_station_id, departure_at, profile)
