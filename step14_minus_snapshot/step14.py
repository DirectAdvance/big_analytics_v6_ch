#!/usr/bin/env python3
"""
step14_minus_snapshot — снапшот минус-фраз Яндекс.Директ в PostgreSQL.
# RENAME_YANDEX_DIRECT_MINUS_SNAPSHOT_2026-06-17
# SCHEMA_V2_2026-06-17
# RETENTION_30D_2026-06-17
# BLOCK_COL_2026-06-17
# SPECIALIST_COL_2026-06-19

Считает количество минус-фраз на трёх уровнях:
  - minus_in_campaign : минус-фразы списком на кампанию (NegativeKeywords)
  - minus_in_groups   : сумма минус-фраз по группам (AdGroups.NegativeKeywords)
  - minus_in_sets     : минус-фразы через наборы (NegativeKeywordSharedSets), привязанные к кампании

minus_total = minus_in_campaign + minus_in_groups + minus_in_sets

Записывает снапшот в public.yandex_direct_minus_snapshot (ON CONFLICT DO NOTHING — идемпотентный).
Запускается ночным пайплайном (pipeline_night.py) для блоков из MINUS_SNAPSHOT_BLOCKS.

Использование:
  python3 step14_minus_snapshot/step14.py --block tp2 --block tp4
  python3 step14_minus_snapshot/step14.py --block tp2 --states ON,SUSPENDED

Аргументы:
  --block БЛОК     имя блока (фильтр по подстроке в имени кампании).
                   Может задаваться несколько раз: --block tp2 --block tp4.
                   Управляет сканированием кампаний, в таблицу НЕ пишется (нет колонки block_filter).
  --states СПИСОК  состояния кампаний через запятую (по умолчанию ON,SUSPENDED,OFF,ENDED,CONVERTED).
                   ARCHIVED исключён: API не раскрывает наборов для ARCHIVED-кампаний.
  --dry-run        только вывод, без записи в БД.
  --max-logins N   ограничить число логинов (для теста).
  --token KEY      работать только под одним токеном (имя ключа из .env).

Таблицы:
  public.yandex_direct_minus_snapshot — снапшоты (append, ON CONFLICT DO NOTHING)
  public.v_yandex_direct_minus_delta  — VIEW: LAG-динамика по (login, campaign_id) ORDER BY "date"
  public.data_quality_log             — запись rows_affected после прогона

НЕ трогает: big_analytics_full, big_analytics_unified, fact_big_analytics,
corrections.py, build_star.py — golden Кудерко не затрагивается.

Retention: после вставки строк текущего прогона удаляются строки старше RETENTION_DAYS
(по умолчанию 30) дней. Текущая и вчерашняя дата под удаление не попадают.
Сбой очистки логируется как warning, не валит шаг.
"""

import sys
import os
import time
import logging
import argparse
import datetime
import uuid
from collections import defaultdict

import requests
import psycopg2

# ── Путь к корню big_analytics_v5 ────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from config.db import init_pool, get_conn, put_conn, close_pool  # noqa: E402
from pipeline import log_step  # noqa: E402

# ── Токены Директа из .secret/.env ───────────────────────────────────────────
_SECRET_ENV = None
for _p in [_ROOT] + list(os.path.abspath(_ROOT + '/' + '../' * i) for i in range(1, 5)):
    _cand = os.path.join(_p, '.secret', '.env')
    if os.path.isfile(_cand):
        _SECRET_ENV = _cand
        break

TOKEN_PREFIXES = ('DIRECT_TOKEN_', 'DIRECT_REVIEWS_TOKEN_')
API = 'https://api.direct.yandex.com/json/v5/'
_SESSION = requests.Session()

# ── Максимум Id в одном запросе Директа (иначе ошибка 4001) ──────────────────
CAMPAIGN_IDS_CHUNK = 10
# Наборы: API не документирует лимит явно, используем тот же батч по 10
SETS_IDS_CHUNK = 10

# ── Retention: сколько дней хранить строки снапшота ──────────────────────────
# Строки с "date" < (current_date - INTERVAL 'N days') удаляются после вставки.
# VIEW v_yandex_direct_minus_delta нужно ≥2 соседних дня — 30 дней хватает с запасом.
RETENTION_DAYS = 30

# ── Блочный маппинг (для вычисляемой колонки block) ──────────────────────────
# Порядок важен: первое совпадение — победитель. Кампания не матчит оба блока
# одновременно (tp2 и tp4 — взаимоисключающие подстроки в именах кампаний).
# Для добавления нового tp-блока: вставить пару (подстрока, метка) ПЕРЕД ('', 'прочее').
_BLOCK_MAP: list[tuple[str, str]] = [
    ('tp2', 'tp2'),
    ('tp4', 'tp4'),
    ('', 'прочее'),  # fallback — всегда последним
]


def _detect_block(campaign_name: str | None) -> str:
    """Определяет блок кампании по подстроке в имени (нижний регистр).
    Возвращает первое совпадение из _BLOCK_MAP, иначе 'прочее'."""
    name_lower = (campaign_name or '').lower()
    for substr, label in _BLOCK_MAP:
        if substr == '' or substr in name_lower:
            return label
    return 'прочее'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('step14')

# ── DDL ───────────────────────────────────────────────────────────────────────

DDL_TABLE = """
CREATE TABLE IF NOT EXISTS public.yandex_direct_minus_snapshot (
    id                BIGSERIAL PRIMARY KEY,
    "date"            DATE        NOT NULL,
    login             TEXT        NOT NULL,
    campaign_id       BIGINT      NOT NULL,
    campaign_name     TEXT,
    campaign_state    TEXT,
    block             TEXT,
    minus_in_campaign INT         NOT NULL DEFAULT 0,
    minus_in_groups   INT         NOT NULL DEFAULT 0,
    minus_in_sets     INT         NOT NULL DEFAULT 0,
    minus_total       INT         NOT NULL DEFAULT 0,
    has_minus         BOOLEAN     NOT NULL DEFAULT FALSE,
    check_ok          BOOLEAN     NOT NULL DEFAULT TRUE,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    "специалист"      TEXT,
    CONSTRAINT yandex_direct_minus_snapshot_uniq
        UNIQUE ("date", login, campaign_id)
);

CREATE INDEX IF NOT EXISTS yandex_direct_minus_snapshot_date_idx
    ON public.yandex_direct_minus_snapshot ("date");
-- INDEX_AUDIT_2026-06-27: удалены мёртвые (idx_scan=0):
--   yandex_direct_minus_snapshot_campaign_id_idx (покрыт UNIQUE(date,login,campaign_id)),
--   yandex_direct_minus_snapshot_login_idx (покрыт UNIQUE).
"""

# DDL для миграции существующей таблицы (добавляет колонку если её ещё нет)
DDL_ALTER_SPECIALIST = """
ALTER TABLE public.yandex_direct_minus_snapshot
    ADD COLUMN IF NOT EXISTS "специалист" TEXT;
"""

DDL_VIEW = """
DROP VIEW IF EXISTS public.v_yandex_direct_minus_delta;
CREATE VIEW public.v_yandex_direct_minus_delta AS
-- SPECIALIST_VIEW_2026-06-19: добавлена колонка "специалист" из snapshot
SELECT
    "date",
    login,
    campaign_id,
    campaign_name,
    campaign_state,
    block,
    "специалист",
    minus_in_campaign,
    minus_in_groups,
    minus_in_sets,
    minus_total,
    has_minus,
    check_ok,
    LAG(minus_total) OVER (
        PARTITION BY login, campaign_id
        ORDER BY "date"
    ) AS minus_total_prev,
    minus_total - LAG(minus_total) OVER (
        PARTITION BY login, campaign_id
        ORDER BY "date"
    ) AS delta,
    CASE
        WHEN LAG(minus_total) OVER (
            PARTITION BY login, campaign_id
            ORDER BY "date"
        ) IS NULL
            THEN 'первый замер'
        WHEN minus_total > LAG(minus_total) OVER (
            PARTITION BY login, campaign_id
            ORDER BY "date"
        )
            THEN 'добавили'
        WHEN minus_total < LAG(minus_total) OVER (
            PARTITION BY login, campaign_id
            ORDER BY "date"
        )
            THEN 'СНЯЛИ'
        ELSE 'без изменений'
    END AS dynamics
FROM public.yandex_direct_minus_snapshot
ORDER BY login, campaign_id, "date";
"""

INSERT_SQL = """
INSERT INTO public.yandex_direct_minus_snapshot
    ("date", login, campaign_id, campaign_name, campaign_state, block,
     minus_in_campaign, minus_in_groups, minus_in_sets, minus_total,
     has_minus, check_ok, "специалист")
VALUES
    (%s, %s, %s, %s, %s, %s,
     %s, %s, %s, %s,
     %s, %s, %s)
ON CONFLICT ("date", login, campaign_id) DO NOTHING
"""


# ── Загрузка токенов ──────────────────────────────────────────────────────────

def load_tokens(only_key: str | None = None) -> dict[str, str]:
    if _SECRET_ENV is None or not os.path.isfile(_SECRET_ENV):
        logger.error('Не найден .secret/.env — секреты ожидаются в .secret/.env')
        sys.exit(1)
    toks: dict[str, str] = {}
    for line in open(_SECRET_ENV, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip(); v = v.strip().strip('"').strip("'")
        if v and any(k.startswith(p) for p in TOKEN_PREFIXES):
            toks[k] = v
    if not toks:
        logger.error('В .secret/.env нет токенов Директа')
        sys.exit(1)
    if only_key:
        if only_key not in toks:
            logger.error('Ключ %s не найден в .secret/.env', only_key)
            sys.exit(1)
        return {only_key: toks[only_key]}
    return toks


# ── HTTP-вызов API Директа ────────────────────────────────────────────────────

def _call(service: str, payload: dict, token: str, login: str | None = None) -> dict:
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept-Language': 'ru',
        'Content-Type': 'application/json; charset=utf-8',
    }
    if login:
        headers['Client-Login'] = login
    last = '?'
    for _ in range(5):
        try:
            r = _SESSION.post(API + service, json=payload, headers=headers, timeout=45)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2)
                continue
            return r.json()
        except Exception as exc:
            last = str(exc)[:80]
            time.sleep(2)
    return {'error': {'error_code': -1, 'error_detail': f'net:{last}'}}


def _n_items(nk: object) -> int:
    """Количество фраз в поле NegativeKeywords (Items-обёртка или список)."""
    if not nk:
        return 0
    if isinstance(nk, dict):
        return len(nk.get('Items', []))
    return len(nk)


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ── Сбор логинов агентства ────────────────────────────────────────────────────

def enumerate_logins(toks: dict[str, str]) -> dict[str, str]:
    """login -> token_key"""
    login_tok: dict[str, str] = {}
    for k, tok in toks.items():
        off = 0
        while True:
            r = _call('agencyclients', {'method': 'get', 'params': {
                'SelectionCriteria': {'Archived': 'NO'},
                'FieldNames': ['Login'],
                'Page': {'Limit': 10000, 'Offset': off},
            }}, tok)
            if 'result' not in r:
                logger.warning('[warn] AgencyClients под %s: %s', k,
                               r.get('error', {}).get('error_detail'))
                break
            for c in r['result'].get('Clients', []):
                login_tok.setdefault(c['Login'], k)
            lim = r['result'].get('LimitedBy')
            if lim:
                off = lim
                time.sleep(0.3)
            else:
                break
    return login_tok


# ── Получение наборов минус-фраз (батчем по уникальным set_id) ───────────────

def fetch_sets_sizes(set_ids: list[int], token: str, login: str) -> tuple[dict[int, int], bool]:
    """
    Возвращает ({set_id: кол-во фраз}, sets_ok).
    sets_ok=False если хотя бы один батч вернул ошибку API — данные по наборам неполные.
    ARCHIVED-кампании не раскрывают наборов через API; при ошибке cnt_sets для chunk = 0.
    Кэшируется на уровне кампании-батча (вызывается с уже уникальными id).
    """
    sizes: dict[int, int] = {}
    sets_ok = True
    if not set_ids:
        return sizes, sets_ok
    for chunk in _chunks(set_ids, SETS_IDS_CHUNK):
        r = _call('negativekeywordsharedsets', {'method': 'get', 'params': {
            'SelectionCriteria': {'Ids': chunk},
            'FieldNames': ['Id', 'NegativeKeywords'],
        }}, token, login)
        if 'result' not in r:
            logger.debug('negativekeywordsharedsets error: %s',
                         r.get('error', {}).get('error_detail'))
            # Сбой API → замер наборов недостоверен
            sets_ok = False
            for sid in chunk:
                sizes[sid] = 0
            continue
        for s in r['result'].get('NegativeKeywordSharedSets', []):
            sid = s.get('Id')
            nk = s.get('NegativeKeywords')
            sizes[sid] = _n_items(nk)
    return sizes, sets_ok


# ── Основная логика для одного логина ────────────────────────────────────────

def process_login(
    login: str,
    token: str,
    blocks: list[str],
    states: list[str],
    snap_date: datetime.date,
    # out-параметр: список строк для вставки
    out_rows: list,
    specialist_map: dict | None = None,
) -> None:
    """
    Тянет кампании логина (с NegativeKeywordSharedSetIds), группы, наборы.
    Добавляет строки в out_rows — по одной на (login, campaign_id).
    Один campaign_id может добавиться несколько раз если матчит несколько блоков —
    при вставке второй и последующие DO NOTHING по UNIQUE("date", login, campaign_id).
    """
    # ── 1. Кампании ───────────────────────────────────────────────────────────
    camps: list[dict] = []
    off = 0
    while True:
        r = _call('campaigns', {'method': 'get', 'params': {
            'SelectionCriteria': {'States': states},
            'FieldNames': ['Id', 'Name', 'State', 'NegativeKeywords'],
            # TextCampaignFieldNames нужны для NegativeKeywordSharedSetIds
            'TextCampaignFieldNames': ['NegativeKeywordSharedSetIds'],
            'Page': {'Limit': 10000, 'Offset': off},
        }}, token, login)
        if 'result' not in r:
            logger.warning('[%s] campaigns.get error: %s', login,
                           r.get('error', {}).get('error_detail'))
            return
        camps += r['result'].get('Campaigns', [])
        lim = r['result'].get('LimitedBy')
        if lim:
            off = lim
            time.sleep(0.2)
        else:
            break

    # ── 2. Фильтр по блокам + счётчик на кампанию ─────────────────────────────
    # Для каждого блока обрабатываем отдельно (одна кампания может попасть в несколько блоков)
    for block in blocks:
        filtered = [c for c in camps if block.lower() in (c.get('Name') or '').lower()]
        if not filtered:
            continue

        ids = [c['Id'] for c in filtered]

        # Счётчик NegativeKeywords на уровне кампании
        camp_cnt: dict[int, int] = {
            c['Id']: _n_items(c.get('NegativeKeywords')) for c in filtered
        }

        # set_id по кампании — из TextCampaign.NegativeKeywordSharedSetIds
        camp_set_ids: dict[int, list[int]] = {}
        for c in filtered:
            tc = c.get('TextCampaign') or {}
            shared = tc.get('NegativeKeywordSharedSetIds') or {}
            items = shared.get('Items', []) if isinstance(shared, dict) else (shared or [])
            camp_set_ids[c['Id']] = [int(x) for x in items] if items else []

        # Уникальные set_id по всему батчу — кэш размеров
        all_set_ids = list({sid for sids in camp_set_ids.values() for sid in sids})
        set_size_cache, sets_ok = fetch_sets_sizes(all_set_ids, token, login)

        # ── 3. Группы (батч по 10 campaign_id) ───────────────────────────────
        grp_cnt: dict[int, int] = defaultdict(int)
        grp_ok = True
        for ch in _chunks(ids, CAMPAIGN_IDS_CHUNK):
            off = 0
            while True:
                gr = _call('adgroups', {'method': 'get', 'params': {
                    'SelectionCriteria': {'CampaignIds': ch},
                    'FieldNames': ['CampaignId', 'NegativeKeywords'],
                    'Page': {'Limit': 10000, 'Offset': off},
                }}, token, login)
                if 'result' not in gr:
                    grp_ok = False
                    break
                for g in gr['result'].get('AdGroups', []):
                    cid = g['CampaignId']
                    grp_cnt[cid] += _n_items(g.get('NegativeKeywords'))
                lim = gr['result'].get('LimitedBy')
                if lim:
                    off = lim
                    time.sleep(0.2)
                else:
                    break
            time.sleep(0.1)

        # ── 4. Сборка строк ───────────────────────────────────────────────────
        for c in filtered:
            cid = c['Id']
            cc = camp_cnt.get(cid, 0)
            gc = grp_cnt.get(cid, 0)

            # cnt_sets: сумма размеров наборов, привязанных к этой кампании
            # per-campaign БЕЗ дедупликации (один набор на нескольких кампаниях считается
            # для каждой — это верная гранула для аудита наличия минусов у кампании)
            sc = sum(set_size_cache.get(sid, 0) for sid in camp_set_ids.get(cid, []))

            total = cc + gc + sc
            check_ok = grp_ok and sets_ok  # False при сбое групп ИЛИ наборов
            has_minus = total > 0

            # Если сбой данных и total == 0 — не можем утверждать «нет минусов»
            if not check_ok and total == 0:
                has_minus = False  # unknown, но not check_ok уже отражает это

            # Вычисляем блок из названия кампании (tp2/tp4/прочее)
            block = _detect_block(c.get('Name'))

            # Специалист: ищем по login_key = SPLIT_PART(login, '@', 1)
            # specialist_map {login_key -> directologist} из local_gsheet_sites
            login_key = login.split('@')[0]
            specialist = (specialist_map or {}).get(login_key)

            out_rows.append((
                snap_date,
                login,
                cid,
                c.get('Name'),
                c.get('State'),
                block,
                cc,
                gc,
                sc,
                total,
                has_minus,
                check_ok,
                specialist,
            ))


# ── DDL: создать таблицу и VIEW ───────────────────────────────────────────────

def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL_TABLE)
        # Миграция: добавляем колонку «специалист» если таблица уже существовала без неё
        cur.execute(DDL_ALTER_SPECIALIST)
        cur.execute(DDL_VIEW)
    conn.commit()
    logger.info('DDL: таблица и VIEW подтверждены (колонка «специалист» добавлена/проверена)')


# ── Загрузка справочника специалистов из local_gsheet_sites ──────────────────

def load_specialist_map(conn) -> dict[str, str]:
    """
    Возвращает dict {login_key -> directologist} из public.local_gsheet_sites.
    Дедупликация: если один login_key встречается с несколькими directologist,
    берём первый непустой по алфавиту. Мусорные login_key (длина ≤3) отсекаем.
    Связка со снапшотом: SPLIT_PART(snapshot.login, '@', 1) = login_key.
    Покрывает ~91% строк / ~97% minus_total (проверено 2026-06-19).
    """
    sql = """
        SELECT DISTINCT ON (login_key)
            login_key,
            directologist
        FROM public.local_gsheet_sites
        WHERE login_key IS NOT NULL
          AND length(trim(login_key)) > 3
          AND directologist IS NOT NULL
          AND directologist != ''
        ORDER BY login_key, directologist
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    result = {r[0]: r[1] for r in rows}
    logger.info('Справочник специалистов: %d логинов загружено из local_gsheet_sites', len(result))
    return result


# ── Бэкфилл колонки «специалист» для уже накопленных строк ──────────────────

def backfill_specialist(conn, specialist_map: dict[str, str]) -> int:
    """
    Заполняет «специалист» в уже существующих строках snapshot по мэппингу specialist_map.
    Обновляет только строки где «специалист» IS NULL (идемпотентно).
    Возвращает число обновлённых строк.
    """
    if not specialist_map:
        logger.warning('Бэкфилл: справочник специалистов пуст, пропускаем')
        return 0

    # UPDATE через VALUES-список: безопасный параметризованный запрос через mogrify.
    # psycopg2.extras.execute_values требует imports extras — используем mogrify напрямую.
    pairs = list(specialist_map.items())
    with conn.cursor() as cur:
        values_clause = ','.join(
            cur.mogrify('(%s,%s)', (lk, spec)).decode()
            for lk, spec in pairs
        )
        sql_values = (
            'UPDATE public.yandex_direct_minus_snapshot AS s '
            'SET "специалист" = m.spec '
            'FROM (VALUES ' + values_clause + ') AS m(lk, spec) '
            "WHERE SPLIT_PART(s.login, '@', 1) = m.lk "
            'AND s."специалист" IS NULL'
        )
        cur.execute(sql_values)
        updated = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    logger.info('Бэкфилл: обновлено %d строк snapshot (колонка «специалист»)', updated)
    return updated



# ── Вставка строк ─────────────────────────────────────────────────────────────

def insert_rows(conn, rows: list) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)
        inserted = cur.rowcount
    conn.commit()
    return inserted


# ── Очистка устаревших снапшотов (retention) ─────────────────────────────────

def prune_old(conn, days: int = RETENTION_DAYS) -> int:
    """
    Удаляет строки с "date" < (current_date - INTERVAL 'N days').
    Возвращает число удалённых строк (0 если удалять нечего).
    Вызывается только после успешной вставки текущего прогона.
    Текущая и вчерашняя дата под удаление не попадают.
    """
    sql = """
        DELETE FROM public.yandex_direct_minus_snapshot
        WHERE "date" < (current_date - INTERVAL '%s days')
    """ % int(days)  # int() гарантирует отсутствие SQL-инъекции
    with conn.cursor() as cur:
        cur.execute(sql)
        deleted = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    return deleted


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description='step14 — снапшот минус-фраз Яндекс.Директ')
    ap.add_argument('--block', action='append', dest='blocks', default=[],
                    help='Фильтр по имени кампании (повторять для нескольких: --block tp2 --block tp4)')
    ap.add_argument('--states', default='ON,SUSPENDED,OFF,ENDED,CONVERTED',
                    help='Состояния кампаний через запятую (по умолчанию без ARCHIVED)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Только вывод, без записи в БД')
    ap.add_argument('--max-logins', type=int, default=None,
                    help='Ограничить число логинов (для теста)')
    ap.add_argument('--token', default=None,
                    help='Работать под одним токеном (имя ключа из .env)')
    args = ap.parse_args()

    blocks: list[str] = args.blocks or ['']  # пустая строка = все кампании
    states = [s.strip() for s in args.states.split(',') if s.strip()]
    snap_date = datetime.date.today()
    run_id = str(uuid.uuid4())[:8]

    logger.info('=== step14_minus_snapshot START snap_date=%s blocks=%s run_id=%s ===',
                snap_date, blocks, run_id)

    # ── Токены ───────────────────────────────────────────────────────────────
    toks = load_tokens(args.token)
    logger.info('Токенов: %d | блоки: %s | состояния: %s', len(toks), blocks, states)

    # ── Логины ───────────────────────────────────────────────────────────────
    logger.info('1/3 Собираю логины (AgencyClients)...')
    login_tok = enumerate_logins(toks)
    logins = list(login_tok)
    if args.max_logins:
        logins = logins[:args.max_logins]
    logger.info('  логинов к обходу: %d', len(logins))

    # ── Справочник специалистов (загружается из БД ДО сбора данных) ──────────
    # specialist_map нужен и для новых строк, и для бэкфилла существующих.
    # Загружаем заблаговременно чтобы не открывать соединение дважды.
    # При --dry-run загружать не нужно (нет записи в БД).
    specialist_map: dict[str, str] = {}
    if not args.dry_run:
        init_pool()
        _spec_conn = get_conn()
        try:
            specialist_map = load_specialist_map(_spec_conn)
        finally:
            put_conn(_spec_conn)
            close_pool()

    # ── Сбор данных ───────────────────────────────────────────────────────────
    logger.info('2/3 Тяну кампании и считаю минус-фразы (включая наборы)...')
    out_rows: list = []
    for i, login in enumerate(logins, 1):
        tok = toks[login_tok[login]]
        try:
            process_login(login, tok, blocks, states, snap_date, out_rows, specialist_map)
        except Exception as exc:
            logger.warning('[%s] неожиданная ошибка: %s', login, exc)
        if i % 50 == 0:
            logger.info('  %d/%d логинов обработано | строк собрано: %d',
                        i, len(logins), len(out_rows))

    logger.info('  итого строк: %d', len(out_rows))

    # ── Запись в БД ───────────────────────────────────────────────────────────
    if args.dry_run:
        logger.info('--dry-run: запись пропущена. Первые 5 строк:')
        for r in out_rows[:5]:
            logger.info('  %s', r)
        return

    logger.info('3/3 Запись в БД...')
    init_pool()
    conn = get_conn()
    try:
        ensure_schema(conn)

        # Бэкфилл: заполнить «специалист» в уже существующих строках
        # (выполняется один раз — обновляет только строки где «специалист» IS NULL)
        backfill_specialist(conn, specialist_map)

        inserted = insert_rows(conn, out_rows)
        logger.info('Вставлено строк (новых): %d из %d', inserted, len(out_rows))
        log_step(conn, run_id, 'step14_minus_snapshot', 'ok', rows_affected=inserted)

        # ── Retention: удаляем строки старше RETENTION_DAYS дней ─────────────
        # Выполняется только после успешной вставки (сбой вставки → до этого не доходим).
        # Сбой очистки не валит шаг — history не критична для падения.
        try:
            pruned = prune_old(conn, RETENTION_DAYS)
            if pruned > 0:
                logger.info(
                    'Retention: удалено %d строк старше %d дней',
                    pruned, RETENTION_DAYS,
                )
                log_step(conn, run_id, 'step14_minus_snapshot_prune', 'ok',
                         rows_affected=pruned)
            else:
                logger.info('Retention: строк старше %d дней нет, удалять нечего', RETENTION_DAYS)
        except Exception as prune_exc:
            logger.warning('Retention: сбой очистки (не критично): %s', prune_exc)
    finally:
        put_conn(conn)
        close_pool()

    logger.info('=== step14_minus_snapshot DONE snap_date=%s ===', snap_date)


if __name__ == '__main__':
    main()
