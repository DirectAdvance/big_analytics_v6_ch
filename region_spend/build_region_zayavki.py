"""Build ClickHouse `fact_region_zayavki` from CRM leads by geoid."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import count_rows, swap_shadow
from step3_build_sources.step3 import _metric_expr

logger = logging.getLogger("pipeline.build_region_zayavki")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("build_region_zayavki v6_ch: fact_region_zayavki")
    client = get_client()
    t0 = time.perf_counter()
    shadow = "ad_analytics.fact_region_zayavki_new"
    metrics = _metric_expr("status", "reason", "source_type", "salon")
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(created_date)
        ORDER BY (created_date, ifNull(campaign_id, 0), ifNull(id_location, 0))
        AS
        WITH
        leads AS
        (
            SELECT
                assumeNotNull(created_date) AS created_date,
                campaign_id,
                toInt64OrNull(extract(ifNull(utm_content, ''), 'geoid:([0-9]+)')) AS id_location,
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
              AND match(ifNull(utm_content, ''), 'geoid:[0-9]+')
        ),
        lead_metrics AS
        (
            SELECT
                created_date,
                campaign_id,
                id_location,
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
                id_location,
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
            GROUP BY created_date, campaign_id, id_location
        ),
        campaign_names AS
        (
            SELECT
                `CampaignId` AS campaign_id,
                anyLast(`CampaignName`) AS campaign_name
            FROM ad_analytics.raw_yandex
            GROUP BY `CampaignId`
        ),
        locations AS
        (
            SELECT
                id_location,
                anyLast(location) AS location,
                anyLast(`Область`) AS `Область`,
                anyLast(GeoRegionType) AS GeoRegionType,
                toInt32OrNull(toString(round(anyLast(distance_km)))) AS distance_km,
                anyLast(distance_km_agreg) AS distance_km_agreg
            FROM ad_analytics.fact_region_spend
            WHERE id_location IS NOT NULL
            GROUP BY id_location
        )
        SELECT
            toString(cityHash64(toString(a.created_date), ifNull(a.campaign_id, 0), ifNull(a.id_location, 0))) AS row_hash,
            a.created_date,
            a.campaign_id AS campaign_id,
            cn.campaign_name,
            a.id_location AS id_location,
            loc.location,
            loc.`Область`,
            loc.GeoRegionType,
            loc.distance_km,
            loc.distance_km_agreg,
            CAST(a.`салон`, 'LowCardinality(Nullable(String))') AS `салон`,
            a.domain_id AS domain_id,
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
        LEFT JOIN locations loc ON loc.id_location = a.id_location
        """
    )
    swap_shadow(client, "ad_analytics.fact_region_zayavki", shadow)
    rows = count_rows(client, "ad_analytics.fact_region_zayavki")
    details = f"fact_region_zayavki={rows:,}"
    logger.info("build_region_zayavki v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
