"""
step13_arrival/step13.py — воронка big_analytics_full_arrival (по дате визита)

═══════════════════════════════════════════════════════════════════════════════
MIRROR-режим (июнь 2026): BFA = СТРУКТУРНОЕ ЗЕРКАЛО big_analytics_full (73 кол.)
═══════════════════════════════════════════════════════════════════════════════

  Строит big_analytics_full_arrival (BFA) с ТЕМИ ЖЕ 73 колонками что big_analytics_full
  (тот же порядок и типы) — чтобы UNION ALL в big_analytics_unified был тривиальным.
  Date = дата фактического визита (arrival_date), а не дата заявки.

  Главное правило MIRROR: BFA несёт ТОЛЬКО визитную воронку (priezd/prodazhi/...).
  Claim-метрики (расходы и счётчики Direct) в BFA = 0 (аддитивные) либо NULL (id/справки),
  чтобы SUM по визитной партиции в unified их НЕ искажал.

  ──────────────────────────────────────────────────────────────────────────────
  ЧЕТЫРЕ ветки (UNION ALL):

    1. Авто-лиды — из raw_leads + ad-dims из raw_yandex по key3_arrival_date:
         Date = eff_arrival_date (фактический визит по правилам CRM).
         НАПРАВЛЕНИЕ считается логикой step3 (НЕ хардкод 'Контекст'):
           utm пусто/seo-organic → SEO (SEO Flow по статусу); статус SEO/SEO Flow → SEO/SEO Flow;
           иначе (реклам. utm) → Контекст. Пиксель (utm victory_%) исключён (ветка 4).
         utm_* — из лида.
         AD-DIMS (ШАГ 1): подтягиваются из raw_yandex JOIN по
           raw_leads.key3_arrival_date = raw_yandex.key3
           (key3 = Date|CampaignId|AdGroupId|Device|RlAdjustmentId).
         Покрытие на дату ВИЗИТА (честно): CampaignName/Device ~63-70%,
         AdGroupName ~44-49%, марки авто ~35% (производное от adgroup_code), остальное NULL.
         Агрегация ad-dims через MAX по группе (как yd_agg step3) — НЕ плодит строк BFA.

    2. Авто-звонки — из local_leads_all WHERE deal_type='Звонок':
         Звонки не несут key3 → ad-dims NULL (у звонка нет CampaignId/AdGroupId).
         НАПРАВЛЕНИЕ — та же логика step3: звонок без utm → Контекст; seo/organic → SEO;
         статус SEO/SEO Flow учитывается.

    3. Посевная ветка — из big_analytics_full WHERE направление='посевы':
         ДВЕ суб-ветки (POSEV_VISIT_DATESHIFT_2026-06-23):
         3A. MATCHED (date-shift): crop_targeting строки с лидами в posev_pool
             (utm_medium='posev', utm_content=DDMMYYYY) → Date = реальная дата визита
             (распределение по долям v_share/s_share, аналог пикселя, Σdolей=1).
         3B. PROXY (полнота): все остальные посевы as-is — orphan crop (нет лидов
             с DDMMYYYY) + все не-crop (tp8/tp9/tp10/telegram/social_посевы) →
             Date = дата заявки (proxy). Anti-join гарантирует нулевое задвоение.
         Итог: Σ priezd/prodazhi BFA посевов = Σ BAF посевов (полный паритет).
         Claim-метрики посевов (cost/clicks/impressions) → ОБНУЛЯЮТСЯ (0) — посевы это
         расходный канал, а в визитной партиции расходов быть не должно (cost живёт в BAF).

    4. Пиксельная ветка — из big_analytics_full WHERE направление='пиксель_атрибуц'
         (ДРОБНАЯ атрибуция step11, priezd/prodazhi размазаны по кампаниям, NUMERIC —
         дробность СОХРАНЯЕТСЯ, НЕ приводится к int). НЕ 'пиксель' (целое) и не обе сразу
         (несут один расход — дубль). Date = РЕАЛЬНАЯ дата визита (eff_arrival_date), а НЕ
         proxy: у пикселя нет CRM-даты на строке витрины, но визит-лиды пиксель-пула
         (local_leads_all JOIN local_pixel_config, как step5) её несут (~99% crmf).
         DATE-SHIFT: внутри (домен, месяц-заявки) строим распределение визит-лидов по
         eff_arrival_date (v_share для приездов, s_share для продаж, Σ=1 на группу) и
         размножаем каждую дробную строку по этим датам с весами. SUM(priezd)=6196 /
         SUM(prodazhi)=449 ИНВАРИАНТНЫ (перераспределяются по реальным датам, не теряются;
         orphan=0 — все 758 (домен,месяц)-пар матчатся с пулом). eff_arrival_date по CRM:
         plex/marcar→created (proxy), crmf/mega→COALESCE(arrival,created). Claim-расходы=0.
         ВАЖНО: эта ветка читает 'пиксель_атрибуц' из BAF, значит BFA должна
         пересобираться ПОСЛЕ step11 (доливка пиксель_атрибуц) — см. fast_pipeline.

  ──────────────────────────────────────────────────────────────────────────────
  73-КОЛОНОЧНАЯ СХЕМА BFA = точная копия big_analytics_full (для UNION ALL в unified).

  Категории колонок в BFA:
    A. Реальные измерения (есть из лидов/посевов): Date, domain, салон, город, регион,
       direction, специалист, статус, направление, источник, _source_table, тип_заявки,
       (для посевов также: тип_сайта, Название crm, проджект, id_салона, менеджер, поставщик)
    B. Ad-dims: Авто — из raw_yandex по key3_arrival_date (MAX); посевы — из BAF.
       CampaignId/CampaignName/AdGroupId/AdGroupName/Device/campaign_code/tp/cpc_cpa/
       site_quiz/adgroup_code/account_login/manager_login/AdNetworkType/RlAdjustmentId/
       RlAdjustmentId_total/марки авто/шаблон/ag_part1..7/ag_part1_name/fid/key3/
       "номер кампании..."/"номер группы..."/"аккаунт|сайт"/campaign_status/payment_model
    C. Claim-метрики (АДДИТИВНЫЕ → 0 в BFA): Impressions, Clicks, total_cost,
       priezd_arrival_date, prodazhi_arrival_date (BIGINT-счётчики claim → 0).
       ВОРОНКА (kol_vo_zayavok/korr/kval/priezd/prodazhi/nekorr/ne_otvechaet/filtr/
       nedozvon/priedet/dohod_do_kredita/dobro) — РЕАЛЬНАЯ визитная, НЕ обнуляется.
    D. Тех/вычисляемые: День недели + week_start (из Date); неверный_кодер_new=NULL;
       План заявки/План приезда=NULL.

  ──────────────────────────────────────────────────────────────────────────────
  НЕПОКРЫТЫЕ РАЗРЕЗЫ «по визиту» (документировано честно):
    Авто-звонки: ad-dims=NULL (нет key3 на уровне звонка).
    Авто-лиды без match по key3_arrival_date (~30-37%): ad-dims=NULL.
    Марки авто покрываются только там где есть adgroup_code (~35%).
    Это ОЖИДАЕМО — на дату визита Direct-данные привязываются лишь частично.

Зависимости (IN):
  raw_leads                   — лиды (не-звонки) + key3_arrival_date (строится в step1)
  raw_yandex                  — Direct ad-dims + key3 (строится в step1)
  local_leads_all             — все лиды включая звонки (для deal_type='Звонок')
  local_domains               — маппинг domain_id → domain name
  local_gsheet_sites          — маппинг domain → атрибуты
  local_gsheet_priezdi_marcar — дата приезда из Маркар Google Sheet
  big_analytics_full          — готовые posev-строки (направление='посевы', все 73 кол.)

Выход (OUT):
  big_analytics_full_arrival  — регулярная таблица, 73 колонки = зеркало big_analytics_full

Запуск: pipeline.py --only-step=13

ВАЖНО: посевная ветка читает big_analytics_full → step13 должен запускаться ПОСЛЕ
step10 (load_crop). Сборка big_analytics_unified — отдельный шаг build_unified.run()
ПОСЛЕ step13 (когда BAF+BFA финализированы и нормализованы).
"""

import logging
import threading
import time

from config.db import get_conn, put_conn
from config.settings import (
    T_RAW_LEADS,
    T_RAW_YANDEX,
    T_GSHEET_SITES,
    T_GSHEET_PRIEZDI,
    T_FULL_ARRIVAL,
    T_FULL,
    T_LEADS_ALL_LOCAL,
    T_LEADS_CROP_ATTR,
    T_DOMAINS_LOCAL,
    WORK_MEM,
    CDR_PATTERN,  # CDR_SPLIT_2026-07-27: единая константа POSIX-паттерна CDR
)
from config.status_sql import load_status_sql
from corrections import (
    _KUDЕРКО_DATE, _KUDЕРКО_NAME, _KUDЕРКО_LOGINS,
    _СЕРГЕЕВ_DATE, _СЕРГЕЕВ_NAME, _СЕРГЕЕВ_LOGINS,
    _ПИТЕРКИНА_LOGIN, _ПИТЕРКИНА_NAME,
)

logger = logging.getLogger('pipeline.step13')

# Русские названия дней недели по ISODOW (1=пн … 7=вс), формат как в big_analytics_full.
def _DOW_SQL_F(date_expr: str) -> str:
    """День недели ('N_Название') из произвольного date-выражения."""
    return (
        f"(EXTRACT(ISODOW FROM {date_expr})::int)::text || '_' || "
        "(ARRAY['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'])"
        f"[EXTRACT(ISODOW FROM {date_expr})::int]"
    )


_DOW_SQL = _DOW_SQL_F('"Date"')


def _build_arrival_sql(leads_agg_cases: str, priezd_statuses_sql: str) -> str:
    """
    Построить SQL создания big_analytics_full_arrival (73-колоночное зеркало).

    Авто-ветки (лиды/звонки): ad-dims из raw_yandex по key3_arrival_date через MAX
    (агрегаты, НЕ в GROUP BY → строк не прибавляют). Посевы: as-is из big_analytics_full
    с обнулением claim-расходов.

    leads_agg_cases / calls подставляются генератором status_sql:
      kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr,
      nedozvon, priedet, dohod_do_kredita, dobro
    """
    return f"""
WITH

-- ── yd_agg: ad-dims по key3 (как step3 yd_agg) ────────────────────────────
-- MAX по key3: один набор ad-dims на ключ Date|Campaign|AdGroup|Device|Adjustment.
yd_agg AS (
    SELECT
        key3,
        MAX("CampaignId")    AS "CampaignId",
        MAX("CampaignName")  AS "CampaignName",
        MAX("AdGroupId")     AS "AdGroupId",
        MAX("AdGroupName")   AS "AdGroupName",
        MAX("AdNetworkType") AS "AdNetworkType",
        MAX("Device")        AS "Device",
        MAX("RlAdjustmentId")AS "RlAdjustmentId",
        MAX(campaign_code)   AS campaign_code,
        MAX(tp)              AS tp,
        MAX(cpc_cpa)         AS cpc_cpa,
        MAX(site_quiz)       AS site_quiz,
        MAX(adgroup_code)    AS adgroup_code,
        MAX(account_login)   AS account_login,
        MAX(manager_login)   AS manager_login
    FROM {T_RAW_YANDEX}
    GROUP BY key3
),

-- ── camp_dict/ag_dict: DATE-INDEPENDENT справочник имён из FDW ─────────────
-- VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17: переведено с local_yandex на
-- FOREIGN TABLE yandex_direct_manager_reports (FDW напрямую).
-- local_yandex была ПОСТОЯННОЙ таблицей (между прогонами raw_yandex пуст).
-- Теперь FDW читается по сети при каждом standalone-прогоне step13 — это OK:
-- step13 запускается внутри pipeline, когда FDW доступен. Standalone-прогон
-- step13 между прогонами тоже потянет FDW по сети — задокументировано.
-- ROOTCAUSE (ARRIVAL_AD_DIMS_NULL): yd_agg резолвит имена ТОЛЬКО через key3 с
-- датой ВИЗИТА — если в день визита по кампании не было открутки, расходной
-- строки нет → JOIN NULL → CampaignName/AdGroupName/Device теряются, хотя лид
-- реально несёт campaign_id/group_id. FDW держит маппинг campaign_id→name и
-- adgroup_id→name для всего периода → безопасно MAX-агрегировать в справочник.
-- Инлайн-касты: "Date"::DATE (в FDW Date = text); adgroup_code вычисляем regex
-- из "AdGroupName" (в FDW нет отдельного столбца adgroup_code).
camp_dict AS (
    SELECT "CampaignId" AS cid,
           MAX("CampaignName")                     AS "CampaignName",
           MAX(account_login)                      AS account_login,
           MAX(manager_login)                      AS manager_login,
           -- ARRIVAL_CODE_NULL FIX (2026-06-10): campaign_code/tp/cpc_cpa/site_quiz —
           -- date-independent fallback. Парсим из CampaignName ТЕМ ЖЕ regex, что step1.
           MAX(REPLACE(REPLACE(
               (REGEXP_MATCH(REPLACE("CampaignName", chr(1089), 'c'),
                   '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'))[1],
               'kviz', 'quiz'), 'Kviz', 'Quiz')) AS campaign_code,
           MAX(LOWER(SPLIT_PART(
               COALESCE((REGEXP_MATCH(REPLACE("CampaignName", chr(1089), 'c'),
                   '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'))[1], ''),
               '_', 1))) AS tp,
           MAX(LOWER(SPLIT_PART(
               COALESCE((REGEXP_MATCH(REPLACE("CampaignName", chr(1089), 'c'),
                   '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'))[1], ''),
               '_', 2))) AS cpc_cpa,
           MAX(REPLACE(LOWER(SPLIT_PART(
               COALESCE((REGEXP_MATCH(REPLACE("CampaignName", chr(1089), 'c'),
                   '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'))[1], ''),
               '_', 3)), 'kviz', 'quiz')) AS site_quiz
    FROM public.yandex_direct_manager_reports
    WHERE "CampaignId" IS NOT NULL
    GROUP BY "CampaignId"
),
ag_dict AS (
    -- adgroup_code вычисляем из "AdGroupName" (в FDW нет отдельного столбца).
    -- Та же regex, что в step1._build_raw_yandex_sql.
    SELECT "AdGroupId" AS agid,
           MAX("AdGroupName") AS "AdGroupName",
           MAX((REGEXP_MATCH(
               SPLIT_PART("AdGroupName", ' — ', 1),
               '(ct\\d+_(?:aoff|aon)_n\\d+_r\\d+_ct\\d+_ag\\d+_g\\d+)'
           ))[1]) AS adgroup_code
    FROM public.yandex_direct_manager_reports
    WHERE "AdGroupId" IS NOT NULL
    GROUP BY "AdGroupId"
),

-- ── Маркар: дата приезда по ID заявки из ссылки ──────────────────────────
-- MARCAR_ID_JOIN_FIX_2026-06-18: матч ТОЛЬКО по ID заявки из ссылки CRM.
-- Телефонный матч убран полностью: raw_leads.phone = 8 цифр vs gsheet 11 цифр
-- → покрытие было 8.3%; ключ по record_id даёт 89.3% (1349/1510 строк 2026+).
-- Форматы ссылок: crm.marcar.ru/leads/<id> (1603 строк), app.plex-crm.ru/pipelines/<id>
-- (13 строк, все 2025-12 — вне DATE_FROM=2026-01-01, фактически мертвы).
-- Регулярка '^.*/([0-9]+)$' покрывает ОБА хоста.
-- Даты в gsheet — ISO 'YYYY-MM-DD' (все 1687 непустых строк; 'DD.MM.YYYY' НЕТ).
-- DISTINCT ON (record_id): берём запись с наиболее поздней датой визита,
-- при равной дате — приоритет по статусу (Продажа > Одобрение > Приехал).
marcar_arrivals AS (
    SELECT DISTINCT ON (record_id)
        REGEXP_REPLACE(link, '^.*/([0-9]+)$', '\\1') AS record_id,
        TO_DATE(NULLIF(TRIM(date), ''), 'YYYY-MM-DD') AS arrival_date_marcar,
        status
    FROM {T_GSHEET_PRIEZDI}
    WHERE link IS NOT NULL
      AND link ~ '^.*/[0-9]+$'
      AND date IS NOT NULL
      AND TRIM(date) ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
    ORDER BY record_id,
             TO_DATE(NULLIF(TRIM(date), ''), 'YYYY-MM-DD') DESC NULLS LAST,
             CASE status
                 WHEN 'Продажа'    THEN 1
                 WHEN 'Одобрение'  THEN 2
                 WHEN 'Дошел в КО' THEN 3
                 ELSE 4
             END
),

-- ── Базовые лиды + атрибуты домена + key3_arrival_date ────────────────────
leads_base AS (
    SELECT
        l.id,
        l.status,
        l.reason,
        l.source_type,
        l.created_date,
        l.arrival_date,
        l.domain,
        l.utm_source,
        l.utm_medium,
        l.utm_campaign,
        l.utm_content,
        -- CDR_SPLIT_2026-07-27: расширенный паттерн (CDR_PATTERN из config/settings.py)
        (COALESCE(l.utm_content, '') ~* '{CDR_PATTERN}') AS zvonki_cdr,
        l.key3_arrival_date,
        l.campaign_id,
        l.group_id,
        -- MARCAR_ID_JOIN_FIX_2026-06-18: source_record_id из local_leads_all для матча
        -- по ID заявки из gsheet-ссылки. raw_leads.id = local_leads_all.id (прямая
        -- проекция в step1), JOIN 1:1 по id → fan-out исключён, строк не прибавляет.
        -- phone убран: raw_leads.phone = 8 цифр, gsheet = 11 цифр → матч мертв.
        la.source_record_id,
        gs.salon      AS "салон",
        gs.city       AS "город",
        gs.region     AS "регион",
        gs.direction,
        gs.directologist AS "специалист",
        gs.status     AS "статус",
        -- FIX тип_сайта (2026-06-10): site_type из того же справочника, что салон/город.
        -- Домены в local_gsheet_sites WHERE direction='Авто' УНИКАЛЬНЫ (4090=4090 distinct,
        -- 0 дублей) → JOIN 1:1, fan-out исключён, новых джойнов НЕ добавляем.
        gs.site_type  AS "тип_сайта",
        -- КАТЕГОРИЯ A (2026-06-11): справочные атрибуты салона из ТОГО ЖЕ gs-JOIN'а
        -- (1:1 по Авто-домену, fan-out исключён, второй JOIN НЕ добавляем). Симметрия
        -- с claim-осью step6:169/173-175 / step3:846/850-852. Детерминированы на домен
        -- → проходят как MAX в leads_scored (строк не плодят).
        gs.template        AS "шаблон",
        NULLIF(TRIM(gs.project_manager), '') AS "проджект",
        gs.client_id       AS "id_салона",
        NULLIF(TRIM(gs.sales_manager), '')   AS "менеджер"
    FROM {T_RAW_LEADS} l
    -- MARCAR_ID_JOIN_FIX_2026-06-18: дотягиваем source_record_id из local_leads_all.
    -- raw_leads.id = local_leads_all.id по construction (step1 SELECT l.id FROM la).
    -- LEFT JOIN: для лидов без source_record_id (crmf/plex/mega) la.source_record_id=NULL
    -- → gsheet-матч не сработает → eff_arrival_date остаётся как было (без изменений).
    LEFT JOIN {T_LEADS_ALL_LOCAL} la ON la.id = l.id
    -- VISIT_CROP_DEDUP_2026-07-23: crop-атрибутированные лиды не должны
    -- повторно заходить в auto direct-ветку визитной оси.
    LEFT JOIN {T_LEADS_CROP_ATTR} lca ON l.id = lca.lead_id
    JOIN {T_GSHEET_SITES} gs
        ON LOWER(TRIM(gs.domain)) = l.domain
    WHERE gs.direction = 'Авто'
      AND l.status IS NOT NULL
      AND l.status != ''
      AND (lca.lead_id IS NULL OR lca.is_crop = FALSE)
      -- PATCH-CRMF-LIDER-DEDUP-2026-06-15: исключаем crmf-дубли «Лидер»,
      -- помеченные run_dedup_crmf_lider() до step3. Симметрично step3 leads_deduped.
      AND l.is_copy_for_removal IS NOT TRUE
      -- VISIT_CROP_DEDUP_2026-07-23: Max/VK/Telegram посевы идут только через
      -- посевную ветку BAF→BFA, а не как direct на визитной оси.
      AND NOT (l.utm_source IN ('telegram', 'stories_tg')
               AND l.utm_medium = 'posev')
      AND NOT (l.utm_source IN ('vk_storis', 'telegram_storis')
               AND l.utm_medium = 'posev')
      AND NOT (l.utm_source IN ('max', 'vk', 'vk_groups')
               AND l.utm_medium IN ('posev', 'paid_social'))
),

-- ── Эффективная дата приезда по source_type ───────────────────────────────
leads_with_eff AS (
    SELECT
        lb.id,
        lb.status,
        lb.reason,
        lb.source_type,
        lb.domain,
        lb."салон",
        lb."город",
        lb."регион",
        lb.direction,
        lb."специалист",
        lb."статус",
        lb."тип_сайта",
        lb."шаблон",
        lb."проджект",
        lb."id_салона",
        lb."менеджер",
        lb.utm_source,
        lb.utm_medium,
        lb.utm_campaign,
        lb.utm_content,
        lb.zvonki_cdr,
        lb.key3_arrival_date,
        lb.campaign_id,
        lb.group_id,
        -- MARCAR_ID_JOIN_FIX_2026-06-18: source_record_id вместо phone_norm.
        -- phone_norm убран (8 vs 11 цифр → мертвый матч).
        lb.source_record_id,
        -- MARCAR_STRICT_ARRIVAL_2026-07-17 (ред. 2, решение Семёна): created_date здесь —
        -- ТОЛЬКО ПОЗИЦИОНИРОВАНИЕ строки на оси, а НЕ засчитанный визит. Fallback как
        -- ИСТОЧНИК ДАТЫ ВИЗИТА у Маркара не действует: сам визит (priezd/prodazhi)
        -- засчитывается лишь при наличии строки в local_gsheet_priezdi_marcar — за это
        -- отвечает гейт marcar_visit_ok (leads_eff ниже) → visit_gate_sql в leads_agg_cases.
        -- Так лид остаётся на визит-оси со своими kol_vo_zayavok/korr/kval, но без приезда.
        CASE
            WHEN lb.source_type IN ('plex_excel', 'marcar_crm_excel')
                THEN lb.created_date
            ELSE COALESCE(lb.arrival_date, lb.created_date)
        END AS eff_arrival_date_base,
        lb.arrival_date AS arrival_date_raw,
        lb.created_date
    FROM leads_base lb
),

-- ── Финальная eff_arrival_date + Маркар JOIN + ad-dims из yd_agg ───────────
leads_eff AS (
    SELECT
        lwe.id,
        lwe.status,
        lwe.reason,
        lwe.source_type,
        lwe.domain,
        lwe."салон",
        lwe."город",
        lwe."регион",
        lwe.direction,
        lwe."специалист",
        lwe."статус",
        lwe."тип_сайта",
        lwe."шаблон",
        lwe."проджект",
        lwe."id_салона",
        lwe."менеджер",
        lwe.utm_source,
        lwe.utm_medium,
        lwe.utm_campaign,
        lwe.utm_content,
        lwe.zvonki_cdr,
        -- AD-DIMS (FIX ARRIVAL_AD_DIMS_NULL 2026-06-09): резолв через COALESCE(
        -- дневной yd_agg по key3_arrival_date, date-independent справочник из
        -- local_yandex по сырым campaign_id/group_id лида). Справочник = FALLBACK:
        -- когда в день визита не было открутки по кампании, дневной y.* = NULL, но
        -- camp_dict/ag_dict резолвят имя по id лида. ID берём напрямую с лида
        -- (lwe.campaign_id/group_id) — они date-independent. Device: дневной ИЛИ
        -- парсинг dev:-токена utm_content (та же логика что step1 key3, значения
        -- mobile/desktop/tablet/smart_tv совпадают с local_yandex.Device).
        -- Расход НЕ трогаем (cost/clicks=0 в BFA by design). Имена идут как MAX-
        -- агрегаты в leads_scored → строк НЕ плодят.
        COALESCE(y."CampaignId", lwe.campaign_id)        AS "CampaignId",
        COALESCE(y."CampaignName", cd."CampaignName")     AS "CampaignName",
        COALESCE(y."AdGroupId", lwe.group_id)             AS "AdGroupId",
        COALESCE(y."AdGroupName", ad."AdGroupName")       AS "AdGroupName",
        y."AdNetworkType",
        COALESCE(
            y."Device",
            CASE
                WHEN lwe.utm_content LIKE '%dev:mobile%'   THEN 'mobile'
                WHEN lwe.utm_content LIKE '%dev:desktop%'  THEN 'desktop'
                WHEN lwe.utm_content LIKE '%dev:tablet%'   THEN 'tablet'
                WHEN lwe.utm_content LIKE '%dev:smart_tv%' THEN 'smart_tv'
                ELSE NULL
            END
        )                                                 AS "Device",
        y."RlAdjustmentId",
        -- CODE FALLBACK (ARRIVAL_CODE_NULL FIX 2026-06-10): дневной yd_agg y.* пуст,
        -- когда в день визита по кампании не было открутки → код терялся, хотя лид несёт
        -- campaign_id. Берём дневной, иначе date-independent из camp_dict (по cid лида).
        -- NULLIF(...,'') — step1 кладёт tp/cpc_cpa/site_quiz='' (не NULL) при не-матче regex,
        -- так что дневное '' трактуем как «нет значения» и тоже падаем на справочник.
        -- Симметрично CampaignName-фоллбэку (стр.310). camp_dict 1:1 по cid → fan-out нет.
        COALESCE(NULLIF(y.campaign_code, ''), NULLIF(cd.campaign_code, '')) AS campaign_code,
        COALESCE(NULLIF(y.tp, ''),           NULLIF(cd.tp, ''))           AS tp,
        COALESCE(NULLIF(y.cpc_cpa, ''),      NULLIF(cd.cpc_cpa, ''))      AS cpc_cpa,
        COALESCE(NULLIF(y.site_quiz, ''),    NULLIF(cd.site_quiz, ''))    AS site_quiz,
        COALESCE(y.adgroup_code, ad.adgroup_code)         AS adgroup_code,
        COALESCE(y.account_login, cd.account_login)       AS account_login,
        COALESCE(y.manager_login, cd.manager_login)       AS manager_login,
        -- MARCAR_ID_JOIN_FIX_2026-06-18: дата визита из gsheet по record_id.
        -- ma.arrival_date_marcar = NULL если лид не матчится → COALESCE → eff_arrival_date_base.
        -- GREATEST гарантирует: дата визита >= created_date (защита от аномальных gsheet дат).
        GREATEST(COALESCE(ma.arrival_date_marcar, lwe.eff_arrival_date_base), lwe.created_date) AS eff_arrival_date,
        -- MARCAR_STRICT_ARRIVAL_2026-07-17 (ред. 2): ГЕЙТ ВИЗИТА (решение Семёна).
        -- Маркар-лид БЕЗ строки в local_gsheet_priezdi_marcar остаётся на визит-оси
        -- (позиционируется по created_date выше) со своими kol_vo_zayavok/korr/kval,
        -- но приезд/продажа ему НЕ засчитываются: FALSE здесь → priezd=0 И prodazhi=0
        -- (visit_gate_sql в _build_leads_agg сужает ровно эти две метрики).
        -- Единственный источник ДАТЫ ВИЗИТА Маркара — gsheet; created_date как источник
        -- даты визита не возвращается.
        -- ⚠️ COALESCE(source_type,'') — защита от NULL-логики: голое
        -- `source_type <> 'marcar_crm_excel'` при source_type IS NULL даёт NULL → OR с
        -- NULL → NULL → гейт молча погасил бы priezd у лидов с NULL source_type.
        -- Не-marcar ветки всегда TRUE (ma.* там NULL: JOIN ограничен marcar_crm_excel),
        -- поэтому plex/crmf/mega/посевы/пиксель не затронуты.
        (COALESCE(lwe.source_type, '') <> 'marcar_crm_excel'
         OR ma.arrival_date_marcar IS NOT NULL) AS marcar_visit_ok
    FROM leads_with_eff lwe
    LEFT JOIN marcar_arrivals ma
        ON lwe.source_type = 'marcar_crm_excel'
       AND lwe.source_record_id = ma.record_id
       AND ma.arrival_date_marcar IS NOT NULL
    LEFT JOIN yd_agg y
        ON y.key3 = lwe.key3_arrival_date
    -- date-independent справочники имён (fallback к дневному yd_agg)
    LEFT JOIN camp_dict cd ON cd.cid  = lwe.campaign_id
    LEFT JOIN ag_dict   ad ON ad.agid = lwe.group_id
),

-- ── Скоринг лидов: GROUP BY измерения, ad-dims через MAX ───────────────────
leads_scored AS (
    SELECT
        eff_arrival_date AS "Date",
        domain,
        "салон",
        "город",
        "регион",
        direction,
        NULLIF(TRIM("специалист"), '') AS "специалист",
        "статус",
        "тип_сайта",
        -- КАТЕГОРИЯ A: справочные атрибуты салона (детерминированы на домен →
        -- MAX в группе строк НЕ плодит; "тип_сайта" — в GROUP BY, остальные нет).
        MAX("шаблон")    AS "шаблон",
        MAX("проджект")  AS "проджект",
        MAX("id_салона") AS "id_салона",
        MAX("менеджер")  AS "менеджер",
        -- Название crm: per-row CASE по сырому source_type (Маркар>Мега>Фаиг>Плекс),
        -- агрегируем MAX по приоритету (1=Маркар наивысший). Симметрично claim dst
        -- (step6:164/step3:840). 1:1 без JOIN — fan-out исключён.
        (ARRAY_AGG(
            CASE source_type
                WHEN 'marcar_crm_excel' THEN 'Маркар'
                WHEN 'mega_crm_excel'   THEN 'Мега'
                WHEN 'crmf_excel'       THEN 'Фаиг'
                WHEN 'plex_excel'       THEN 'Плекс'
                ELSE source_type
            END
            ORDER BY CASE source_type
                WHEN 'marcar_crm_excel' THEN 1
                WHEN 'mega_crm_excel'   THEN 2
                WHEN 'crmf_excel'       THEN 3
                WHEN 'plex_excel'       THEN 4
                ELSE 9 END
        ))[1] AS "Название crm",
        -- CDR_SPLIT_2026-07-27: zvonki_cdr в GROUP BY → используем напрямую (не BOOL_OR)
        CASE WHEN zvonki_cdr THEN 'Звонки_CDR' ELSE 'Заявки' END AS "тип_заявки",
        'direct'::TEXT AS source_type,
        -- НАПРАВЛЕНИЕ/ИСТОЧНИК: логика step3 (НЕ хардкод 'Контекст').
        -- Зависит от utm_source/utm_medium + gs.status — все три в GROUP BY,
        -- поэтому выражение детерминировано на группу.
        --   utm пусто ИЛИ seo/organic                  → SEO (или SEO Flow по статусу)
        --   gs.status='SEO'                            → SEO
        --   gs.status='SEO Flow'                       → SEO Flow
        --   иначе (есть реклам. utm)                   → Контекст
        -- Пиксельные лиды (utm_source LIKE 'victory_%') исключены в WHERE ниже —
        -- их визит-сторона строится отдельной proxy-веткой (направление='пиксель').
        -- ВАЖНО: внутри CASE используем ТЕ ЖЕ выражения, что в GROUP BY ниже
        -- (NULLIF(TRIM(utm_source),'') / NULLIF(TRIM(utm_medium),'')), а НЕ голые
        -- utm_source/utm_medium — иначе Postgres считает их негруппированными
        -- (GroupingError: must appear in GROUP BY). NULLIF(TRIM(x),'') IS NULL
        -- эквивалентно (x IS NULL OR TRIM(x)='') — семантика сохранена.
        -- KOMPLEKS_REFACTOR_REDO_2026-07-09: направление → 'Комплекс'; источник = SEO/SEO Flow/Контекст
        -- FIX1_ORGANIC_SEO_SYMMETRY: органические лиды (utm NULL OR seo/organic) всегда → 'SEO'/'SEO Flow',
        -- никогда не 'Контекст' — симметрия с step3._build_seo_sql (ELSE→'SEO', а не 'Контекст').
        -- VISIT_PERFORM_DIRECTION_2026-07-10: зеркалим corrections._rule_perform_direction на визит-оси —
        -- Перформ (id_салона='avto_0415') НЕ сваливаем в 'Комплекс'. id_салона детерминирован на домен
        -- (gs.client_id, 1:1), domain в GROUP BY → MAX("id_салона") = единственное значение группы,
        -- строк НЕ плодит. Переклейка ТОЛЬКО метки Комплекс↔Перформ внутри визит-оси (priezd/prodazhi инвариантны).
        CASE WHEN MAX("id_салона") = 'avto_0415' THEN 'Перформ'::TEXT ELSE 'Комплекс'::TEXT END AS "направление",
        (CASE
            WHEN NULLIF(TRIM(utm_source), '') IS NULL
              OR (NULLIF(TRIM(utm_source), '') = 'seo' AND NULLIF(TRIM(utm_medium), '') = 'organic')
                THEN CASE WHEN "статус" = 'SEO Flow' THEN 'SEO Flow' ELSE 'SEO' END
            WHEN "статус" = 'SEO Flow' THEN 'SEO Flow'
            WHEN "статус" = 'SEO'      THEN 'SEO'
            ELSE 'Контекст'
        END)::TEXT AS "источник",
        NULLIF(TRIM(utm_source), '')   AS utm_source,
        NULLIF(TRIM(utm_medium), '')   AS utm_medium,
        NULLIF(TRIM(utm_campaign), '') AS utm_campaign,
        NULLIF(TRIM(utm_content), '')  AS utm_content,
        -- ad-dims: MAX внутри группы (не плодит строк; покрытие частичное)
        MAX("CampaignId")    AS "CampaignId",
        MAX("CampaignName")  AS "CampaignName",
        MAX("AdGroupId")     AS "AdGroupId",
        MAX("AdGroupName")   AS "AdGroupName",
        MAX("AdNetworkType") AS "AdNetworkType",
        MAX("Device")        AS "Device",
        MAX("RlAdjustmentId")AS "RlAdjustmentId",
        MAX(campaign_code)   AS campaign_code,
        MAX(tp)              AS tp,
        MAX(cpc_cpa)         AS cpc_cpa,
        MAX(site_quiz)       AS site_quiz,
        MAX(adgroup_code)    AS adgroup_code,
        MAX(account_login)   AS account_login,
        MAX(manager_login)   AS manager_login,
        {leads_agg_cases}
    FROM leads_eff
    WHERE eff_arrival_date IS NOT NULL
      -- Пиксель идёт отдельной proxy-веткой (направление='пиксель' из big_analytics_full)
      AND (utm_source IS NULL OR utm_source NOT LIKE 'victory_%')
    GROUP BY
        eff_arrival_date, domain, "салон", "город", "регион", direction,
        NULLIF(TRIM("специалист"), ''), "статус", "тип_сайта",
        NULLIF(TRIM(utm_source), ''), NULLIF(TRIM(utm_medium), ''),
        NULLIF(TRIM(utm_campaign), ''), NULLIF(TRIM(utm_content), ''),
        zvonki_cdr  -- CDR_SPLIT_2026-07-27: расщепление строки по CDR на визит-оси
),

-- VISIT_CROP_DEDUP_2026-07-23: tp8/tp9/tp10 уже перенесены из direct в crop на
-- claim-оси; на визитной оси они тоже должны жить только в посевной ветке.
leads_scored_direct_only AS (
    SELECT *
    FROM leads_scored
    WHERE COALESCE(tp, '') NOT IN ('tp8', 'tp9', 'tp10')
),

-- ── Звонки: базовые данные + атрибуты домена (без key3 → ad-dims NULL) ─────
calls_base AS (
    SELECT
        l.id,
        l.status,
        l.reason,
        l.source_type,
        ld.name AS domain,
        l.utm_source,
        l.utm_medium,
        l.utm_campaign,
        l.utm_content,
        -- MARCAR_ID_JOIN_FIX_2026-06-18: source_record_id для матча marcar-звонков
        -- по ID заявки из gsheet. local_leads_all читается напрямую → поле доступно.
        l.source_record_id,
        gs.salon      AS "салон",
        gs.city       AS "город",
        gs.region     AS "регион",
        gs.direction,
        gs.directologist AS "специалист",
        gs.status     AS "статус",
        -- FIX тип_сайта (2026-06-10): site_type из того же gs (Авто-домены уникальны → 1:1)
        gs.site_type  AS "тип_сайта",
        -- КАТЕГОРИЯ A (2026-06-11): справочные атрибуты салона из того же gs (1:1, без 2-го JOIN)
        gs.template        AS "шаблон",
        NULLIF(TRIM(gs.project_manager), '') AS "проджект",
        gs.client_id       AS "id_салона",
        NULLIF(TRIM(gs.sales_manager), '')   AS "менеджер",
        l.created_date,
        l.arrival_date
    FROM {T_LEADS_ALL_LOCAL} l
    JOIN {T_DOMAINS_LOCAL} ld ON ld.id = l.domain_id
    JOIN {T_GSHEET_SITES} gs
        ON LOWER(TRIM(gs.domain)) = LOWER(TRIM(ld.name))
    WHERE l.deal_type = 'Звонок'
      AND gs.direction = 'Авто'
      AND l.status IS NOT NULL
      AND l.status != ''
),

calls_eff AS (
    SELECT
        cb.id,
        cb.status,
        cb.reason,
        cb.source_type,
        cb.domain,
        cb."салон",
        cb."город",
        cb."регион",
        cb.direction,
        cb."специалист",
        cb."статус",
        cb."тип_сайта",
        cb."шаблон",
        cb."проджект",
        cb."id_салона",
        cb."менеджер",
        cb.utm_source,
        cb.utm_medium,
        cb.utm_campaign,
        cb.utm_content,
        -- MARCAR_ID_JOIN_FIX_2026-06-18: для marcar-звонков берём дату из gsheet,
        -- если матч есть. Иначе fallback = created_date (было раньше для всех marcar).
        -- GREATEST: гарантируем дату >= created_date (защита от аномалий gsheet).
        -- MARCAR_STRICT_ARRIVAL_2026-07-17 (ред. 2): created_date у marcar здесь —
        -- ТОЛЬКО позиционирование строки, приезд гасит гейт marcar_visit_ok (ниже).
        CASE
            WHEN cb.source_type = 'marcar_crm_excel'
                THEN GREATEST(COALESCE(ma.arrival_date_marcar, cb.created_date), cb.created_date)
            WHEN cb.source_type = 'plex_excel'
                THEN cb.created_date
            ELSE GREATEST(COALESCE(cb.arrival_date, cb.created_date), cb.created_date)
        END AS eff_arrival_date,
        -- MARCAR_STRICT_ARRIVAL_2026-07-17 (ред. 2): гейт визита, симметрично ветке лидов
        -- (через calls идут 49 из 221 гасимых приездов). FALSE → priezd=0 И prodazhi=0,
        -- заявочные счётчики звонка сохраняются. COALESCE — защита от NULL-логики.
        (COALESCE(cb.source_type, '') <> 'marcar_crm_excel'
         OR ma.arrival_date_marcar IS NOT NULL) AS marcar_visit_ok
    FROM calls_base cb
    LEFT JOIN marcar_arrivals ma
        ON cb.source_type = 'marcar_crm_excel'
       AND cb.source_record_id = ma.record_id
       AND ma.arrival_date_marcar IS NOT NULL
),

calls_scored AS (
    SELECT
        eff_arrival_date AS "Date",
        domain,
        "салон",
        "город",
        "регион",
        direction,
        NULLIF(TRIM("специалист"), '') AS "специалист",
        "статус",
        "тип_сайта",
        -- КАТЕГОРИЯ A: справочные атрибуты салона (детерминированы на домен → MAX/агрегат
        -- строк не плодит). Симметрия с claim step6:169/173-175.
        MAX("шаблон")    AS "шаблон",
        MAX("проджект")  AS "проджект",
        MAX("id_салона") AS "id_салона",
        MAX("менеджер")  AS "менеджер",
        (ARRAY_AGG(
            CASE source_type
                WHEN 'marcar_crm_excel' THEN 'Маркар'
                WHEN 'mega_crm_excel'   THEN 'Мега'
                WHEN 'crmf_excel'       THEN 'Фаиг'
                WHEN 'plex_excel'       THEN 'Плекс'
                ELSE source_type
            END
            ORDER BY CASE source_type
                WHEN 'marcar_crm_excel' THEN 1
                WHEN 'mega_crm_excel'   THEN 2
                WHEN 'crmf_excel'       THEN 3
                WHEN 'plex_excel'       THEN 4
                ELSE 9 END
        ))[1] AS "Название crm",
        'calls'::TEXT AS source_type,
        -- НАПРАВЛЕНИЕ/ИСТОЧНИК звонков: та же логика step3.
        -- Звонок без UTM → Контекст; seo/organic → SEO; статус SEO/SEO Flow учитывается.
        -- ВАЖНО: те же выражения NULLIF(TRIM(...)) что в GROUP BY (см. ветку лидов).
        -- KOMPLEKS_REFACTOR_REDO_2026-07-09: направление → 'Комплекс'; источник = SEO/SEO Flow/Контекст
        -- VISIT_PERFORM_DIRECTION_2026-07-10: Перформ-звонки (id_салона='avto_0415') → 'Перформ' (см. leads_scored).
        -- MAX("id_салона") т.к. id_салона детерминирован на домен, domain в GROUP BY → строк не плодит.
        CASE WHEN MAX("id_салона") = 'avto_0415' THEN 'Перформ'::TEXT ELSE 'Комплекс'::TEXT END AS "направление",
        (CASE
            WHEN (NULLIF(TRIM(utm_source), '') IS NULL
                  OR (NULLIF(TRIM(utm_source), '') = 'seo' AND NULLIF(TRIM(utm_medium), '') = 'organic'))
                 THEN CASE WHEN "статус" = 'SEO Flow' THEN 'SEO Flow'
                           WHEN "статус" = 'SEO'      THEN 'SEO'
                           ELSE 'Контекст' END
            WHEN "статус" = 'SEO Flow'               THEN 'SEO Flow'
            WHEN "статус" = 'SEO'                    THEN 'SEO'
            ELSE 'Контекст'
        END)::TEXT AS "источник",
        NULLIF(TRIM(utm_source), '')   AS utm_source,
        NULLIF(TRIM(utm_medium), '')   AS utm_medium,
        NULLIF(TRIM(utm_campaign), '') AS utm_campaign,
        NULLIF(TRIM(utm_content), '')  AS utm_content,
        {leads_agg_cases}
    FROM calls_eff
    WHERE eff_arrival_date IS NOT NULL
    GROUP BY
        eff_arrival_date, domain, "салон", "город", "регион", direction,
        NULLIF(TRIM("специалист"), ''), "статус", "тип_сайта",
        NULLIF(TRIM(utm_source), ''), NULLIF(TRIM(utm_medium), ''),
        NULLIF(TRIM(utm_campaign), ''), NULLIF(TRIM(utm_content), '')
),

-- ── manager_login для ЗВОНКОВ: дозаполнение по домену (point-in-time) ───────
-- РЕШЕНИЕ ЮЗЕРА (2026-06-10): звонок (calls_scored) физически НЕ несёт Direct-
-- кампанию (нет key3) → manager_login=NULL у 100% звонков. Дозаполняем логин
-- рекламного КАБИНЕТА Директа по домену звонка, выбирая кабинет, который вёл
-- сайт на ДАТУ ВИЗИТА звонка (POINT-IN-TIME, вариант (б)), а НЕ сегодняшний
-- (global-latest). Историчнее: звонок прошлого периода получает кабинет,
-- который реально вёл домен ТОГДА. На многозначных доменах point-in-time
-- расходится с global-latest у 28.3% строк → выбрана историческая семантика.
--
-- ИСТОЧНИК МАППИНГА: big_analytics_full (T_FULL) — там manager_login = логин
-- Direct-кабинета, ДОСТОВЕРНО привязан к (domain, "Date"=дата заявки) на claim-
-- строках (3.7M строк, manager_login непуст у 100% claim-direct). T_FULL уже
-- построена к моменту step13 (step13 идёт после step6/step11). manager_login —
-- логин кабинета, НЕ CRM-менеджер ("менеджер" остаётся NULL у звонков, стр выше).
--
-- ИНТЕРВАЛЫ: day_winner — один кабинет-победитель на (domain, дата) (tie-break
-- mgr DESC, детерминированно); intervals — непересекающиеся отрезки [d_from,d_to)
-- через LEAD → дата визита попадает РОВНО в один интервал ⇒ JOIN НЕ плодит строк.
-- calls_mgr_first — самый ранний кабинет домена (fallback для звонков, чья дата
-- визита РАНЬШЕ всей истории кабинетов домена).
--
-- ВАЖНО: меняем ТОЛЬКО manager_login у звонков. Прочие ad-dims (CampaignId/Name,
-- AdGroup, campaign_code, …) остаются NULL — звонок не несёт кампанию by design.
-- claim-партиция/golden/воронка/расход НЕ затрагиваются: manager_login — атрибут-
-- срез на VISIT-стороне, в claim-меры (cost/обращения/продажи) не входит.
calls_mgr_map0 AS (
    SELECT lower(btrim(domain)) AS dom, manager_login AS mgr, "Date" AS d
    FROM {T_FULL}
    WHERE manager_login IS NOT NULL AND btrim(manager_login) <> ''
      AND domain IS NOT NULL AND btrim(domain) <> ''
    GROUP BY 1, 2, 3
),
calls_mgr_day_winner AS (
    SELECT DISTINCT ON (dom, d) dom, d, mgr
    FROM calls_mgr_map0
    ORDER BY dom, d, mgr DESC
),
calls_mgr_intervals AS (
    SELECT
        dom,
        d                                              AS d_from,
        LEAD(d) OVER (PARTITION BY dom ORDER BY d)     AS d_to,
        mgr
    FROM calls_mgr_day_winner
),
calls_mgr_first AS (
    SELECT DISTINCT ON (dom) dom, mgr AS mgr_first
    FROM calls_mgr_intervals
    ORDER BY dom, d_from ASC
),

-- ── Пиксель-пул визит-распределение по eff_arrival_date ────────────────────
-- Для пиксель-визит ветки (date-shift дробной атрибуции 'пиксель_атрибуц').
-- Пиксель-пул = РОВНО логика step5/build_pixel: local_leads_all JOIN
-- local_pixel_config (source_name=pixel_name OR utm_source=pixel_name) +
-- валидный домен (utm_source IN local_gsheet_sites). domain пикселя = utm_source.
-- Внутри (домен, месяц-заявки) строим долю визит-лидов (v_share) и продаж-лидов
-- (s_share) на каждое eff_arrival_date. Σshare=1 на группу → суммы инвариантны.
pixel_visit_statuses AS (
    -- visit + sale + credit + approved → визит (auto-merge); src='' = общий статус
    SELECT crm_status,
           CASE WHEN crm_name = 'MEGA' THEN 'mega_crm_excel' ELSE '' END AS src
    FROM local_crm_statuses
    WHERE kind = 'status'
      AND lead_status IN ('visit', 'sale', 'credit', 'approved')
      AND crm_name IN ('', 'default', 'MEGA')
),
pixel_sale_statuses AS (
    SELECT crm_status,
           CASE WHEN crm_name = 'MEGA' THEN 'mega_crm_excel' ELSE '' END AS src
    FROM local_crm_statuses
    WHERE kind = 'status'
      AND lead_status = 'sale'
      AND crm_name IN ('', 'default', 'MEGA')
),

-- ── POSEV_VISIT_DATESHIFT_2026-06-23 ──────────────────────────────────────
-- Посевной пул визит-распределения по eff_arrival_date (аналог pixel_pool/
-- pixel_eff_dist для посевной ветки 3 BFA).
--
-- ЗАДАЧА: посевная ветка 3 сейчас несёт Date=дата_заявки (proxy «Решение 3B»).
-- Заменяем proxy на РЕАЛЬНУЮ дату визита лида, точно как пиксельная ветка 4.
--
-- МЕХАНИКА:
--   BAF (big_analytics_full) НЕ хранит utm_campaign / utm_content —
--   они были свёрнуты при загрузке: utm_content(DDMMYYYY)→"Date", channel_link→"CampaignName".
--   Поэтому ключ поста строится только из полей, которые в BAF РЕАЛЬНО ЕСТЬ:
--     (LOWER(TRIM(domain)), "Date").
--   «Date» у посевных строк BAF = дата поста (TO_DATE(utm_content,'DDMMYYYY')).
--   В local_leads_all та же дата восстанавливается через
--     TO_DATE(lpad(btrim(utm_content),8,'0'), 'DDMMYYYY').
--   Фильтр utm_content ~ '^[0-9]{8}$': только валидные DDMMYYYY (зеркало load_api_leads).
--   utm_medium='posev': отсеиваем SEO/direct/pixel лидов тех же доменов.
--   plex/marcar → created_date (proxy, как пиксельная ветка и leads_with_eff).
--
-- posev_eff_dist: внутри каждого (dom, post_date) строим доли v_share/s_share
--   по реальным датам визита (Σ=1 на (dom, post_date)). Все лиды домена за этот
--   день-поста входят в одно распределение — BAF не хранит canton utm_campaign,
--   поэтому разбить по каналу невозможно и нецелесообразно.
--   Статусы «визит» и «продажа» — те же local_crm_statuses что пиксельная ветка.
--
-- ИНВАРИАНТЫ:
--   Σ priezd/prodazhi посевов за всё время СОХРАНЯЕТСЯ (Σдолей=1 на (dom,post_date)).
--   Расход (total_cost=0 в ветке 3 BFA) НЕ сдвигается — он живёт в BAF.
--   BAF не трогаем вообще. Ветки 1/2/4 step13 не трогаем.
--   Дробность NUMERIC сохранена (NO int-каст).
--   Один домен → один салон (gsheet_sites 1:1) → GROUP BY (new_date, domain, "салон")
--   не вызывает раздувания при нескольких каналах за один день.
--
-- КРАЕВЫЕ СЛУЧАИ:
--   Лид без arrival_date → eff = created_date (fallback, строка не теряется).
--   Пост без визит-лидов в posev_pool → LEFT JOIN → v_share/s_share = NULL →
--     строка выпадает из визит-оси BFA (посев без единого приезда по CRM не несёт
--     реальной даты визита; такие посты логируются как orphan в run()).

-- Посевные лиды из local_leads_all с реальной датой визита.
-- Ключ: (LOWER(TRIM(ld.name)), TO_DATE(lpad(utm_content,8,'0'),'DDMMYYYY')) —
-- это то же что (domain, "Date") посевной строки в BAF.
-- Домен берётся через JOIN local_domains (local_leads_all хранит только domain_id).
-- Фильтр utm_medium='posev' + utm_content=8 цифр отсекает SEO/direct/pixel.
posev_pool AS (
    SELECT
        LOWER(TRIM(ld.name))                                     AS dom,
        TO_DATE(LPAD(BTRIM(l.utm_content), 8, '0'), 'DDMMYYYY') AS post_date,
        CASE WHEN l.source_type IN ('plex_excel', 'marcar_crm_excel')
                 THEN l.created_date::date
             ELSE COALESCE(l.arrival_date::date, l.created_date::date)
        END AS eff,
        -- визит-флаг: те же статусы что pixel_visit_statuses
        (EXISTS (SELECT 1 FROM pixel_visit_statuses v
                  WHERE v.crm_status = l.status
                    AND (v.src = '' OR v.src = l.source_type)))::int AS isv,
        -- продажа-флаг: те же статусы что pixel_sale_statuses
        (EXISTS (SELECT 1 FROM pixel_sale_statuses s
                  WHERE s.crm_status = l.status
                    AND (s.src = '' OR s.src = l.source_type)))::int AS iss
    FROM {T_LEADS_ALL_LOCAL} l
    JOIN {T_DOMAINS_LOCAL} ld ON ld.id = l.domain_id
    WHERE l.is_copy_for_removal IS NOT TRUE
      AND l.status IS NOT NULL
      AND l.status != ''
      AND l.utm_medium = 'posev'
      AND l.utm_campaign IS NOT NULL AND l.utm_campaign != ''
      AND COALESCE(l.utm_content, '') ~ '^[0-9]{{8}}$'
),

-- Доли визитов/продаж по реальной дате внутри каждого (домен, дата_поста).
-- Σ=1 на (dom, post_date). Все каналы домена за этот день объединены —
-- BAF не хранит utm_campaign, разбить по каналу невозможно.
posev_eff_dist AS (
    SELECT
        dom,
        post_date,
        eff AS new_date,
        SUM(isv)::numeric / NULLIF(SUM(SUM(isv)) OVER (PARTITION BY dom, post_date), 0) AS v_share,
        SUM(iss)::numeric / NULLIF(SUM(SUM(iss)) OVER (PARTITION BY dom, post_date), 0) AS s_share
    FROM posev_pool
    GROUP BY dom, post_date, eff
),

pixel_pool AS (
    SELECT
        LOWER(TRIM(l.utm_source)) AS dom,
        DATE_TRUNC('month', l.created_date)::date AS mon,
        CASE WHEN l.source_type IN ('plex_excel', 'marcar_crm_excel')
                 THEN l.created_date::date
             ELSE COALESCE(l.arrival_date::date, l.created_date::date)
        END AS eff,
        (EXISTS (SELECT 1 FROM pixel_visit_statuses v
                  WHERE v.crm_status = l.status
                    AND (v.src = '' OR v.src = l.source_type)))::int AS isv,
        (EXISTS (SELECT 1 FROM pixel_sale_statuses s
                  WHERE s.crm_status = l.status
                    AND (s.src = '' OR s.src = l.source_type)))::int AS iss
    FROM {T_LEADS_ALL_LOCAL} l
    JOIN local_pixel_config pc
      ON (l.source_name = pc.pixel_name
          OR LOWER(l.utm_source) = LOWER(pc.pixel_name))
    WHERE l.is_copy_for_removal IS NOT TRUE
      AND l.utm_source IS NOT NULL
      AND l.utm_source != ''
      AND LOWER(TRIM(l.utm_source)) IN (
            SELECT DISTINCT LOWER(TRIM("domain"))
            FROM {T_GSHEET_SITES}
            WHERE "domain" IS NOT NULL AND TRIM("domain") != ''
      )
),
pixel_eff_dist AS (
    SELECT
        dom, mon, eff AS new_date,
        SUM(isv)::numeric / NULLIF(SUM(SUM(isv)) OVER (PARTITION BY dom, mon), 0) AS v_share,
        SUM(iss)::numeric / NULLIF(SUM(SUM(iss)) OVER (PARTITION BY dom, mon), 0) AS s_share
    FROM pixel_pool
    GROUP BY dom, mon, eff
)

-- ── Финальная таблица: 73 колонки = зеркало big_analytics_full ────────────
-- Порядок колонок ТОЧНО как в big_analytics_full (см. information_schema).
SELECT
    -- 1  key3
    NULL::text                          AS key3,
    -- 2  Date
    u."Date"::date                      AS "Date",
    -- 3  День недели (из Date)
    {_DOW_SQL}                          AS "День недели",
    -- 4  week_start (из Date)
    DATE_TRUNC('week', u."Date")::date  AS week_start,
    -- 5  CampaignId
    u."CampaignId"::bigint              AS "CampaignId",
    -- 6  CampaignName
    u."CampaignName"::text              AS "CampaignName",
    -- 7  AdGroupId
    u."AdGroupId"::bigint               AS "AdGroupId",
    -- 8  AdGroupName
    u."AdGroupName"::text               AS "AdGroupName",
    -- 9  AdNetworkType
    u."AdNetworkType"::text             AS "AdNetworkType",
    -- 10 Device
    u."Device"::text                    AS "Device",
    -- 11 Impressions (claim → 0)
    0::numeric                          AS "Impressions",
    -- 12 Clicks (claim → 0)
    0::numeric                          AS "Clicks",
    -- 13 total_cost (claim → 0)
    0::numeric                          AS total_cost,
    -- 14 domain
    u.domain::text                      AS domain,
    -- 15 RlAdjustmentId
    u."RlAdjustmentId"::bigint          AS "RlAdjustmentId",
    -- 16 RlAdjustmentId_total
    CASE WHEN u."RlAdjustmentId" IS NULL THEN NULL
         WHEN u."RlAdjustmentId" > 0     THEN 'Есть корректировка'
         ELSE 'Нет корректировки' END   AS "RlAdjustmentId_total",
    -- 17 campaign_code — КАТЕГОРИЯ A (2026-06-11): симметрия с claim.
    --     calls → 'звонки' (step6:154); SEO-лиды → 'seo' (step3:833);
    --     direct-лиды (Контекст) → РЕАЛЬНЫЙ код из fallback Задачи 2 — НЕ перетираем.
    --     Ветка определяется u.source_type ('calls') и u."направление" (SEO/SEO Flow).
    CASE
        WHEN u.source_type = 'calls'                 THEN 'звонки'
        WHEN u."источник" IN ('SEO', 'SEO Flow')  THEN 'seo'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
        ELSE u.campaign_code::text
    END                                 AS campaign_code,
    -- 18 tp — TP_EMPTY_TO_NULL_2026_06_09: пустую строку '' нормализуем к NULL,
    --         чтобы в PBI-слайсере был ОДИН пустой пункт «(Пусто)», а не два.
    --         КАТЕГОРИЯ A: calls → 'звонки' (step6:154); SEO → NULL (step3:833 SEO tp=NULL);
    --         direct → реальный tp (fallback Задачи 2) НЕ перетираем.
    CASE
        WHEN u.source_type = 'calls' THEN 'звонки'
        ELSE NULLIF(u.tp::text, '')
    END                                 AS tp,
    -- 19 cpc_cpa — КАТЕГОРИЯ A: calls → 'звонки' (step6:155); иначе реальный/NULL.
    CASE
        WHEN u.source_type = 'calls' THEN 'звонки'
        ELSE u.cpc_cpa::text
    END                                 AS cpc_cpa,
    -- 20 site_quiz — КАТЕГОРИЯ A: calls → 'звонки' (step6:155); иначе реальный/NULL.
    CASE
        WHEN u.source_type = 'calls' THEN 'звонки'
        ELSE u.site_quiz::text
    END                                 AS site_quiz,
    -- 21 adgroup_code
    u.adgroup_code::text                AS adgroup_code,
    -- 22 account_login
    u.account_login::text               AS account_login,
    -- 23 manager_login
    u.manager_login::text               AS manager_login,
    -- 24-30 ag_part1..7 (нейминг недоступен на lead-уровне → NULL)
    NULL::text AS ag_part1, NULL::text AS ag_part2, NULL::text AS ag_part3,
    NULL::text AS ag_part4, NULL::text AS ag_part5, NULL::text AS ag_part6,
    NULL::text AS ag_part7,
    -- 31 марки авто (производное от adgroup_code → недоступно дёшево → NULL)
    NULL::text                          AS "марки авто",
    -- 32 Название crm — КАТЕГОРИЯ A: CRM-приоритет домена (Маркар>Мега>Фаиг>Плекс),
    --    собран в leads_scored/calls_scored (симметрия claim step6:164/step3:840).
    u."Название crm"::text              AS "Название crm",
    -- 33 тип_заявки — КАТЕГОРИЯ A: calls → 'Звонки' (step6:169); lead-ветка сохраняет
    --    маркировку 'Звонки_CDR' из CRM-лидов с subsource:cdr, прочие лиды → 'Заявки'.
    --    CAPITALIZE_FIX_2026-07-10: с большой буквы, симметрия с build_star.py:99 фильтром
    --    ('Звонки'/'Отзывы') — строчные значения выпадали из витрины на визит-оси.
    CASE
        WHEN u.source_type = 'calls' THEN 'Звонки'
        ELSE u."тип_заявки"::text
    END                                 AS "тип_заявки",
    -- 34-45 воронка (РЕАЛЬНАЯ визитная)
    u.kol_vo_zayavok::numeric           AS kol_vo_zayavok,
    u.korr::numeric                     AS korr,
    u.kval::numeric                     AS kval,
    u.priezd::numeric                   AS priezd,
    u.prodazhi::numeric                 AS prodazhi,
    u.nekorr::numeric                   AS nekorr,
    u.ne_otvechaet::numeric             AS ne_otvechaet,
    u.filtr::numeric                    AS filtr,
    u.nedozvon::numeric                 AS nedozvon,
    u.priedet::numeric                  AS priedet,
    u.dohod_do_kredita::bigint          AS dohod_do_kredita,
    u.dobro::bigint                     AS dobro,
    -- 46 статус
    u."статус"::text                    AS "статус",
    -- 47 специалист
    u."специалист"::text                AS "специалист",
    -- 48 тип_сайта (FIX 2026-06-10: из справочника gsheet_sites, было хардкод NULL)
    u."тип_сайта"::text                 AS "тип_сайта",
    -- 49 шаблон — КАТЕГОРИЯ A: gsheet_sites.template (step6:169/step3:846), было хардкод NULL
    u."шаблон"::text                    AS "шаблон",
    -- 50 салон
    u."салон"::text                     AS "салон",
    -- 51 город
    u."город"::text                     AS "город",
    -- 52 регион
    u."регион"::text                    AS "регион",
    -- 53 direction
    u.direction::text                   AS direction,
    -- 54 неверный_кодер_new
    NULL::text                          AS "неверный_кодер_new",
    -- 55 fid
    NULL::text                          AS fid,
    -- 56 проджект — КАТЕГОРИЯ A: gsheet_sites.project_manager (step6:173), было хардкод NULL
    u."проджект"::text                  AS "проджект",
    -- 57 id_салона — КАТЕГОРИЯ A: gsheet_sites.client_id (step6:174), было хардкод NULL
    u."id_салона"::text                 AS "id_салона",
    -- 58 менеджер — КАТЕГОРИЯ A: gsheet_sites.sales_manager (step6:175), было хардкод NULL
    u."менеджер"::text                  AS "менеджер",
    -- 59 источник
    u."источник"::text                  AS "источник",
    -- 60 направление
    u."направление"::text               AS "направление",
    -- 61 "номер кампании | название кампании"
    NULL::text                          AS "номер кампании | название кампании",
    -- 62 "номер группы | название группы"
    NULL::text                          AS "номер группы | название группы",
    -- 63 План заявки
    NULL::integer                       AS "План заявки",
    -- 64 План приезда
    NULL::integer                       AS "План приезда",
    -- 65 "аккаунт|сайт"
    -- FIX-ARRIVAL-ACCOUNT-SITE-2026-06-15:
    -- Лиды (leads_scored): u.account_login уже заполнен через COALESCE(yd_agg, camp_dict).
    -- Звонки (calls_scored): account_login=NULL (звонок не несёт Direct-кампанию by design).
    -- Для звонков логин кабинета дотягиваем по домену из local_gsheet_sites.login_key
    -- (тот же путь что step3 для calls: gs.login_key || '|' || la.domain, строки 868/961).
    -- local_gsheet_sites WHERE direction='Авто' уникален по domain (4090=4090 distinct) →
    -- скалярный подзапрос возвращает ровно 1 строку, fan-out исключён.
    -- Если логин не найден (ни u.account_login, ни gs.login_key) → результат NULL (не '|domain').
    -- Заявочная сторона (BAF) и golden Кудерко не затрагиваются: эта колонка — атрибут-срез
    -- на visit-стороне; total_cost=0 в BFA, расходная часть golden живёт в BAF.
    COALESCE(
        u.account_login,
        (SELECT gs.login_key
         FROM {T_GSHEET_SITES} gs
         WHERE LOWER(TRIM(gs."domain")) = LOWER(TRIM(u.domain))
           AND gs.direction = 'Авто'
         LIMIT 1)
    ) || '|' || LOWER(TRIM(u.domain))  AS "аккаунт|сайт",
    -- 66 priezd_arrival_date (claim BIGINT-счётчик → 0)
    0::bigint                           AS priezd_arrival_date,
    -- 67 prodazhi_arrival_date (claim → 0)
    0::bigint                           AS prodazhi_arrival_date,
    -- 68 поставщик — КАТЕГОРИЯ A: calls → 'звонки' (step6:189); SEO-лиды → 'SEO' (step3:862);
    --    direct-лиды (Контекст) → NULL (claim direct не несёт фикс. поставщика).
    CASE
        WHEN u.source_type = 'calls'                 THEN 'звонки'
        WHEN u."источник" IN ('SEO', 'SEO Flow')  THEN 'SEO'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
        ELSE NULL
    END                                 AS "поставщик",
    -- 69 _source_table
    u.source_type::text                 AS _source_table,
    -- 70 key_pixel_score
    NULL::text                          AS key_pixel_score,
    -- 71 campaign_status
    NULL::text                          AS campaign_status,
    -- 72 payment_model
    NULL::text                          AS payment_model,
    -- 73 ag_part1_name
    NULL::text                          AS ag_part1_name
FROM (
    SELECT
        "Date", domain, "салон", "город", "регион", direction, "специалист", "статус",
        "тип_сайта",
        -- КАТЕГОРИЯ A: справочные атрибуты протаскиваем через подзапрос u
        "шаблон", "проджект", "id_салона", "менеджер", "Название crm",
        "тип_заявки", source_type, "направление", "источник",
        "CampaignId", "CampaignName", "AdGroupId", "AdGroupName", "AdNetworkType",
        "Device", "RlAdjustmentId", campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
        account_login, manager_login,
        kol_vo_zayavok, korr, kval, priezd, prodazhi,
        nekorr, ne_otvechaet, filtr, nedozvon, priedet, dohod_do_kredita, dobro
    FROM leads_scored_direct_only
    WHERE priezd > 0 OR prodazhi > 0

    UNION ALL

    SELECT
        cs."Date", cs.domain, cs."салон", cs."город", cs."регион", cs.direction, cs."специалист", cs."статус",
        cs."тип_сайта",
        cs."шаблон", cs."проджект", cs."id_салона", cs."менеджер", cs."Название crm",
        'Звонки'::text AS "тип_заявки", cs.source_type, cs."направление", cs."источник",
        NULL::bigint, NULL::text, NULL::bigint, NULL::text, NULL::text,
        NULL::text, NULL::bigint, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text,
        NULL::text,
        -- manager_login (ДОЗАПОЛНЕНИЕ point-in-time): кабинет, ведший домен на дату
        -- визита звонка; fallback на самый ранний кабинет домена; иначе NULL
        -- (домен без кабинета в маппинге). account_login (предыдущий столбец) и все
        -- прочие ad-dims остаются NULL — звонок не несёт Direct-кампанию.
        COALESCE(iv.mgr, cf.mgr_first)::text        AS manager_login,
        cs.kol_vo_zayavok, cs.korr, cs.kval, cs.priezd, cs.prodazhi,
        cs.nekorr, cs.ne_otvechaet, cs.filtr, cs.nedozvon, cs.priedet, cs.dohod_do_kredita, cs.dobro
    FROM calls_scored cs
    LEFT JOIN calls_mgr_intervals iv
        ON iv.dom = lower(btrim(cs.domain))
       AND cs."Date" >= iv.d_from
       AND (iv.d_to IS NULL OR cs."Date" < iv.d_to)
    LEFT JOIN calls_mgr_first cf
        ON cf.dom = lower(btrim(cs.domain))
    WHERE cs.priezd > 0 OR cs.prodazhi > 0
) u

UNION ALL

-- ══════════════════════════════════════════════════════════════════════════
-- ── Посевная ветка: ДВЕ ветки (matched date-shift + proxy полнота) ──────
-- POSEV_VISIT_DATESHIFT_2026-06-23
-- ══════════════════════════════════════════════════════════════════════════
-- ДВЕ SUB-ВЕТКИ внутри одного UNION ALL:
--
-- 3A. MATCHED-ветка (date-shift по реальной дате визита):
--     Только crop_targeting строки BAF, для которых есть лиды в posev_pool
--     (utm_medium='posev', utm_content=DDMMYYYY, тот же domain+post_date).
--     INNER JOIN posev_eff_dist выполняет отбор автоматически.
--     Date = реальная eff_arrival_date (по долям v_share/s_share).
--     priezd×v_share / prodazhi×s_share; агрегация по (new_date, domain, салон, направление).
--     Σdolей=1 → суммы инвариантны.
--
-- 3B. PROXY-ветка (восстановление полноты, as-is из BAF):
--     Все посевы НЕ попавшие в matched-ветку:
--       (а) НЕ-crop: tp8/tp9/tp10/telegram/social_посевы — нет DDMMYYYY utm_content,
--           posev_pool их не покрывает.
--       (б) orphan crop_targeting: crop без лидов в posev_pool для данного (domain, Date).
--           Anti-join через NOT EXISTS(posev_pool match).
--     Date = дата заявки (proxy) — реальной визитной даты нет.
--     priezd/prodazhi as-is. Обеспечивает Σ(BFA) = Σ(BAF) по посевам (полный паритет).
--     Anti-join гарантирует: matched crop строка попадает РОВНО в 3A (в 3B НЕ войдёт).
--
-- ИНВАРИАНТЫ:
--   total_cost = 0 (расход живёт в BAF, в BFA = 0 by design).
--   Σ priezd / Σ prodazhi BFA посевов = Σ BAF посевов (полный паритет).
--   Дробность NUMERIC сохранена, NO int-каст.
--   FIX3 анти-задвоение звонков: _source_table <> 'calls' в обеих ветках.
--   Нет задвоения: matched crop строка EXISTS(posev_pool) → не пройдёт NOT EXISTS в 3B.
SELECT
    MAX(ps.key3)                          AS key3,
    ps."Date",
    MAX(ps."День недели")                 AS "День недели",
    MAX(ps.week_start)                    AS week_start,
    MAX(ps."CampaignId")                  AS "CampaignId",
    MAX(ps."CampaignName")                AS "CampaignName",
    MAX(ps."AdGroupId")                   AS "AdGroupId",
    MAX(ps."AdGroupName")                 AS "AdGroupName",
    MAX(ps."AdNetworkType")               AS "AdNetworkType",
    MAX(ps."Device")                      AS "Device",
    0::numeric                            AS "Impressions",  -- claim → 0
    0::numeric                            AS "Clicks",       -- claim → 0
    0::numeric                            AS total_cost,     -- claim → 0 (расход в BAF)
    ps.domain,
    MAX(ps."RlAdjustmentId")              AS "RlAdjustmentId",
    MAX(ps."RlAdjustmentId_total")        AS "RlAdjustmentId_total",
    MAX(ps.campaign_code)                 AS campaign_code,
    MAX(ps.tp)                            AS tp,
    MAX(ps.cpc_cpa)                       AS cpc_cpa,
    MAX(ps.site_quiz)                     AS site_quiz,
    MAX(ps.adgroup_code)                  AS adgroup_code,
    MAX(ps.account_login)                 AS account_login,
    MAX(ps.manager_login)                 AS manager_login,
    MAX(ps.ag_part1) AS ag_part1, MAX(ps.ag_part2) AS ag_part2,
    MAX(ps.ag_part3) AS ag_part3, MAX(ps.ag_part4) AS ag_part4,
    MAX(ps.ag_part5) AS ag_part5, MAX(ps.ag_part6) AS ag_part6,
    MAX(ps.ag_part7) AS ag_part7,
    MAX(ps."марки авто")                  AS "марки авто",
    MAX(ps."Название crm")                AS "Название crm",
    MAX(ps."тип_заявки")                  AS "тип_заявки",
    0::numeric                            AS kol_vo_zayavok,
    0::numeric                            AS korr,
    0::numeric                            AS kval,
    SUM(ps.priezd_shifted)                AS priezd,    -- NUMERIC, дробность сохранена
    SUM(ps.prodazhi_shifted)              AS prodazhi,  -- NUMERIC, дробность сохранена
    0::numeric                            AS nekorr,
    0::numeric                            AS ne_otvechaet,
    0::numeric                            AS filtr,
    0::numeric                            AS nedozvon,
    0::numeric                            AS priedet,
    0::bigint                             AS dohod_do_kredita,
    0::bigint                             AS dobro,
    MAX(ps."статус")                      AS "статус",
    MAX(ps."специалист")                  AS "специалист",
    MAX(ps."тип_сайта")                   AS "тип_сайта",
    MAX(ps."шаблон")                      AS "шаблон",
    ps."салон",
    MAX(ps."город")                       AS "город",
    MAX(ps."регион")                      AS "регион",
    MAX(ps.direction)                     AS direction,
    MAX(ps."неверный_кодер_new")          AS "неверный_кодер_new",
    MAX(ps.fid)                           AS fid,
    MAX(ps."проджект")                    AS "проджект",
    MAX(ps."id_салона")                   AS "id_салона",
    MAX(ps."менеджер")                    AS "менеджер",
    MAX(ps."источник")                    AS "источник",
    ps."направление",
    MAX(ps."номер кампании | название кампании") AS "номер кампании | название кампании",
    MAX(ps."номер группы | название группы")     AS "номер группы | название группы",
    MAX(ps."План заявки")                 AS "План заявки",
    MAX(ps."План приезда")                AS "План приезда",
    MAX(ps."аккаунт|сайт")               AS "аккаунт|сайт",
    0::bigint                             AS priezd_arrival_date,    -- claim → 0
    0::bigint                             AS prodazhi_arrival_date,  -- claim → 0
    MAX(ps."поставщик")                   AS "поставщик",
    MAX(ps._source_table)                 AS _source_table,
    MAX(ps.key_pixel_score)               AS key_pixel_score,
    MAX(ps.campaign_status)               AS campaign_status,
    MAX(ps.payment_model)                 AS payment_model,
    MAX(ps.ag_part1_name)                 AS ag_part1_name
FROM (
    -- date-shift: каждая посевная строка BAF × eff_date с весами (ДО агрегации).
    -- JOIN по (domain,"Date") — единственный ключ в BAF для посевных строк.
    -- Агрегация по (new_date, domain, салон, направление) схлопывает fan-out.
    SELECT
        f.key3,
        psd.new_date::date                              AS "Date",
        {_DOW_SQL_F('psd.new_date')}                   AS "День недели",
        DATE_TRUNC('week', psd.new_date)::date         AS week_start,
        f."CampaignId",
        f."CampaignName",
        f."AdGroupId",
        f."AdGroupName",
        f."AdNetworkType",
        f."Device",
        f.domain,
        f."RlAdjustmentId",
        f."RlAdjustmentId_total",
        f.campaign_code,
        f.tp,
        f.cpc_cpa,
        f.site_quiz,
        f.adgroup_code,
        f.account_login,
        f.manager_login,
        f.ag_part1, f.ag_part2, f.ag_part3, f.ag_part4,
        f.ag_part5, f.ag_part6, f.ag_part7,
        f."марки авто",
        f."Название crm",
        f."тип_заявки",
        -- priezd × v_share: NUMERIC, БЕЗ int-каста (инвариант дробной атрибуции)
        COALESCE(f.priezd, 0)   * psd.v_share          AS priezd_shifted,
        COALESCE(f.prodazhi, 0) * psd.s_share          AS prodazhi_shifted,
        f."статус",
        NULLIF(TRIM(f."специалист"), '')                AS "специалист",
        f."тип_сайта",
        f."шаблон",
        f."салон",
        f."город",
        f."регион",
        f.direction,
        f."неверный_кодер_new",
        f.fid,
        f."проджект",
        f."id_салона",
        f."менеджер",
        f."источник",
        f."направление",
        f."номер кампании | название кампании",
        f."номер группы | название группы",
        f."План заявки",
        f."План приезда",
        f."аккаунт|сайт",
        f."поставщик",
        f._source_table,
        f.key_pixel_score,
        f.campaign_status,
        f.payment_model,
        f.ag_part1_name
        -- POSEV_VISIT_DATESHIFT_2026-06-23: utm_campaign/utm_content убраны из SELECT
        -- BAF не хранит эти поля (они были свёрнуты при загрузке:
        -- utm_content→"Date", channel_link→"CampaignName"). JOIN теперь по (domain,"Date").
    FROM {T_FULL} f
    JOIN posev_eff_dist psd
      ON psd.dom       = LOWER(TRIM(f.domain))
     AND psd.post_date = f."Date"
    WHERE f."источник" LIKE 'Посевы_%'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09 (было: направление='посевы')
      AND f."Date" IS NOT NULL
      -- FIX3 анти-задвоение звонков: посев-звонки несёт ветка 2 (calls_scored)
      AND f._source_table <> 'calls'
      -- POSEV_VISIT_DATESHIFT_2026-06-23: matched-ветка только для crop_targeting,
      -- у которых есть лиды в posev_pool (INNER JOIN posev_eff_dist фильтрует автоматически).
      -- Orphan crop (нет match) и все не-crop посевы идут в proxy-ветку ниже.
      AND f._source_table = 'crop_targeting'
      -- только строки с визитом (расходные строки priezd=0 не несут визитной информации)
      AND (COALESCE(f.priezd, 0) > 0 OR COALESCE(f.prodazhi, 0) > 0)
      -- применяем сдвиг только для ненулевых долей
      AND ( (COALESCE(f.priezd, 0)   > 0 AND psd.v_share > 0)
         OR (COALESCE(f.prodazhi, 0) > 0 AND psd.s_share > 0) )

    UNION ALL

    -- ── 3B: PROXY-ветка посевов (полнота) ────────────────────────────────────
    -- POSEV_VISIT_DATESHIFT_2026-06-23: восстановление полноты посевной оси.
    -- Несёт ВСЕ посевы которые НЕ попали в matched-ветку:
    --   (а) НЕ-crop посевы: tp8/tp9/tp10/telegram/social_посевы —
    --       у них utm_content без DDMMYYYY → posev_pool их не покрывает.
    --   (б) orphan crop_targeting: crop-строки в BAF, для которых нет ни одного
    --       лида в local_leads_all с тем же (domain, post_date=TO_DATE(utm_content,...)).
    --       Нет матча → posev_eff_dist не даст строку → INNER JOIN исключит их.
    --       Anti-join: _source_table='crop_targeting' AND NOT EXISTS(posev_pool).
    --       Вместе условия покрывают все выпавшие строки без задвоения:
    --       matched-строка (_source_table='crop_targeting' AND EXISTS posev_pool)
    --       попадает РОВНО в matched-ветку (INNER JOIN туда она прошла),
    --       поэтому здесь НЕ пройдёт NOT EXISTS / != 'crop_targeting'.
    -- Date = дата заявки (proxy) — реальной даты визита нет для этих строк.
    -- priezd/prodazhi берутся as-is из BAF (без умножения на share).
    -- FIX3 сохранён: _source_table <> 'calls'.
    -- Фильтр priezd>0 OR prodazhi>0 сохранён.
    SELECT
        f.key3,
        f."Date"::date                                  AS "Date",
        {_DOW_SQL_F('f."Date"')}                        AS "День недели",
        DATE_TRUNC('week', f."Date")::date              AS week_start,
        f."CampaignId",
        f."CampaignName",
        f."AdGroupId",
        f."AdGroupName",
        f."AdNetworkType",
        f."Device",
        f.domain,
        f."RlAdjustmentId",
        f."RlAdjustmentId_total",
        f.campaign_code,
        f.tp,
        f.cpc_cpa,
        f.site_quiz,
        f.adgroup_code,
        f.account_login,
        f.manager_login,
        f.ag_part1, f.ag_part2, f.ag_part3, f.ag_part4,
        f.ag_part5, f.ag_part6, f.ag_part7,
        f."марки авто",
        f."Название crm",
        f."тип_заявки",
        -- proxy: priezd/prodazhi as-is (нет date-shift → нет умножения на share)
        COALESCE(f.priezd, 0)::numeric                  AS priezd_shifted,
        COALESCE(f.prodazhi, 0)::numeric                AS prodazhi_shifted,
        f."статус",
        NULLIF(TRIM(f."специалист"), '')                AS "специалист",
        f."тип_сайта",
        f."шаблон",
        f."салон",
        f."город",
        f."регион",
        f.direction,
        f."неверный_кодер_new",
        f.fid,
        f."проджект",
        f."id_салона",
        f."менеджер",
        f."источник",
        f."направление",
        f."номер кампании | название кампании",
        f."номер группы | название группы",
        f."План заявки",
        f."План приезда",
        f."аккаунт|сайт",
        f."поставщик",
        f._source_table,
        f.key_pixel_score,
        f.campaign_status,
        f.payment_model,
        f.ag_part1_name
    FROM {T_FULL} f
    WHERE f."источник" LIKE 'Посевы_%'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09 (было: направление='посевы')
      AND f."Date" IS NOT NULL
      -- BY-DESIGN: посевные звонки (_source_table='calls', источник='Посевы_Звонки') НАМЕРЕННО
      -- исключены из этой ветки и идут через обычную calls_scored (ветка 2).
      -- Следствие расхождения full↔arrival: ЗАЯВКА (big_analytics_full) несёт источник='Посевы_Звонки',
      -- ВИЗИТ/ПРОДАЖА (arrival-ось) — источник='Контекст' через calls_scored.
      -- Почему нельзя убрать этот фильтр: посев-звонки идут с direction='Авто' доменов
      -- (calls_base НЕ исключает их), поэтому при снятии фильтра они считались бы дважды —
      -- и в calls_scored (источник=Контекст), и в этой ветке (источник=Посевы_Звонки). ~22 продажи ×2.
      -- Полный сквозной учёт посевных звонков — отдельная будущая задача
      -- (Вариант B: исключить из calls_base по direction_main='Посевы', пронести отдельной веткой).
      AND f._source_table <> 'calls'
      -- только строки с визитом
      AND (COALESCE(f.priezd, 0) > 0 OR COALESCE(f.prodazhi, 0) > 0)
      -- ANTI-JOIN к matched-ветке:
      -- matched = _source_table='crop_targeting' AND EXISTS(posev_pool match).
      -- Строка попадает в proxy если она НЕ matched:
      --   вариант А: не crop → всегда в proxy
      --   вариант Б: crop, но orphan (нет лидов с DDMMYYYY utm_content за этот domain+Date)
      AND (
          f._source_table <> 'crop_targeting'
          OR NOT EXISTS (
              SELECT 1
              FROM {T_LEADS_ALL_LOCAL} l
              JOIN {T_DOMAINS_LOCAL} ld ON ld.id = l.domain_id
              WHERE LOWER(TRIM(ld.name)) = LOWER(TRIM(f.domain))
                AND l.is_copy_for_removal IS NOT TRUE
                AND l.utm_medium = 'posev'
                AND l.utm_campaign IS NOT NULL AND l.utm_campaign != ''
                AND COALESCE(l.utm_content, '') ~ '^[0-9]{{8}}$'
                AND TO_DATE(LPAD(BTRIM(l.utm_content), 8, '0'), 'DDMMYYYY') = f."Date"
          )
      )
) ps
-- FIX2-паттерн анти-fan-out: агрегируем ПОСЛЕ date-shift.
-- Ключ (new_date, domain, салон, направление) — domain→салон это 1:1 из gsheet_sites,
-- поэтому нескольких каналов за один (domain, "Date") не вызывают раздувания.
-- utm_campaign/utm_content убраны: BAF их не хранит → GROUP BY по ним невозможен.
GROUP BY ps."Date", ps.domain, ps."салон", ps."направление"

UNION ALL

-- ═══════════════════════════════════════════════════════════════════════════
-- ── Пиксельная ветка: ДРОБНАЯ атрибуция по РЕАЛЬНОЙ дате визита ─────────────
-- ═══════════════════════════════════════════════════════════════════════════
-- Источник: big_analytics_full WHERE направление='пиксель_атрибуц' (ДРОБНАЯ
-- атрибуция step11, priezd/prodazhi размазаны по кампаниям; NUMERIC, не int).
-- НЕ 'пиксель' (целое) — берём дробное, чтобы сохранить кампанийный разрез.
-- НЕ обе сразу — 'пиксель' и 'пиксель_атрибуц' несут ОДИН расход (дубль).
--
-- DATE-SHIFT: у дробной строки Date=created_date (дата заявки). Реальная дата
-- визита живёт на lead-уровне в local_leads_all (arrival_date по CRM-правилам).
-- Строим из пиксель-пула (тот же JOIN local_pixel_config что step5) распределение
-- визит-лидов по eff_arrival_date внутри (домен, месяц-заявки) и применяем его как
-- сдвиг: каждую дробную строку размножаем по eff_arrival_date с весами распределения.
--   priezd_new   = priezd_old   × v_share(домен,месяц,eff_date)   [визит-доля]
--   prodazhi_new = prodazhi_old × s_share(домен,месяц,eff_date)   [продажа-доля]
-- Σv_share=Σs_share=1 в каждой (домен,месяц) → SUM(priezd)/SUM(prodazhi) пикселя
-- ИНВАРИАНТНЫ (только перераспределяются по реальным датам). Проверено на боевых:
-- 6196.00 приездов / 449.00 продаж сохраняются 1:1, orphan=0 (все 758 пар матчатся).
--
-- eff_arrival_date по CRM (как step13 для лидов):
--   plex_excel/marcar_crm_excel → created_date (proxy, дата ненадёжна/отсутствует)
--   crmf_excel/mega_crm_excel   → COALESCE(arrival_date, created_date)
-- Покрытие реальной даты у визит-лидов пиксель-пула ~99% (crmf), proxy там где нет.
--
-- ВОРОНКА визита: visit = lead_status IN ('visit','sale') (+ credit/approved,
-- auto-merge в visit); продажа = lead_status='sale'. Маппинг из local_crm_statuses
-- kind='status' с учётом crm_name ('' / 'default' общие, 'MEGA'→mega override).
-- Claim cost/clicks/impressions/arrival-счётчики = 0 (как все визит-ветки).
--
-- FIX2 АНТИ-FAN-OUT: JOIN 165910 кампанийных строк 'пиксель_атрибуц' к pixel_eff_dist
-- ТОЛЬКО по (домен,месяц) без ключа даты → каждая строка ×N(eff_arrival_date группы)
-- = 849146 строк (×5.1, декартово размножение campaign×date ВНУТРИ группы). Суммы
-- ЦЕЛЫ (Σv_share=1), раздут только row-count. Фикс: после date-shift АГРЕГИРУЕМ до
-- здорового разреза (Date, domain, салон, направление, CampaignId/Name) — SUM(priezd),
-- SUM(prodazhi) сохраняют дробность (NUMERIC, БЕЗ int-каста), Σ остаётся 6196/449.
-- Прочие текстовые поля детерминированы внутри (домен,кампания,дата) → MAX.
SELECT
    MAX(px.key3)                        AS key3,
    px."Date",
    MAX(px."День недели")               AS "День недели",
    MAX(px.week_start)                  AS week_start,
    px."CampaignId",
    px."CampaignName",
    MAX(px."AdGroupId")                 AS "AdGroupId",
    MAX(px."AdGroupName")               AS "AdGroupName",
    MAX(px."AdNetworkType")             AS "AdNetworkType",
    MAX(px."Device")                    AS "Device",
    0::numeric AS "Impressions",          -- claim → 0
    0::numeric AS "Clicks",               -- claim → 0
    0::numeric AS total_cost,             -- claim → 0
    px.domain,
    MAX(px."RlAdjustmentId")             AS "RlAdjustmentId",
    MAX(px."RlAdjustmentId_total")       AS "RlAdjustmentId_total",
    MAX(px.campaign_code)               AS campaign_code,
    MAX(px.tp)                          AS tp,
    MAX(px.cpc_cpa)                     AS cpc_cpa,
    MAX(px.site_quiz)                   AS site_quiz,
    MAX(px.adgroup_code)                AS adgroup_code,
    MAX(px.account_login)               AS account_login,
    MAX(px.manager_login)               AS manager_login,
    MAX(px.ag_part1) AS ag_part1, MAX(px.ag_part2) AS ag_part2, MAX(px.ag_part3) AS ag_part3,
    MAX(px.ag_part4) AS ag_part4, MAX(px.ag_part5) AS ag_part5, MAX(px.ag_part6) AS ag_part6,
    MAX(px.ag_part7) AS ag_part7,
    MAX(px."марки авто")                AS "марки авто",
    MAX(px."Название crm")              AS "Название crm",
    MAX(px."тип_заявки")                AS "тип_заявки",
    0::numeric                          AS kol_vo_zayavok,
    0::numeric                          AS korr,
    0::numeric                          AS kval,
    SUM(px.priezd_shifted)              AS priezd,     -- FIX2: SUM сохраняет дробность
    SUM(px.prodazhi_shifted)            AS prodazhi,   -- FIX2: SUM сохраняет дробность
    0::numeric AS nekorr,
    0::numeric AS ne_otvechaet,
    0::numeric AS filtr,
    0::numeric AS nedozvon,
    0::numeric AS priedet,
    0::bigint  AS dohod_do_kredita,
    0::bigint  AS dobro,
    MAX(px."статус")                    AS "статус",
    MAX(px."специалист")                AS "специалист",
    MAX(px."тип_сайта")                 AS "тип_сайта",
    MAX(px."шаблон")                    AS "шаблон",
    px."салон",
    MAX(px."город")                     AS "город",
    MAX(px."регион")                    AS "регион",
    MAX(px.direction)                   AS direction,
    MAX(px."неверный_кодер_new")         AS "неверный_кодер_new",
    MAX(px.fid)                         AS fid,
    MAX(px."проджект")                  AS "проджект",
    MAX(px."id_салона")                 AS "id_салона",
    MAX(px."менеджер")                  AS "менеджер",
    MAX(px."источник")                  AS "источник",
    px."направление",
    MAX(px."номер кампании | название кампании") AS "номер кампании | название кампании",
    MAX(px."номер группы | название группы")     AS "номер группы | название группы",
    MAX(px."План заявки")               AS "План заявки",
    MAX(px."План приезда")              AS "План приезда",
    MAX(px."аккаунт|сайт")              AS "аккаунт|сайт",
    0::bigint AS priezd_arrival_date,     -- claim-счётчик → 0
    0::bigint AS prodazhi_arrival_date,   -- claim-счётчик → 0
    MAX(px."поставщик")                 AS "поставщик",
    MAX(px._source_table)               AS _source_table,
    MAX(px.key_pixel_score)             AS key_pixel_score,
    MAX(px.campaign_status)             AS campaign_status,
    MAX(px.payment_model)               AS payment_model,
    MAX(px.ag_part1_name)               AS ag_part1_name
FROM (
    -- date-shift дробной атрибуции (ДО агрегации) — каждая строка размножена по
    -- eff_arrival_date с весами v_share/s_share, суммы инвариантны (Σshare=1).
    SELECT
        f.key3,
        pxv.new_date::date                  AS "Date",
        {_DOW_SQL_F('pxv.new_date')}        AS "День недели",
        DATE_TRUNC('week', pxv.new_date)::date AS week_start,
        f."CampaignId",
        f."CampaignName",
        f."AdGroupId",
        f."AdGroupName",
        f."AdNetworkType",
        f."Device",
        f.domain,
        f."RlAdjustmentId",
        f."RlAdjustmentId_total",
        f.campaign_code,
        f.tp,
        f.cpc_cpa,
        f.site_quiz,
        f.adgroup_code,
        f.account_login,
        f.manager_login,
        f.ag_part1, f.ag_part2, f.ag_part3, f.ag_part4, f.ag_part5, f.ag_part6, f.ag_part7,
        f."марки авто",
        f."Название crm",
        f."тип_заявки",
        COALESCE(f.priezd, 0)   * pxv.v_share AS priezd_shifted,
        COALESCE(f.prodazhi, 0) * pxv.s_share AS prodazhi_shifted,
        f."статус",
        NULLIF(TRIM(f."специалист"), '') AS "специалист",
        f."тип_сайта",
        f."шаблон",
        f."салон",
        f."город",
        f."регион",
        f.direction,
        f."неверный_кодер_new",
        f.fid,
        f."проджект",
        f."id_салона",
        f."менеджер",
        f."источник",
        -- Визит-метод пикселя носит направление='пиксель_атрибуц' (симметрично claim-методу).
        -- Это МЕТОД атрибуции (claim+visit, по кампаниям), а НЕ факт. Литерал 'пиксель'
        -- остаётся ИСКЛЮЧИТЕЛЬНО за фактом заявок с пикселя (claim, по доменам) и visit его
        -- НЕ несёт. Так на оси 'направление' факт('пиксель') и метод('пиксель_атрибуц') не
        -- мешаются — фильтры отчёта по 'пиксель_атрибуц' корректно ловят visit-метод.
        -- _source_table остаётся 'пиксель_атрибуц' (трассировка). Фильтр-предикат входа
        -- (WHERE f."направление"='пиксель_атрибуц' ниже) НЕ трогаем — он отбирает строки.
        -- Анти-задвоение: лиды-ветка исключает пиксель (utm victory_%), это единственный
        -- источник визит-метода → метка НЕ создаёт дубля.
        'Пиксель_атрибуц'::text          AS "направление",  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
        f."номер кампании | название кампании",
        f."номер группы | название группы",
        f."План заявки",
        f."План приезда",
        f."аккаунт|сайт",
        f."поставщик",
        f._source_table,
        f.key_pixel_score,
        f.campaign_status,
        f.payment_model,
        f.ag_part1_name
    FROM {T_FULL} f
    JOIN pixel_eff_dist pxv
      ON pxv.dom = LOWER(TRIM(f.domain))
     AND pxv.mon = DATE_TRUNC('month', f."Date")::date
    WHERE f."направление" = 'Пиксель_атрибуц'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
      AND f."Date" IS NOT NULL
      AND ( (COALESCE(f.priezd, 0)   > 0 AND pxv.v_share > 0)
         OR (COALESCE(f.prodazhi, 0) > 0 AND pxv.s_share > 0) )
) px
-- FIX2 разрез: (Date, domain, салон, направление, CampaignId/Name) — здоровый
-- кампанийный срез без декартова размножения campaign×date.
GROUP BY px."Date", px.domain, px."салон", px."направление",
         px."CampaignId", px."CampaignName"
"""


# ── PATCH-SPECIALIST-SYMMETRY-2026-06-15 ─────────────────────────────────────
# Устраняет асимметрию специалиста между заявочной (BAF) и визитной (BFA) гранями.
#
# МЕХАНИКА ПРОБЛЕМЫ:
#   BAF: специалист берётся из big_analytics_direct, где corrections.apply() уже
#        переназначил его через rule1/1б/1в по account_login. Например, Кудерко
#        назначается на все аккаунты из _KUDЕРКО_LOGINS для строк Date < 2026-04-10.
#   BFA: специалист берётся из local_gsheet_sites.directologist по домену. Этот
#        справочник хранит ТЕКУЩЕГО специалиста (Терехов/Тумашенко), без учёта
#        исторического переназначения corrections. Итог: 9 доменов Кудерко в BFA
#        числятся за Тереховым/Тумашенко → продажи «По дате визита» < «По заявке».
#
# РЕШЕНИЕ:
#   После CTAS big_analytics_full_arrival применяем те же переназначения,
#   что corrections.apply() делает в BAF — но через JOIN с local_gsheet_sites
#   по login_key вместо прямого account_login (у лидов в BFA нет account_login).
#   GUARD: только _source_table NOT IN ('посевы', 'пиксель_атрибуц') — ветки 3/4
#   берут специалиста из BAF, где corrections уже отработали; трогать их не нужно.
#
# ЗОНА ВЛИЯНИЯ: только поле "специалист" в BFA. Воронка (korr/kval/priezd/prodazhi),
# расход (total_cost=0), источник/направление — НЕ затрагиваются.

def _apply_specialist_corrections(conn) -> dict[str, int]:
    """
    Применяет те же переназначения специалиста к big_analytics_full_arrival,
    что corrections.apply() делает к big_analytics_direct (rule1/1б/1в).
    Маппинг domain→account_login берётся из local_gsheet_sites.login_key.
    Возвращает словарь {имя_правила: кол-во_строк}.
    """
    results = {}

    with conn.cursor() as cur:

        # ── Rule 1: Кудерко Семен (Date < 2026-04-10, logins список) ─────────
        # Находим домены, чей login_key входит в список Кудерко, и
        # переназначаем специалиста для строк с датой визита < cutoff.
        cur.execute(
            f"""
            UPDATE {T_FULL_ARRIVAL} bfa
            SET    "специалист" = %s
            FROM   {T_GSHEET_SITES} gs
            WHERE  LOWER(TRIM(gs."domain")) = LOWER(TRIM(bfa.domain))
              AND  gs.login_key = ANY(%s)
              AND  bfa."Date" < %s::date
              AND  bfa._source_table NOT IN ('посевы', 'пиксель_атрибуц')
            """,
            (_KUDЕРКО_NAME, list(_KUDЕРКО_LOGINS), _KUDЕРКО_DATE),
        )
        n1 = cur.rowcount
        results['rule1_кудерко'] = n1
        logger.info(
            '[step13] PATCH-SPECIALIST rule1 (Кудерко): %d строк BFA переназначено', n1
        )

        # ── Rule 1б: Сергеев Алексей (Date < 2026-04-21, logins список) ──────
        cur.execute(
            f"""
            UPDATE {T_FULL_ARRIVAL} bfa
            SET    "специалист" = %s
            FROM   {T_GSHEET_SITES} gs
            WHERE  LOWER(TRIM(gs."domain")) = LOWER(TRIM(bfa.domain))
              AND  gs.login_key = ANY(%s)
              AND  bfa."Date" < %s::date
              AND  bfa._source_table NOT IN ('посевы', 'пиксель_атрибуц')
            """,
            (_СЕРГЕЕВ_NAME, list(_СЕРГЕЕВ_LOGINS), _СЕРГЕЕВ_DATE),
        )
        n1b = cur.rowcount
        results['rule1б_сергеев'] = n1b
        logger.info(
            '[step13] PATCH-SPECIALIST rule1б (Сергеев): %d строк BFA переназначено', n1b
        )

        # ── Rule 1в: Питеркина Дарья (только где специалист IS NULL/пустой) ──
        # Домен geely-nobosibirsk.ru не имеет directologist в gsheet →
        # в BFA специалист NULL. Заполняем по login_key аналогично rule1в.
        cur.execute(
            f"""
            UPDATE {T_FULL_ARRIVAL} bfa
            SET    "специалист" = %s
            FROM   {T_GSHEET_SITES} gs
            WHERE  LOWER(TRIM(gs."domain")) = LOWER(TRIM(bfa.domain))
              AND  gs.login_key = %s
              AND  (bfa."специалист" IS NULL OR TRIM(bfa."специалист") = '')
              AND  bfa._source_table NOT IN ('посевы', 'пиксель_атрибуц')
            """,
            (_ПИТЕРКИНА_NAME, _ПИТЕРКИНА_LOGIN),
        )
        n1v = cur.rowcount
        results['rule1в_питеркина'] = n1v
        logger.info(
            '[step13] PATCH-SPECIALIST rule1в (Питеркина): %d строк BFA переназначено', n1v
        )

    conn.commit()
    total = sum(results.values())
    logger.info(
        '[step13] PATCH-SPECIALIST-SYMMETRY итого %d строк BFA; детали: %s',
        total, results,
    )
    return results


def run(conn, run_id: str) -> dict:
    """
    Полный пересчёт big_analytics_full_arrival (73-колоночное зеркало).
    Возвращает {'rows': N, 'details': '...'}.
    """
    t0 = time.perf_counter()
    logger.info('step13_arrival: загрузка статусов из local_crm_statuses')

    # leads_scored CTE переименовывает salon → "салон" (кириллица), поэтому
    # salon-override ветка leads-агрегата должна ссылаться на '"салон"', а не 'salon'
    # (иначе при salon-override в local_crm_statuses шаг валится 'column "salon" does
    # not exist' — латентная ветка, та же семья что фикс #11 _build_calls_agg).
    # MARCAR_STRICT_ARRIVAL_2026-07-17 (ред. 2): visit_gate_sql сужает ТОЛЬКО
    # priezd/prodazhi в leads_agg_cases — приезд Маркара засчитывается исключительно
    # при наличии строки в local_gsheet_priezdi_marcar (колонка marcar_visit_ok
    # приходит из leads_eff и calls_eff; оба CTE, использующие leads_agg_cases, её несут).
    # Лид/звонок при этом ОСТАЁТСЯ на визит-оси со своими kol_vo_zayavok/korr/kval.
    # Заявка-ось (step3/step6/step5/corrections) зовёт load_status_sql БЕЗ этого
    # параметра → её SQL байт-в-байт прежний → golden Кудерко не затронут.
    status_sql = load_status_sql(conn, leads_alias_salon='"салон"',
                                 visit_gate_sql='marcar_visit_ok')
    leads_agg_cases     = status_sql['leads_agg_cases']
    priezd_statuses_sql = status_sql['priezd_statuses_sql']

    arrival_sql = _build_arrival_sql(leads_agg_cases, priezd_statuses_sql)

    with conn.cursor() as cur:
        # FIX-ANALYZE (STEP13_HANG_ROOTCAUSE_STALE_STATS, 2026-06-08): первопричина
        # 2ч-зависания step13. step11 ВСТАВЛЯЕТ ~188k пиксель-строк в big_analytics_full
        # СРАЗУ ПЕРЕД step13 (и в fast_pipeline step13 пересобирается повторно после
        # step11). Если last_analyze был ДО вставки, pg_statistic не знает про
        # пиксель-ветку → планировщик оценивает Seq Scan по направление='пиксель_атрибуц'
        # как rows=1 (реально ~97k) → выбирает катастрофический Nested Loop,
        # переисполняющий дорогой pxv-подзапрос (cost 4.18M) на каждую строку → 900с+
        # single-thread / parallel tuple-queue deadlock = ~2ч.
        #
        # SPEEDUP_ANALYZE_2026-06-18: ANALYZE T_FULL → ANALYZE T_FULL (направление).
        # Планировщику нужна статистика по колонке "направление" для оценки
        # WHERE направление='пиксель_атрибуц' (rows ~97k). Полный ANALYZE 3.9M строк
        # тратил ~19 мин; точечный ANALYZE(направление) — ~1-2 мин, эффект тот же
        # (обновляет pg_statistic именно для той колонки которую читает план CTAS).
        # ВАЖНО: НЕ убираем ANALYZE перед CREATE — это фикс STEP13_HANG. Только сужаем
        # область анализа до нужной колонки.
        logger.info(
            'step13_arrival: ANALYZE %s ("направление") — точечно для пиксель-ветки '
            '(SPEEDUP_ANALYZE_2026-06-18, фикс STEP13_HANG сохранён)', T_FULL
        )
        cur.execute(f'ANALYZE {T_FULL} ("направление")')

        logger.info('step13_arrival: SET work_mem = %s', WORK_MEM)
        cur.execute(f"SET work_mem = '{WORK_MEM}'")

        logger.info('step13_arrival: DROP + CREATE %s (73-кол. зеркало)', T_FULL_ARRIVAL)
        cur.execute(f'DROP TABLE IF EXISTS {T_FULL_ARRIVAL}')
        cur.execute(f"""
            CREATE TABLE {T_FULL_ARRIVAL} AS
            {arrival_sql}
        """)
        rows = cur.rowcount
        # UTC_DATE_GUARD_2026-06-26: явный контракт инварианта CANON.md.
        # Строки с "Date" < 2026-01-01 быть не должно. 13 мёртвых строк 2025-12
        # (marcar app.plex-crm.ru/pipelines/...) удаляем превентивно + защита от
        # будущих регрессий UTC-сдвига при смене TZ сервера или коннектора PBI.
        cur.execute(f"""
            DELETE FROM {T_FULL_ARRIVAL}
            WHERE "Date" < '2026-01-01'::date
        """)
        deleted = cur.rowcount
        if deleted:
            logger.warning(
                'step13_arrival: UTC_DATE_GUARD удалил %d строк с "Date" < 2026-01-01',
                deleted,
            )
        conn.commit()

    logger.info('step13_arrival: вставлено %d строк', rows)

    # POSEV_VISIT_DATESHIFT_2026-06-23: логируем orphan-посты (посты без визит-лидов
    # в posev_pool → выпали из визит-оси BFA). Строки в BAF остаются нетронутыми.
    # Это read-only диагностика — на данные не влияет.
    try:
        with conn.cursor() as cur_orphan:
            # POSEV_VISIT_DATESHIFT_2026-06-23: orphan = посевная BAF-строка (priezd>0),
            # для которой нет ни одного посевного лида в local_leads_all с той же
            # (domain, дата_поста=TO_DATE(utm_content,'DDMMYYYY')).
            # Фильтры по f.utm_campaign / f.utm_content убраны — BAF их не содержит.
            # JOIN к local_leads_all: ключ (domain, "Date") как и в posev_eff_dist.
            cur_orphan.execute(f"""
                SELECT COUNT(*) AS orphan_posts,
                       SUM(COALESCE(f.priezd, 0)) AS orphan_priezd,
                       SUM(COALESCE(f.prodazhi, 0)) AS orphan_prodazhi
                FROM {T_FULL} f
                WHERE f."источник" LIKE 'Посевы_%'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
                  AND f."Date" IS NOT NULL
                  -- orphan только для crop_targeting — только у него date-shift включён
                  AND f._source_table = 'crop_targeting'
                  AND (COALESCE(f.priezd, 0) > 0 OR COALESCE(f.prodazhi, 0) > 0)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM {T_LEADS_ALL_LOCAL} l
                      JOIN {T_DOMAINS_LOCAL} ld ON ld.id = l.domain_id
                      WHERE LOWER(TRIM(ld.name)) = LOWER(TRIM(f.domain))
                        AND l.utm_medium = 'posev'
                        AND l.utm_campaign IS NOT NULL AND l.utm_campaign != ''
                        AND COALESCE(l.utm_content, '') ~ '^[0-9]{{8}}$'
                        AND TO_DATE(LPAD(BTRIM(l.utm_content), 8, '0'), 'DDMMYYYY')
                            = f."Date"
                        AND l.is_copy_for_removal IS NOT TRUE
                  )
            """)
            orp = cur_orphan.fetchone()
            logger.info(
                '[step13] POSEV_VISIT_DATESHIFT: orphan-посты (нет визит-лидов) = %s постов, '
                'priezd=%s, prodazhi=%s (остались в BAF по дате заявки, из BFA выпали)',
                orp[0], orp[1], orp[2],
            )
    except Exception as e:
        logger.warning('[step13] POSEV_VISIT_DATESHIFT: orphan-диагностика упала: %s', e)

    # PATCH-SPECIALIST-SYMMETRY-2026-06-15: применяем те же переназначения
    # специалиста, что corrections.apply() делает в BAF. Симметрирует визитную
    # грань с заявочной для rule1 (Кудерко), rule1б (Сергеев), rule1в (Питеркина).
    _apply_specialist_corrections(conn)

    _create_indexes(conn)

    elapsed = time.perf_counter() - t0
    details = (
        f'rows={rows}'
        f'; 73-кол. зеркало big_analytics_full'
        f'; Авто(лиды+звонки, направление по step3-логике) + посевы + пиксель(proxy=дата заявки)'
        f'; claim-метрики (cost/clicks/impressions/arrival-счётчики)=0'
        f'; PATCH-SPECIALIST-SYMMETRY-2026-06-15 применён (rule1/1б/1в)'
    )
    logger.info('step13_arrival: готово за %.1f сек, %d строк', elapsed, rows)
    return {'rows': rows, 'details': details}


def _create_indexes(conn) -> None:
    """Создать индексы на big_analytics_full_arrival — параллельно.

    P4_STEP13_PARALLEL_INDEXES_2026-06-18: портируем паттерн параллельных индексов
    из step7 (threading + Semaphore(3) + отдельное соединение на поток).
    Результат идентичен последовательному — только быстрее (~2-3 мин экономии).

    Семафор _IDX_MAX_PARALLEL=3 ограничивает одновременные get_conn() — аналогично
    step7 (3 idx + прочие потоки ≤ maxconn=12; пул не голодает).
    Семафор берётся ДО get_conn и освобождается ПОСЛЕ put_conn.

    ВАЖНО: ANALYZE T_FULL ПЕРЕД _create_indexes() НЕ трогаем — это фикс
    STEP13_HANG (stale statistics → nested-loop патология). Параллелим только
    сами CREATE INDEX на T_FULL_ARRIVAL (не T_FULL).
    """
    _index_sqls = [
        ('idx_bafa_date',   f'CREATE INDEX IF NOT EXISTS idx_bafa_date   ON {T_FULL_ARRIVAL} ("Date")'),
        ('idx_bafa_salon',  f'CREATE INDEX IF NOT EXISTS idx_bafa_salon  ON {T_FULL_ARRIVAL} ("салон")'),
        ('idx_bafa_domain', f'CREATE INDEX IF NOT EXISTS idx_bafa_domain ON {T_FULL_ARRIVAL} (domain)'),
        ('idx_bafa_src',    f'CREATE INDEX IF NOT EXISTS idx_bafa_src    ON {T_FULL_ARRIVAL} (_source_table)'),
        ('idx_bafa_dir',    f'CREATE INDEX IF NOT EXISTS idx_bafa_dir    ON {T_FULL_ARRIVAL} (direction)'),
        ('idx_bafa_naprav', f'CREATE INDEX IF NOT EXISTS idx_bafa_naprav ON {T_FULL_ARRIVAL} ("направление")'),
        ('idx_bafa_spec',   f'CREATE INDEX IF NOT EXISTS idx_bafa_spec   ON {T_FULL_ARRIVAL} ("специалист")'),
    ]

    _IDX_MAX_PARALLEL = 3
    _idx_sem = threading.Semaphore(_IDX_MAX_PARALLEL)

    def _build_index(idx_name: str, sql: str) -> None:
        _idx_sem.acquire()
        try:
            c = get_conn()
        except Exception:
            _idx_sem.release()
            raise
        try:
            with c.cursor() as cur2:
                cur2.execute(sql)
            c.commit()
            logger.debug('  step13 INDEX %s', idx_name)
        except Exception as e:
            logger.warning('  step13 INDEX %s: %s', idx_name, e)
            try:
                c.rollback()
            except Exception:
                pass
        finally:
            put_conn(c)
            _idx_sem.release()

    threads = [
        threading.Thread(
            target=_build_index, args=(idx_name, sql),
            daemon=True, name=f'bafa_idx_{idx_name}'
        )
        for idx_name, sql in _index_sqls
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    logger.info('step13_arrival: индексы созданы (параллельно, P4_STEP13_PARALLEL_INDEXES_2026-06-18)')


def get_explain_sql(conn) -> str:
    """SELECT-эквивалент для EXPLAIN ANALYZE тяжёлого CTAS step13.

    Используется explain_capture при EXPLAIN_CAPTURE=1. Таблица
    big_analytics_full_arrival уже создана к моменту вызова (после run()).
    Запрос анализирует скан результирующей таблицы по ключевым аналитическим
    разрезам (специалист+дата) — то же, что читает PBI по визитной оси.
    """
    return f"""
        SELECT
            "специалист",
            direction,
            DATE_TRUNC('month', "Date") AS month,
            _source_table,
            COUNT(*)            AS rows,
            SUM(priezd)         AS priezd,
            SUM(prodazhi)       AS prodazhi
        FROM {T_FULL_ARRIVAL}
        WHERE "Date" >= '2026-01-01'
        GROUP BY "специалист", direction, DATE_TRUNC('month', "Date"), _source_table
        ORDER BY month DESC, priezd DESC NULLS LAST
    """
