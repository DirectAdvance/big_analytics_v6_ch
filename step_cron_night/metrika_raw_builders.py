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
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, swap_shadow, table_exists

log = logging.getLogger("pipeline.metrika_raw_builders")


def _require(client, database: str, table: str) -> None:
    if not table_exists(client, database, table):
        raise RuntimeError(f"{database}.{table} отсутствует")


def build_metrika_yandex(client=None) -> int:
    client = client or get_client()
    for table in ("gsheet_sites", "metrika_yandex_counters", "metrika_yandex_goals"):
        _require(client, "raw_data", table)

    shadow = "ad_analytics.metrika_yandex_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        """
        CREATE TABLE ad_analytics.metrika_yandex_new
        ENGINE = MergeTree
        ORDER BY ifNull(domain, '')
        AS
        WITH
        goals AS
        (
            SELECT
                counter_id,
                anyLastIf(goal_id, name = 'Все формы') AS all_forms,
                anyLastIf(goal_id, name = 'CRM: Заказ создан') AS crm_order_created,
                anyLastIf(goal_id, name = 'CRM: Заказ оплачен') AS crm_order_paid,
                anyLastIf(goal_id, name = 'CRM: Спам заказ') AS crm_spam_order,
                anyLastIf(goal_id, name = 'CRM: Заказ отменен') AS crm_order_canceled
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
                anyLast(status) AS counter_status
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
            now() AS updated_at
        FROM sites s
        LEFT JOIN counters c
            ON c.counter_id = s.counter_id_from_site OR (s.counter_id_from_site IS NULL AND c.domain_key = s.domain_key)
        LEFT JOIN goals g ON g.counter_id = coalesce(s.counter_id_from_site, c.counter_id)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.metrika_yandex", shadow)
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


def build_check_utm(client=None, date_from: str = "2026-01-01", date_to: str | None = None) -> tuple[int, int]:
    client = client or get_client()
    _require(client, "raw_data", "metrika_yandex_utm_daily")
    _require(client, "raw_data", "metrika_yandex_counters")
    _require(client, "raw_data", "gsheet_sites")

    check_shadow = "ad_analytics.check_utm_new"
    fuck_shadow = "ad_analytics.check_utm_fuck_direct_new"
    dim_shadow = "ad_analytics.check_utm_counter_dim_new"
    client.command(f"DROP TABLE IF EXISTS {check_shadow} SYNC")
    client.command(f"DROP TABLE IF EXISTS {fuck_shadow} SYNC")
    client.command(f"DROP TABLE IF EXISTS {dim_shadow} SYNC")

    client.command(
        """
        CREATE TABLE ad_analytics.check_utm_counter_dim_new
        ENGINE = MergeTree
        ORDER BY counter_id
        AS
        WITH
        counters AS
        (
            SELECT
                counter_id,
                anyLast(domain) AS domain,
                anyLast(campaign_id) AS campaign_id,
                anyLast(ad_group_id) AS ad_group_id
            FROM raw_data.metrika_yandex_counters
            GROUP BY counter_id
        ),
        sites AS
        (
            SELECT
                lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                anyLast(login_key) AS login_key,
                anyLast(directologist) AS directologist
            FROM raw_data.gsheet_sites
            WHERE ifNull(domain, '') != ''
            GROUP BY domain_key
        )
        SELECT
            c.counter_id,
            c.domain,
            c.campaign_id,
            c.ad_group_id,
            s.login_key,
            s.directologist
        FROM counters c
        LEFT JOIN sites s ON s.domain_key = lowerUTF8(trim(ifNull(c.domain, '')))
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    client.command(
        """
        CREATE TABLE ad_analytics.check_utm_new
        (
            id Nullable(Int64),
            login Nullable(String),
            CampaignId Nullable(Int64),
            CampaignName Nullable(String),
            group_id Nullable(Int64),
            group_name Nullable(String),
            tracking_params Nullable(String),
            `домен` Nullable(String),
            cost Nullable(Decimal(18, 6)),
            `специалист` Nullable(String),
            date Nullable(Date),
            utm_source_type Nullable(String),
            visits Nullable(Int64),
            cls String
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(date, toDate('2026-01-01')))
        ORDER BY (ifNull(date, toDate('2026-01-01')), ifNull(login, ''), ifNull(CampaignId, 0), ifNull(group_id, 0), cls)
        """
    )
    client.command(
        """
        CREATE TABLE ad_analytics.check_utm_fuck_direct_new
        (
            id Nullable(Int64),
            login Nullable(String),
            CampaignId Nullable(Int64),
            CampaignName Nullable(String),
            group_id Nullable(Int64),
            group_name Nullable(String),
            tracking_params Nullable(String),
            `домен` Nullable(String),
            cost Nullable(Decimal(18, 6)),
            `специалист` Nullable(String),
            date Nullable(Date),
            utm_source_type Nullable(String)
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(date, toDate('2026-01-01')))
        ORDER BY (ifNull(date, toDate('2026-01-01')), ifNull(login, ''), ifNull(CampaignId, 0), ifNull(group_id, 0))
        """
    )

    chunks = day_ranges(date_from=date_from, date_to=date_to)
    if not chunks:
        raise ValueError(f"empty check_utm date range: date_from={date_from!r}, date_to={date_to!r}")
    log.info("check_utm chunked build: %d daily chunks, range=[%s, %s)", len(chunks), chunks[0][0], chunks[-1][1])
    for start, end in chunks:
        log.info("check_utm chunk %s -> %s", start, end)
        client.command(
            f"""
            INSERT INTO ad_analytics.check_utm_new
            WITH u AS
            (
                SELECT
                    m.day AS date,
                    m.counter_id,
                    ifNull(m.utm_source, '') AS utm_source,
                    ifNull(m.utm_medium, '') AS utm_medium,
                    ifNull(m.utm_campaign, '') AS utm_campaign,
                    ifNull(m.utm_content, '') AS utm_content,
                    ifNull(m.utm_term, '') AS utm_term,
                    sum(ifNull(m.visits, 0)) AS visits,
                    coalesce(
                        toInt64OrNull(extract(ifNull(m.utm_campaign, ''), '^(\\\\d+)')),
                        toInt64OrNull(extract(ifNull(m.utm_content, ''), 'cid[_:=](\\\\d+)'))
                    ) AS parsed_campaign_id,
                    coalesce(
                        toInt64OrNull(extract(ifNull(m.utm_content, ''), 'g:(\\\\d+)')),
                        toInt64OrNull(extract(ifNull(m.utm_content, ''), 'gbid[_-](\\\\d+)')),
                        toInt64OrNull(extract(ifNull(m.utm_content, ''), 'gid[_:=](\\\\d+)'))
                    ) AS parsed_group_id
                FROM raw_data.metrika_yandex_utm_daily m
                WHERE m.day >= toDate('{start}')
                  AND m.day < toDate('{end}')
                  AND (
                      lowerUTF8(ifNull(m.utm_source, '')) IN ('yandex', 'direct', 'yandex_direct')
                      OR lowerUTF8(ifNull(m.utm_medium, '')) IN ('cpc', 'context')
                      OR positionCaseInsensitive(ifNull(m.utm_content, ''), 'gbid') > 0
                      OR positionCaseInsensitive(ifNull(m.utm_content, ''), 'g:') > 0
                  )
                GROUP BY
                    date,
                    counter_id,
                    utm_source,
                    utm_medium,
                    utm_campaign,
                    utm_content,
                    utm_term,
                    parsed_campaign_id,
                    parsed_group_id
            )
            SELECT
                CAST(
                    bitAnd(cityHash64(toString(tuple(u.date, u.counter_id, u.utm_source, u.utm_medium, u.utm_campaign, u.utm_content, u.utm_term))), 9223372036854775807),
                    'Nullable(Int64)'
                ) AS id,
                CAST(cd.login_key, 'Nullable(String)') AS login,
                CAST(coalesce(u.parsed_campaign_id, cd.campaign_id), 'Nullable(Int64)') AS CampaignId,
                CAST(NULL, 'Nullable(String)') AS CampaignName,
                CAST(coalesce(u.parsed_group_id, cd.ad_group_id), 'Nullable(Int64)') AS group_id,
                CAST(NULL, 'Nullable(String)') AS group_name,
                CAST(concat('utm_source=', u.utm_source, '|utm_medium=', u.utm_medium, '|utm_campaign=', u.utm_campaign, '|utm_content=', u.utm_content), 'Nullable(String)') AS tracking_params,
                CAST(cd.domain, 'Nullable(String)') AS `домен`,
                CAST(toDecimal64(0, 6), 'Nullable(Decimal(18, 6))') AS cost,
                CAST(cd.directologist, 'Nullable(String)') AS `специалист`,
                CAST(u.date, 'Nullable(Date)') AS date,
                CAST('metrika', 'Nullable(String)') AS utm_source_type,
                CAST(u.visits, 'Nullable(Int64)') AS visits,
                multiIf(
                    u.parsed_campaign_id IS NULL AND u.parsed_group_id IS NULL, 'НЕТ_UTM',
                    cd.campaign_id != 0 AND u.parsed_campaign_id IS NOT NULL AND cd.campaign_id != u.parsed_campaign_id, 'ДРУГОЙ_UTM',
                    cd.ad_group_id != 0 AND u.parsed_group_id IS NOT NULL AND cd.ad_group_id != u.parsed_group_id, 'ДРУГОЙ_UTM',
                    'OK'
                ) AS cls
            FROM u
            LEFT JOIN ad_analytics.check_utm_counter_dim_new cd ON cd.counter_id = u.counter_id
            """,
            settings=SAFE_QUERY_SETTINGS,
        )

        client.command(
            f"""
            INSERT INTO ad_analytics.check_utm_fuck_direct_new
            SELECT
                id,
                login,
                CampaignId,
                CampaignName,
                group_id,
                group_name,
                tracking_params,
                `домен`,
                cost,
                `специалист`,
                date,
                utm_source_type
            FROM ad_analytics.check_utm_new
            WHERE date >= toDate('{start}')
              AND date < toDate('{end}')
              AND cls IN ('НЕТ_UTM', 'ДРУГОЙ_UTM')
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
    swap_shadow(client, "ad_analytics.check_utm", check_shadow)
    swap_shadow(client, "ad_analytics.check_utm_fuck_direct", fuck_shadow)
    client.command(f"DROP TABLE IF EXISTS {dim_shadow} SYNC")
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
