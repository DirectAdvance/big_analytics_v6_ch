"""
step13_arrival/build_unified.py — сборка big_analytics_unified (ШАГ 3 MIRROR+UNION)

big_analytics_unified = ФИЗИЧЕСКАЯ таблица (CTAS, НЕ view):

    SELECT *, 'По дате заявки'::text AS атрибуция FROM big_analytics_full
    UNION ALL
    SELECT <align cols>, 'По дате визита'         FROM big_analytics_full_arrival

Обе таблицы имеют ИДЕНТИЧНУЮ 73-колоночную схему (BFA доведена до зеркала в step13),
поэтому UNION ALL тривиален: `SELECT *` с обеих сторон + константа атрибуции.

Колонка `атрибуция` ('По дате заявки' | 'По дате визита') — единственное отличие от
исходных таблиц. PBI на ШАГЕ 4 репойнтит партицию модели big_analytics_full на эту
таблицу (имя модельной таблицы не меняется → визуалы целы), а срез делает по `атрибуция`.

ИДЕМПОТЕНТНОСТЬ: DROP TABLE IF EXISTS + CREATE TABLE AS — повторный прогон пайплайна
полностью пересоздаёт unified из текущих BAF+BFA. Никаких накопительных эффектов.

ПОРЯДОК В ПАЙПЛАЙНЕ: вызывается ПОСЛЕ step13 (BFA готова) и ПОСЛЕ normalize_salons /
cleanup_old_dates (BAF и BFA финализированы и нормализованы), чтобы unified содержал
итоговые данные.

Индексы: (атрибуция), (Date), (салон), (направление), (специалист).

Выход: big_analytics_unified  (≈ 3.62M[BAF] + 34.8k[BFA] строк).
"""

import logging
import time

from config.settings import T_FULL, T_FULL_ARRIVAL, WORK_MEM

logger = logging.getLogger('pipeline.build_unified')

T_UNIFIED = 'big_analytics_unified'


def _build_unified_sql() -> str:
    """
    UNION ALL двух 73-колоночных таблиц + константа `атрибуция`.

    Используем явный список колонок (а не SELECT *), чтобы порядок UNION ALL был
    гарантированно одинаковым с обеих сторон даже если когда-нибудь схемы разойдутся
    по физическому ordinal_position (CTAS фиксирует порядок из SELECT-листа).
    Колонки берём из information_schema в run() и собираем список динамически —
    это самозащита от рассинхрона схем (если BFA != BAF по колонкам → ошибка явная).
    """
    raise NotImplementedError  # SQL строится динамически в run() по факт. схеме BAF


def _columns(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def run(conn, run_id: str = '') -> dict:
    """
    Полный пересчёт big_analytics_unified (CTAS). Идемпотентно.
    Возвращает {'rows': N, 'details': '...'}.
    """
    t0 = time.perf_counter()

    baf_cols = _columns(conn, T_FULL)
    bfa_cols = _columns(conn, T_FULL_ARRIVAL)

    # Защита: схемы обязаны совпадать (BFA = зеркало BAF). Иначе UNION даст мусор/упадёт.
    if baf_cols != bfa_cols:
        only_baf = [c for c in baf_cols if c not in bfa_cols]
        only_bfa = [c for c in bfa_cols if c not in baf_cols]
        raise RuntimeError(
            f'build_unified: схемы big_analytics_full ({len(baf_cols)} кол.) и '
            f'big_analytics_full_arrival ({len(bfa_cols)} кол.) расходятся. '
            f'Только в BAF: {only_baf}; только в BFA: {only_bfa}; '
            f'порядок: {"совпадает" if set(baf_cols)==set(bfa_cols) else "разный набор"}. '
            f'Сначала довести BFA до зеркала (step13).'
        )

    # EMPTY_STRING_TO_NULL_DIMS_2026_06_09 — унификация «двойного пустого» в слайсерах PBI.
    # Эти dimension-колонки в обеих источниках (BAF и BFA) могли нести И NULL, И '' одновременно
    # (PBI показывал «(Пусто)» + отдельный безымянный пустой пункт). Источники '': step3/step6
    # placeholder ''::TEXT (не-direct ветки) + пустые ячейки local_gsheet_sites через LEFT JOIN +
    # step13 SPLIT_PART (cpc_cpa/site_quiz, как tp). '' семантически = «пусто» (corrections.py уже
    # применяет NULLIF к шаблон/салон/город/регион/специалист) — приводим к единому стандарту NULL.
    # Единая точка нормализации: финальная проекция unified (источник всех слайсеров PBI). NULLIF
    # на колонке без '' — безопасный no-op, поэтому покрываем все подтверждённые dual-empty.
    _NULLIF_EMPTY_DIMS = {
        'марки авто', 'специалист', 'шаблон', 'account_login', 'domain',
        'салон', 'город', 'регион', 'cpc_cpa', 'site_quiz',
    }

    # ─── TP_DIRECTION_LABEL_2026_06_10 (Задача 3) ──────────────────────────────
    # Ярлык по направлению для ПУСТОГО tp — единая точка для ОБЕИХ сторон атрибуции.
    # build_unified = единственное место, где BAF (claim/«По дате заявки») и BFA
    # (visit/«По дате визита») проходят через ОДИН _col_expr с доступом к колонке
    # «направление» → ярлык согласован на обеих сторонах автоматически.
    #
    # ПОРЯДОК: build_unified идёт ПОСЛЕ step13 (включая повторную пересборку после
    # step11 в fast_pipeline) и ПОСЛЕ corrections/normalize → fallback-парсинг кодов
    # (step13 leads_eff, Задача 2 ARRIVAL_CODE_NULL) уже отработал, реальные tp стоят.
    # COALESCE(NULLIF(tp,''), <ярлык>) → валидный tp НИКОГДА не затирается, ярлык
    # падает ТОЛЬКО в пустую дырку.
    #
    # СКОУП ярлыка (строго по согласованному маппингу):
    #   посевы            → 'Посевы'
    #   отзывы            → 'отзывы'
    #   SEO               → 'SEO'
    #   SEO Flow          → 'SEO Flow'
    #   пиксель           → 'пиксель'
    #   пиксель_атрибуц   → 'пиксель_атрибуц'
    # НЕ метятся (остаются пустыми / как есть):
    #   - направление='Контекст' (direct_zero/«Контекст без кампании» + visit-direct
    #     без CampaignId) — остаётся ПУСТЫМ (Контекста НЕТ в маппинге);
    #   - звонки (направление='Контекст', _source_table='calls') — tp уже = 'звонки'
    #     (не пусто) → COALESCE не трогает, остаётся 'звонки' как сейчас;
    #   - существующие литералы 'звонки' / 'неверный кодер' — не пусты → не трогаются.
    # ТОЛЬКО tp (текстовое измерение). Меры (cost/priezd/prodazhi, дробная пиксельная
    # атрибуция) НЕ трогаются → golden (расход/продажи) Δ0, дробность сохранена.
    _TP_DIRECTION_LABELS = {
        'посевы': 'Посевы',
        'отзывы': 'отзывы',
        'SEO': 'SEO',
        'SEO Flow': 'SEO Flow',
        'пиксель': 'пиксель',
        'пиксель_атрибуц': 'пиксель_атрибуц',
    }
    _tp_label_when = '\n            '.join(
        f"WHEN \"направление\" = '{src}' THEN '{lbl}'"
        for src, lbl in _TP_DIRECTION_LABELS.items()
    )
    _TP_LABEL_EXPR = (
        'COALESCE(NULLIF("tp", \'\'), CASE\n            '
        f'{_tp_label_when}\n            '
        'ELSE NULL\n        END) AS "tp"'
    )

    def _col_expr(c: str) -> str:
        if c == 'tp':
            return _TP_LABEL_EXPR
        if c in _NULLIF_EMPTY_DIMS:
            return f'NULLIF("{c}", \'\') AS "{c}"'
        return f'"{c}"'

    col_list = ', '.join(_col_expr(c) for c in baf_cols)

    sql = f"""
        SELECT {col_list}, 'По дате заявки'::text AS "атрибуция"
        FROM {T_FULL}
        UNION ALL
        SELECT {col_list}, 'По дате визита'::text AS "атрибуция"
        FROM {T_FULL_ARRIVAL}
    """

    # DISKFREE_DROP_FIRST_2026-06-27: DROP TABLE в autocommit ДО CTAS.
    # Освобождает ~5.7 GB сразу (OS возвращает место без flush CTAS-транзакции).
    # Аналог паттерна spend-builders. Если диска < 12 GB после DROP — abort с понятной ошибкой.
    # BAF/BFA целы → recovery = python3 build_unified.py → build_star.py
    import shutil as _shu_uni
    _MIN_FREE_GB_UNI = 12.0
    _free_pre_uni = _shu_uni.disk_usage('/').free / 1024 ** 3
    logger.info('build_unified DISK_GUARD: свободно %.1f GB перед DROP', _free_pre_uni)
    if _free_pre_uni < 4.0:
        raise RuntimeError(
            f'build_unified DISK_GUARD: {_free_pre_uni:.1f} GB ДО DROP — '
            f'диск критически мал, освободите место вручную'
        )
    conn.rollback()   # убедиться, что нет незакрытой транзакции перед autocommit
    conn.autocommit = True
    with conn.cursor() as _drop_cur:
        logger.info('build_unified DISKFREE_DROP_FIRST: DROP TABLE IF EXISTS %s (autocommit)', T_UNIFIED)
        _drop_cur.execute(f'DROP TABLE IF EXISTS {T_UNIFIED}')
    conn.autocommit = False
    _free_after_uni = _shu_uni.disk_usage('/').free / 1024 ** 3
    logger.info('build_unified DISK_GUARD: %.1f GB после DROP (Δ+%.1f GB)',
                _free_after_uni, _free_after_uni - _free_pre_uni)
    if _free_after_uni < _MIN_FREE_GB_UNI:
        raise RuntimeError(
            f'build_unified DISK_GUARD: {_free_after_uni:.1f} GB < {_MIN_FREE_GB_UNI} GB — '
            f'недостаточно для CTAS big_analytics_unified (~6 GB). '
            f'Старый unified дропнут; BAF/BFA целы. '
            f'Освободите место и запустите: build_unified → build_star'
        )

    with conn.cursor() as cur:
        logger.info('build_unified: SET work_mem = %s', WORK_MEM)
        cur.execute(f"SET work_mem = '{WORK_MEM}'")

        logger.info('build_unified: CREATE %s (CTAS, %d кол. + атрибуция)',
                    T_UNIFIED, len(baf_cols))
        cur.execute(f'CREATE TABLE {T_UNIFIED} AS {sql}')
        rows = cur.rowcount
        conn.commit()

    logger.info('build_unified: вставлено %d строк', rows)

    # ── LZ4_DIRECT_FULL_UNIFIED_2026-06-17: lz4-сжатие TEXT-колонок big_analytics_unified ──
    # T_UNIFIED — CTAS из big_analytics_full ∪ big_analytics_full_arrival + колонка атрибуция.
    # Схема наследуется от big_analytics_full (baf_cols), плюс 'атрибуция' TEXT.
    # lz4 навешивается после CREATE TABLE AS — данные уже записаны, сжатие активно с этого прогона
    # (TOAST читает/пишет через атрибут). Downstream (build_star) только SELECT — lz4 прозрачен.
    # Ожидаемый эффект: unified ~5.8 GB → ~2 GB.
    _TEXT_COLS_UNIFIED = [
        'key3', '"День недели"', '"CampaignName"', '"AdGroupName"',
        '"AdNetworkType"', '"Device"', 'domain',
        '"RlAdjustmentId_total"', 'campaign_code', 'tp', 'cpc_cpa', 'site_quiz',
        'adgroup_code', 'account_login', 'manager_login',
        'ag_part1', 'ag_part2', 'ag_part3', 'ag_part4',
        'ag_part5', 'ag_part6', 'ag_part7',
        '"марки авто"', '"Название crm"', 'тип_заявки',
        '"статус"', '"специалист"', '"тип_сайта"', '"шаблон"',
        '"салон"', '"город"', '"регион"', 'direction',
        '"неверный_кодер_new"', 'fid', 'проджект', 'id_салона', 'менеджер',
        'источник', 'направление', 'key_pixel_score',
        '"номер кампании | название кампании"',
        '"номер группы | название группы"',
        '"аккаунт|сайт"', 'поставщик', '_source_table',
        'campaign_status', 'payment_model',
        '"атрибуция"',  # колонка добавлена build_unified (По дате заявки / По дате визита)
    ]
    try:
        with conn.cursor() as _lz4_cur:
            for _col in _TEXT_COLS_UNIFIED:
                _lz4_cur.execute(
                    f'ALTER TABLE {T_UNIFIED} '
                    f'ALTER COLUMN {_col} SET COMPRESSION lz4'
                )
        conn.commit()
        logger.info('build_unified: lz4 навешено на %d TEXT-колонок %s',
                    len(_TEXT_COLS_UNIFIED), T_UNIFIED)
    except Exception as _lz4_err:
        logger.warning('build_unified: не удалось навесить lz4 на %s: %s (продолжаем)',
                       T_UNIFIED, _lz4_err)
        try:
            conn.rollback()
        except Exception:
            pass

    # ── CRM_NAME_EXCEL_MAP_2026-07-10: финальная зачистка технических _excel в "Название crm" ──
    # Человекочитаемая колонка "Название crm" на осях direct/calls местами несла raw-идентификаторы
    # CRM (mauto_excel/genzes_excel/redauto_excel/plex_excel): заявка-ось чинится в step3/step6
    # domain_source_type, но ВИЗИТ-ось (step13 leads_scored/calls_scored) в CASE отдаёт source_type
    # через ELSE → raw утекал на визите. Единый финальный маппинг в unified (BAF ∪ BFA, самая поздняя
    # точка ПОСЛЕ всех доливок обеих осей) → гарантия: fact_big_analytics не содержит raw _excel ни на
    # одной оси. Написание единообразно step6 4b / step3 (МаАвто/Генезис/Ред Авто/Плекс).
    # ВАЖНО: правим ТОЛЬКО человекочитаемую "Название crm"; технический _source_table (значения
    # 'mauto_excel' и т.п. как ИДЕНТИФИКАТОР источника) НЕ трогаем.
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_UNIFIED}
            SET "Название crm" = CASE "Название crm"
                WHEN 'mauto_excel'   THEN 'МаАвто'
                WHEN 'genzes_excel'  THEN 'Генезис'
                WHEN 'redauto_excel' THEN 'Ред Авто'
                WHEN 'plex_excel'    THEN 'Плекс'
                ELSE "Название crm"
            END
            WHERE "Название crm" IN ('mauto_excel', 'genzes_excel', 'redauto_excel', 'plex_excel')
        """)
        n_crm_excel = cur.rowcount
    conn.commit()
    logger.info('build_unified: raw _excel в "Название crm" → рус: %d строк', n_crm_excel)

    # ── CRM_NAME_MAPPING_2026-07-10: финальная заливка "Название crm" NULL/пустая → 'Не указана' ──
    # Перенесено из step6 (block 4c): step6 отрабатывает ДО доливок load_reviews/load_crop/step11,
    # их пустые "Название crm" обходили ту заливку. big_analytics_unified = BAF ∪ BFA — самая поздняя
    # точка консолидации ПОСЛЕ всех доливок обеих осей. Гарантия: fact_big_analytics (проекция unified)
    # не содержит ни одной пустой "Название crm".
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_UNIFIED}
            SET "Название crm" = 'Не указана'
            WHERE COALESCE(TRIM("Название crm"), '') = ''
        """)
        n_crm_null = cur.rowcount
    conn.commit()
    logger.info('build_unified: CRM NULL/пустая → Не указана: %d строк', n_crm_null)

    _create_indexes(conn)

    with conn.cursor() as cur:
        cur.execute(f'ANALYZE {T_UNIFIED}')
    conn.commit()

    elapsed = time.perf_counter() - t0
    details = (
        f'rows={rows}; big_analytics_unified = big_analytics_full ∪ big_analytics_full_arrival '
        f'+ атрибуция (По дате заявки / По дате визита); {len(baf_cols)+1} колонок'
    )
    logger.info('build_unified: готово за %.1f сек, %d строк', elapsed, rows)
    return {'rows': rows, 'details': details}


def get_explain_sql(conn) -> str:
    """SELECT-эквивалент для EXPLAIN ANALYZE тяжёлого UNION ALL build_unified.

    Используется explain_capture при EXPLAIN_CAPTURE=1. Таблица
    big_analytics_unified уже создана к моменту вызова (после run()).
    Golden-запрос Кудерко — ключевой read-path над unified.
    """
    return f"""
        SELECT
            "атрибуция",
            "специалист",
            COUNT(*)            AS rows,
            SUM(total_cost)     AS total_cost,
            SUM(prodazhi)       AS prodazhi,
            SUM(priezd)         AS priezd
        FROM {T_UNIFIED}
        WHERE "Date" >= '2026-01-01'
        GROUP BY "атрибуция", "специалист"
        ORDER BY "атрибуция", total_cost DESC NULLS LAST
    """


def _create_indexes(conn) -> None:
    indexes = [
        f'CREATE INDEX IF NOT EXISTS idx_unified_attr   ON {T_UNIFIED} ("атрибуция")',
        f'CREATE INDEX IF NOT EXISTS idx_unified_date   ON {T_UNIFIED} ("Date")',
        f'CREATE INDEX IF NOT EXISTS idx_unified_salon  ON {T_UNIFIED} ("салон")',
        f'CREATE INDEX IF NOT EXISTS idx_unified_naprav ON {T_UNIFIED} ("направление")',
        f'CREATE INDEX IF NOT EXISTS idx_unified_spec   ON {T_UNIFIED} ("специалист")',
    ]
    with conn.cursor() as cur:
        for sql in indexes:
            try:
                cur.execute(sql)
            except Exception as e:
                logger.warning('Индекс не создан: %s — %s', sql, e)
                conn.rollback()
                continue
    conn.commit()
    logger.info('build_unified: индексы созданы')
