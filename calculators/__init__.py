"""Калькуляторы. Импорт модуля регистрирует калькулятор в core.plugins.CALCULATORS."""

from calculators import (
    env_climate,  # noqa: F401  (C01–C03 — климат)
    env_landcover,  # noqa: F401  (доля классов WorldCover)
    env_marine,  # noqa: F401  (B02 — температура моря)
    env_vegetation,  # noqa: F401  (пример контракта — E03)
)
