"""Перепроверка URL из yandex_direct_404_errors реальным HTTP-кодом.

Таблица yandex_direct_404_errors — журнал визитов на страницы, у которых в Метрике
title содержал «404». Это косвенный признак: страницу могли починить, а строка
осталась. Скрипт берёт свежие URL, дёргает их HTTP-запросом и удаляет из таблицы те,
что уже отдают нормальный ответ.

Семантика удаления консервативная: удаляем URL, только если он ЯВНО живой
(2xx/3xx и в теле нет 404-заголовка). Любая сетевая ошибка, таймаут, 5xx, 403/429
или soft-404 (200 + «404» в <title>) → строки остаются.

Запуск:
    Автоматически — шаг «recheck_404» ночного пайплайна pipeline_night.py
    (сразу после шага «404_errors», который только что загрузил/обновил таблицу).

    Вручную (из корня big_analytics_v5, cwd=BASE_DIR):
        ~/venv/bin/python3 step_cron_night/404_errors/recheck_404.py
        ... --days 30      окно свежести по visit_date (по умолчанию 30)
        ... --all          проверить все URL таблицы, а не только свежие
        ... --dry-run      ничего не удалять, только отчёт
"""
import argparse
import os
import re
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg2
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import DB_DST  # noqa: E402

# ============================================================
# НАСТРОЙКИ
# ============================================================
DEFAULT_DAYS = 30          # окно свежести по visit_date
WORKERS      = 12          # параллельных проверок всего
PER_HOST     = 2           # параллельных проверок на один домен
TIMEOUT      = 15          # сек на запрос
RETRIES      = 2           # попыток на URL (сетевые ошибки)
BODY_LIMIT   = 64 * 1024   # сколько байт тела читаем ради <title>

UA = ('Mozilla/5.0 (compatible; VictoryLinkChecker/1.0; '
      '+https://victoryagency.ru/) Python-requests')

# Трекинговые параметры — на наличие 404 не влияют, чистим перед запросом.
DROP_PARAMS = re.compile(
    r'^(utm_\w+|yclid|gclid|ysclid|fbclid|_openstat|from|erid|roistat\w*)$', re.I)

TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
DB_CONFIG = DB_DST
# ============================================================

_host_sem: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(PER_HOST))
_sem_lock = threading.Lock()


def _sem(host: str) -> threading.Semaphore:
    with _sem_lock:
        return _host_sem[host]


def clean_url(url: str) -> str:
    """Убирает трекинговые параметры, оставляя остальной query нетронутым."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not DROP_PARAMS.match(k)]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def is_soft_404(html: str) -> bool:
    m = TITLE_RE.search(html)
    return bool(m) and '404' in m.group(1)


def check_url(url: str) -> tuple[str, str, str]:
    """-> (url, verdict, detail). verdict: 'alive' | 'broken' | 'unknown'.

    Удаляем из таблицы только 'alive'. 'unknown' (сеть, 5xx, 403/429) —
    страховка от массового ложного удаления при недоступности сайта.
    """
    target = clean_url(url)
    host = urlsplit(target).netloc

    if urlsplit(target).scheme not in ('http', 'https') or not host:
        return url, 'unknown', 'bad-url'

    last_err = ''
    for _ in range(RETRIES):
        try:
            with _sem(host):
                r = requests.get(
                    target,
                    headers={'User-Agent': UA, 'Accept': 'text/html,*/*'},
                    timeout=TIMEOUT,
                    allow_redirects=True,
                    stream=True,
                )
                code = r.status_code
                body = ''
                if code == 200 and 'html' in r.headers.get('Content-Type', '').lower():
                    body = r.raw.read(BODY_LIMIT, decode_content=True).decode(
                        r.encoding or 'utf-8', errors='ignore')
                r.close()

            if code in (404, 410):
                return url, 'broken', str(code)
            if code >= 500 or code in (403, 429, 401):
                return url, 'unknown', str(code)
            if code == 200 and body and is_soft_404(body):
                return url, 'broken', '200-soft404'
            if 200 <= code < 400:
                return url, 'alive', str(code)
            return url, 'unknown', str(code)

        except Exception as e:
            last_err = type(e).__name__

    return url, 'unknown', last_err or 'error'


def fetch_urls(cur, days: int | None) -> list[str]:
    if days is None:
        cur.execute("SELECT DISTINCT url FROM yandex_direct_404_errors WHERE url <> ''")
    else:
        cur.execute("""
            SELECT DISTINCT url FROM yandex_direct_404_errors
            WHERE url <> '' AND visit_date >= current_date - %s::int
        """, (days,))
    return [r[0] for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description='Перепроверка 404-URL реальным HTTP-кодом')
    ap.add_argument('--days', type=int, default=DEFAULT_DAYS,
                    help=f'окно свежести по visit_date (по умолчанию {DEFAULT_DAYS})')
    ap.add_argument('--all', action='store_true', help='проверить все URL таблицы')
    ap.add_argument('--dry-run', action='store_true', help='не удалять, только отчёт')
    args = ap.parse_args()

    days = None if args.all else args.days
    scope = 'ВСЕ URL' if days is None else f'visit_date >= current_date - {days}'

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'yandex_direct_404_errors'
                )
            """)
            if not cur.fetchone()[0]:
                print('Таблица yandex_direct_404_errors не существует — нечего проверять.')
                return 0
            cur.execute('SELECT count(*) FROM yandex_direct_404_errors')
            rows_before = cur.fetchone()[0]
            urls = fetch_urls(cur, days)

        print('=' * 60)
        print(f'Перепроверка 404 | {scope} | URL к проверке: {len(urls)}')
        print(f'   строк в таблице до: {rows_before}')
        print('=' * 60)

        if not urls:
            print('Нет URL в выбранном окне.')
            return 0

        alive, broken, unknown = [], [], []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for i, (url, verdict, detail) in enumerate(pool.map(check_url, urls), 1):
                {'alive': alive, 'broken': broken, 'unknown': unknown}[verdict].append((url, detail))
                if i % 200 == 0:
                    print(f'   ... {i}/{len(urls)}')

        print(f'\nживых (удалить):  {len(alive)}')
        print(f'всё ещё 404:      {len(broken)}')
        print(f'не определено:    {len(unknown)}  (не трогаем)')

        for url, detail in alive[:5]:
            print(f'   [{detail}] {url}')

        if not alive:
            print('\nУдалять нечего.')
            return 0

        if args.dry_run:
            print(f'\nDRY-RUN: удалено бы строк по {len(alive)} URL (запрос не выполнялся).')
            return 0

        dead_urls = [u for u, _ in alive]
        with conn.cursor() as cur:
            cur.execute('DELETE FROM yandex_direct_404_errors WHERE url = ANY(%s)', (dead_urls,))
            deleted = cur.rowcount
            conn.commit()
            cur.execute('SELECT count(*) FROM yandex_direct_404_errors')
            rows_after = cur.fetchone()[0]

        print(f'\nУдалено строк: {deleted} (по {len(dead_urls)} восстановленным URL)')
        print(f'   строк в таблице: {rows_before} -> {rows_after}')
        print('Готово!')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
