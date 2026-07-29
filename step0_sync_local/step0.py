"""
step0_sync_local/step0.py — синхронизация ad_analytics → ad_analytics_bi

Два соединения:
  src_conn  — ad_analytics (только чтение)
  dst_conn  — ad_analytics_bi (запись)

Алгоритм:
  local_yandex  — TRUNCATE+INSERT (полная замена, WHERE "Date" >= DATE_FROM)
                  id в источнике не уникален — PRIMARY KEY не используем
  все остальные — TRUNCATE+INSERT (полная замена, динамическая схема)

Проверка перед копированием:
  yandex: копируем только если ВСЕ три условия выполнены одновременно:
          src_count > dst_count И src_sum_cost > dst_sum_cost И src_max_date > dst_max_date
  Если dst пустой (count=0) — копируем всегда без проверки.
"""

import json
import logging
import time
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

from config.db import get_src_conn, put_src_conn
from config.settings import (
    DATE_FROM, STREAM_CHUNK,
    T_YANDEX_LOCAL,
    TRUNCATE_INSERT_TABLES,
    SRC_LEADS_ALL, SRC_PERFORM_LEADS,  # PERFORM_LEADS_2026-07-01
    T_LOCAL_VK_ADS,                    # VK_ADS_INTEGRATION_2026-07-06
)

logger = logging.getLogger('pipeline.step0')


# ── DDL локальных копий ───────────────────────────────────────────────────────

# id — без PRIMARY KEY: в источнике один id встречается у разных строк
DDL_YANDEX_LOCAL = f"""
CREATE TABLE IF NOT EXISTS {T_YANDEX_LOCAL} (
    id                  BIGINT,
    "Date"              DATE,
    "CampaignId"        BIGINT,
    "CampaignName"      TEXT,
    "AdGroupId"         BIGINT,
    "AdGroupName"       TEXT,
    "AdNetworkType"     TEXT,
    "Device"            TEXT,
    "Cost"              NUMERIC,
    total_cost          NUMERIC,
    "Impressions"       BIGINT,
    "Clicks"            BIGINT,
    "RlAdjustmentId"    BIGINT,
    account_login       TEXT,
    manager_login       TEXT,
    adgroup_code        TEXT,
    updated_at          TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_yandex_local_campaign_id ON {T_YANDEX_LOCAL} ("CampaignId");
CREATE INDEX IF NOT EXISTS idx_yandex_local_date        ON {T_YANDEX_LOCAL} ("Date");
"""


# ── Запросы для потокового INSERT ─────────────────────────────────────────────

YANDEX_INSERT_QUERY = """
    SELECT
        id,
        "Date"::DATE,
        "CampaignId",
        "CampaignName",
        "AdGroupId",
        "AdGroupName",
        "AdNetworkType",
        "Device",
        "Cost",
        total_cost,
        "Impressions",
        "Clicks",
        "RlAdjustmentId",
        account_login,
        manager_login,
        "AdGroupName"  AS adgroup_code,
        updated_at
    FROM yandex_direct_manager_reports
    WHERE "Date" >= %s
"""

YANDEX_COLS = (
    'id, "Date", "CampaignId", "CampaignName", "AdGroupId", "AdGroupName", '
    '"AdNetworkType", "Device", "Cost", total_cost, "Impressions", "Clicks", '
    '"RlAdjustmentId", account_login, manager_login, adgroup_code, updated_at'
)


# ── Проверка: нужно ли копировать ─────────────────────────────────────────────

def _check_yandex_needs_sync(src_conn, dst_conn) -> bool:
    """
    Возвращает True если нужно копировать.
    Sync ЛЮБОЕ расхождение: count != или max_date !=.
    Раньше использовалось `>` — состояние dst > src (избыток/дубли) было слепым пятном
    и не лечилось автоматически (см. инцидент 14.04.2026 — задвоение local_yandex).
    Если dst пустой — копируем всегда.
    """
    with dst_conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*), MAX("Date") FROM {T_YANDEX_LOCAL}')
        dst_count, dst_max_date = cur.fetchone()

    if not dst_count:
        logger.info('%s пустой — копируем без проверки', T_YANDEX_LOCAL)
        return True

    with src_conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*), MAX("Date")
            FROM yandex_direct_manager_reports
            WHERE "Date" >= %s
        """, (DATE_FROM,))
        src_count, src_max_date = cur.fetchone()

    logger.info(
        '%s — src: count=%s max_date=%s | dst: count=%s max_date=%s',
        T_YANDEX_LOCAL, src_count, src_max_date, dst_count, dst_max_date,
    )

    counts_differ = (src_count or 0) != (dst_count or 0)
    dates_differ = (
        src_max_date is not None
        and dst_max_date is not None
        and str(src_max_date) != str(dst_max_date)
    )
    needs_sync = counts_differ or dates_differ

    if needs_sync:
        if (dst_count or 0) > (src_count or 0):
            logger.warning(
                '%s: dst (%s) > src (%s) — лечим избыточные строки полным resync',
                T_YANDEX_LOCAL, dst_count, src_count,
            )
        return True

    logger.info('%s актуален — пропускаем копирование', T_YANDEX_LOCAL)
    return False



# ── Защита TRUNCATE от молчаливого зависания на локе ───────────────────────────
# LOCK_TIMEOUT_GUARD_2026-07-10: инцидент — fast_pipeline завис на step0 ~75 мин.
# TRUNCATE local_leads_all (AccessExclusive) молча ждал лок, который держали ДВЕ
# осиротевшие наши read-сессии (ad-hoc анти-join, провисели 2.5ч). В лог ничего не
# писалось → выглядело как «прогон идёт», хотя он стоял. Защита = ОГРАНИЧЕННЫЙ
# lock_timeout + видимость блокировщиков + ретраи с backoff + fail loud (без автокилла
# чужих сессий — по правилу проекта их не гасим, решение об этом остаётся человеку).

# 45 с на попытку: короткие легитимные блокировки (мелкий SELECT/COUNT на local-таблице)
# переживаем спокойно; часами не висим. 4 попытки, backoff 5→10→20 с между ними →
# суммарный «потолок» до падения ≈ 4×45 + (5+10+20) ≈ 3.5 мин, а не бесконечность.
TRUNCATE_LOCK_TIMEOUT_MS = 45_000
TRUNCATE_LOCK_RETRIES = 4
TRUNCATE_LOCK_BACKOFF_SEC = (5, 10, 20)  # backoff перед попытками 2,3,4
_LOCK_NOT_AVAILABLE_SQLSTATE = '55P03'   # psycopg2: lock_not_available (lock_timeout сработал)


def _log_lock_blockers(dst_conn, local_table: str, waited_sec: float) -> None:
    """
    Логирует, КТО сейчас держит конфликтующие локи на целевой таблице.

    Вызывается ПОСЛЕ rollback (транзакция уже чистая) — поэтому смотрим не наш
    wait-стейт (он уже снят таймаутом), а granted-локи на самом relation через
    pg_locks ⨝ pg_stat_activity. Так видно «стоим из-за pid X (usename/client/query)»,
    а не тишина. Чужие сессии НЕ трогаем — только показываем.
    """
    try:
        with dst_conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.pid, a.usename, a.client_addr, a.state,
                       a.wait_event_type,
                       COALESCE(EXTRACT(EPOCH FROM (now() - a.query_start))::int, -1) AS query_age_sec,
                       COALESCE(EXTRACT(EPOCH FROM (now() - a.xact_start))::int, -1)  AS xact_age_sec,
                       string_agg(DISTINCT l.mode, ',')                              AS lock_modes,
                       left(regexp_replace(a.query, '\\s+', ' ', 'g'), 200)          AS query
                FROM pg_locks l
                JOIN pg_stat_activity a ON a.pid = l.pid
                WHERE l.relation = %s::regclass
                  AND l.granted
                  AND l.pid <> pg_backend_pid()
                GROUP BY a.pid, a.usename, a.client_addr, a.state,
                         a.wait_event_type, a.query_start, a.xact_start, a.query
                ORDER BY xact_age_sec DESC
                """,
                (local_table,),
            )
            blockers = cur.fetchall()
        dst_conn.commit()
    except Exception as diag_err:  # диагностика не должна ронять сам ретрай
        try:
            dst_conn.rollback()
        except Exception:
            pass
        logger.warning(
            'TRUNCATE %s: не удалось собрать список блокировщиков (%s)',
            local_table, diag_err,
        )
        return

    if not blockers:
        logger.warning(
            'TRUNCATE %s: лок не взят за %.0f с, но granted-блокировщиков на relation '
            'не видно (могли только что освободиться / держит autovacuum) — повторяем',
            local_table, waited_sec,
        )
        return

    logger.warning(
        'TRUNCATE %s: СТОИМ НА ЛОКЕ (ждали ~%.0f с) — %d блокирующих сессий на таблице:',
        local_table, waited_sec, len(blockers),
    )
    for pid, usename, client_addr, state, wait_ev, q_age, x_age, modes, query in blockers:
        logger.warning(
            '  blocker pid=%s usename=%s client=%s state=%s wait=%s '
            'query_age=%ss xact_age=%ss modes=[%s] query=%s',
            pid, usename, client_addr, state, wait_ev,
            q_age, x_age, modes, query,
        )


def _truncate_with_lock_guard(dst_conn, local_table: str) -> None:
    """
    TRUNCATE local_table с ОГРАНИЧЕННЫМ lock_timeout, логом блокировщиков, ретраями
    и понятной ошибкой вместо бесконечного молчаливого ожидания (fail loud).

    Контракт для вызывающего: при УСПЕХЕ возвращает с ОТКРЫТОЙ транзакцией, в которой
    уже взят AccessExclusive на local_table (TRUNCATE выполнен, но НЕ закоммичен) —
    т.е. дальше в той же транзакции идёт INSERT + один общий commit (атомарность
    TRUNCATE+INSERT сохраняется, как раньше). При неудаче — RuntimeError после ретраев.

    lock_timeout ставится через SET LOCAL (действует до конца транзакции; на сам INSERT
    не влияет — лок уже наш, а lock_timeout ограничивает только ОЖИДАНИЕ лока, не
    длительность запроса).
    """
    last_waited = 0.0
    for attempt in range(1, TRUNCATE_LOCK_RETRIES + 1):
        t0 = time.monotonic()
        try:
            with dst_conn.cursor() as cur:
                cur.execute('SET LOCAL lock_timeout = %s', (TRUNCATE_LOCK_TIMEOUT_MS,))
                cur.execute(f'TRUNCATE {local_table}')
            # УСПЕХ: транзакция открыта, лок наш — отдаём управление под INSERT.
            if attempt > 1:
                logger.info(
                    'TRUNCATE %s: лок взят с попытки %d/%d',
                    local_table, attempt, TRUNCATE_LOCK_RETRIES,
                )
            return
        except psycopg2.Error as exc:
            if getattr(exc, 'pgcode', None) != _LOCK_NOT_AVAILABLE_SQLSTATE:
                raise  # не лок-таймаут (напр. таблицы нет) — пробрасываем как есть
            last_waited = time.monotonic() - t0
            # Транзакция aborted после таймаута — откатываем ПЕРЕД диагностикой.
            dst_conn.rollback()
            _log_lock_blockers(dst_conn, local_table, last_waited)
            if attempt >= TRUNCATE_LOCK_RETRIES:
                raise RuntimeError(
                    f'TRUNCATE {local_table}: не удалось взять AccessExclusive-лок за '
                    f'{TRUNCATE_LOCK_RETRIES} попыток (lock_timeout={TRUNCATE_LOCK_TIMEOUT_MS} мс). '
                    f'Таблицу держат другие сессии (см. WARNING со списком блокировщиков выше). '
                    f'Разберитесь с блокировщиком вручную и перезапустите step0 — '
                    f'step0 чужие сессии НЕ гасит.'
                ) from exc
            backoff = TRUNCATE_LOCK_BACKOFF_SEC[
                min(attempt - 1, len(TRUNCATE_LOCK_BACKOFF_SEC) - 1)
            ]
            logger.warning(
                'TRUNCATE %s: попытка %d/%d не взяла лок за ~%.0f с — backoff %d с, повтор',
                local_table, attempt, TRUNCATE_LOCK_RETRIES, last_waited, backoff,
            )
            time.sleep(backoff)


# ── Потоковый TRUNCATE+INSERT ──────────────────────────────────────────────────

def _stream_insert(src_conn, dst_conn, src_query: str, src_params: tuple,
                   local_table: str, col_list: str) -> int:
    """
    TRUNCATE local_table + потоковый INSERT в ОДНОЙ транзакции.

    Атомарность: либо весь TRUNCATE+INSERT успешно коммитится, либо ROLLBACK
    откатывает обратно (TRUNCATE и все INSERT'ы транзакционно безопасны).
    Раньше каждый batch коммитился отдельно — при падении посредине таблица
    оставалась частично заполненной, и retry мог задублировать данные
    (см. инцидент 14.04.2026 — задвоение local_yandex).
    """
    total = 0
    cursor_name = f'stream_{local_table}_{int(time.time())}'
    try:
        # LOCK_TIMEOUT_GUARD_2026-07-10: TRUNCATE под защитой lock_timeout+ретраи+лог блокировщиков.
        _truncate_with_lock_guard(dst_conn, local_table)
        # НЕ коммитим — TRUNCATE остаётся в транзакции до финального COMMIT.

        with src_conn.cursor(name=cursor_name) as src_cur:
            src_cur.itersize = STREAM_CHUNK
            src_cur.execute(src_query, src_params)
            batch: list = []
            for row in src_cur:
                batch.append(row)
                if len(batch) >= STREAM_CHUNK:
                    with dst_conn.cursor() as cur:
                        execute_values(
                            cur,
                            f'INSERT INTO {local_table} ({col_list}) VALUES %s',
                            batch,
                            page_size=5_000,
                        )
                    total += len(batch)
                    batch = []
            if batch:
                with dst_conn.cursor() as cur:
                    execute_values(
                        cur,
                        f'INSERT INTO {local_table} ({col_list}) VALUES %s',
                        batch,
                        page_size=5_000,
                    )
                total += len(batch)

        dst_conn.commit()
    except Exception:
        try:
            dst_conn.rollback()
        except Exception:
            pass
        raise

    with dst_conn.cursor() as cur:
        cur.execute(f'ANALYZE {local_table}')
    dst_conn.commit()
    logger.info('%s: вставлено %d строк', local_table, total)
    return total


def _sync_yandex_full(src_conn, dst_conn) -> int:
    if not _check_yandex_needs_sync(src_conn, dst_conn):
        return 0
    logger.info('%s: полная замена WHERE "Date" >= %s …', T_YANDEX_LOCAL, DATE_FROM)
    rows = _stream_insert(
        src_conn, dst_conn,
        src_query=YANDEX_INSERT_QUERY,
        src_params=(DATE_FROM,),
        local_table=T_YANDEX_LOCAL,
        col_list=YANDEX_COLS,
    )

    # Post-sync verify: dst_count должен совпасть с src_count
    with src_conn.cursor() as cur:
        cur.execute(
            'SELECT COUNT(*) FROM yandex_direct_manager_reports WHERE "Date" >= %s',
            (DATE_FROM,),
        )
        src_count = cur.fetchone()[0]
    with dst_conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {T_YANDEX_LOCAL}')
        dst_count = cur.fetchone()[0]

    if src_count != dst_count:
        logger.warning(
            '%s: post-sync MISMATCH src=%s dst=%s diff=%s — проверь логи!',
            T_YANDEX_LOCAL, src_count, dst_count, dst_count - src_count,
        )
    else:
        logger.info('%s: post-sync OK (%s строк)', T_YANDEX_LOCAL, dst_count)
    return rows



# ── Патч: дополнительные маппинги статусов ────────────────────────────────────

# Новая воронка (май 2026):
#   status-сторона: kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr
#   reason-сторона: dohod_do_kredita, dobro
# auto-merge: sale→visit→qualified→correct (status), sale→approved→credit (reason)
#
# Удаляются маппинги, которые теперь "мёртвые" (нет в leads.status) либо неправильные.
# Формат: (crm_status, crm_name, salon, kind)
# ЗАКОММЕНТИРОВАНО 2026-05-20: crm_statuses всегда верный, патч не нужен
# _DELETE_CRM_STATUSES = [
#     # Консультация — переопределена ниже как visit (status+reason)
#     ('Консультация', 'default', '', 'status'),
#     ('Консультация', 'default', '', 'reason'),
#     # Соскок — мусор, не используется
#     ('Соскок', 'default', '', 'status'),
#     ('Соскок', 'default', '', 'reason'),
#     # 4 полностью мёртвых статуса (нет ни в leads.status, ни в leads.reason)
#     ('Дошел в КО',    '', '', 'status'),
#     ('Нет данных',    '', '', 'status'),
#     ('Одобрение',     '', '', 'status'),
#     ('Одобрено банк', '', '', 'status'),
#     # 14 status-маппингов мигрировали в leads.reason — удаляем status-копии
#     # (добавим как kind='reason' в _EXTRA_CRM_STATUSES если нужно)
#     ('Болт (приехал не заемщик)', '', '', 'status'),
#     ('Долги ФССП',                '', '', 'status'),
#     ('Забился',                   '', '', 'status'),
#     ('Игнор',                     '', '', 'status'),
#     ('НЕ БЫЛ В КО',               '', '', 'status'),
#     ('Нал по цене кредита',       '', '', 'status'),
#     ('Наличка-отказ',             '', '', 'status'),
#     ('Не был в КСО',              '', '', 'status'),
#     ('Не забился',                '', '', 'status'),
#     ('Регион',                    '', '', 'status'),
#     ('Урез',                      '', '', 'status'),
#     ('лизинг/юр.лицо',            '', '', 'status'),
#     ('нп ДОК',                    '', '', 'status'),
#     ('приедет еще',               '', '', 'status'),
#     # Старые status-маппинги для credit/approved — теперь они только reason
#     # (DELETE удаляет ВСЕ строки с (crm_status, crm_name, '', 'status') —
#     # потом INSERT восстановит нужные status-категории через _EXTRA)
#     # ВНИМАНИЕ: эти строки существуют в БД как и status, и reason — DELETE по kind='status'
#     # затронет только status-копии. Reason-копии останутся.
#     # Купил (reason) → incorrect — баг, удаляем целиком reason для Купил, восстановим как credit/approved
#     ('Купил', 'default', '', 'reason'),
# ]

# Маппинги для добавления (если не существуют). Новая воронка:
#   - status-категории (kind='status'): correct/visit/sale/incorrect/qualified
#   - reason-категории (kind='reason'): credit/approved
# Auto-merge добавляет недостающие переходы (sale→visit→qualified→correct и т.д.)
# Формат: (crm_status, lead_status, crm_name, salon, kind)
# ЗАКОММЕНТИРОВАНО 2026-05-20: crm_statuses всегда верный, патч не нужен
# _EXTRA_CRM_STATUSES = [
#     # ── STATUS-сторона (основная воронка) ─────────────────────────────────────
#     # Marcar Продажа = correct/visit/sale (kind='status')
#     ('Продажа', 'correct', '', '', 'status'),
#     ('Продажа', 'visit',   '', '', 'status'),
#     ('Продажа', 'sale',    '', '', 'status'),
# 
#     # correct — Недозвон/Отказ из status (Регион в leads.status НЕТ → не добавляем)
#     ('Недозвон',  'correct', '', '', 'status'),
#     ('Отказ',     'correct', '', '', 'status'),
# 
#     # incorrect — Некорректные данные / Спам (status)
#     ('Некорректные данные', 'incorrect', '', '', 'status'),
#     ('Спам',                'incorrect', '', '', 'status'),
# 
#     # Override: Консультация → visit (kind=status и kind=reason)
#     ('Консультация', 'visit', 'default', '', 'status'),
#     ('Консультация', 'visit', 'default', '', 'reason'),
# 
#     # ── REASON-сторона (доход / добро) ────────────────────────────────────────
#     # Все продажи входят в credit и approved (через auto-merge sale→approved→credit
#     # это будет применено для default-маппингов sale-reason, но safety: явно)
#     ('Купил',               'credit',   'default', '', 'reason'),
#     ('Оформленные',         'credit',   'default', '', 'reason'),
#     ('Продажа в кредит',    'credit',   'default', '', 'reason'),
#     ('Продажа за наличные', 'credit',   'default', '', 'reason'),
# 
#     ('Купил',               'approved', 'default', '', 'reason'),
#     ('Оформленные',         'approved', 'default', '', 'reason'),
#     ('Продажа в кредит',    'approved', 'default', '', 'reason'),
#     ('Продажа за наличные', 'approved', 'default', '', 'reason'),
# 
#     # 14 status→reason миграция: добавляем kind='reason' (если есть в reason)
#     # Категории берутся из старых маппингов (visit и т.д.)
#     ('Болт (приехал не заемщик)', 'visit', '', '', 'reason'),
#     ('Долги ФССП',                'visit', '', '', 'reason'),
#     ('Забился',                   'visit', '', '', 'reason'),
#     ('Игнор',                     'visit', '', '', 'reason'),
#     ('НЕ БЫЛ В КО',               'visit', '', '', 'reason'),
#     ('Нал по цене кредита',       'visit', '', '', 'reason'),
#     ('Наличка-отказ',             'visit', '', '', 'reason'),
#     ('Не был в КСО',              'visit', '', '', 'reason'),
#     ('Не забился',                'visit', '', '', 'reason'),
#     ('Регион',                    'correct', '', '', 'reason'),
#     ('Урез',                      'approved', 'default', '', 'reason'),
#     ('лизинг/юр.лицо',            'visit', '', '', 'reason'),
#     ('нп ДОК',                    'visit', '', '', 'reason'),
#     ('приедет еще',               'visit', '', '', 'reason'),
# ]


# ── Ручные замены UTM-полей в local_telega_in_orders ──────────────────────────

# Источник: telega_in_orders_replacements.json (красные ячейки Google Sheets «посевы»).
# Ключ = id (PK). Для каждого id обновляются только перечисленные в записи поля.
_TELEGA_REPLACEMENTS_FILE = Path(__file__).resolve().parent / 'telega_in_orders_replacements.json'
_TELEGA_REPLACEABLE_FIELDS = (
    'post_links', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
)


def _apply_telega_replacements(dst_conn) -> int:
    """Применяет ручные замены UTM-полей к local_telega_in_orders из JSON-файла.

    Вызывается внутри _sync_telega_in_orders ПОСЛЕ TRUNCATE+INSERT, в той же
    транзакции — чтобы замены и синк были атомарны. Для каждого id выполняется
    UPDATE только тех полей, что присутствуют в записи (whitelist полей).
    Возвращает число фактически изменённых строк. Не коммитит — коммит делает вызывающий.
    """
    if not _TELEGA_REPLACEMENTS_FILE.exists():
        logger.warning('Файл замен %s не найден — замены пропущены', _TELEGA_REPLACEMENTS_FILE.name)
        return 0
    try:
        doc = json.loads(_TELEGA_REPLACEMENTS_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        logger.error('Не прочитать %s: %s — замены пропущены', _TELEGA_REPLACEMENTS_FILE.name, e)
        return 0

    reps = doc.get('replacements', [])
    updated = 0
    with dst_conn.cursor() as cur:
        for rec in reps:
            rid = rec.get('id')
            if not isinstance(rid, int):
                continue
            sets = {f: rec[f] for f in _TELEGA_REPLACEABLE_FIELDS if f in rec}
            if not sets:
                continue
            cols = ', '.join(f'{f} = %s' for f in sets)
            cur.execute(
                f'UPDATE public.local_telega_in_orders SET {cols} WHERE id = %s',
                list(sets.values()) + [rid],
            )
            updated += cur.rowcount
    logger.info('local_telega_in_orders: применено %d замен из %s (записей в файле: %d)',
                updated, _TELEGA_REPLACEMENTS_FILE.name, len(reps))
    return updated


# Поля UTM, которые можно вытащить из post_links регуляркой.
_TELEGA_UTM_FROM_LINK = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')


def _fill_telega_utm_from_post_links(dst_conn) -> int:
    """Дозаполняет ПУСТЫЕ utm_* колонки значениями из post_links.

    Чинит битые ссылки, где перед UTM нет '?' (например '/utm_source=' вместо
    '?utm_source='): регулярка ищет 'utm_X=...' где угодно в строке, не полагаясь
    на разбор URL. Значение берётся до первого из &, ?, " (так erid после '?'
    и закрывающая скобка JSON-массива не попадают в значение).

    Трогает только поля, которые ещё пусты (NULL или '') — ручные замены и
    значения из FDW в приоритете. Вызывается ПОСЛЕ _apply_telega_replacements,
    до коммита. Возвращает суммарное число обновлённых ячеек.
    """
    updated = 0
    with dst_conn.cursor() as cur:
        for field in _TELEGA_UTM_FROM_LINK:
            # substring(... from 'utm_field=([^&?"]+)') — POSIX-регулярка PostgreSQL.
            cur.execute(f"""
                UPDATE public.local_telega_in_orders
                SET {field} = NULLIF(TRIM(substring(post_links FROM %s)), '')
                WHERE ({field} IS NULL OR {field} = '')
                  AND post_links LIKE %s
                  AND NULLIF(TRIM(COALESCE(substring(post_links FROM %s), '')), '') IS NOT NULL
            """, (f'{field}=([^&?"]+)', f'%{field}=%', f'{field}=([^&?"]+)'))
            n = cur.rowcount
            updated += n
            if n:
                logger.info('local_telega_in_orders: %s дозаполнено из post_links — %d строк', field, n)
    return updated


# ── Синхронизация telega_in_orders (FDW в той же ad_analytics_bi) ─────────────

def _sync_telega_in_orders(dst_conn) -> int:
    """TRUNCATE+INSERT из FDW public.telega_in_orders -> public.local_telega_in_orders.

    FDW telega_in_orders живёт в той же БД (ad_analytics_bi), поэтому
    dst_conn используется и для чтения FDW, и для записи в local_*.
    Защита: если FDW вернул 0 строк — TRUNCATE не делается, старые данные сохраняются.
    """
    LOCAL = 'public.local_telega_in_orders'
    FDW   = 'public.telega_in_orders'

    with dst_conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.local_telega_in_orders (
                id                  BIGINT PRIMARY KEY,
                uid                 TEXT,
                order_id            BIGINT,
                order_project_name  TEXT,
                order_comment       TEXT,
                channel_id          BIGINT,
                channel_name        TEXT,
                channel_link        TEXT,
                post_link           TEXT,
                placement_format    TEXT,
                status              TEXT,
                cancel_comment      TEXT,
                price               NUMERIC,
                total_price         NUMERIC,
                total_views         BIGINT,
                clicks              BIGINT,
                post_links          TEXT,
                utm_source          TEXT,
                utm_medium          TEXT,
                utm_campaign        TEXT,
                utm_content         TEXT,
                utm_term            TEXT,
                created_at          TIMESTAMP,
                completed_at        TIMESTAMP,
                done_at             TIMESTAMP,
                run_at              TIMESTAMP,
                raw                 JSONB,
                updated_at          TIMESTAMP
            )
        """)
        dst_conn.commit()

        cur.execute('SELECT COUNT(*) FROM ' + FDW)
        src_count = cur.fetchone()[0]

    if src_count == 0:
        with dst_conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM ' + LOCAL)
            existing = cur.fetchone()[0]
        logger.warning('%s: FDW вернул 0 строк — пропускаем, оставляем %d старых', LOCAL, existing)
        return existing

    try:
        # LOCK_TIMEOUT_GUARD_2026-07-10: TRUNCATE под защитой lock_timeout+ретраи+лог блокировщиков.
        _truncate_with_lock_guard(dst_conn, LOCAL)
        with dst_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.local_telega_in_orders
                SELECT
                    id, uid, order_id, order_project_name, order_comment,
                    channel_id, channel_name, channel_link, post_link,
                    placement_format, status, cancel_comment,
                    price, total_price, total_views, clicks,
                    post_links::TEXT, utm_source, utm_medium, utm_campaign,
                    utm_content, utm_term,
                    created_at, completed_at, done_at, run_at,
                    raw, updated_at
                FROM public.telega_in_orders
                ON CONFLICT (id) DO UPDATE SET
                    uid                = EXCLUDED.uid,
                    order_id           = EXCLUDED.order_id,
                    order_project_name = EXCLUDED.order_project_name,
                    order_comment      = EXCLUDED.order_comment,
                    channel_id         = EXCLUDED.channel_id,
                    channel_name       = EXCLUDED.channel_name,
                    channel_link       = EXCLUDED.channel_link,
                    post_link          = EXCLUDED.post_link,
                    placement_format   = EXCLUDED.placement_format,
                    status             = EXCLUDED.status,
                    cancel_comment     = EXCLUDED.cancel_comment,
                    price              = EXCLUDED.price,
                    total_price        = EXCLUDED.total_price,
                    total_views        = EXCLUDED.total_views,
                    clicks             = EXCLUDED.clicks,
                    post_links         = EXCLUDED.post_links,
                    utm_source         = EXCLUDED.utm_source,
                    utm_medium         = EXCLUDED.utm_medium,
                    utm_campaign       = EXCLUDED.utm_campaign,
                    utm_content        = EXCLUDED.utm_content,
                    utm_term           = EXCLUDED.utm_term,
                    created_at         = EXCLUDED.created_at,
                    completed_at       = EXCLUDED.completed_at,
                    done_at            = EXCLUDED.done_at,
                    run_at             = EXCLUDED.run_at,
                    raw                = EXCLUDED.raw,
                    updated_at         = EXCLUDED.updated_at
            """)
        # Ручные замены UTM-полей (красные ячейки Google Sheets «посевы») —
        # до коммита, атомарно с синком.
        _apply_telega_replacements(dst_conn)
        # Дозаполнение пустых utm_* из post_links (чинит битый '?' в ссылке) —
        # после ручных замен, трогает только оставшиеся пустыми поля.
        _fill_telega_utm_from_post_links(dst_conn)
        with dst_conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM ' + LOCAL)
            count = cur.fetchone()[0]
        dst_conn.commit()
    except Exception:
        dst_conn.rollback()
        raise

    logger.info('%s: %d строк синхронизировано из FDW', LOCAL, count)
    return count



# ── Синхронизация vk_ads_stats_day (FDW в той же ad_analytics_bi) ─────────────

def _sync_vk_ads_stats(dst_conn) -> int:
    """TRUNCATE+INSERT из FDW public.vk_ads_stats_day -> public.local_vk_ads_stats_day.

    VK_ADS_BANNER_GRAIN_2026-07-10: тянем на уровне BANNER×DATE (без схлопывания
    группы/баннера) — детальная грань нужна датамарту fact_vk_ads (сегмент×оффер×объявление).
    Раньше (VK_ADS_INTEGRATION_2026-07-06) схлопывали до (date, account_id, ad_plan_id).
    Теперь несём полный разрез: account/ad_plan(оффер)/ad_group(сегмент)/banner(объявление)
    + метрики shows/clicks/spent/ctr/cpm. Грань FDW = (banner_id, date) — уникальна для
    Авто (проверено: 388=388), поэтому GROUP BY НЕ нужен, строки проходят как есть.

    СКОУП: только платный VK Ads Авто-аккаунтов (account_id ∈ vk_client_id ниши «Авто»
    в local_gsheet_sites). Посевы VK не участвуют (у них нет записи в vk_ads_stats_day).
    Фильтры: spent > 0 (исключаем нулевые строки FDW) и date >= '2026-01-01'.

    Совместимость step3 (_add_vk_ads_to_crop_sql, ветка vk_ads): step3 ре-агрегирует
    эту таблицу до (date, account_id, ad_plan_id) через CTE vk_ads_by_plan (SUM(spent)),
    поэтому расход VK Ads Комплекса (~531k) и уникальность его key3 не меняются.

    FDW живёт в той же БД (ad_analytics_bi), поэтому dst_conn используется и для
    чтения FDW, и для записи в local_*.
    Защита: если FDW вернул 0 Авто-строк с spent>0 — TRUNCATE не делается, старые данные
    сохраняются.
    """
    LOCAL = f'public.{T_LOCAL_VK_ADS}'
    FDW   = 'public.vk_ads_stats_day'

    # Авто-аккаунты VK: vk_client_id (TEXT) ниши «Авто», приведённый к bigint.
    AUTO_ACC = (
        "SELECT NULLIF(TRIM(vk_client_id), '')::bigint "
        "FROM public.local_gsheet_sites "
        "WHERE niche = 'Авто' AND vk_client_id ~ '^[0-9]+$'"
    )

    with dst_conn.cursor() as cur:
        # Свежая БД: полная banner-grain схема. account_name/ad_plan_id держим для
        # обратной совместимости step3; NOT NULL только на несомненно заполненных полях.
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {LOCAL} (
                date          DATE    NOT NULL,
                account_id    BIGINT  NOT NULL,
                account_name  TEXT,
                ad_plan_id    BIGINT,
                ad_plan_name  TEXT,
                ad_group_id   BIGINT,
                ad_group_name TEXT,
                banner_id     BIGINT,
                banner_name   TEXT,
                shows         BIGINT,
                clicks        BIGINT,
                spent         NUMERIC NOT NULL,
                ctr           NUMERIC,
                cpm           NUMERIC
            )
        """)
        # VK_ADS_BANNER_GRAIN_2026-07-10: миграция существующей (плановой) таблицы —
        # добавка ТОЛЬКО новых колонок (старые date/account_id/ad_plan_id/spent сохранены).
        for _col, _typ in (
            ('ad_group_id', 'BIGINT'), ('ad_group_name', 'TEXT'),
            ('banner_id', 'BIGINT'),   ('banner_name', 'TEXT'),
            ('shows', 'BIGINT'),       ('clicks', 'BIGINT'),
            ('ctr', 'NUMERIC'),        ('cpm', 'NUMERIC'),
        ):
            cur.execute(f'ALTER TABLE {LOCAL} ADD COLUMN IF NOT EXISTS {_col} {_typ}')
        dst_conn.commit()

        # Живость FDW: считаем ровно те строки, что будем вставлять (Авто + spent>0 + дата).
        cur.execute(
            f"SELECT COUNT(*) FROM {FDW} vk "
            f"WHERE vk.spent > 0 AND vk.date::date >= '2026-01-01' "
            f"AND vk.account_id::bigint IN ({AUTO_ACC})"
        )
        src_count = cur.fetchone()[0]

    if src_count == 0:
        with dst_conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {LOCAL}')
            existing = cur.fetchone()[0]
        logger.warning(
            '%s: FDW вернул 0 Авто-строк с spent>0 — пропускаем, оставляем %d старых',
            LOCAL, existing,
        )
        return existing

    try:
        # LOCK_TIMEOUT_GUARD_2026-07-10: TRUNCATE под защитой lock_timeout+ретраи+лог блокировщиков.
        _truncate_with_lock_guard(dst_conn, LOCAL)
        with dst_conn.cursor() as cur:
            # Явный список колонок: после ALTER ADD порядок колонок в мигрированной
            # таблице ≠ порядок в свежем CREATE → полагаться на позицию нельзя.
            cur.execute(f"""
                INSERT INTO {LOCAL}
                    (date, account_id, account_name, ad_plan_id, ad_plan_name,
                     ad_group_id, ad_group_name, banner_id, banner_name,
                     shows, clicks, spent, ctr, cpm)
                SELECT
                    vk.date::date            AS date,
                    vk.account_id::bigint    AS account_id,
                    vk.account_name          AS account_name,
                    vk.ad_plan_id::bigint    AS ad_plan_id,
                    vk.ad_plan_name          AS ad_plan_name,
                    vk.ad_group_id::bigint   AS ad_group_id,
                    vk.ad_group_name         AS ad_group_name,
                    vk.banner_id::bigint     AS banner_id,
                    vk.banner_name           AS banner_name,
                    vk.shows::bigint         AS shows,
                    vk.clicks::bigint        AS clicks,
                    vk.spent::numeric        AS spent,
                    vk.ctr::numeric          AS ctr,
                    vk.cpm::numeric          AS cpm
                FROM {FDW} vk
                WHERE vk.spent > 0
                  AND vk.date::date >= '2026-01-01'
                  AND vk.account_id::bigint IN ({AUTO_ACC})
            """)
            cur.execute(f'SELECT COUNT(*) FROM {LOCAL}')
            count = cur.fetchone()[0]
        dst_conn.commit()
    except Exception:
        dst_conn.rollback()
        raise

    logger.info(
        '%s: %d строк синхронизировано из FDW (banner×date, только Авто-аккаунты)',
        LOCAL, count,
    )
    return count


# ЗАКОММЕНТИРОВАНО 2026-05-20: crm_statuses всегда верный, патч не нужен
# def _patch_crm_statuses(dst_conn) -> None:
#     """Удаляет переопределяемые и добавляет недостающие маппинги в local_crm_statuses."""
#     with dst_conn.cursor() as cur:
#         for crm_status, crm_name, salon, kind in _DELETE_CRM_STATUSES:
#             cur.execute("""
#                 DELETE FROM public.local_crm_statuses
#                 WHERE crm_status = %s
#                   AND COALESCE(crm_name, '') = %s
#                   AND COALESCE(salon, '')    = %s
#                   AND COALESCE(kind, 'status') = %s
#             """, (crm_status, crm_name, salon, kind))
#         for crm_status, lead_status, crm_name, salon, kind in _EXTRA_CRM_STATUSES:
#             cur.execute("""
#                 INSERT INTO public.local_crm_statuses (crm_status, lead_status, crm_name, salon, kind)
#                 SELECT %s, %s, %s, %s, %s
#                 WHERE NOT EXISTS (
#                     SELECT 1 FROM public.local_crm_statuses
#                     WHERE crm_status = %s AND lead_status = %s AND crm_name = %s AND kind = %s
#                 )
#             """, (crm_status, lead_status, crm_name, salon, kind,
#                   crm_status, lead_status, crm_name, kind))
#     dst_conn.commit()
#     logger.info('[step0] _patch_crm_statuses: %d маппингов проверено', len(_EXTRA_CRM_STATUSES))
# 
# 
# # ── Полная замена (TRUNCATE + INSERT) для остальных таблиц ────────────────────
# 
# # Когда схема источника изменилась, но локальная схема должна оставаться стабильной.
# # Формат: src_table -> {local_col: src_col}
_COLUMN_REMAPS: dict[str, dict[str, str]] = {
    'crm_statuses': {
        'crm_status':  'value',
        'lead_status': 'category',
        'crm_name':    'crm',
        'salon':       'salon',
        'kind':        'kind',
    },
}


def _date_window(date_col) -> tuple[str, tuple]:
    """
    Возвращает (where_sql, params) для оконного фильтра синка по дате.

    Обычный случай: `WHERE "<date_col>" >= DATE_FROM`.

    Спец-случай leads_all (date_col='created_date'): окно расширено так, чтобы
    попадали ещё и ЗАЯВКИ 2025 года С ВИЗИТОМ в 2026+:
        WHERE (created_date >= DATE_FROM OR arrival_date >= DATE_FROM)

    Зачем: иначе заявки 2025-го (created_date < 2026-01-01) с реальным визитом
    в 2026 (arrival_date >= 2026-01-01) не попадают в local_leads_all → raw_leads
    → теряются в big_analytics_full_arrival (партиция «по дате визита»). На данных
    источника таких ~1727 (159 доменов, визиты янв–май 2026). На партицию «по дате
    заявки» (big_analytics_full) это НЕ влияет: там Date=created_date<2026 и эти
    строки штатно удаляются cleanup_old_dates (DELETE WHERE "Date" < DATE_FROM).
    Один и тот же предикат используется и в count-проверке идемпотентности, и в
    SELECT — чтобы count(src)==count(dst) сравнивался корректно.
    """
    if not date_col:
        return '', ()
    if date_col == 'created_date':
        return (' WHERE ("created_date" >= %s OR "arrival_date" >= %s)',
                (DATE_FROM, DATE_FROM))
    return (f' WHERE "{date_col}" >= %s', (DATE_FROM,))


def _sync_truncate_insert(src_conn, dst_conn, src_table: str, local_table: str,
                          date_col=None) -> int:
    """
    Полная замена локальной копии из источника.
    Динамически создаёт таблицу по схеме источника.
    BEGIN → TRUNCATE → INSERT → COMMIT.
    """
    logger.info('%s: полная замена из %s …', local_table, src_table)

    src_count = src_max_date = None
    if date_col:
        _win_sql, _win_params = _date_window(date_col)
        with src_conn.cursor() as cur:
            cur.execute(
                f'SELECT COUNT(*), MAX({date_col}) FROM {src_table}{_win_sql}',
                _win_params,
            )
            src_count, src_max_date = cur.fetchone()
        logger.info(
            '%s — src (>= %s): count=%s max_date=%s',
            local_table, DATE_FROM, src_count, src_max_date,
        )

        # Сравнение с dst: если count и max_date совпадают — пропускаем копирование.
        # to_regclass=None если таблицы ещё нет (первый запуск) → копируем без проверки.
        with dst_conn.cursor() as cur:
            cur.execute('SELECT to_regclass(%s)', (local_table,))
            dst_exists = cur.fetchone()[0] is not None

        if dst_exists:
            with dst_conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*), MAX("{date_col}") FROM {local_table}')
                dst_count, dst_max_date = cur.fetchone()
            logger.info(
                '%s — dst: count=%s max_date=%s',
                local_table, dst_count, dst_max_date,
            )
            counts_match = (src_count or 0) == (dst_count or 0)
            dates_match = (
                src_max_date is not None
                and dst_max_date is not None
                and str(src_max_date) == str(dst_max_date)
            )
            if dst_count and counts_match and dates_match:
                logger.info(
                    '%s актуален (count=%s max_date=%s) — пропускаем копирование',
                    local_table, dst_count, dst_max_date,
                )
                return dst_count
            if (dst_count or 0) > (src_count or 0):
                logger.warning(
                    '%s: dst (%s) > src (%s) — лечим избыточные строки полным resync',
                    local_table, dst_count, src_count,
                )

    remap = _COLUMN_REMAPS.get(src_table)

    if remap:
        # Стабильная локальная схема — не зависит от колонок источника
        col_names = list(remap.keys())
        select_exprs = ', '.join(
            f'"{src_col}" AS "{loc_col}"' for loc_col, src_col in remap.items()
        )
        col_defs = ', '.join(f'"{c}" TEXT' for c in col_names)
        create_sql = f'CREATE TABLE IF NOT EXISTS {local_table} ({col_defs})'
        with dst_conn.cursor() as cur:
            cur.execute(create_sql)
            # Добавить новые колонки если таблица уже существовала со старой схемой
            for col in col_names:
                cur.execute(
                    f"ALTER TABLE {local_table} ADD COLUMN IF NOT EXISTS \"{col}\" TEXT"
                )
        dst_conn.commit()

        where_sql, params = _date_window(date_col)

        with src_conn.cursor(name=f'sync_ti_{src_table}') as cur:
            cur.itersize = 10_000
            cur.execute(f'SELECT {select_exprs} FROM {src_table}{where_sql}', params)
            all_rows = cur.fetchall()
    else:
        with src_conn.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
            """, (src_table,))
            cols_info = cur.fetchall()

        if not cols_info:
            logger.warning('  %s: таблица не найдена в источнике — пропускаем', src_table)
            return 0

        col_names = [c[0] for c in cols_info]

        col_defs_list = []
        for name, dtype in cols_info:
            if dtype in ('integer', 'bigint', 'smallint'):
                pg_type = 'BIGINT'
            elif dtype in ('numeric', 'real', 'double precision'):
                pg_type = 'NUMERIC'
            elif dtype in ('boolean',):
                pg_type = 'BOOLEAN'
            elif dtype in ('date',):
                pg_type = 'DATE'
            elif 'timestamp' in dtype:
                pg_type = 'TIMESTAMP'
            else:
                pg_type = 'TEXT'
            col_defs_list.append(f'"{name}" {pg_type}')

        create_sql = f'CREATE TABLE IF NOT EXISTS {local_table} ({", ".join(col_defs_list)})'
        with dst_conn.cursor() as cur:
            cur.execute(create_sql)
        dst_conn.commit()

        # Добавить отсутствующие колонки если таблица уже существует
        with dst_conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
            """, (local_table.split('.')[-1],))
            dst_col_names = {row[0] for row in cur.fetchall()}

        for name, dtype in cols_info:
            if name not in dst_col_names:
                if dtype in ('integer', 'bigint', 'smallint'):
                    pg_type = 'BIGINT'
                elif dtype in ('numeric', 'real', 'double precision'):
                    pg_type = 'NUMERIC'
                elif dtype in ('boolean',):
                    pg_type = 'BOOLEAN'
                elif dtype in ('date',):
                    pg_type = 'DATE'
                elif 'timestamp' in dtype:
                    pg_type = 'TIMESTAMP'
                else:
                    pg_type = 'TEXT'
                with dst_conn.cursor() as cur:
                    cur.execute(f'ALTER TABLE {local_table} ADD COLUMN "{name}" {pg_type}')
                logger.info('  %s добавлена колонка "%s"', local_table, name)
        dst_conn.commit()

        quoted_cols = ', '.join(f'"{c}"' for c in col_names)
        where_sql, params = _date_window(date_col)

        with src_conn.cursor(name=f'sync_ti_{src_table}') as cur:
            cur.itersize = 10_000
            cur.execute(f'SELECT {quoted_cols} FROM {src_table}{where_sql}', params)
            all_rows = cur.fetchall()

    # Защита от обнуления: если src вернул 0 строк — не TRUNCATE, оставить старые данные
    if not all_rows:
        with dst_conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {local_table}')
            existing = cur.fetchone()[0]
        logger.warning(
            '  %s: src=%s вернул 0 строк — пропускаем TRUNCATE, оставляем %d старых строк',
            local_table, src_table, existing
        )
        return existing

    quoted_local = ', '.join(f'"{c}"' for c in col_names)
    try:
        # LOCK_TIMEOUT_GUARD_2026-07-10: TRUNCATE local_leads_all и др. под защитой
        # (инцидент 75-мин зависания). Возвращает с открытой транзакцией + взятым локом →
        # INSERT ниже идёт в той же транзакции (атомарность TRUNCATE+INSERT сохранена).
        _truncate_with_lock_guard(dst_conn, local_table)
        with dst_conn.cursor() as cur:
            execute_values(
                cur,
                f'INSERT INTO {local_table} ({quoted_local}) VALUES %s',
                all_rows,
                page_size=5000,
            )
            cur.execute(f'SELECT COUNT(*) FROM {local_table}')
            count = cur.fetchone()[0]
        dst_conn.commit()
        logger.info('  %s: %d строк', local_table, count)
        return count
    except Exception:
        dst_conn.rollback()
        raise


# ── Создание DDL и миграция ───────────────────────────────────────────────────

# Маппинг старых имён (суффикс _local) → новые (префикс local_)
_RENAME_MAP = [
    ('yandex_local',                    'local_yandex'),
    ('domains_local',                   'local_domains'),
    ('gsheet_sites_local',              'local_gsheet_sites'),
    ('gsheet_naming_local',             'local_gsheet_naming'),
    ('gsheet_plan_fakt_local',          'local_gsheet_plan_fakt'),
    ('gsheet_autosalony_clients_local', 'local_gsheet_autosalony_clients'),
    ('gsheet_priezdi_marcar_local',     'local_gsheet_priezdi_marcar'),
]


def _ensure_local_tables(dst_conn) -> None:
    """Создать local_yandex если не существует. Применить миграции."""
    with dst_conn.cursor() as cur:
        # Миграция Block A: переименовываем таблицы xxx_local → local_xxx
        for old, new in _RENAME_MAP:
            cur.execute(f'ALTER TABLE IF EXISTS {old} RENAME TO {new}')

        cur.execute(DDL_YANDEX_LOCAL)

        # Миграция: убираем PRIMARY KEY (id в источнике не уникален)
        cur.execute(
            f'ALTER TABLE {T_YANDEX_LOCAL} DROP CONSTRAINT IF EXISTS yandex_local_pkey'
        )
        cur.execute(
            f'ALTER TABLE {T_YANDEX_LOCAL} DROP CONSTRAINT IF EXISTS local_yandex_pkey'
        )
        # Удаляем старый индекс по updated_at
        cur.execute('DROP INDEX IF EXISTS idx_yandex_local_updated_at')
        # Миграция: добавляем total_cost из источника (отличается от "Cost")
        cur.execute(
            f'ALTER TABLE {T_YANDEX_LOCAL} ADD COLUMN IF NOT EXISTS total_cost NUMERIC'
        )
        # Миграция: direction из gsheet_sites
        cur.execute(
            'ALTER TABLE IF EXISTS local_gsheet_sites ADD COLUMN IF NOT EXISTS direction TEXT'
        )
    dst_conn.commit()
    logger.info('%s проверен/создан', T_YANDEX_LOCAL)


# ── Патч: статусы лидов Маркар из Google Sheets ──────────────────────────────

# Иерархия статусов Маркар (от высшего к низшему).
# Патч не перезаписывает статус "вниз" по воронке.
# Индекс = приоритет (0 = наивысший).
_MARCAR_STATUS_PRIORITY: dict[str, int] = {
    'Продажа':    0,
    'Дошел в КО': 1,
    'Одобрение':  2,
    'Приехал':    3,
}

# Маппинги: gsheet-статус → lead_status для local_crm_statuses.
# Добавляются если отсутствуют.
_MARCAR_EXTRA_STATUSES = [
    # crm_status,   lead_status, crm_name, salon, kind
    ('Дошел в КО', 'visit',     '',       '',    'status'),
    ('Одобрение',  'visit',     '',       '',    'status'),
    # 'Приехал' уже есть: ('Приехал', 'visit', 'default', '', 'status')
]


def _ensure_marcar_crm_statuses(dst_conn) -> None:
    """Добавляет недостающие маппинги Маркар-статусов в local_crm_statuses."""
    with dst_conn.cursor() as cur:
        for crm_status, lead_status, crm_name, salon, kind in _MARCAR_EXTRA_STATUSES:
            cur.execute("""
                INSERT INTO public.local_crm_statuses (crm_status, lead_status, crm_name, salon, kind)
                SELECT %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM public.local_crm_statuses
                    WHERE crm_status = %s AND lead_status = %s AND kind = %s
                )
            """, (crm_status, lead_status, crm_name, salon, kind,
                  crm_status, lead_status, kind))
    dst_conn.commit()
    logger.info('local_crm_statuses: проверены маппинги Маркар-приездов')


# PATCH-CRMF-LIDER-CRM-STATUSES-2026-06-15
# Маппинги crmf_excel + salon='Лидер' для воронки.
# Источник: SELECT status, COUNT(*) FROM local_leads_all
#            WHERE source_type='crmf_excel' AND salon='Лидер' GROUP BY status
# Конвенции те же, что у mauto+Лидер / general default:
#   visit  = физический приезд в салон
#   sale   = покупка
#   qualified = тёплый лид в работе
#   correct  = обращение корректное (без заявки на визит)
#   incorrect = не целевой / технический дубль
# Фиксированные (не нужны здесь): Недозвон (_NEDOZVON), Фильтр (_FILTR) — хардкод в status_sql.py
_CRMF_LIDER_EXTRA_STATUSES = [
    # crm_status,            lead_status,   crm_name, salon,    kind
    ('В салоне',             'visit',        'crmf',  'Лидер',  'status'),
    ('В салоне не отмечен',  'visit',        'crmf',  'Лидер',  'status'),
    ('Купил',                'sale',         'crmf',  'Лидер',  'status'),
    ('В работе',             'qualified',    'crmf',  'Лидер',  'status'),
    ('Новый',                'qualified',    'crmf',  'Лидер',  'status'),
    ('Отказ',                'correct',      'crmf',  'Лидер',  'status'),
    ('Некорректные данные',  'incorrect',    'crmf',  'Лидер',  'status'),
    ('Дубль',                'incorrect',    'crmf',  'Лидер',  'status'),
]


def _ensure_crmf_lider_crm_statuses(dst_conn) -> None:
    """Добавляет недостающие маппинги crmf+Лидер в local_crm_statuses (idempotent).

    Вызывается при каждом step0: после TRUNCATE+INSERT crm_statuses из FDW
    эти salon-specific строки были бы потеряны (FDW их не содержит).
    Паттерн: INSERT ... WHERE NOT EXISTS — безопасный re-run.
    """
    with dst_conn.cursor() as cur:
        for crm_status, lead_status, crm_name, salon, kind in _CRMF_LIDER_EXTRA_STATUSES:
            cur.execute("""
                INSERT INTO public.local_crm_statuses (crm_status, lead_status, crm_name, salon, kind)
                SELECT %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM public.local_crm_statuses
                    WHERE crm_status = %s
                      AND COALESCE(crm_name, '') = %s
                      AND COALESCE(salon, '')    = %s
                      AND COALESCE(kind, 'status') = %s
                )
            """, (crm_status, lead_status, crm_name, salon, kind,
                  crm_status, crm_name, salon, kind))
    dst_conn.commit()
    logger.info('local_crm_statuses: проверены маппинги crmf+Лидер (%d строк)', len(_CRMF_LIDER_EXTRA_STATUSES))


def _patch_marcar_statuses(dst_conn) -> dict:
    """Обновляет status в local_leads_all по данным из local_gsheet_priezdi_marcar.

    Патчит статусы: Продажа, Приехал, Дошел в КО, Одобрение.
    Приоритет: Продажа > Дошел в КО > Одобрение > Приехал.
    Статус в CRM не перезаписывается "вниз" по воронке — если уже стоит
    'Продажа', патч 'Приехал' из gsheet не применится.

    Извлекает ID из поля link (число после последнего /), матчит с
    local_leads_all.source_record_id где source_type='marcar_crm_excel'.
    """
    # Сначала убеждаемся что все нужные статусы есть в local_crm_statuses
    _ensure_marcar_crm_statuses(dst_conn)

    patched_statuses = list(_MARCAR_STATUS_PRIORITY.keys())

    # CASE-выражения для приоритетов:
    # — gsheet_priority_inner: используется внутри подзапроса, ссылается на колонку 'status'
    # — gsheet_priority_outer: используется в WHERE снаружи, ссылается на 'pm.status'
    # — crm_priority:          используется в WHERE, ссылается на 'l.status'
    gsheet_priority_inner = 'CASE status ' + ' '.join(
        f"WHEN '{s}' THEN {p}" for s, p in _MARCAR_STATUS_PRIORITY.items()
    ) + ' ELSE 9999 END'

    gsheet_priority_outer = 'CASE pm.status ' + ' '.join(
        f"WHEN '{s}' THEN {p}" for s, p in _MARCAR_STATUS_PRIORITY.items()
    ) + ' ELSE 9999 END'

    crm_priority = 'CASE l.status ' + ' '.join(
        f"WHEN '{s}' THEN {p}" for s, p in _MARCAR_STATUS_PRIORITY.items()
    ) + ' ELSE 9999 END'

    statuses_in = ', '.join(f"'{s}'" for s in patched_statuses)

    sql = f"""
        UPDATE public.local_leads_all l
        SET status = pm.status
        FROM (
            -- Для каждого лида берём gsheet-статус с наивысшим приоритетом
            SELECT DISTINCT ON (REGEXP_REPLACE(link, '^.+/', ''))
                REGEXP_REPLACE(link, '^.+/', '') AS lead_record_id,
                status
            FROM public.local_gsheet_priezdi_marcar
            WHERE link LIKE '%crm.marcar.ru%'
              AND link ~ '^https?://.+/[0-9]+$'
              AND status IN ({statuses_in})
            ORDER BY REGEXP_REPLACE(link, '^.+/', ''), {gsheet_priority_inner}
        ) pm
        WHERE l.source_record_id = pm.lead_record_id
          AND l.source_type = 'marcar_crm_excel'
          AND l.status IS DISTINCT FROM pm.status
          -- Не перезаписывать статус "вниз": gsheet-приоритет должен быть выше текущего CRM-приоритета
          AND {gsheet_priority_outer} < {crm_priority}
    """

    with dst_conn.cursor() as cur:
        cur.execute(sql)
        n = cur.rowcount
    dst_conn.commit()

    # Подсчёт по статусам для лога
    with dst_conn.cursor() as cur:
        cur.execute(f"""
            SELECT status, COUNT(*) FROM public.local_leads_all
            WHERE source_type = 'marcar_crm_excel'
              AND status IN ({statuses_in})
            GROUP BY status ORDER BY status
        """)
        breakdown = dict(cur.fetchall())

    logger.info(
        'local_leads_all: обновлено %d статусов из local_gsheet_priezdi_marcar | '
        'итого Маркар по статусам: %s',
        n, breakdown,
    )
    return {'patched': n, 'breakdown': breakdown}


def _insert_pixel_pr_sites(dst_conn) -> int:
    """Добавляет 7 доменов Перформ-пикселя (_pixel_pr) в local_gsheet_sites.

    PIXEL_PR_2026-07-09: utm_source вида 'victory_*_pixel_pr' — пиксель-ретаргетинг Перформа.
    Эти значения НЕТ в Google Sheet gsheet_sites, поэтому build_pixel.py (step5) отправляет
    такие лиды в pixel_leads_check вместо pixel_leads (домен не проходит _valid_domains фильтр).

    step0 делает TRUNCATE+INSERT local_gsheet_sites из FDW → прямые INSERT откатываются.
    Решение: вставлять ПОСЛЕ TRUNCATE_INSERT_TABLES, по аналогии с _patch_remove_elit_avto.

    Только поля domain и direction обязательны для фильтрации (salon — для атрибуции салона).
    """
    # PIXEL_PR_2026-07-09 (niche='Авто' добавлен 2026-07-10: без niche строки не
    # попадают в AUTO_DOMAIN_SET build_star.py → fact_big_analytics пуст для pixel_pr)
    PIXEL_PR_SITES = [
        'victory_mavto_pixel_pr',
        'victory_urbancar_pixel_pr',
        'victory_premier_pixel_pr',
        'victory_uralauto_pixel_pr',
        'victory_pixel_pr',
        'victory_vershina_pixel_pr',
        'victory_carcity_pixel_pr',
    ]
    n = 0
    with dst_conn.cursor() as cur:
        for domain in PIXEL_PR_SITES:
            # INSERT если строки нет
            cur.execute("""
                INSERT INTO public.local_gsheet_sites (domain, direction, salon, niche)
                SELECT %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM public.local_gsheet_sites WHERE domain = %s
                )
            """, (domain, 'Авто', 'Перформ РФ', 'Авто', domain))
            if cur.rowcount == 0:
                # Строка уже есть — убеждаемся что niche='Авто' (мог быть NULL от старой вставки)
                cur.execute("""
                    UPDATE public.local_gsheet_sites
                    SET niche = 'Авто'
                    WHERE domain = %s AND (niche IS NULL OR niche != 'Авто')
                """, (domain,))
            n += cur.rowcount
    dst_conn.commit()
    logger.info("local_gsheet_sites: upsert %d pixel_pr доменов Перформа c niche='Авто' (PIXEL_PR_2026-07-09)", n)
    return n


def _patch_remove_elit_avto(dst_conn) -> int:
    """Удаляет из local_gsheet_sites закрытый салон 'Элит Авто' (status='Удален').

    'Элит Авто' закрылся, его Директ-логины перенесены на 'АвтоСтайл'.
    В исходном Google Sheet (gsheet_sites) остались 30 СТАРЫХ доменов-клонов
    этого салона со status='Удален' (autobase-chlb.ru / cherycar-chlb.ru /
    ladaauto-chlb.ru / vash-avto-174.ru и т.д. — старое написание доменов).

    Проблема: 5 из этих доменов имеют login_key реального Direct-аккаунта,
    который ОДНОВРЕМЕННО привязан к новому домену 'АвтоСтайл'. Один аккаунт
    матчится к двум домен-салон записям → расход размазывается: часть утекает
    в несуществующий 'Элит Авто' (~5.4M ₽ direct+tp8 + ~1.7M пиксель).

    step0 делает полную перезагрузку local_gsheet_sites из FDW gsheet_sites,
    поэтому ручное удаление откатывается — фикс должен жить в коде шага.

    НЕ трогаем строку crm-174.ru (status='Не трогать!', login_key='Нет') —
    она не несёт fan-out расхода и помечена как защищённая.
    """
    with dst_conn.cursor() as cur:
        cur.execute("""
            DELETE FROM public.local_gsheet_sites
            WHERE salon = 'Элит Авто' AND status = 'Удален'
        """)
        n = cur.rowcount
    dst_conn.commit()
    logger.info("local_gsheet_sites: удалено %d строк закрытого салона 'Элит Авто' (status='Удален')", n)
    return n


# ── PERFORM_FUNNEL_2026-07-07-v5: статусы воронки Перформа ──────────────────
#
# Проблема: local_perform_leads не содержит CRM-статусов (status=''), т.к.
#   perform_leads в источнике создаётся без статуса (лиды идут отдельным потоком).
#   Реальные статусы живут в leads_all WHERE source_type='crmf_excel' AND
#   source_name IN (_PERFORM_SOURCE_NAMES) — 12 точек входа Перформ.
#
# Решение (v5): вместо отдельной таблицы local_perform_statuses —
#   UPDATE прямо в local_perform_leads.status по нормализованному телефону.
#   Нормализация: RIGHT(REGEXP_REPLACE(phone,'[^0-9]','','g'),10) — последние 10 цифр.
#   v5-логика матчинга: 4 ветки source_names/utm (crmf/mauto/plex/genzes), DISTINCT ON phone_norm.
#   Приоритет: sale(1) > visit(2) > qualified(3) > correct(4) > incorrect(5) > прочие(6).
#   Тай-брейкер: funnel-приоритет, затем created_date DESC.
#
# Вызывается в run() ПОСЛЕ TRUNCATE_INSERT_TABLES (local_perform_leads уже синкнут)
#   И ПОСЛЕ _ensure_crmf_lider_crm_statuses (local_crm_statuses актуальна).
# step1 читает status напрямую из local_perform_leads.status (без JOIN).

_PERFORM_SOURCE_NAMES = (
    'LeadVDL Perform Южный Обход',
    'LeadVDL Perform Автопарк Южный',
    'LeadVDL Perform Кубань Драйв',
    'LeadVDL Perform АвтоМаркет',
    'LeadVDL Perform Нижний Центр Авто',
    'LeadVDL Perform Нави Кар',
    'LeadV Perform Автопарк Южный',
    'LeadV Perform Южный Обход',
    'LeadV Perform Нижний Центр Авто',
    'LeadV Perform АвтоМаркет',
    'LeadV Perform Нави Кар',
    'LeadV Perform Кубань Драйв',
)

# PERFORM_FUNNEL_2026-07-07-v5: v5-матчинг по 4 веткам source_names/utm.
# Результат пишется напрямую в local_perform_leads.status (не в отдельную таблицу).

# crmf_excel: дополнительные source_names (Эйс Авто + Лидер + Лидер Авто НСК)
_PERFORM_CRMF_EXTRA_SOURCE_NAMES = (
    # Эйс Авто (явные source_names, подтверждено трассировкой телефонов)
    'LeadVDL 2 Эйс Авто', 'LeadV 2 Эйс Авто', 'LeadV ПБ Эйс Авто',
    'LeadV ПБО Эйс Авто', 'LeadVDLS Эйс Авто',
    # Лидер (явные source_names)
    'LeadVDL Лидер', 'LeadV Лидер', 'LeadVDL 2 Лидер',
    # Лидер Авто НСК (явные source_names)
    'LeadVDL 2 Лидер Авто НСК', 'LeadV ACB Лидер Авто НСК', 'LeadV Лидер Авто НСК',
    'LeadVDL 3 Лидер Авто НСК', 'LeadVDLS Лидер Авто НСК', 'LeadПБ Лидер Авто НСК',
)

# mauto_excel source_names с явным маркером «Перформ»
_PERFORM_MAUTO_SOURCE_NAMES = (
    'LeadV Перформ Лидер',
    'LeadV Перформ КТ Лидер',
)

def _apply_perform_statuses(dst_conn) -> int:
    """PERFORM_FUNNEL_2026-07-07-v5: пишет статус прямо в local_perform_leads.status.

    Вместо отдельной таблицы local_perform_statuses — UPDATE по нормализованному телефону.
    local_perform_leads синкается из FDW (TRUNCATE_INSERT_TABLES) с status='' для всех строк.
    После UPDATE matched-строки получают реальный CRM-статус из local_leads_all.
    Unmatched остаются '' → в step1: NULLIF('','')→NULL → COALESCE→'без статуса'.

    v5-логика матчинга (4 ветки):
      1. crmf_excel × 12 исходных source_names (LeadVDL/LeadV Perform × 6 салонов)
      2. crmf_excel × доп. source_names (Эйс Авто, Лидер, Лидер Авто НСК)
      3. mauto_excel × LeadV Перформ source_names
      4. plex_excel / genzes_excel × utm_source ILIKE '%perform%' (UTM явно указывает
         на Perform-трафик). Безопасно: кейс +79277779226 (plex, 'Продажа в кредит',
         utm_source='s:dzen.ru') НЕ матчится под ILIKE '%perform%'. Его же легитимная
         запись (utm_source='victory_urbancar_perform') матчится корректно.
    Телефоны вне этих 4 веток не получают статус (sentinel 'без статуса' в step1).

    DISTINCT ON phone_norm: приоритет sale(1)>visit(2)>qualified(3)>correct(4)>incorrect(5)>прочие(6).
    Тай-брейк при равном funnel-приоритете: created_date DESC NULLS LAST.
    """
    _crmf_names_sql  = ', '.join(f"'{n}'" for n in _PERFORM_SOURCE_NAMES)
    _crmf_extra_sql  = ', '.join(f"'{n}'" for n in _PERFORM_CRMF_EXTRA_SOURCE_NAMES)
    _mauto_names_sql = ', '.join(f"'{n}'" for n in _PERFORM_MAUTO_SOURCE_NAMES)

    # Guard: проверяем источник — local_leads_all уже синкнута выше в run()
    with dst_conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*)
            FROM public.local_leads_all
            WHERE source_type = 'crmf_excel'
              AND source_name IN ({_crmf_names_sql})
        """)
        src_count = cur.fetchone()[0]

    if src_count == 0:
        logger.warning(
            'local_perform_leads status: perform-записей в local_leads_all = 0 — '
            'UPDATE не выполнен (возможно, local_leads_all ещё не обновлена)',
        )
        return 0

    try:
        with dst_conn.cursor() as cur:
            cur.execute(f"""
                -- PERFORM_FUNNEL_2026-07-07-v5: UPDATE статуса прямо в local_perform_leads.
                -- v5-логика матчинга: 4 ветки (crmf/mauto/plex/genzes), DISTINCT ON phone_norm.
                -- Победитель DISTINCT ON: funnel-приоритет, тай-брейк — created_date DESC.
                WITH
                cs_ranked AS (
                    -- Маппинг CRM-статус → lead_status (sale/visit/qualified/correct/incorrect).
                    -- Приоритет crm_name: crmf/mauto(1) > default(2) > прочие(3).
                    SELECT DISTINCT ON (crm_status)
                        crm_status,
                        lead_status
                    FROM public.local_crm_statuses
                    WHERE COALESCE(kind, 'status') = 'status'
                      AND crm_name IN ('crmf', 'mauto', '', 'default')
                      AND COALESCE(salon, '') = ''
                    ORDER BY crm_status,
                        CASE crm_name
                            WHEN 'crmf'    THEN 1
                            WHEN 'mauto'   THEN 1
                            WHEN 'default' THEN 2
                            ELSE 3
                        END
                ),
                matched AS (
                    -- v5: 4 ветки (crmf/mauto/plex/genzes), DISTINCT ON phone_norm.
                    SELECT DISTINCT ON (RIGHT(REGEXP_REPLACE(la.phone, '[^0-9]', '', 'g'), 10))
                        RIGHT(REGEXP_REPLACE(la.phone, '[^0-9]', '', 'g'), 10) AS phone_norm,
                        la.status
                    FROM public.local_leads_all la
                    LEFT JOIN cs_ranked cs ON cs.crm_status = la.status
                    WHERE RIGHT(REGEXP_REPLACE(la.phone, '[^0-9]', '', 'g'), 10) != ''
                      AND (
                        -- Ветка 1: исходные 12 crmf_excel source_names (LeadVDL/LeadV Perform × 6)
                        (la.source_type = 'crmf_excel'
                         AND la.source_name IN ({_crmf_names_sql}))
                        -- Ветка 2: доп. crmf_excel source_names (Эйс Авто, Лидер, Лидер Авто НСК)
                        OR (la.source_type = 'crmf_excel'
                            AND la.source_name IN ({_crmf_extra_sql}))
                        -- Ветка 3: mauto_excel LeadV Перформ source_names
                        OR (la.source_type = 'mauto_excel'
                            AND la.source_name IN ({_mauto_names_sql}))
                        -- Ветка 4: plex_excel/genzes_excel с utm_source ILIKE '%perform%'
                        --   UTM явно указывает на Perform-трафик. Кейс +79277779226
                        --   (plex, utm_source='s:dzen.ru') НЕ матчится — безопасно.
                        OR (la.source_type IN ('plex_excel', 'genzes_excel')
                            AND la.utm_source ILIKE '%perform%')
                      )
                    ORDER BY
                        RIGHT(REGEXP_REPLACE(la.phone, '[^0-9]', '', 'g'), 10),
                        CASE COALESCE(cs.lead_status, '')
                            WHEN 'sale'      THEN 1
                            WHEN 'visit'     THEN 2
                            WHEN 'qualified' THEN 3
                            WHEN 'correct'   THEN 4
                            WHEN 'incorrect' THEN 5
                            ELSE 6
                        END,
                        la.created_date DESC NULLS LAST
                )
                UPDATE public.local_perform_leads lpl
                SET status = matched.status
                FROM matched
                WHERE RIGHT(REGEXP_REPLACE(lpl.phone, '[^0-9]', '', 'g'), 10)
                    = matched.phone_norm
            """)
            updated = cur.rowcount
        dst_conn.commit()
    except Exception:
        dst_conn.rollback()
        raise

    logger.info(
        'local_perform_leads: статус обновлён для %d строк (Перформ, v5-матчинг, 4 ветки)',
        updated,
    )
    return updated


# ── ROSTER_SYNC_2026-07-07: синк account_specialist_roster ───────────────────

def _sync_account_specialist_roster(dst_conn) -> dict:
    """ROSTER_SYNC_2026-07-07: автоматический UPSERT-синк account_specialist_roster.

    1. Удаляет строки с пустым specialist (нарушение правила + источник дублей).
    2. Обеспечивает UNIQUE(account) constraint (один раз, idempotent).
    3. UPSERT specialist из local_gsheet_sites WHERE directologist непустой.
       Если directologist пуст/NULL — строку не трогать вообще.
    4. UPDATE slepok из direct_ready_logins (только SET, никогда не обнулять).
       Если аккаунт пропал из direct_ready_logins — slepok оставить последнее известное значение.
    5. Пересчитывает robots:
         'люди'    — specialist есть, slepok пуст
         'гибриды' — specialist есть, slepok есть
         NULL      — specialist пуст (не должно быть в таблице после шага 3)

    Вызывается в run() ПОСЛЕ _sync_vk_ads_stats (local_gsheet_sites уже синкнут
    через TRUNCATE_INSERT_TABLES). direct_ready_logins живёт в той же БД ad_analytics_bi.
    """
    ROSTER = 'public.account_specialist_roster'

    # ─── 1. Удалить строки с пустым specialist ────────────────────────────────
    # Устраняет дубли (hand-filled пустые строки) и обеспечивает инвариант:
    # каждая строка roster обязана иметь specialist.
    with dst_conn.cursor() as cur:
        cur.execute(f"""
            DELETE FROM {ROSTER}
            WHERE specialist IS NULL OR TRIM(specialist) = ''
        """)
        deleted = cur.rowcount
    dst_conn.commit()
    if deleted:
        logger.info('%s: удалено %d строк с пустым specialist', ROSTER, deleted)

    # ─── 2. UNIQUE(account) — добавить constraint если отсутствует ────────────
    # После шага 1 дублей нет, ALTER TABLE безопасен.
    with dst_conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM information_schema.table_constraints
            WHERE table_schema    = 'public'
              AND table_name      = 'account_specialist_roster'
              AND constraint_type = 'UNIQUE'
        """)
        has_unique = cur.fetchone()[0] > 0

    if not has_unique:
        with dst_conn.cursor() as cur:
            cur.execute(f"""
                ALTER TABLE {ROSTER}
                ADD CONSTRAINT uq_account_specialist_roster_account UNIQUE (account)
            """)
        dst_conn.commit()
        logger.info('%s: добавлен UNIQUE(account) constraint', ROSTER)

    # ─── 3–5. UPSERT specialist + UPDATE slepok + UPDATE robots (одна транзакция)
    try:
        with dst_conn.cursor() as cur:

            # 3. UPSERT specialist из local_gsheet_sites.
            # Только аккаунты с непустым directologist — без него строку не трогать.
            # DISTINCT ON (login_key): один аккаунт может иметь несколько доменов в
            # local_gsheet_sites — берём первый по (login_key, directologist) детерминировано,
            # иначе ON CONFLICT DO UPDATE падает на «affect row a second time».
            cur.execute(f"""
                INSERT INTO {ROSTER} (account, specialist)
                SELECT DISTINCT ON (gs.login_key)
                    gs.login_key     AS account,
                    gs.directologist AS specialist
                FROM public.local_gsheet_sites gs
                WHERE gs.login_key IS NOT NULL
                  AND TRIM(COALESCE(gs.login_key, '')) != ''
                  AND gs.directologist IS NOT NULL
                  AND TRIM(gs.directologist) != ''
                ORDER BY gs.login_key, gs.directologist
                ON CONFLICT (account) DO UPDATE
                    SET specialist = EXCLUDED.specialist
            """)
            upserted = cur.rowcount

            # 4. UPDATE slepok из direct_ready_logins — только SET, никогда не обнулять.
            # Если аккаунт пропал из direct_ready_logins — slepok не трогаем.
            cur.execute(f"""
                UPDATE {ROSTER} r
                SET slepok = drl.slepok
                FROM public.direct_ready_logins drl
                WHERE r.account = drl.login
                  AND COALESCE(drl.slepok, '') != ''
                  AND drl.slepok IS DISTINCT FROM r.slepok
            """)
            slepok_updated = cur.rowcount

            # 5. Пересчёт robots по specialist + slepok
            cur.execute(f"""
                UPDATE {ROSTER}
                SET robots = CASE
                    WHEN specialist IS NOT NULL
                         AND (slepok IS NULL OR slepok = '') THEN 'люди'
                    WHEN specialist IS NOT NULL
                         AND slepok IS NOT NULL AND slepok != '' THEN 'гибриды'
                    ELSE NULL
                END
            """)
            robots_updated = cur.rowcount

        dst_conn.commit()
    except Exception:
        dst_conn.rollback()
        raise

    # ─── Итоговый отчёт ──────────────────────────────────────────────────────
    with dst_conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                COUNT(*)                                         AS total,
                COUNT(*) FILTER (WHERE robots = 'люди')         AS lyudi,
                COUNT(*) FILTER (WHERE robots = 'гибриды')      AS gibridy,
                COUNT(*) FILTER (WHERE slepok IS NOT NULL
                                   AND slepok != '')            AS with_slepok
            FROM {ROSTER}
        """)
        total, lyudi, gibridy, with_slepok = cur.fetchone()

    logger.info(
        '%s: total=%d люди=%d гибриды=%d со_слепком=%d | '
        'upsert_specialist=%d slepok_updated=%d robots_updated=%d deleted=%d',
        ROSTER, total, lyudi, gibridy, with_slepok,
        upserted, slepok_updated, robots_updated, deleted,
    )
    return {
        'total': total,
        'lyudi': lyudi,
        'gibridy': gibridy,
        'with_slepok': with_slepok,
        'upserted': upserted,
        'slepok_updated': slepok_updated,
        'robots_updated': robots_updated,
        'deleted': deleted,
    }


# ── Точка входа шага ──────────────────────────────────────────────────────────

def run(conn, run_id: str) -> dict:
    """conn — DST соединение (ad_analytics_bi). SRC берётся из пула."""
    logger.info('Шаг 0: синхронизация локальных копий')

    src_conn = get_src_conn()
    try:
        _ensure_local_tables(conn)

        # ── Яндекс: VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17 ────────────────
        # local_yandex больше не нужна — все потребители переведены напрямую
        # на FOREIGN TABLE yandex_direct_manager_reports (FDW).
        # _sync_yandex_full отключена; local_yandex таблица остаётся в БД
        # (DDL сохранён для обратной совместимости), но не заполняется.
        yandex_rows = 0
        # (было: yandex_rows = _sync_yandex_full(src_conn, conn))

        # ── Остальные таблицы (TRUNCATE+INSERT всегда) ────────────────────
        other_rows = 0
        for src_table, local_table in TRUNCATE_INSERT_TABLES:
            # PERFORM_LEADS_2026-07-01: perform_leads имеет ту же схему (created_date + arrival_date)
            date_col = 'created_date' if src_table in (SRC_LEADS_ALL, SRC_PERFORM_LEADS) else None
            rows = _sync_truncate_insert(src_conn, conn, src_table, local_table, date_col=date_col)
            other_rows += rows

        # Удаляем закрытый салон 'Элит Авто' (status='Удален') — fan-out расхода.
        # Должно идти ПОСЛЕ синка gsheet_sites_local→local_gsheet_sites, иначе
        # полная перезагрузка из FDW восстановит строки.
        _patch_remove_elit_avto(conn)

        # PIXEL_PR_2026-07-09: pixel_pr домены Перформа не существуют в GSheet gsheet_sites,
        # добавляем после TRUNCATE+INSERT чтобы build_pixel.py (step5) пропускал их в pixel_leads.
        _insert_pixel_pr_sites(conn)

        _sync_telega_in_orders(conn)
        # _patch_crm_statuses(conn)  # отключено 2026-05-20: crm_statuses уже верный
        marcar_patch = _patch_marcar_statuses(conn)
        # PATCH-CRMF-LIDER-CRM-STATUSES-2026-06-15: salon-specific маппинги crmf+Лидер
        # (FDW crm_statuses их не содержит → добавляем после каждого TRUNCATE+INSERT)
        _ensure_crmf_lider_crm_statuses(conn)

        # PERFORM_FUNNEL_2026-07-07-v5: UPDATE статуса в local_perform_leads.
        # Вызов ПОСЛЕ TRUNCATE_INSERT_TABLES (local_perform_leads уже синкнут из FDW)
        # и _ensure_crmf_lider_crm_statuses (local_crm_statuses должна быть актуальна).
        _apply_perform_statuses(conn)

        # VK_ADS_INTEGRATION_2026-07-06: расходы ВК Реклама из FDW (pre-агрег. до day+account+кампания).
        # Вызов ПОСЛЕ синка local_gsheet_sites (уже в TRUNCATE_INSERT_TABLES выше):
        # step3 джойнит local_vk_ads_stats_day с local_gsheet_sites по vk_client_id
        # для фильтрации direction='Авто' (2 аккаунта: autopro-116.site, autocenter-152.site).
        _sync_vk_ads_stats(conn)

        # ROSTER_SYNC_2026-07-07: синк account_specialist_roster.
        # Вызов ПОСЛЕ _sync_vk_ads_stats и TRUNCATE_INSERT_TABLES (local_gsheet_sites свежая).
        # direct_ready_logins в той же БД (ad_analytics_bi) — обычный SQL, без FDW.
        _sync_account_specialist_roster(conn)

        # ── Индексы на local_leads_all (пересоздаётся TRUNCATE+INSERT каждый прогон) ──
        # Без индексов таблица 286 MB / 845K строк → seq scan на каждом запросе.
        # domain_id     — JOIN в step13 (local_leads_all JOIN local_domains ON ld.id = l.domain_id)
        # deal_type     — WHERE deal_type='Звонок' в step13 (calls_base CTE)
        # source_type   — WHERE source_type='marcar_crm_excel' в _patch_marcar_statuses и step10
        # utm_campaign  — удалён INDEX_AUDIT_2026-06-27 (заменён на utm_src_med, idx_scan=0)
        # created_date  — range filter в step10 (created_date >= '2026-01-01')
        # status        — INDEX_AUDIT_2026-06-27: добавлен (step2_build_analytics фильтр по статусу)
        # domain_date   — INDEX_AUDIT_2026-06-27: добавлен (составной domain_id+created_date, step2)
        # utm_src_med   — INDEX_AUDIT_2026-06-27: добавлен, заменяет utm_campaign (атрибуция source/medium)
        with conn.cursor() as _cur:
            for _idx_sql in (
                'CREATE INDEX IF NOT EXISTS idx_local_leads_all_domain_id'
                '    ON public.local_leads_all (domain_id)',
                'CREATE INDEX IF NOT EXISTS idx_local_leads_all_deal_type'
                '    ON public.local_leads_all (deal_type)',
                'CREATE INDEX IF NOT EXISTS idx_local_leads_all_source_type'
                '    ON public.local_leads_all (source_type)',
                'CREATE INDEX IF NOT EXISTS idx_local_leads_all_created_date'
                '    ON public.local_leads_all (created_date)',
                'CREATE INDEX IF NOT EXISTS idx_local_leads_all_status'
                '    ON public.local_leads_all (status)',
                'CREATE INDEX IF NOT EXISTS idx_local_leads_all_domain_date'
                '    ON public.local_leads_all (domain_id, created_date)',
                'CREATE INDEX IF NOT EXISTS idx_local_leads_all_utm_src_med'
                '    ON public.local_leads_all (utm_source, utm_medium)',
                # local_domains тоже пересоздаётся TRUNCATE+INSERT, 4 488 строк
                # маппинг domain_id→name в step13/step10 — lookup по name
                'CREATE INDEX IF NOT EXISTS idx_local_domains_name'
                '    ON public.local_domains (name)',
                # PERFORM_LEADS_2026-07-01: local_perform_leads — аналог local_leads_all для perform-аккаунтов
                'CREATE INDEX IF NOT EXISTS idx_local_perform_leads_domain_id'
                '    ON public.local_perform_leads (domain_id)',
                'CREATE INDEX IF NOT EXISTS idx_local_perform_leads_created_date'
                '    ON public.local_perform_leads (created_date)',
                'CREATE INDEX IF NOT EXISTS idx_local_perform_leads_deal_type'
                '    ON public.local_perform_leads (deal_type)',
            ):
                _cur.execute(_idx_sql)
        conn.commit()
        logger.info('local_leads_all + local_domains: индексы созданы/обновлены')

    finally:
        put_src_conn(src_conn)

    total_rows = yandex_rows + other_rows
    return {
        'rows': total_rows,
        'details': f'yandex={yandex_rows}, other={other_rows}',
    }
