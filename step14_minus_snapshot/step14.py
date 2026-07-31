#!/usr/bin/env python3
"""Step 14 for v6_ch: Yandex Direct negative keywords snapshot in ClickHouse."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import logging
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

from loader import load_yandex_direct, load_yandex_direct_reviews  # noqa: E402

from config.ch_db import get_client  # noqa: E402
from config.ch_settings import MINUS_SNAPSHOT_BLOCKS  # noqa: E402
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, q, swap_shadow, table_exists  # noqa: E402

logger = logging.getLogger("pipeline.step14")

API = "https://api.direct.yandex.com/json/v5/"
SESSION = requests.Session()

CAMPAIGN_IDS_CHUNK = 10
SETS_IDS_CHUNK = 10
INSERT_BATCH_SIZE = 5_000
RETENTION_DAYS = 30
DEFAULT_STATES = ["ON", "SUSPENDED", "OFF", "ENDED", "CONVERTED"]

TABLE = "ad_analytics.yandex_direct_minus_snapshot"
VIEW = "ad_analytics.v_yandex_direct_minus_delta"

BLOCK_MAP: list[tuple[str, str]] = [
    ("tp2", "tp2"),
    ("tp4", "tp4"),
    ("", "прочее"),
]

SNAPSHOT_COLUMNS = [
    "id",
    "date",
    "login",
    "campaign_id",
    "campaign_name",
    "campaign_state",
    "block",
    "minus_in_campaign",
    "minus_in_groups",
    "minus_in_sets",
    "minus_total",
    "has_minus",
    "check_ok",
    "loaded_at",
    "специалист",
]


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _n_items(value: object) -> int:
    if not value:
        return 0
    if isinstance(value, dict):
        return len(value.get("Items", []))
    return len(value)


def _detect_block(campaign_name: str | None) -> str:
    name = (campaign_name or "").lower()
    for substr, label in BLOCK_MAP:
        if substr == "" or substr in name:
            return label
    return "прочее"


def _stable_id(snap_date: dt.date, login: str, campaign_id: int) -> int:
    raw = f"{snap_date.isoformat()}|{login}|{campaign_id}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=False)


def load_tokens(only_key: str | None = None) -> dict[str, str]:
    """Return Direct API tokens keyed by stable local names."""
    direct = load_yandex_direct().get("tokens", {})
    tokens: dict[str, str] = {
        key: spec["oauth_token"]
        for key, spec in direct.items()
        if spec.get("oauth_token")
    }
    for idx, item in enumerate(load_yandex_direct_reviews(), start=1):
        oauth_token = item[0]
        login = item[1] if len(item) > 1 else f"reviews_{idx}"
        if oauth_token:
            tokens.setdefault(f"reviews:{login}", oauth_token)

    if only_key:
        if only_key not in tokens:
            raise RuntimeError(f"Direct token key not found: {only_key}")
        return {only_key: tokens[only_key]}
    if not tokens:
        raise RuntimeError("No Yandex Direct tokens loaded from .secret/.env")
    return tokens


def _call(service: str, payload: dict, token: str, login: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    if login:
        headers["Client-Login"] = login
    last = "?"
    for _ in range(5):
        try:
            response = SESSION.post(API + service, json=payload, headers=headers, timeout=45)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2)
                continue
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:120]
            time.sleep(2)
    return {"error": {"error_code": -1, "error_detail": f"net:{last}"}}


def enumerate_logins(tokens: dict[str, str]) -> dict[str, str]:
    """Return client login -> token key."""
    login_token: dict[str, str] = {}
    for key, token in tokens.items():
        offset = 0
        while True:
            result = _call(
                "agencyclients",
                {
                    "method": "get",
                    "params": {
                        "SelectionCriteria": {"Archived": "NO"},
                        "FieldNames": ["Login"],
                        "Page": {"Limit": 10000, "Offset": offset},
                    },
                },
                token,
            )
            if "result" not in result:
                logger.warning(
                    "AgencyClients under %s: %s",
                    key,
                    result.get("error", {}).get("error_detail"),
                )
                break
            for client in result["result"].get("Clients", []):
                login_token.setdefault(client["Login"], key)
            limited_by = result["result"].get("LimitedBy")
            if not limited_by:
                break
            offset = limited_by
            time.sleep(0.3)
    return login_token


def fetch_sets_sizes(set_ids: list[int], token: str, login: str) -> tuple[dict[int, int], bool]:
    sizes: dict[int, int] = {}
    ok = True
    if not set_ids:
        return sizes, ok
    for chunk in _chunks(set_ids, SETS_IDS_CHUNK):
        result = _call(
            "negativekeywordsharedsets",
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": chunk},
                    "FieldNames": ["Id", "NegativeKeywords"],
                },
            },
            token,
            login,
        )
        if "result" not in result:
            ok = False
            logger.debug(
                "[%s] negativekeywordsharedsets error: %s",
                login,
                result.get("error", {}).get("error_detail"),
            )
            for set_id in chunk:
                sizes[set_id] = 0
            continue
        for item in result["result"].get("NegativeKeywordSharedSets", []):
            sizes[item.get("Id")] = _n_items(item.get("NegativeKeywords"))
    return sizes, ok


def process_login(
    login: str,
    token: str,
    blocks: list[str],
    states: list[str],
    snap_date: dt.date,
    rows: list[list],
    specialist_map: dict[str, str],
) -> bool:
    campaigns: list[dict] = []
    offset = 0
    while True:
        result = _call(
            "campaigns",
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"States": states},
                    "FieldNames": ["Id", "Name", "State", "NegativeKeywords"],
                    "TextCampaignFieldNames": ["NegativeKeywordSharedSetIds"],
                    "Page": {"Limit": 10000, "Offset": offset},
                },
            },
            token,
            login,
        )
        if "result" not in result:
            logger.warning("[%s] campaigns.get error: %s", login, result.get("error", {}).get("error_detail"))
            return False
        campaigns.extend(result["result"].get("Campaigns", []))
        limited_by = result["result"].get("LimitedBy")
        if not limited_by:
            break
        offset = limited_by
        time.sleep(0.2)

    seen_campaigns: set[int] = set()
    login_key = login.split("@", 1)[0]
    specialist = specialist_map.get(login_key)

    for block_filter in blocks:
        filtered = [c for c in campaigns if block_filter.lower() in (c.get("Name") or "").lower()]
        if not filtered:
            continue

        ids = [c["Id"] for c in filtered]
        campaign_minus = {c["Id"]: _n_items(c.get("NegativeKeywords")) for c in filtered}
        campaign_set_ids: dict[int, list[int]] = {}
        for campaign in filtered:
            text_campaign = campaign.get("TextCampaign") or {}
            shared = text_campaign.get("NegativeKeywordSharedSetIds") or {}
            items = shared.get("Items", []) if isinstance(shared, dict) else (shared or [])
            campaign_set_ids[campaign["Id"]] = [int(x) for x in items] if items else []

        all_set_ids = list({sid for values in campaign_set_ids.values() for sid in values})
        set_size_cache, sets_ok = fetch_sets_sizes(all_set_ids, token, login)

        group_minus: dict[int, int] = defaultdict(int)
        groups_ok = True
        for chunk in _chunks(ids, CAMPAIGN_IDS_CHUNK):
            offset = 0
            while True:
                result = _call(
                    "adgroups",
                    {
                        "method": "get",
                        "params": {
                            "SelectionCriteria": {"CampaignIds": chunk},
                            "FieldNames": ["CampaignId", "NegativeKeywords"],
                            "Page": {"Limit": 10000, "Offset": offset},
                        },
                    },
                    token,
                    login,
                )
                if "result" not in result:
                    groups_ok = False
                    break
                for group in result["result"].get("AdGroups", []):
                    group_minus[group["CampaignId"]] += _n_items(group.get("NegativeKeywords"))
                limited_by = result["result"].get("LimitedBy")
                if not limited_by:
                    break
                offset = limited_by
                time.sleep(0.2)
            time.sleep(0.1)

        for campaign in filtered:
            campaign_id = int(campaign["Id"])
            if campaign_id in seen_campaigns:
                continue
            seen_campaigns.add(campaign_id)

            minus_in_campaign = campaign_minus.get(campaign_id, 0)
            minus_in_groups = group_minus.get(campaign_id, 0)
            minus_in_sets = sum(set_size_cache.get(sid, 0) for sid in campaign_set_ids.get(campaign_id, []))
            minus_total = minus_in_campaign + minus_in_groups + minus_in_sets
            check_ok = groups_ok and sets_ok
            rows.append(
                [
                    _stable_id(snap_date, login, campaign_id),
                    snap_date,
                    login,
                    campaign_id,
                    campaign.get("Name"),
                    campaign.get("State"),
                    _detect_block(campaign.get("Name")),
                    minus_in_campaign,
                    minus_in_groups,
                    minus_in_sets,
                    minus_total,
                    minus_total > 0,
                    check_ok,
                    dt.datetime.now(),
                    specialist,
                ]
            )
    return True


def ensure_schema(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE}
        (
            id UInt64,
            date Date,
            login String,
            campaign_id Int64,
            campaign_name Nullable(String),
            campaign_state Nullable(String),
            block Nullable(String),
            minus_in_campaign Int64,
            minus_in_groups Int64,
            minus_in_sets Int64,
            minus_total Int64,
            has_minus Bool,
            check_ok Bool,
            loaded_at DateTime,
            `специалист` Nullable(String)
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, login, campaign_id)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    replace_delta_view(client)


def replace_delta_view(client) -> None:
    client.command(f"DROP TABLE IF EXISTS {VIEW} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        f"""
        CREATE VIEW {VIEW} AS
        WITH snapshot AS
        (
            SELECT
                date,
                login,
                campaign_id,
                argMax(campaign_name, loaded_at) AS campaign_name,
                argMax(campaign_state, loaded_at) AS campaign_state,
                argMax(block, loaded_at) AS block,
                argMax(`специалист`, loaded_at) AS `специалист`,
                argMax(minus_in_campaign, loaded_at) AS minus_in_campaign,
                argMax(minus_in_groups, loaded_at) AS minus_in_groups,
                argMax(minus_in_sets, loaded_at) AS minus_in_sets,
                argMax(minus_total, loaded_at) AS minus_total,
                argMax(has_minus, loaded_at) AS has_minus,
                argMax(check_ok, loaded_at) AS check_ok
            FROM {TABLE}
            GROUP BY date, login, campaign_id
        ),
        with_prev AS
        (
            SELECT
                *,
                lagInFrame(toNullable(minus_total), 1, CAST(NULL, 'Nullable(Int64)')) OVER (
                    PARTITION BY login, campaign_id
                    ORDER BY date
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                ) AS minus_total_prev
            FROM snapshot
        )
        SELECT
            date,
            login,
            campaign_id,
            campaign_name,
            campaign_state,
            block,
            `специалист`,
            minus_in_campaign,
            minus_in_groups,
            minus_in_sets,
            minus_total,
            has_minus,
            check_ok,
            minus_total_prev,
            minus_total - minus_total_prev AS delta,
            multiIf(
                minus_total_prev IS NULL, 'первый замер',
                minus_total > minus_total_prev, 'добавили',
                minus_total < minus_total_prev, 'СНЯЛИ',
                'без изменений'
            ) AS dynamics
        FROM with_prev
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def load_specialist_map(client) -> dict[str, str]:
    rows = client.query(
        """
        SELECT
            login_key,
            min(directologist) AS specialist
        FROM raw_data.gsheet_sites
        WHERE login_key IS NOT NULL
          AND length(trim(BOTH ' ' FROM login_key)) > 3
          AND raw_data.gsheet_sites.directologist IS NOT NULL
          AND raw_data.gsheet_sites.directologist != ''
        GROUP BY login_key
        """,
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    result = {str(login_key): str(specialist) for login_key, specialist in rows}
    logger.info("Справочник специалистов: %d логинов из raw_data.gsheet_sites", len(result))
    return result


def load_candidate_logins(client, blocks: list[str]) -> set[str]:
    filters = [block for block in blocks if block]
    if not filters:
        return set()
    rows = client.query(
        """
        SELECT DISTINCT account_login
        FROM raw_data.direct_campaigns
        WHERE account_login IS NOT NULL
          AND account_login != ''
          AND arrayExists(
              block -> positionCaseInsensitive(ifNull(campaign_name, ''), block) > 0,
              {blocks:Array(String)}
          )
        """,
        parameters={"blocks": filters},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    result = {str(row[0]) for row in rows}
    logger.info("Кандидатов step14 по raw_data.direct_campaigns blocks=%s: %d", filters, len(result))
    return result


def _create_shadow(client, shadow: str, snap_date: dt.date) -> None:
    cutoff = snap_date - dt.timedelta(days=RETENTION_DAYS)
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, login, campaign_id)
        AS
        SELECT {", ".join(q(col) for col in SNAPSHOT_COLUMNS)}
        FROM {TABLE}
        WHERE date >= toDate('{cutoff.isoformat()}')
          AND date != toDate('{snap_date.isoformat()}')
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def insert_snapshot_rows(client, rows: list[list], snap_date: dt.date) -> int:
    shadow = "ad_analytics.yandex_direct_minus_snapshot_new"
    _create_shadow(client, shadow, snap_date)
    for batch in _chunks(rows, INSERT_BATCH_SIZE):
        client.insert(shadow, batch, column_names=SNAPSHOT_COLUMNS)
    swap_shadow(client, TABLE, shadow)
    replace_delta_view(client)
    return len(rows)


def run(
    conn=None,
    run_id: str | None = None,
    blocks: list[str] | None = None,
    states: list[str] | None = None,
    dry_run: bool = False,
    max_logins: int | None = None,
    token_key: str | None = None,
) -> dict:  # noqa: ARG001
    t0 = time.perf_counter()
    snap_date = dt.date.today()
    run_id = run_id or uuid.uuid4().hex[:8]
    blocks = blocks or list(MINUS_SNAPSHOT_BLOCKS or [""])
    states = states or list(DEFAULT_STATES)

    logger.info(
        "step14_minus_snapshot v6_ch START snap_date=%s blocks=%s states=%s run_id=%s",
        snap_date,
        blocks,
        states,
        run_id,
    )
    tokens = load_tokens(token_key)
    logger.info("Токенов Директа: %d", len(tokens))

    client = get_client()
    ensure_schema(client)
    specialist_map = load_specialist_map(client)
    candidate_logins = load_candidate_logins(client, blocks)

    login_token = enumerate_logins(tokens)
    logins = [login for login in login_token if not candidate_logins or login in candidate_logins]
    if max_logins:
        logins = logins[:max_logins]
    logger.info("Логинов к обходу: %d из %d agency clients", len(logins), len(login_token))

    rows: list[list] = []
    failed_logins = 0
    for idx, login in enumerate(logins, start=1):
        token = tokens[login_token[login]]
        try:
            ok = process_login(login, token, blocks, states, snap_date, rows, specialist_map)
            failed_logins += 0 if ok else 1
        except Exception as exc:  # noqa: BLE001
            failed_logins += 1
            logger.warning("[%s] unexpected error: %s", login, exc)
        if idx % 50 == 0:
            logger.info("  %d/%d логинов обработано | строк собрано: %d", idx, len(logins), len(rows))

    logger.info("Собрано строк snapshot: %d | failed_logins=%d", len(rows), failed_logins)
    if dry_run:
        return {
            "rows": len(rows),
            "details": f"dry_run yandex_direct_minus_snapshot={len(rows):,}, failed_logins={failed_logins}",
        }

    inserted = insert_snapshot_rows(client, rows, snap_date)
    total_rows = count_rows(client, TABLE)
    elapsed = time.perf_counter() - t0
    details = (
        f"yandex_direct_minus_snapshot_inserted={inserted:,}, "
        f"total={total_rows:,}, failed_logins={failed_logins}"
    )
    logger.info("step14_minus_snapshot v6_ch DONE за %.1f сек: %s", elapsed, details)
    return {"rows": inserted, "details": details}


def main() -> int:
    parser = argparse.ArgumentParser(description="step14: Yandex Direct negative keywords snapshot to ClickHouse")
    parser.add_argument("--block", action="append", dest="blocks", default=[])
    parser.add_argument("--states", default=",".join(DEFAULT_STATES))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-logins", type=int)
    parser.add_argument("--token")
    args = parser.parse_args()
    states = [s.strip() for s in args.states.split(",") if s.strip()]
    run(
        blocks=args.blocks or list(MINUS_SNAPSHOT_BLOCKS or [""]),
        states=states,
        dry_run=args.dry_run,
        max_logins=args.max_logins,
        token_key=args.token,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    raise SystemExit(main())
