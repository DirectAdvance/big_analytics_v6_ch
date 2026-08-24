#!/usr/bin/env python3
"""Selective refresh опубликованного BA6-датасета в Power BI Service."""

from __future__ import annotations

import logging
import sys
import time
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


class PowerBIRefreshError(RuntimeError):
    pass


_ALL_TABLES = [
    "big_analytics_full",
    "Dim_Date", "Dim_Campaign", "Dim_AdGroup", "Dim_Site", "Dim_City_Tier",
    "fact_vk_ads", "direct_history", "check_utm_fuck_direct",
    "yandex_direct_korrektirovki", "yandex_direct_404_errors",
    "pixel_score", "yandex_direct_cookie_analytics_website_pages",
    "fact_adformat_spend", "fact_criterion_spend", "fact_criterion_zayavki",
    "dim_criterion", "fact_region_spend", "fact_region_zayavki", "Dim_Location",
    "Dim_PlacementFeed", "fact_direct_feed_funnel",
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


def refresh_powerbi() -> int:
    config = load_powerbi()
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

    logger.info("Power BI: запускаю selective refresh BA6 (%d таблиц)", len(_ALL_TABLES))
    response = requests.post(
        f"{api_base}/refreshes",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "type": "full",
            "commitMode": "transactional",
            "maxParallelism": 1,
            "retryCount": 0,
            "notifyOption": "NoNotification",
            "objects": [{"table": table} for table in _ALL_TABLES],
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
            return int(time.monotonic() - started)
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


def main(notify: bool = True) -> int:
    try:
        elapsed = refresh_powerbi()
    except Exception as error:
        logger.error("Power BI BA6 refresh остановлен: %s", error, exc_info=True)
        if notify:
            _notify(build_run_failed_message())
        return 1

    if notify:
        _notify(build_refresh_done_message(elapsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(notify="--no-notify" not in sys.argv[1:]))
