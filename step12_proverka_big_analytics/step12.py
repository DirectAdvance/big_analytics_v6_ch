"""Step 12 for v6_ch: ClickHouse quality checks."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, table_exists
from step3_build_sources.step3 import CROP_SOURCE_TYPES, SOURCE_STORE, _CROP_UTM_FILTER, _direct_lead_universe_filter

logger = logging.getLogger("pipeline.step12")


def _scalar(client, sql: str):
    return client.query(sql).result_rows[0][0]


def _source_types_sql(source_types: tuple[str, ...]) -> str:
    return ", ".join(f"'{source_type}'" for source_type in source_types)


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 12 v6_ch: проверки big_analytics ClickHouse")
    client = get_client()
    t0 = time.perf_counter()
    failures: list[str] = []

    required = ["big_analytics_full", SOURCE_STORE]
    for table in required:
        if not table_exists(client, "ad_analytics", table):
            failures.append(f"missing:{table}")
            continue
        if count_rows(client, f"ad_analytics.{table}") == 0:
            failures.append(f"empty:{table}")

    if table_exists(client, "ad_analytics", "big_analytics_full"):
        checks = {
            "full_before_2026": "SELECT count() FROM ad_analytics.big_analytics_full WHERE `Date` < toDate('2026-01-01')",
            "full_null_source": "SELECT count() FROM ad_analytics.big_analytics_full WHERE `источник` IS NULL OR `источник` = ''",
            "full_funnel_korr_lt_kval": "SELECT count() FROM ad_analytics.big_analytics_full WHERE korr < kval",
            "full_funnel_kval_lt_priezd": "SELECT count() FROM ad_analytics.big_analytics_full WHERE kval < priezd",
            "full_funnel_priezd_lt_prodazhi": "SELECT count() FROM ad_analytics.big_analytics_full WHERE priezd < prodazhi",
            "full_credit_lt_approved": "SELECT count() FROM ad_analytics.big_analytics_full WHERE dohod_do_kredita < dobro",
            # DIRECT_CROP_DISJOINT_2026-08-05: + 'direct_unmatched'. Каскадные лиды
            # (CASCADE_MATCH) уже покрыты — они лежат с _source_table='direct'/'tp*'.
            # ⛔ 'direct_zero' сюда добавить НЕЛЬЗЯ: у его строк key3 вырожден
            # ('дата|0|0|0|0'), и он совпадает у РАЗНЫХ лидов по построению — замерено
            # 203 совпадения key3 (51 по паре key3+domain) с абсолютно легальными
            # посевными лидами. Проверка бы падала всегда. Реальный инвариант для
            # zero-ветки закрывает `direct_crop_universe_overlap` ниже.
            "direct_crop_key_overlap": f"""
                SELECT count()
                FROM
                (
                    SELECT key3
                    FROM ad_analytics.big_analytics_sources
                    WHERE ifNull(key3, '') != ''
                      AND _source_table IN ('direct', 'tp8', 'tp9', 'tp10', 'direct_unmatched')
                    INTERSECT
                    SELECT key3
                    FROM ad_analytics.big_analytics_sources
                    WHERE ifNull(key3, '') != ''
                      AND _source_table IN ({_source_types_sql(CROP_SOURCE_TYPES)})
                )
            """,
            # DIRECT_CROP_DISJOINT_2026-08-05: дизъюнктность универсов НА УРОВНЕ
            # ПРЕДИКАТОВ, а не результата. Ловит корневую причину для ВСЕХ веток
            # Директа (включая direct_zero, чей key3 непроверяем) и не зависит от
            # гейта `direction='Авто'`, который сегодня случайно маскирует проблему:
            # без исключения по utm_medium здесь было 562 лида 2026, стало 0.
            "direct_crop_universe_overlap": f"""
                SELECT count()
                FROM
                (
                    SELECT key3, utm_source, utm_medium FROM ad_analytics.raw_leads
                    UNION ALL
                    SELECT key3, utm_source, utm_medium FROM ad_analytics.raw_perform_leads
                ) AS l
                WHERE {_direct_lead_universe_filter("l.")}
                  AND {_CROP_UTM_FILTER.strip()}
            """,
        }
        for name, sql in checks.items():
            value = int(_scalar(client, sql))
            logger.info("  %s=%d", name, value)
            if value:
                failures.append(f"{name}={value}")

    if failures:
        raise RuntimeError("CH quality checks failed: " + "; ".join(failures))

    rows = count_rows(client, "ad_analytics.big_analytics_full")
    logger.info("Шаг 12 v6_ch PASS за %.1f сек", time.perf_counter() - t0)
    return {"rows": rows, "details": "PASS"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
