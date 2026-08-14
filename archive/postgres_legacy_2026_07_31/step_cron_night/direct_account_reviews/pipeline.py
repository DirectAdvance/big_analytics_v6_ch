"""
direct_account_reviews/pipeline.py

Запускает полный пайплайн отзывов:

  Шаг 1. load_reviews.py                  — загружает справочник из Google Sheets
  Шаг 2. fetch_direct_stats.py            — скачивает статистику из Яндекс.Директ API
  Шаг 3. load_reviews_to_big_analytics.py — вставляет в big_analytics_full

ВАЖНО: запускать только после завершения полного пайплайна big_analytics_v5 (шаги 0–7).
Шаги 2 и 3 используют Direct API — не запускать одновременно с step7.

Запуск (из папки big analytics_v5/):
    python direct_account_reviews/pipeline.py
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
    print('ШАГ 1: load_reviews.py')
    print('=' * 60)
    _load_module('load_reviews.py').main()


def _step2():
    print('\n' + '=' * 60)
    print('ШАГ 2: fetch_direct_stats.py')
    print('=' * 60)
    _load_module('fetch_direct_stats.py').main()


def _step3():
    print('\n' + '=' * 60)
    print('ШАГ 3: load_reviews_to_big_analytics.py')
    print('=' * 60)
    _load_module('load_reviews_to_big_analytics.py').main()


if __name__ == '__main__':
    _step1()
    _step2()
    _step3()
    print('\nПайплайн отзывов завершён')
