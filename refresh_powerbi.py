#!/usr/bin/env python3
"""Selective refresh опубликованного BA6-датасета в Power BI Service."""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SECRET_DIR = next(
    (parent / ".secret" for parent in Path(__file__).resolve().parents
     if (parent / ".secret" / "loader.py").exists()),
    None,
)
if SECRET_DIR is None:
    raise RuntimeError("refresh_powerbi: .secret/loader.py не найден")
sys.path.insert(0, str(SECRET_DIR))

from loader import load_powerbi  # type: ignore  # noqa: E402
from config.ch_db import get_client  # noqa: E402
from config.ch_utils import SAFE_QUERY_SETTINGS  # noqa: E402
from config.tokens import (  # noqa: E402
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_PROXY_VARIANTS,
)
from notifications.telegram import TelegramMessage, TelegramSection, send_notification  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
POLL_TIMEOUT_SECONDS = 3600
FINGERPRINT_TABLE = "ad_analytics.powerbi_refresh_fingerprints"
PBI_SOURCE_ALIASES = {
    "big_analytics_full": ("bi_pbi_big_analytics_full", "pbi_big_analytics_full"),
    "direct_history": ("yandex_direct_history", "bi_yandex_direct_history"),
    "analytics_report_placement_links": ("bi_analytics_report_placement",),
    "yandex_direct_accounts_human_cyborgs": ("raw_new_human_cyborgs",),
}
TABLE_REF_RE = re.compile(
    r"\b(ad_analytics|raw_data|reference_data)\s*\.\s*(?:`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))"
)


class PowerBIRefreshError(RuntimeError):
    pass


@dataclass(frozen=True)
class TableFingerprint:
    model_table: str
    source_object: str
    rows: int
    fingerprint: str


_ALL_TABLES = [
    "big_analytics_full",
    "Dim_Date", "Dim_Campaign", "Dim_AdGroup", "Dim_Site", "Dim_City_Tier",
    "Dim_AdFormat", "Dim_AdText", "Dim_AdNetworkType", "Dim_Adjustment", "Dim_Device",
    "Dim_Source", "Dim_VkAdPlan", "Dim_VkAdGroup", "Dim_VkBanner", "Dim_ManagerLogin",
    "fact_vk_ads", "direct_history", "check_utm_fuck_direct",
    "yandex_direct_korrektirovki", "yandex_direct_404_errors",
    "pixel_score", "yandex_direct_cookie_analytics_website_pages",
    "fact_adformat_spend", "fact_criterion_spend", "fact_criterion_zayavki",
    "dim_criterion", "fact_region_spend", "fact_region_zayavki", "Dim_Location",
    "Dim_PlacementFeed", "fact_direct_feed_funnel",
    "analytics_report_placement_links", "yandex_direct_ads_texts",
    "yandex_direct_type_placement_report_master", "yandex_direct_search_query_report_master",
    "yandex_direct_accounts_human_cyborgs",
    "yandex_direct_minus_snapshot", "v_yandex_direct_minus_delta",
    "fact_ml_korrektirovki",
]


def build_run_failed_message() -> TelegramMessage:
    return TelegramMessage(
        title="🔴 refresh_powerbi: остановлен ошибкой",
        summary="Подробности — в логе refresh_powerbi на Victory.",
    )


def build_refresh_failed_message(status: str, request_id: str) -> TelegramMessage:
    return TelegramMessage(
        title=f"❌ Power BI: обновление завершилось со статусом {status}",
        sections=[TelegramSection("Детали", rows=[
            ("requestId", request_id or "—"),
            ("Полный текст ошибки", "в логе refresh_powerbi на Victory"),
        ])],
    )


def build_refresh_done_message(elapsed_seconds: int) -> TelegramMessage:
    if elapsed_seconds < 0:
        return TelegramMessage(title="✅ Power BI: refresh пропущен — данные не изменились")
    return TelegramMessage(
        title=f"✅ Power BI: обновление завершено "
              f"({elapsed_seconds // 60} мин {elapsed_seconds % 60} сек)"
    )


def build_poll_timeout_message() -> TelegramMessage:
    return TelegramMessage(
        title="⚠️ Power BI: не удалось получить статус обновления (таймаут опроса)"
    )


def _notify(message: TelegramMessage) -> None:
    proxy_chain = [None, *(proxy for proxy in TELEGRAM_PROXY_VARIANTS if proxy)]
    if not send_notification(
        message,
        bot_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID,
        proxy_variants=proxy_chain,
    ):
        logger.warning("Power BI: Telegram не доставлен")


def _token(config: dict) -> str:
    response = requests.post(
        f"https://login.microsoftonline.com/{config['tenant_id']}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "scope": "https://analysis.windows.net/powerbi/api/.default",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _api_base(config: dict) -> str:
    return (
        f"https://api.powerbi.com/v1.0/myorg/groups/{config['workspace_id']}"
        f"/datasets/{config['dataset_id']}"
    )


def _assert_ba6_datasource(api_base: str, headers: dict[str, str]) -> None:
    response = requests.get(f"{api_base}/datasources", headers=headers, timeout=30)
    response.raise_for_status()
    datasource_types = {
        str(source.get("datasourceType", "")).lower()
        for source in response.json().get("value", [])
    }
    if not datasource_types:
        raise PowerBIRefreshError("у датасета Power BI не найден источник данных")
    if "postgresql" in datasource_types:
        raise PowerBIRefreshError(
            "настроенный датасет всё ещё BA5/PostgreSQL; сначала опубликуй BA6/ClickHouse"
        )
    logger.info("Power BI: источник BA6 подтверждён: %s", sorted(datasource_types))


def _refresh_status(response: requests.Response) -> str:
    payload = response.json()
    if "status" in payload:
        return str(payload["status"])
    items = payload.get("value", [])
    return str(items[0].get("status", "")) if items else ""


def _ensure_fingerprint_table(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {FINGERPRINT_TABLE}
        (
            dataset_id String,
            model_table LowCardinality(String),
            source_object LowCardinality(String),
            rows UInt64,
            fingerprint String,
            recorded_at DateTime('UTC'),
            refresh_id String
        )
        ENGINE = MergeTree
        ORDER BY (dataset_id, model_table, recorded_at)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _source_candidates(model_table: str) -> list[str]:
    return [*PBI_SOURCE_ALIASES.get(model_table, ()), f"bi_{model_table}", model_table]


def _resolve_source_object(client, model_table: str) -> str:
    candidates = _source_candidates(model_table)
    rows = client.query(
        """
        SELECT name
        FROM system.tables
        WHERE database='ad_analytics' AND name IN {names:Array(String)}
        """,
        parameters={"names": candidates},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    found = {row[0] for row in rows}
    for candidate in candidates:
        if candidate in found:
            return candidate
    raise PowerBIRefreshError(f"Power BI table {model_table}: source object not found")


def _table_meta(client, database: str, table: str) -> tuple[str, str]:
    rows = client.query(
        """
        SELECT engine, create_table_query
        FROM system.tables
        WHERE database={database:String} AND name={table:String}
        """,
        parameters={"database": database, "table": table},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    if not rows:
        raise PowerBIRefreshError(f"{database}.{table}: source object not found")
    return rows[0][0], rows[0][1] or ""


def _referenced_tables(create_sql: str) -> list[tuple[str, str]]:
    return sorted(
        {
            (match.group(1), match.group(2) or match.group(3))
            for match in TABLE_REF_RE.finditer(create_sql)
        }
    )


def _source_leaf_tables(client, source_object: str) -> tuple[list[str], str]:
    leaves: set[str] = set()
    view_definitions: list[str] = []
    seen: set[tuple[str, str]] = set()

    def visit(database: str, table: str) -> None:
        object_key = (database, table)
        if object_key in seen:
            return
        seen.add(object_key)
        engine, create_sql = _table_meta(client, database, table)
        if engine != "View":
            leaves.add(f"{database}.{table}")
            return
        view_definitions.append(f"{database}.{table}:{create_sql}")
        refs = [ref for ref in _referenced_tables(create_sql) if ref != object_key]
        if not refs:
            raise PowerBIRefreshError(f"{database}.{table}: view dependencies not found")
        for ref_database, ref_table in refs:
            visit(ref_database, ref_table)

    visit("ad_analytics", source_object)
    view_hash = sha256("\n".join(sorted(view_definitions)).encode()).hexdigest()[:16]
    return sorted(leaves), view_hash


def _table_fingerprint(client, model_table: str) -> TableFingerprint:
    source_object = _resolve_source_object(client, model_table)
    leaf_tables, view_hash = _source_leaf_tables(client, source_object)
    row_count, byte_count, max_modified, parts_hash = client.query(
        """
        SELECT
            toUInt64(ifNull(sum(`rows`), 0)) AS row_count,
            toUInt64(ifNull(sum(bytes_on_disk), 0)) AS byte_count,
            toString(ifNull(max(modification_time), toDateTime(0))) AS max_part_modified,
            hex(groupBitXor(cityHash64(
                `table`,
                name,
                toString(`rows`),
                toString(bytes_on_disk),
                toString(modification_time)
            ))) AS parts_hash
        FROM system.parts
        WHERE active
          AND concat(database, '.', table) IN {tables:Array(String)}
        """,
        parameters={"tables": leaf_tables},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0]
    dependency_key = ",".join(leaf_tables)
    fingerprint = (
        f"{source_object}:{view_hash}:{dependency_key}:"
        f"{int(row_count)}:{byte_count}:{max_modified}:{parts_hash}"
    )
    return TableFingerprint(
        model_table=model_table,
        source_object=source_object,
        rows=int(row_count),
        fingerprint=fingerprint,
    )


def _current_fingerprints(client) -> list[TableFingerprint]:
    return [_table_fingerprint(client, table) for table in _ALL_TABLES]


def _latest_loaded_fingerprints(client, dataset_id: str) -> dict[str, str]:
    _ensure_fingerprint_table(client)
    rows = client.query(
        f"""
        SELECT model_table, argMax(fingerprint, recorded_at) AS fingerprint
        FROM {FINGERPRINT_TABLE}
        WHERE dataset_id={{dataset_id:String}}
        GROUP BY model_table
        """,
        parameters={"dataset_id": dataset_id},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    return {row[0]: row[1] for row in rows}


def _record_loaded_fingerprints(dataset_id: str, fingerprints: list[TableFingerprint], refresh_id: str) -> None:
    if not fingerprints:
        return
    client = get_client()
    _ensure_fingerprint_table(client)
    now = datetime.now(timezone.utc)
    client.insert(
        FINGERPRINT_TABLE,
        [
            (dataset_id, fp.model_table, fp.source_object, fp.rows, fp.fingerprint, now, refresh_id)
            for fp in fingerprints
        ],
        column_names=[
            "dataset_id",
            "model_table",
            "source_object",
            "rows",
            "fingerprint",
            "recorded_at",
            "refresh_id",
        ],
    )


def _plan_refresh_tables(dataset_id: str) -> tuple[list[str], list[TableFingerprint]]:
    client = get_client()
    current = _current_fingerprints(client)
    previous = _latest_loaded_fingerprints(client, dataset_id)
    if len(previous) < len(_ALL_TABLES):
        logger.info(
            "Power BI fingerprint: нет полного baseline (%d/%d) — refresh all",
            len(previous),
            len(_ALL_TABLES),
        )
        return list(_ALL_TABLES), current
    changed = [fp.model_table for fp in current if previous.get(fp.model_table) != fp.fingerprint]
    logger.info(
        "Power BI fingerprint: changed=%d unchanged=%d",
        len(changed),
        len(_ALL_TABLES) - len(changed),
    )
    if changed:
        logger.info("Power BI fingerprint changed tables: %s", ", ".join(changed[:20]))
    return changed, current


def refresh_powerbi(force: bool = False) -> int:
    config = load_powerbi()

    fingerprints: list[TableFingerprint] = []
    if force:
        refresh_tables = list(_ALL_TABLES)
        try:
            fingerprints = _current_fingerprints(get_client())
        except Exception:
            logger.warning("Power BI fingerprint planning failed for force refresh", exc_info=True)
    else:
        try:
            refresh_tables, fingerprints = _plan_refresh_tables(config["dataset_id"])
        except Exception:
            logger.warning("Power BI fingerprint planning failed; falling back to full refresh", exc_info=True)
            refresh_tables = list(_ALL_TABLES)
        if not refresh_tables:
            logger.info("Power BI: данные не изменились — refresh пропущен")
            return -1

    token = _token(config)
    headers = {"Authorization": f"Bearer {token}"}
    api_base = _api_base(config)

    _assert_ba6_datasource(api_base, headers)

    latest_url = f"{api_base}/refreshes?$top=1"
    latest = requests.get(latest_url, headers=headers, timeout=30)
    latest.raise_for_status()
    if _refresh_status(latest) == "Unknown":
        raise PowerBIRefreshError(
            "предыдущий refresh ещё выполняется; свежие данные pipeline не опубликованы"
        )

    logger.info("Power BI: запускаю selective refresh BA6 (%d таблиц)", len(refresh_tables))
    response = requests.post(
        f"{api_base}/refreshes",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "type": "full",
            "commitMode": "transactional",
            "maxParallelism": 1,
            "retryCount": 0,
            "notifyOption": "NoNotification",
            "objects": [{"table": table} for table in refresh_tables],
        },
        timeout=30,
    )
    if response.status_code not in (200, 202):
        raise PowerBIRefreshError(
            f"триггер refresh вернул HTTP {response.status_code}: {response.text[:500]}"
        )

    location = response.headers.get("Location", "")
    status_url = location or latest_url
    started = time.monotonic()
    while time.monotonic() - started <= POLL_TIMEOUT_SECONDS:
        try:
            status_response = requests.get(status_url, headers=headers, timeout=30)
            status_response.raise_for_status()
            status = _refresh_status(status_response)
        except requests.HTTPError as error:
            status_code = error.response.status_code if error.response is not None else 0
            if status_code not in {408, 429, 500, 502, 503, 504}:
                raise
            logger.warning("Power BI: временная ошибка polling HTTP %d", status_code)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        except (requests.ConnectionError, requests.Timeout, ValueError) as error:
            logger.warning("Power BI: временная ошибка polling: %s", error)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        logger.info("Power BI: статус=%s", status or "неизвестен")
        if status == "Completed":
            elapsed = int(time.monotonic() - started)
            try:
                _record_loaded_fingerprints(
                    config["dataset_id"],
                    fingerprints,
                    response.headers.get("RequestId", "") or response.headers.get("Location", ""),
                )
            except Exception:
                logger.warning("Power BI fingerprint baseline was not recorded", exc_info=True)
            return elapsed
        if status in {"Failed", "Cancelled", "Disabled"}:
            payload = status_response.json()
            details = payload if "status" in payload else (payload.get("value") or [{}])[0]
            request_id = details.get("requestId", "")
            service_error = details.get("serviceExceptionJson", "")
            logger.error(
                "Power BI: refresh %s (requestId=%s): %s",
                status,
                request_id or "—",
                service_error or "нет serviceExceptionJson",
            )
            raise PowerBIRefreshError(
                f"refresh завершился со статусом {status} (requestId={request_id or '—'})"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise PowerBIRefreshError("таймаут ожидания refresh (60 минут)")


def main(notify: bool = True, force: bool = False) -> int:
    try:
        elapsed = refresh_powerbi(force=force)
    except Exception as error:
        logger.error("Power BI BA6 refresh остановлен: %s", error, exc_info=True)
        if notify:
            _notify(build_run_failed_message())
        return 1

    if notify:
        _notify(build_refresh_done_message(elapsed))
    return 0


if __name__ == "__main__":
    args = set(sys.argv[1:])
    raise SystemExit(main(notify="--no-notify" not in args, force="--force" in args))
