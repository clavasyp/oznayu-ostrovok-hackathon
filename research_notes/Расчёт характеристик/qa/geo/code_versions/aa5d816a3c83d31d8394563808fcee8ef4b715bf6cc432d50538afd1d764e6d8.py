"""Requested-instant hours with an explicit, calendar-safe unsupported-AE adapter.

Only the library's unsupported ISO country AE error permits dropping country,
and only if the expression contains no PH/SH token. Timezone is never dropped.
The returned state concerns an OSM declared schedule, not physical operation.
"""
import functools
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import opening_hours

VERSION = 'V9-local-instant-hours-cache-2-AE-calendar-safe'

@functools.lru_cache(maxsize=32768)
def _state(expression, date, city, timezone, country, kitchen, selected_tag_key):
    holiday = bool(re.search(r'\b(PH|SH)\b', expression))
    base = {'expression': expression, 'requested_country': country,
            'used_country': country, 'country_adapter_reason': None,
            'has_public_holiday_expression': holiday,
            'next_change': None,
            'next_change_reason': 'Optional future-change search not required for the feature and omitted; requested-instant state was evaluated exactly.',
            'evaluation_method_version': VERSION}
    try:
        dt = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(timezone))
        base['evaluated_local'] = dt.isoformat()
        try:
            obj = opening_hours.OpeningHours(expression, country=country, timezone=ZoneInfo(timezone))
        except Exception as error:
            unsupported_ae = country == 'AE' and 'Unknown ISO code `AE`' in str(error)
            if not unsupported_ae:
                raise
            if holiday:
                return {**base, 'state': 'unknown', 'is_open': None, 'is_closed': None,
                        'reason': 'unsupported_AE_holiday_calendar: PH/SH expression requires the requested country; country=None is forbidden',
                        'country_adapter_reason': 'AE unsupported by installed parser; holiday tokens prevent a calendar-free fallback.'}
            obj = opening_hours.OpeningHours(expression, country=None, timezone=ZoneInfo(timezone))
            base['used_country'] = None
            base['country_adapter_reason'] = 'Installed parser rejects ISO AE; expression has no PH/SH tokens, so no country holiday calendar affects its requested-instant state. Asia/Dubai retained.'
        is_open = bool(obj.is_open(dt))
        is_closed = bool(obj.is_closed(dt))
        return {**base, 'state': 'open' if is_open else 'closed' if is_closed else 'unknown',
                'is_open': is_open, 'is_closed': is_closed,
                'reason': 'OSM declared recurring schedule; future exceptions/physical operation require refresh'}
    except Exception as error:
        return {**base, 'state': 'unknown', 'is_open': None, 'is_closed': None,
                'reason': 'parser_or_country_holiday_failure:' + str(error)[:160]}

def evaluate(poi, date, city, kitchen=False, *, timezones, countries):
    tags = poi['tags']
    keys = ['opening_hours:kitchen', 'kitchen:opening_hours', 'food_service_hours'] if kitchen else ['opening_hours']
    selected = next((key for key in keys if tags.get(key)), None)
    expression = tags[selected] if selected else None
    if not expression:
        return {'state': 'unknown', 'reason': 'no_kitchen_service_hours' if kitchen else 'no_opening_hours',
                'expression': None, 'requested_country': countries[city],
                'used_country': countries[city], 'evaluation_method_version': VERSION}
    return dict(_state(expression, date, city, timezones[city], countries[city], bool(kitchen), selected))
