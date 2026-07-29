#!/usr/bin/env python3
"""pipeline_mutex.py — PIPELINE_MUTEX_2026-07-12

Взаимное исключение тяжёлых прогонов big_analytics_v5 на Victory.

Проблема: pipeline_powerbi.py / fast_pipeline.py / build_spend_daily.py и job
report_placement (step_cron_night/report_placement/run.py) при параллельном запуске
делают DROP+CTAS / TRUNCATE+INSERT ОДНИХ И ТЕХ ЖЕ таблиц
(fact_*_spend, fact_*_zayavki, big_analytics_*, analytics_report_placement) →
гонки, полу-собранные витрины, ENOSPC от удвоенного temp-спилла.

Решение: НЕблокирующий файловый flock по ОБЩЕМУ пути. Все четыре процесса живут на
одном хосте (Victory), поэтому flock (advisory, per-inode) даёт корректное взаимное
исключение между ними. Если лок держит другой процесс из группы — вызывающий
получает PipelineBusy и должен выйти рано (skip), а не висеть.

Почему flock, а не pg_advisory_lock:
  • flock авто-освобождается ОС при завершении процесса (в т.ч. при SIGKILL/OOM/crash)
    → устаревших локов не остаётся, не нужен «живой» держащий connection к БД;
  • не требует БД-соединения до самого решения «стартовать ли»;
  • report_placement/run.py — тонкий запускатор (step1→step2 subprocess'ами), ему
    вообще не нужен psycopg2 для взаимного исключения.

Использование (в начале main / __main__):

    import pipeline_mutex
    try:
        _lock_fd = pipeline_mutex.acquire('pipeline_powerbi')  # держим fd весь прогон
    except pipeline_mutex.PipelineBusy as busy:
        logger.warning('PIPELINE_MUTEX: активен другой прогон группы (%s) — выходим', busy)
        sys.exit(0)

fd НЕ закрываем вручную: ОС снимет flock при выходе процесса. Ссылку на fd держим
в переменной, чтобы явно показать намерение «лок жив весь прогон».
"""

from __future__ import annotations

import fcntl
import logging
import os

logger = logging.getLogger(__name__)

# Общий путь лок-файла для всех четырёх job. /tmp — локальный, переживает между
# запусками (сам файл-маркер лёгкий, освобождается сама блокировка, а не файл).
LOCK_PATH = os.environ.get('BIG_ANALYTICS_LOCK_PATH', '/tmp/big_analytics_v5_pipeline.lock')


class PipelineBusy(Exception):
    """Лок занят другим прогоном из группы взаимного исключения.

    Строковое представление — содержимое лок-файла (имя+PID держателя) для диагностики.
    """


def acquire(name: str, lock_path: str = LOCK_PATH) -> int:
    """Неблокирующе взять общий flock группы пайплайнов.

    Возвращает открытый fd (держать его до конца процесса — ОС снимет lock при выходе).
    Бросает PipelineBusy(держатель), если лок занят другим процессом группы.

    fail-open по инфраструктуре: если сам механизм flock недоступен (не-POSIX FS,
    ошибка open) — НЕ блокируем прогон, а логируем WARNING и возвращаем -1
    (лучше редкая гонка, чем полностью не стартующий пайплайн из-за проблем с /tmp).
    """
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        logger.warning('PIPELINE_MUTEX: не удалось открыть лок-файл %s (%s) — '
                       'продолжаем без взаимного исключения (fail-open)', lock_path, e)
        return -1

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        # Лок держит другой процесс группы — читаем маркер держателя и отдаём PipelineBusy.
        holder = ''
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            holder = os.read(fd, 256).decode('utf-8', 'replace').strip()
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        raise PipelineBusy(holder or 'неизвестный процесс группы')

    # Лок взят — пишем в файл имя+PID держателя (для диагностики следующего, кто упрётся).
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f'{name} pid={os.getpid()}'.encode('utf-8'))
        os.fsync(fd)
    except OSError:
        pass  # запись маркера не критична — сам lock уже наш

    logger.info('PIPELINE_MUTEX: лок группы взят (%s pid=%d, %s)', name, os.getpid(), lock_path)
    return fd
