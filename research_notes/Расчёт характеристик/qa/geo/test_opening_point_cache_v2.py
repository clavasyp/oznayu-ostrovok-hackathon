#!/usr/bin/env python3
"""Expected AE states plus exact point-state regression for supported countries."""
import json
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'scripts'))
import geo
from geo_opening_point_cache import evaluate as old_evaluate
from geo_opening_point_cache_v2 import evaluate, _state, VERSION

def main():
    dates = ['2026-11-14T02:00:00', '2027-07-17T02:00:00']
    examples = {'24/7': 'open', 'Mo-Su 09:00-22:00': 'closed',
                'Fr 20:00-04:00': 'open', 'Mo-Su 22:00-05:00': 'open',
                'Mo-Su 08:00-12:00,14:00-22:00': 'closed',
                '24/7; PH off': 'unknown', 'Mo-Su 08:00-23:00; SH off': 'unknown',
                'notvalid(': 'unknown'}
    rows = []
    for city in geo.CENTERS:
        for date in dates:
            for expression, expected_ae in examples.items():
                tags = {'opening_hours': expression}
                new = evaluate({'tags': tags}, date, city, timezones=geo.TZ, countries=geo.COUNTRY)
                old = old_evaluate({'tags': tags}, date, city, timezones=geo.TZ, countries=geo.COUNTRY)
                if city == 'dubai':
                    passed = new['state'] == expected_ae and new['evaluated_local'].endswith('+04:00')
                    if 'PH' in expression or 'SH' in expression:
                        passed &= new['used_country'] == 'AE' and 'requires the requested country' in new['reason']
                    elif expected_ae != 'unknown':
                        passed &= new['used_country'] is None and new['requested_country'] == 'AE'
                        passed &= new['is_open'] == (expected_ae == 'open') and new['is_closed'] == (expected_ae == 'closed')
                else:
                    passed = all(new.get(key) == old.get(key) for key in ['state', 'expression', 'evaluated_local'])
                    # Failed old parser rows omitted evaluated_local; v2 adds a useful timestamp.
                    if old['state'] == 'unknown' and not old.get('evaluated_local'):
                        passed = new['state'] == old['state'] and new['expression'] == old['expression']
                rows.append({'city_id': city, 'date': date, 'expression': expression,
                             'expected_ae': expected_ae if city == 'dubai' else None,
                             'old': old, 'new': new, 'passed': bool(passed)})
    _state.cache_clear()
    contexts = [({'opening_hours': '24/7'}, False),
                ({'opening_hours': '24/7', 'opening_hours:kitchen': '24/7'}, True),
                ({'kitchen:opening_hours': '24/7'}, True),
                ({'food_service_hours': '24/7'}, True)]
    for tags, kitchen in contexts:
        evaluate({'tags': tags}, dates[0], 'dubai', kitchen, timezones=geo.TZ, countries=geo.COUNTRY)
    distinct = _state.cache_info().misses == len(contexts)
    result = {'version': VERSION, 'cases': len(rows), 'all_cases_passed': all(row['passed'] for row in rows),
              'kitchen_and_selected_tag_keys_distinct': distinct,
              'cache_key': ['expression', 'date', 'city', 'timezone', 'country', 'kitchen', 'selected_tag_key'],
              'scope': 'Library semantic/component regression only; no physical business hours accuracy claim.', 'rows': rows}
    result['passed'] = result['all_cases_passed'] and distinct
    (BASE / 'qa/geo/opening_point_cache_v2_checks.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print({key: value for key, value in result.items() if key != 'rows'})
    assert result['passed']

if __name__ == '__main__':
    main()
