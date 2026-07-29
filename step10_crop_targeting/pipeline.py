"""
big analytics_v5/crop_targeting/pipeline.py

Запускает полный пайплайн посевов (crop targeting):

  Шаг 1. load_crop_targeting.py       — загружает Google Sheets
                                         → gsheets_crop_targeting_account
                                         → gsheets_crop_targeting_account_pravilo_utm
  Шаг 2. load_crop_targeting_leads.py — gsheets + лиды (до мая)
                                         → gsheets_crop_targeting_account_leads
  Шаг 3. load_telega_in_orders.py            — Telega.in API + лиды (с мая)
                                         → crop_targeting_api_telegain_lead
  Шаг 4. load_crop_to_big_analytics.py — оба источника →
                                          big_analytics_crop_targeting →
                                          big_analytics_full

ВАЖНО: запускать только после завершения полного пайплайна big_analytics_v5 (шаги 0–7).

Запуск (из папки big analytics_v5/):
    python3 crop_targeting/pipeline.py
"""

import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_module(filename: str):
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _step1():
    print('\n' + '=' * 60)
    print('ШАГ 1: load_crop_targeting.py')
    print('=' * 60)
    _load_module('load_crop_targeting.py').main()


def _step2():
    print('\n' + '=' * 60)
    print('ШАГ 2: load_crop_targeting_leads.py')
    print('=' * 60)
    _load_module('load_crop_targeting_leads.py').main()


def _step3():
    print('\n' + '=' * 60)
    print('ШАГ 3: load_telega_in_orders.py')
    print('=' * 60)
    _load_module('load_telega_in_orders.py').main()


def _step4():
    print('\n' + '=' * 60)
    print('ШАГ 4: load_crop_to_big_analytics.py')
    print('=' * 60)
    _load_module('load_crop_to_big_analytics.py').main()


if __name__ == '__main__':
    _step1()
    _step2()
    _step3()
    _step4()
    print('\nПайплайн посевов завершён')
