"""ClickHouse v6 corrections hook.

The historical v5 file applied many PostgreSQL UPDATE statements after source
tables were built. v6_ch avoids large in-place updates: normalization and basic
classification are folded into `step3_build_sources.step3`, and late enrichment
is handled by shadow-table rebuilds in later steps.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.ch_db import get_client
from config.ch_utils import count_rows, swap_shadow, table_exists

logger = logging.getLogger("corrections")

COMPONENT_TABLES = ["big_analytics_sources"]

_KUDERKO_DATE = "2026-04-10"
_KUDERKO_NAME = "Кудерко Семен"
_KUDERKO_LOGINS = (
    "e-20086621", "e-20086622", "e-20084860", "e-20084861", "e-20086619",
    "porg-gcegsszl", "e-20086660", "e-20086659", "porg-h27zek57", "e-20086658",
    "e-20077075", "e-20086657", "porg-edmpebhr", "e-20086620", "e-20084857",
    "e-20086661", "e-20086623", "e-20084859", "e-20084858", "e-20076544",
    "e-20076545", "porg-kkhtgf2u", "e-20074366", "porg-mgrauofh", "e-20078432",
    "e-20077077", "e-20077078", "e-20078433", "e-20078430", "e-20077079",
    "e-20078431", "e-20077076", "e-20078429", "porg-7yibjfp4", "e-20076541",
    "e-20074364", "porg-pzm4243t", "porg-riga5gvo", "porg-sblzprjm", "porg-xagqvz3v",
    "porg-gbj6e3ji", "porg-3q2n22ux", "porg-x7iyctbh", "porg-qruhft2a", "porg-53t6ygdz",
    "porg-qeyeclqv", "porg-iljlldjs", "porg-wlta5kmb", "porg-cs34qdr7", "porg-cz2jqzbo",
    "e-20074363", "e-20076540", "porg-uguxrece", "porg-jnbd47au", "porg-klfzrvhu",
    "porg-v6ao2xka", "porg-jelgic43", "e-20076539", "e-20074365", "porg-nen5jouv",
    "porg-vvxm6gma", "porg-rcg54tv4", "porg-bczfmt3d", "porg-tr47xrja", "porg-5v4n6spu",
    "porg-p4uskpj6", "porg-wgyzlarl",
)

_SERGEEV_DATE = "2026-04-21"
_SERGEEV_NAME = "Сергеев Алексей"
_SERGEEV_LOGINS = (
    "porg-tde4jof6", "kazan-ca-532199-z761", "e-20074360", "porg-wzisnv32",
    "porg-rmkn7sz4", "porg-2xphfcul", "e-20074359", "porg-fuko7yzw", "e-20074361",
)

_PITERKINA_LOGIN = "porg-o2lqtxk5"
_PITERKINA_DATE = "2026-06-19"
_PITERKINA_NAME = "Питеркина Дарья"
_PITERKINA_LOGINS = (
    "direct175", "e-20074386", "e-20074391", "e-20075581", "e-20076024",
    "e-20076025", "e-20076032", "e-20076035", "e-20077735", "e-20080927",
    "e-20086590", "porg-3kybbaqw", "porg-52ddldh4", "porg-a7ysf76k",
    "porg-asnsozgg", "porg-cy3l6otz", "porg-de56ixiq", "porg-dnprpowd",
    "porg-efrpw7tl", "porg-g5el6elk", "porg-hwoltj3u", "porg-nqw6yxoc",
    "porg-o2lqtxk5", "porg-orfyrlvm", "porg-ounlaznf", "porg-p6eyociq",
    "porg-rrq4agov", "porg-ze76vrem",
)

SPECIALIST_DATE_RULES = (
    (_KUDERKO_NAME, _KUDERKO_DATE, _KUDERKO_LOGINS),
    (_SERGEEV_NAME, _SERGEEV_DATE, _SERGEEV_LOGINS),
    (_PITERKINA_NAME, _PITERKINA_DATE, _PITERKINA_LOGINS),
)


def _sql_list(items: tuple[str, ...]) -> str:
    return ", ".join(f"'{item}'" for item in items)


def specialist_correction_expr(
    date_expr: str,
    account_expr: str,
    specialist_expr: str,
) -> str:
    """Return CH expression matching v5 account-based specialist corrections."""
    branches: list[str] = []
    for name, date_barrier, logins in SPECIALIST_DATE_RULES:
        branches.append(
            f"{date_expr} < toDate('{date_barrier}') "
            f"AND lower(ifNull({account_expr}, '')) IN ({_sql_list(logins)}), '{name}'"
        )
    branches.append(
        f"lower(ifNull({account_expr}, '')) = '{_PITERKINA_LOGIN}' "
        f"AND empty(trim(ifNull({specialist_expr}, ''))), '{_PITERKINA_NAME}'"
    )
    branches.append(specialist_expr)
    return f"multiIf({', '.join(branches)})"


def _rule1_specialists(client) -> int:
    """CH-safe equivalent of v5 account/date specialist reassignment rules."""
    table = "ad_analytics.big_analytics_sources"
    shadow = "ad_analytics.big_analytics_sources_new"
    expr = specialist_correction_expr("`Date`", "account_login", "`специалист`")
    changed = int(
        client.query(
            f"""
            SELECT count()
            FROM {table}
            WHERE ifNull({expr}, '') != ifNull(`специалист`, '')
            """
        ).result_rows[0][0]
    )
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(`CampaignId`, 0), ifNull(key3, ''))
        AS
        SELECT
            * REPLACE (
                {expr} AS `специалист`
            )
        FROM {table}
        """
    )
    swap_shadow(client, table, shadow)
    logger.info("[corrections] Specialist account/date reassignment rules: %d rows", changed)
    return changed


def apply(conn=None, run_id: str | None = None) -> dict:  # noqa: A001, ARG001
    logger.info("corrections v6_ch: CH-safe shadow rebuild rules")
    client = get_client()
    t0 = time.perf_counter()
    changed = 0
    if table_exists(client, "ad_analytics", "big_analytics_sources"):
        changed += _rule1_specialists(client)
    total = 0
    parts: list[str] = []
    for table in COMPONENT_TABLES:
        if not table_exists(client, "ad_analytics", table):
            continue
        rows = count_rows(client, f"ad_analytics.{table}")
        total += rows
        parts.append(f"{table}={rows:,}")
    parts.append(f"specialist_rule_rows={changed:,}")
    details = ", ".join(parts)
    logger.info("corrections v6_ch завершены за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": total, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(apply())
