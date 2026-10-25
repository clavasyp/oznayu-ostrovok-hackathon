"""Новая характеристика из текста (core §7.4): черновик спецификации → проверка → пробный расчёт.

Включает характеристику человек; папка `specs/` и статус `active` — у лида.
"""

from agents.specgen.draft import Draft, draft_spec
from agents.specgen.spec import FeatureSpec, from_yaml, to_yaml
from agents.specgen.validate import check

__all__ = ["Draft", "FeatureSpec", "check", "draft_spec", "from_yaml", "to_yaml"]
