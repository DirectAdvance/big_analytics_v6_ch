"""Build ClickHouse `fact_criterion_zayavki` from CRM leads by utm_term."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import count_rows, swap_shadow
from criterion_spend.build_criterion_spend import CRITERION_CLEAN
from step3_build_sources.step3 import _metric_expr

logger = logging.getLogger("pipeline.build_criterion_zayavki")

UTM_TERM_CLEAN = """
trim(replaceRegexpAll(
    replaceRegexpAll(
        replaceRegexpAll(
            replaceRegexpAll(
                replaceAll(replaceAll(replaceAll(lower(splitByChar('|', ifNull(utm_term, ''))[1]), '\u00a0', ' '), '\u202f', ' '), '\u2009', ' '),
                '^-+',
                ''
            ),
            '\\\\s+-.*$',
            ''
        ),
        '[!+\\\\[\\\\]]',
        ''
    ),
    '\\\\s+',
    ' '
))
"""


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("build_criterion_zayavki v6_ch: fact_criterion_zayavki")
    client = get_client()
    t0 = time.perf_counter()
    shadow = "ad_analytics.fact_criterion_zayavki_new"
    metrics = _metric_expr("status", "reason", "source_type", "salon")
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(created_date)
        ORDER BY (created_date, ifNull(campaign_id, 0), ifNull(criterion, ''))
        AS
        WITH
        leads AS
        (
            SELECT
                assumeNotNull(created_date) AS created_date,
                campaign_id,
                {UTM_TERM_CLEAN} AS criterion,
                status,
                reason,
                source_type,
                salon,
                domain_id
            FROM ad_analytics.raw_leads
            WHERE created_date >= toDate('{DATE_FROM}')
              AND ifNull(deal_type, '') != 'Звонок'
              AND is_copy_for_removal = 0
              AND campaign_id IS NOT NULL
              AND notEmpty(trim(ifNull(utm_term, '')))
        ),
        lead_metrics AS
        (
            SELECT
                created_date,
                campaign_id,
                nullIf(criterion, '') AS criterion,
                salon,
                domain_id,
                {metrics}
            FROM leads
        ),
        agg AS
        (
            SELECT
                created_date,
                campaign_id,
                criterion,
                anyLast(salon) AS `салон`,
                anyLast(domain_id) AS domain_id,
                toInt64(sum(kol_vo_zayavok)) AS kol_vo_zayavok,
                toInt64(sum(korr)) AS korr,
                toInt64(sum(kval)) AS kval,
                toInt64(sum(priezd)) AS priezd,
                toInt64(sum(prodazhi)) AS prodazhi,
                toInt64(sum(nekorr)) AS nekorr,
                toInt64(sum(ne_otvechaet)) AS ne_otvechaet,
                toInt64(sum(filtr)) AS filtr,
                toInt64(sum(nedozvon)) AS nedozvon,
                toInt64(sum(priedet)) AS priedet,
                toInt64(sum(dohod_do_kredita)) AS dohod_do_kredita,
                toInt64(sum(dobro)) AS dobro
            FROM lead_metrics
            GROUP BY created_date, campaign_id, criterion
        ),
        campaign_names AS
        (
            SELECT
                `CampaignId` AS campaign_id,
                anyLast(`CampaignName`) AS campaign_name
            FROM ad_analytics.raw_yandex
            GROUP BY `CampaignId`
        ),
        criterion_dim AS
        (
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM criterion_norm)) AS criterion_key,
                anyLast(criterion_type) AS criterion_type,
                anyLast(criterion_raw) AS criterion_raw
            FROM
            (
                SELECT
                    criterion_norm,
                    criterion_raw,
                    multiIf(
                        positionCaseInsensitive(criterion_norm, 'autotargeting') > 0, 'autotargeting',
                        positionCaseInsensitive(criterion_norm, 'ретаргетинг') > 0, 'retargeting',
                        positionCaseInsensitive(criterion_norm, 'интерес') > 0 OR positionCaseInsensitive(criterion_norm, 'привычк') > 0, 'interests',
                        'keyword'
                    ) AS criterion_type
                FROM
                (
                    SELECT
                        {CRITERION_CLEAN} AS criterion_norm,
                        criterion AS criterion_raw
                    FROM raw_data.yandex_direct_report_rows
                    WHERE toDate(day) >= toDate('{DATE_FROM}')
                      AND campaign_id != 0
                )
            )
            WHERE notEmpty(lowerUTF8(trim(BOTH ' ' FROM criterion_norm)))
            GROUP BY criterion_key
        )
        SELECT
            toString(cityHash64(toString(a.created_date), ifNull(a.campaign_id, 0), ifNull(a.criterion, ''))) AS row_hash,
            a.created_date,
            a.campaign_id AS campaign_id,
            cn.campaign_name,
            a.criterion AS criterion,
            CAST(cd.criterion_type, 'LowCardinality(Nullable(String))') AS criterion_type,
            cd.criterion_raw,
            CAST(a.`салон`, 'LowCardinality(Nullable(String))') AS `салон`,
            a.domain_id AS domain_id,
            CAST(NULL, 'LowCardinality(Nullable(String))') AS `шаблон`,
            CAST(NULL, 'LowCardinality(Nullable(String))') AS `шаблон_марка`,
            a.kol_vo_zayavok,
            a.korr,
            a.kval,
            a.priezd,
            a.prodazhi,
            a.nekorr,
            a.ne_otvechaet,
            a.filtr,
            a.nedozvon,
            a.priedet,
            CAST(a.dohod_do_kredita, 'Nullable(Int64)') AS dohod_do_kredita,
            CAST(a.dobro, 'Nullable(Int64)') AS dobro,
            now() AS updated_at
        FROM agg a
        LEFT JOIN campaign_names cn ON cn.campaign_id = a.campaign_id
        LEFT JOIN criterion_dim cd ON cd.criterion_key = lowerUTF8(trim(BOTH ' ' FROM ifNull(a.criterion, '')))
        """
    )
    swap_shadow(client, "ad_analytics.fact_criterion_zayavki", shadow)
    rows = count_rows(client, "ad_analytics.fact_criterion_zayavki")
    details = f"fact_criterion_zayavki={rows:,}"
    logger.info("build_criterion_zayavki v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
