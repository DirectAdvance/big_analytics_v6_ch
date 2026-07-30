-- =============================================================================
-- 01_init_schema.sql — звёздная схема big_analytics_v6_ch под ClickHouse
-- =============================================================================
-- Этап 1 плана (PLAN.md §4 "Этапы работы"). Применяется к базе `ad_analytics`
-- (managed ClickHouse, Yandex Cloud). НЕ трогает Postgres/Victory, НЕ грузит
-- данные (ETL — Этап 2+), только DDL.
--
-- Источники правды для колонок: work/big_analytics_v5/DB_TABLES.md (типы),
-- PBI_TABLES.md (что читает Power BI), STAR_REFACTOR_BRIEF.md +
-- star_refactor/build_star.py (как строится звезда сейчас в Postgres).
--
-- ─────────────────────────────────────────────────────────────────────────
-- АРХИТЕКТУРНЫЕ РЕШЕНИЯ (PLAN.md §3а, зафиксированы 2026-07-30, НЕ пересматривать
-- в рамках этого файла — только применять):
--
-- 1. ПОЛНОСТЬЮ ПЕРЕСОБИРАЕМЫЕ ВИТРИНЫ (в v5 — TRUNCATE+INSERT / DROP+CTAS каждый
--    прогон) → движок MergeTree. При переносе ETL (Этап 3+) паттерн апдейта —
--    "собери в `<имя>_new`, затем `EXCHANGE TABLES <имя> <имя>_new`" (атомарная
--    подмена, PBI не видит промежуточного состояния). Сам DDL ниже создаёт
--    таблицы под их РЕАЛЬНЫМИ именами (без суффикса _new) — это Этап 1
--    (создание схемы на пустой базе), паттерн _new/EXCHANGE относится к Этапу 3+
--    (ETL-прогонам), не к этому файлу.
--
-- 2. НАКОПИТЕЛЬНЫЕ ТАБЛИЦЫ СОСТОЯНИЯ (в v5 — построчный UPSERT/UPDATE во времени)
--    → ReplacingMergeTree(version) по натуральному бизнес-ключу. ⚠️ ОБЯЗАТЕЛЬНО:
--    для каждой такой таблицы ниже создана ПАРНАЯ VIEW с FINAL — читать
--    ТОЛЬКО через неё (PBI, verify_big_analytics и любой другой потребитель).
--    Читать таблицу напрямую (без FINAL) — источник "почти правильных" метрик
--    с дублями (см. ревью Codex, PLAN.md §3а). Таблицы этой категории в этом
--    файле: campaign_status, analytics_report_placement (ARP raw).
--
-- 3. ДРОБНАЯ ПИКСЕЛЬНАЯ АТРИБУЦИЯ → Decimal(18,6), НЕ Float64 (дрейф плавающей
--    точки на суммах) и НЕ Int (усечение дроби — главный исторический баг
--    проекта, см. GOLDEN_BASELINE.md). Это касается ТОЛЬКО тех измерений
--    воронки, которые реально проходят через step11_pixel_score (дробное
--    распределение кредита): kol_vo_zayavok/korr/kval/priezd/prodazhi И
--    total_cost В fact_big_analytics/fact_ml_korrektirovki (fact_ml_korrektirovki
--    строится как `SELECT f.*` поверх fact_big_analytics — те же дробные меры).
--    ⚠️ ВСЕ ОСТАЛЬНЫЕ факты в этом файле (ARP raw, fact_region/adformat/
--    criterion_spend, fact_region/criterion_zayavki, fact_direct_feed_funnel,
--    fact_vk_ads) строятся НЕ из пиксель-атрибутированного источника, а из
--    ARP (`analytics_report_placement`, BIGINT-счётчики в Postgres) или
--    напрямую из CRM-лидов (`local_leads_all` через `config/status_sql`,
--    целочисленные категории воронки, без пиксельного дробления — см.
--    комментарий в v5 build_region_zayavki.py: "SUM(kol_vo_zayavok) ДОЛЖЕН
--    дать ~154 835" — целое число). Поэтому там воронка — Int64, а не
--    Decimal(18,6): это НЕ отступление от инварианта, а точное отражение
--    того, что дробной атрибуции в этих таблицах нет (проверено по
--    исходному SQL v5, не предположение). Денежные суммы (cost/total_cost/
--    spent) везде — Decimal(18,2) (валюта, 2 знака после запятой,
--    дефолт-решение, не финальное — см. правило 3 задания).
--
-- 4. ПАРТИЦИОНИРОВАНИЕ — PARTITION BY toYYYYMM(<дата>) для всех таблиц с
--    датой (месяц, не день) — стандартная CH-практика для time-series
--    такого объёма (~3-5M строк/год). Dim-таблицы и справочники без даты —
--    без партиционирования (малы, полный скан дёшев).
--
-- 5. ИМЕНОВАНИЕ — 1:1 с v5/PBI_TABLES.md (fact_big_analytics, Dim_Campaign
--    и т.д., БЕЗ суффикса _ch) — отдельная база ad_analytics, коллизий нет.
--    Дефолт-решение, не финальное (переименовать позже дёшево).
--
-- ORDER BY — выбран под то, как реально читает PBI (Import = SELECT * целиком,
-- ORDER BY почти не влияет) и под verify_big_analytics.py / аналитические
-- срезы (фильтры по "Date"/campaign_id/created_date) — обоснование см. в
-- комментарии перед каждой таблицей.
-- =============================================================================


-- =============================================================================
-- SECTION 1 — DIM-ТАБЛИЦЫ (conformed dimensions)
-- =============================================================================
-- Малые справочники (сотни–сотни тысяч строк), полностью пересобираются
-- каждый прогон build_star. MergeTree без партиционирования (нет большого
-- объёма, партиции не нужны); ORDER BY = сам первичный ключ измерения.

-- ─── Dim_Date ────────────────────────────────────────────────────────────
-- PK "Date". ~200 строк/год — без партиционирования.
CREATE TABLE IF NOT EXISTS ad_analytics."Dim_Date"
(
    "Date"          Date,
    week_start      Date,
    "День недели"   LowCardinality(String),
    year            UInt16,
    month           UInt8,
    year_month      LowCardinality(String),
    day             UInt8
)
ENGINE = MergeTree
ORDER BY ("Date");

-- ─── Dim_Campaign ────────────────────────────────────────────────────────
-- PK CampaignId. ⚠️ CampaignId может быть ОТРИЦАТЕЛЬНЫМ (суррогатный ключ
-- для площадок без CampaignId — hashtext(CampaignName), см. build_star.py
-- SURROGATE-CAMPAIGN-FIX) → Int64 (знаковый), НЕ UInt64.
CREATE TABLE IF NOT EXISTS ad_analytics."Dim_Campaign"
(
    "CampaignId"                            Int64,
    "CampaignName"                          Nullable(String),
    "account_login"                         Nullable(String),
    "статус_кампании"                       Nullable(String),
    "специалист"                            LowCardinality(Nullable(String)),
    "manager_login"                         Nullable(String),
    campaign_code                           LowCardinality(Nullable(String)),
    tp                                      LowCardinality(Nullable(String)),
    cpc_cpa                                 LowCardinality(Nullable(String)),
    site_quiz                               LowCardinality(Nullable(String)),
    "campaign_status"                       LowCardinality(Nullable(String)),
    "payment_model"                         LowCardinality(Nullable(String)),
    "номер кампании | название кампании"    Nullable(String)
)
ENGINE = MergeTree
ORDER BY ("CampaignId");

-- ─── Dim_AdGroup ─────────────────────────────────────────────────────────
-- PK AdGroupId. parent_CampaignId — та же знаковая семантика (surrogate).
CREATE TABLE IF NOT EXISTS ad_analytics."Dim_AdGroup"
(
    "AdGroupId"                         Int64,
    "AdGroupName"                       Nullable(String),
    "adgroup_code"                      Nullable(String),
    "номер группы | название группы"    Nullable(String),
    "ag_part1"                          Nullable(String),
    "ag_part2"                          Nullable(String),
    "ag_part3"                          Nullable(String),
    "ag_part4"                          Nullable(String),
    "ag_part5"                          Nullable(String),
    "ag_part6"                          Nullable(String),
    "ag_part7"                          Nullable(String),
    "ag_part1_name"                     Nullable(String),
    "неверный_кодер_new"                LowCardinality(Nullable(String)),
    "parent_CampaignId"                 Nullable(Int64)
)
ENGINE = MergeTree
ORDER BY ("AdGroupId");

-- ─── Dim_Site ────────────────────────────────────────────────────────────
-- PK domain. Решение 2026-07-30: site/domain attributes доступны в Dim_Site
-- для BI-срезов, но row-level версии остаются и в fact_big_analytics.
CREATE TABLE IF NOT EXISTS ad_analytics."Dim_Site"
(
    domain                  Nullable(String),
    "салон"                 LowCardinality(Nullable(String)),
    "город"                 LowCardinality(Nullable(String)),
    "регион"                LowCardinality(Nullable(String)),
    "тип_сайта"             LowCardinality(Nullable(String)),
    "шаблон"                LowCardinality(Nullable(String)),
    "направление"           LowCardinality(Nullable(String)),
    "статус"                LowCardinality(Nullable(String)),
    status                  LowCardinality(Nullable(String)),
    "специалист"            LowCardinality(Nullable(String)),
    "проджект"              LowCardinality(Nullable(String)),
    project_manager         LowCardinality(Nullable(String)),
    "id_салона"             Nullable(String),
    "менеджер"              Nullable(String),
    "Название crm"          Nullable(String)
)
ENGINE = MergeTree
ORDER BY (ifNull(domain, ''), ifNull("салон", ''));

-- ─── Dim_Adjustment ──────────────────────────────────────────────────────
-- PK RlAdjustmentId. RlAdjustmentId_total строго функц. зависит от ключа
-- (1:1, см. build_star.py::build_dim_adjustment) — вынесено с факта сюда.
CREATE TABLE IF NOT EXISTS ad_analytics."Dim_Adjustment"
(
    "RlAdjustmentId"        Int64,
    "RlAdjustmentId_total"  Nullable(String)
)
ENGINE = MergeTree
ORDER BY ("RlAdjustmentId");

-- ─── Dim_Location ────────────────────────────────────────────────────────
-- PK id_location. Измерение РЕГИОНА ПОКАЗА (не салона) — обслуживает
-- fact_region_spend/fact_region_zayavki. distance_km_agreg — верхняя
-- граница бакета расстояния (100/200/.../1900) или NULL.
CREATE TABLE IF NOT EXISTS ad_analytics."Dim_Location"
(
    id_location         Int64,
    location            String,
    "Область"           String,
    "GeoRegionType"      LowCardinality(Nullable(String)),
    distance_km_agreg   Nullable(Int32)
)
ENGINE = MergeTree
ORDER BY (id_location);


-- =============================================================================
-- SECTION 2 — НАКОПИТЕЛЬНЫЕ ТАБЛИЦЫ СОСТОЯНИЯ (ReplacingMergeTree + VIEW)
-- =============================================================================
-- ⚠️⚠️⚠️ ПРАВИЛО ЧТЕНИЯ (обязательно для ВСЕХ потребителей — PBI, verify,
-- любой аналитический SQL): НЕ читать таблицы этой секции напрямую (без
-- FINAL) — ReplacingMergeTree дедуплицирует дубли ТОЛЬКО в фоне при мёрже
-- партов, между мёржами дубли реально лежат на диске и SELECT без FINAL их
-- вернёт. Читать ТОЛЬКО через парную VIEW `<имя>_v` (использует FINAL).
-- Это требование появилось из внешнего ревью (Codex, PLAN.md §3а) — без
-- VIEW-обёртки легко получить "почти правильные" метрики с дублями.

-- ─── campaign_status (+ campaign_status_v) ──────────────────────────────
-- Бизнес-ключ: CampaignId (PK в Postgres). В v5 версии/updated_at НЕТ
-- (колонка была убрана при рефакторинге, см. DB_TABLES.md) — для
-- ReplacingMergeTree нужна версия, поэтому добавлена служебная колонка
-- `_version` (DateTime, дефолт now()) — это РЕШЕНИЕ ЭТОГО DDL (не из
-- источника v5), нужна чисто технически для движка. При переносе ETL
-- (Этап 3) писать в неё `now()` на каждый апдейт строки.
CREATE TABLE IF NOT EXISTS ad_analytics.campaign_status
(
    "CampaignId"        Int64,
    account_login       Nullable(String),
    "статус"            LowCardinality(Nullable(String)),
    "специалист"        LowCardinality(Nullable(String)),
    "CampaignName"       Nullable(String),
    manager_login       Nullable(String),
    campaign_status     LowCardinality(Nullable(String)),
    payment_model       LowCardinality(Nullable(String)),
    _version            DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(_version)
ORDER BY ("CampaignId");

-- Читать ТОЛЬКО через эту VIEW (FINAL гасит дубли между мёржами).
CREATE VIEW IF NOT EXISTS ad_analytics.campaign_status_v AS
SELECT
    "CampaignId",
    account_login,
    "статус",
    "специалист",
    "CampaignName",
    manager_login,
    campaign_status,
    payment_model
FROM ad_analytics.campaign_status
FINAL;

-- ─── analytics_report_placement (+ analytics_report_placement_v) ───────
-- ARP raw — самая тяжёлая накопительная таблица (~8.1M строк / 12 ГБ в
-- Postgres). Бизнес-ключ строки — row_hash (готовый хэш композитного ключа
-- дата×домен×логин×плейсмент×кампания, вычисляется в ETL, НЕ здесь).
-- Версия — updated_at (TIMESTAMP в Postgres, пишется при каждом апдейте).
-- ⚠️ ТИПЫ ВОРОНКИ: kol_vo_zayavok/korr/kval/priezd/prodazhi/nekorr/
-- ne_otvechaet/nedozvon/filtr/priedet/dohod_do_kredita/dobro — BIGINT в
-- Postgres (DB_TABLES.md), НЕ NUMERIC — здесь нет пиксельного дробления
-- (ARP — сырой уровень площадок, пиксель-атрибуция применяется позже, на
-- уровне fact_big_analytics). Поэтому Int64, не Decimal — это точное
-- отражение источника, не отступление от инварианта дробной атрибуции.
-- PARTITION BY месяц (date >= 2026-01-01, инвариант CANON.md).
-- ORDER BY (date, row_hash): date первым — ARP читается по датным окнам
-- (verify/агрегаты по месяцам), row_hash — дедуп-ключ ReplacingMergeTree.
CREATE TABLE IF NOT EXISTS ad_analytics.analytics_report_placement
(
    row_hash                        String,
    date                             Date,
    domain                           Nullable(String),
    "логин"                          Nullable(String),
    ad_network_type                  LowCardinality(Nullable(String)),
    placement                        Nullable(String),
    placement_key                    Nullable(String),
    clicks                           Int64,
    cost                             Decimal(18, 2),
    "Все формы"                      Int64,
    "CRM: Заказ создан"              Int64,
    "CRM: Заказ оплачен"             Int64,
    "CRM: Спам заказ"                Int64,
    "CRM: Заказ отменен"             Int64,
    campaign_id                      Nullable(Int64),
    campaign_name                    Nullable(String),
    campaign_code                    LowCardinality(Nullable(String)),
    tp                               LowCardinality(Nullable(String)),
    cpc_cpa                          LowCardinality(Nullable(String)),
    site_quiz                        LowCardinality(Nullable(String)),
    ad_group_id                      Nullable(Int64),
    key                              Nullable(String),
    key2                             Nullable(String),
    "директолог"                     Nullable(String),
    "город"                          LowCardinality(Nullable(String)),
    "регион"                         LowCardinality(Nullable(String)),
    "салон"                          LowCardinality(Nullable(String)),
    "шаблон"                         LowCardinality(Nullable(String)),
    "тип_сайта"                      LowCardinality(Nullable(String)),
    "статус"                         LowCardinality(Nullable(String)),
    "направление"                    LowCardinality(Nullable(String)),
    updated_at                       DateTime,
    "Название crm"                   Nullable(String),
    "тип_заявки"                     LowCardinality(Nullable(String)),
    kol_vo_zayavok                   Int64,
    korr                             Int64,
    kval                             Int64,
    priezd                           Int64,
    prodazhi                         Int64,
    nekorr                           Int64,
    ne_otvechaet                     Int64,
    filtr                            Int64,
    nedozvon                         Int64,
    priedet                          Int64,
    dohod_do_kredita                 Nullable(Int64),
    dobro                            Nullable(Int64),
    "номер кампании|название кампании" Nullable(String)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(date)
ORDER BY (date, row_hash);

-- Читать ТОЛЬКО через эту VIEW (FINAL гасит дубли между мёржами). Это
-- ANALYTICS_REPORT_PLACEMENT_v, НЕ путать с public.arp_fact из v5 (тот —
-- проекция с переименованными колонками под PBI-партицию; лёгкая
-- проекция под PBI строится отдельно на Этапе 4/5, не здесь).
CREATE VIEW IF NOT EXISTS ad_analytics.analytics_report_placement_v AS
SELECT *
FROM ad_analytics.analytics_report_placement
FINAL;


-- =============================================================================
-- SECTION 3 — ФАКТЫ (полностью пересобираемые, MergeTree, партиция по месяцу)
-- =============================================================================

-- ─── fact_big_analytics ──────────────────────────────────────────────────
-- ГЛАВНЫЙ ФАКТ (golden Кудерко читается отсюда). ~3.8M строк.
-- ⚠️ kol_vo_zayavok/korr/kval/priezd/prodazhi/total_cost — Decimal(18,6)/
-- Decimal(18,2): ЕДИНСТВЕННЫЙ факт в этом файле, где реально проходит
-- дробная пиксель-атрибуция (step11_pixel_score). НИКОГДА не приводить
-- к Int — усечение дроби ломает golden (см. GOLDEN_BASELINE.md, история
-- priezd пикселя 6097→1614 при ошибочном ::integer).
-- CampaignId/AdGroupId — Nullable(Int64): могут быть NULL (посевы/звонки/
-- seo с пустым CampaignName — суррогатный ключ неприменим, see build_fact
-- COALESCE-логика в v5) — сознательно НЕ подставляем sentinel-0, чтобы не
-- исказить связь с Dim_Campaign/Dim_AdGroup (NULL в CH корректно не
-- матчится ни с одной строкой Dim, как и в Postgres LEFT JOIN).
-- ORDER BY ("Date", "CampaignId", domain): "Date" первым — большинство
-- аналитических срезов (PBI-матрицы, verify_big_analytics агрегаты по
-- месяцам) фильтруют/группируют по дате; CampaignId вторым — точечные
-- джойны с Dim_Campaign и посуточные разрезы по кампании; domain третьим —
-- разрез по площадке. PARTITION BY месяц — ~300-400k строк/месяц.
CREATE TABLE IF NOT EXISTS ad_analytics.fact_big_analytics
(
    "CampaignId"                Nullable(Int64),
    "AdGroupId"                  Nullable(Int64),
    "RlAdjustmentId"             Nullable(Int64),
    priezd_arrival_date         Nullable(Int64),
    prodazhi_arrival_date       Nullable(Int64),
    dohod_do_kredita            Nullable(Int64),
    dobro                       Nullable(Int64),
    total_cost                  Decimal(18, 2),
    -- ⚠️ ДРОБНАЯ ПИКСЕЛЬ-АТРИБУЦИЯ — Decimal(18,6), НЕ Int/Float64.
    kol_vo_zayavok               Decimal(18, 6),
    korr                         Decimal(18, 6),
    kval                         Decimal(18, 6),
    priezd                       Decimal(18, 6),
    prodazhi                     Decimal(18, 6),
    -- ⚠️ FIX 2026-07-30 (director review): "Clicks"/"Impressions" — v5-источник NUMERIC
    -- (не int), дробятся по CDR-долям в step3.py (`"Clicks" * (kol_vo_zayavok::NUMERIC /
    -- total_kol)`, маркер CDR_CLICKS_SPLIT_FIX_2026-07-28) → Decimal(18,6), НЕ Int32
    -- (int-каст здесь ломает ту же атрибуцию, что и по воронке — правило 3 задания).
    "Clicks"                     Decimal(18, 6),
    "Impressions"                Decimal(18, 6),
    nekorr                       Int32,
    ne_otvechaet                 Int32,
    nedozvon                     Int32,
    filtr                        Int32,
    priedet                      Int32,
    "План заявки"                Nullable(Int32),
    "План приезда"                Nullable(Int32),
    "Date"                       Date,
    domain                       Nullable(String),
    "атрибуция"                  LowCardinality(String),
    _source_table                LowCardinality(String),
    tp                           LowCardinality(Nullable(String)),
    "источник"                  LowCardinality(Nullable(String)),
    "AdNetworkType"              LowCardinality(Nullable(String)),
    "аккаунт|сайт"               Nullable(String),
    campaign_code                LowCardinality(Nullable(String)),
    "поставщик"                  LowCardinality(Nullable(String)),
    "Device"                     LowCardinality(Nullable(String)),
    fid                          Nullable(String),
    cpc_cpa                      LowCardinality(Nullable(String)),
    "направление"                LowCardinality(Nullable(String)),
    site_quiz                    LowCardinality(Nullable(String)),
    "марки авто"                 Nullable(String),
    "специалист"                 LowCardinality(Nullable(String)),
    "тип_сайта"                  LowCardinality(Nullable(String)),
    "статус"                     LowCardinality(Nullable(String)),
    status                       LowCardinality(Nullable(String)),
    "салон"                      LowCardinality(Nullable(String)),
    "шаблон"                     LowCardinality(Nullable(String)),
    "id_салона"                  Nullable(String),
    "город"                      LowCardinality(Nullable(String)),
    "регион"                     LowCardinality(Nullable(String)),
    "проджект"                   LowCardinality(Nullable(String)),
    project_manager              LowCardinality(Nullable(String)),
    "менеджер"                   Nullable(String),
    "Название crm"               Nullable(String),
    "тип_заявки"                 LowCardinality(Nullable(String)),
    manager_login                Nullable(String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM("Date")
ORDER BY ("Date", "CampaignId", domain)
SETTINGS allow_nullable_key = 1;

-- ─── fact_region_spend ────────────────────────────────────────────────────
-- Расход по гео-регионам показа. Источник — ARP (BIGINT-счётчики) → воронка
-- Int64 (не пиксель-атрибутирована, см. пояснение в шапке файла).
CREATE TABLE IF NOT EXISTS ad_analytics.fact_region_spend
(
    row_hash             String,
    date                 Date,
    campaign_id          Nullable(Int64),
    campaign_name        Nullable(String),
    ad_group_id          Nullable(Int64),
    ad_network_type      LowCardinality(Nullable(String)),
    id_location          Nullable(Int64),
    location             Nullable(String),
    "Область"            Nullable(String),
    "GeoRegionType"       LowCardinality(Nullable(String)),
    distance_km          Nullable(Int32),
    distance_km_agreg    Nullable(Int32),
    impressions          Int64,
    clicks               Int64,
    cost                 Decimal(18, 2),
    "Все формы"           Int64,
    "CRM: Заказ создан"   Int64,
    "CRM: Заказ оплачен"  Int64,
    "CRM: Спам заказ"     Int64,
    "CRM: Заказ отменен"  Int64,
    domain               Nullable(String),
    "логин"              Nullable(String),
    "директолог"         Nullable(String),
    "город"              LowCardinality(Nullable(String)),
    "регион"             LowCardinality(Nullable(String)),
    "салон"              LowCardinality(Nullable(String)),
    "шаблон"             LowCardinality(Nullable(String)),
    "тип_сайта"          LowCardinality(Nullable(String)),
    "статус"             LowCardinality(Nullable(String)),
    "направление"        LowCardinality(Nullable(String)),
    project_manager      Nullable(String),
    client_id            Nullable(String),
    campaign_code        LowCardinality(Nullable(String)),
    tp                   LowCardinality(Nullable(String)),
    cpc_cpa              LowCardinality(Nullable(String)),
    site_quiz            LowCardinality(Nullable(String)),
    updated_at           DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (date, campaign_id, id_location)
SETTINGS allow_nullable_key = 1;

-- ─── fact_adformat_spend ──────────────────────────────────────────────────
-- Расход по формату объявления. Та же грань, что fact_region_spend, но ось
-- ad_format вместо id_location.
CREATE TABLE IF NOT EXISTS ad_analytics.fact_adformat_spend
(
    row_hash             String,
    date                 Date,
    campaign_id          Nullable(Int64),
    campaign_name        Nullable(String),
    ad_group_id          Nullable(Int64),
    ad_network_type      LowCardinality(Nullable(String)),
    ad_format            LowCardinality(Nullable(String)),
    impressions          Int64,
    clicks               Int64,
    cost                 Decimal(18, 2),
    "Все формы"           Int64,
    "CRM: Заказ создан"   Int64,
    "CRM: Заказ оплачен"  Int64,
    "CRM: Спам заказ"     Int64,
    "CRM: Заказ отменен"  Int64,
    domain               Nullable(String),
    "логин"              Nullable(String),
    "директолог"         Nullable(String),
    "город"              LowCardinality(Nullable(String)),
    "регион"             LowCardinality(Nullable(String)),
    "салон"              LowCardinality(Nullable(String)),
    "шаблон"             LowCardinality(Nullable(String)),
    "тип_сайта"          LowCardinality(Nullable(String)),
    "статус"             LowCardinality(Nullable(String)),
    "направление"        LowCardinality(Nullable(String)),
    project_manager      Nullable(String),
    client_id            Nullable(String),
    campaign_code        LowCardinality(Nullable(String)),
    tp                   LowCardinality(Nullable(String)),
    cpc_cpa              LowCardinality(Nullable(String)),
    site_quiz            LowCardinality(Nullable(String)),
    adgroup_code         Nullable(String),
    adgroup_brand        LowCardinality(Nullable(String)),
    updated_at           DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (date, campaign_id, ad_group_id)
SETTINGS allow_nullable_key = 1;

-- ─── fact_criterion_spend ─────────────────────────────────────────────────
-- Расход по критерию/ключевику (autotargeting/retargeting/interests/keyword,
-- скоуп ниши «Авто»).
CREATE TABLE IF NOT EXISTS ad_analytics.fact_criterion_spend
(
    row_hash             String,
    date                 Date,
    campaign_id          Nullable(Int64),
    campaign_name        Nullable(String),
    ad_group_id          Nullable(Int64),
    ad_network_type      LowCardinality(Nullable(String)),
    criterion_id         Nullable(Int64),
    criterion             Nullable(String),
    criterion_raw         Nullable(String),
    criterion_type        LowCardinality(Nullable(String)),
    impressions          Int64,
    clicks               Int64,
    cost                 Decimal(18, 2),
    "Все формы"           Int64,
    "CRM: Заказ создан"   Int64,
    "CRM: Заказ оплачен"  Int64,
    "CRM: Спам заказ"     Int64,
    "CRM: Заказ отменен"  Int64,
    domain               Nullable(String),
    "логин"              Nullable(String),
    "директолог"         Nullable(String),
    "город"              LowCardinality(Nullable(String)),
    "регион"             LowCardinality(Nullable(String)),
    "салон"              LowCardinality(Nullable(String)),
    "шаблон"             LowCardinality(Nullable(String)),
    -- ⚠️ FIX 2026-07-30 (director review): недостающая колонка — источник
    -- criterion_spend/build_criterion_spend.py:121 (TEXT, обогащение из
    -- local_gsheet_sites рядом с "шаблон"). PG=36/CH=35 до фикса.
    "шаблон_марка"       LowCardinality(Nullable(String)),
    "тип_сайта"          LowCardinality(Nullable(String)),
    "статус"             LowCardinality(Nullable(String)),
    "направление"        LowCardinality(Nullable(String)),
    project_manager      Nullable(String),
    client_id            Nullable(String),
    campaign_code        LowCardinality(Nullable(String)),
    tp                   LowCardinality(Nullable(String)),
    cpc_cpa              LowCardinality(Nullable(String)),
    site_quiz            LowCardinality(Nullable(String)),
    updated_at           DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (date, campaign_id, criterion_id)
SETTINGS allow_nullable_key = 1;

-- ─── fact_region_zayavki ──────────────────────────────────────────────────
-- Воронка ПО ЗАЯВКАМ в разрезе региона (близнец fact_region_spend, но грань
-- заявок, не расхода). Источник — public.local_leads_all (CRM-лиды через
-- config/status_sql), НЕ пиксель-атрибуция → Int64 (см. шапку файла:
-- комментарий v5 build_region_zayavki.py "SUM(kol_vo_zayavok) ДОЛЖЕН дать
-- ~154 835" — целое число, проверено по исходнику). Дата = created_date
-- (день заявки), НЕ день показа. Связи в PBI: только campaign_id/created_date
-- (нет ad_group/domain — этих осей у лидов нет, иначе задвоение).
CREATE TABLE IF NOT EXISTS ad_analytics.fact_region_zayavki
(
    row_hash             String,
    created_date         Date,
    campaign_id          Nullable(Int64),
    campaign_name        Nullable(String),
    id_location          Nullable(Int64),
    location             Nullable(String),
    "Область"            Nullable(String),
    "GeoRegionType"       LowCardinality(Nullable(String)),
    distance_km          Nullable(Int32),
    distance_km_agreg    Nullable(Int32),
    "салон"              LowCardinality(Nullable(String)),
    domain_id            Nullable(Int64),
    kol_vo_zayavok        Int64,
    korr                 Int64,
    kval                 Int64,
    priezd               Int64,
    prodazhi             Int64,
    nekorr               Int64,
    ne_otvechaet          Int64,
    filtr                Int64,
    nedozvon             Int64,
    priedet              Int64,
    dohod_do_kredita      Nullable(Int64),
    dobro                Nullable(Int64),
    updated_at           DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(created_date)
ORDER BY (created_date, campaign_id, id_location)
SETTINGS allow_nullable_key = 1;

-- ─── fact_criterion_zayavki ────────────────────────────────────────────────
-- Воронка ПО ЗАЯВКАМ в разрезе критерия/ключа (близнец fact_criterion_spend).
-- Та же логика типов, что fact_region_zayavki (CRM-целочисленная воронка).
CREATE TABLE IF NOT EXISTS ad_analytics.fact_criterion_zayavki
(
    row_hash             String,
    created_date         Date,
    campaign_id          Nullable(Int64),
    campaign_name        Nullable(String),
    criterion             Nullable(String),
    criterion_type        LowCardinality(Nullable(String)),
    criterion_raw         Nullable(String),
    "салон"              LowCardinality(Nullable(String)),
    domain_id            Nullable(Int64),
    -- ⚠️ FIX 2026-07-30 (director review): 2 недостающие колонки — источник
    -- criterion_spend/build_criterion_zayavki.py:121 (TEXT, обогащение по
    -- campaign_id из fact_criterion_spend, 1:1). PG=24/CH=22 до фикса.
    "шаблон"             LowCardinality(Nullable(String)),
    "шаблон_марка"       LowCardinality(Nullable(String)),
    kol_vo_zayavok        Int64,
    korr                 Int64,
    kval                 Int64,
    priezd               Int64,
    prodazhi             Int64,
    nekorr               Int64,
    ne_otvechaet          Int64,
    filtr                Int64,
    nedozvon             Int64,
    priedet              Int64,
    dohod_do_kredita      Nullable(Int64),
    dobro                Nullable(Int64),
    updated_at           DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(created_date)
ORDER BY (created_date, campaign_id)
SETTINGS allow_nullable_key = 1;

-- ─── fact_direct_feed_funnel ──────────────────────────────────────────────
-- Воронка по фиду (grain: date × domain × feed_key, для tp6/tp7 —
-- date × campaign_id × feed_key; adgroup_id/adgroup_name — NULL по гранe,
-- сохранены для PBI-совместимости, см. direct_feed_funnel/build_keyed.py
-- FEED_ADGROUP_NULLCOL_2026-06-29). Источник заявок — direct_feed_lead_attribution
-- (сборка через utm_content/fid, целочисленные CRM-категории, не пиксель) →
-- Int64. dk2 — суррогатный текстовый ключ грани (аналог row_hash).
CREATE TABLE IF NOT EXISTS ad_analytics.fact_direct_feed_funnel
(
    dk2                          String,
    date                         Date,
    login_key                    Nullable(String),
    domain                       Nullable(String),
    campaign_id                  Nullable(Int64),
    campaign_name                Nullable(String),
    adgroup_id                   Nullable(Int64),
    adgroup_name                 Nullable(String),
    feed_id                      Nullable(Int64),
    feed_name                    Nullable(String),
    feed_url                     Nullable(String),
    feed_url_key                 Nullable(String),
    feed_key                     Nullable(String),
    is_tp67                      Nullable(Bool),
    impressions                  Int64,
    clicks                       Int64,
    total_cost                   Decimal(18, 2),
    goal_all_forms                Int64,
    goal_crm_order_created        Int64,
    goal_crm_order_paid           Int64,
    goal_crm_spam_order           Int64,
    goal_crm_order_canceled       Int64,
    attributed_leads              Int64,
    kol_vo_zayavok                Int64,
    korr                         Int64,
    kval                         Int64,
    priezd                       Int64,
    prodazhi                     Int64,
    nekorr                       Int64,
    ne_otvechaet                  Int64,
    filtr                        Int64,
    nedozvon                     Int64,
    priedet                      Int64,
    dohod_do_kredita              Nullable(Int64),
    dobro                        Nullable(Int64),
    generated_at                  DateTime
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (date, feed_key, campaign_id)
SETTINGS allow_nullable_key = 1;

-- ─── fact_vk_ads ──────────────────────────────────────────────────────────
-- VK Ads: грань date × account × ad_plan(оффер) × ad_group(сегмент) ×
-- banner(объявление) × атрибуция ('По дате заявки'/'По дате визита').
-- Воронка построена из VK-лидов (CRM, целочисленно — "VK-лиды целочисленные,
-- дробной пиксель-атрибуции здесь нет", см. build_vk_ads_fact docstring v5)
-- → Int64. shows/clicks/spent несёт ТОЛЬКО ось 'По дате заявки' (на визит-оси
-- = 0, дедуп — SUM по всей таблице не удваивается, см. PBI_TABLES.md).
-- golden Кудерко НЕ затрагивается (VK вне GOLDEN_SOURCES).
CREATE TABLE IF NOT EXISTS ad_analytics.fact_vk_ads
(
    date            Date,
    account_id      Nullable(Int64),
    "салон"         LowCardinality(Nullable(String)),
    ad_plan_id      Nullable(Int64),
    ad_plan_name    Nullable(String),
    ad_group_id     Nullable(Int64),
    ad_group_name   Nullable(String),
    banner_id       Nullable(Int64),
    banner_name     Nullable(String),
    "атрибуция"     LowCardinality(String),
    shows           Int64,
    clicks          Int64,
    spent           Decimal(18, 2),
    "заявки"        Int64,
    "заявки_корр"   Int64,
    "записи"        Int64,
    "квал"          Int64,
    "визиты"        Int64,
    "продажи"       Int64,
    "регион"        LowCardinality(Nullable(String)),
    "тип_сайта"     LowCardinality(Nullable(String)),
    "специалист"    LowCardinality(Nullable(String))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (date, account_id, ad_group_id, banner_id)
SETTINGS allow_nullable_key = 1;

-- ─── fact_ml_korrektirovki ────────────────────────────────────────────────
-- ML-аудиторные корректировки ставок: fact_big_analytics (ключ RlAdjustmentId)
-- JOIN yandex_direct_korrektirovki (ILIKE '%_ml_%'). В v5 строится как
-- `SELECT f.* FROM fact_big_analytics f JOIN ml_korr ...` — те же дробные
-- меры воронки/расхода, что fact_big_analytics (Decimal(18,6)/Decimal(18,2)),
-- + 3 новые колонки (ml_audience_name/bid_percent/ml_tier). ⚠️ Зависимость
-- yandex_direct_korrektirovki НЕ создаётся в этом DDL (не входит в список
-- Этапа 1 из задания) — таблица здесь описывает СХЕМУ витрины; наполнение
-- (Этап 3+) потребует либо перенести yandex_direct_korrektirovki отдельно,
-- либо построить fact_ml_korrektirovki на пустом множестве (as в v5-guard
-- "korrektirovki ещё не запускался — создаём пустую таблицу").
-- ORDER BY идентичен fact_big_analytics (та же грань + фильтр по аудитории).
CREATE TABLE IF NOT EXISTS ad_analytics.fact_ml_korrektirovki
(
    "CampaignId"                Nullable(Int64),
    "AdGroupId"                  Nullable(Int64),
    "RlAdjustmentId"             Nullable(Int64),
    priezd_arrival_date         Nullable(Int64),
    prodazhi_arrival_date       Nullable(Int64),
    dohod_do_kredita            Nullable(Int64),
    dobro                       Nullable(Int64),
    total_cost                  Decimal(18, 2),
    kol_vo_zayavok               Decimal(18, 6),
    korr                         Decimal(18, 6),
    kval                         Decimal(18, 6),
    priezd                       Decimal(18, 6),
    prodazhi                     Decimal(18, 6),
    -- ⚠️ FIX 2026-07-30 (director review) — см. идентичный комментарий в fact_big_analytics.
    "Clicks"                     Decimal(18, 6),
    "Impressions"                Decimal(18, 6),
    nekorr                       Int32,
    ne_otvechaet                 Int32,
    nedozvon                     Int32,
    filtr                        Int32,
    priedet                      Int32,
    "План заявки"                Nullable(Int32),
    "План приезда"                Nullable(Int32),
    "Date"                       Date,
    domain                       Nullable(String),
    "атрибуция"                  LowCardinality(String),
    _source_table                LowCardinality(String),
    tp                           LowCardinality(Nullable(String)),
    "источник"                  LowCardinality(Nullable(String)),
    "AdNetworkType"              LowCardinality(Nullable(String)),
    "аккаунт|сайт"               Nullable(String),
    campaign_code                LowCardinality(Nullable(String)),
    "поставщик"                  LowCardinality(Nullable(String)),
    "Device"                     LowCardinality(Nullable(String)),
    fid                          Nullable(String),
    cpc_cpa                      LowCardinality(Nullable(String)),
    "направление"                LowCardinality(Nullable(String)),
    site_quiz                    LowCardinality(Nullable(String)),
    "марки авто"                 Nullable(String),
    "специалист"                 LowCardinality(Nullable(String)),
    "тип_сайта"                  LowCardinality(Nullable(String)),
    "статус"                     LowCardinality(Nullable(String)),
    status                       LowCardinality(Nullable(String)),
    "салон"                      LowCardinality(Nullable(String)),
    "шаблон"                     LowCardinality(Nullable(String)),
    "id_салона"                  Nullable(String),
    "город"                      LowCardinality(Nullable(String)),
    "регион"                     LowCardinality(Nullable(String)),
    "проджект"                   LowCardinality(Nullable(String)),
    project_manager              LowCardinality(Nullable(String)),
    "менеджер"                   Nullable(String),
    "Название crm"               Nullable(String),
    "тип_заявки"                 LowCardinality(Nullable(String)),
    manager_login                Nullable(String),
    ml_audience_name             Nullable(String),
    bid_percent                  Nullable(String),
    ml_tier                      LowCardinality(Nullable(String))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM("Date")
ORDER BY ("Date", "CampaignId", domain)
SETTINGS allow_nullable_key = 1;

-- =============================================================================
-- КОНЕЦ 01_init_schema.sql — 15 MergeTree-таблиц (6 Dim + 9 fact) +
-- 2 ReplacingMergeTree (campaign_status, analytics_report_placement) +
-- 2 парные VIEW (_v). Итого 19 объектов. Применено к ad_analytics
-- (2026-07-30), см. отчёт .claude/sdd/v6-etap1-ddl-report.md.
-- =============================================================================
