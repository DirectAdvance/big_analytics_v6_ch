-- VIEW: помесячная KPI-воронка по big_analytics_full
-- Фильтр: direction = 'Авто' (соответствует PBI; equivalent: направление != 'пиксель_атрибуц')
-- Используется dashboard endpoint /api/monthly-kpi
-- Метрики:
--   Клики/Расходы:  clicks, cpc, cost, cost_share_pct
--   Обращения:      obrash, obrash_cpl, obrash_cr_click
--   Заявки:         zayavki, zayavki_share_pct, zayavki_cpl, zayavki_cr_click
--   Квал:           kval, kval_cpl, kval_index, kval_share_pct
--   Приезд:         priezd, priezd_cpl, priezd_index, priezd_share_pct
--   Продажи:        prodazhi, prodazhi_cpl, prodazhi_index, prodazhi_share_pct
-- Строки: месяцы (YYYY-MM) + итог 'TOTAL'.

CREATE OR REPLACE VIEW public.v_monthly_kpi_avto AS
WITH base AS (
    SELECT
        TO_CHAR("Date", 'YYYY-MM')      AS month_key,
        SUM("Clicks")                    AS clicks,
        SUM(total_cost)                  AS cost,
        SUM(kol_vo_zayavok)              AS obrash,
        SUM(korr)                        AS zayavki,
        SUM(kval)                        AS kval,
        SUM(priezd)                      AS priezd,
        SUM(prodazhi)                    AS prodazhi
    FROM public.big_analytics_full
    WHERE direction = 'Авто'
      AND "Date" IS NOT NULL
    GROUP BY 1
),
rolled AS (
    SELECT month_key, clicks, cost, obrash, zayavki, kval, priezd, prodazhi FROM base
    UNION ALL
    SELECT 'TOTAL'  AS month_key,
           SUM(clicks), SUM(cost), SUM(obrash),
           SUM(zayavki), SUM(kval), SUM(priezd), SUM(prodazhi)
    FROM base
),
totals AS (
    SELECT
        SUM(cost)     AS t_cost,
        SUM(zayavki)  AS t_zayavki,
        SUM(kval)     AS t_kval,
        SUM(priezd)   AS t_priezd,
        SUM(prodazhi) AS t_prodazhi,
        COUNT(*)      AS n_months
    FROM base
),
avg_metrics AS (
    SELECT
        AVG(kval)     AS avg_kval,
        AVG(priezd)   AS avg_priezd,
        AVG(prodazhi) AS avg_prodazhi
    FROM base
)
SELECT
    r.month_key,
    -- Клики/Расходы
    r.clicks::bigint                                                  AS clicks,
    CASE WHEN r.clicks > 0 THEN r.cost / r.clicks END                 AS cpc,
    r.cost::numeric                                                   AS cost,
    CASE WHEN t.t_cost > 0 THEN 100.0 * r.cost / t.t_cost END         AS cost_share_pct,
    -- Обращения
    r.obrash::bigint                                                  AS obrash,
    CASE WHEN r.obrash > 0 THEN r.cost / r.obrash END                 AS obrash_cpl,
    CASE WHEN r.clicks > 0 THEN 100.0 * r.obrash / r.clicks END       AS obrash_cr_click,
    -- Заявки
    r.zayavki::bigint                                                 AS zayavki,
    CASE WHEN t.t_zayavki > 0 THEN 100.0 * r.zayavki / t.t_zayavki END AS zayavki_share_pct,
    CASE WHEN r.zayavki > 0 THEN r.cost / r.zayavki END               AS zayavki_cpl,
    CASE WHEN r.clicks > 0 THEN 100.0 * r.zayavki / r.clicks END      AS zayavki_cr_click,
    -- Квал
    r.kval::bigint                                                    AS kval,
    CASE WHEN r.kval > 0 THEN r.cost / r.kval END                     AS kval_cpl,
    CASE WHEN r.month_key = 'TOTAL' THEN NULL
         WHEN a.avg_kval > 0 THEN 100.0 * r.kval / a.avg_kval END     AS kval_index,
    CASE WHEN t.t_kval > 0 THEN 100.0 * r.kval / t.t_kval END         AS kval_share_pct,
    -- Приезд
    r.priezd::bigint                                                  AS priezd,
    CASE WHEN r.priezd > 0 THEN r.cost / r.priezd END                 AS priezd_cpl,
    CASE WHEN r.month_key = 'TOTAL' THEN NULL
         WHEN a.avg_priezd > 0 THEN 100.0 * r.priezd / a.avg_priezd END AS priezd_index,
    CASE WHEN t.t_priezd > 0 THEN 100.0 * r.priezd / t.t_priezd END   AS priezd_share_pct,
    -- Продажи
    r.prodazhi::bigint                                                AS prodazhi,
    CASE WHEN r.prodazhi > 0 THEN r.cost / r.prodazhi END             AS prodazhi_cpl,
    CASE WHEN r.month_key = 'TOTAL' THEN NULL
         WHEN a.avg_prodazhi > 0 THEN 100.0 * r.prodazhi / a.avg_prodazhi END AS prodazhi_index,
    CASE WHEN t.t_prodazhi > 0 THEN 100.0 * r.prodazhi / t.t_prodazhi END AS prodazhi_share_pct
FROM rolled r
CROSS JOIN totals t
CROSS JOIN avg_metrics a
ORDER BY
    CASE WHEN r.month_key = 'TOTAL' THEN 1 ELSE 0 END,
    r.month_key;

COMMENT ON VIEW public.v_monthly_kpi_avto IS
'Помесячная KPI-воронка по big_analytics_full с фильтром direction=Авто. Используется dashboard /api/monthly-kpi. Source: pipeline big_analytics_v5.';
