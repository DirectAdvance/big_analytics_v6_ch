"""
step11_pixel_score/step11.py — атрибуция pixel-воронки по составному CR
→ pixel_score (веса) → big_analytics_pixel_score → big_analytics_full.

Логика:
  1. pixel_score — таблица CPL-скоров по кампаниям:
     Для каждой кампании считается cpl_score (0.3–3.0) — качество CPL
     относительно среднего по домену за тот же месяц.
     cpl_score = 1.0 (нейтральный) если нет данных (статус «ждём»).

  2. Атрибуция pixel-лидов по кампаниям через cpl_score:
     weight = cpl_score_кампании / SUM(cpl_score домена+месяц) × 100.
     Лучшая кампания (выше cpl_score) получает бо́льшую долю pixel-лидов.

  3. big_analytics_pixel_score = pixel_daily × weight per campaign.
     Метрики (NUMERIC) сохраняют SUM(pixel) = SUM(score) на уровне домена.

  4. Перенос в big_analytics_full БЕЗ приведения к целому: целевые воронко-метрики
     (kol_vo_zayavok/korr/kval/priezd/prodazhi) в big_analytics_full имеют тип NUMERIC,
     поэтому дробная пиксель-атрибуция сохраняется точно (инвариант дробной атрибуции).
     (_source_table='пиксель_атрибуц', источник='пиксель_атрибуц').

Инвариант:
  SUM(kol_vo_zayavok) big_analytics_pixel
   == SUM(kol_vo_zayavok) big_analytics_pixel_score
  Проверяется глобально + по доменам (топ-расхождений логируется).

Обновление: DROP + CREATE + полный пересчёт. Идемпотентно.
"""

import logging
import threading
import time

logger = logging.getLogger('pipeline.step11')

# STEP11_TEMP_REDUCE_2026-07-11: целевой work_mem для тяжёлых hash-aggregate/GROUP BY
# step11 (по big_analytics_full и big_analytics_direct). 4 GB (vs общий 256MB/WORK_MEM)
# уменьшает hash/sort-спилл в pgsql_tmp, чтобы step11 попытался влезть без ENOSPC при
# диске ~0. Применяется СТРОГО в паре с max_parallel_workers_per_gather=0 (serial) —
# ровно как _WM_DIRECT в step3: на Victory hash_mem_multiplier=2 и
# max_parallel_workers_per_gather=2 → параллельный 4GB дал бы до 4GB×2×3(leader+2worker)
# = 24 GB на ОДИН hash-узел; при Swap=0 это OOM SIGKILL (главный источник ОСИРОТЕВШЕГО
# pgsql_tmp, который без root не вычистить). Serial бюджет памяти ограничен
# ~work_mem×hash_mem_multiplier ≈ 8 GB/узел, с запасом под ~30 GB avail. Через SET LOCAL —
# авто-сброс на commit, downstream-шаги на этом (пуловом) conn не затрагиваются.
_WM_STEP11 = '4096MB'

T_FULL   = 'big_analytics_full'
T_PIXEL  = 'big_analytics_pixel'
T_DIRECT = 'big_analytics_direct'   # источник domain/campaign-бенчмарков (= config.settings.T_DIRECT)
# DIRECT_SLIM_2026-07-11: бенчмарк step11 (domain_stats/campaign_stats_monthly) читает
# ТОЛЬКО 8 узких колонок из direct ("Date", domain, "CampaignId", total_cost, kval,
# priezd, prodazhi, _source_table). pipeline.py после step6 строит точную проекцию этих
# колонок в big_analytics_direct_slim и TRUNCATE-ит толстый big_analytics_direct (−15 GB
# пика диска). slim = ТОЧНАЯ проекция БЕЗ фильтра direction → бенчмарк семантически
# идентичен (те же cpl_avg→cpl_score→веса → дробная пиксель-атрибуция и golden не
# сдвигаются). Источник бенчмарка (slim vs direct) выбирается в РАНТАЙМЕ в run() через
# to_regclass — НЕ хардкодится (step11.py общий для 3 пайплайнов; fast_pipeline slim не
# строит → fallback на big_analytics_direct). Остальной step11 (INSERT/перенос в full)
# работает с big_analytics_full/big_analytics_pixel — не с direct.
T_DIRECT_SLIM = 'big_analytics_direct_slim'
T_SCORE  = 'pixel_score'
T_OUT    = 'big_analytics_pixel_score'

_OLD_TABLES = ('analytics_pixel_score', 'analytics_pixel_score_click')

_NUMERIC_METRIC_COLS = (
    'kol_vo_zayavok', 'korr', 'kval', 'priezd', 'prodazhi',
    'nekorr', 'ne_otvechaet', 'filtr', 'nedozvon', 'priedet',
    'dohod_do_kredita', 'dobro',
)


def _build_score_ddl_sql() -> str:
    """DDL для pixel_score — промежуточная таблица весов кампаний."""
    return f"""
DROP TABLE IF EXISTS {T_SCORE};
CREATE UNLOGGED TABLE {T_SCORE} (
    month          DATE    NOT NULL,
    "салон"        TEXT    NOT NULL,
    domain         TEXT    NOT NULL,
    источник       TEXT,
    направление    TEXT,
    "CampaignId"   BIGINT  NOT NULL,
    "CampaignName" TEXT,
    kol_vo_zayavok NUMERIC,
    korr           NUMERIC,
    kval           NUMERIC,
    priezd         NUMERIC,
    prodazhi       NUMERIC,
    cpl_score      NUMERIC(4,2) DEFAULT 1.0,

    -- Domain benchmarks (avg по домену за тот же месяц)
    cpl_avg_квал      NUMERIC(12,2),
    cpl_avg_визит     NUMERIC(12,2),
    cpl_avg_продажа   NUMERIC(12,2),

    -- Campaign CPL per stage
    cpl_кам_квал      NUMERIC(12,2),
    cpl_кам_визит     NUMERIC(12,2),
    cpl_кам_продажа   NUMERIC(12,2),

    -- Score per stage (clamped ratio 0.3-3.0)
    score_квал        NUMERIC(4,2),
    score_визит       NUMERIC(4,2),
    score_продажа     NUMERIC(4,2),

    -- Weight per stage
    w_квал            NUMERIC(4,2),
    w_визит           NUMERIC(4,2),
    w_продажа         NUMERIC(4,2),

    -- Status per stage
    status_квал       TEXT,
    status_визит      TEXT,
    status_продажа    TEXT,

    -- Расход кампании за месяц (из Direct)
    расход                 NUMERIC(14,2),

    -- Доля кампании в домене (на основе cpl_score)
    weight                 NUMERIC(8,4),   -- = cpl_score / SUM(cpl_score домена) × 100

    -- Pixel totals (домен) и атрибутированные значения кампании
    pixel_kol_vo_домена       NUMERIC(10,2),  -- SUM(kol_vo_zayavok) из big_analytics_pixel по домену
    pixel_kol_vo_кампании     NUMERIC(10,4),  -- = pixel_kol_vo_домена × weight / 100
    pixel_квал_домена         NUMERIC(10,2),  -- SUM(kval) из big_analytics_pixel по домену
    attr_pixel_квал_кампании  NUMERIC(10,4),  -- = pixel_квал_домена × weight / 100
    pixel_приезд_домена       NUMERIC(10,2),  -- SUM(priezd) из big_analytics_pixel по домену
    attr_pixel_приезд_кампании NUMERIC(10,4), -- = pixel_приезд_домена × weight / 100
    pixel_продажи_домена      NUMERIC(10,2),  -- SUM(prodazhi) из big_analytics_pixel по домену
    attr_pixel_продажи_кампании NUMERIC(10,4) -- = pixel_продажи_домена × weight / 100
);
"""


def _build_ddl_sql() -> str:
    """DDL для big_analytics_pixel_score."""
    drop_old = '\n'.join(f'DROP TABLE IF EXISTS {t};' for t in _OLD_TABLES)
    alter_metrics = '\n'.join(
        f'ALTER TABLE {T_OUT} ALTER COLUMN {col} TYPE NUMERIC USING {col}::NUMERIC;'
        for col in _NUMERIC_METRIC_COLS
    )
    return f"""
{drop_old}
DROP TABLE IF EXISTS {T_OUT};

-- big_analytics_pixel_score = LIKE big_analytics_full, но метрики NUMERIC.
-- NUMERIC нужен чтобы точно сохранить SUM(pixel)=SUM(score) без округления долей.
CREATE UNLOGGED TABLE {T_OUT} (LIKE {T_FULL} INCLUDING DEFAULTS);
{alter_metrics}
"""


# ─── pixel_score INSERT ────────────────────────────────────────────────────────
# Материализует campaign_share + raw metrics + CampaignName из big_analytics_full.
_SCORE_INSERT_SQL = f"""
WITH
-- 1. Метрики кампаний за месяц (только платный трафик).
campaign_monthly AS (
    SELECT
        DATE_TRUNC('month', "Date")::DATE AS month,
        "салон",
        domain,
        источник,
        _source_table,
        "CampaignId",
        SUM(COALESCE("Clicks",       0)) AS clicks,
        SUM(COALESCE(total_cost,     0)) AS расход,
        SUM(COALESCE(kol_vo_zayavok, 0)) AS kol_vo,
        SUM(COALESCE(korr,           0)) AS korr,
        SUM(COALESCE(kval,           0)) AS kval,
        SUM(COALESCE(priezd,         0)) AS priezd,
        SUM(COALESCE(prodazhi,       0)) AS prodazhi
    FROM {T_FULL}
    WHERE _source_table IN ('direct', 'crop_targeting', 'tp8', 'tp9', 'tp10')
      AND "CampaignId" IS NOT NULL
      AND domain       IS NOT NULL
      AND "салон"      IS NOT NULL
      AND "Date"       IS NOT NULL
    GROUP BY DATE_TRUNC('month', "Date")::DATE, "салон", domain, источник, _source_table, "CampaignId"
    HAVING SUM(COALESCE(total_cost, 0)) > 0
       AND SUM(COALESCE("Clicks",   0)) > 0
),

-- 2. направление.
campaign_weight AS (
    SELECT
        month, "салон", domain, источник, "CampaignId",
        clicks, расход, kol_vo, korr, kval, priezd, prodazhi,
        CASE WHEN _source_table = 'direct' THEN 'контекст' ELSE 'посевы' END AS направление,
        (1.0 * kol_vo + 3.0 * korr + 10.0 * kval + 30.0 * priezd + 100.0 * prodazhi)
            / NULLIF(clicks::NUMERIC, 0) AS cr_composite
    FROM campaign_monthly
),

-- 3. SUM cr_composite по (домену, салону).
domain_weight AS (
    SELECT month, "салон", domain, SUM(cr_composite) AS d_composite
    FROM campaign_weight
    WHERE cr_composite > 0
    GROUP BY month, "салон", domain
),

-- 4. Атрибуты кампании — последняя строка из платного трафика.
campaign_attrs AS (
    SELECT DISTINCT ON ("CampaignId")
        "CampaignId",
        "CampaignName"
    FROM {T_FULL}
    WHERE _source_table IN ('direct', 'crop_targeting', 'tp8', 'tp9', 'tp10')
      AND "CampaignId" IS NOT NULL
    ORDER BY "CampaignId", "Date" DESC
)

INSERT INTO {T_SCORE} (
    month, "салон", domain, источник, направление, "CampaignId", "CampaignName",
    расход, kol_vo_zayavok, korr, kval, priezd, prodazhi
)
SELECT
    cw.month,
    cw."салон",
    cw.domain,
    cw.источник,
    cw.направление,
    cw."CampaignId",
    ca."CampaignName",
    cw.расход,
    cw.kol_vo,
    cw.korr,
    cw.kval,
    cw.priezd,
    cw.prodazhi
FROM campaign_weight cw
JOIN domain_weight   dw
  ON dw.month   = cw.month
 AND dw.domain  = cw.domain
 AND dw."салон" = cw."салон"
LEFT JOIN campaign_attrs ca ON ca."CampaignId" = cw."CampaignId"
WHERE dw.d_composite > 0
  AND cw.cr_composite > 0
"""

_SCORE_WEIGHT_UPDATE_SQL = f"""
WITH w AS (
    SELECT month, "салон", domain, "CampaignId",
        cpl_score / NULLIF(SUM(cpl_score) OVER (PARTITION BY month, "салон", domain), 0) * 100.0 AS weight
    FROM {T_SCORE}
    WHERE cpl_score > 0
)
UPDATE {T_SCORE} ps
SET weight = w.weight
FROM w
WHERE ps.month        = w.month
  AND ps."салон"      = w."салон"
  AND ps.domain       = w.domain
  AND ps."CampaignId" = w."CampaignId"
"""

_SCORE_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_ps_weight_dom_mon ON {T_SCORE} (domain, month);
CREATE INDEX IF NOT EXISTS idx_ps_weight_salon   ON {T_SCORE} ("салон");
CREATE INDEX IF NOT EXISTS idx_ps_weight_cid     ON {T_SCORE} ("CampaignId");
ALTER TABLE {T_SCORE} SET LOGGED;
ANALYZE {T_SCORE};
"""

# ─── CPL-скор: обновление cpl_score в pixel_score ────────────────────────────
# Рассчитывается из big_analytics_direct за тот же месяц, что и строка pixel_score.
# Апрель → domain avg из апреля, март → из марта, май → из мая.
# Смысл: насколько CPL данной кампании лучше/хуже среднего по домену за тот же месяц.
# Диапазон: [0.3, 3.0]. 1.0 = нейтрально (нет данных или около среднего).
def _build_score_cpl_update_sql(bench_src: str) -> str:
    """DIRECT_SLIM_2026-07-11: SQL бенчмарка cpl_score. Источник (bench_src) выбирается
    в РАНТАЙМЕ в run() — big_analytics_direct_slim (если pipeline.py построил slim после
    step6) ИЛИ big_analytics_direct (fallback для fast_pipeline, где slim не строится).
    НЕ хардкодим на import: step11.py общий для 3 пайплайнов; хардкод slim → в fast
    'relation big_analytics_direct_slim does not exist' → откат всей транзакции step11."""
    return f"""
WITH
-- Уникальные месяцы из pixel_score — обрабатываем каждый независимо.
-- Domain-level статистика за каждый отдельный месяц (из big_analytics_direct, целые числа).
domain_stats AS (
    SELECT
        DATE_TRUNC('month', "Date")::DATE            AS month,
        domain,
        SUM(total_cost) / NULLIF(SUM(kval), 0)      AS cpl_avg_kval,
        SUM(total_cost) / NULLIF(SUM(priezd), 0)    AS cpl_avg_vizit,
        CASE WHEN SUM(prodazhi) >= 3
             THEN SUM(total_cost) / SUM(prodazhi)
             ELSE NULL END                           AS cpl_avg_prodazhi,
        AVG(CASE WHEN kval    > 0 THEN kval    END)  AS cnt_avg_kval,
        CASE WHEN SUM(priezd) >= 5
             THEN AVG(CASE WHEN priezd > 0 THEN priezd END)
             ELSE NULL END                           AS cnt_avg_vizit,
        AVG(CASE WHEN prodazhi > 0 THEN prodazhi END) AS cnt_avg_prodazhi
    FROM {bench_src}   -- DIRECT_SLIM_2026-07-11: рантайм-источник (slim | direct-fallback)
    WHERE _source_table IN ('direct', 'crop_targeting', 'tp8', 'tp9', 'tp10')
      AND total_cost > 0
      AND domain IS NOT NULL
    GROUP BY DATE_TRUNC('month', "Date")::DATE, domain
),
-- Предагрегированные метрики кампаний по месяцам — один скан big_analytics_direct.
campaign_stats_monthly AS (
    SELECT
        DATE_TRUNC('month', "Date")::DATE AS month,
        "CampaignId",
        domain,
        SUM(total_cost) AS spend,
        SUM(kval)       AS kval,
        SUM(priezd)     AS priezd,
        SUM(prodazhi)   AS prodazhi
    FROM {bench_src}   -- DIRECT_SLIM_2026-07-11: рантайм-источник (slim | direct-fallback)
    WHERE _source_table IN ('direct', 'crop_targeting', 'tp8', 'tp9', 'tp10')
      AND "CampaignId" IS NOT NULL
      AND domain IS NOT NULL
    GROUP BY DATE_TRUNC('month', "Date")::DATE, "CampaignId", domain
),
-- Уникальные (CampaignId, domain, month) из pixel_score с JOIN к данным того же месяца.
campaign_cpl AS (
    SELECT
        ps."CampaignId",
        ps.domain,
        ps.month,
        COALESCE(cs.spend,    0) AS spend,
        COALESCE(cs.kval,     0) AS kval,
        COALESCE(cs.priezd,   0) AS priezd,
        COALESCE(cs.prodazhi, 0) AS prodazhi
    FROM (SELECT DISTINCT "CampaignId", domain, month FROM {T_SCORE} WHERE "CampaignId" IS NOT NULL) ps
    LEFT JOIN campaign_stats_monthly cs
           ON cs."CampaignId" = ps."CampaignId"
          AND cs.domain        = ps.domain
          AND cs.month         = ps.month
),
-- Вычисляем score и вес для каждого этапа воронки
scores AS (
    SELECT
        cc."CampaignId",
        cc.domain,
        cc.month,
        cc.spend,
        -- ── domain benchmarks (прокидываем из domain_stats) ──
        ds.cpl_avg_kval,
        ds.cpl_avg_vizit,
        ds.cpl_avg_prodazhi,
        -- ── campaign CPL per stage ──
        CASE WHEN COALESCE(cc.kval,     0) > 0 THEN cc.spend / cc.kval     ELSE NULL END AS cpl_kam_kval,
        CASE WHEN COALESCE(cc.priezd,   0) > 0 THEN cc.spend / cc.priezd   ELSE NULL END AS cpl_kam_vizit,
        CASE WHEN COALESCE(cc.prodazhi, 0) > 0 THEN cc.spend / cc.prodazhi ELSE NULL END AS cpl_kam_prodazhi,
        -- ── status per stage ──
        CASE
            WHEN ds.cpl_avg_kval IS NULL
                THEN 'ждём'
            WHEN COALESCE(cc.kval, 0) > 0
                THEN 'данные'
            WHEN COALESCE(cc.spend, 0) < 3 * ds.cpl_avg_kval
                THEN 'ждём'
            ELSE 'плохо'
        END AS status_kval,
        CASE
            WHEN ds.cpl_avg_vizit IS NULL
                THEN 'ждём'
            WHEN COALESCE(cc.priezd, 0) > 0
                THEN 'данные'
            WHEN COALESCE(cc.spend, 0) < 3 * ds.cpl_avg_vizit
                THEN 'ждём'
            ELSE 'плохо'
        END AS status_vizit,
        CASE
            WHEN ds.cpl_avg_prodazhi IS NULL
                THEN 'ждём'
            WHEN COALESCE(cc.prodazhi, 0) > 0
                THEN 'данные'
            WHEN COALESCE(cc.spend, 0) < 3 * ds.cpl_avg_prodazhi
                THEN 'ждём'
            ELSE 'плохо'
        END AS status_prodazhi,
        -- ── score per stage ──
        CASE
            WHEN COALESCE(cc.kval, 0) > 0 AND cc.spend > 0 AND ds.cpl_avg_kval IS NOT NULL
                THEN GREATEST(0.3, LEAST(3.0, ds.cpl_avg_kval / (cc.spend / cc.kval)))
            WHEN COALESCE(cc.kval, 0) = 0
                 AND COALESCE(cc.spend, 0) < 3 * COALESCE(ds.cpl_avg_kval, 999999999)
                THEN 1.0   -- ждём: мало потрачено
            WHEN COALESCE(cc.kval, 0) = 0
                THEN 0.3   -- плохо: тратим, конверсий нет
            ELSE 1.0
        END AS score_kval,
        CASE
            WHEN COALESCE(cc.kval, 0) = 0
                 AND COALESCE(cc.spend, 0) < 3 * COALESCE(ds.cpl_avg_kval, 999999999)
                THEN 0.0   -- ждём → исключаем из суммы весов
            WHEN COALESCE(cc.kval, 0) = 0
                THEN 1.0   -- плохо → минимальный вес
            WHEN ds.cnt_avg_kval IS NOT NULL AND ds.cnt_avg_kval > 0
                THEN GREATEST(1.0, LEAST(3.0, cc.kval / ds.cnt_avg_kval))
            ELSE 1.0
        END AS w_kval,
        -- ── визит ──
        CASE
            WHEN COALESCE(cc.priezd, 0) > 0 AND cc.spend > 0 AND ds.cpl_avg_vizit IS NOT NULL
                THEN GREATEST(0.3, LEAST(3.0, ds.cpl_avg_vizit / (cc.spend / cc.priezd)))
            WHEN COALESCE(cc.priezd, 0) = 0
                 AND COALESCE(cc.spend, 0) < 3 * COALESCE(ds.cpl_avg_vizit, 999999999)
                THEN 1.0
            WHEN COALESCE(cc.priezd, 0) = 0
                THEN 0.3
            ELSE 1.0
        END AS score_vizit,
        CASE
            WHEN COALESCE(cc.priezd, 0) = 0
                 AND COALESCE(cc.spend, 0) < 3 * COALESCE(ds.cpl_avg_vizit, 999999999)
                THEN 0.0
            WHEN COALESCE(cc.priezd, 0) = 0
                THEN 1.0
            WHEN ds.cnt_avg_vizit IS NOT NULL AND ds.cnt_avg_vizit > 0
                THEN GREATEST(1.0, LEAST(3.0, cc.priezd / ds.cnt_avg_vizit))
            ELSE 1.0
        END AS w_vizit,
        -- ── продажа ──
        CASE
            WHEN COALESCE(cc.prodazhi, 0) > 0 AND cc.spend > 0 AND ds.cpl_avg_prodazhi IS NOT NULL
                THEN GREATEST(0.3, LEAST(3.0, ds.cpl_avg_prodazhi / (cc.spend / cc.prodazhi)))
            WHEN COALESCE(cc.prodazhi, 0) = 0
                 AND COALESCE(cc.spend, 0) < 3 * COALESCE(ds.cpl_avg_prodazhi, 999999999)
                THEN 1.0
            WHEN COALESCE(cc.prodazhi, 0) = 0
                THEN 0.3
            ELSE 1.0
        END AS score_prodazhi,
        CASE
            WHEN COALESCE(cc.prodazhi, 0) = 0
                 AND COALESCE(cc.spend, 0) < 3 * COALESCE(ds.cpl_avg_prodazhi, 999999999)
                THEN 0.0
            WHEN COALESCE(cc.prodazhi, 0) = 0
                THEN 1.0
            WHEN ds.cnt_avg_prodazhi IS NOT NULL AND ds.cnt_avg_prodazhi > 0
                THEN GREATEST(1.0, LEAST(3.0, cc.prodazhi / ds.cnt_avg_prodazhi))
            ELSE 1.0
        END AS w_prodazhi
    FROM campaign_cpl cc
    LEFT JOIN domain_stats ds
           ON ds.domain = cc.domain
          AND ds.month  = cc.month
),
-- Итоговый взвешенный скор (на каждый месяц отдельно) + все промежуточные колонки
final_scores AS (
    SELECT
        "CampaignId",
        domain,
        month,
        -- итоговый cpl_score
        CASE
            WHEN (w_kval + w_vizit + w_prodazhi) = 0
                THEN 1.0   -- все этапы в «ждём» → нейтральный скор
            ELSE GREATEST(0.3, LEAST(3.0,
                (w_kval * score_kval + w_vizit * score_vizit + w_prodazhi * score_prodazhi)
                / NULLIF(w_kval + w_vizit + w_prodazhi, 0)
            ))
        END AS cpl_score,
        -- domain benchmarks
        cpl_avg_kval,
        cpl_avg_vizit,
        cpl_avg_prodazhi,
        -- campaign CPL per stage
        cpl_kam_kval,
        cpl_kam_vizit,
        cpl_kam_prodazhi,
        -- score per stage
        score_kval,
        score_vizit,
        score_prodazhi,
        -- weight per stage
        w_kval,
        w_vizit,
        w_prodazhi,
        -- status per stage
        status_kval,
        status_vizit,
        status_prodazhi
    FROM scores
)
UPDATE {T_SCORE} ps
SET
    cpl_score        = fs.cpl_score,
    -- domain benchmarks
    cpl_avg_квал     = fs.cpl_avg_kval,
    cpl_avg_визит    = fs.cpl_avg_vizit,
    cpl_avg_продажа  = fs.cpl_avg_prodazhi,
    -- campaign CPL per stage
    cpl_кам_квал     = fs.cpl_kam_kval,
    cpl_кам_визит    = fs.cpl_kam_vizit,
    cpl_кам_продажа  = fs.cpl_kam_prodazhi,
    -- score per stage
    score_квал       = fs.score_kval,
    score_визит      = fs.score_vizit,
    score_продажа    = fs.score_prodazhi,
    -- weight per stage
    w_квал           = fs.w_kval,
    w_визит          = fs.w_vizit,
    w_продажа        = fs.w_prodazhi,
    -- status per stage
    status_квал      = fs.status_kval,
    status_визит     = fs.status_vizit,
    status_продажа   = fs.status_prodazhi
FROM final_scores fs
WHERE ps."CampaignId" = fs."CampaignId"
  AND ps.domain = fs.domain
  AND ps.month  = fs.month
"""


# ─── Pixel totals: атрибутированные метрики из big_analytics_pixel ────────────
# Использует stored weight (уже заполнен _SCORE_WEIGHT_UPDATE_SQL).
_SCORE_PIXEL_TOTALS_UPDATE_SQL = f"""
WITH domain_pixel_totals AS (
    SELECT
        "салон",
        domain,
        DATE_TRUNC('month', "Date")::DATE AS month,
        SUM(COALESCE(kol_vo_zayavok, 0)) AS pixel_kol_vo_домена,
        SUM(COALESCE(kval,           0)) AS pixel_квал_домена,
        SUM(COALESCE(priezd,         0)) AS pixel_приезд_домена,
        SUM(COALESCE(prodazhi,       0)) AS pixel_продажи_домена
    FROM {T_PIXEL}
    WHERE domain IS NOT NULL
      AND "Date"  IS NOT NULL
      AND "салон" IS NOT NULL
    GROUP BY "салон", domain, DATE_TRUNC('month', "Date")::DATE
)
UPDATE {T_SCORE} ps
SET
    pixel_kol_vo_домена          = dpt.pixel_kol_vo_домена,
    pixel_kol_vo_кампании        = dpt.pixel_kol_vo_домена   * ps.weight / 100.0,
    pixel_квал_домена            = dpt.pixel_квал_домена,
    attr_pixel_квал_кампании     = dpt.pixel_квал_домена     * ps.weight / 100.0,
    pixel_приезд_домена          = dpt.pixel_приезд_домена,
    attr_pixel_приезд_кампании   = dpt.pixel_приезд_домена   * ps.weight / 100.0,
    pixel_продажи_домена         = dpt.pixel_продажи_домена,
    attr_pixel_продажи_кампании  = dpt.pixel_продажи_домена  * ps.weight / 100.0
FROM domain_pixel_totals dpt
WHERE ps.domain       = dpt.domain
  AND ps.month        = dpt.month
  AND ps."салон"      = dpt."салон"
"""

# ─── big_analytics_pixel_score INSERT ─────────────────────────────────────────
# Использует pixel_score (веса/доли уже посчитаны) + pixel_daily + campaign_attrs.
_KEY_PIXEL_SCORE_EXPR = (
    """COALESCE(p."Date"::TEXT, '') || '|' || """
    """COALESCE(s.domain, '') || '|' || """
    """'пиксель_атрибуц' || '|' || """
    """COALESCE(s."CampaignId"::TEXT, '')"""
)

_INSERT_SQL = f"""
WITH
-- Weight кампании = cpl_score_кампании / SUM(cpl_score домена).
-- cpl_score уже посчитан в pixel_score на шаге CPL.
score_weights AS (
    SELECT
        month, "салон", domain, "CampaignId",
        cpl_score / NULLIF(SUM(cpl_score) OVER (PARTITION BY month, "салон", domain), 0) * 100.0 AS weight
    FROM {T_SCORE}
    WHERE cpl_score > 0
),
-- Pixel за день по (Date, салон, domain).
pixel_daily AS (
    SELECT
        "Date",
        "салон",
        domain,
        DATE_TRUNC('month', "Date")::DATE AS month,
        SUM(COALESCE(total_cost,     0)) AS px_cost,
        SUM(COALESCE(kol_vo_zayavok, 0)) AS px_zayavok,
        SUM(COALESCE(korr,           0)) AS px_korr,
        SUM(COALESCE(kval,           0)) AS px_kval,
        SUM(COALESCE(priezd,         0)) AS px_priezd,
        SUM(COALESCE(prodazhi,       0)) AS px_prodazhi
    FROM {T_PIXEL}
    WHERE "Date"  IS NOT NULL
      AND domain  IS NOT NULL
      AND "салон" IS NOT NULL
    GROUP BY "Date", "салон", domain
),

-- Атрибуты кампании (свежая строка) для заполнения полей строки.
campaign_attrs AS (
    SELECT DISTINCT ON ("CampaignId")
        "CampaignId",
        "CampaignName",
        campaign_code, tp, cpc_cpa, site_quiz,
        account_login, manager_login,
        "марки авто", "Название crm",
        "статус", "специалист", "тип_сайта", "шаблон",
        "салон" AS camp_salon, "город", "регион",
        проджект, id_салона, менеджер,
        направление,
        "номер кампании | название кампании",
        "аккаунт|сайт",
        поставщик,
        campaign_status
    FROM {T_FULL}
    WHERE _source_table IN ('direct', 'crop_targeting', 'tp8', 'tp9', 'tp10')
      AND "CampaignId" IS NOT NULL
    ORDER BY "CampaignId", "Date" DESC
)

INSERT INTO {T_OUT} (
    key3, "Date", "День недели", week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
    "RlAdjustmentId", "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    account_login, manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто", "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi,
    nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
    direction,
    "неверный_кодер_new", fid,
    проджект, id_салона, менеджер,
    источник, направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки", "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date, prodazhi_arrival_date,
    поставщик, _source_table,
    key_pixel_score,
    campaign_status
)
SELECT
    NULL::TEXT     AS key3,
    p."Date",
    CASE EXTRACT(ISODOW FROM p."Date")
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END            AS "День недели",
    DATE_TRUNC('week', p."Date")::DATE          AS week_start,
    s."CampaignId",
    ca."CampaignName",
    NULL::BIGINT   AS "AdGroupId",
    NULL::TEXT     AS "AdGroupName",
    NULL::TEXT     AS "AdNetworkType",
    NULL::TEXT     AS "Device",
    NULL::BIGINT   AS "Impressions",
    NULL::BIGINT   AS "Clicks",
    p.px_cost     * sw.weight / 100.0            AS total_cost,
    s.domain,
    NULL::BIGINT   AS "RlAdjustmentId",
    NULL::TEXT     AS "RlAdjustmentId_total",
    ca.campaign_code, ca.tp, ca.cpc_cpa, ca.site_quiz,
    NULL::TEXT     AS adgroup_code,
    ca.account_login, ca.manager_login,
    NULL::TEXT AS ag_part1, NULL::TEXT AS ag_part2, NULL::TEXT AS ag_part3,
    NULL::TEXT AS ag_part4, NULL::TEXT AS ag_part5, NULL::TEXT AS ag_part6,
    NULL::TEXT AS ag_part7,
    ca."марки авто",
    ca."Название crm",
    'Пиксель_атрибуц'::TEXT                     AS тип_заявки,
    p.px_zayavok  * sw.weight / 100.0            AS kol_vo_zayavok,
    p.px_korr     * sw.weight / 100.0            AS korr,
    p.px_kval     * sw.weight / 100.0            AS kval,
    p.px_priezd   * sw.weight / 100.0            AS priezd,
    p.px_prodazhi * sw.weight / 100.0            AS prodazhi,
    0::NUMERIC AS nekorr, 0::NUMERIC AS ne_otvechaet, 0::NUMERIC AS filtr,
    0::NUMERIC AS nedozvon, 0::NUMERIC AS priedet,
    0::NUMERIC AS dohod_do_kredita, 0::NUMERIC AS dobro,
    ca."статус", ca."специалист", ca."тип_сайта", ca."шаблон",
    ca.camp_salon, ca."город", ca."регион",
    'Авто'::TEXT   AS direction,
    NULL::TEXT     AS "неверный_кодер_new",
    NULL::TEXT     AS fid,
    ca.проджект, ca.id_салона, ca.менеджер,
    'Пиксель_атрибуц'::TEXT                     AS источник,   -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    'Пиксель_атрибуц'::TEXT                     AS направление,
    ca."номер кампании | название кампании",
    NULL::TEXT     AS "номер группы | название группы",
    NULL::INTEGER  AS "План заявки",
    NULL::INTEGER  AS "План приезда",
    ca."аккаунт|сайт",
    NULL::INTEGER  AS priezd_arrival_date,
    NULL::INTEGER  AS prodazhi_arrival_date,
    ca.поставщик,
    'пиксель_атрибуц'::TEXT                     AS _source_table,
    {_KEY_PIXEL_SCORE_EXPR}                     AS key_pixel_score,
    ca.campaign_status
FROM pixel_daily   p
JOIN {T_SCORE} s
  ON s.month   = p.month
 AND s."салон" = p."салон"
 AND s.domain  = p.domain
JOIN score_weights sw
  ON sw.month   = s.month
 AND sw."салон" = s."салон"
 AND sw.domain  = s.domain
 AND sw."CampaignId" = s."CampaignId"
LEFT JOIN campaign_attrs ca ON ca."CampaignId" = s."CampaignId"
"""

# ─── Остаток: домены без платных кампаний → share=1.0 ─────────────────────────
# Домены без direct/crop/tp8 кампаний сохраняются с CampaignId=NULL
# чтобы SUM(pixel)==SUM(pixel_score) инвариант выполнялся точно.
_INSERT_LEFTOVERS_SQL = f"""
WITH
pixel_daily AS (
    SELECT
        "Date",
        "салон",
        domain,
        DATE_TRUNC('month', "Date")::DATE AS month,
        SUM(COALESCE(total_cost,     0)) AS px_cost,
        SUM(COALESCE(kol_vo_zayavok, 0)) AS px_zayavok,
        SUM(COALESCE(korr,           0)) AS px_korr,
        SUM(COALESCE(kval,           0)) AS px_kval,
        SUM(COALESCE(priezd,         0)) AS px_priezd,
        SUM(COALESCE(prodazhi,       0)) AS px_prodazhi
    FROM {T_PIXEL}
    WHERE "Date"  IS NOT NULL
      AND domain  IS NOT NULL
      AND "салон" IS NOT NULL
    GROUP BY "Date", "салон", domain
)
INSERT INTO {T_OUT} (
    "Date", "День недели", week_start,
    "CampaignId", total_cost, domain,
    тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi,
    nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    "салон", direction, источник, направление, _source_table, key_pixel_score,
    проджект, id_салона, менеджер
)
SELECT
    p."Date",
    CASE EXTRACT(ISODOW FROM p."Date")
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    DATE_TRUNC('week', p."Date")::DATE,
    NULL,
    p.px_cost,
    p.domain,
    'Пиксель_атрибуц'::TEXT,
    p.px_zayavok::NUMERIC,
    p.px_korr::NUMERIC,
    p.px_kval::NUMERIC,
    p.px_priezd::NUMERIC,
    p.px_prodazhi::NUMERIC,
    0::NUMERIC, 0::NUMERIC, 0::NUMERIC, 0::NUMERIC, 0::NUMERIC, 0::NUMERIC, 0::NUMERIC,
    p."салон",
    'Авто'::TEXT,
    'Пиксель_атрибуц'::TEXT,  -- KOMPLEKS_REFACTOR_REDO_2026-07-09 ostatok fix: источник
    'Пиксель_атрибуц'::TEXT,  -- KOMPLEKS_REFACTOR_REDO_2026-07-09 ostatok fix: направление
    'пиксель_атрибуц'::TEXT,  -- _source_table: технический идентификатор, остаётся строчным
    COALESCE(p."Date"::TEXT, '') || '|' || COALESCE(p.domain, '') || '|пиксель_атрибуц|NULL',
    NULLIF(TRIM(gs.project_manager), ''),
    gs.client_id,
    NULLIF(TRIM(gs.sales_manager), '')
FROM pixel_daily p
LEFT JOIN local_gsheet_sites gs
       ON LOWER(TRIM(gs."domain")) = LOWER(TRIM(p.domain))
WHERE NOT EXISTS (
    SELECT 1 FROM {T_SCORE} s
    WHERE s.month   = p.month
      AND s."салон" = p."салон"
      AND s.domain  = p.domain
)
"""

_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_pixel_score_salon_date   ON {T_OUT} ("салон", "Date");
CREATE INDEX IF NOT EXISTS idx_pixel_score_domain_date  ON {T_OUT} (domain, "Date");
CREATE INDEX IF NOT EXISTS idx_pixel_score_date         ON {T_OUT} ("Date");
CREATE INDEX IF NOT EXISTS idx_pixel_score_campaignid   ON {T_OUT} ("CampaignId");
CREATE INDEX IF NOT EXISTS idx_pixel_score_key          ON {T_OUT} (key_pixel_score);
ALTER TABLE {T_OUT} SET LOGGED;
ANALYZE {T_OUT};
"""


# ─── Перенос в big_analytics_full ─────────────────────────────────────────────
_PUSH_TO_FULL_SQL = f"""
DELETE FROM {T_FULL} WHERE _source_table = 'пиксель_атрибуц';

INSERT INTO {T_FULL} (
    key3, "Date", "День недели", week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
    "RlAdjustmentId", "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    account_login, manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто", "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi,
    nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
    direction,
    "неверный_кодер_new", fid,
    проджект, id_салона, менеджер,
    источник, направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки", "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date, prodazhi_arrival_date,
    поставщик, _source_table,
    key_pixel_score,
    campaign_status
)
SELECT
    ps.key3, ps."Date", ps."День недели", ps.week_start,
    ps."CampaignId", ps."CampaignName", ps."AdGroupId", ps."AdGroupName",
    ps."AdNetworkType", ps."Device", ps."Impressions", ps."Clicks", ps.total_cost, ps.domain,
    ps."RlAdjustmentId", ps."RlAdjustmentId_total",
    ps.campaign_code, ps.tp, ps.cpc_cpa, ps.site_quiz, ps.adgroup_code,
    ps.account_login, ps.manager_login,
    ps.ag_part1, ps.ag_part2, ps.ag_part3, ps.ag_part4, ps.ag_part5, ps.ag_part6, ps.ag_part7,
    ps."марки авто", ps."Название crm", ps.тип_заявки,
    ps.kol_vo_zayavok                   AS kol_vo_zayavok,
    ps.korr                             AS korr,
    ps.kval                             AS kval,
    ps.priezd                           AS priezd,
    ps.prodazhi                         AS prodazhi,
    ps.nekorr                           AS nekorr,
    ps.ne_otvechaet                     AS ne_otvechaet,
    ps.filtr                            AS filtr,
    ps.nedozvon                         AS nedozvon,
    ps.priedet                          AS priedet,
    ps.dohod_do_kredita                 AS dohod_do_kredita,
    ps.dobro                            AS dobro,
    COALESCE(ps."статус",      gs."status")        AS "статус",
    COALESCE(ps."специалист",  gs."directologist") AS "специалист",
    COALESCE(ps."тип_сайта",  gs."site_type")     AS "тип_сайта",
    COALESCE(ps."шаблон",     gs."template")      AS "шаблон",
    COALESCE(ps."салон",      gs."salon")         AS "салон",
    COALESCE(ps."город",      gs."city")          AS "город",
    COALESCE(ps."регион",     gs."region")        AS "регион",
    ps.direction,
    ps."неверный_кодер_new", ps.fid,
    ps.проджект, ps.id_салона, ps.менеджер,
    ps.источник, ps.направление,
    ps."номер кампании | название кампании",
    ps."номер группы | название группы",
    ps."План заявки", ps."План приезда",
    ps."аккаунт|сайт",
    ps.priezd_arrival_date, ps.prodazhi_arrival_date,
    ps.поставщик, ps._source_table,
    ps.key_pixel_score,
    ps.campaign_status
FROM {T_OUT} ps
LEFT JOIN local_gsheet_sites gs ON LOWER(TRIM(ps.domain)) = LOWER(TRIM(gs."domain"));
"""


# ─── Прямой перенос big_analytics_pixel в big_analytics_full (_source_table='пиксель') ──
# Без атрибуции — строки из big_analytics_pixel переносятся 1:1.
# Параллельно с 'пиксель_атрибуц' (атрибутированные через pixel_score).
_PUSH_PIXEL_DIRECT_TO_FULL_SQL = f"""
DELETE FROM {T_FULL} WHERE _source_table = 'пиксель';

INSERT INTO {T_FULL} (
    key3, "Date", "День недели", week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
    "RlAdjustmentId", "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    account_login, manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто", "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi,
    nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
    direction,
    "неверный_кодер_new", fid,
    проджект, id_салона, менеджер,
    источник, направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки", "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date, prodazhi_arrival_date,
    поставщик, _source_table,
    key_pixel_score,
    campaign_status,
    payment_model
)
SELECT
    p.key3, p."Date", p."День недели", p.week_start,
    p."CampaignId", p."CampaignName", p."AdGroupId", p."AdGroupName",
    p."AdNetworkType", p."Device", p."Impressions", p."Clicks", p.total_cost, p.domain,
    p."RlAdjustmentId", p."RlAdjustmentId_total",
    p.campaign_code, p.tp, p.cpc_cpa, p.site_quiz, p.adgroup_code,
    p.account_login, p.manager_login,
    p.ag_part1, p.ag_part2, p.ag_part3, p.ag_part4, p.ag_part5, p.ag_part6, p.ag_part7,
    p."марки авто", p."Название crm", p.тип_заявки,
    p.kol_vo_zayavok, p.korr, p.kval, p.priezd, p.prodazhi,
    p.nekorr, p.ne_otvechaet, p.filtr, p.nedozvon, p.priedet,
    p.dohod_do_kredita, p.dobro,
    COALESCE(p."статус",     gs."status")        AS "статус",
    COALESCE(p."специалист", gs."directologist") AS "специалист",
    COALESCE(p."тип_сайта",  gs."site_type")     AS "тип_сайта",
    COALESCE(p."шаблон",     gs."template")      AS "шаблон",
    COALESCE(p."салон",      gs."salon")         AS "салон",
    COALESCE(p."город",      gs."city")          AS "город",
    COALESCE(p."регион",     gs."region")        AS "регион",
    p.direction,
    p."неверный_кодер_new", p.fid,
    p.проджект, p.id_салона, p.менеджер,
    'Пиксель'::TEXT                              AS источник,
    p.направление,
    p."номер кампании | название кампании",
    p."номер группы | название группы",
    p."План заявки", p."План приезда",
    p."аккаунт|сайт",
    p.priezd_arrival_date, p.prodazhi_arrival_date,
    p.поставщик,
    'пиксель'::TEXT                              AS _source_table,
    NULL::TEXT                                   AS key_pixel_score,
    p.campaign_status,
    p.payment_model
FROM {T_PIXEL} p
LEFT JOIN local_gsheet_sites gs ON LOWER(TRIM(p.domain)) = LOWER(TRIM(gs."domain"));
"""


# ─── Валидация ─────────────────────────────────────────────────────────────────
# Доменный инвариант: SUM(pixel) == SUM(score) по каждому домену.
_DOMAIN_DIFF_SQL = f"""
WITH
pixel_by_domain AS (
    SELECT domain,
           COALESCE(SUM(kol_vo_zayavok), 0) AS px_z,
           COALESCE(SUM(korr),           0) AS px_korr,
           COALESCE(SUM(kval),           0) AS px_kval,
           COALESCE(SUM(priezd),         0) AS px_priezd,
           COALESCE(SUM(prodazhi),       0) AS px_prodazhi
    FROM {T_PIXEL}
    WHERE domain IS NOT NULL
    GROUP BY domain
),
score_by_domain AS (
    SELECT domain,
           COALESCE(SUM(kol_vo_zayavok), 0) AS sc_z,
           COALESCE(SUM(korr),           0) AS sc_korr,
           COALESCE(SUM(kval),           0) AS sc_kval,
           COALESCE(SUM(priezd),         0) AS sc_priezd,
           COALESCE(SUM(prodazhi),       0) AS sc_prodazhi
    FROM {T_OUT}
    WHERE domain IS NOT NULL
    GROUP BY domain
)
SELECT
    p.domain,
    ABS(p.px_z       - COALESCE(s.sc_z,       0)) AS diff_z,
    ABS(p.px_korr    - COALESCE(s.sc_korr,    0)) AS diff_korr,
    ABS(p.px_kval    - COALESCE(s.sc_kval,    0)) AS diff_kval,
    ABS(p.px_priezd  - COALESCE(s.sc_priezd,  0)) AS diff_priezd,
    ABS(p.px_prodazhi- COALESCE(s.sc_prodazhi,0)) AS diff_prodazhi
FROM pixel_by_domain p
LEFT JOIN score_by_domain s ON s.domain = p.domain
WHERE ABS(p.px_z       - COALESCE(s.sc_z,       0)) > 0.01
   OR ABS(p.px_korr    - COALESCE(s.sc_korr,    0)) > 0.01
   OR ABS(p.px_kval    - COALESCE(s.sc_kval,    0)) > 0.01
   OR ABS(p.px_priezd  - COALESCE(s.sc_priezd,  0)) > 0.01
   OR ABS(p.px_prodazhi- COALESCE(s.sc_prodazhi,0)) > 0.01
ORDER BY diff_z DESC
LIMIT 10
"""


def _check_table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = %s AND table_schema = 'public')",
            (table,),
        )
        return cur.fetchone()[0]


def run(conn, run_id=None, **kwargs) -> dict:
    t0 = time.perf_counter()

    if not _check_table_exists(conn, T_FULL):
        logger.warning('%s не существует — пропускаем step11', T_FULL)
        return {'rows': 0, 'details': f'{T_FULL} missing'}

    if not _check_table_exists(conn, T_PIXEL):
        logger.warning('%s не существует — пропускаем step11', T_PIXEL)
        return {'rows': 0, 'details': f'{T_PIXEL} missing'}

    logger.info(
        'Шаг 11: атрибуция %s → %s → %s (cr_composite=(1·kol_vo+3·korr+10·kval+30·priezd+100·prodazhi)/Clicks, weight=cr_composite/Σ×100)',
        T_PIXEL, T_SCORE, T_OUT,
    )

    rows = leftover_rows = pushed_direct = 0

    # ── STEP11_DISK_WATCHDOG_2026-07-11: не-root защита от забивания диска в 0 ──
    # step11 весь работает в ОДНОЙ транзакции на conn; первый тяжёлый _SCORE_INSERT
    # (hash-aggregate GROUP BY по big_analytics_full) спиллит в pgsql_tmp и падал на
    # ENOSPC за ~18 с при диске ~0. temp_file_limit под bi_analytic не работает (SUSET,
    # permission denied — проверено в step3), поэтому НАСТОЯЩИЙ «чистый abort до ENOSPC»
    # делает фоновый поток: раз в 2 с проверяет free диска и при free < 2 GB отменяет
    # СВОЙ backend (pg_cancel_backend СВОЕГО pid — своя роль/сессия, разрешено). Отмена →
    # PG аварийно завершает запрос → чистит СВОЙ pgsql_tmp → диск восстанавливается,
    # backend жив. Так step11 падает ЧИСТО, НЕ доводя диск до нуля и НЕ оставляя
    # осиротевший temp. Watcher — отдельное соединение (основной conn занят запросами),
    # daemon, fail-safe (своя ошибка → тихий выход, НИКОГДА не вредит основному запросу),
    # try/finally-остановка. Отменяет ТОЛЬКО свой pid, чужие backend'ы не трогает.
    import shutil as _s11_shutil
    _S11_DISK_FLOOR_GB = 2.0
    with conn.cursor() as _pcur:
        _pcur.execute("SELECT pg_backend_pid()")
        _s11_pid = _pcur.fetchone()[0]
    _s11_stop = threading.Event()

    def _s11_disk_watchdog():
        import psycopg2 as _wpg
        from config.settings import DB_DST as _WDB
        _wc = None
        try:
            _wc = _wpg.connect(**_WDB)
            _wc.autocommit = True
            while not _s11_stop.wait(2.0):
                _free = _s11_shutil.disk_usage('/').free / (1024 ** 3)
                if _free < _S11_DISK_FLOOR_GB:
                    with _wc.cursor() as _wcur:
                        _wcur.execute("SELECT pg_cancel_backend(%s)", (_s11_pid,))
                    logger.error(
                        '  STEP11_DISK_WATCHDOG: free=%.2f GB < %.1f GB → '
                        'pg_cancel_backend(%s) — чистый abort ДО ENOSPC',
                        _free, _S11_DISK_FLOOR_GB, _s11_pid
                    )
                    break
        except Exception as _we:  # noqa: F841
            logger.warning('  STEP11_DISK_WATCHDOG: watcher-ошибка (не критично): %s', _we)
        finally:
            if _wc is not None:
                try:
                    _wc.close()
                except Exception:
                    pass

    _s11_wt = threading.Thread(
        target=_s11_disk_watchdog, name='step11-disk-watchdog', daemon=True
    )
    _s11_wt.start()

    try:
        with conn.cursor() as cur:
            # DDL
            cur.execute(_build_score_ddl_sql())
            cur.execute(_build_ddl_sql())

            # STEP11_TEMP_REDUCE_2026-07-11: было SET LOCAL work_mem = 256MB.
            # Теперь 4 GB work_mem + serial (max_parallel_workers_per_gather=0) на ВСЕ
            # тяжёлые запросы step11 (все в этой транзакции) — меньше temp-спилла на диск,
            # бюджет памяти ≤ work_mem×hash_mem_multiplier(2) ≈ 8 GB/узел, без OOM при
            # Swap=0 (serial убирает ×3 параллельных worker'а). SET LOCAL → авто-сброс на
            # commit, downstream на пуловом conn не затрагивается. См. _WM_STEP11.
            cur.execute(f"SET LOCAL work_mem = '{_WM_STEP11}'")
            cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")

            # Шаг 1: наполнить pixel_score (веса/доли)
            cur.execute(_SCORE_INSERT_SQL)
            score_rows = cur.rowcount
            logger.info('Шаг 11: вставлено в %s: %d записей (кампания+месяц)', T_SCORE, score_rows)

            cur.execute(_SCORE_INDEX_SQL)

            # Шаг 1б: рассчитать cpl_score для каждой кампании в pixel_score.
            # DIRECT_SLIM_2026-07-11: источник бенчмарка выбираем В РАНТАЙМЕ.
            # pipeline.py/pipeline_powerbi строят big_analytics_direct_slim после step6
            # (толстый direct TRUNCATE-нут) → читаем slim. fast_pipeline slim не строит
            # (direct TRUNCATE-нут до step11) → fallback на big_analytics_direct (пустой,
            # ровно как сегодня: 0 строк бенчмарка → cpl_score=дефолт, БЕЗ ошибки
            # 'does not exist' и БЕЗ отката транзакции step11).
            cur.execute("SELECT to_regclass(%s)", (f'public.{T_DIRECT_SLIM}',))
            _bench_src = T_DIRECT_SLIM if cur.fetchone()[0] is not None else T_DIRECT
            logger.info(
                'Шаг 11: benchmark source = %s%s',
                _bench_src,
                '' if _bench_src == T_DIRECT_SLIM else ' (fallback: slim отсутствует)',
            )
            cur.execute(_build_score_cpl_update_sql(_bench_src))
            logger.info('Шаг 11: cpl_score обновлён для %d кампаний', cur.rowcount)

            # Шаг 1в: сохранить weight = cpl_score_доля по домену
            cur.execute(_SCORE_WEIGHT_UPDATE_SQL)
            logger.info('Шаг 11: weight обновлён для %d кампаний', cur.rowcount)

            # Шаг 1г: заполнить pixel totals и атрибутированные метрики
            cur.execute(_SCORE_PIXEL_TOTALS_UPDATE_SQL)
            logger.info('Шаг 11: pixel атрибуция обновлена для %d кампаний', cur.rowcount)

            # Шаг 2: наполнить big_analytics_pixel_score (атрибуция через pixel_score)
            cur.execute(_INSERT_SQL)
            rows = cur.rowcount
            logger.info('Шаг 11: вставлено в %s: %d строк', T_OUT, rows)

            # Шаг 2б: остаток — домены без кампаний (CampaignId=NULL, share=1.0)
            cur.execute(_INSERT_LEFTOVERS_SQL)
            leftover_rows = cur.rowcount
            if leftover_rows:
                logger.info('Шаг 11: остаток (нет кампаний) в %s: %d строк', T_OUT, leftover_rows)

            cur.execute(_INDEX_SQL)

            # Шаг 3: перенос пиксель_атрибуц в big_analytics_full
            cur.execute(_PUSH_TO_FULL_SQL)
            pushed = cur.rowcount
            logger.info('Шаг 11: вставлено в %s (пиксель_атрибуц): %d строк', T_FULL, pushed)

            # Backfill "Название crm" для пиксель_атрибуц строк по domain
            # (пиксель_атрибуц не проходит через step6 backfill; берём CRM из других строк того же домена)
            cur.execute(f"""
                UPDATE {T_FULL} f
                SET "Название crm" = src.crm_name
                FROM (
                    SELECT domain, MAX("Название crm") AS crm_name
                    FROM {T_FULL}
                    WHERE _source_table != 'пиксель_атрибуц'
                      AND "Название crm" IS NOT NULL
                      AND "Название crm" NOT IN ('отзывы', 'посевы')
                      AND domain IS NOT NULL
                    GROUP BY domain
                ) src
                WHERE f._source_table = 'пиксель_атрибуц'
                  AND f."Название crm" IS NULL
                  AND f.domain = src.domain
            """)
            logger.info('Шаг 11: backfill "Название crm" (пиксель_атрибуц): %d строк', cur.rowcount)

            # Шаг 3б: прямой перенос big_analytics_pixel в big_analytics_full (_source_table='пиксель')
            cur.execute(_PUSH_PIXEL_DIRECT_TO_FULL_SQL)
            pushed_direct = cur.rowcount
            logger.info('Шаг 11: вставлено в %s (пиксель прямой): %d строк', T_FULL, pushed_direct)

            # Глобальный инвариант (все метрики воронки)
            cur.execute(f"""
                SELECT
                    (SELECT COALESCE(SUM(kol_vo_zayavok), 0) FROM {T_PIXEL}) AS px_z,
                    (SELECT COALESCE(SUM(kol_vo_zayavok), 0) FROM {T_OUT})   AS sc_z,
                    (SELECT COALESCE(SUM(korr),           0) FROM {T_PIXEL}) AS px_korr,
                    (SELECT COALESCE(SUM(korr),           0) FROM {T_OUT})   AS sc_korr,
                    (SELECT COALESCE(SUM(kval),           0) FROM {T_PIXEL}) AS px_kval,
                    (SELECT COALESCE(SUM(kval),           0) FROM {T_OUT})   AS sc_kval,
                    (SELECT COALESCE(SUM(priezd),         0) FROM {T_PIXEL}) AS px_priezd,
                    (SELECT COALESCE(SUM(priezd),         0) FROM {T_OUT})   AS sc_priezd,
                    (SELECT COALESCE(SUM(prodazhi),       0) FROM {T_PIXEL}) AS px_prodazhi,
                    (SELECT COALESCE(SUM(prodazhi),       0) FROM {T_OUT})   AS sc_prodazhi,
                    (SELECT COALESCE(SUM(total_cost),     0) FROM {T_PIXEL}) AS px_cost,
                    (SELECT COALESCE(SUM(total_cost),     0) FROM {T_OUT})   AS sc_cost
            """)
            (px_z, sc_z, px_korr, sc_korr, px_kval, sc_kval,
             px_priezd, sc_priezd, px_prodazhi, sc_prodazhi,
             px_cost, sc_cost) = cur.fetchone()

            # Доменный инвариант
            cur.execute(_DOMAIN_DIFF_SQL)
            domain_diffs = cur.fetchall()

        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _s11_stop.set()
        _s11_wt.join(timeout=5)

    elapsed = time.perf_counter() - t0

    if domain_diffs:
        diff_str = ', '.join(
            f'{row[0]}(z:{row[1]:.2f} korr:{row[2]:.2f} kval:{row[3]:.2f} '
            f'priezd:{row[4]:.2f} prod:{row[5]:.2f})'
            for row in domain_diffs[:3]
        )
        logger.warning(
            'Шаг 11: расхождение по доменам (%d): %s',
            len(domain_diffs), diff_str,
        )

    logger.info(
        'Шаг 11: инвариант z px=%s sc=%s | korr px=%s sc=%s | kval px=%s sc=%s'
        ' | priezd px=%s sc=%s | prodazhi px=%s sc=%s | cost px=%s sc=%s',
        px_z, sc_z, px_korr, sc_korr, px_kval, sc_kval,
        px_priezd, sc_priezd, px_prodazhi, sc_prodazhi, px_cost, sc_cost,
    )
    total_rows = rows + leftover_rows
    logger.info('Шаг 11 завершён: %d строк (атриб=%d, остаток=%d, пиксель_прямой=%d) за %.1f сек',
                total_rows, rows, leftover_rows, pushed_direct, elapsed)

    return {
        'rows': total_rows,
        'leftover_rows': leftover_rows,
        'pushed_direct': pushed_direct,
        'details': (
            f'{T_SCORE}: {score_rows:,} записей | {T_OUT}: {rows:,} атриб + {leftover_rows:,} остаток | '
            f'пиксель_прямой: {pushed_direct:,} строк | '
            f'z: px={px_z} sc={sc_z} | korr: px={px_korr} sc={sc_korr} | '
            f'kval: px={px_kval} sc={sc_kval} | priezd: px={px_priezd} sc={sc_priezd} | '
            f'prodazhi: px={px_prodazhi} sc={sc_prodazhi} | cost: px={px_cost} sc={sc_cost}'
            + (f' | РАСХОЖДЕНИЯ ДОМЕНОВ: {len(domain_diffs)}' if domain_diffs else '')
        ),
    }


def get_explain_sql(conn) -> str:
    """SELECT-эквивалент для EXPLAIN ANALYZE тяжёлого JOIN pixel_score×pixel step11.

    Используется explain_capture при EXPLAIN_CAPTURE=1. Таблицы pixel_score и
    big_analytics_pixel_score уже созданы к моменту вызова (после run()).
    Запрос воспроизводит ключевой JOIN (pixel_score × big_analytics_pixel)
    для профилирования плана атрибуции.
    """
    return f"""
        SELECT
            ps.салон,
            ps.domain,
            ps.month,
            COUNT(ps.*)             AS score_rows,
            SUM(ps.weight)          AS weight_sum,
            SUM(sc.kol_vo_zayavok)  AS attributed_kol_vo,
            SUM(sc.prodazhi)        AS attributed_prodazhi,
            SUM(sc.total_cost)      AS attributed_cost
        FROM {T_SCORE} ps
        LEFT JOIN {T_OUT} sc
            ON sc."салон" = ps.салон
           AND sc.domain  = ps.domain
           AND DATE_TRUNC('month', sc."Date") = ps.month
        GROUP BY ps.салон, ps.domain, ps.month
        ORDER BY attributed_cost DESC NULLS LAST
        LIMIT 1000
    """


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
        result = run(_conn)
        print(result)
    finally:
        db_module.put_conn(_conn)
        db_module.close_pool()
