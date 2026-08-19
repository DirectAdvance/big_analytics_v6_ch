"""Build Metrika-derived ClickHouse tables from existing raw_data snapshots.

These builders intentionally do not call Yandex Metrika API. The raw Metrika
layer is populated upstream in ClickHouse and contains counters, goals, 404
pageviews and daily UTM traffic.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, swap_shadow, table_exists

log = logging.getLogger("pipeline.metrika_raw_builders")


def _require(client, database: str, table: str) -> None:
    if not table_exists(client, database, table):
        raise RuntimeError(f"{database}.{table} отсутствует")


def build_metrika_yandex(client=None) -> int:
    client = client or get_client()
    for table in ("gsheet_sites", "metrika_yandex_counters", "metrika_yandex_goals"):
        _require(client, "raw_data", table)

    client.command("DROP TABLE IF EXISTS ad_analytics.metrika_yandex SYNC")
    client.command(
        """
        CREATE VIEW ad_analytics.metrika_yandex AS
        WITH
        goals AS
        (
            SELECT
                counter_id,
                anyLastIf(goal_id, name = 'Все формы') AS all_forms,
                anyLastIf(goal_id, name = 'CRM: Заказ создан') AS crm_order_created,
                anyLastIf(goal_id, name = 'CRM: Заказ оплачен') AS crm_order_paid,
                anyLastIf(goal_id, name = 'CRM: Спам заказ') AS crm_spam_order,
                anyLastIf(goal_id, name = 'CRM: Заказ отменен') AS crm_order_canceled,
                max(synced_at) AS goals_synced_at
            FROM raw_data.metrika_yandex_goals
            GROUP BY counter_id
        ),
        counters AS
        (
            SELECT
                counter_id,
                domain_key,
                anyLast(name) AS counter_name,
                anyLast(site) AS site,
                anyLast(status) AS counter_status,
                max(synced_at) AS counters_synced_at
            FROM
            (
                SELECT *, lowerUTF8(trim(ifNull(domain, ''))) AS domain_key
                FROM raw_data.metrika_yandex_counters
            )
            GROUP BY counter_id, domain_key
        ),
        sites AS
        (
            SELECT
                domain_key,
                anyLast(domain) AS domain,
                anyLast(login_key) AS login_key,
                anyLast(directologist) AS directologist,
                anyLast(status) AS status,
                toInt64OrNull(anyLast(nullIf(trim(ifNull(counter_number, '')), ''))) AS counter_id_from_site
            FROM
            (
                SELECT *, lowerUTF8(trim(ifNull(domain, ''))) AS domain_key
                FROM raw_data.gsheet_sites
                WHERE ifNull(domain, '') != ''
            )
            GROUP BY domain_key
        )
        SELECT
            s.domain,
            s.login_key,
            coalesce(s.counter_id_from_site, c.counter_id) AS counter_id,
            coalesce(c.counter_name, c.site) AS counter_name,
            s.directologist,
            CAST(g.all_forms, 'Nullable(Int64)') AS all_forms,
            CAST(g.crm_order_created, 'Nullable(Int64)') AS crm_order_created,
            CAST(g.crm_order_paid, 'Nullable(Int64)') AS crm_order_paid,
            CAST(g.crm_spam_order, 'Nullable(Int64)') AS crm_spam_order,
            CAST(g.crm_order_canceled, 'Nullable(Int64)') AS crm_order_canceled,
            coalesce(s.counter_id_from_site, c.counter_id) IS NOT NULL AS grant_done_lots1,
            coalesce(s.counter_id_from_site, c.counter_id) IS NOT NULL AS grant_done_lots04,
            coalesce(s.counter_id_from_site, c.counter_id) IS NOT NULL AS grant_done_skuderko1,
            s.status,
            CAST(
                if(
                    g.goals_synced_at IS NULL AND c.counters_synced_at IS NULL,
                    toDateTime64(now(), 6, 'UTC'),
                    greatest(
                        ifNull(g.goals_synced_at, toDateTime64(0, 6, 'UTC')),
                        ifNull(c.counters_synced_at, toDateTime64(0, 6, 'UTC'))
                    )
                ),
                'DateTime'
            ) AS updated_at
        FROM sites s
        LEFT JOIN counters c
            ON c.counter_id = s.counter_id_from_site OR (s.counter_id_from_site IS NULL AND c.domain_key = s.domain_key)
        LEFT JOIN goals g ON g.counter_id = coalesce(s.counter_id_from_site, c.counter_id)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    return count_rows(client, "ad_analytics.metrika_yandex")


def build_404_errors(client=None) -> int:
    client = client or get_client()
    _require(client, "raw_data", "metrika_yandex_not_found_daily")
    _require(client, "raw_data", "metrika_yandex_counters")
    _require(client, "raw_data", "gsheet_sites")

    shadow = "ad_analytics.yandex_direct_404_errors_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        """
        CREATE TABLE ad_analytics.yandex_direct_404_errors_new
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(visit_date, toDate('2026-01-01')))
        ORDER BY (ifNull(visit_date, toDate('2026-01-01')), ifNull(site, ''), ifNull(url, ''))
        AS
        WITH
        counters AS
        (
            SELECT
                counter_id,
                anyLast(name) AS counter_name,
                anyLast(site) AS site,
                anyLast(domain) AS domain
            FROM raw_data.metrika_yandex_counters
            GROUP BY counter_id
        ),
        sites AS
        (
            SELECT
                lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                anyLast(directologist) AS directologist
            FROM raw_data.gsheet_sites
            WHERE ifNull(domain, '') != ''
            GROUP BY domain_key
        )
        SELECT
            CAST(
                bitAnd(cityHash64(toString(tuple(n.day, n.counter_id, n.page_url, n.utm_campaign, n.utm_content))), 9223372036854775807),
                'Nullable(Int64)'
            ) AS id,
            CAST(toString(n.counter_id), 'Nullable(String)') AS `№ счетчика`,
            CAST(c.counter_name, 'Nullable(String)') AS counter_name,
            CAST(coalesce(n.domain, c.domain, c.site), 'Nullable(String)') AS site,
            CAST(s.directologist, 'Nullable(String)') AS `специалист`,
            CAST(n.page_url, 'Nullable(String)') AS url,
            CAST('404 ошибка', 'Nullable(String)') AS page_title,
            CAST(nullIf(n.utm_campaign, ''), 'Nullable(String)') AS utm_campaign,
            CAST(toInt64OrNull(extract(ifNull(n.utm_campaign, ''), '^(\\\\d+)')), 'Nullable(Int64)') AS `№ кампании`,
            CAST(nullIf(n.utm_content, ''), 'Nullable(String)') AS utm_content,
            CAST(
                coalesce(
                    toInt64OrNull(extract(ifNull(n.utm_content, ''), 'g:(\\\\d+)')),
                    toInt64OrNull(extract(ifNull(n.utm_content, ''), 'gbid[_-](\\\\d+)'))
                ),
                'Nullable(Int64)'
            ) AS `№ группы`,
            CAST(now(), 'Nullable(DateTime)') AS detected_at,
            CAST(n.day, 'Nullable(Date)') AS visit_date,
            CAST(toStartOfWeek(n.day, 1), 'Nullable(Date)') AS week_start
        FROM raw_data.metrika_yandex_not_found_daily n
        LEFT JOIN counters c ON c.counter_id = n.counter_id
        LEFT JOIN sites s ON s.domain_key = lowerUTF8(trim(ifNull(coalesce(n.domain, c.domain, c.site), '')))
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.yandex_direct_404_errors", shadow)
    return count_rows(client, "ad_analytics.yandex_direct_404_errors")


def _direct_tracking_class_sql(col: str) -> str:
    return f"""
        multiIf(
            ifNull({col}, '') = '' OR position({col}, 'utm_source') = 0,
                'НЕТ_UTM',
            position({col}, 'utm_source=s:{{source}}') > 0
            AND position({col}, 'utm_campaign={{campaign_id}}|{{campaign_name}}') > 0
            AND position({col}, 'utm_content=g:{{gbid}}') > 0
            AND position({col}, 'geoname:{{region_name}}') > 0
            AND position({col}, 'geoid:{{region_id}}') > 0
            AND position({col}, 'dev:{{device_type}}') > 0
            AND position({col}, 'r:{{retargeting_id}}') > 0
            AND position({col}, 'cor:{{coef_goal_context_id}}') > 0
            AND (
                position({col}, 'utm_term={{keyword}}') > 0
                OR position({col}, 'utm_term={{phrase}}') > 0
            )
            AND (
                position({col}, 'utm_medium=cpc') > 0
                OR position({col}, 'utm_medium=cpa') > 0
            ),
                'OK',
            'ДРУГОЙ_UTM'
        )
    """


def build_check_utm(client=None, date_from: str = "2026-01-01", date_to: str | None = None) -> tuple[int, int]:  # noqa: ARG001
    client = client or get_client()
    _require(client, "raw_data", "direct_adgroups")
    _require(client, "raw_data", "direct_campaigns")
    _require(client, "raw_data", "gsheet_sites")
    _require(client, "raw_data", "yandex_direct_report_rows")

    check_shadow = "ad_analytics.check_utm_new"
    fuck_shadow = "ad_analytics.check_utm_fuck_direct_new"
    client.command(f"DROP TABLE IF EXISTS {check_shadow} SYNC")
    client.command(f"DROP TABLE IF EXISTS {fuck_shadow} SYNC")

    lookback_days = int(os.getenv("CHECK_UTM_LOOKBACK_DAYS", "30"))
    history_days = int(os.getenv("CHECK_UTM_HISTORY_DAYS", "90"))
    class_sql = _direct_tracking_class_sql("ifNull(ag.tracking_params, '')")
    client.command(
        f"""
        CREATE TABLE ad_analytics.check_utm_new
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(date, toDate('2026-01-01')))
        ORDER BY (ifNull(date, toDate('2026-01-01')), ifNull(login, ''), ifNull(CampaignId, 0), ifNull(group_id, 0), cls)
        AS
        WITH
        active_logins AS
        (
            SELECT
                lowerUTF8(trim(ifNull(login_key, ''))) AS login,
                anyLast(directologist) AS directologist
            FROM raw_data.gsheet_sites
            WHERE status = 'Контекст активно'
              AND ifNull(login_key, '') != ''
            GROUP BY login
        ),
        recent_costs AS
        (
            SELECT
                lowerUTF8(trim(rr.client_login)) AS login,
                rr.campaign_id,
                ifNull(rr.ad_group_id, 0) AS group_id,
                anyLast(rr.campaign_name) AS campaign_name,
                anyLast(rr.ad_group_name) AS group_name,
                anyLast(rr.domain) AS domain,
                sum(ifNull(rr.total_cost, rr.cost)) AS cost_30d,
                max(toDate(rr.day)) AS last_date
            FROM raw_data.yandex_direct_report_rows AS rr
            WHERE toDate(rr.day) >= today() - {lookback_days}
              AND ifNull(rr.total_cost, rr.cost) > 0
              AND rr.campaign_id != 0
              AND ifNull(rr.ad_group_id, 0) != 0
            GROUP BY login, campaign_id, group_id
        ),
        adgroups AS
        (
            SELECT
                lowerUTF8(trim(account_login)) AS login,
                campaign_id,
                group_id,
                anyLast(group_name) AS group_name,
                anyLast(tracking_params) AS tracking_params,
                anyLast(status) AS status
            FROM raw_data.direct_adgroups
            GROUP BY login, campaign_id, group_id
        ),
        campaigns AS
        (
            SELECT
                lowerUTF8(trim(account_login)) AS login,
                campaign_id,
                anyLast(campaign_name) AS campaign_name,
                anyLast(status) AS campaign_status,
                anyLast(state) AS campaign_state
            FROM raw_data.direct_campaigns
            GROUP BY login, campaign_id
        )
        SELECT
            CAST(
                bitAnd(cityHash64(toString(tuple(rc.login, rc.campaign_id, rc.group_id, today()))), 9223372036854775807),
                'Nullable(Int64)'
            ) AS id,
            CAST(rc.login, 'Nullable(String)') AS login,
            CAST(rc.campaign_id, 'Nullable(Int64)') AS CampaignId,
            CAST(coalesce(c.campaign_name, rc.campaign_name), 'Nullable(String)') AS CampaignName,
            CAST(rc.group_id, 'Nullable(Int64)') AS group_id,
            CAST(coalesce(ag.group_name, rc.group_name), 'Nullable(String)') AS group_name,
            CAST(ag.status, 'Nullable(String)') AS status,
            {class_sql} AS cls,
            CAST(ifNull(ag.tracking_params, ''), 'Nullable(String)') AS tracking_params,
            CAST('direct', 'Nullable(String)') AS utm_source_type,
            CAST(rc.domain, 'Nullable(String)') AS domain,
            CAST(NULL, 'Nullable(Int64)') AS counter_id,
            CAST(al.directologist, 'Nullable(String)') AS `специалист`,
            CAST(rc.domain, 'Nullable(String)') AS `домен`,
            CAST(rc.cost_30d, 'Nullable(Decimal(18, 6))') AS cost,
            CAST(rc.last_date, 'Nullable(Date)') AS date,
            CAST(NULL, 'Nullable(Int64)') AS visits
        FROM recent_costs rc
        INNER JOIN active_logins al ON al.login = rc.login
        LEFT JOIN adgroups ag
            ON ag.login = rc.login AND ag.campaign_id = rc.campaign_id AND ag.group_id = rc.group_id
        LEFT JOIN campaigns c
            ON c.login = rc.login AND c.campaign_id = rc.campaign_id
        WHERE ifNull(c.campaign_state, '') NOT IN ('ARCHIVED')
          AND ifNull(c.campaign_status, '') NOT IN ('DRAFT')
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    client.command(
        f"""
        CREATE TABLE ad_analytics.check_utm_fuck_direct_new
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(date, toDate('2026-01-01')))
        ORDER BY (ifNull(date, toDate('2026-01-01')), ifNull(login, ''), ifNull(CampaignId, 0), ifNull(group_id, 0))
        AS
        WITH bad AS
        (
            SELECT DISTINCT
                login,
                CampaignId,
                CampaignName,
                group_id,
                group_name,
                tracking_params,
                `домен`,
                `специалист`,
                utm_source_type
            FROM ad_analytics.check_utm_new
            WHERE cls IN ('НЕТ_UTM', 'ДРУГОЙ_UTM')
              AND login IS NOT NULL
              AND CampaignId IS NOT NULL
              AND group_id IS NOT NULL
        ),
        daily_cost AS
        (
            SELECT
                lowerUTF8(trim(rr.client_login)) AS login,
                rr.campaign_id,
                ifNull(rr.ad_group_id, 0) AS group_id,
                toDate(rr.day) AS date,
                sum(ifNull(rr.total_cost, rr.cost)) AS cost
            FROM raw_data.yandex_direct_report_rows AS rr
            WHERE toDate(rr.day) >= today() - {history_days}
              AND ifNull(rr.total_cost, rr.cost) > 0
            GROUP BY login, campaign_id, group_id, date
        )
        SELECT
            CAST(
                bitAnd(cityHash64(toString(tuple(dc.date, b.login, b.CampaignId, b.group_id))), 9223372036854775807),
                'Nullable(Int64)'
            ) AS id,
            b.login AS login,
            b.CampaignId AS CampaignId,
            b.CampaignName AS CampaignName,
            b.group_id AS group_id,
            b.group_name AS group_name,
            b.tracking_params AS tracking_params,
            b.`домен` AS `домен`,
            CAST(dc.cost, 'Nullable(Decimal(18, 6))') AS cost,
            b.`специалист` AS `специалист`,
            CAST(dc.date, 'Nullable(Date)') AS date,
            b.utm_source_type AS utm_source_type
        FROM daily_cost dc
        INNER JOIN bad b
            ON b.login = dc.login AND b.CampaignId = dc.campaign_id AND b.group_id = dc.group_id
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.check_utm", check_shadow)
    swap_shadow(client, "ad_analytics.check_utm_fuck_direct", fuck_shadow)
    log.info(
        "check_utm built from Direct raw snapshots: lookback_days=%d history_days=%d",
        lookback_days,
        history_days,
    )
    return count_rows(client, "ad_analytics.check_utm"), count_rows(client, "ad_analytics.check_utm_fuck_direct")


def run_all() -> dict[str, int]:
    client = get_client()
    t0 = time.perf_counter()
    rows = {
        "metrika_yandex": build_metrika_yandex(client),
        "yandex_direct_404_errors": build_404_errors(client),
    }
    if os.getenv("BUILD_CHECK_UTM") == "1":
        check_rows, fuck_rows = build_check_utm(client)
        rows["check_utm"] = check_rows
        rows["check_utm_fuck_direct"] = fuck_rows
    else:
        log.info("check_utm skipped; run step_cron_night/step13_utm_direct_audit/run.py explicitly")
    log.info("metrika raw builders done in %.1f sec: %s", time.perf_counter() - t0, rows)
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run_all())
