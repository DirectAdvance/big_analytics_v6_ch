"""
refresh_cookies.py — автоматическое обновление cookies.json с эндпоинта glavpotok.ru.

Заменяет ручное обновление куки Я.Директ. Для каждого из 3 менеджерских логинов
делает GET https://glavpotok.ru/api/cookies/yandex-direct/<login> с
Authorization: Bearer <token> (токен из .secret/.env → GLAVPOTOK_COOKIES_TOKEN),
собирает {login: cookie_string} и АТОМАРНО перезаписывает cookies.json рядом
с pipeline.py.

Поведение по логину:
  200 → берём cookie_string (формат 1:1 совместим с HTTP-заголовком Cookie).
  404 → лог-варнинг, НЕ падаем: оставляем старую куку этого логина из текущего
        cookies.json (если есть).
  401 → явная ошибка (нет/неверный токен) — RuntimeError.
  сеть/таймаут → retry до RETRIES раз; если всё равно неудача — как 404
        (оставить старую, варнинг), чтобы не ронять пайплайн из-за блипа сети.

Запуск standalone:
    python3 refresh_cookies.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

# ── Загрузка секрета через общий loader ───────────────────────────────────────
for _p in Path(__file__).resolve().parents:
    if (_p / '.secret' / 'loader.py').exists():
        sys.path.insert(0, str(_p / '.secret'))
        break
from loader import load_glavpotok_cookies  # noqa: E402

import requests  # noqa: E402

logger = logging.getLogger(__name__)

# Путь к cookies.json — рядом с pipeline.py (в этой же директории)
COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.json')

# 3 менеджерских логина — единственные нужные ключи cookies.json.
# Совпадают с _TEST_LOGINS в config/cookies.py.
MANAGER_LOGINS = [
    'victorylotsofads1',
    'victoryagency-direct1618440',
    'victoryagency14',
]

TIMEOUT = 20      # секунд на запрос
RETRIES = 3       # попыток на логин при сетевых ошибках / 5xx
RETRY_SLEEP = 2   # секунд между попытками


def _logins_from_existing(existing: dict) -> list[str]:
    """Источник логинов: ключи существующего cookies.json, иначе константа.

    Позволяет не хардкодить список, если в будущем добавятся аккаунты.
    Сейчас фактически совпадает с MANAGER_LOGINS.
    """
    keys = [k for k in (existing or {}) if isinstance(k, str) and k]
    return keys or list(MANAGER_LOGINS)


def _load_existing() -> dict:
    """Прочитать текущий cookies.json (если есть) — для fallback по 404/сети."""
    if os.path.exists(COOKIES_PATH):
        try:
            with open(COOKIES_PATH, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:  # noqa: BLE001
            logger.warning('refresh_cookies: не удалось прочитать старый cookies.json: %s', e)
    return {}


def _atomic_write(path: str, data: dict) -> None:
    """Атомарная перезапись JSON через temp + os.replace в той же директории."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.cookies_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fetch_one(session: requests.Session, base: str, headers: dict, login: str) -> str | None:
    """Вернуть cookie_string для логина, либо None если получить не удалось (404/сеть/5xx).

    401 → RuntimeError (явная ошибка токена).
    """
    url = f'{base}/{login}'
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, headers=headers, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning('refresh_cookies: %s сетевая ошибка (попытка %d/%d): %s',
                           login, attempt, RETRIES, e)
            if attempt < RETRIES:
                time.sleep(RETRY_SLEEP)
            continue

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception as e:  # noqa: BLE001
                logger.warning('refresh_cookies: %s 200 но не JSON: %s', login, e)
                return None
            cs = (data.get('cookie_string') or '').strip()
            if not cs:
                logger.warning('refresh_cookies: %s 200 но пустой cookie_string', login)
                return None
            return cs

        if r.status_code == 401:
            raise RuntimeError('glavpotok 401 Unauthorized — нет/неверный токен GLAVPOTOK_COOKIES_TOKEN')

        if r.status_code == 404:
            logger.warning('refresh_cookies: %s 404 — нет куки на glavpotok, оставляю старую', login)
            return None

        # Прочие коды (5xx и т.п.) — ретраим
        last_err = f'HTTP {r.status_code}: {r.text[:160]}'
        logger.warning('refresh_cookies: %s %s (попытка %d/%d)',
                       login, r.status_code, attempt, RETRIES)
        if attempt < RETRIES:
            time.sleep(RETRY_SLEEP)

    logger.warning('refresh_cookies: %s не получен после %d попыток (%s), оставляю старую',
                   login, RETRIES, last_err)
    return None


def refresh_cookies() -> dict:
    """Обновить cookies.json с glavpotok. Вернуть итоговый словарь {login: cookie_string}.

    Атомарно перезаписывает COOKIES_PATH. 404/сеть по одному логину не роняют процесс —
    старая кука этого логина (если есть) сохраняется. 401 → RuntimeError.
    """
    cfg = load_glavpotok_cookies()
    base = cfg['base_url'].rstrip('/')
    headers = {'Authorization': f"Bearer {cfg['token']}"}

    existing = _load_existing()
    logins = _logins_from_existing(existing)

    result: dict[str, str] = dict(existing)  # старт со старых значений (fallback)
    fresh = 0
    kept = 0
    with requests.Session() as session:
        for login in logins:
            cs = _fetch_one(session, base, headers, login)
            if cs is not None:
                result[login] = cs
                fresh += 1
            elif login in existing:
                kept += 1  # оставляем старую куку этого логина
            else:
                logger.warning('refresh_cookies: %s не обновлён и старой куки нет', login)

    _atomic_write(COOKIES_PATH, result)
    logger.info('refresh_cookies: записано %d аккаунтов (свежих %d, оставлено старых %d)',
                len(result), fresh, kept)
    return result


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    try:
        data = refresh_cookies()
    except Exception as e:  # noqa: BLE001
        logger.error('refresh_cookies FAILED: %s', e)
        sys.exit(1)
    print(f'cookies.json обновлён: {len(data)} аккаунтов → {COOKIES_PATH}')
    for k, v in data.items():
        print(f'  {k:30s} len={len(v)}')
