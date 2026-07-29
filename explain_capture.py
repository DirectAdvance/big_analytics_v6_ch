"""
explain_capture.py — захват EXPLAIN ANALYZE планов тяжёлых шагов.

Активируется ТОЛЬКО при EXPLAIN_CAPTURE=1 (env). По умолчанию — ноп.

Правильный механизм (EXPLAIN_CAPTURE_REAL_v2):
  • При EXPLAIN_CAPTURE=1 реальный тяжёлый statement шага оборачивается в
    EXPLAIN (ANALYZE, BUFFERS, VERBOSE, TIMING ON, FORMAT TEXT) ВМЕСТО обычного
    execute. EXPLAIN ANALYZE на CTAS/INSERT/UPDATE исполняет statement РЕАЛЬНО —
    таблица строится идентично, NOT a double run.
  • rowcount под флагом берётся через SELECT COUNT(*) FROM <target>, поскольку
    у EXPLAIN ANALYZE cur.rowcount = кол-во строк плана, не данных.
  • Без флага EXPLAIN_CAPTURE — поведение байт-в-байт прежнее (обычный execute,
    обычный rowcount).
  • Для шага 7 (utility-операторы: SET LOGGED / CREATE INDEX / VACUUM / ANALYZE)
    EXPLAIN не поддерживается → пишем wall-clock каждой под-операции в тот же лог.

API:
  wrap_explain(conn, sql, step_name, run_id, log_file, wall_sec=None, count_table=None)
      Оборачивает sql в EXPLAIN ANALYZE, пишет план в лог.
      Если count_table задан — добавляет SELECT COUNT(*) FROM count_table в план.
      Возвращает rowcount (int) если count_table задан, иначе None.
      Ноп при EXPLAIN_CAPTURE=False.

  log_wall_sub(run_id, step_name, sub_name, wall_sec, log_file=None)
      Пишет wall-clock под-операции в лог (для step7).
      Ноп при EXPLAIN_CAPTURE=False.

  write_header(run_id, log_file=None) -> str
      Записывает заголовок файла лога в начале прогона.

Файл лога: ~/big_analytics_v5/_logs/explain_run_<run_id>.log
Разделитель: ===== <step_name> wall=<сек> =====
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger('pipeline.explain_capture')

# Глобальный флаг (читается один раз при импорте)
EXPLAIN_CAPTURE: bool = os.environ.get('EXPLAIN_CAPTURE', '').strip() == '1'

# Маркер в логе для проверки доезда патча (grep-маркер деплоя)
# EXPLAIN_CAPTURE_REAL_v2


def _log_path(run_id: str) -> str:
    """Путь к файлу лога планов для данного run_id."""
    base = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(base, '_logs')
    os.makedirs(logs_dir, exist_ok=True)
    return os.path.join(logs_dir, f'explain_run_{run_id}.log')


def write_header(run_id: str, log_file: Optional[str] = None) -> str:
    """Записать заголовок файла лога в начале прогона. Вернуть путь."""
    if not EXPLAIN_CAPTURE:
        return ''
    path = log_file or _log_path(run_id)
    import datetime
    header = (
        f'EXPLAIN CAPTURE LOG  run_id={run_id}  '
        f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
        f'Флаг: EXPLAIN_CAPTURE=1\n'
        f'Механизм: EXPLAIN ANALYZE оборачивает РЕАЛЬНЫЙ statement (CTAS/INSERT/UPDATE).\n'
        f'  EXPLAIN ANALYZE исполняет statement — таблица строится идентично (не двойной прогон).\n'
        f'  rowcount берётся через SELECT COUNT(*) FROM <target> под флагом.\n'
        f'  step7: utility-операторы (SET LOGGED/CREATE INDEX/VACUUM/ANALYZE) — wall-clock без EXPLAIN.\n'
        f'Шаги: step1, step3, step6, step7(wall-clock), step11, step13, build_unified\n'
        f'{"=" * 70}\n'
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(header)
        f.flush()
    logger.info('explain_capture: лог открыт: %s', path)
    return path


def wrap_explain(
    conn,
    sql: str,
    step_name: str,
    run_id: str,
    log_file: Optional[str] = None,
    wall_sec: Optional[float] = None,
    count_table: Optional[str] = None,
    work_mem: str = '512MB',
) -> Optional[int]:
    """
    Оборачивает sql в EXPLAIN (ANALYZE, BUFFERS, TIMING ON, FORMAT TEXT) и
    исполняет его ВМЕСТО обычного execute. Пишет план в лог.

    EXPLAIN ANALYZE на CTAS/INSERT INTO ... SELECT / UPDATE РЕАЛЬНО исполняет
    statement — данные материализуются идентично. НЕ двойной прогон.

    Args:
        conn:          psycopg2 соединение
        sql:           реальный SQL statement (CTAS / INSERT INTO / UPDATE)
        step_name:     метка шага (step1_raw_yandex, step3_direct, ...)
        run_id:        идентификатор прогона
        log_file:      путь к файлу лога (если None — вычисляется из run_id)
        wall_sec:      wall-clock время шага из pipeline (для заголовка)
        count_table:   если задан — SELECT COUNT(*) FROM count_table после EXPLAIN
                       (получаем rowcount, т.к. EXPLAIN ANALYZE не возвращает rowcount)
        work_mem:      work_mem для сессии

    Returns:
        int если count_table задан (кол-во строк в таблице), иначе None.
        При ошибке — логирует предупреждение и возвращает None.
    """
    if not EXPLAIN_CAPTURE:
        return None

    path = log_file or _log_path(run_id)
    wall_str = f'{wall_sec:.1f}s' if wall_sec is not None else '?s'
    header = (
        f'\n{"=" * 70}\n'
        f'===== {step_name} wall={wall_str} =====\n'
        f'{"=" * 70}\n'
    )

    logger.info('explain_capture: захват плана для %s', step_name)
    t0 = time.perf_counter()

    rowcount: Optional[int] = None
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET work_mem = '{work_mem}'")
            wrapped = (
                f'EXPLAIN (ANALYZE, BUFFERS, TIMING ON, VERBOSE OFF, FORMAT TEXT)\n'
                f'{sql}'
            )
            cur.execute(wrapped)
            rows = cur.fetchall()
            plan_text = '\n'.join(r[0] for r in rows)

        elapsed = time.perf_counter() - t0

        # rowcount: EXPLAIN не даёт cur.rowcount с числом строк данных.
        # Берём через COUNT(*) если задан count_table.
        if count_table:
            with conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) FROM {count_table}')
                rowcount = cur.fetchone()[0]

        footer = f'\n--- EXPLAIN took {elapsed:.1f}s ---\n'
        if rowcount is not None:
            footer += f'--- rowcount ({count_table}): {rowcount:,} ---\n'

        with open(path, 'a', encoding='utf-8') as f:
            f.write(header)
            f.write(plan_text)
            f.write(footer)
            f.flush()

        logger.info(
            'explain_capture: план %s записан (%.1f сек)%s',
            step_name, elapsed,
            f', rowcount={rowcount:,}' if rowcount is not None else '',
        )
        return rowcount

    except Exception as e:
        logger.warning('explain_capture: не удалось снять план для %s: %s', step_name, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def log_wall_sub(
    run_id: str,
    step_name: str,
    sub_name: str,
    wall_sec: float,
    log_file: Optional[str] = None,
    extra: str = '',
) -> None:
    """
    Записать wall-clock время под-операции (для step7 utility-statement'ов).
    EXPLAIN не поддерживает SET LOGGED / CREATE INDEX / VACUUM / ANALYZE —
    поэтому для них пишем только стену.

    Args:
        run_id:    идентификатор прогона
        step_name: метка шага (step7)
        sub_name:  имя под-операции (SET LOGGED / CREATE INDEX idx_... / VACUUM / ANALYZE)
        wall_sec:  wall-clock время
        log_file:  путь к файлу лога
        extra:     доп. информация (кол-во строк, имя таблицы)
    """
    if not EXPLAIN_CAPTURE:
        return

    path = log_file or _log_path(run_id)
    line = f'[{step_name}] {sub_name}: wall={wall_sec:.3f}s'
    if extra:
        line += f'  # {extra}'
    line += '\n'

    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line)
            f.flush()
    except Exception as e:
        logger.warning('explain_capture: не удалось записать wall_sub %s/%s: %s', step_name, sub_name, e)


def write_step7_header(run_id: str, log_file: Optional[str] = None) -> None:
    """Записать заголовок блока step7 в лог (для группировки wall-clock записей)."""
    if not EXPLAIN_CAPTURE:
        return
    path = log_file or _log_path(run_id)
    header = (
        f'\n{"=" * 70}\n'
        f'===== step7 (utility: SET LOGGED / INDEX / ANALYZE / VACUUM) =====\n'
        f'  EXPLAIN не поддерживает utility-statements — только wall-clock.\n'
        f'{"=" * 70}\n'
    )
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(header)
            f.flush()
    except Exception as e:
        logger.warning('explain_capture: не удалось записать step7 header: %s', e)


# ── Backward compat (старый API, оставлен для pipeline.py inline-захватов) ────
# capture_step был в v1 — теперь не используется для реальных statement'ов.
# Оставлен как ноп чтобы не ломать возможные внешние вызовы.
def capture_step(
    conn,
    step_name: str,
    explain_sql: str,
    wall_sec: float,
    run_id: str,
    log_file: Optional[str] = None,
    work_mem: str = '512MB',
) -> None:
    """Устаревший API v1 (SELECT-эквиваленты после run). Удалён в v2.
    Оставлен как ноп для обратной совместимости."""
    pass
