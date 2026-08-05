"""Step 3 for v6_ch: build source marts in ClickHouse.

The v5 module was a large PostgreSQL CTAS/corrections pipeline. This v6 module
materializes the same table names in ClickHouse from `ad_analytics.raw_*` and
`raw_data` reference tables. It intentionally consumes the current ClickHouse
raw_data snapshot; it does not import CRM data.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, q, replace_view, swap_shadow

logger = logging.getLogger("pipeline.step3")


SOURCE_STORE = "big_analytics_sources"
LEADS_DEDUPED_STAGE = "_step3_leads_deduped"
DIRECT_SOURCE_TYPES = ("direct", "tp8", "tp9", "tp10")

# source_type (raw_leads/raw_calls) -> ключ `crm` в raw_data.crm_status_mapping.
# Сверено с живой БД 2026-08-05: raw_leads / raw_data.leads_all / raw_calls дают ровно эти
# source_type, crm_status_mapping — ровно эти 8 значений crm. Маркер: CRM_MAP_RIVENDELL_2026-08-05.
CRM_BY_SOURCE_TYPE = {
    "crmf_excel": "crmf",
    "genzes_excel": "genzes",
    "marcar_crm_excel": "marcar",
    "mauto_excel": "mauto",
    "mega_crm_excel": "mega",
    "plex_excel": "plex",
    "redauto_excel": "redauto",
    "rivendell_excel": "rivendell",
}
# Фолбэк для source_type, которого нет в словаре: снять суффикс `_excel` / `_crm_excel`.
_CRM_FALLBACK_RE = "(_crm)?_excel$"
STEP3_QUERY_SETTINGS = {
    **SAFE_QUERY_SETTINGS,
    "max_execution_time": 180,
    "max_memory_usage": 1_500_000_000,
}


def _is_transient_clickhouse_error(exc: Exception) -> bool:
    message = str(exc)
    return any(
        marker in message
        for marker in (
            "EOF occurred in violation of protocol",
            "Connection reset by peer",
            "Max retries exceeded",
            "SESSION_IS_LOCKED",
            "Read timed out",
            "RemoteDisconnected",
        )
    )


def _command_with_retry(client, sql: str, *, label: str, settings=None, attempts: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            client.command(sql, settings=settings)
            return client
        except Exception as exc:
            if attempt == attempts or not _is_transient_clickhouse_error(exc):
                raise
            logger.warning(
                "    %s failed (%d/%d): %s; reconnecting",
                label,
                attempt,
                attempts,
                exc,
            )
            time.sleep(attempt * 2)
            client = get_client()
    return client


def _weekday_expr(date_expr: str) -> str:
    return (
        f"multiIf(toDayOfWeek({date_expr}) = 1, '1_Понедельник', "
        f"toDayOfWeek({date_expr}) = 2, '2_Вторник', "
        f"toDayOfWeek({date_expr}) = 3, '3_Среда', "
        f"toDayOfWeek({date_expr}) = 4, '4_Четверг', "
        f"toDayOfWeek({date_expr}) = 5, '5_Пятница', "
        f"toDayOfWeek({date_expr}) = 6, '6_Суббота', '7_Воскресенье')"
    )


def _month_plan_expr(date_expr: str, value_expr: str) -> str:
    return (
        f"if(toStartOfMonth({date_expr}) = toStartOfMonth(today()), "
        f"toInt32OrNull(replaceAll(replaceAll(replaceAll(ifNull({value_expr}, ''), '\u00a0', ''), ' ', ''), ',', '.')), NULL)"
    )


def _key_pixel_expr(date_expr: str, domain_expr: str, source_expr: str, campaign_expr: str) -> str:
    return (
        f"concat(ifNull(toString({date_expr}), ''), '|', ifNull({domain_expr}, ''), '|', "
        f"ifNull({source_expr}, ''), '|', ifNull(toString({campaign_expr}), ''))"
    )


def _crm_key(source_type: str) -> str:
    """Python-двойник `_crm_expr` — тот же ключ `crm` для одного source_type."""
    if source_type in CRM_BY_SOURCE_TYPE:
        return CRM_BY_SOURCE_TYPE[source_type]
    return re.sub(_CRM_FALLBACK_RE, "", source_type)


def _crm_expr(source_type_expr: str) -> str:
    """SQL-выражение source_type -> ключ `crm` в raw_data.crm_status_mapping.

    Фолбэк — снятие суффикса, НЕ self-map: `else source_type` возвращал ключ вида
    `rivendell_excel`, которого в crm_status_mapping нет, и вся воронка такой CRM
    молча обнулялась (в CH-маппинге нет general-ветки, в отличие от v5).
    """
    branches = "".join(
        f"{source_type_expr} = '{source_type}', '{crm}', "
        for source_type, crm in sorted(CRM_BY_SOURCE_TYPE.items())
    )
    fallback = f"replaceRegexpOne({source_type_expr}, '{_CRM_FALLBACK_RE}', '')"
    return f"multiIf({branches}{fallback})"


def check_crm_mapping_coverage(client) -> list[str]:
    """Громко сообщить про source_type, чей ключ отсутствует в crm_status_mapping.

    Не роняет шаг: неизвестная CRM не должна останавливать pipeline. Но и молчать
    нельзя — ключ без строк в raw_data.crm_status_mapping обнуляет ВСЮ воронку
    (korr/kval/priezd/prodazhi) этих строк.
    """
    problems: list[str] = []
    try:
        rows = client.query(
            """
            SELECT source_type, sum(n) AS n
            FROM (
                SELECT source_type, count() AS n FROM ad_analytics.raw_leads GROUP BY source_type
                UNION ALL
                SELECT source_type, count() AS n FROM ad_analytics.raw_calls GROUP BY source_type
            )
            GROUP BY source_type
            ORDER BY n DESC
            """
        ).result_rows
        mapped = {
            str(row[0])
            for row in client.query("SELECT DISTINCT crm FROM raw_data.crm_status_mapping").result_rows
        }
    except Exception as exc:  # проверка не обязана ронять шаг
        logger.warning("  CRM mapping coverage check пропущен: %s", exc)
        return problems

    for source_type, n in rows:
        source_type = str(source_type or "")
        crm = _crm_key(source_type)
        if source_type not in CRM_BY_SOURCE_TYPE:
            logger.warning(
                "  CRM mapping: неизвестный source_type=%r (%s строк) — ключ выведен фолбэком как %r; "
                "добавьте его в CRM_BY_SOURCE_TYPE (step3.py)",
                source_type,
                f"{int(n):,}",
                crm,
            )
        if crm not in mapped:
            problems.append(f"{source_type}->{crm}:{int(n)}")
            logger.error(
                "  CRM mapping: source_type=%r -> crm=%r ОТСУТСТВУЕТ в raw_data.crm_status_mapping "
                "— воронка (korr/kval/priezd/prodazhi) обнулится для %s строк",
                source_type,
                crm,
                f"{int(n):,}",
            )
    if not problems:
        logger.info("  CRM mapping coverage OK: %d source_type покрыты crm_status_mapping", len(rows))
    return problems


def _category_match_expr(
    categories: tuple[str, ...],
    status_expr: str,
    reason_expr: str,
    source_type_expr: str,
    salon_expr: str,
) -> str:
    cats_sql = ", ".join(f"'{category}'" for category in categories)
    crm = _crm_expr(source_type_expr)
    status = f"ifNull({status_expr}, '')"
    reason = f"lower(ifNull({reason_expr}, ''))"
    salon = f"lower(trim(ifNull({salon_expr}, '')))"
    return f"""
    (
        ({crm}, {status}) IN (
            SELECT crm, status FROM raw_data.crm_status_mapping
            WHERE category IN ({cats_sql}) AND reason = '' AND salon = ''
        )
        OR ({crm}, {status}, {reason}) IN (
            SELECT crm, status, lower(reason) FROM raw_data.crm_status_mapping
            WHERE category IN ({cats_sql}) AND reason != '' AND salon = ''
        )
        OR ({crm}, {salon}, {status}) IN (
            SELECT crm, lower(salon), status FROM raw_data.crm_status_mapping
            WHERE category IN ({cats_sql}) AND reason = '' AND salon != ''
        )
        OR ({crm}, {salon}, {status}, {reason}) IN (
            SELECT crm, lower(salon), status, lower(reason) FROM raw_data.crm_status_mapping
            WHERE category IN ({cats_sql}) AND reason != '' AND salon != ''
        )
    )
    """


def _metric_expr(status_expr: str, reason_expr: str, source_type_expr: str, salon_expr: str) -> str:
    status = f"ifNull({status_expr}, '')"
    reason = f"lower(ifNull({reason_expr}, ''))"
    # REASON_CRM_SCOPE_2026-08-05: reason-метрики (dohod_do_kredita/dobro) матчатся В РАЗРЕЗЕ CRM,
    # как и status-сторона в _category_match_expr. Глобальный матч по одному lower(reason)
    # тянул причину чужой CRM на все CRM сразу и раздувал reason-воронку.
    crm = _crm_expr(source_type_expr)
    correct = _category_match_expr(
        ("correct", "qualified", "visit", "sale", "credit", "approved"),
        status_expr,
        reason_expr,
        source_type_expr,
        salon_expr,
    )
    qualified = _category_match_expr(
        ("qualified", "visit", "sale", "credit", "approved"),
        status_expr,
        reason_expr,
        source_type_expr,
        salon_expr,
    )
    visit = _category_match_expr(
        ("visit", "sale", "credit", "approved"),
        status_expr,
        reason_expr,
        source_type_expr,
        salon_expr,
    )
    sale = _category_match_expr(("sale",), status_expr, reason_expr, source_type_expr, salon_expr)
    incorrect = _category_match_expr(("incorrect",), status_expr, reason_expr, source_type_expr, salon_expr)
    return f"""
    toDecimal64(if({status} != '', 1, 0), 6) AS kol_vo_zayavok,
    toDecimal64(if({correct}, 1, 0), 6) AS korr,
    toDecimal64(if({qualified}, 1, 0), 6) AS kval,
    toDecimal64(if({visit}, 1, 0), 6) AS priezd,
    toDecimal64(if({sale}, 1, 0), 6) AS prodazhi,
    toDecimal64(if({incorrect}, 1, 0), 6) AS nekorr,
    toDecimal64(if({status} IN ('Не отвечает', 'Новая: Не отвечает'), 1, 0), 6) AS ne_otvechaet,
    toDecimal64(if({status} = 'Фильтр', 1, 0), 6) AS filtr,
    toDecimal64(if({status} = 'Недозвон', 1, 0), 6) AS nedozvon,
    toDecimal64(if({status} = 'Приедет', 1, 0), 6) AS priedet,
    toInt64(if(({crm}, {reason}) IN (
        SELECT crm, lower(reason) FROM raw_data.crm_status_mapping
        WHERE category IN ('credit', 'approved') AND ifNull(reason, '') != ''
    ), 1, 0)) AS dohod_do_kredita,
    toInt64(if(({crm}, {reason}) IN (
        SELECT crm, lower(reason) FROM raw_data.crm_status_mapping
        WHERE category = 'approved' AND ifNull(reason, '') != ''
    ), 1, 0)) AS dobro
"""


def _gs_account_cte() -> str:
    return """
gs_account AS
(
    SELECT
        login_key_norm AS login_key,
        anyLast(domain) AS domain,
        anyLast(status) AS status,
        anyLast(directologist) AS directologist,
        anyLast(site_type) AS site_type,
        anyLast(template) AS template,
        anyLast(salon) AS salon,
        anyLast(city) AS city,
        anyLast(region) AS region,
        anyLast(direction) AS direction,
        anyLast(project_manager) AS project_manager,
        anyLast(client_id) AS client_id,
        anyLast(sales_manager) AS sales_manager
    FROM
    (
        SELECT *, lower(ifNull(login_key, '')) AS login_key_norm
        FROM raw_data.gsheet_sites
        WHERE ifNull(login_key, '') != ''
    )
    GROUP BY login_key_norm
),
gs_domain AS
(
    SELECT
        domain_key_norm AS domain_key,
        anyLast(domain) AS domain,
        anyLast(status) AS status,
        anyLast(directologist) AS directologist,
        anyLast(site_type) AS site_type,
        anyLast(template) AS template,
        anyLast(salon) AS salon,
        anyLast(city) AS city,
        anyLast(region) AS region,
        anyLast(direction) AS direction,
        anyLast(project_manager) AS project_manager,
        anyLast(client_id) AS client_id,
        anyLast(sales_manager) AS sales_manager,
        anyLast(login_key) AS login_key
    FROM
    (
        SELECT *, lower(trim(ifNull(domain, ''))) AS domain_key_norm
        FROM raw_data.gsheet_sites
        WHERE ifNull(domain, '') != ''
    )
    GROUP BY domain_key_norm
),
crm_by_domain AS
(
    SELECT
        domain_key_norm AS domain_key,
        multiIf(
            has(groupArray(source_type), 'marcar_crm_excel'), 'Маркар',
            has(groupArray(source_type), 'mega_crm_excel'), 'Мега',
            has(groupArray(source_type), 'crmf_excel'), 'Фаиг',
            has(groupArray(source_type), 'plex_excel'), 'Плекс',
            has(groupArray(source_type), 'redauto_excel'), 'Ред Авто',
            has(groupArray(source_type), 'genzes_excel'), 'Генезис',
            has(groupArray(source_type), 'mauto_excel'), 'МаАвто',
            anyLast(source_type)
        ) AS crm_name
    FROM
    (
        SELECT lower(trim(ifNull(domain, ''))) AS domain_key_norm, source_type FROM ad_analytics.raw_leads
        UNION ALL
        SELECT lower(trim(ifNull(domain, ''))) AS domain_key_norm, source_type FROM ad_analytics.raw_calls
    )
    WHERE domain_key_norm != ''
    GROUP BY domain_key_norm
)
"""


def _leads_deduped_cte() -> str:
    return """
perform_domains AS
(
    SELECT lowerUTF8(trim(ifNull(domain, ''))) AS domain
    FROM raw_data.gsheet_sites
    WHERE client_id = 'avto_0415'
      AND ifNull(domain, '') != ''
),
priezd_statuses AS
(
    SELECT DISTINCT status
    FROM raw_data.crm_status_mapping
    WHERE category IN ('visit', 'sale', 'credit', 'approved')
      AND ifNull(status, '') != ''
),
all_leads AS
(
    SELECT *
    FROM ad_analytics.raw_leads
    WHERE lowerUTF8(trim(ifNull(domain, ''))) NOT IN (SELECT domain FROM perform_domains)
    UNION ALL
    SELECT *
    FROM ad_analytics.raw_perform_leads
),
ranked_leads AS
(
    SELECT
        *,
        max(if(status IN (SELECT status FROM priezd_statuses), 1, 0))
            OVER (PARTITION BY phone, yclid) AS _hp,
        row_number() OVER (
            PARTITION BY phone, yclid
            ORDER BY if(status IN (SELECT status FROM priezd_statuses), 0, 1), created_date
        ) AS _rn
    FROM all_leads
    WHERE ifNull(phone, '') != ''
      AND ifNull(yclid, '') != ''
),
leads_deduped AS
(
    SELECT * EXCEPT(_hp, _rn)
    FROM all_leads
    WHERE ifNull(phone, '') = ''
       OR ifNull(yclid, '') = ''
    UNION ALL
    SELECT * EXCEPT(_hp, _rn)
    FROM ranked_leads
    WHERE (_hp = 1 AND status IN (SELECT status FROM priezd_statuses))
       OR (_hp = 0 AND _rn = 1)
)
"""


def _rebuild_leads_deduped_stage(client):
    target = f"ad_analytics.{LEADS_DEDUPED_STAGE}"
    client = _command_with_retry(client, f"DROP TABLE IF EXISTS {target} SYNC", label=f"{LEADS_DEDUPED_STAGE} drop")
    client = _command_with_retry(
        client,
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(created_date, toDate('2026-01-01')))
        ORDER BY (ifNull(created_date, toDate('2026-01-01')), ifNull(key3, ''), id)
        AS
        WITH
        {_leads_deduped_cte()}
        SELECT *
        FROM leads_deduped
        WHERE ifNull(created_date, toDate('1970-01-01')) >= toDate('2026-01-01')
        """,
        label=f"{LEADS_DEDUPED_STAGE} create",
        settings=STEP3_QUERY_SETTINGS,
    )
    return client, count_rows(client, target)


def _ag_parts_expr(prefix: str = "") -> str:
    ag = f"{prefix}adgroup_code"
    return f"""
    splitByChar('_', ifNull({ag}, ''))[1] AS ag_part1,
    splitByChar('_', ifNull({ag}, ''))[2] AS ag_part2,
    splitByChar('_', ifNull({ag}, ''))[3] AS ag_part3,
    splitByChar('_', ifNull({ag}, ''))[4] AS ag_part4,
    splitByChar('_', ifNull({ag}, ''))[5] AS ag_part5,
    splitByChar('_', ifNull({ag}, ''))[6] AS ag_part6,
    splitByChar('_', ifNull({ag}, ''))[7] AS ag_part7
"""


def _gs_pick_expr(field: str) -> str:
    return (
        f"if(la.domain_key != '' AND la.domain_key != lower(trim(ifNull(gs.domain, ''))), "
        f"coalesce(gs_dir.{field}, gs.{field}), coalesce(gs.{field}, gs_dir.{field}))"
    )


def _direct_specialist_expr() -> str:
    return _domain_specialist_expr("gs")


def _domain_specialist_expr(alias: str) -> str:
    return (
        f"if({alias}.match_priority IN (1, 2), "
        f"nullIf(trim(ifNull({alias}.directologist, '')), ''), "
        "CAST(NULL, 'Nullable(String)'))"
    )


# DIRECT_LEAD_BRANCHES_2026-08-05
# key3 лида = lower(created_date|campaign_id|group_id|device|correction_id) (step1.py:225-238).
# Лид без campaign_id/группы/девайса/корректировки даёт хвост '|0|0|0|0' — в статистике Директа
# такого ключа не бывает (замер: 0 строк raw_yandex с этим хвостом), значит ветка direct_zero
# пересекаться с основной веткой direct не может по построению.
_ZERO_KEY3_PATTERN = "%|0|0|0|0"


def _direct_lead_universe_filter(prefix: str = "") -> str:
    """Единый предикат «лид принадлежит Директу» — не SEO, не пиксель, не соц.посевы.

    Используется И основной веткой (`lead_scored` в `_build_direct_sql`), И ветками
    direct_unmatched / direct_zero. Одно определение на три ветки — иначе разъезд
    фильтров даст либо потерю лидов, либо двойной учёт.
    """
    p = prefix
    return f"""
    ifNull({p}key3, '') != ''
      AND NOT (ifNull({p}utm_source, '') = '' OR ({p}utm_source = 'seo' AND {p}utm_medium = 'organic'))
      AND ifNull({p}utm_source, '') NOT LIKE 'victory_%'
      AND ifNull({p}utm_source, '') NOT IN ('telegram','stories_tg','vk_storis','telegram_storis','max','vk','vk_groups','vkads')
"""


def _lead_date_filter(lo: str, hi: str) -> str:
    return f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')"


def _build_direct_sql(target_table: str, raw_date_filter: str = "") -> str:
    return f"""
CREATE TABLE ad_analytics.{target_table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(`Date`)
ORDER BY (`Date`, ifNull(`CampaignId`, 0), ifNull(key3, ''))
AS
WITH
{_gs_account_cte()},
yd AS
(
    SELECT
        key3,
        max(`Date`) AS `Date`,
        anyLast(`CampaignId`) AS `CampaignId`,
        anyLast(`CampaignName`) AS `CampaignName`,
        anyLast(`AdGroupId`) AS `AdGroupId`,
        anyLast(`AdGroupName`) AS `AdGroupName`,
        anyLast(`AdNetworkType`) AS `AdNetworkType`,
        anyLast(`Device`) AS `Device`,
        sum(`Impressions`) AS `Impressions`,
        sum(`Clicks`) AS `Clicks`,
        sum(total_cost) AS total_cost,
        anyLast(`RlAdjustmentId`) AS `RlAdjustmentId`,
        anyLast(week_start) AS week_start,
        lower(anyLast(account_login)) AS account_login,
        anyLast(manager_login) AS manager_login,
        anyLast(campaign_code) AS campaign_code,
        anyLast(tp) AS tp,
        anyLast(cpc_cpa) AS cpc_cpa,
        anyLast(site_quiz) AS site_quiz,
        anyLast(adgroup_code) AS adgroup_code
    FROM ad_analytics.raw_yandex AS ry
    WHERE 1 = 1
      {raw_date_filter}
    GROUP BY key3
),
gs_best AS
(
    SELECT *
    FROM
    (
        SELECT
            ud.login_key AS match_login_key,
            ud.date_val AS match_date,
            gs.domain,
            gs.status,
            gs.directologist,
            gs.site_type,
            gs.template,
            gs.salon,
            gs.city,
            gs.region,
            gs.direction,
            gs.project_manager,
            gs.client_id,
            gs.sales_manager,
            gs.login_key,
            multiIf(
                ifNull(gs.login_key, '') = '', 99,
                ifNull(trim(gs.launch_date), '') = '' AND ifNull(trim(gs.block_date), '') = '', 2,
                (ifNull(trim(gs.launch_date), '') = '' OR ud.date_val >= toDate(parseDateTimeBestEffortOrNull(gs.launch_date)))
                    AND (ifNull(trim(gs.block_date), '') = '' OR ud.date_val < toDate(parseDateTimeBestEffortOrNull(gs.block_date))),
                1,
                3
            ) AS match_priority,
            row_number() OVER (
                PARTITION BY ud.login_key, ud.date_val
                ORDER BY
                    match_priority ASC,
                    ifNull(toDate(parseDateTimeBestEffortOrNull(gs.launch_date)), toDate('1900-01-01')) DESC,
                    ifNull(gs.domain, '') ASC
            ) AS rn
        FROM
        (
            SELECT DISTINCT account_login AS login_key, `Date` AS date_val
            FROM yd
        ) ud
        LEFT JOIN raw_data.gsheet_sites gs
          ON lower(trim(ifNull(gs.login_key, ''))) = ud.login_key
    )
    WHERE rn = 1
),
lead_scored AS
(
    SELECT
        key3,
        lower(trim(ifNull(domain, ''))) AS domain_key,
        domain AS domain,
        fid AS fid,
        {_metric_expr("status", "reason", "source_type", "salon")}
    FROM ad_analytics.{LEADS_DEDUPED_STAGE}
    WHERE {_direct_lead_universe_filter()}
),
la AS
(
    SELECT
        key3,
        anyLast(domain_key) AS domain_key,
        anyLast(domain) AS domain,
        anyLast(fid) AS fid,
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
    FROM lead_scored
    GROUP BY key3
)
SELECT
    yd.key3 AS key3,
    yd.`Date` AS `Date`,
    {_weekday_expr("yd.`Date`")} AS `День недели`,
    yd.week_start AS week_start,
    yd.`CampaignId` AS `CampaignId`,
    yd.`CampaignName` AS `CampaignName`,
    yd.`AdGroupId` AS `AdGroupId`,
    yd.`AdGroupName` AS `AdGroupName`,
    yd.`AdNetworkType` AS `AdNetworkType`,
    yd.`Device` AS `Device`,
    toDecimal64(yd.`Impressions`, 6) AS `Impressions`,
    toDecimal64(yd.`Clicks`, 6) AS `Clicks`,
    toDecimal64(yd.total_cost, 6) AS total_cost,
    coalesce(nullIf(la.domain, ''), {_gs_pick_expr("domain")}) AS domain,
    yd.`RlAdjustmentId` AS `RlAdjustmentId`,
    toString(yd.`RlAdjustmentId`) AS `RlAdjustmentId_total`,
    yd.campaign_code AS campaign_code,
    yd.tp AS tp,
    yd.cpc_cpa AS cpc_cpa,
    yd.site_quiz AS site_quiz,
    yd.adgroup_code AS adgroup_code,
    yd.account_login AS account_login,
    coalesce(nullIf(yd.manager_login, ''), {_gs_pick_expr("directologist")}) AS manager_login,
    {_ag_parts_expr("yd.")},
    '' AS `марки авто`,
    crm.crm_name AS `Название crm`,
    if(la.kol_vo_zayavok > 0, 'Заявка', NULL) AS `тип_заявки`,
    ifNull(la.kol_vo_zayavok, toDecimal64(0, 6)) AS kol_vo_zayavok,
    ifNull(la.korr, toDecimal64(0, 6)) AS korr,
    ifNull(la.kval, toDecimal64(0, 6)) AS kval,
    ifNull(la.priezd, toDecimal64(0, 6)) AS priezd,
    ifNull(la.prodazhi, toDecimal64(0, 6)) AS prodazhi,
    ifNull(la.nekorr, toDecimal64(0, 6)) AS nekorr,
    ifNull(la.ne_otvechaet, toDecimal64(0, 6)) AS ne_otvechaet,
    ifNull(la.filtr, toDecimal64(0, 6)) AS filtr,
    ifNull(la.nedozvon, toDecimal64(0, 6)) AS nedozvon,
    ifNull(la.priedet, toDecimal64(0, 6)) AS priedet,
    ifNull(la.dohod_do_kredita, 0) AS dohod_do_kredita,
    ifNull(la.dobro, 0) AS dobro,
    {_gs_pick_expr("status")} AS `статус`,
    {_direct_specialist_expr()} AS `специалист`,
    {_gs_pick_expr("site_type")} AS `тип_сайта`,
    {_gs_pick_expr("template")} AS `шаблон`,
    {_gs_pick_expr("salon")} AS `салон`,
    {_gs_pick_expr("city")} AS `город`,
    {_gs_pick_expr("region")} AS `регион`,
    {_gs_pick_expr("direction")} AS direction,
    if(ifNull(yd.campaign_code, '') = '', 'неверный кодер', NULL) AS `неверный_кодер_new`,
    la.fid AS fid,
    {_gs_pick_expr("project_manager")} AS `проджект`,
    {_gs_pick_expr("client_id")} AS `id_салона`,
    {_gs_pick_expr("sales_manager")} AS `менеджер`,
    'Контекст' AS `источник`,
    if(startsWith(ifNull(yd.tp, ''), 'tp8') OR startsWith(ifNull(yd.tp, ''), 'tp9') OR startsWith(ifNull(yd.tp, ''), 'tp10'), 'Комплекс', 'Контекст') AS `направление`,
    concat(toString(yd.`CampaignId`), '|', ifNull(yd.`CampaignName`, '')) AS `номер кампании | название кампании`,
    concat(toString(yd.`AdGroupId`), '|', ifNull(yd.`AdGroupName`, '')) AS `номер группы | название группы`,
    CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
    CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
    concat(ifNull(yd.account_login, ''), '|', ifNull(coalesce(nullIf(la.domain, ''), {_gs_pick_expr("domain")}), '')) AS `аккаунт|сайт`,
    CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
    CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
    'Яндекс' AS `поставщик`,
    if(startsWith(ifNull(yd.tp, ''), 'tp8'), 'tp8', if(startsWith(ifNull(yd.tp, ''), 'tp9'), 'tp9', if(startsWith(ifNull(yd.tp, ''), 'tp10'), 'tp10', 'direct'))) AS _source_table,
    CAST(NULL, 'Nullable(String)') AS cascade_level,
    cs.campaign_status AS campaign_status,
    cs.payment_model AS payment_model
FROM yd
LEFT JOIN la ON la.key3 = yd.key3
LEFT JOIN gs_best gs ON gs.match_login_key = yd.account_login AND gs.match_date = yd.`Date`
LEFT JOIN gs_domain gs_dir ON gs_dir.domain_key = la.domain_key
LEFT JOIN crm_by_domain crm ON crm.domain_key = lower(trim(ifNull(coalesce(nullIf(la.domain, ''), {_gs_pick_expr("domain")}), '')))
LEFT JOIN ad_analytics.campaign_status_v cs ON cs.`CampaignId` = yd.`CampaignId`
WHERE ifNull({_gs_pick_expr("direction")}, 'Авто') = 'Авто'
"""


def _build_lead_source_sql(
    table: str,
    source_filter: str,
    source_name: str,
    direction_name: str,
    provider: str,
    lead_date_filter: str = "",
    source_table: str | None = None,
    extra_where: str = "",
) -> str:
    return f"""
CREATE TABLE ad_analytics.{table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(key3, ''))
AS
WITH
{_gs_account_cte()},
lead_scored AS
(
    SELECT
        l.*,
        lower(trim(ifNull(l.domain, ''))) AS domain_key,
        {_metric_expr("l.status", "l.reason", "l.source_type", "l.salon")}
    FROM ad_analytics.{LEADS_DEDUPED_STAGE} l
    WHERE {source_filter}
      {lead_date_filter}
),
gs_domain_best AS
(
    SELECT *
    FROM
    (
        SELECT
            ld.domain_key AS domain_key,
            ld.date_val AS match_date,
            gs.domain,
            gs.status,
            gs.directologist,
            gs.site_type,
            gs.template,
            gs.salon,
            gs.city,
            gs.region,
            gs.direction,
            gs.project_manager,
            gs.client_id,
            gs.sales_manager,
            gs.login_key,
            multiIf(
                ifNull(gs.domain, '') = '', 99,
                ifNull(trim(gs.launch_date), '') = '' AND ifNull(trim(gs.block_date), '') = '', 2,
                (ifNull(trim(gs.launch_date), '') = '' OR ld.date_val >= toDate(parseDateTimeBestEffortOrNull(gs.launch_date)))
                    AND (ifNull(trim(gs.block_date), '') = '' OR ld.date_val < toDate(parseDateTimeBestEffortOrNull(gs.block_date))),
                1,
                3
            ) AS match_priority,
            row_number() OVER (
                PARTITION BY ld.domain_key, ld.date_val
                ORDER BY
                    match_priority ASC,
                    ifNull(toDate(parseDateTimeBestEffortOrNull(gs.launch_date)), toDate('1900-01-01')) DESC,
                    ifNull(gs.domain, '') ASC
            ) AS rn
        FROM
        (
            SELECT DISTINCT domain_key, created_date AS date_val
            FROM lead_scored
            WHERE domain_key != ''
        ) ld
        LEFT JOIN raw_data.gsheet_sites gs
          ON lower(trim(ifNull(gs.domain, ''))) = ld.domain_key
    )
    WHERE rn = 1
)
SELECT
    l.key3 AS key3,
    l.created_date AS `Date`,
    {_weekday_expr("l.created_date")} AS `День недели`,
    toStartOfWeek(l.created_date, 1) AS week_start,
    l.campaign_id AS `CampaignId`,
    CAST(NULL, 'Nullable(String)') AS `CampaignName`,
    l.group_id AS `AdGroupId`,
    CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
    CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
    CAST(NULL, 'Nullable(String)') AS `Device`,
    toDecimal64(0, 6) AS `Impressions`,
    toDecimal64(0, 6) AS `Clicks`,
    toDecimal64(0, 6) AS total_cost,
    l.domain AS domain,
    l.correction_id AS `RlAdjustmentId`,
    toString(l.correction_id) AS `RlAdjustmentId_total`,
    CAST(NULL, 'Nullable(String)') AS campaign_code,
    CAST(NULL, 'Nullable(String)') AS tp,
    CAST(NULL, 'Nullable(String)') AS cpc_cpa,
    CAST(NULL, 'Nullable(String)') AS site_quiz,
    CAST(NULL, 'Nullable(String)') AS adgroup_code,
    gs.login_key AS account_login,
    gs.directologist AS manager_login,
    CAST(NULL, 'Nullable(String)') AS ag_part1,
    CAST(NULL, 'Nullable(String)') AS ag_part2,
    CAST(NULL, 'Nullable(String)') AS ag_part3,
    CAST(NULL, 'Nullable(String)') AS ag_part4,
    CAST(NULL, 'Nullable(String)') AS ag_part5,
    CAST(NULL, 'Nullable(String)') AS ag_part6,
    CAST(NULL, 'Nullable(String)') AS ag_part7,
    '' AS `марки авто`,
    crm.crm_name AS `Название crm`,
    if(ifNull(l.deal_type, '') = '', 'Заявка', l.deal_type) AS `тип_заявки`,
    l.kol_vo_zayavok,
    l.korr,
    l.kval,
    l.priezd,
    l.prodazhi,
    l.nekorr,
    l.ne_otvechaet,
    l.filtr,
    l.nedozvon,
    l.priedet,
    l.dohod_do_kredita,
    l.dobro,
    gs.status AS `статус`,
    {_domain_specialist_expr("gs")} AS `специалист`,
    gs.site_type AS `тип_сайта`,
    gs.template AS `шаблон`,
    coalesce(nullIf(l.salon, ''), gs.salon) AS `салон`,
    gs.city AS `город`,
    gs.region AS `регион`,
    gs.direction AS direction,
    CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
    l.fid AS fid,
    gs.project_manager AS `проджект`,
    gs.client_id AS `id_салона`,
    gs.sales_manager AS `менеджер`,
    '{source_name}' AS `источник`,
    '{direction_name}' AS `направление`,
    CAST(NULL, 'Nullable(String)') AS `номер кампании | название кампании`,
    CAST(NULL, 'Nullable(String)') AS `номер группы | название группы`,
    CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
    CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
    concat(ifNull(gs.login_key, ''), '|', ifNull(l.domain, '')) AS `аккаунт|сайт`,
    CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
    CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
    '{provider}' AS `поставщик`,
    '{source_table or table.replace("big_analytics_", "")}' AS _source_table,
    CAST(NULL, 'Nullable(String)') AS cascade_level,
    CAST(NULL, 'Nullable(String)') AS campaign_status,
    CAST(NULL, 'Nullable(String)') AS payment_model
FROM lead_scored l
LEFT JOIN gs_domain_best gs ON gs.domain_key = l.domain_key AND gs.match_date = l.created_date
LEFT JOIN crm_by_domain crm ON crm.domain_key = l.domain_key
WHERE ifNull(l.created_date, toDate('1970-01-01')) >= toDate('2026-01-01')
  {extra_where}
"""


def _build_empty_like_sql(table: str) -> str:
    return f"""
CREATE TABLE ad_analytics.{table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(`Date`)
ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(key3, ''))
AS
SELECT *
FROM ad_analytics.{SOURCE_STORE}
WHERE 0
"""


def _build_crop_sql() -> str:
    filt = """
(
    ifNull(utm_source, '') IN ('telegram','stories_tg','vk_storis','telegram_storis','max','vk','vk_groups','vkads')
    OR ifNull(utm_medium, '') IN ('posev','paid_social')
)
"""
    return _build_lead_source_sql("big_analytics_crop_targeting", filt, "Посевы", "Комплекс", "Посевы")


def _build_seo_sql(lead_date_filter: str = "") -> str:
    filt = """
(
    ifNull(utm_source, '') = ''
    OR (utm_source = 'seo' AND ifNull(utm_medium, '') = 'organic')
)
AND lowerUTF8(trim(ifNull(domain, ''))) IN (
    SELECT lowerUTF8(trim(ifNull(gs2.domain, '')))
    FROM raw_data.gsheet_sites gs2
    WHERE ifNull(gs2.domain, '') != ''
)
AND lowerUTF8(trim(ifNull(domain, ''))) NOT IN (
    SELECT lowerUTF8(trim(ifNull(ca.`Сайт`, '')))
    FROM ad_analytics.gsheets_crop_targeting_account ca
    WHERE ifNull(ca.`Сайт`, '') != ''
)
"""
    return _build_lead_source_sql("big_analytics_seo", filt, "SEO", "Комплекс", "Victory", lead_date_filter)


def _build_pixel_sql(lead_date_filter: str = "") -> str:
    raise RuntimeError("big_analytics_pixel is built by step5_build_pixel, not by step3")


# ══════════════════════════════════════════════════════════════════════════════
# DIRECT_LEAD_BRANCHES_2026-08-05 — две потерянные ветки лидов Директа
# ------------------------------------------------------------------------------
# Основная ветка (`_build_direct_sql`) идёт ОТ ТАБЛИЦЫ РАСХОДА: `FROM yd LEFT JOIN la`.
# Лид, чьего key3 нет в статистике Директа, не порождает ни одной строки — исчезает
# из витрины целиком. В v5 такие лиды жили отдельными ветками (step3.py:467/628/1109
# → direct_unmatched, :475/1287 → direct_zero) с total_cost=NULL.
#
# Обе ветки идут ОТ ЛИДА (`_build_lead_source_sql`), поэтому:
#   * total_cost / Impressions / Clicks = 0 — расход НЕ дублируется, только воронка;
#   * `_source_table` = 'direct_unmatched' / 'direct_zero' — ровно те значения, что
#     ждёт GOLDEN_SOURCES в data_check/verify_big_analytics.py:127.
#
# Дизъюнктность трёх веток (лид попадает ровно в одну):
#   direct           — key3 ∈ raw_yandex за тот же день;
#   direct_unmatched — key3 ∉ raw_yandex И key3 не '…|0|0|0|0';
#   direct_zero      — key3 вида '…|0|0|0|0' (в raw_yandex таких ключей 0 строк).
# Anti-join ограничен окном батча [lo, hi) СПЕЦИАЛЬНО: key3 начинается с даты
# (created_date у лида, `Date` у расхода), поэтому лид дня D может совпасть только
# со строкой расхода дня D. Ограничение окна = точный эквивалент глобального
# anti-join, но без построения множества из 4.7 млн ключей на каждый батч.
#
# `gs.direction = 'Авто'` — СТРОГОЕ равенство (NULL исключается) — воспроизводит
# v5-гейт `FROM big_analytics_direct WHERE direction = 'Авто'`
# (v5 step6_build_full/step6.py:114): лид на домене, которого нет в gsheet_sites,
# в витрину v5 не попадал. Без этого гейта ветки притащили бы ~273 тыс. лидов
# доменов-«ничьих» (domain_id IS NULL в CRM). ⚠️ Именно ЗДЕСЬ строгое равенство,
# а не `ifNull(…, 'Авто')` как в основной ветке: там гейт стоит поверх строки
# расхода, у которой домен известен из аккаунта Директа, здесь — поверх лида.
# ══════════════════════════════════════════════════════════════════════════════


def _build_direct_unmatched_sql(lo: str, hi: str) -> str:
    """Лиды Директа, чьего key3 нет в статистике расхода (v5: direct_unmatched)."""
    filt = f"""
{_direct_lead_universe_filter("l.")}
      AND l.key3 NOT LIKE '{_ZERO_KEY3_PATTERN}'
      AND l.key3 NOT IN (
          SELECT key3
          FROM ad_analytics.raw_yandex
          WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
            AND ifNull(key3, '') != ''
      )
"""
    return _build_lead_source_sql(
        "big_analytics_direct_unmatched",
        filt,
        "Контекст",
        "Комплекс",
        "Яндекс",
        _lead_date_filter(lo, hi),
        source_table="direct_unmatched",
        extra_where="AND gs.direction = 'Авто'",
    )


def _build_direct_zero_sql(lo: str, hi: str) -> str:
    """Лиды Директа без campaign_id (key3 '…|0|0|0|0') — v5: direct_zero."""
    filt = f"""
{_direct_lead_universe_filter("l.")}
      AND l.key3 LIKE '{_ZERO_KEY3_PATTERN}'
"""
    return _build_lead_source_sql(
        "big_analytics_direct_zero",
        filt,
        "Контекст",
        "Комплекс",
        "Яндекс",
        _lead_date_filter(lo, hi),
        source_table="direct_zero",
        extra_where="AND gs.direction = 'Авто'",
    )


def _build_crop_sql_batched(lead_date_filter: str = "") -> str:
    filt = """
(
    ifNull(utm_source, '') IN ('telegram','stories_tg','vk_storis','telegram_storis','max','vk','vk_groups','vkads')
    OR ifNull(utm_medium, '') IN ('posev','paid_social')
)
"""
    return _build_lead_source_sql("big_analytics_crop_targeting", filt, "Посевы", "Комплекс", "Посевы", lead_date_filter)


def _select_from_create(create_sql: str) -> str:
    marker = "\nAS\n"
    if marker not in create_sql:
        raise ValueError("CREATE TABLE SQL does not contain expected AS marker")
    return create_sql.split(marker, 1)[1].strip()


def _swap_shadow(client, table: str) -> None:
    swap_shadow(client, f"ad_analytics.{table}", f"ad_analytics.{table}_new")


def _rebuild_batched(client, table: str, empty_create_sql: str, batch_select_sqls: list[str]):
    target = f"ad_analytics.{table}"
    shadow = f"ad_analytics.{table}_new"
    client = _command_with_retry(client, f"DROP TABLE IF EXISTS {shadow} SYNC", label=f"{table} shadow drop")
    client = _command_with_retry(
        client,
        empty_create_sql.replace(target, shadow, 1),
        label=f"{table} shadow create",
        settings=STEP3_QUERY_SETTINGS,
    )
    for idx, select_sql in enumerate(batch_select_sqls, start=1):
        t0 = time.perf_counter()
        client = _command_with_retry(
            client,
            f"INSERT INTO {shadow}\n{select_sql}",
            label=f"{table} batch {idx}/{len(batch_select_sqls)}",
            settings=STEP3_QUERY_SETTINGS,
        )
        logger.info("    batch %s %d/%d inserted за %.1f сек", table, idx, len(batch_select_sqls), time.perf_counter() - t0)
    _swap_shadow(client, table)
    return client, int(client.query(f"SELECT count() FROM {target}", settings=STEP3_QUERY_SETTINGS).result_rows[0][0])


def _source_types_sql(source_types: tuple[str, ...]) -> str:
    return ", ".join(f"'{item}'" for item in source_types)


def recreate_source_views(client) -> None:
    direct_types = _source_types_sql(DIRECT_SOURCE_TYPES)
    replace_view(
        client,
        "ad_analytics.big_analytics_direct",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table IN ({direct_types})",
    )
    replace_view(
        client,
        "ad_analytics.big_analytics_seo",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table = 'seo'",
    )
    replace_view(
        client,
        "ad_analytics.big_analytics_pixel",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table = 'pixel'",
    )
    replace_view(
        client,
        "ad_analytics.big_analytics_crop_targeting",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table = 'crop_targeting'",
    )
    replace_view(
        client,
        "ad_analytics.big_analytics_reviews",
        f"SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table = 'reviews'",
    )


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 3 v6_ch: сборка source marts ClickHouse батчами")
    client = get_client()
    t0 = time.perf_counter()

    total = 0
    parts: list[str] = []
    shadow = f"ad_analytics.{SOURCE_STORE}_new"

    crm_problems = check_crm_mapping_coverage(client)
    if crm_problems:
        parts.append("crm_mapping_missing=" + "|".join(crm_problems))

    t_stage = time.perf_counter()
    client, leads_rows = _rebuild_leads_deduped_stage(client)
    parts.append(f"{LEADS_DEDUPED_STAGE}={leads_rows:,}")
    logger.info("  rebuilt ad_analytics.%s: %s rows за %.1f сек", LEADS_DEDUPED_STAGE, f"{leads_rows:,}", time.perf_counter() - t_stage)

    direct_ranges = day_ranges("2026-01-01")
    direct_batches = [
        _select_from_create(_build_direct_sql(SOURCE_STORE, f"AND ry.`Date` >= toDate('{lo}') AND ry.`Date` < toDate('{hi}')"))
        for lo, hi in direct_ranges
    ]
    logger.info("  rebuild ad_analytics.%s (%d direct daily batches)", SOURCE_STORE, len(direct_batches))
    client = _command_with_retry(client, f"DROP TABLE IF EXISTS {shadow} SYNC", label=f"{SOURCE_STORE} shadow drop")
    client = _command_with_retry(
        client,
        _build_direct_sql(f"{SOURCE_STORE}_new", "AND 0"),
        label=f"{SOURCE_STORE} shadow create",
        settings=STEP3_QUERY_SETTINGS,
    )
    for idx, select_sql in enumerate(direct_batches, start=1):
        t_batch = time.perf_counter()
        client = _command_with_retry(
            client,
            f"INSERT INTO {shadow}\n{select_sql}",
            label=f"{SOURCE_STORE} batch {idx}/{len(direct_batches)}",
            settings=STEP3_QUERY_SETTINGS,
        )
        logger.info(
            "    batch %s %d/%d inserted за %.1f сек",
            SOURCE_STORE,
            idx,
            len(direct_batches),
            time.perf_counter() - t_batch,
        )
    parts.append("direct=inserted")

    lead_ranges = day_ranges("2026-01-01")

    # Билдеры принимают окно батча (lo, hi): веткам direct_unmatched/direct_zero
    # нужна не только дата лида, но и то же окно для anti-join к raw_yandex.
    lead_builders = [
        ("big_analytics_seo", lambda lo, hi: _build_seo_sql(_lead_date_filter(lo, hi))),
        ("big_analytics_crop_targeting", lambda lo, hi: _build_crop_sql_batched(_lead_date_filter(lo, hi))),
        # DIRECT_LEAD_BRANCHES_2026-08-05
        ("direct_unmatched", _build_direct_unmatched_sql),
        ("direct_zero", _build_direct_zero_sql),
    ]
    for table, builder in lead_builders:
        t_table = time.perf_counter()
        batch_selects = [_select_from_create(builder(lo, hi)) for lo, hi in lead_ranges]
        logger.info("  append %s into ad_analytics.%s (%d daily batches)", table, SOURCE_STORE, len(batch_selects))
        for idx, select_sql in enumerate(batch_selects, start=1):
            t_batch = time.perf_counter()
            client = _command_with_retry(
                client,
                f"INSERT INTO {shadow}\n{select_sql}",
                label=f"{table} batch {idx}/{len(batch_selects)}",
                settings=STEP3_QUERY_SETTINGS,
            )
            logger.info(
                "    batch %s %d/%d inserted за %.1f сек",
                table,
                idx,
                len(batch_selects),
                time.perf_counter() - t_batch,
            )
        parts.append(f"{table}=inserted")
        logger.info("  %s inserted за %.1f сек", table, time.perf_counter() - t_table)

    _swap_shadow(client, SOURCE_STORE)
    recreate_source_views(client)

    source_rows = count_rows(client, f"ad_analytics.{SOURCE_STORE}")
    rows = count_rows(client, "ad_analytics.big_analytics_reviews")
    parts.append(f"big_analytics_reviews={rows:,}")
    parts.append(f"{SOURCE_STORE}={source_rows:,}")
    total = source_rows + rows

    client.command(f"DROP TABLE IF EXISTS ad_analytics.{LEADS_DEDUPED_STAGE} SYNC")
    logger.info("  cleaned ad_analytics.%s staging", LEADS_DEDUPED_STAGE)

    details = ", ".join(parts)
    logger.info("Шаг 3 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": total, "details": details}


def get_explain_sql(conn=None) -> str:  # noqa: ARG001
    return f"SELECT count() FROM ad_analytics.{SOURCE_STORE}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
