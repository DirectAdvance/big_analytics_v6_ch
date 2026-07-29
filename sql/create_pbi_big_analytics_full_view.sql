-- Compatibility view for the Power BI model table big_analytics_full.
-- Moves the former Power Query join chain into PostgreSQL.

DROP VIEW IF EXISTS public.pbi_big_analytics_full;

CREATE VIEW public.pbi_big_analytics_full AS
SELECT
    f."Date",
    f."CampaignId",
    f."AdNetworkType",
    f."Device",
    f.total_cost,
    f.campaign_code,
    f.tp,
    f.cpc_cpa,
    f.kol_vo_zayavok AS "Обращения",
    f.korr,
    f.kval,
    f.priezd,
    f.prodazhi,
    f.nekorr,
    f.ne_otvechaet,
    f.filtr,
    f.nedozvon,
    f."RlAdjustmentId",
    COALESCE(adj."RlAdjustmentId_total", '') AS "RlAdjustmentId_total",
    f.fid,
    f."Clicks",
    f."источник",
    f."План заявки",
    f."План приезда",
    f."аккаунт|сайт",
    f."поставщик",
    f.domain AS "домен",
    f.priedet,
    f.dohod_do_kredita,
    f.dobro,
    f."атрибуция",
    f."AdGroupId",
    f."направление",
    f.site_quiz,
    f."марки авто",
    f."специалист",
    f."тип_сайта",
    f."статус",
    f."салон",
    f."шаблон",
    f."id_салона",
    f."город",
    f."регион",
    f."проджект",
    f."менеджер",
    f."Название crm",
    f."тип_заявки",
    f.manager_login,
    COALESCE(dc."CampaignName", '') AS "CampaignName",
    COALESCE(da."AdGroupName", '') AS "AdGroupName",
    COALESCE(dc."account_login", '') AS "account_login",
    COALESCE(da."adgroup_code", '') AS "adgroup_code",
    COALESCE(da."ag_part1", '') AS "ag_part1",
    COALESCE(da."ag_part2", '') AS "ag_part2",
    COALESCE(da."ag_part3", '') AS "ag_part3",
    COALESCE(da."ag_part4", '') AS "ag_part4",
    COALESCE(da."ag_part5", '') AS "ag_part5",
    COALESCE(da."ag_part6", '') AS "ag_part6",
    COALESCE(da."ag_part7", '') AS "ag_part7",
    COALESCE(dc."campaign_status", '') AS "campaign_status",
    COALESCE(dc."payment_model", '') AS "payment_model",
    COALESCE(da."неверный_кодер_new", '') AS "неверный_кодер_new",
    GREATEST(dd.week_start, DATE '2026-01-01') AS week_start,
    COALESCE(dd."День недели", '') AS "День недели",
    COALESCE(da."номер группы | название группы", '') AS "номер группы | название группы",
    COALESCE(dc."номер кампании | название кампании", '') AS "номер кампании | название кампании"
FROM public.fact_big_analytics f
LEFT JOIN public."Dim_Campaign" dc
       ON f."CampaignId" = dc."CampaignId"
LEFT JOIN public."Dim_AdGroup" da
       ON f."AdGroupId" = da."AdGroupId"
LEFT JOIN public."Dim_Date" dd
       ON f."Date" = dd."Date"
LEFT JOIN public."Dim_Adjustment" adj
       ON f."RlAdjustmentId" = adj."RlAdjustmentId";

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bi_analytic') THEN
        GRANT SELECT ON public.pbi_big_analytics_full TO bi_analytic;
    END IF;
END $$;
