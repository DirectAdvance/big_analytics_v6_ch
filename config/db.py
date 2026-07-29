"""
db.py — пулы соединений к ad_analytics (SRC) и ad_analytics_bi (DST)
"""

import logging
from typing import Optional

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

from config.settings import DB_SRC, DB_DST

logger = logging.getLogger('pipeline.db')

# ── DST pool (ad_analytics_bi — запись) ──────────────────────────────────────
_dst_pool: Optional[ThreadedConnectionPool] = None

# ── SRC pool (ad_analytics — только чтение, step0) ────────────────────────────
_src_pool: Optional[ThreadedConnectionPool] = None


def _make_pool(cfg: dict, minconn: int = 2, maxconn: int = 12) -> ThreadedConnectionPool:
    # maxconn 8→12 (2026-06-10): headroom для одновременной работы step7 (7 idx-потоков,
    # ограничены семафором до 3) + фоновых step9 prefetch_history (до ~4 conn) + verify
    # /build. 12 даёт запас без раздувания памяти PG как 16 (work_mem × backends на
    # маломощном VPS). Семафор в step7 — основной фикс PoolError; 12 — страховочный headroom.
    return ThreadedConnectionPool(
        minconn=minconn,
        maxconn=maxconn,
        host=cfg['host'],
        port=cfg['port'],
        database=cfg['database'],
        user=cfg['user'],
        password=cfg['password'],
        connect_timeout=30,
        options='-c statement_timeout=0',
        keepalives=1,
        keepalives_idle=10,
        keepalives_interval=5,
        keepalives_count=10,
    )


def _conn_is_alive(conn: psycopg2.extensions.connection) -> bool:
    """
    Проверить что соединение живо.

    ThreadedConnectionPool.getconn() возвращает соединения из кэша без проверки.
    После обрыва SSL backend-процессом (OOM-kill, PG restart) conn.closed == 0,
    но любой следующий запрос вернёт InterfaceError/OperationalError.
    Делаем лёгкий ping: SELECT 1. Если упал — соединение мёртвое.
    """
    try:
        if conn.closed:
            return False
        with conn.cursor() as cur:
            cur.execute('SELECT 1')
        return True
    except Exception:
        return False


# ── DST ───────────────────────────────────────────────────────────────────────

def init_pool() -> None:
    """Инициализировать DST-пул (ad_analytics_bi). Вызвать один раз в main()."""
    global _dst_pool
    _dst_pool = _make_pool(DB_DST)
    logger.info('DST pool инициализирован (%s/%s)', DB_DST['database'], DB_DST['host'])


def get_conn() -> psycopg2.extensions.connection:
    """
    Взять соединение из DST-пула с проверкой живости.

    Если соединение из пула мёртвое (backend убит OOM-killer, PG перезапущен,
    SSL-обрыв) — закрываем/выбрасываем его из пула и открываем свежее.
    Защищает от «отравления пула» после транзиентных обрывов в тяжёлых шагах.
    """
    if _dst_pool is None:
        raise RuntimeError('DST pool не инициализирован — вызовите init_pool()')
    conn = _dst_pool.getconn()
    if not _conn_is_alive(conn):
        logger.warning('get_conn: соединение из пула мёртвое, пересоздаём')
        _dst_pool.putconn(conn, close=True)
        conn = _dst_pool.getconn()
    return conn


def put_conn(conn: psycopg2.extensions.connection) -> None:
    """Вернуть соединение в пул. Мёртвые соединения закрываем, не возвращаем.

    # DB_POOL_UNKEYED_FIX_2026-06-17
    # Защита от PoolError("trying to put unkeyed connection"):
    # Возникает когда параллельный поток уже вычистил conn из _rused через
    # putconn(close=True) (get_conn живость-проверка) пока текущий поток держал
    # тот же Python-объект. _putconn не находит id(conn) в _rused → PoolError.
    # Фикс: перехватываем PoolError в ветке else и тихо закрываем conn —
    # не позволяем PoolError всплыть из finally _build_one_table и убить шаг.
    """
    if _dst_pool is None:
        return
    if conn.closed:
        # Не возвращаем closed conn в пул — это отравит пул
        try:
            _dst_pool.putconn(conn, close=True)
        except Exception:
            pass
    else:
        try:
            _dst_pool.putconn(conn)
        except Exception:
            # PoolError("trying to put unkeyed connection") — conn уже вычищен
            # из пула другим потоком; тихо закрываем чтобы не отравлять пул.
            logger.warning('put_conn: PoolError при возврате соединения, закрываем')
            try:
                conn.close()
            except Exception:
                pass


def close_pool() -> None:
    if _dst_pool is not None:
        _dst_pool.closeall()
        logger.info('DST pool закрыт')


# ── SRC ───────────────────────────────────────────────────────────────────────

def init_src_pool() -> None:
    """Инициализировать SRC-пул (ad_analytics, только чтение). Вызвать в main()."""
    global _src_pool
    _src_pool = _make_pool(DB_SRC, minconn=1, maxconn=3)
    logger.info('SRC pool инициализирован (%s/%s)', DB_SRC['database'], DB_SRC['host'])


def get_src_conn() -> psycopg2.extensions.connection:
    """Взять соединение из SRC-пула с проверкой живости."""
    if _src_pool is None:
        raise RuntimeError('SRC pool не инициализирован — вызовите init_src_pool()')
    conn = _src_pool.getconn()
    if not _conn_is_alive(conn):
        logger.warning('get_src_conn: соединение из пула мёртвое, пересоздаём')
        _src_pool.putconn(conn, close=True)
        conn = _src_pool.getconn()
    return conn


def put_src_conn(conn: psycopg2.extensions.connection) -> None:
    """Вернуть соединение в SRC-пул. Мёртвые соединения закрываем."""
    if _src_pool is None:
        return
    if conn.closed:
        try:
            _src_pool.putconn(conn, close=True)
        except Exception:
            pass
    else:
        try:
            _src_pool.putconn(conn)
        except Exception:
            # DB_POOL_UNKEYED_FIX_2026-06-17: аналогично put_conn
            logger.warning('put_src_conn: PoolError при возврате соединения, закрываем')
            try:
                conn.close()
            except Exception:
                pass


def close_src_pool() -> None:
    if _src_pool is not None:
        _src_pool.closeall()
        logger.info('SRC pool закрыт')
