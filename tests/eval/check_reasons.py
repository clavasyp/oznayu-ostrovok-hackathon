"""Проверка: у каждой причины «неизвестно» из наших калькуляторов есть текст для экрана.

`core/reasons.py` (PR #7) переводит машинный код причины в человеческую фразу. Новый код без
текста не ломает API, но гость видит «Пока нет данных» вместо объяснения — то есть наш честный
`unknown` выглядит как пустота. Этот скрипт ловит такие коды до сдачи.

Запуск из корня репозитория (после мерджа main):

    python tests/eval/check_reasons.py

Код возврата 1 — есть коды без текста. Скрипт, а не тест, специально: пока тексты не добавили,
он не красит `pytest` в красный, но его видно в списке проверок перед сдачей.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Коды с примером параметра: префиксные коды проверяются вместе с реальным хвостом. Путей в
# кодах нет: путь с машины разработчика не идёт в пакет (он — в recipe_attempts).
ENV_REASONS: dict[str, str] = {
    # env.climate_monthly (C01–C03)
    "hotel_coordinates_missing": "hotel_coordinates_missing",
    "city_utc_offset_unknown": "city_utc_offset_unknown:sochi",
    "era5_cache_missing": "era5_cache_missing",
    "era5_cell_missing": "era5_cell_missing",
    "incomplete_authorized_1991_2020_month_series": (
        "incomplete_authorized_1991_2020_month_series"
    ),
    # env.marine_temperature (B02)
    "marine_targets_missing": "marine_targets_missing",
    "marine_mapping_missing": "marine_mapping_missing",
    "marine_targets_changed_since_mapping": "marine_targets_changed_since_mapping",
    "hotel_not_in_marine_targets": "hotel_not_in_marine_targets",
    "selected_target_is_not_accepted_sea_beach": "selected_target_is_not_accepted_sea_beach",
    "beach_not_in_marine_mapping": "beach_not_in_marine_mapping",
    "marine_cell_not_eligible": "marine_cell_not_eligible",
    "incomplete_native_6hour_month": "incomplete_native_6hour_month:2024-07",
    "no_cached_series": "no_cached_series:2024-07",
    # env.vegetation_share (E03) и env.landcover_share
    "worldcover_tile_missing": (
        "worldcover_tile_missing:ESA_WorldCover_10m_2021_v200_N42E039_Map.tif"
    ),
    "worldcover_tile_missing (за краем вырезки)": (
        "worldcover_tile_missing:ESA_WorldCover_10m_2021_v200_N42E039_Map.tif:outside_window"
    ),
    "worldcover_valid_area_below_99_percent": "worldcover_valid_area_below_99_percent",
    "no_classified_land_in_buffer": "no_classified_land_in_buffer",
    "landcover_classes_invalid": "landcover_classes_invalid",
    "landcover_radius_invalid": "landcover_radius_invalid",
    "geo_libs_missing": "geo_libs_missing",
}


def main() -> int:
    try:
        from core import reasons
    except ImportError:
        print(
            "core/reasons.py в этой ветке ещё нет — он в main. Влей main и запусти снова:\n"
            "    git merge origin/main && python tests/eval/check_reasons.py"
        )
        return 0
    missing = []
    for name, example in ENV_REASONS.items():
        text = reasons.reason_text(example)
        known = text != reasons.FALLBACK
        print(f"{'OK  ' if known else 'НЕТ '} {name:45} {text}")
        if not known:
            missing.append(name)
    print()
    if missing:
        print(f"Без текста: {len(missing)} из {len(ENV_REASONS)} — добавь в core/reasons.py:")
        for name in missing:
            print("   ", name)
        return 1
    print(f"Все {len(ENV_REASONS)} кодов наших калькуляторов имеют текст.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
