"""Подключение к ClickHouse Victory (Yandex Cloud) для big_analytics_v6_ch.

Креды — из .secret/.env через loader.load_db('victory_clickhouse') (DB_VICTORY_CLICKHOUSE_*).
Managed ClickHouse Yandex Cloud использует сертификат Yandex Cloud CA, которого нет
в системном trust store по умолчанию — поэтому передаём ca_cert явно (см. get_ca_cert_path).
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import clickhouse_connect
from clickhouse_connect.driver.client import Client

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break
from loader import load_db  # noqa: E402

_YC_CA_URL = "https://storage.yandexcloud.net/cloud-certs/CA.pem"
_CA_CACHE_PATH = Path.home() / ".cache" / "yandex_cloud_ca.pem"


def get_ca_cert_path() -> str:
    """Путь к сертификату Yandex Cloud CA, скачивает и кеширует при первом вызове."""
    if not _CA_CACHE_PATH.exists():
        _CA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(_YC_CA_URL, _CA_CACHE_PATH)
    return str(_CA_CACHE_PATH)


def get_client(database: str | None = None) -> Client:
    """clickhouse-connect клиент к Victory ClickHouse.

    database=None → берётся DB_VICTORY_CLICKHOUSE_NAME (сейчас 'ad_analytics').
    Явно передать 'raw_data' для чтения сырых источников.
    """
    db = load_db("victory_clickhouse")
    return clickhouse_connect.get_client(
        host=db["host"],
        port=db["port"],
        username=db["user"],
        password=db["password"],
        database=database or db["database"],
        secure=True,
        ca_cert=get_ca_cert_path(),
    )


if __name__ == "__main__":
    client = get_client()
    print("ad_analytics ping:", client.command("SELECT 1"))
    raw = get_client("raw_data")
    print("raw_data ping:", raw.command("SELECT 1"))
    print("raw_data tables:", raw.command("SELECT count() FROM system.tables WHERE database='raw_data'"))
