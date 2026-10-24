"""Статусы для гостя (D31) из проверки условий по логике да / нет / неизвестно (D5).

«Подходит» — все обязательные условия «да»; «Не подходит» — хоть одно валидное «нет»;
«Нужно уточнить» — нет «нет», но хоть одно «неизвестно». Для условий уровня номера
это считается на каждом допустимом размещении категории (D17) — это делает движок поиска.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Verdict(StrEnum):
    yes = "yes"
    no = "no"
    unknown = "unknown"


class GuestStatus(StrEnum):
    fits = "fits"
    needs_check = "needs_check"
    not_fit = "not_fit"


GUEST_LABELS = {
    GuestStatus.fits: "Подходит",
    GuestStatus.needs_check: "Нужно уточнить",
    GuestStatus.not_fit: "Не подходит",
}
BOOKED_LABEL = "Забронировано"
BOOKING_IMITATION_NOTE = "Имитация оформления Островка. Реальная бронь не создаётся"


def not3(v: Verdict) -> Verdict:
    """NOT unknown = unknown."""
    return {Verdict.yes: Verdict.no, Verdict.no: Verdict.yes}.get(v, Verdict.unknown)


def and3(verdicts: Iterable[Verdict]) -> Verdict:
    """false AND unknown = false; true AND unknown = unknown."""
    vs = list(verdicts)
    if any(v == Verdict.no for v in vs):
        return Verdict.no
    if any(v == Verdict.unknown for v in vs):
        return Verdict.unknown
    return Verdict.yes


def or3(verdicts: Iterable[Verdict]) -> Verdict:
    """true OR unknown = true; false OR unknown = unknown. Пустое OR — unknown, не true."""
    vs = list(verdicts)
    if any(v == Verdict.yes for v in vs):
        return Verdict.yes
    if any(v == Verdict.unknown for v in vs) or not vs:
        return Verdict.unknown
    return Verdict.no


def guest_status(hard_verdicts: Iterable[Verdict]) -> GuestStatus:
    verdicts = list(hard_verdicts)
    if any(v == Verdict.no for v in verdicts):
        return GuestStatus.not_fit
    if any(v == Verdict.unknown for v in verdicts):
        return GuestStatus.needs_check
    return GuestStatus.fits
