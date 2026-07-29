"""
spend/build_spend_staging.py — единый проход FDW → UNLOGGED staging.

FDW_SINGLE_PASS_STAGING_2026-06-17

ПРОБЛЕМА (до фикса E):
  Каждый из трёх спенд-билдеров (region / adformat / criterion) делал свой
  независимый DROP+CTAS с полным сканом + сортировкой FDW yandex_direct_manager_reports
  (18.9M строк). Итого FDW читался 3 раза — дорого по времени и временному месту.

РЕШЕНИЕ — единый проход:
  1. Один скан FDW → UNLOGGED staging-таблица _spend_staging_tmp по детальной грани,
     включающей все три оси одновременно:
       date × campaign_id × ad_group_id × ad_network_type
       × id_location × ad_format × criterion_id × criterion (ОЧИЩЕННЫЙ)
     + все общие метрики (total_cost/clicks/impressions/crm_*) + кодер кампании + account_login
     + LEFT JOIN local_gsheet_sites (DISTINCT ON login_key)
     + LEFT JOIN local_gsheet_yandex_direct_id_location (DISTINCT ON id_location)
  2. Три роллапа GROUP BY из staging (НЕ из FDW) → fact_region_spend /
     fact_adformat_spend / fact_criterion_spend — те же колонки, те же row_hash/PK.
  3. В конце DROP staging (явный вызов drop_staging()).

ОРКЕСТРАЦИЯ:
  - ensure_staging(conn) всегда дропает старую _spend_staging_tmp и создаёт свежую.
    Staging нельзя переиспользовать между прогонами: это transient scratch для одного
    запуска, иначе можно собрать устаревший срез spend-витрин.
    SET LOCAL work_mem/temp_file_limit применяются ОДИН РАЗ — при создании.
  - drop_staging(conn) вызывается оркестратором в finally после последовательной
    spend-фазы.
  - Если staging уже существует при вызове ensure_staging (например, прогон упал и
    не очистил), она всегда дропается и пересоздаётся (guard STALE).

ИНВАРИАНТЫ:
  - Один столбец criterion в staging = ОЧИЩЕННЫЙ текст (_CRIT_CLEAN из criterion_spend).
    GROUP BY criterion_id + criterion в роллапе criterion даёт те же строки, что
    оригинальный билдер (включая коллизии сырых текстов с одним id → деdup при очистке).
  - ad_format в staging = МАППИРОВАННЫЙ в русские названия (тот же CASE, что adformat-билдер).
    GROUP BY ad_format в роллапе даёт те же строки.
  - id_location в staging = m."LocationOfPresenceId" (как region-билдер).
  - Справочные обогащения (gs.* / d.*) вычисляются в staging один раз — все три роллапа
    берут их без повторного JOIN.
  - keys_with_both=0: адрес факта (row_hash) каждого роллапа зависит только от своей
    оси — staging не вносит двойной учёт, он лишь источник данных роллапов.

СОВМЕСТИМОСТЬ SET LOCAL:
  - SPEND_WORKMEM_2026-06-17 и TEMP_FILE_LIMIT_CAP_2026-06-17 (фиксы C/F) сохранены:
    SET LOCAL выполняется в ensure_staging(), в той же транзакции что и CTAS staging.
    Роллапы (тоже в транзакции) наследуют эти SET LOCAL автоматически (они действуют
    до конца транзакции или до явного SET LOCAL обратного значения).
"""

import logging
import time

from config.settings import DATE_FROM

logger = logging.getLogger('pipeline.spend_staging')

# Имя UNLOGGED staging-таблицы (в schema public, prefixed _ чтобы отличать от продовых).
STAGING_TABLE = '_spend_staging_tmp'

# ── Нормализация CampaignName и кодер (1:1 как в трёх билдерах и step1.py) ─────
# REPLACE(chr(1089),'c') — кириллическая 'с'(U+0441) → латинская 'c'.
_CN_NORM = "REPLACE(m.\"CampaignName\", chr(1089), 'c')"
_CODE_RE  = "'(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'"
_CAMPAIGN_CODE_EXPR = (
    f"REPLACE(REPLACE((REGEXP_MATCH({_CN_NORM}, {_CODE_RE}))[1], 'kviz','quiz'),'Kviz','Quiz')"
)
_TP_EXPR = (
    f"LOWER(SPLIT_PART(COALESCE((REGEXP_MATCH({_CN_NORM}, {_CODE_RE}))[1], ''), '_', 1))"
)
_CPC_CPA_EXPR = (
    f"LOWER(SPLIT_PART(COALESCE((REGEXP_MATCH({_CN_NORM}, {_CODE_RE}))[1], ''), '_', 2))"
)
_SITE_QUIZ_EXPR = (
    f"REPLACE(LOWER(SPLIT_PART(COALESCE((REGEXP_MATCH({_CN_NORM}, {_CODE_RE}))[1], ''), '_', 3)),"
    f" 'kviz','quiz')"
)

# ── Нормализация AdFormat → русские названия (1:1 как build_adformat_spend) ────
_ADFORMAT_CASE = """
    CASE m."AdFormat"
        WHEN 'IMAGE'          THEN 'графический'
        WHEN 'TEXT'           THEN 'текстовый (ТГО)'
        WHEN 'VIDEO'          THEN 'видео'
        WHEN 'SMART_SINGLE'   THEN 'смарт-баннер'
        WHEN 'SMART_MULTIPLE' THEN 'смарт-баннер'
        WHEN 'SMART_TILE'     THEN 'смарт-баннер'
        WHEN 'ADAPTIVE_IMAGE' THEN 'адаптивный графический'
        WHEN 'multicard'      THEN 'комбинаторное объявление'
        ELSE m."AdFormat"
    END
"""

# ── Нормализация Criterion → очищенный текст (1:1 как build_criterion_spend) ───
# Порядок шагов критичен: (0) NBSP → (1) ведущие дефисы → (2) минус-слова → (3) операторы → (4) trim.
_CRIT_CLEAN = (
    r"""trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace("""
    r"""translate(m."Criterion", chr(160)||chr(8239)||chr(8201), '   '), """
    r"""'^-+', ''), '\s+-.*$', ''), '[!+\[\]]', '', 'g'), '\s+', ' ', 'g'))"""
)

# ── DDL staging (UNLOGGED — нет WAL, быстро создаётся и дропается) ─────────────
_DDL_STAGING = f"""
CREATE UNLOGGED TABLE public.{STAGING_TABLE} (
    -- ось даты + кампании + группы + сети (общая для всех трёх роллапов)
    date                DATE        NOT NULL,
    campaign_id         BIGINT      NOT NULL,
    campaign_name       TEXT,
    ad_group_id         BIGINT      NOT NULL,
    ad_group_name       TEXT,
    ad_network_type     TEXT,
    -- ось 1: регион показа (для роллапа region_spend)
    id_location         BIGINT,
    -- ось 2: формат объявления — уже в русских названиях (для роллапа adformat_spend)
    ad_format           TEXT,
    -- ось 3: критерий — criterion_id + ОЧИЩЕННЫЙ criterion + сырой (для роллапа criterion_spend)
    criterion_id        BIGINT,
    criterion           TEXT,
    criterion_raw       TEXT,
    -- метрики (агрегированы по детальной грани всех трёх осей)
    impressions         BIGINT,
    clicks              BIGINT,
    cost                NUMERIC,
    all_forms           NUMERIC,
    crm_order_created   NUMERIC,
    crm_order_paid      NUMERIC,
    crm_spam_order      NUMERIC,
    crm_order_canceled  NUMERIC,
    -- кодер кампании (1:1 из step1.py)
    campaign_code       TEXT,
    tp                  TEXT,
    cpc_cpa             TEXT,
    site_quiz           TEXT,
    adgroup_code        TEXT,
    -- account_login (нужен для JOIN к gsheet_sites)
    account_login       TEXT,
    -- обогащение из local_gsheet_sites (DISTINCT ON login_key — выполнено при CTAS)
    domain              TEXT,
    directologist       TEXT,
    city                TEXT,
    region              TEXT,
    salon               TEXT,
    template            TEXT,
    site_type           TEXT,
    status              TEXT,
    direction           TEXT,
    project_manager     TEXT,
    client_id           TEXT,
    -- обогащение из local_gsheet_yandex_direct_id_location (для роллапа region_spend)
    location            TEXT,
    oblast              TEXT,
    geo_region_type     TEXT,
    distance_km         INTEGER,
    distance_km_agreg   INTEGER
)
"""

# Индексы на staging намеренно НЕ строим.
# Роллапы читают всю staging-таблицу и группируют весь объём; btree-индексы на staging
# в таком сценарии дают мало пользы, но занимают гигабайты и увеличивают пик места.
_STAGING_INDEXES = []

# ── SQL для CTAS staging (единый скан FDW) ─────────────────────────────────────
_STAGING_CTAS_SQL = f"""
WITH raw AS (
    SELECT
        m."Date"::date                              AS date,
        m."CampaignId"                              AS campaign_id,
        MAX(m."CampaignName")                       AS campaign_name,
        m."AdGroupId"                               AS ad_group_id,
        MIN(m."AdGroupName")                        AS ad_group_name,
        m."AdNetworkType"                           AS ad_network_type,
        -- ось 1: регион показа (как region_spend)
        m."LocationOfPresenceId"                    AS id_location,
        -- ось 2: формат объявления — русские названия (как adformat_spend)
        {_ADFORMAT_CASE}                            AS ad_format,
        -- ось 3: критерий (как criterion_spend)
        m."CriterionId"                             AS criterion_id,
        {_CRIT_CLEAN}                               AS criterion,
        MAX(m."Criterion")                          AS criterion_raw,
        -- account_login для JOIN к gsheet_sites
        MAX(LOWER(TRIM(m.account_login)))           AS account_login,
        -- метрики
        SUM(m."Impressions")                        AS impressions,
        SUM(m."Clicks")                             AS clicks,
        SUM(m.total_cost)::numeric                  AS cost,
        SUM(m.all_forms)                            AS all_forms,
        SUM(m.crm_order_created)                    AS crm_order_created,
        SUM(m.crm_order_paid)                       AS crm_order_paid,
        SUM(m.crm_spam_order)                       AS crm_spam_order,
        SUM(m.crm_order_canceled)                   AS crm_order_canceled,
        -- кодер кампании
        MAX({_CAMPAIGN_CODE_EXPR})                  AS campaign_code,
        MAX({_TP_EXPR})                             AS tp,
        MAX({_CPC_CPA_EXPR})                        AS cpc_cpa,
        MAX({_SITE_QUIZ_EXPR})                      AS site_quiz,
        (REGEXP_MATCH(
            SPLIT_PART(MIN(m."AdGroupName"), ' — ', 1),
            '(ct\\d+_(?:aoff|aon)_n\\d+_r\\d+_ct\\d+_ag\\d+_g\\d+)'
        ))[1]                                       AS adgroup_code
    FROM public.yandex_direct_manager_reports m
    WHERE m."Date" IS NOT NULL
      AND m."Date" >= '{DATE_FROM}'
    GROUP BY
        m."Date"::date,
        m."CampaignId",
        m."AdGroupId",
        m."AdNetworkType",
        -- ось 1
        m."LocationOfPresenceId",
        -- ось 2: маппинг повторяется в GROUP BY (PostgreSQL требует совпадения выражения)
        {_ADFORMAT_CASE},
        -- ось 3: criterion_id + ОЧИЩЕННЫЙ текст (как вариант B в criterion_spend)
        m."CriterionId",
        {_CRIT_CLEAN}
)
INSERT INTO public.{STAGING_TABLE} (
    date, campaign_id, campaign_name, ad_group_id, ad_group_name, ad_network_type,
    id_location, ad_format, criterion_id, criterion, criterion_raw,
    impressions, clicks, cost, all_forms,
    crm_order_created, crm_order_paid, crm_spam_order, crm_order_canceled,
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code, account_login,
    domain, directologist, city, region, salon, template, site_type, status, direction,
    project_manager, client_id,
    location, oblast, geo_region_type, distance_km, distance_km_agreg
)
SELECT
    r.date,
    r.campaign_id,
    r.campaign_name,
    r.ad_group_id,
    r.ad_group_name,
    r.ad_network_type,
    r.id_location,
    r.ad_format,
    r.criterion_id,
    r.criterion,
    r.criterion_raw,
    r.impressions,
    r.clicks,
    r.cost,
    r.all_forms,
    r.crm_order_created,
    r.crm_order_paid,
    r.crm_spam_order,
    r.crm_order_canceled,
    r.campaign_code,
    r.tp,
    r.cpc_cpa,
    r.site_quiz,
    r.adgroup_code,
    r.account_login,
    -- обогащение из local_gsheet_sites (DISTINCT ON login_key, tie-break row_hash)
    gs."domain"                                     AS domain,
    gs."directologist"                              AS directologist,
    gs."city"                                       AS city,
    gs."region"                                     AS region,
    gs."salon"                                      AS salon,
    gs."template"                                   AS template,
    gs."site_type"                                  AS site_type,
    gs."status"                                     AS status,
    gs."direction"                                  AS direction,
    NULLIF(TRIM(gs."project_manager"), '')          AS project_manager,
    gs."client_id"                                  AS client_id,
    -- обогащение локации (DISTINCT ON id_location, tie-break location)
    d.location                                      AS location,
    d."Область"                                     AS oblast,
    d."GeoRegionType"                               AS geo_region_type,
    d.distance_km                                   AS distance_km,
    d.distance_km_agreg                             AS distance_km_agreg
FROM raw r
LEFT JOIN (
    SELECT DISTINCT ON (login_key)
        login_key, domain, directologist, city, region, salon, template,
        site_type, status, direction, project_manager, client_id
    FROM public.local_gsheet_sites
    ORDER BY login_key, row_hash
) gs ON r.account_login = gs.login_key
LEFT JOIN (
    SELECT DISTINCT ON (id_location)
        id_location, location, "Область", "GeoRegionType", distance_km, distance_km_agreg
    FROM public.local_gsheet_yandex_direct_id_location
    ORDER BY id_location, location
) d ON r.id_location = d.id_location
"""


def ensure_staging(conn) -> int:
    """
    FDW_SINGLE_PASS_STAGING_2026-06-17

    Идемпотентно создаёт UNLOGGED staging-таблицу _spend_staging_tmp.
    - Всегда дропает старую public._spend_staging_tmp перед созданием.
      Это важно: staging — transient scratch, его нельзя переиспользовать после
      прошлого падения, иначе spend-витрины могут собрать устаревший срез.
    - Создаёт: DDL → SET LOCAL work_mem/temp_file_limit → INSERT (единый скан FDW).

    SET LOCAL work_mem='192MB' и SET LOCAL temp_file_limit='20GB' (фиксы C/F:
    SPEND_WORKMEM_2026-06-17 и TEMP_FILE_LIMIT_CAP_2026-06-17) применяются ЗДЕСЬ,
    в момент создания staging. Роллапы, работающие в той же транзакции, наследуют их.

    Возвращает число строк, записанных в staging (0 = таблица уже существовала).
    """
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS public.{STAGING_TABLE}')
    conn.commit()

    t0 = time.perf_counter()
    logger.info('spend_staging: CREATE UNLOGGED TABLE + единый скан FDW (18.9M строк)')

    with conn.cursor() as cur:
        # SPEND_WORKMEM_2026-06-17: SET LOCAL ограничивает work_mem только этой транзакцией.
        # TEMP_FILE_LIMIT_CAP_2026-06-17: контролируемое падение вместо забивания диска в ноль.
        cur.execute("SET LOCAL work_mem = '192MB'")
        # TEMP_FILE_LIMIT_SAFE_2026-06-18: bi_analytic не имеет привилегии superuser
        # → SET temp_file_limit вызывает permission denied и плодит WARNING в логах.
        # SAVEPOINT позволяет откатить только неудачный SET, не трогая work_mem.
        cur.execute("SAVEPOINT before_tfl")
        try:
            cur.execute("SET LOCAL temp_file_limit = '20GB'")
            cur.execute("RELEASE SAVEPOINT before_tfl")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT before_tfl")
        # DDL staging (UNLOGGED — без WAL)
        cur.execute(_DDL_STAGING)
        # Единый скан FDW → INSERT INTO staging
        cur.execute(_STAGING_CTAS_SQL)
        rows = cur.rowcount
        # Индексы на staging отключены: экономим место, staging живёт только на время
        # последовательной spend-фазы.
        for idx_sql in _STAGING_INDEXES:
            cur.execute(idx_sql)
    conn.commit()

    elapsed = time.perf_counter() - t0
    logger.info(
        'spend_staging: готова за %.1f сек, %d строк (1 проход FDW → staging)',
        elapsed, rows,
    )
    return rows


def drop_staging(conn) -> None:
    """
    FDW_SINGLE_PASS_STAGING_2026-06-17

    Явно дропает UNLOGGED staging-таблицу _spend_staging_tmp.
    Вызывается последним из трёх спенд-билдеров (criterion_spend).
    IF EXISTS — безопасно если уже дропнута (retry / ручной запуск).
    """
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS public.{STAGING_TABLE}')
    conn.commit()
    logger.info('spend_staging: _spend_staging_tmp дропнута')


def staging_exists(conn) -> bool:
    """True если локальный spend staging существует и непустой."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.%s')" % STAGING_TABLE)
        if cur.fetchone()[0] is None:
            return False
        cur.execute(f'SELECT EXISTS (SELECT 1 FROM public.{STAGING_TABLE} LIMIT 1)')
        return bool(cur.fetchone()[0])
