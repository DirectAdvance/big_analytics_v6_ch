# step_cron_night/report_placement/step1_fetch_direct.py
# Что делает:
#   1. Читает public.metrika_yandex — логины и ID 5 целей
#   2. Инкрементное обновление: MAX(date) по таблице − 61 день → удаляет Direct-строки за этот период → скачивает заново
#   3. Запрашивает Яндекс.Директ Reports API недельными батчами
#   4. UPSERT в public.analytics_report_placement (напрямую, минуя yandex_direct_report_placement)
#      Direct-строки заполняют только 28 Direct-колонок; 15 лид-колонок остаются NULL/0 (заполнит step2)
#
# PARALLEL_2026-06-20: параллелизация по агентским токенам.
#   Архитектура: 5 токенов × WORKERS_PER_TOKEN=3 = 15 воркеров (ThreadPoolExecutor).
#   Семафор на каждый токен не даёт превысить 3 одновременных отчёта на один токен
#   (официальный лимит Reports API — 5 на токен, оставляем 40% запас).
#   Аккаунты без кэша попадают под семафор токена idx=0; после первого успеха кэш обновляется.
#   Аккаунты НЕ привязанные ни к одному нашему токену (если такие есть) — обрабатываются
#   в том же пуле, просто перебирают все токены последовательно внутри воркера.
#
# Запуск: cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/report_placement/step1_fetch_direct.py

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from io import StringIO

import pandas as pd
import psycopg2
import requests
from psycopg2.extras import execute_values
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import DB_DST, DATE_FROM
from config.tokens import OAUTH_TOKEN_1, OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS

_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'step1_fetch_direct.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_FILE, encoding='utf-8', mode='a'),
    ]
)
logger = logging.getLogger(__name__)

# ==================== НАСТРОЙКИ ====================

TOKENS = [t for t in [OAUTH_TOKEN_1, OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5] if t and t.strip()]

DATE_FROM_MANUAL    = '2026-01-01'  # BACKFILL_2026-06-30 — бэкфилл Jan-Apr 2026; вернуть None после завершения
DATE_FROM           = DATE_FROM_MANUAL if DATE_FROM_MANUAL else DATE_FROM  # noqa: F811
DATE_TO             = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
REPORTS_URL         = 'https://api.direct.yandex.com/json/v5/reports'
TARGET_TABLE        = 'analytics_report_placement'
SOURCE_TABLE        = 'metrika_yandex'
PAUSE_BETWEEN       = 2.0
WAIT_QUEUE_MIN      = 15  # TOKENFIX_2026-06-20: снижено с 20с (Direct обычно отвечает быстрее)
MAX_QUEUE_ATTEMPTS  = 15  # TOKENFIX_2026-06-20: лимит ожиданий очереди (15×15с = 225с макс)

# PARALLEL_2026-06-20: параллелизм по агентским токенам.
# Reports API лимит — 5 одновременных отчётов на один токен.
# WORKERS_PER_TOKEN=3 даёт 40% запас (3/5 лимита).
# Итого воркеров: len(TOKENS) * WORKERS_PER_TOKEN = 5 * 3 = 15.
WORKERS_PER_TOKEN   = 3

# TOKENCACHE_2026-06-20: персистентный кэш рабочего токена между прогонами (суббота→суббота).
# Хранит: login → 0-based индекс рабочего токена в списке TOKENS.
# НЕ хранит сами токены — только индекс. Файл в папке проекта (не в .secret).
_TOKEN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token_cache.json')

# (колонка в metrika_yandex, имя цели для Direct API и колонки в таблице результата)
GOAL_FIELDS = [
    ('all_forms',          'Все формы'),
    ('crm_order_created',  'CRM: Заказ создан'),
    ('crm_order_paid',     'CRM: Заказ оплачен'),
    ('crm_spam_order',     'CRM: Спам заказ'),
    ('crm_order_canceled', 'CRM: Заказ отменен'),
]


# ==================== TELEGRAM ====================

def send_telegram(message):
    """Отправка в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct)."""
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            r = requests.post(url, data={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
            }, proxies=proxies, timeout=30)
            if r.status_code == 200:
                return
        except Exception as e:
            logger.warning(f'Telegram (proxies={proxies}): {e}')


# ==================== СЕССИЯ ====================
# PARALLEL_2026-06-20: requests.Session не thread-safe при конкурентных POST.
# Каждый воркер создаёт свою сессию через create_session() вместо глобального SESSION.

def create_session():
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5,
                  status_forcelist=[500, 502, 503, 504],
                  allowed_methods=['POST', 'GET'])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


# ==================== БД ====================

def ensure_table_exists(conn):
    """Создаёт public.analytics_report_placement с полной 43-колоночной схемой ARP.
    Direct-строки заполняют только 28 Direct-колонок; 15 лид-колонок получают
    значения по умолчанию (BIGINT → 0, TEXT → NULL) и заполняются позже в step2."""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS public.{TARGET_TABLE} (
                row_hash         TEXT PRIMARY KEY,
                date             DATE,
                domain           TEXT,
                логин            TEXT,
                ad_network_type  TEXT,
                placement        TEXT,
                placement_key    TEXT,
                clicks           BIGINT,
                cost             NUMERIC,
                "Все формы"           BIGINT,
                "CRM: Заказ создан"   BIGINT,
                "CRM: Заказ оплачен"  BIGINT,
                "CRM: Спам заказ"     BIGINT,
                "CRM: Заказ отменен"  BIGINT,
                campaign_id      BIGINT,
                campaign_name    TEXT,
                campaign_code    TEXT,
                tp               TEXT,
                cpc_cpa          TEXT,
                site_quiz        TEXT,
                ad_group_id      BIGINT,
                key              TEXT,
                key2             TEXT,
                директолог       TEXT,
                город            TEXT,
                регион           TEXT,
                салон            TEXT,
                шаблон           TEXT,
                тип_сайта        TEXT,
                статус           TEXT,
                направление      TEXT,
                "номер кампании|название кампании" TEXT,
                updated_at       TIMESTAMP DEFAULT NOW(),
                "Название crm"   TEXT      DEFAULT NULL,
                тип_заявки       TEXT      DEFAULT NULL,
                kol_vo_zayavok   BIGINT    DEFAULT 0,
                korr             BIGINT    DEFAULT 0,
                kval             BIGINT    DEFAULT 0,
                priezd           BIGINT    DEFAULT 0,
                prodazhi         BIGINT    DEFAULT 0,
                nekorr           BIGINT    DEFAULT 0,
                ne_otvechaet     BIGINT    DEFAULT 0,
                filtr            BIGINT    DEFAULT 0,
                nedozvon         BIGINT    DEFAULT 0,
                priedet          BIGINT    DEFAULT 0,
                dohod_do_kredita BIGINT    DEFAULT NULL,
                dobro            BIGINT    DEFAULT NULL
            )
        """)
    conn.commit()
    logger.info('Таблица готова.')


def get_incremental_date_from(conn, fallback: str) -> str:
    """Возвращает MAX(date) - 61 день по Direct-строкам (логин IS NOT NULL).
    Leads-only строки (логин IS NULL) намеренно исключены: они всегда свежие
    и иначе сдвигали бы инкремент вперёд, скрывая пропуск исторических Direct-данных.
    Если Direct-строк нет — возвращает fallback (DATE_FROM из config)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MAX(date) FROM public.{TARGET_TABLE} WHERE логин IS NOT NULL"
        )
        max_date = cur.fetchone()[0]
    if max_date is None:
        logger.info(f'Direct-строк нет, используем fallback DATE_FROM: {fallback}')
        return fallback
    inc_from = (max_date - timedelta(days=61)).strftime('%Y-%m-%d')
    logger.info(f'MAX(date) Direct-строк={max_date} → инкремент с {inc_from}')
    return inc_from


def delete_rows_from(conn, date_from: str) -> int:
    """Удаляет ТОЛЬКО Direct-строки (логин IS NOT NULL) где date >= date_from.
    Leads-only строки (логин IS NULL) не трогаем — ими управляет step2 (этапы C/D)."""
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM public.{TARGET_TABLE} WHERE date >= %s AND логин IS NOT NULL",
            (date_from,)
        )
        deleted = cur.rowcount
    conn.commit()
    logger.info(f'Удалено Direct-строк (date >= {date_from}): {deleted:,}')
    return deleted


def load_gsheet_sites(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT LOWER(TRIM(domain)) AS domain,
                   salon, city, region, site_type, status, template
            FROM local_gsheet_sites
            WHERE domain IS NOT NULL AND TRIM(domain) != ''
        """)
        cols = [d[0] for d in cur.description]
        return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def load_accounts(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT domain, login_key,
                   all_forms, crm_order_created, crm_order_paid,
                   crm_spam_order, crm_order_canceled,
                   directologist
            FROM public.{SOURCE_TABLE}
            WHERE login_key IS NOT NULL
              AND TRIM(login_key) != ''
              AND login_key != 'Нет'
              AND login_key !~ '^-+$'
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    logger.info(f'Строк из {SOURCE_TABLE}: {len(rows)}')
    return rows


def upsert_rows(conn, rows):
    if not rows:
        return
    query = f"""
        INSERT INTO public.{TARGET_TABLE} (
            row_hash, date, domain, логин,
            ad_network_type, placement, clicks, cost,
            "Все формы", "CRM: Заказ создан", "CRM: Заказ оплачен",
            "CRM: Спам заказ", "CRM: Заказ отменен",
            campaign_id, campaign_name, campaign_code, tp, cpc_cpa, site_quiz,
            ad_group_id, key, placement_key, key2,
            директолог, город, регион, салон, шаблон, тип_сайта,
            статус, направление, updated_at
        ) VALUES %s
        ON CONFLICT (row_hash) DO UPDATE SET
            date            = EXCLUDED.date,
            clicks          = EXCLUDED.clicks,
            cost            = EXCLUDED.cost,
            "Все формы"           = EXCLUDED."Все формы",
            "CRM: Заказ создан"   = EXCLUDED."CRM: Заказ создан",
            "CRM: Заказ оплачен"  = EXCLUDED."CRM: Заказ оплачен",
            "CRM: Спам заказ"     = EXCLUDED."CRM: Спам заказ",
            "CRM: Заказ отменен"  = EXCLUDED."CRM: Заказ отменен",
            updated_at      = EXCLUDED.updated_at
    """
    deduped = {row[0]: row for row in rows}
    with conn.cursor() as cur:
        execute_values(cur, query, list(deduped.values()))
    conn.commit()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================

def make_row_hash(domain, date_val, campaign_id, ad_group_id, network_type, placement):
    key = f'{domain}|{date_val}|{campaign_id}|{ad_group_id}|{network_type}|{placement}'
    return hashlib.md5(key.encode('utf-8')).hexdigest()


_CYR_S = 'с'  # кириллическая 'с' — опечатка в cpc/cpa в названиях кампаний


def _normalize_campaign_name(s):
    s = s.replace('cp' + _CYR_S, 'cpc')
    s = s.replace('kviz', 'quiz').replace('Kviz', 'Quiz')
    s = s.replace('tp8_cpa_site', 'tp8_cpc_site')
    return s


def parse_campaign_code(campaign_name):
    if not campaign_name or pd.isna(campaign_name):
        return None, None, None, None
    try:
        first_part = _normalize_campaign_name(str(campaign_name).split(' — ')[0])
        match = re.search(r'(tp\d+_(?:cpc|cpa)_(?:site|quiz))', first_part)
        if match:
            code = match.group(1)
            parts = code.split('_')
            return code, parts[0] if parts else None, parts[1] if len(parts) > 1 else None, parts[2] if len(parts) > 2 else None
    except Exception:
        pass
    return None, None, None, None


def safe_int(val, default=0):
    try:
        if val in [None, '', '--', 'nan']:
            return default
        return int(float(val))
    except Exception:
        return default


def safe_float(val, default=0.0):
    try:
        if val in [None, '', '--', 'nan']:
            return default
        return float(val)
    except Exception:
        return default


def get_conv(row, goal_map, display_name):
    gid = goal_map.get(display_name)
    if not gid:
        return None
    col = f'Conversions_{gid}_AUTO'
    v = row.get(col)
    return safe_int(v) if v is not None else None


def normalize_columns(df):
    if df.empty:
        return df
    df.columns = df.columns.str.strip()
    col_map = {}
    for col in df.columns:
        u = col.upper()
        if 'CAMPAIGNID'     in u: col_map[col] = 'CampaignId'
        elif 'CAMPAIGNNAME'  in u: col_map[col] = 'CampaignName'
        elif u == 'DATE':          col_map[col] = 'Date'
        elif 'ADGROUPID'    in u: col_map[col] = 'AdGroupId'
        elif 'ADNETWORKTYPE' in u: col_map[col] = 'AdNetworkType'
        elif 'PLACEMENT'    in u: col_map[col] = 'Placement'
        elif 'CLICKS'       in u: col_map[col] = 'Clicks'
        elif 'COST'         in u: col_map[col] = 'Cost'
    if col_map:
        df = df.rename(columns=col_map)
    return df


def generate_weekly_ranges(date_from_str, date_to_str):
    start = datetime.strptime(date_from_str, '%Y-%m-%d').date()
    end   = datetime.strptime(date_to_str,   '%Y-%m-%d').date()
    ranges, current = [], start
    while current <= end:
        week_end = min(current + timedelta(days=6), end)
        ranges.append((current.strftime('%Y-%m-%d'), week_end.strftime('%Y-%m-%d')))
        current = week_end + timedelta(days=1)
    return ranges


# ==================== ТОКЕН-КЭШ (потокобезопасный) ====================

# TOKENCACHE_2026-06-20: in-memory зеркало персистентного кэша.
# Загружается из JSON в начале прогона, сохраняется по мере нахождения рабочих токенов.
# Ключ: login (str) → 0-based индекс в TOKENS (int).
# PARALLEL_2026-06-20: _CACHE_LOCK защищает _token_start при конкурентной записи из воркеров.
_token_start: dict[str, int] = {}
_CACHE_LOCK = threading.Lock()


def _load_token_cache() -> None:
    """Загружает кэш login→token_idx из JSON-файла в _token_start (in-memory)."""
    global _token_start
    if not os.path.exists(_TOKEN_CACHE_FILE):
        logger.info('TOKENCACHE: файл отсутствует, начинаем с пустого кэша')
        return
    try:
        with open(_TOKEN_CACHE_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        # Валидируем: только int-значения в диапазоне 0..len(TOKENS)-1
        validated = {
            k: v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, int) and 0 <= v < len(TOKENS)
        }
        with _CACHE_LOCK:
            _token_start.update(validated)
        logger.info(f'TOKENCACHE: загружено {len(validated)} записей из {_TOKEN_CACHE_FILE}')
    except Exception as e:
        logger.warning(f'TOKENCACHE: не удалось загрузить кэш ({e}), продолжаем без него')


def _save_token_cache() -> None:
    """Сохраняет текущий in-memory кэш в JSON-файл (атомарная запись через tmp).
    PARALLEL_2026-06-20: вызывается только под _CACHE_LOCK (caller держит лок)."""
    try:
        tmp_path = _TOKEN_CACHE_FILE + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(_token_start, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _TOKEN_CACHE_FILE)
    except Exception as e:
        logger.warning(f'TOKENCACHE: не удалось сохранить кэш ({e})')


def _get_cached_token_idx(login: str) -> int:
    """Потокобезопасное чтение кэша."""
    with _CACHE_LOCK:
        return _token_start.get(login, 0)


def _update_token_cache(login: str, new_idx: int) -> None:
    """Потокобезопасное обновление кэша + персистирование."""
    with _CACHE_LOCK:
        if _token_start.get(login) != new_idx:
            _token_start[login] = new_idx
            _save_token_cache()


def _invalidate_token_cache(login: str) -> None:
    """Потокобезопасная инвалидация записи в кэше."""
    with _CACHE_LOCK:
        if login in _token_start:
            del _token_start[login]
            _save_token_cache()
            logger.info(f'[{login}] TOKENCACHE: инвалидирована запись (токен отозван/сменился)')


# ==================== СЕМАФОРЫ ПО ТОКЕНАМ ====================

# PARALLEL_2026-06-20: по одному семафору на каждый токен-индекс (0..N-1).
# Семафор(WORKERS_PER_TOKEN) — не более WORKERS_PER_TOKEN одновременных отчётов на токен.
# Аккаунт захватывает семафор СВОЕГО токена (по кэшу или idx=0 для неизвестных).
# Если токен меняется по ходу перебора (400/53), семафор первого берётся при входе и
# удерживается всё время обработки аккаунта — безопасно (pessimistic).
_token_semaphores: list[threading.Semaphore] = []


def _init_semaphores(n_tokens: int) -> None:
    global _token_semaphores
    _token_semaphores = [threading.Semaphore(WORKERS_PER_TOKEN) for _ in range(n_tokens)]


def _sem_for(token_idx: int) -> threading.Semaphore:
    """Семафор для индекса токена (0-based). Безопасен при выходе за пределы."""
    idx = max(0, min(token_idx, len(_token_semaphores) - 1))
    return _token_semaphores[idx]


# ==================== DIRECT API ====================

def fetch_report(login, goals, d_from, d_to, session: requests.Session, prefix: str = ''):
    """TOKENCACHE_2026-06-20 + PARALLEL_2026-06-20: потокобезопасная выборка отчёта.

    Стратегии:
    1. ReportName включает суффикс токена — при смене токена создаётся новый отчёт
       (не попадаем в очередь от предыдущего токена).
    2. _token_start кэш (персистентный JSON, с Lock) — если для этого login
       ранее нашли рабочий токен K, сразу начинаем с него (пропускаем 0..K-1).
       При 400/53 на кэшированном токене — инвалидируем запись, перебираем остальные.
    3. MAX_QUEUE_ATTEMPTS — лимит ожидания очереди.
    4. session — передаётся из воркера (per-thread сессия, не глобальная).
    5. prefix — строка для логов, например '[t=1][12/872]'.
    """
    if not goals:
        return pd.DataFrame(), None, None
    last_error = None
    start_idx = _get_cached_token_idx(login)
    tokens_to_try = list(enumerate(TOKENS, 1))[start_idx:]
    for t_idx, token in tokens_to_try:
        attempt = 0
        # ReportName уникален по токену: смена токена → новый отчёт, нет попадания в чужую очередь
        report_name = f'RP_{login.replace(".", "_")}_{d_from}_{d_to}_t{t_idx}'
        while True:
            attempt += 1
            if attempt > MAX_QUEUE_ATTEMPTS:
                return None, None, (
                    f'Превышено время ожидания очереди '
                    f'({MAX_QUEUE_ATTEMPTS * WAIT_QUEUE_MIN}с, токен #{t_idx})'
                )
            headers = {
                'Authorization':       f'Bearer {token}',
                'Client-Login':        login,
                'processingMode':      'auto',
                'returnMoneyInMicros': 'false',
                'skipReportHeader':    'true',
                'skipColumnHeader':    'false',
                'skipReportSummary':   'true',
                'Content-Type':        'application/json',
            }
            body = json.dumps({'params': {
                'SelectionCriteria': {'DateFrom': d_from, 'DateTo': d_to},
                'Goals': goals,
                'FieldNames': [
                    'Date', 'CampaignId', 'CampaignName', 'AdGroupId',
                    'AdNetworkType', 'Placement', 'Clicks', 'Cost', 'Conversions',
                ],
                'ReportName':      report_name,
                'ReportType':      'CUSTOM_REPORT',
                'DateRangeType':   'CUSTOM_DATE',
                'Format':          'TSV',
                'IncludeVAT':      'YES',
                'IncludeDiscount': 'NO',
                'AttributionModels': ['AUTO'],
            }})
            try:
                r = session.post(REPORTS_URL, data=body, headers=headers, timeout=(60, 300))
                r.encoding = 'utf-8'
                if r.status_code == 200:
                    data = r.text.strip()
                    # Запоминаем рабочий токен (0-based индекс = t_idx-1), потокобезопасно
                    _update_token_cache(login, t_idx - 1)
                    if not data:
                        return pd.DataFrame(), t_idx, None
                    df = pd.read_csv(StringIO(data), sep='\t')
                    return normalize_columns(df), t_idx, None
                elif r.status_code in (201, 202):
                    retry_in = int(r.headers.get('retryIn', WAIT_QUEUE_MIN))
                    wait = max(retry_in, WAIT_QUEUE_MIN)
                    label = 'В очереди' if r.status_code == 201 else 'Формируется'
                    logger.info(f'{prefix}[{login}] {label}, ждём {wait}с...')
                    time.sleep(wait)
                    continue
                elif r.status_code == 429:
                    wait = int(r.headers.get('Retry-After', 5))
                    logger.warning(f'{prefix}[{login}] 429, ждём {wait}с')
                    time.sleep(wait)
                    continue
                elif r.status_code == 401:
                    last_error = '401: невалидный токен'
                    logger.warning(f'{prefix}[{login}] {last_error} (#{t_idx})')
                    break
                elif r.status_code == 404:
                    last_error = '404: аккаунт не найден'
                    if t_idx == len(TOKENS):
                        return pd.DataFrame(), None, 'ERR_404'
                    break
                elif r.status_code == 400:
                    err = r.text[:200] if r.text else ''
                    # error_code 53 = Invalid OAuth token → пробуем следующий токен.
                    # Если этот токен был в кэше — инвалидируем запись (token мог быть
                    # отозван или переведён в другое агентство).
                    if '"error_code":"53"' in err or '"error_code": "53"' in err:
                        last_error = f'400/53: невалидный токен (#{t_idx})'
                        logger.warning(f'{prefix}[{login}] {last_error}')
                        if _get_cached_token_idx(login) == t_idx - 1:
                            _invalidate_token_cache(login)
                        break
                    logger.error(f'{prefix}[{login}] 400: {err}')
                    return None, None, f'ERR_400: {err}'
                else:
                    last_error = f'Status {r.status_code}'
                    logger.warning(f'{prefix}[{login}] {last_error}')
                    time.sleep(1)
                    continue
            except requests.exceptions.Timeout:
                wait = min(2 ** attempt, 60)
                logger.warning(f'{prefix}[{login}] Таймаут, ждём {wait}с')
                time.sleep(wait)
            except requests.exceptions.ConnectionError:
                wait = min(2 ** attempt, 60)
                logger.warning(f'{prefix}[{login}] Ошибка соединения, ждём {wait}с')
                time.sleep(wait)
            except Exception as e:
                logger.exception(f'{prefix}[{login}] Неожиданная ошибка: {e}')
                time.sleep(1)
    return None, None, f'Все токены исчерпаны. Последняя ошибка: {last_error}'


# ==================== ВОРКЕР ====================

def _process_account(
    acc: dict,
    gsheet_map: dict,
    weekly_ranges: list,
    goal_map: dict,
    acc_idx: int,
    total: int,
    done_counter: list,   # [int] — атомарный счётчик через lock
    done_lock: threading.Lock,
    stats: dict,
    stats_lock: threading.Lock,
) -> None:
    """PARALLEL_2026-06-20: воркер для одного аккаунта.

    Создаёт собственную DB-коннекцию и requests.Session.
    Захватывает семафор токена на всё время обработки аккаунта.
    Освобождает семафор и коннекцию при любом исходе.
    """
    login      = str(acc['login_key']).strip()
    domain     = acc.get('domain', '') or ''
    direktolog = str(acc.get('directologist') or '')
    gs         = gsheet_map.get(domain.lower(), {})

    # Определяем семафор по кэшированному токену (или 0 для неизвестных)
    cached_idx = _get_cached_token_idx(login)
    sem = _sem_for(cached_idx)

    with sem:
        # Обновляем счётчик выполненных
        with done_lock:
            done_counter[0] += 1
            done_num = done_counter[0]

        prefix = f'[t={cached_idx+1}][{done_num}/{total}] '

        logger.info(f'{prefix}{login} | домен: {domain} | целей: {len(goal_map)}')

        # Каждый воркер — своя DB-коннекция (нет общего курсора, нет дедлоков)
        try:
            conn = psycopg2.connect(**DB_DST)
            conn.autocommit = False
        except Exception as e:
            logger.error(f'{prefix}{login} | ошибка подключения к БД: {e}')
            with stats_lock:
                stats['error'] += 1
            return

        # Каждый воркер — своя requests.Session (Session не thread-safe)
        session = create_session()

        account_rows = 0
        account_ok   = True

        try:
            for w_from, w_to in weekly_ranges:
                df_w, t_used, err = fetch_report(
                    login, list(goal_map.values()), w_from, w_to,
                    session=session, prefix=prefix,
                )

                if err == 'ERR_404':
                    logger.warning(f'{prefix}{login} | 404, пропускаем')
                    with stats_lock:
                        stats['not_found'] += 1
                    account_ok = False
                    break
                elif df_w is None:
                    logger.error(f'{prefix}{login} | неделя {w_from}–{w_to} | {err}')
                    with stats_lock:
                        stats['error'] += 1
                    account_ok = False
                    break
                elif df_w.empty:
                    continue

                df_w = df_w.replace('--', 0).fillna(0)

                week_rows = []
                for _, row in df_w.iterrows():
                    date_str      = str(row.get('Date', '') or '') or None
                    campaign_id   = safe_int(row.get('CampaignId', 0))
                    campaign_name_val = str(row.get('CampaignName', '') or '')
                    ad_group_id   = safe_int(row.get('AdGroupId', 0))
                    network_type  = str(row.get('AdNetworkType', '') or '')
                    placement     = str(row.get('Placement', '') or '')
                    clicks        = safe_int(row.get('Clicks', 0))
                    cost          = safe_float(row.get('Cost', 0.0))

                    camp_code, tp, cpc_cpa, site_quiz = parse_campaign_code(campaign_name_val)

                    key           = f'{date_str or ""}|{campaign_id}|{ad_group_id}|{placement}'.lower()
                    placement_key = re.sub(r'^(www\.|m\.)', '', placement.lower())
                    key2          = f'{date_str or ""}|{campaign_id}|{ad_group_id}|{placement_key}'
                    row_hash      = make_row_hash(domain, date_str or '', campaign_id, ad_group_id, network_type, placement)

                    week_rows.append((
                        row_hash, date_str, domain, login,
                        network_type, placement, clicks, cost,
                        get_conv(row, goal_map, 'Все формы'),
                        get_conv(row, goal_map, 'CRM: Заказ создан'),
                        get_conv(row, goal_map, 'CRM: Заказ оплачен'),
                        get_conv(row, goal_map, 'CRM: Спам заказ'),
                        get_conv(row, goal_map, 'CRM: Заказ отменен'),
                        campaign_id, campaign_name_val,
                        camp_code, tp, cpc_cpa, site_quiz,
                        ad_group_id, key, placement_key, key2,
                        direktolog,
                        gs.get('city') or None,
                        gs.get('region') or None,
                        gs.get('salon') or None,
                        gs.get('template') or None,
                        gs.get('site_type') or None,
                        gs.get('status') or None,
                        site_quiz or None,
                        datetime.now(),
                    ))

                try:
                    upsert_rows(conn, week_rows)
                    account_rows += len(week_rows)
                    logger.info(f'{prefix}{login} | {w_from}–{w_to} | +{len(week_rows)} строк')
                except Exception as e:
                    logger.exception(f'{prefix}{login} | ошибка INSERT {w_from}–{w_to}: {e}')
                    conn.rollback()
                    account_ok = False
                    break

                time.sleep(PAUSE_BETWEEN)

        finally:
            conn.close()
            session.close()

        with stats_lock:
            if account_ok and account_rows > 0:
                stats['success'] += 1
            elif account_ok:
                stats['empty'] += 1


# ==================== MAIN ====================

def main():
    start_time = datetime.now()
    logger.info(f'СТАРТ: {start_time}')

    # TOKENCACHE_2026-06-20: загружаем кэш login→token_idx из предыдущих прогонов
    _load_token_cache()

    # PARALLEL_2026-06-20: инициализируем семафоры по числу токенов
    _init_semaphores(len(TOKENS))
    max_workers = len(TOKENS) * WORKERS_PER_TOKEN
    logger.info(
        f'PARALLEL: {len(TOKENS)} токенов × {WORKERS_PER_TOKEN} воркеров/токен = '
        f'{max_workers} воркеров (лимит Reports API: 5/токен, запас 40%)'
    )

    try:
        conn = psycopg2.connect(**DB_DST)
        conn.autocommit = False
    except Exception as e:
        logger.exception(f'Ошибка подключения к БД: {e}')
        send_telegram(f'<b>step1_fetch_direct</b>\n\nОшибка БД:\n<code>{e}</code>')
        return

    try:
        ensure_table_exists(conn)
        if DATE_FROM_MANUAL:
            fetch_from = DATE_FROM_MANUAL
            logger.info(
                f'DATE_FROM_MANUAL задан → принудительный старт с {fetch_from} '
                f'(инкремент пропускается)'
            )
        else:
            fetch_from = get_incremental_date_from(conn, DATE_FROM)
        delete_rows_from(conn, fetch_from)
        raw_accounts = load_accounts(conn)
        gsheet_map = load_gsheet_sites(conn)
        logger.info(f'Доменов из gsheet_sites: {len(gsheet_map)}')
        logger.info(f'Период: {fetch_from} → {DATE_TO}')
    except Exception as e:
        logger.exception(f'Ошибка загрузки данных: {e}')
        conn.close()
        return
    finally:
        # Главная коннекция нужна только для инициализации — закрываем
        conn.close()

    valid_accs, invalid, no_goals = [], [], []
    for a in raw_accounts:
        login = str(a.get('login_key') or '').strip()
        if not login or not re.fullmatch(r'[a-zA-Z0-9.\-_]+', login):
            if login:
                invalid.append(login)
            continue
        has_goals = any(a.get(src_col) for src_col, _ in GOAL_FIELDS)
        if not has_goals:
            no_goals.append(login)
            continue
        valid_accs.append(a)

    def _goal_count(a):
        return sum(1 for src_col, _ in GOAL_FIELDS if a.get(src_col))

    best = {}
    for a in valid_accs:
        login = str(a['login_key']).strip()
        if login not in best or _goal_count(a) > _goal_count(best[login]):
            best[login] = a
    valid_accs = list(best.values())

    logger.info(f'Валидных: {len(valid_accs)} | Невалидных: {len(invalid)} | Без целей: {len(no_goals)}')

    weekly_ranges = generate_weekly_ranges(fetch_from, DATE_TO)
    logger.info(f'Недельных батчей: {len(weekly_ranges)}')

    # Строим goal_map для каждого аккаунта заранее (не в воркере)
    acc_goal_maps = {}
    for acc in valid_accs:
        login = str(acc['login_key']).strip()
        goal_map = {}
        for src_col, display_name in GOAL_FIELDS:
            val = acc.get(src_col)
            if val:
                try:
                    goal_map[display_name] = str(int(float(val)))
                except (ValueError, TypeError):
                    pass
        acc_goal_maps[login] = goal_map

    total = len(valid_accs)
    stats: dict[str, int] = {'success': 0, 'empty': 0, 'error': 0, 'not_found': 0}
    stats_lock = threading.Lock()
    done_counter = [0]  # список из одного int — мутабелен через ссылку
    done_lock = threading.Lock()

    logger.info(f'PARALLEL_2026-06-20: запускаем {total} аккаунтов в {max_workers} воркерах')

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_account,
                acc,
                gsheet_map,
                weekly_ranges,
                acc_goal_maps[str(acc['login_key']).strip()],
                idx,
                total,
                done_counter,
                done_lock,
                stats,
                stats_lock,
            ): acc
            for idx, acc in enumerate(valid_accs, 1)
        }

        for future in as_completed(futures):
            acc = futures[future]
            login = str(acc.get('login_key', '?')).strip()
            try:
                future.result()
            except Exception as e:
                logger.exception(f'[PARALLEL] Необработанное исключение воркера [{login}]: {e}')
                with stats_lock:
                    stats['error'] += 1

    elapsed = str(datetime.now() - start_time).split('.')[0]
    summary = (
        f'<b>step1_fetch_direct</b>\n\n'
        f'Период: {fetch_from} → {DATE_TO}\n'
        f'Время: {elapsed}\n\n'
        f'Успешно: {stats["success"]}\n'
        f'Нет данных: {stats["empty"]}\n'
        f'Ошибки: {stats["error"]}\n'
        f'404: {stats["not_found"]}\n'
        f'Всего аккаунтов: {total}\n'
        f'Воркеров: {max_workers} ({len(TOKENS)} токена × {WORKERS_PER_TOKEN}/токен)'
    )
    logger.info(f'ИТОГО: {stats}')
    logger.info(f'ЗАВЕРШЕНО за {elapsed}')
    send_telegram(summary)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nПрервано')
        sys.exit(0)
    except Exception as e:
        logger.exception(f'Фатальная ошибка: {e}')
        send_telegram(f'<b>step1_fetch_direct</b>\n\nФатальная ошибка:\n<code>{e}</code>')
        sys.exit(1)
