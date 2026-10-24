"""Готовность приложения для /health (core.md §10): 503 с фазой, пока идёт запуск, затем ok."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Phase(StrEnum):
    starting = "starting"
    waiting_for_init = "waiting_for_init"  # init ещё грузит миграции и пакеты
    loading = "loading"
    ready = "ready"
    failed = "failed"


@dataclass
class AppState:
    phase: Phase = Phase.starting
    detail: str | None = None
    data_mode: str = "fixtures"  # fixtures — заглушки из фактов v9; db — база после init
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.phase == Phase.ready
