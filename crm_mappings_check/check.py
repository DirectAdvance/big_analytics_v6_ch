"""
crm_mappings_check/check.py — отчёт о неиспользуемых маппингах в local_crm_statuses.

Логика:
  - Для kind='status': crm_status проверяется против local_leads_all.status
  - Для kind='reason': crm_status проверяется против local_leads_all.reason

Если статус определён в local_crm_statuses, но не встречается в leads → unused.
Отправляется Telegram при каждом прогоне pipeline.
Экспорт UNMAPPED reason → Google Sheets (SPREADSHEET_ID, лист Лист1).

Вызывается из pipeline.py / fast_pipeline.py inline (как step11/step12).
"""

import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger('pipeline.crm_mappings_check')

try:
    from config.tokens import TELEGRAM_PROXY_VARIANTS as _TG_PROXY_VARIANTS
except Exception:
    _TG_PROXY_VARIANTS = [None]  # direct fallback если не удалось загрузить

# Google Sheets с UNMAPPED reason
SPREADSHEET_ID = '1Oe_2a0F6m3seZUgzN9ui-X3WHk8eIEGmRv5Xan1oHog'
WORKSHEET_NAME = 'Лист1'

# Колонки листа (фиксированный порядок, совпадает с текущей структурой)
SHEET_HEADERS = ['reason', 'source_type (CRM)', 'count', 'statuses', 'Предложение', 'ИТОГ']


_UNUSED_SQL = """
SELECT cs.crm_status,
       cs.lead_status,
       COALESCE(NULLIF(cs.crm_name, ''), 'default') AS crm_name,
       COALESCE(NULLIF(cs.salon, ''), '-')          AS salon,
       cs.kind
FROM local_crm_statuses cs
WHERE
  (cs.kind = 'status' AND cs.crm_status NOT IN (
       SELECT DISTINCT status FROM local_leads_all
       WHERE status IS NOT NULL AND status != ''
  ))
  OR
  (cs.kind = 'reason' AND cs.crm_status NOT IN (
       SELECT DISTINCT reason FROM local_leads_all
       WHERE reason IS NOT NULL AND reason != ''
  ))
ORDER BY cs.kind, cs.lead_status, cs.crm_status;
"""


_UNMAPPED_STATUS_SQL = """
SELECT status AS val, COUNT(*) AS cnt
FROM local_leads_all
WHERE status IS NOT NULL AND status != ''
  AND status NOT IN (
      SELECT DISTINCT crm_status FROM local_crm_statuses WHERE kind='status'
  )
GROUP BY status
ORDER BY cnt DESC;
"""


# Расширенный SQL: reason + source_type(cnt) + statuses из local_leads_all
_UNMAPPED_REASON_SQL = """
WITH reason_by_source AS (
    SELECT
        reason,
        source_type,
        COUNT(*) AS cnt
    FROM local_leads_all
    WHERE reason IS NOT NULL AND reason != ''
      AND reason NOT IN (
          SELECT DISTINCT crm_status FROM local_crm_statuses WHERE kind = 'reason'
      )
    GROUP BY reason, source_type
),
reason_agg AS (
    SELECT
        reason AS val,
        STRING_AGG(source_type || '(' || cnt::text || ')', ', ' ORDER BY cnt DESC) AS source_type_crm,
        SUM(cnt) AS cnt
    FROM reason_by_source
    GROUP BY reason
),
reason_statuses AS (
    SELECT
        lla.reason,
        STRING_AGG(DISTINCT lla.status, ', ' ORDER BY lla.status) AS statuses
    FROM local_leads_all lla
    WHERE lla.reason IS NOT NULL AND lla.reason != ''
      AND lla.status IS NOT NULL AND lla.status != ''
      AND lla.reason IN (SELECT DISTINCT reason FROM reason_by_source)
    GROUP BY lla.reason
)
SELECT
    ra.val,
    ra.source_type_crm,
    ra.cnt,
    COALESCE(rs.statuses, '') AS statuses
FROM reason_agg ra
LEFT JOIN reason_statuses rs ON rs.reason = ra.val
ORDER BY ra.cnt DESC;
"""


def _send_telegram(text, bot_token, chat_id):
    """Отправка в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct)."""
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    chunks = []
    cur = ''
    for line in text.split('\n'):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = ''
        cur += line + '\n'
    if cur:
        chunks.append(cur)
    for c in chunks:
        sent = False
        for proxies in _TG_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
            try:
                resp = requests.post(
                    url,
                    json={'chat_id': chat_id, 'text': c,
                          'disable_web_page_preview': True},
                    timeout=15,
                    proxies=proxies,
                )
                if resp.ok:
                    sent = True
                    break
                logger.warning('Telegram %s: %s', resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning('Telegram (proxies=%s): %s', proxies, e)
        if not sent:
            logger.warning('Telegram чанк не отправлен')


def _build_text(unused, unmapped_status, unmapped_reason, run_id):
    """Отчёт: только UNMAPPED секции (status + reason).
    UNUSED-маппинги (определены, но не встречаются в leads) намеренно не печатаем
    в Telegram — пользователь не хочет шума о неиспользуемых маппингах.
    """
    lines = []
    lines.append('🧹 Проверка local_crm_statuses ↔ local_leads_all')
    if run_id:
        lines.append(f'run_id: {run_id}')
    lines.append('')

    # === Unmapped статусы (есть в leads, нет в crm_statuses) ===
    total_status_leads  = sum(r['cnt'] for r in unmapped_status)
    total_reason_leads  = sum(r['cnt'] for r in unmapped_reason)

    lines.append('═══ UNMAPPED status в leads (нет в local_crm_statuses) ═══')
    lines.append(f'Уникальных: {len(unmapped_status)}, лидов без категории: {total_status_leads}')
    if unmapped_status:
        for r in unmapped_status[:50]:
            lines.append(f'  {r["val"]!r}  cnt={r["cnt"]}')
        if len(unmapped_status) > 50:
            lines.append(f'  ... ещё {len(unmapped_status) - 50}')
    lines.append('')

    lines.append('═══ UNMAPPED reason в leads (нет в local_crm_statuses) ═══')
    lines.append(f'Уникальных: {len(unmapped_reason)}, лидов без категории: {total_reason_leads}')
    if unmapped_reason:
        for r in unmapped_reason[:50]:
            statuses_str = f'  [statuses: {r["statuses"]}]' if r.get('statuses') else ''
            lines.append(f'  {r["val"]!r}  cnt={r["cnt"]}{statuses_str}')
        if len(unmapped_reason) > 50:
            lines.append(f'  ... ещё {len(unmapped_reason) - 50}')
    lines.append('')

    return '\n'.join(lines)


def _get_gspread_client():
    """Авторизация через service_account.json (ищем стандартные пути)."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.warning('gspread / google-auth не установлены — экспорт в Sheets пропущен')
        return None

    _SA_CANDIDATES = [
        Path(__file__).resolve().parent.parent / 'config' / 'cedar-gearbox-464117-e5-676d6cc8937e.json',
        Path(os.path.expanduser('~/.secret/google/service_account.json')),
        Path(os.path.expanduser('~/.secret/service_account.json')),
    ]
    sa_file = next((str(p) for p in _SA_CANDIDATES if p.exists()), None)
    if not sa_file:
        logger.warning('service_account.json не найден — экспорт в Sheets пропущен')
        return None

    try:
        creds = Credentials.from_service_account_file(
            sa_file,
            scopes=['https://www.googleapis.com/auth/spreadsheets'],
        )
        return gspread.authorize(creds)
    except Exception as e:
        logger.warning('Ошибка авторизации gspread: %s', e)
        return None


def _export_to_sheets(unmapped_reason: list[dict]) -> int:
    """Обновляет Google Sheets: перезаписывает данные, сохраняя столбцы Предложение и ИТОГ.

    Структура листа (SHEET_HEADERS):
      reason | source_type (CRM) | count | statuses | Предложение | ИТОГ

    Алгоритм:
      1. Читаем текущие данные листа — сохраняем Предложение/ИТОГ по ключу reason.
      2. Формируем новые строки из unmapped_reason (колонки 0-3).
      3. Восстанавливаем Предложение/ИТОГ из сохранённых данных (колонки 4-5).
      4. Перезаписываем весь лист.

    Возвращает количество записанных строк данных (без заголовка).
    """
    client = _get_gspread_client()
    if client is None:
        return 0

    try:
        import gspread
        sh = client.open_by_key(SPREADSHEET_ID)
        try:
            ws = sh.worksheet(WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=10)

        # Считываем текущие данные — сохраняем Предложение/ИТОГ по reason
        existing = ws.get_all_values()
        saved_annotations: dict[str, list] = {}
        if existing:
            hdr = existing[0]
            try:
                idx_reason     = hdr.index('reason')
                idx_predlozh   = hdr.index('Предложение')
                idx_itog       = hdr.index('ИТОГ')
            except ValueError:
                idx_reason = 0
                idx_predlozh = 4
                idx_itog = 5
            for row in existing[1:]:
                if len(row) > idx_reason:
                    key = row[idx_reason]
                    pred = row[idx_predlozh] if len(row) > idx_predlozh else ''
                    itog = row[idx_itog] if len(row) > idx_itog else ''
                    saved_annotations[key] = [pred, itog]

        # Формируем новые строки
        new_rows = [SHEET_HEADERS]
        for r in unmapped_reason:
            val           = r['val']
            source_crm    = r.get('source_type_crm', '')
            cnt           = str(int(r['cnt']))
            statuses      = r.get('statuses', '')
            pred, itog    = saved_annotations.get(val, ['', ''])
            new_rows.append([val, source_crm, cnt, statuses, pred, itog])

        # Очищаем лист и записываем новые данные
        ws.clear()
        ws.update(values=new_rows, range_name='A1', value_input_option='RAW')
        logger.info('Sheets: записано %d строк в %s', len(new_rows) - 1, WORKSHEET_NAME)
        return len(new_rows) - 1

    except Exception as e:
        logger.warning('Ошибка экспорта в Google Sheets: %s', e)
        return 0


def run(conn, run_id=None, **kwargs) -> dict:
    """Сверка local_crm_statuses ↔ local_leads_all + Telegram + Sheets."""
    t0 = time.perf_counter()

    from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    def _query(sql):
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    unused          = _query(_UNUSED_SQL)
    unmapped_status = _query(_UNMAPPED_STATUS_SQL)
    unmapped_reason = _query(_UNMAPPED_REASON_SQL)

    text = _build_text(unused, unmapped_status, unmapped_reason, run_id)
    _send_telegram(text, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    sheets_rows = _export_to_sheets(unmapped_reason)

    elapsed = time.perf_counter() - t0
    logger.info(
        'crm_mappings_check: unused=%d, unmapped_status=%d, unmapped_reason=%d, sheets=%d, %.1f сек',
        len(unused), len(unmapped_status), len(unmapped_reason), sheets_rows, elapsed,
    )
    return {
        'rows': len(unused) + len(unmapped_status) + len(unmapped_reason),
        'details': (
            f'unused={len(unused)}, unmapped_status={len(unmapped_status)}, '
            f'unmapped_reason={len(unmapped_reason)}, sheets={sheets_rows}'
        ),
    }


if __name__ == '__main__':
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config.db as db_module

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )
    db_module.init_pool()
    _conn = db_module.get_conn()
    try:
        result = run(_conn, run_id='manual')
        print(result)
    finally:
        db_module.put_conn(_conn)
        db_module.close_pool()
