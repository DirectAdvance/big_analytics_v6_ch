"""
cookies.py — проверка валидности кук Яндекс.Директ перед шагами пайплайна
"""
import json
import logging
import os
import re

import requests
import urllib3

from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GRID_URL   = 'https://direct.yandex.ru/web-api/grid/api'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

# Тестовые логины (совпадают с cookie_checker.py на хоум сервере)
_TEST_LOGINS = {
    'victoryagency-direct1618440': 'acbu-spb-436222-ns89',
    'victorylotsofads1':           'e-20074351',
    'victoryagency14':             'porg-7uhutcdh',
}

_QUERY = (
    'query CampaignsTotal($login:String! $campaignInput:GdCampaignsContainerInput!){'
    'client(searchBy:{login:$login}){'
    'campaigns(input:$campaignInput){totalCampaigns{totalSumRest}}'
    '}}'
)

_INPUT = {
    'filter':           {},
    'statRequirements': {'preset': 'LAST_30DAYS', 'goalIds': [], 'useCampaignGoalIds': True},
    'limitOffset':      {'limit': 1, 'offset': 0},
    'orderBy':          [{'order': 'ASC', 'field': 'STATUS'}],
}


def _post_grid(test_login: str, cookie: str, csrf=None):
    payload = {
        'operationName': 'CampaignsTotal',
        'query':         _QUERY,
        'variables':     {'login': test_login, 'campaignInput': _INPUT},
    }
    headers = {
        'Cookie':             cookie,
        'dna-operation-name': 'CampaignsTotal',
        'x-direct-api':       '1',
        'x-detected-locale':  'ru',
        'Content-Type':       'application/json',
        'User-Agent':         USER_AGENT,
    }
    if csrf:
        headers['x-csrf-token'] = csrf
    url = f'{GRID_URL}?operationName=CampaignsTotal&ulogin={test_login}'
    try:
        return requests.post(url, json=payload, headers=headers, timeout=30, verify=False)
    except Exception:
        return None


def _extract_csrf(resp):
    csrf = resp.cookies.get('_direct_csrf_token')
    if csrf:
        return csrf
    for sc in resp.headers.get('Set-Cookie', '').split(','):
        m = re.search(r'_direct_csrf_token=([^;,\s]+)', sc)
        if m:
            return m.group(1)
    return None


def check_cookies_alive(cookies: dict) -> bool:
    """
    Проверяет живы ли куки через Grid API.
    Возвращает True если хотя бы одна кука из двух известных аккаунтов работает.
    Сетевые ошибки не считаются протуханием — возвращает True.
    """
    any_tested = False
    for account, test_login in _TEST_LOGINS.items():
        cookie = cookies.get(account)
        if not cookie:
            continue
        any_tested = True
        resp = _post_grid(test_login, cookie)
        if resp is None:
            return True  # сетевая ошибка — не блокируем шаг
        if resp.status_code == 403:
            csrf = _extract_csrf(resp)
            if csrf:
                resp = _post_grid(test_login, cookie, csrf)
            if resp is None or resp.status_code != 200:
                continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except Exception:
            continue
        if 'data' in data and not data.get('errors'):
            return True
    # Если ни один аккаунт не прошёл тест — куки мертвы
    # Если аккаунты не найдены в файле — не блокируем
    return not any_tested


def _check_single_account(test_login: str, cookie: str) -> bool:
    """
    Проверяет одну куку через Grid API (с CSRF fallback).
    Возвращает True если живая или произошла сетевая ошибка (не считаем мёртвой).
    False — если 403 без CSRF / status != 200 / errors в ответе / невалидный JSON.
    """
    resp = _post_grid(test_login, cookie)
    if resp is None:
        return True  # сетевая ошибка — не считаем мёртвой
    if resp.status_code == 403:
        csrf = _extract_csrf(resp)
        if not csrf:
            return False
        resp = _post_grid(test_login, cookie, csrf)
        if resp is None:
            return True  # сетевая ошибка на retry — не считаем мёртвой
        if resp.status_code != 200:
            return False
    elif resp.status_code != 200:
        return False
    try:
        data = resp.json()
    except Exception:
        return False
    if 'data' in data and not data.get('errors'):
        return True
    return False


def check_all_cookies_strict(cookies: dict) -> list[str]:
    """
    Строгая проверка: проверяет ВСЕ аккаунты из _TEST_LOGINS.
    Возвращает список имён аккаунтов, чьи куки мертвы (пустой список = все ОК).

    Используется в самом начале pipeline.py — если хоть одна кука мертва,
    пайплайн останавливается.
    Сетевые ошибки не считаются протуханием (аккаунт считается живым).
    """
    dead: list[str] = []
    for account, test_login in _TEST_LOGINS.items():
        cookie = cookies.get(account)
        if not cookie:
            dead.append(account)
            continue
        if not _check_single_account(test_login, cookie):
            dead.append(account)
    return dead


def send_tg(text: str) -> None:
    """Отправляет произвольное сообщение в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct)."""
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            r = requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text},
                              proxies=proxies, timeout=15)
            if r.status_code == 200:
                return
        except Exception:
            pass
    logger.error('TG_SEND_FAIL_2026-07-03: все proxy-варианты отказали, сообщение потеряно')


def send_tg_cookies_dead(step_name: str) -> None:
    """Отправляет уведомление в Telegram о протухших куках с ротацией прокси."""
    text = (
        f'\U0001f36a Куки Яндекс.Директ протухли\n'
        f'Шаг: {step_name}\n'
        f'Шаг пропущен, старые данные сохранены.\n'
        f'Обнови cookies.json на сервере.'
    )
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            r = requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text},
                              proxies=proxies, timeout=15)
            if r.status_code == 200:
                return
        except Exception:
            pass
    logger.error('TG_SEND_FAIL_2026-07-03: все proxy-варианты отказали (cookies_dead), сообщение потеряно')


# ── Общий guard живости кук (check-first self-healing) ─────────────────────────

# Путь к cookies.json — в корне big_analytics_v5 (на уровень выше config/).
_COOKIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cookies.json'
)


class CookiesDeadError(RuntimeError):
    """Куки протухли и не восстановились даже после рефреша с glavpotok.

    Несёт список мёртвых аккаунтов в .dead. Пайплайн ловит это исключение
    и делает свой clean-exit (закрыть пулы БД и т.п.).
    """

    def __init__(self, dead: list[str]):
        self.dead = dead
        super().__init__('cookies dead even after glavpotok refresh: ' + ', '.join(dead))


def ensure_cookies_alive_or_stop(pipeline_name: str = 'big_analytics_v5',
                                 send_tg=None,
                                 cookies_path=None) -> dict:
    """Единый check-first self-healing guard живости кук Яндекс.Директ.

    Логика (одинаковая для pipeline.py и pipeline_powerbi.py):
      1. Читаем cookies.json.
      2. Проверяем НАШЕЙ проверкой check_all_cookies_strict (3 менеджерских аккаунта).
      3. Если хоть одна протухла → refresh_cookies() (запрос свежих с glavpotok).
      4. Перепроверяем check_all_cookies_strict ещё раз.
      5. Если и после рефреша мертво → Telegram + CookiesDeadError (стоп пайплайна).
      6. Если всё живо (сразу или после рефреша) → возвращаем словарь кук.

    glavpotok дёргается ТОЛЬКО когда наша проверка нашла мёртвую куку
    (а не безусловно в начале) — меньше лишних запросов к эндпоинту.

    Args:
        pipeline_name: имя пайплайна для текста Telegram-уведомления.
        send_tg: функция отправки в Telegram (text -> None). Если None — модульная send_tg.
        cookies_path: путь к cookies.json (по умолчанию — рядом с pipeline.py).

    Returns:
        dict {login: cookie_string} — актуальные живые куки.

    Raises:
        CookiesDeadError: куки мертвы даже после рефреша (TG уже отправлен).
        Exception: проброс ошибки чтения cookies.json (после TG).
    """
    if send_tg is None:
        send_tg = globals()['send_tg']  # модульная send_tg (без proxy-зависимости вызова)
    path = cookies_path or _COOKIES_PATH

    # ── 1. Читаем cookies.json ───────────────────────────────────────────────
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cookies = json.load(f)
    except Exception as e:
        logger.error('ensure_cookies: не удалось прочитать cookies.json (%s): %s', path, e)
        send_tg(f'❌ {pipeline_name}: не удалось прочитать cookies.json\n{e}')
        raise

    # ── 2. Проверяем нашей проверкой ─────────────────────────────────────────
    dead = check_all_cookies_strict(cookies)

    # ── 3-4. Если мертво → refresh с glavpotok → перепроверка ─────────────────
    if dead:
        logger.warning('ensure_cookies: куки мертвы %s — рефреш с glavpotok и перепроверка...', dead)
        try:
            from refresh_cookies import refresh_cookies as _refresh_cookies
            cookies = _refresh_cookies()
            dead = check_all_cookies_strict(cookies)
        except Exception as e:
            logger.error('ensure_cookies: рефреш кук с glavpotok не удался: %s', e)
            # dead остаётся прежним → ниже сработает стоп

    # ── 5. Если и после рефреша мертво → TG + стоп ───────────────────────────
    if dead:
        msg = (
            f'🍪 <b>{pipeline_name} остановлен</b>: куки протухли\n'
            'Мертвые аккаунты: ' + ', '.join(f'<code>{a}</code>' for a in dead) + '\n'
            'Не удалось обновить автоматически с glavpotok. Проверь токен/эндпоинт.'
        )
        logger.error('ensure_cookies: куки мертвы даже после рефреша: %s. Стоп.', dead)
        send_tg(msg)
        raise CookiesDeadError(dead)

    # ── 6. Всё живо ──────────────────────────────────────────────────────────
    logger.info('ensure_cookies: куки валидны ✅')
    return cookies
