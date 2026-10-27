"""Размещения категорий со слов отеля (D17): вопрос с выбором корпуса → свободный ответ →
пары «здание × этаж × окна» или уточняющий вопрос. Сохраняет тот же `build_answer` /
`apply_answer`, что и остальные ответы отеля; названия корпусов — в `hotel_building.evidence`.
Ответ имитатора (песочница, `is_simulated`) не заменяет ответ настоящего отеля (D41-8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from agents import hotelier as agent
from agents.hotelier.placement import FEATURE_ID, answer_placement
from core.hotelier import AnswerError, AnswerIn, build_answer
from core.placement_catalog import name_key
from core.store import Store

router = APIRouter(prefix="/api/hotelier")

NO_CATEGORIES = (
    "У отеля нет категорий номеров в пакете — спрашивать о размещении не о чем: "
    "сначала нужен список категорий Островка."
)
NO_BUILDINGS = (
    "У отеля нет корпусов в справочнике: ответ можно разобрать, но размещения не к чему "
    "привязать — они не сохранятся."
)


class PlacementAnswerRequest(BaseModel):
    room_type_id: str
    answer_text: str = Field(min_length=1)
    # Корпуса, отмеченные в списке: как отель называет корпус → building_id из списка.
    building_aliases: dict[str, str] = Field(default_factory=dict)
    # Песочница кабинета и имитатор — true (D41-8): такой ответ не заменяет настоящий.
    is_simulated: bool = False
    respondent: str | None = None

    @field_validator("building_aliases")
    @classmethod
    def _named(cls, aliases: dict[str, str]) -> dict[str, str]:
        if any(not name_key(name) for name in aliases):
            # Пустое название стало бы «подстановкой» для любого ответа без названия корпуса.
            raise ValueError("пустое название корпуса: напишите, как отель называет корпус")
        return aliases


class PlacementSimulateRequest(BaseModel):
    room_type_id: str


def _context(request: Request, hotel_id: str) -> tuple[Store, dict[str, Any], list[dict]]:
    s: Store = request.app.state.store
    hotel = s.hotel(hotel_id)
    if hotel is None:
        raise HTTPException(404, "Отель не найден")
    return s, hotel, s.room_types([hotel_id]).get(hotel_id, [])


def _room_type(room_types: list[dict[str, Any]], room_type_id: str) -> dict[str, Any]:
    rt = next((r for r in room_types if r["room_type_id"] == room_type_id), None)
    if rt is None:
        raise HTTPException(404, f"нет категории {room_type_id} у отеля")
    return rt


def _with_state(question: dict[str, Any], rt: dict[str, Any]) -> dict[str, Any]:
    """Вопрос о размещении + состояние: `answered` — набор категории из ответа на этот вопрос;
    `placement_status` — статус текущего набора (`hotelier`, `simulated_hotelier`, `curated`)."""
    ps = rt.get("placement_set") or {}
    source = ps.get("source") or {}
    answered = bool(ps) and source.get("question_id") == question["question_id"]
    return question | {
        "answer_status": "answered" if answered else "not_answered",
        "answered_at": source.get("answered_at") if answered else None,
        "placement_status": ps.get("status"),
        "placement_completeness": ps.get("completeness"),
    }


@router.get("/{hotel_id}/placement-questions")
def placement_questions(hotel_id: str, request: Request) -> dict[str, Any]:
    """По вопросу на категорию: стороны и этажи окон, со списком корпусов отеля по карте."""
    s, _, room_types = _context(request, hotel_id)
    if not room_types:
        return {"stub": s.stub, "hotel_id": hotel_id, "questions": [], "building_choices": [],
                "notes": [NO_CATEGORIES]}  # fmt: skip
    choices = s.placement_geometry(hotel_id).get("placement_building_choices") or []
    return {
        "stub": s.stub,
        "hotel_id": hotel_id,
        "questions": [
            _with_state(agent.placement_question(hotel_id, rt, choices), rt) for rt in room_types
        ],
        "building_choices": choices,
        "notes": [] if choices else [NO_BUILDINGS],
    }


@router.post("/{hotel_id}/placement-simulate")
def placement_simulate(
    hotel_id: str, body: PlacementSimulateRequest, request: Request
) -> dict[str, Any]:
    """Ответ имитатора по закрытому профилю (D31: `is_simulated`). Не сохраняется: его
    отправляют в `…/placement-answers` вместе с `building_aliases`."""
    _, _, room_types = _context(request, hotel_id)
    rt = _room_type(room_types, body.room_type_id)
    out = agent.simulate_placement(hotel_id, body.room_type_id)
    status = (rt.get("placement_set") or {}).get("status")
    if status in ("hotelier", "curated"):
        out["notes"] = [
            f"У категории уже есть размещения ({status}): ответ имитатора их не заменит (D41-8)."
        ]
    return out


@router.post("/{hotel_id}/placement-answers")
def placement_answer(
    hotel_id: str, body: PlacementAnswerRequest, request: Request
) -> dict[str, Any]:
    """Ответ о категории → размещения D17 (с полнотой и гарантией) или уточняющий вопрос.

    Сторона света — все стены корпуса на эту сторону от 3 м, номер — на одной из них (D41-3).
    Чего не понять по тексту и карте — уточнение; понятое сохраняется, но список тогда не полный.
    Ответ имитатора не заменяет размещения настоящего отеля или команды (D41-8). Корпуса,
    отмеченные в списке, сохраняются как названия со слов отеля (`hotelier` /
    `simulated_hotelier`): вопрос, дата, «выбран в списке» и цитата, если название есть в тексте.
    """
    s, hotel, room_types = _context(request, hotel_id)
    rt = _room_type(room_types, body.room_type_id)
    geometry = s.placement_geometry(hotel_id)
    q = agent.placement_question(hotel_id, rt, geometry.get("placement_building_choices"))
    got = answer_placement(
        body.answer_text, geometry, building_aliases=body.building_aliases, question=q["question"]
    )
    now = datetime.now(UTC)
    status = "simulated_hotelier" if body.is_simulated else "hotelier"
    notes = list(got.report["notes"])
    kept = None
    if got.placements:
        answer = AnswerIn(
            feature_id=FEATURE_ID,
            room_type_id=rt["room_type_id"],
            answer_text=body.answer_text,
            placements=got.placements,
            is_simulated=body.is_simulated,
            respondent=body.respondent,
        )
        # Вопроса о размещении нет в таблице вопросов: его ID `qp_…` — в паспорте размещений.
        question = {"question_id": q["question_id"], "hotel_id": hotel_id,
                    "feature_id": FEATURE_ID, "city_id": hotel["city_id"]}  # fmt: skip
        try:
            result = build_answer(question, answer, [], now)
            result.placement_source |= {"parsed_by": got.report["parsed_by"], "rule": "D41-3",
                                        "stated": got.stated}  # fmt: skip
            s.apply_answer(result)
        except AnswerError as e:
            raise HTTPException(422, str(e)) from e
        kept = result.placements_kept
    saved = bool(got.placements) and kept is None
    aliases = []
    if kept is None:  # имитатор при настоящем ответе ничего не пишет
        source = {
            "provider": "hotelier_simulator" if body.is_simulated else "hotelier",
            "question_id": q["question_id"],
            "room_type_id": rt["room_type_id"],
            "answered_at": now.isoformat(),
            "method": "selected_in_list",
        } | ({"respondent": body.respondent} if body.respondent else {})
        for a in got.aliases:
            # Цитата — только если отель назвал корпус этим названием в тексте ответа.
            quote = {"quote": a["quote"]} if a.get("quote") else {}
            alias = {"name": a["name"], "status": status, "source": source | quote}
            if s.add_building_alias(hotel_id, a["building_id"], alias):
                aliases.append({"building_id": a["building_id"]} | alias)
    if kept:
        notes.append(
            f"Ответ имитатора не сохранён: у категории размещения со статусом {kept} — "
            "настоящий ответ остаётся (D41-8)."
        )
    elif body.is_simulated:
        notes.append("Ответ имитатора: размещения и названия корпусов помечены simulated_hotelier.")
    if kept:
        state = "real_answer_kept"
    elif saved:
        state = "answered"
    else:
        state = "needs_clarification" if got.followup else "not_placed"
    return {
        "stub": s.stub,
        "hotel_id": hotel_id,
        "room_type_id": rt["room_type_id"],
        "question_id": q["question_id"],
        # answered — размещения сохранены (`followup` тогда — подтвердить или дополнить);
        # needs_clarification — ничего не сохранено, ждём ответа на `followup`;
        # real_answer_kept — ответ имитатора не заменил настоящий (D41-8);
        # not_placed — корпусов отеля нет в справочнике, привязать не к чему.
        "state": state,
        "placements_saved": saved,
        "real_answer_kept": kept is not None,
        "placements": got.placements,
        "followup": got.followup,
        "aliases_saved": aliases,
        "room_type": _room_type(s.room_types([hotel_id]).get(hotel_id, []), rt["room_type_id"]),
        "notes": notes,
        # Что понял агент: варианты с цитатами, корпусом и стенами, отклонённое с причинами.
        "agent": got.report,
    }
