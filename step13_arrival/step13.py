"""Step 13 for v6_ch: build the REAL visit axis (`big_analytics_full_arrival`).

ARRIVAL_REAL_AXIS_2026-08-05 — порт механики v5 `step13_arrival/step13.py` на ClickHouse.

════════════════════════════════════════════════════════════════════════════════
ЧТО БЫЛО (баг, который здесь чинится)
════════════════════════════════════════════════════════════════════════════════
Предыдущая версия делала `INSERT ... SELECT * FROM big_analytics_full WHERE priezd>0
OR prodazhi>0` и обнуляла только Impressions/Clicks/total_cost. `Date` копировалась
как есть → «По дате визита» был отфильтрованной КОПИЕЙ «По дате заявки»:
помесячные суммы совпадали побитово, `priezd_arrival_date` был NULL у 100% строк.

════════════════════════════════════════════════════════════════════════════════
ЧТО СТАЛО — ось строится ЗАНОВО от даты визита
════════════════════════════════════════════════════════════════════════════════
Три ветки UNION ALL (в v5 их четыре: посевная ветка v5 здесь не нужна — см. ниже):

  1. ЛИДЫ (`_leads_branch`) — из `ad_analytics.raw_leads` через тот же дедуп
     (`step3._leads_deduped_cte`), что и заявочная ось.
     `Date` = eff_arrival_date (v5 step13.py:521):
        GREATEST(COALESCE(<дата приезда Маркара из gsheet>, <дата по правилам CRM>),
                 created_date)
     Правила CRM (v5 step13.py:444-448): plex/marcar → created_date (у них своей
     даты визита нет), остальные → COALESCE(arrival_date, created_date).
     Воронка — РЕАЛЬНАЯ, считается тем же `step3._metric_expr` (единственное
     определение воронки в проекте: korr/kval/priezd/prodazhi/nekorr/reason-метрики).

  2. ЗВОНКИ (`_calls_branch`) — из `ad_analytics.raw_calls` (там уже применён патч
     статусов Маркара из step1) + `raw_data.leads_all` по `id` за `arrival_date` и
     `source_record_id` (в `raw_calls` этих колонок нет). Та же eff_arrival_date.

  3. ПИКСЕЛЬ (`_pixel_branch`) — порт v5-ветки 4 (`DATE-SHIFT` дробной атрибуции).
     Источник — `big_analytics_full WHERE направление='Пиксель_атрибуц'` (ДРОБНАЯ
     атрибуция step11). У пиксельной строки витрины своей CRM-даты нет, поэтому
     внутри (домен, месяц-заявки) строится распределение визит-лидов пиксель-пула
     по eff_arrival_date (доля v_share, Σ=1 на группу) и каждая дробная строка
     размножается по реальным датам с этими весами (одна доля на приезды и продажи —
     см. комментарий в `_pixel_branch_sql`).
     Σ priezd / Σ prodazhi ИНВАРИАНТНЫ, дробность сохраняется (Float64 → Decimal,
     округление только у итоговой суммы, НИКАКОГО int-каста).

════════════════════════════════════════════════════════════════════════════════
ИНВАРИАНТЫ (проверять при любой правке)
════════════════════════════════════════════════════════════════════════════════
  * Заявочная ось не читается на запись и не меняется — шаг пишет ТОЛЬКО
    `big_analytics_full_arrival`.
  * Расход/клики/показы на визитной оси = 0. Здесь это достигается тем, что
    колонки `Impressions`/`Clicks`/`total_cost` вообще НЕ перечисляются в списке
    INSERT — ClickHouse кладёт дефолт типа Decimal(18,6) = 0. Деньги живут только
    на заявочной оси, задваивать нельзя.
  * `priezd_arrival_date`/`prodazhi_arrival_date` = 0 (НЕ NULL). Это claim-счётчики,
    v5 обнуляет их на визитной оси (замер v5: count(*)=count(non-null)=117 215,
    sum=0). Прежняя версия оставляла NULL — это и был признак «зеркала».
  * Дробная пиксельная атрибуция не приводится к int.
  * Вложенность korr ≥ kval ≥ priezd ≥ prodazhi держится тем, что метрики берутся
    из `_metric_expr` (категории вложены по построению) и суммируются по группе.
  * `Date >= DATE_FROM` — гарантируется фильтрами веток.

════════════════════════════════════════════════════════════════════════════════
ОСОЗНАННЫЕ ОТЛИЧИЯ ОТ v5 (не забыть при ревью)
════════════════════════════════════════════════════════════════════════════════
  * ПОСЕВНАЯ ВЕТКА v5 (3A date-shift + 3B proxy) НЕ портируется. В v5 посевные
    строки витрины — это АГРЕГАТЫ ПОСТОВ (`Date` = дата поста, лид-уровня нет),
    поэтому v5 вынужден раскладывать их по долям. В v6 `big_analytics_crop_targeting`
    строится `step3._build_lead_source_sql` ОТ ЛИДА (`Date` = created_date лида),
    значит посевной лид несёт собственную дату визита и покрывается веткой 1
    напрямую — точнее, чем распределение по долям.
  * tp8/tp9/tp10: v5 вырезает их из лид-ветки и берёт из витрины. Здесь они
    классифицируются по `tp` кампании лида (camp_dict) — те же значения
    `_source_table`/`направление`, что ставит заявочная ось (step3.py:657/666).
  * AD-DIMS: v5 тянет их через `raw_yandex.key3 = raw_leads.key3_arrival_date`
    (в день визита открутки может не быть → имена терялись) плюс date-independent
    справочник. Здесь оставлен ТОЛЬКО справочник (camp_dict/ag_dict по
    campaign_id/group_id лида) — покрытие имён выше, стоимость ниже, на воронку
    и деньги не влияет. `AdNetworkType` на визитной оси = NULL.
  * MARCAR_STRICT_ARRIVAL (v5 гасит priezd/prodazhi у лида Маркара без строки в
    gsheet) НЕ портирован: в v6 патч статусов Маркара уже применён в step1 к ОБЕИМ
    осям, и гейт только на визитной оси создал бы асимметрию, которой нет в v5-логике
    v6. Замер цены решения — в отчёте (порядка 0.7% приездов).
  * Метки (`источник`/`направление`/`_source_table`/`поставщик`) берутся по конвенции
    ЗАЯВОЧНОЙ оси v6 (step3/step6), а не v5 — чтобы срез по `атрибуция` в
    `big_analytics_unified` фильтровался одинаково на обеих осях.

Зависимости (IN):  ad_analytics.raw_leads, ad_analytics.raw_calls,
                   ad_analytics.big_analytics_full, ad_analytics.local_pixel_config,
                   ad_analytics.raw_yandex, raw_data.leads_all, raw_data.gsheet_sites,
                   raw_data.gsheet_priezdi_marcar, raw_data.crm_status_mapping
Выход (OUT):       ad_analytics.big_analytics_full_arrival
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, column_names, count_rows, q, swap_shadow
from corrections import specialist_correction_expr
from step3_build_sources.step3 import (
    _ag_parts_expr,
    _category_match_expr,
    _gs_account_cte,
    _leads_deduped_cte,
    _metric_expr,
    _weekday_expr,
)

logger = logging.getLogger("pipeline.step13")

TARGET = "ad_analytics.big_analytics_full_arrival"
SHADOW = "ad_analytics.big_analytics_full_arrival_new"

# Ветки читают всю историю лидов одним запросом (дедуп по (phone, yclid) — оконный,
# по месяцам не режется). Лимиты подняты по образцу STEP3_QUERY_SETTINGS.
STEP13_QUERY_SETTINGS = {
    **SAFE_QUERY_SETTINGS,
    "max_execution_time": 900,
    "max_memory_usage": 6_000_000_000,
    "max_bytes_before_external_group_by": 2_000_000_000,
    "max_bytes_before_external_sort": 2_000_000_000,
}

# Статусы Маркара из гугл-таблицы приездов, 0 = самый глубокий (v5 step13.py:273-278).
_MARCAR_PRIO = (
    "multiIf(status = 'Продажа', 0, status = 'Дошел в КО', 1, "
    "status = 'Одобрение', 2, status = 'Приехал', 3, 9999)"
)

# UTM соц.посевов — тот же список, что `step3._build_crop_sql` / `_direct_lead_universe_filter`.
_POSEV_UTM_SOURCES = "'telegram','stories_tg','vk_storis','telegram_storis','max','vk','vk_groups','vkads'"


def _device_from_utm(utm_content_expr: str) -> str:
    """Device из dev:-токена utm_content — та же логика, что в key3 (step1.py:313-318)."""
    u = f"ifNull({utm_content_expr}, '')"
    return f"""
multiIf(
    position({u}, 'dev:mobile') > 0, 'mobile',
    position({u}, 'dev:desktop') > 0, 'desktop',
    position({u}, 'dev:tablet') > 0, 'tablet',
    position({u}, 'dev:smart_tv') > 0, 'smart_tv',
    CAST(NULL, 'Nullable(String)')
)
"""


# ══════════════════════════════════════════════════════════════════════════════
# Общие CTE
# ══════════════════════════════════════════════════════════════════════════════
def _marcar_arrivals_cte() -> str:
    """id карточки Маркара -> дата приезда из гугл-таблицы (порт v5 marcar_arrivals).

    v5 берёт запись с САМОЙ ПОЗДНЕЙ датой визита, при равной дате — самый глубокий
    статус (Продажа > Дошел в КО > Одобрение > Приехал). Здесь это argMax по
    кортежу (дата, -приоритет), что даёт ту же выборку.
    """
    return f"""
marcar_arrivals AS
(
    SELECT
        lead_record_id,
        argMax(arrival_date_marcar, tuple(arrival_date_marcar, -prio)) AS arrival_date_marcar
    FROM
    (
        SELECT
            replaceRegexpOne(ifNull(link, ''), '^.+/', '') AS lead_record_id,
            toDate(parseDateTimeBestEffortOrNull(trim(ifNull(date, '')))) AS arrival_date_marcar,
            {_MARCAR_PRIO} AS prio
        FROM raw_data.gsheet_priezdi_marcar
        WHERE match(ifNull(link, ''), '^https?://.+/[0-9]+$')
          AND match(trim(ifNull(date, '')), '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$')
          AND toDate(parseDateTimeBestEffortOrNull(trim(ifNull(date, '')))) IS NOT NULL
    )
    GROUP BY lead_record_id
),
marcar_record_ids AS
(
    SELECT id, ifNull(source_record_id, '') AS source_record_id
    FROM raw_data.leads_all
    WHERE source_type = 'marcar_crm_excel'
      AND ifNull(source_record_id, '') != ''
)
"""


def _ad_dicts_cte() -> str:
    """Date-independent справочники имён кампаний/групп (v5 camp_dict/ag_dict).

    В v5 источник — FDW `yandex_direct_manager_reports`; в v6 тот же материал лежит
    в `ad_analytics.raw_yandex`. Справочник резолвит имя по campaign_id/group_id
    САМОГО ЛИДА, поэтому не зависит от того, была ли открутка в день визита.
    """
    return """
camp_dict AS
(
    SELECT
        `CampaignId` AS cid,
        max(`CampaignName`) AS `CampaignName`,
        max(campaign_code) AS campaign_code,
        max(tp) AS tp,
        max(cpc_cpa) AS cpc_cpa,
        max(site_quiz) AS site_quiz,
        max(account_login) AS account_login,
        max(manager_login) AS manager_login
    FROM ad_analytics.raw_yandex
    WHERE `CampaignId` != 0
    GROUP BY `CampaignId`
),
ag_dict AS
(
    SELECT
        `AdGroupId` AS agid,
        max(`AdGroupName`) AS `AdGroupName`,
        max(adgroup_code) AS adgroup_code
    FROM ad_analytics.raw_yandex
    WHERE `AdGroupId` != 0
    GROUP BY `AdGroupId`
)
"""


def _eff_arrival_expr(alias: str, marcar_alias: str = "ma") -> str:
    """Эффективная дата визита (порт v5 step13.py:444-448 + :521).

    `assumeNotNull` снимает Nullable, который тянется от `created_date`/`arrival_date`:
    колонка `Date` витрины — не-Nullable Date, и без снятия INSERT падал бы на
    приведении типа. Строки с `created_date IS NULL` отсекаются WHERE обеих веток.
    """
    return f"""
assumeNotNull(greatest(
    ifNull(
        {marcar_alias}.arrival_date_marcar,
        multiIf(
            {alias}.source_type IN ('plex_excel', 'marcar_crm_excel'), {alias}.created_date,
            ifNull({alias}.arrival_date, {alias}.created_date)
        )
    ),
    {alias}.created_date
))
"""


def _auto_domains_filter(domain_expr: str) -> str:
    """Гейт `direction='Авто'` (v5 step13.py:390) — строгое равенство, NULL исключается."""
    return f"""
lower(trim(ifNull({domain_expr}, ''))) IN (
    SELECT lower(trim(ifNull(domain, '')))
    FROM raw_data.gsheet_sites
    WHERE direction = 'Авто' AND ifNull(domain, '') != ''
)
"""


# ══════════════════════════════════════════════════════════════════════════════
# Ветка 1 — лиды
# ══════════════════════════════════════════════════════════════════════════════
def _leads_branch_columns() -> dict[str, str]:
    """Колонка витрины -> выражение. Колонки, которых здесь нет, ClickHouse заполнит
    дефолтом типа (Decimal→0, String→'', Nullable→NULL) — именно так обнуляются
    claim-метрики Impressions/Clicks/total_cost."""
    specialist = specialist_correction_expr(
        "g.eff_arrival_date", "g.account_login", "g.specialist_raw", "g.domain"
    )
    return {
        "key3": "concat('visit_lead|', g._source_table, '|', toString(g.eff_arrival_date), "
                "'|', ifNull(g.domain, ''), '|', toString(g.cid))",
        "Date": "g.eff_arrival_date",
        "День недели": _weekday_expr("g.eff_arrival_date"),
        "week_start": "toStartOfWeek(g.eff_arrival_date, 1)",
        "CampaignId": "g.cid",
        "CampaignName": "g.`CampaignName`",
        "AdGroupId": "g.agid",
        "AdGroupName": "g.`AdGroupName`",
        "Device": "g.`Device`",
        "domain": "g.domain",
        "RlAdjustmentId": "g.rl_adjustment_id",
        "RlAdjustmentId_total": "if(g.rl_adjustment_id > 0, 'Есть корректировка', 'Нет корректировки')",
        "campaign_code": "g.campaign_code",
        "tp": "g.tp",
        "cpc_cpa": "g.cpc_cpa",
        "site_quiz": "g.site_quiz",
        "adgroup_code": "g.adgroup_code",
        "account_login": "g.account_login",
        "manager_login": "g.manager_login",
        "марки авто": "''",
        "Название crm": "g.crm_name",
        "тип_заявки": "g.`тип_заявки`",
        "kol_vo_zayavok": "g.kol_vo_zayavok",
        "korr": "g.korr",
        "kval": "g.kval",
        "priezd": "g.priezd",
        "prodazhi": "g.prodazhi",
        "nekorr": "g.nekorr",
        "ne_otvechaet": "g.ne_otvechaet",
        "filtr": "g.filtr",
        "nedozvon": "g.nedozvon",
        "priedet": "g.priedet",
        "dohod_do_kredita": "g.dohod_do_kredita",
        "dobro": "g.dobro",
        "статус": "g.`статус`",
        "специалист": specialist,
        "тип_сайта": "g.`тип_сайта`",
        "шаблон": "g.`шаблон`",
        "салон": "g.`салон`",
        "город": "g.`город`",
        "регион": "g.`регион`",
        "direction": "g.direction",
        "fid": "g.fid",
        "проджект": "g.`проджект`",
        "id_салона": "g.`id_салона`",
        "менеджер": "g.`менеджер`",
        "источник": "g.`источник`",
        "направление": "g.`направление`",
        "номер кампании | название кампании":
            "if(g.cid = 0, '', concat(toString(g.cid), '|', ifNull(g.`CampaignName`, '')))",
        "номер группы | название группы":
            "if(g.agid = 0, '', concat(toString(g.agid), '|', ifNull(g.`AdGroupName`, '')))",
        "аккаунт|сайт": "concat(g.account_login, '|', ifNull(g.domain, ''))",
        "priezd_arrival_date": "toInt64(0)",
        "prodazhi_arrival_date": "toInt64(0)",
        "поставщик": "g.`поставщик`",
        "_source_table": "g._source_table",
        "key_pixel_score": "concat(toString(g.eff_arrival_date), '|', ifNull(g.domain, ''), "
                           "'|', g.`источник`, '|', toString(g.cid))",
    }


def _leads_branch_sql(date_from: str) -> str:
    """Лид-ветка визитной оси: одна строка на (дата визита × разрез витрины)."""
    metrics = _metric_expr("l.status", "l.reason", "l.source_type", "l.salon")
    ag_parts = _ag_parts_expr("")
    posev_flag = (
        f"(ifNull(l.utm_source, '') IN ({_POSEV_UTM_SOURCES}) "
        "OR ifNull(l.utm_medium, '') IN ('posev', 'paid_social'))"
    )
    seo_flag = "(ifNull(l.utm_source, '') = '' OR (l.utm_source = 'seo' AND ifNull(l.utm_medium, '') = 'organic'))"
    tp_expr = "ifNull(coalesce(cd.tp, ''), '')"
    return f"""
WITH
{_gs_account_cte()},
{_leads_deduped_cte()},
{_marcar_arrivals_cte()},
{_ad_dicts_cte()},
lead_visits AS
(
    SELECT
        {_eff_arrival_expr("l")} AS eff_arrival_date,
        l.domain AS domain,
        ifNull(l.campaign_id, 0) AS cid,
        ifNull(l.group_id, 0) AS agid,
        ifNull(l.correction_id, 0) AS rl_adjustment_id,
        cd.`CampaignName` AS `CampaignName`,
        ad.`AdGroupName` AS `AdGroupName`,
        {_device_from_utm("l.utm_content")} AS `Device`,
        cd.campaign_code AS campaign_code_raw,
        {tp_expr} AS tp_raw,
        ifNull(cd.cpc_cpa, '') AS cpc_cpa_raw,
        ifNull(cd.site_quiz, '') AS site_quiz_raw,
        ad.adgroup_code AS adgroup_code,
        lower(ifNull(coalesce(nullIf(cd.account_login, ''), gs.login_key), '')) AS account_login,
        ifNull(coalesce(nullIf(cd.manager_login, ''), gs.directologist), '') AS manager_login,
        ifNull(crm.crm_name, '') AS crm_name,
        if(ifNull(l.deal_type, '') = '', 'Заявка', l.deal_type) AS `тип_заявки`,
        gs.status AS `статус`,
        nullIf(trim(ifNull(gs.directologist, '')), '') AS specialist_raw,
        gs.site_type AS `тип_сайта`,
        gs.template AS `шаблон`,
        coalesce(nullIf(l.salon, ''), gs.salon) AS `салон`,
        gs.city AS `город`,
        gs.region AS `регион`,
        gs.direction AS direction,
        l.fid AS fid,
        gs.project_manager AS `проджект`,
        gs.client_id AS `id_салона`,
        gs.sales_manager AS `менеджер`,
        -- utm_campaign/utm_content — НЕ выходные колонки витрины, они входят в ключ
        -- группировки как в v5 (step13.py:654-655). Без них группа склеивает визитные
        -- и невизитные лиды одного дня/домена, и `kol_vo_zayavok` строки визитной оси
        -- раздувается на «попутные» заявки (замер лид-ветки: обращения 49 607 без
        -- этих полей в ключе против 35 742 с ними; приезды в обоих случаях 29 214).
        nullIf(trim(ifNull(l.utm_campaign, '')), '') AS utm_campaign,
        nullIf(trim(ifNull(l.utm_content, '')), '') AS utm_content,
        -- Метки — конвенция ЗАЯВОЧНОЙ оси v6: посевные utm → crop_targeting/Посевы,
        -- органика → seo/SEO, остальное → direct (или tp8/tp9/tp10 по коду кампании).
        multiIf(
            {posev_flag}, 'Посевы',
            {seo_flag}, 'SEO',
            'Контекст'
        ) AS `источник`,
        multiIf(
            {posev_flag}, 'Комплекс',
            {seo_flag}, 'Комплекс',
            {tp_expr} IN ('tp8', 'tp9', 'tp10'), 'Комплекс',
            'Контекст'
        ) AS `направление`,
        multiIf(
            {posev_flag}, 'Посевы',
            {seo_flag}, 'Victory',
            'Яндекс'
        ) AS `поставщик`,
        multiIf(
            {posev_flag}, 'crop_targeting',
            {seo_flag}, 'seo',
            {tp_expr} IN ('tp8', 'tp9', 'tp10'), {tp_expr},
            'direct'
        ) AS _source_table,
        {metrics}
    FROM leads_deduped l
    LEFT JOIN marcar_record_ids mi ON mi.id = l.id
    LEFT JOIN marcar_arrivals ma ON ma.lead_record_id = mi.source_record_id
    LEFT JOIN camp_dict cd ON cd.cid = l.campaign_id
    LEFT JOIN ag_dict ad ON ad.agid = l.group_id
    LEFT JOIN gs_domain gs ON gs.domain_key = lower(trim(ifNull(l.domain, '')))
    LEFT JOIN crm_by_domain crm ON crm.domain_key = lower(trim(ifNull(l.domain, '')))
    WHERE l.created_date IS NOT NULL
      AND l.created_date >= toDate('{date_from}')
      AND ifNull(l.status, '') != ''
      -- пиксельные лиды идут отдельной веткой (дробная атрибуция step11)
      AND ifNull(l.utm_source, '') NOT LIKE 'victory_%'
      AND {_auto_domains_filter("l.domain")}
),
lead_groups AS
(
    SELECT
        eff_arrival_date,
        domain,
        cid,
        agid,
        rl_adjustment_id,
        `CampaignName`,
        `AdGroupName`,
        `Device`,
        campaign_code_raw,
        tp_raw,
        cpc_cpa_raw,
        site_quiz_raw,
        adgroup_code,
        account_login,
        manager_login,
        crm_name,
        `тип_заявки`,
        `статус`,
        specialist_raw,
        `тип_сайта`,
        `шаблон`,
        `салон`,
        `город`,
        `регион`,
        direction,
        fid,
        `проджект`,
        `id_салона`,
        `менеджер`,
        `источник`,
        `направление`,
        `поставщик`,
        _source_table,
        utm_campaign,
        utm_content,
        sum(kol_vo_zayavok) AS kol_vo_zayavok,
        sum(korr) AS korr,
        sum(kval) AS kval,
        sum(priezd) AS priezd,
        sum(prodazhi) AS prodazhi,
        sum(nekorr) AS nekorr,
        sum(ne_otvechaet) AS ne_otvechaet,
        sum(filtr) AS filtr,
        sum(nedozvon) AS nedozvon,
        sum(priedet) AS priedet,
        sum(dohod_do_kredita) AS dohod_do_kredita,
        sum(dobro) AS dobro
    FROM lead_visits
    GROUP BY
        eff_arrival_date, domain, cid, agid, rl_adjustment_id, `CampaignName`, `AdGroupName`,
        `Device`, campaign_code_raw, tp_raw, cpc_cpa_raw, site_quiz_raw, adgroup_code,
        account_login, manager_login, crm_name, `тип_заявки`, `статус`, specialist_raw,
        `тип_сайта`, `шаблон`, `салон`, `город`, `регион`, direction, fid, `проджект`,
        `id_салона`, `менеджер`, `источник`, `направление`, `поставщик`, _source_table,
        utm_campaign, utm_content
)
SELECT
    *,
    -- симметрия с заявочной осью: SEO-строки не несут кода кампании Директа
    if(`источник` = 'SEO', 'seo', campaign_code_raw) AS campaign_code,
    if(`источник` = 'SEO', '', tp_raw) AS tp,
    if(`источник` = 'SEO', '', cpc_cpa_raw) AS cpc_cpa,
    if(`источник` = 'SEO', '', site_quiz_raw) AS site_quiz,
    {ag_parts}
FROM lead_groups
WHERE priezd > 0 OR prodazhi > 0
"""


# ══════════════════════════════════════════════════════════════════════════════
# Ветка 2 — звонки
# ══════════════════════════════════════════════════════════════════════════════
def _calls_branch_columns() -> dict[str, str]:
    specialist = specialist_correction_expr(
        "g.eff_arrival_date", "g.account_login", "g.specialist_raw", "g.domain"
    )
    return {
        "key3": "concat('visit_call|', toString(g.eff_arrival_date), '|', ifNull(g.domain, ''))",
        "Date": "g.eff_arrival_date",
        "День недели": _weekday_expr("g.eff_arrival_date"),
        "week_start": "toStartOfWeek(g.eff_arrival_date, 1)",
        "CampaignName": "'звонки'",
        "AdGroupName": "'звонки'",
        "AdNetworkType": "'Звонки'",
        "Device": "'звонки'",
        "domain": "g.domain",
        "tp": "'звонки'",
        "cpc_cpa": "'звонки'",
        "site_quiz": "'звонки'",
        "account_login": "g.account_login",
        "manager_login": "g.manager_login",
        "Название crm": "g.crm_name",
        "тип_заявки": "'Звонки'",
        "kol_vo_zayavok": "g.kol_vo_zayavok",
        "korr": "g.korr",
        "kval": "g.kval",
        "priezd": "g.priezd",
        "prodazhi": "g.prodazhi",
        "nekorr": "g.nekorr",
        "ne_otvechaet": "g.ne_otvechaet",
        "filtr": "g.filtr",
        "nedozvon": "g.nedozvon",
        "priedet": "g.priedet",
        "dohod_do_kredita": "g.dohod_do_kredita",
        "dobro": "g.dobro",
        "статус": "g.`статус`",
        "специалист": specialist,
        "тип_сайта": "g.`тип_сайта`",
        "шаблон": "g.`шаблон`",
        "салон": "g.`салон`",
        "город": "g.`город`",
        "регион": "g.`регион`",
        "direction": "g.direction",
        "проджект": "g.`проджект`",
        "id_салона": "g.`id_салона`",
        "менеджер": "g.`менеджер`",
        "источник": "'Звонки'",
        "направление": "'Контекст'",
        "аккаунт|сайт": "concat(g.account_login, '|', ifNull(g.domain, ''))",
        "priezd_arrival_date": "toInt64(0)",
        "prodazhi_arrival_date": "toInt64(0)",
        "поставщик": "'звонки'",
        "_source_table": "'calls'",
        "key_pixel_score": "concat(toString(g.eff_arrival_date), '|', ifNull(g.domain, ''), '|Звонки|0')",
    }


def _calls_branch_sql(date_from: str) -> str:
    """Звонки на визитной оси.

    `raw_calls` (там уже патченый статус Маркара из step1) не несёт `arrival_date` и
    `source_record_id` — оба поля дотягиваются из `raw_data.leads_all` по `id`
    (1:1 по построению step1).
    """
    metrics = _metric_expr("c.status", "c.reason", "c.source_type", "gs.salon")
    return f"""
WITH
{_gs_account_cte()},
{_marcar_arrivals_cte()},
call_visits AS
(
    SELECT
        {_eff_arrival_expr("c")} AS eff_arrival_date,
        c.domain AS domain,
        ifNull(lower(ifNull(gs.login_key, '')), '') AS account_login,
        ifNull(gs.directologist, '') AS manager_login,
        ifNull(crm.crm_name, '') AS crm_name,
        gs.status AS `статус`,
        nullIf(trim(ifNull(gs.directologist, '')), '') AS specialist_raw,
        gs.site_type AS `тип_сайта`,
        gs.template AS `шаблон`,
        gs.salon AS `салон`,
        gs.city AS `город`,
        gs.region AS `регион`,
        gs.direction AS direction,
        gs.project_manager AS `проджект`,
        gs.client_id AS `id_салона`,
        gs.sales_manager AS `менеджер`,
        {metrics}
    FROM
    (
        SELECT
            rc.id AS id,
            rc.domain AS domain,
            rc.status AS status,
            rc.reason AS reason,
            rc.source_type AS source_type,
            rc.created_date AS created_date,
            la.arrival_date AS arrival_date,
            ifNull(la.source_record_id, '') AS source_record_id
        FROM ad_analytics.raw_calls rc
        LEFT JOIN
        (
            SELECT id, arrival_date, source_record_id
            FROM raw_data.leads_all
            WHERE deal_type = 'Звонок'
        ) la ON la.id = rc.id
    ) c
    LEFT JOIN marcar_arrivals ma ON ma.lead_record_id = c.source_record_id
    LEFT JOIN gs_domain gs ON gs.domain_key = lower(trim(ifNull(c.domain, '')))
    LEFT JOIN crm_by_domain crm ON crm.domain_key = lower(trim(ifNull(c.domain, '')))
    WHERE c.created_date IS NOT NULL
      AND c.created_date >= toDate('{date_from}')
      AND ifNull(c.status, '') != ''
      AND {_auto_domains_filter("c.domain")}
)
SELECT
    eff_arrival_date,
    domain,
    account_login,
    manager_login,
    crm_name,
    `статус`,
    specialist_raw,
    `тип_сайта`,
    `шаблон`,
    `салон`,
    `город`,
    `регион`,
    direction,
    `проджект`,
    `id_салона`,
    `менеджер`,
    sum(kol_vo_zayavok) AS kol_vo_zayavok,
    sum(korr) AS korr,
    sum(kval) AS kval,
    sum(priezd) AS priezd,
    sum(prodazhi) AS prodazhi,
    sum(nekorr) AS nekorr,
    sum(ne_otvechaet) AS ne_otvechaet,
    sum(filtr) AS filtr,
    sum(nedozvon) AS nedozvon,
    sum(priedet) AS priedet,
    sum(dohod_do_kredita) AS dohod_do_kredita,
    sum(dobro) AS dobro
FROM call_visits
GROUP BY
    eff_arrival_date, domain, account_login, manager_login, crm_name, `статус`,
    specialist_raw, `тип_сайта`, `шаблон`, `салон`, `город`, `регион`, direction,
    `проджект`, `id_салона`, `менеджер`
HAVING priezd > 0 OR prodazhi > 0
"""


# ══════════════════════════════════════════════════════════════════════════════
# Ветка 3 — пиксель (date-shift дробной атрибуции)
# ══════════════════════════════════════════════════════════════════════════════
def _pixel_branch_columns() -> dict[str, str]:
    return {
        "key3": "concat('visit_pixel|', toString(g.`Date`), '|', ifNull(g.domain, ''), "
                "'|', toString(g.`CampaignId`))",
        "Date": "g.`Date`",
        "День недели": _weekday_expr("g.`Date`"),
        "week_start": "toStartOfWeek(g.`Date`, 1)",
        "CampaignId": "g.`CampaignId`",
        "CampaignName": "g.`CampaignName`",
        "AdGroupId": "g.`AdGroupId`",
        "AdGroupName": "g.`AdGroupName`",
        "AdNetworkType": "g.`AdNetworkType`",
        "Device": "g.`Device`",
        "domain": "g.domain",
        "RlAdjustmentId": "g.`RlAdjustmentId`",
        "RlAdjustmentId_total": "g.`RlAdjustmentId_total`",
        "campaign_code": "g.campaign_code",
        "tp": "g.tp",
        "cpc_cpa": "g.cpc_cpa",
        "site_quiz": "g.site_quiz",
        "adgroup_code": "g.adgroup_code",
        "account_login": "g.account_login",
        "manager_login": "g.manager_login",
        "ag_part1": "g.ag_part1",
        "ag_part2": "g.ag_part2",
        "ag_part3": "g.ag_part3",
        "ag_part4": "g.ag_part4",
        "ag_part5": "g.ag_part5",
        "ag_part6": "g.ag_part6",
        "ag_part7": "g.ag_part7",
        "марки авто": "g.`марки авто`",
        "Название crm": "g.`Название crm`",
        "тип_заявки": "g.`тип_заявки`",
        # kol_vo_zayavok/korr/kval не переносим: заявки пикселя живут на заявочной оси,
        # здесь только перераспределённые приезды/продажи (порт v5 step13.py:1675-1679).
        "priezd": "toDecimal64(round(g.priezd_shifted, 6), 6)",
        "prodazhi": "toDecimal64(round(g.prodazhi_shifted, 6), 6)",
        "статус": "g.`статус`",
        "специалист": "g.`специалист`",
        "тип_сайта": "g.`тип_сайта`",
        "шаблон": "g.`шаблон`",
        "салон": "g.`салон`",
        "город": "g.`город`",
        "регион": "g.`регион`",
        "direction": "g.direction",
        "fid": "g.fid",
        "проджект": "g.`проджект`",
        "id_салона": "g.`id_салона`",
        "менеджер": "g.`менеджер`",
        "источник": "g.`источник`",
        "направление": "'Пиксель_атрибуц'",
        "номер кампании | название кампании": "g.`номер кампании | название кампании`",
        "номер группы | название группы": "g.`номер группы | название группы`",
        "аккаунт|сайт": "g.`аккаунт|сайт`",
        "priezd_arrival_date": "toInt64(0)",
        "prodazhi_arrival_date": "toInt64(0)",
        "поставщик": "g.`поставщик`",
        "_source_table": "g._source_table",
        "key_pixel_score": "g.key_pixel_score",
    }


def _pixel_branch_sql(date_from: str) -> str:
    """Порт v5-ветки 4: дробная пиксельная атрибуция по РЕАЛЬНОЙ дате визита.

    Доля v_share считается внутри (домен, месяц заявки) по визит-лидам пиксель-пула
    (тот же JOIN на `local_pixel_config`, что в step5). Σ долей = 1 на группу, поэтому
    SUM(priezd)/SUM(prodazhi) пикселя ИНВАРИАНТНЫ — меняются только даты. Умножение
    идёт в Float64, округление — только у итоговой суммы (round(,6)), int-каста нет.
    """
    isv = _category_match_expr(
        ("visit", "sale", "credit", "approved"), "l.status", "l.reason", "l.source_type", "l.salon"
    )
    iss = _category_match_expr(("sale",), "l.status", "l.reason", "l.source_type", "l.salon")
    return f"""
WITH
pixel_pool AS
(
    SELECT
        lower(trim(ifNull(l.utm_source, ''))) AS dom,
        toStartOfMonth(l.created_date) AS mon,
        -- greatest(..., created_date): в CRM встречается arrival_date РАНЬШЕ даты
        -- заявки; без зажима такие лиды уносили пиксельные приезды в 2025 год
        -- и ломали инвариант `Date >= DATE_FROM` (замер: 31 строка / 2.3 приезда).
        -- assumeNotNull: `Date`/`week_start` витрины — не-Nullable, а без снятия
        -- Nullable UNION с proxy-веткой давал Nullable(Date) и INSERT падал бы
        -- на приведении типа (поймано DESCRIBE-сверкой типов веток с витриной).
        assumeNotNull(greatest(
            multiIf(
                l.source_type IN ('plex_excel', 'marcar_crm_excel'), l.created_date,
                ifNull(l.arrival_date, l.created_date)
            ),
            l.created_date
        )) AS eff,
        toUInt8(if({isv}, 1, 0)) AS isv,
        toUInt8(if({iss}, 1, 0)) AS iss
    FROM raw_data.leads_all l
    INNER JOIN ad_analytics.local_pixel_config pc
        ON (l.source_name = pc.pixel_name OR lower(ifNull(l.utm_source, '')) = lower(pc.pixel_name))
    WHERE l.is_copy_for_removal = 0
      AND l.created_date IS NOT NULL
      AND l.created_date >= toDate('{date_from}')
      AND ifNull(l.utm_source, '') != ''
      AND lower(trim(ifNull(l.utm_source, ''))) IN (
          SELECT lower(trim(ifNull(domain, '')))
          FROM raw_data.gsheet_sites
          WHERE ifNull(domain, '') != ''
      )
),
pixel_totals AS
(
    SELECT dom, mon, sum(isv) AS visit_leads
    FROM pixel_pool
    GROUP BY dom, mon
),
pixel_eff_dist AS
(
    -- ОДНА доля на обе метрики (отличие от v5, где приезды идут по v_share, а продажи
    -- по отдельной s_share). Причина — жёсткий инвариант `priezd >= prodazhi`:
    -- у разных нормировок s_share(d) может превысить v_share(d), и строка получает
    -- продаж больше, чем приездов. Замер на живых данных с раздельными долями:
    -- 571 строка / 16.12 «лишних» продаж; у v5 и у заявочной оси v6 таких строк 0.
    -- Общая доля по визит-лидам сохраняет обе суммы (Σ доля = 1) и держит вложенность
    -- по построению: prodazhi_r <= priezd_r на исходной строке ⇒ и после умножения
    -- на один и тот же вес. Продажа всегда происходит в день визита, поэтому
    -- распределение визитов — корректный носитель и для продаж.
    SELECT
        dom,
        mon,
        eff AS new_date,
        sum(isv) / nullIf(sum(sum(isv)) OVER (PARTITION BY dom, mon), 0) AS v_share
    FROM pixel_pool
    GROUP BY dom, mon, eff
),
pixel_shifted AS
(
    -- 3A. MATCHED: у (домен, месяц) есть визит-лиды пиксель-пула → раскладываем
    --     дробную атрибуцию по реальным датам визита с весами v_share.
    SELECT
        pxv.new_date AS `Date`,
        f.domain AS domain,
        f.`салон` AS `салон`,
        f.`CampaignId` AS `CampaignId`,
        f.`CampaignName` AS `CampaignName`,
        f.`AdGroupId` AS `AdGroupId`,
        f.`AdGroupName` AS `AdGroupName`,
        f.`AdNetworkType` AS `AdNetworkType`,
        f.`Device` AS `Device`,
        f.`RlAdjustmentId` AS `RlAdjustmentId`,
        f.`RlAdjustmentId_total` AS `RlAdjustmentId_total`,
        f.campaign_code AS campaign_code,
        f.tp AS tp,
        f.cpc_cpa AS cpc_cpa,
        f.site_quiz AS site_quiz,
        f.adgroup_code AS adgroup_code,
        f.account_login AS account_login,
        f.manager_login AS manager_login,
        f.ag_part1 AS ag_part1,
        f.ag_part2 AS ag_part2,
        f.ag_part3 AS ag_part3,
        f.ag_part4 AS ag_part4,
        f.ag_part5 AS ag_part5,
        f.ag_part6 AS ag_part6,
        f.ag_part7 AS ag_part7,
        f.`марки авто` AS `марки авто`,
        f.`Название crm` AS `Название crm`,
        f.`тип_заявки` AS `тип_заявки`,
        f.`статус` AS `статус`,
        f.`специалист` AS `специалист`,
        f.`тип_сайта` AS `тип_сайта`,
        f.`шаблон` AS `шаблон`,
        f.`город` AS `город`,
        f.`регион` AS `регион`,
        f.direction AS direction,
        f.fid AS fid,
        f.`проджект` AS `проджект`,
        f.`id_салона` AS `id_салона`,
        f.`менеджер` AS `менеджер`,
        f.`источник` AS `источник`,
        f.`номер кампании | название кампании` AS `номер кампании | название кампании`,
        f.`номер группы | название группы` AS `номер группы | название группы`,
        f.`аккаунт|сайт` AS `аккаунт|сайт`,
        f.`поставщик` AS `поставщик`,
        f._source_table AS _source_table,
        f.key_pixel_score AS key_pixel_score,
        toFloat64(f.priezd) * ifNull(pxv.v_share, 0) AS priezd_part,
        toFloat64(f.prodazhi) * ifNull(pxv.v_share, 0) AS prodazhi_part
    FROM ad_analytics.big_analytics_full f
    INNER JOIN pixel_eff_dist pxv
        ON pxv.dom = lower(trim(ifNull(f.domain, '')))
       AND pxv.mon = toStartOfMonth(f.`Date`)
    WHERE f.`направление` = 'Пиксель_атрибуц'
      AND f.`Date` >= toDate('{date_from}')
      AND ifNull(pxv.v_share, 0) > 0
      AND (f.priezd > 0 OR f.prodazhi > 0)

    UNION ALL

    -- 3B. ORPHAN (полнота): у (домен, месяц) визит-лидов в пуле НЕТ, доли не
    --     определены. Строка остаётся на дате ЗАЯВКИ (proxy) — иначе её приезды и
    --     продажи просто исчезли бы с визитной оси. Условия 3A и 3B взаимно
    --     исключающие (visit_leads > 0 против = 0), поэтому Σ priezd / Σ prodazhi
    --     по обеим суб-веткам ровно равна исходной сумме витрины.
    SELECT
        f.`Date` AS `Date`,
        f.domain AS domain,
        f.`салон` AS `салон`,
        f.`CampaignId` AS `CampaignId`,
        f.`CampaignName` AS `CampaignName`,
        f.`AdGroupId` AS `AdGroupId`,
        f.`AdGroupName` AS `AdGroupName`,
        f.`AdNetworkType` AS `AdNetworkType`,
        f.`Device` AS `Device`,
        f.`RlAdjustmentId` AS `RlAdjustmentId`,
        f.`RlAdjustmentId_total` AS `RlAdjustmentId_total`,
        f.campaign_code AS campaign_code,
        f.tp AS tp,
        f.cpc_cpa AS cpc_cpa,
        f.site_quiz AS site_quiz,
        f.adgroup_code AS adgroup_code,
        f.account_login AS account_login,
        f.manager_login AS manager_login,
        f.ag_part1 AS ag_part1,
        f.ag_part2 AS ag_part2,
        f.ag_part3 AS ag_part3,
        f.ag_part4 AS ag_part4,
        f.ag_part5 AS ag_part5,
        f.ag_part6 AS ag_part6,
        f.ag_part7 AS ag_part7,
        f.`марки авто` AS `марки авто`,
        f.`Название crm` AS `Название crm`,
        f.`тип_заявки` AS `тип_заявки`,
        f.`статус` AS `статус`,
        f.`специалист` AS `специалист`,
        f.`тип_сайта` AS `тип_сайта`,
        f.`шаблон` AS `шаблон`,
        f.`город` AS `город`,
        f.`регион` AS `регион`,
        f.direction AS direction,
        f.fid AS fid,
        f.`проджект` AS `проджект`,
        f.`id_салона` AS `id_салона`,
        f.`менеджер` AS `менеджер`,
        f.`источник` AS `источник`,
        f.`номер кампании | название кампании` AS `номер кампании | название кампании`,
        f.`номер группы | название группы` AS `номер группы | название группы`,
        f.`аккаунт|сайт` AS `аккаунт|сайт`,
        f.`поставщик` AS `поставщик`,
        f._source_table AS _source_table,
        f.key_pixel_score AS key_pixel_score,
        toFloat64(f.priezd) AS priezd_part,
        toFloat64(f.prodazhi) AS prodazhi_part
    FROM ad_analytics.big_analytics_full f
    LEFT JOIN pixel_totals pt
        ON pt.dom = lower(trim(ifNull(f.domain, '')))
       AND pt.mon = toStartOfMonth(f.`Date`)
    WHERE f.`направление` = 'Пиксель_атрибуц'
      AND f.`Date` >= toDate('{date_from}')
      AND ifNull(pt.visit_leads, 0) = 0
      AND (f.priezd > 0 OR f.prodazhi > 0)
)
SELECT
    `Date`, domain, `салон`, `CampaignId`, `CampaignName`,
    any(`AdGroupId`) AS `AdGroupId`,
    any(`AdGroupName`) AS `AdGroupName`,
    any(`AdNetworkType`) AS `AdNetworkType`,
    any(`Device`) AS `Device`,
    any(`RlAdjustmentId`) AS `RlAdjustmentId`,
    any(`RlAdjustmentId_total`) AS `RlAdjustmentId_total`,
    any(campaign_code) AS campaign_code,
    any(tp) AS tp,
    any(cpc_cpa) AS cpc_cpa,
    any(site_quiz) AS site_quiz,
    any(adgroup_code) AS adgroup_code,
    any(account_login) AS account_login,
    any(manager_login) AS manager_login,
    any(ag_part1) AS ag_part1,
    any(ag_part2) AS ag_part2,
    any(ag_part3) AS ag_part3,
    any(ag_part4) AS ag_part4,
    any(ag_part5) AS ag_part5,
    any(ag_part6) AS ag_part6,
    any(ag_part7) AS ag_part7,
    any(`марки авто`) AS `марки авто`,
    any(`Название crm`) AS `Название crm`,
    any(`тип_заявки`) AS `тип_заявки`,
    any(`статус`) AS `статус`,
    any(`специалист`) AS `специалист`,
    any(`тип_сайта`) AS `тип_сайта`,
    any(`шаблон`) AS `шаблон`,
    any(`город`) AS `город`,
    any(`регион`) AS `регион`,
    any(direction) AS direction,
    any(fid) AS fid,
    any(`проджект`) AS `проджект`,
    any(`id_салона`) AS `id_салона`,
    any(`менеджер`) AS `менеджер`,
    any(`источник`) AS `источник`,
    any(`номер кампании | название кампании`) AS `номер кампании | название кампании`,
    any(`номер группы | название группы`) AS `номер группы | название группы`,
    any(`аккаунт|сайт`) AS `аккаунт|сайт`,
    any(`поставщик`) AS `поставщик`,
    any(_source_table) AS _source_table,
    any(key_pixel_score) AS key_pixel_score,
    sum(priezd_part) AS priezd_shifted,
    sum(prodazhi_part) AS prodazhi_shifted
FROM pixel_shifted
GROUP BY `Date`, domain, `салон`, `CampaignId`, `CampaignName`
-- round(,6) — та же точность, с которой доли лягут в Decimal(38,6). Без него группа
-- с суммой ~1e-9 давала бы строку с priezd=0 и prodazhi=0 (пустой мусор на оси).
HAVING round(priezd_shifted, 6) > 0 OR round(prodazhi_shifted, 6) > 0
"""


# ══════════════════════════════════════════════════════════════════════════════
# Сборка
# ══════════════════════════════════════════════════════════════════════════════
def _branch_insert_sql(shadow: str, columns: dict[str, str], inner_sql: str, target_cols: set[str]) -> str:
    """`INSERT INTO shadow (<колонки ветки>) SELECT <выражения> FROM (<ветка>) g`.

    Перечисляются ТОЛЬКО колонки, которые ветка реально считает. Остальные
    ClickHouse заполняет дефолтом типа: Decimal→0 (так обнуляются
    Impressions/Clicks/total_cost), String→'', Nullable→NULL.
    """
    used = [(name, expr) for name, expr in columns.items() if name in target_cols]
    missing = [name for name in columns if name not in target_cols]
    if missing:
        logger.warning("  колонки ветки отсутствуют в big_analytics_full и пропущены: %s", missing)
    col_list = ", ".join(q(name) for name, _ in used)
    expr_list = ",\n    ".join(f"{expr} AS {q(name)}" for name, expr in used)
    return f"INSERT INTO {shadow} ({col_list})\nSELECT\n    {expr_list}\nFROM (\n{inner_sql}\n) AS g"


def _create_empty(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('{DATE_FROM}')))
        ORDER BY (ifNull(`Date`, toDate('{DATE_FROM}')), ifNull(domain, ''), ifNull(_source_table, ''))
        AS SELECT * FROM ad_analytics.big_analytics_full WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def build_branches(date_from: str = DATE_FROM) -> list[tuple[str, dict[str, str], str]]:
    """(имя ветки, колонки, SQL) — вынесено отдельно, чтобы ветку можно было
    прогнать read-only (SELECT) без записи в БД."""
    return [
        ("leads", _leads_branch_columns(), _leads_branch_sql(date_from)),
        ("calls", _calls_branch_columns(), _calls_branch_sql(date_from)),
        ("pixel", _pixel_branch_columns(), _pixel_branch_sql(date_from)),
    ]


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 13 v6_ch: big_analytics_full_arrival (реальная визитная ось)")
    client = get_client()
    t0 = time.perf_counter()
    _create_empty(client, SHADOW)
    target_cols = set(column_names(client, "ad_analytics", "big_analytics_full"))
    details_parts: list[str] = []
    for name, columns, inner_sql in build_branches():
        t_branch = time.perf_counter()
        client.command(
            _branch_insert_sql(SHADOW, columns, inner_sql, target_cols),
            settings=STEP13_QUERY_SETTINGS,
        )
        rows = count_rows(client, SHADOW)
        logger.info("  ветка %s залита за %.1f сек (накопительно строк: %d)",
                    name, time.perf_counter() - t_branch, rows)
        details_parts.append(f"{name}")
    swap_shadow(client, TARGET, SHADOW)
    rows = count_rows(client, TARGET)
    logger.info("Шаг 13 v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"big_analytics_full_arrival={rows:,} ({'+'.join(details_parts)})"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
