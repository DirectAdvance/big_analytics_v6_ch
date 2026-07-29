"""
revoke_metrika_grants.py — отзыв грантов Яндекс.Метрики для удалённых/остановленных сайтов.

Логика:
1. Добавляет колонку `status` в public.metrika_yandex (если нет) и синхронизирует из local_gsheet_sites.
2. Находит строки где:
   - status IN ('Удален', 'Стоп')
   - counter_id NOT NULL, counter_name NOT NULL
   - crm_order_created / crm_order_paid / crm_spam_order / crm_order_canceled NOT NULL
   - grant_done_lots04 = TRUE ИЛИ grant_done_skuderko1 = TRUE
3. Через API Метрики удаляет grant для victorylotsofads04 и skuderko1 на каждом counter_id.
4. Сбрасывает флаги grant_done_lots04 / grant_done_skuderko1 = FALSE в БД.

ОСТОРОЖНО: grant для victorylotsofads1 (grant_done_lots1) НЕ отзывается —
он нужен для основного sync целей (Фаза 3 metrika_yandex.py).

Запуск:
    python revoke_metrika_grants.py [--dry-run]

--dry-run: только показывает что будет сделано, без реальных API-вызовов и изменений в БД.
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import requests

from config.settings import DB_DST
from config.tokens import OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5

logger = logging.getLogger('revoke_metrika_grants')

METRIKA_BASE = 'https://api-metrika.yandex.net'

# Донорские токены — те же что в metrika_yandex.py (они владельцы счётчиков)
GRANT_DONOR_TOKENS = [
    ('victoryagency-direct1618440', OAUTH_TOKEN_2),
    ('y-direct-victory',            OAUTH_TOKEN_3),
    ('victoryagency14',             OAUTH_TOKEN_4),
    ('useful-call-agency',          OAUTH_TOKEN_5),
]

# Только эти два аккаунта отзываются (audit-аккаунты)
REVOKE_TARGETS = [
    ('victorylotsofads04', 'grant_done_lots04'),
    ('skuderko1',          'grant_done_skuderko1'),
]

# Статусы при которых отзываем доступ
REVOKE_STATUSES = ('Удален', 'Стоп')


# ── Шаг 1: добавить и заполнить колонку status ────────────────────────────────

def _ensure_status_column(conn) -> None:
    """Добавляет колонку status в metrika_yandex если её нет."""
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE public.metrika_yandex
            ADD COLUMN IF NOT EXISTS status TEXT
        """)
    conn.commit()
    logger.info('[revoke] Колонка status обеспечена (ADD IF NOT EXISTS)')


def _sync_status_from_gsheet(conn) -> int:
    """Обновляет поле status из local_gsheet_sites по домену.
    Возвращает количество обновлённых строк.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.metrika_yandex m
            SET status = gs.status
            FROM public.local_gsheet_sites gs
            WHERE LOWER(TRIM(m.domain)) = LOWER(TRIM(gs.domain))
              AND m.status IS DISTINCT FROM gs.status
        """)
        updated = cur.rowcount
    conn.commit()
    logger.info('[revoke] Синхронизировано статусов: %d строк', updated)
    return updated


# ── Шаг 2: найти строки под условие ──────────────────────────────────────────

def _find_candidates(conn) -> list[dict]:
    """Возвращает список строк для отзыва гранта.

    Условие:
    - status IN ('Удален', 'Стоп')
    - counter_id NOT NULL, counter_name NOT NULL и не пустое
    - crm_order_created / crm_order_paid / crm_spam_order / crm_order_canceled NOT NULL
    - grant_done_lots04 = TRUE ИЛИ grant_done_skuderko1 = TRUE
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT domain, counter_id, counter_name, status,
                   grant_done_lots04, grant_done_skuderko1
            FROM public.metrika_yandex
            WHERE status IN %s
              AND counter_id IS NOT NULL
              AND counter_name IS NOT NULL AND counter_name != ''
              AND crm_order_created IS NOT NULL
              AND crm_order_paid IS NOT NULL
              AND crm_spam_order IS NOT NULL
              AND crm_order_canceled IS NOT NULL
              AND (grant_done_lots04 = TRUE OR grant_done_skuderko1 = TRUE)
            ORDER BY status, domain
        """, (REVOKE_STATUSES,))
        rows = cur.fetchall()

    candidates = []
    for domain, counter_id, counter_name, status, lots04, skuderko1 in rows:
        candidates.append({
            'domain': domain,
            'counter_id': counter_id,
            'counter_name': counter_name,
            'status': status,
            'grant_done_lots04': lots04,
            'grant_done_skuderko1': skuderko1,
        })
    return candidates


# ── Шаг 3: отзыв через Metrika API ───────────────────────────────────────────

def _revoke_grant(counter_id: int, target_login: str, dry_run: bool) -> bool:
    """Отзывает grant для target_login на счётчике counter_id.

    Перебирает донорские токены. True = успех (отозван или не существовал).
    Использует DELETE /management/v1/counter/{id}/grants/{login}
    """
    if dry_run:
        logger.info('  [dry-run] REVOKE grant: %d → %s', counter_id, target_login)
        return True

    for donor_login, token in GRANT_DONOR_TOKENS:
        try:
            r = requests.delete(
                f'{METRIKA_BASE}/management/v1/counter/{counter_id}/grants/{target_login}',
                headers={'Authorization': f'OAuth {token}'},
                timeout=30,
            )
            if r.status_code in (200, 201, 204):
                logger.info('  grant отозван: %d → %s ← %s', counter_id, target_login, donor_login)
                return True
            if r.status_code == 404:
                # Grant уже не существует — считаем успехом
                logger.info('  grant не найден (404): %d → %s ← %s', counter_id, target_login, donor_login)
                return True
            if r.status_code == 403:
                # Этот донор не владелец — пробуем следующего
                logger.debug('  403 (не владелец): %d ← %s, пробуем следующего донора', counter_id, donor_login)
                time.sleep(0.2)
                continue
            logger.warning('  ошибка revoke %d → %s через %s: HTTP %d: %s',
                           counter_id, target_login, donor_login, r.status_code, r.text[:200])
        except Exception as e:
            logger.warning('  revoke exception %d → %s через %s: %s', counter_id, target_login, donor_login, e)
        time.sleep(0.3)

    logger.error('  revoke НЕ УДАЛОСЬ: %d → %s (все доноры исчерпаны)', counter_id, target_login)
    return False


# ── Шаг 4: сброс флагов в БД ─────────────────────────────────────────────────

def _reset_flag(conn, counter_id: int, flag_col: str, dry_run: bool) -> None:
    """Сбрасывает флаг grant_done_* = FALSE для указанного counter_id."""
    if dry_run:
        logger.info('  [dry-run] RESET %s = FALSE WHERE counter_id = %d', flag_col, counter_id)
        return
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE public.metrika_yandex
            SET {flag_col} = FALSE, updated_at = NOW()
            WHERE counter_id = %s
        """, (counter_id,))
    conn.commit()


# ── Основная логика ───────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    logger.info('[revoke] Старт. dry_run=%s', dry_run)
    conn = psycopg2.connect(**DB_DST)

    try:
        # Шаг 1: добавить колонку и синхронизировать статусы
        _ensure_status_column(conn)
        updated = _sync_status_from_gsheet(conn)
        logger.info('[revoke] Статусы синхронизированы: %d строк обновлено', updated)

        # Шаг 2: найти кандидатов
        candidates = _find_candidates(conn)
        logger.info('[revoke] Кандидатов на отзыв гранта: %d строк', len(candidates))

        if not candidates:
            logger.info('[revoke] Нечего отзывать. Завершение.')
            return

        # Группируем по counter_id (один счётчик может быть у нескольких доменов)
        by_counter: dict[int, dict] = {}
        for c in candidates:
            cid = c['counter_id']
            if cid not in by_counter:
                by_counter[cid] = c
            else:
                # Если хотя бы один домен требует отзыва — отзываем
                if c['grant_done_lots04']:
                    by_counter[cid]['grant_done_lots04'] = True
                if c['grant_done_skuderko1']:
                    by_counter[cid]['grant_done_skuderko1'] = True

        logger.info('[revoke] Уникальных counter_id: %d', len(by_counter))
        if dry_run:
            logger.info('[revoke] === DRY-RUN MODE: изменений НЕ будет ===')
            for cid, info in by_counter.items():
                logger.info('  counter=%d (%s) status=%s lots04=%s skuderko1=%s',
                            cid, info['domain'], info['status'],
                            info['grant_done_lots04'], info['grant_done_skuderko1'])
            logger.info('[revoke] Итого: %d counter_id × %d targets = %d API-вызовов (dry-run)',
                        len(by_counter), len(REVOKE_TARGETS),
                        len(by_counter) * len(REVOKE_TARGETS))
            return

        # Шаг 3+4: для каждого counter_id × каждый target
        revoked_ok = 0
        revoke_failed = []

        for i, (cid, info) in enumerate(by_counter.items()):
            logger.info('[revoke] [%d/%d] counter=%d (%s, status=%s)',
                        i + 1, len(by_counter), cid, info['domain'], info['status'])

            for target_login, flag_col in REVOKE_TARGETS:
                if not info.get(flag_col, False):
                    logger.debug('  %s: флаг уже FALSE, пропускаем', target_login)
                    continue

                ok = _revoke_grant(cid, target_login, dry_run=False)
                if ok:
                    _reset_flag(conn, cid, flag_col, dry_run=False)
                    revoked_ok += 1
                else:
                    revoke_failed.append((cid, target_login))

            time.sleep(0.3)

            if (i + 1) % 50 == 0:
                logger.info('[revoke]   ...%d/%d counter_id обработано', i + 1, len(by_counter))

        logger.info('[revoke] Готово. Успешно: %d, Ошибок: %d',
                    revoked_ok, len(revoke_failed))
        if revoke_failed:
            logger.warning('[revoke] Не удалось отозвать (%d): %s',
                           len(revoke_failed), revoke_failed[:20])

    except Exception as e:
        logger.error('[revoke] Критическая ошибка: %s', e, exc_info=True)
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )

    parser = argparse.ArgumentParser(description='Отзыв грантов Метрики для Удален/Стоп сайтов')
    parser.add_argument('--dry-run', action='store_true',
                        help='Показать что будет сделано без реальных изменений')
    args = parser.parse_args()

    run(dry_run=args.dry_run)
