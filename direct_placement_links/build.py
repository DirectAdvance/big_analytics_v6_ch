"""Build tp8-tp10 Yandex Direct placement links from Direct Grid PagesReport."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, fix_mojibake_sql, swap_shadow, table_exists
from config.cookies import ensure_cookies_alive_or_stop

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("pipeline.direct_placement_links")

TARGET_TABLE = "ad_analytics.yandex_direct_tp_placement_links"
STAGING_TABLE = "ad_analytics.yandex_direct_tp_placement_link_matches"
GRID_URL = "https://direct.yandex.ru/web-api/grid/api"
CLIENTS_URL = "https://api.direct.yandex.com/json/v5/clients"
DEFAULT_PAGE_LIMIT = 5000
SPEND_TOLERANCE = Decimal("0.02")
MIN_TEXT_SCORE = Decimal("0.20")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

AGENCY_USER_IDS = {
    "victorylotsofads1": "2100790271",
    "victoryagency14": "2363588436",
    "victoryagency-direct1618440": "1785923399",
    "useful-call-agency": "2370640285",
}

GRAPHQL_QUERY = (
    "query CubeReportFacts($login:String!$input:GdCubeQueryReportInputInput!)"
    "{reqId:getReqId client(searchBy:{login:$login})"
    "{cubeQueryReport(input:$input)"
    "{reportState{axes{selectedDimensionAttributes selectedMeasures allLevelMeasures}}"
    "currencyCode axes{values}}}}"
)

PAGE_DIMS = ["Targettype", "PageGroup", "Campaign"]
PAGE_ATTRS = ["Targettype", "PageGroup", "PageGroupHomePage", "Campaign", "CampName"]
PAGE_MEASURES = ["Sum"]


@dataclass(frozen=True)
class RawPlacement:
    client_login: str
    manager_key: str
    campaign_id: int
    campaign_name: str
    placement: str
    spend: Decimal
    period_from: str
    period_to: str


@dataclass(frozen=True)
class CubePlacement:
    client_login: str
    campaign_id: int
    page_group: str
    link: str | None
    spend: Decimal


@dataclass(frozen=True)
class PlacementMatch:
    raw: RawPlacement
    cube_page_group: str | None
    placement_link: str | None
    cube_spend: Decimal | None
    spend_diff: Decimal | None
    match_status: str
    match_reason: str
    candidate_count: int


def normalized_tokens(text: str) -> tuple[str, ...]:
    words = re.findall(r"[\w]+", text.lower(), flags=re.UNICODE)
    return tuple(sorted(word for word in words if word))


def filter_unknown_placements(
    raw_rows: list[RawPlacement],
    known_links: dict[str, str | None],
) -> list[RawPlacement]:
    return [row for row in raw_rows if not known_links.get(row.placement)]


def _money_diff(left: Decimal, right: Decimal) -> Decimal:
    return abs(left - right)


def _is_same_money(left: Decimal, right: Decimal) -> bool:
    return _money_diff(left, right) <= SPEND_TOLERANCE


def _text_score(left: str, right: str) -> Decimal:
    left_tokens = set(normalized_tokens(left))
    right_tokens = set(normalized_tokens(right))
    if not left_tokens or not right_tokens:
        return Decimal("0")
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return Decimal(overlap) / Decimal(union)


def _empty_match(raw: RawPlacement, status: str, reason: str) -> PlacementMatch:
    return PlacementMatch(
        raw=raw,
        cube_page_group=None,
        placement_link=None,
        cube_spend=None,
        spend_diff=None,
        match_status=status,
        match_reason=reason,
        candidate_count=0,
    )


def _matched(raw: RawPlacement, cube: CubePlacement, status: str, reason: str, candidate_count: int) -> PlacementMatch:
    diff = _money_diff(raw.spend, cube.spend)
    return PlacementMatch(
        raw=raw,
        cube_page_group=cube.page_group,
        placement_link=cube.link,
        cube_spend=cube.spend,
        spend_diff=diff,
        match_status=status,
        match_reason=reason,
        candidate_count=candidate_count,
    )


def _best_by_spend(raw: RawPlacement, candidates: list[CubePlacement]) -> CubePlacement:
    return min(
        candidates,
        key=lambda item: (
            _money_diff(raw.spend, item.spend),
            item.page_group,
            item.link or "",
        ),
    )


def _best_by_text(raw: RawPlacement, candidates: list[CubePlacement]) -> tuple[CubePlacement, Decimal]:
    best = max(
        candidates,
        key=lambda item: (
            _text_score(raw.placement, item.page_group),
            -_money_diff(raw.spend, item.spend),
            item.page_group,
            item.link or "",
        ),
    )
    return best, _text_score(raw.placement, best.page_group)


def match_raw_placements(
    raw_rows: list[RawPlacement],
    cube_rows: list[CubePlacement],
) -> list[PlacementMatch]:
    exact_index: dict[tuple[str, int, str], list[CubePlacement]] = defaultdict(list)
    campaign_index: dict[tuple[str, int], list[CubePlacement]] = defaultdict(list)
    for cube in cube_rows:
        if not cube.link:
            continue
        exact_index[(cube.client_login, cube.campaign_id, cube.page_group.strip())].append(cube)
        campaign_index[(cube.client_login, cube.campaign_id)].append(cube)

    matches: list[PlacementMatch] = []
    for raw in raw_rows:
        exact_candidates = exact_index.get((raw.client_login, raw.campaign_id, raw.placement.strip()), [])
        if exact_candidates:
            cube = _best_by_spend(raw, exact_candidates)
            matches.append(_matched(raw, cube, "exact_name", "campaign_id + placement", len(exact_candidates)))
            continue

        campaign_candidates = campaign_index.get((raw.client_login, raw.campaign_id), [])
        same_spend = [cube for cube in campaign_candidates if _is_same_money(raw.spend, cube.spend)]
        if len(same_spend) == 1:
            matches.append(
                _matched(raw, same_spend[0], "same_campaign_spend", "campaign_id + spend", len(same_spend))
            )
            continue
        if len(same_spend) > 1:
            cube, score = _best_by_text(raw, same_spend)
            if score >= MIN_TEXT_SCORE:
                matches.append(
                    _matched(
                        raw,
                        cube,
                        "same_campaign_spend_text",
                        f"campaign_id + spend + text_score={score:.3f}",
                        len(same_spend),
                    )
                )
            else:
                matches.append(_empty_match(raw, "ambiguous_spend", f"same spend candidates={len(same_spend)}"))
            continue

        same_tokens = [
            cube
            for cube in campaign_candidates
            if normalized_tokens(raw.placement) == normalized_tokens(cube.page_group)
        ]
        if same_tokens:
            cube = _best_by_spend(raw, same_tokens)
            matches.append(_matched(raw, cube, "same_campaign_tokens", "campaign_id + placement tokens", len(same_tokens)))
            continue

        matches.append(_empty_match(raw, "not_matched", "no Direct Grid candidate"))
    return matches


def collapse_placement_links(matches: list[PlacementMatch]) -> list[tuple[str, str]]:
    grouped: dict[str, list[PlacementMatch]] = defaultdict(list)
    for match in matches:
        if match.placement_link:
            grouped[match.raw.placement].append(match)

    priority = {
        "skipped_existing": 100,
        "exact_name": 90,
        "same_campaign_spend_text": 80,
        "same_campaign_spend": 70,
        "same_campaign_tokens": 60,
    }
    links: list[tuple[str, str]] = []
    for placement, placement_matches in grouped.items():
        best = max(
            placement_matches,
            key=lambda item: (
                priority.get(item.match_status, 0),
                item.raw.spend,
                -(item.spend_diff or Decimal("999999999")),
                item.placement_link or "",
            ),
        )
        links.append((placement, best.placement_link or ""))
    return sorted(links, key=lambda item: item[0].lower())


def _date_filter_disjunctions(date_from: str, date_to: str) -> list[dict[str, Any]]:
    return [
        {
            "disjuncts": [
                {
                    "filterAttribute": {"measure": None, "dimensionAttribute": "Date"},
                    "filterValue": {"rawType": "Date", "dateValue": date_from},
                    "visibleInUi": True,
                    "filterOperator": "Ge",
                }
            ]
        },
        {
            "disjuncts": [
                {
                    "filterAttribute": {"measure": None, "dimensionAttribute": "Date"},
                    "filterValue": {"rawType": "Date", "dateValue": date_to},
                    "visibleInUi": True,
                    "filterOperator": "Le",
                }
            ]
        },
    ]


def _build_pages_payload(login: str, client_id: str, user_id: str, date_from: str, date_to: str, offset: int) -> dict:
    return {
        "operationName": "CubeReportFacts",
        "query": GRAPHQL_QUERY,
        "variables": {
            "login": login,
            "input": {
                "reportSubquery": {
                    "dimensions": PAGE_DIMS,
                    "orderBy": {"descending": True, "measure": "Sum", "row": None, "orderByValue": "VALUE"},
                    "uncollapseAll": False,
                    "limit": DEFAULT_PAGE_LIMIT,
                    "offset": offset,
                },
                "reportState": {
                    "reportStateId": 0,
                    "clientId": client_id,
                    "createdAt": "2026-08-06T00:00:00",
                    "lastUpdated": "2026-08-06T00:00:00",
                    "reportStateType": "Current",
                    "reportType": "PagesReport",
                    "userId": user_id,
                    "userReportId": "0",
                    "axes": [
                        {
                            "selectedDimensions": PAGE_DIMS,
                            "selectedDimensionAttributes": PAGE_ATTRS,
                            "selectedMeasures": PAGE_MEASURES,
                            "allLevelMeasures": PAGE_MEASURES,
                        }
                    ],
                    "name": None,
                    "cubeReportAudience": "Operator",
                    "withNds": True,
                    "attributionModel": "Automatic",
                    "filters": [{"disjunctions": _date_filter_disjunctions(date_from, date_to)}],
                    "cubeReportPresentation": {
                        "orderBy": {"descending": True, "measure": "Sum", "row": None, "orderByValue": "VALUE"},
                        "webUiState": "{}",
                    },
                },
            },
        },
    }


def _make_headers(cookie: str, login: str, csrf: str | None = None) -> dict[str, str]:
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "dna-operation-name": "CubeReportFacts",
        "x-direct-api": "1",
        "x-detected-locale": "ru",
        "User-Agent": USER_AGENT,
        "Origin": "https://direct.yandex.ru",
        "Referer": f"https://direct.yandex.ru/dna/statistics/direct/reports/{login}",
    }
    if csrf:
        headers["x-csrf-token"] = csrf
    return headers


def _extract_csrf(response: requests.Response) -> str | None:
    csrf = response.cookies.get("_direct_csrf_token")
    if csrf:
        return csrf
    match = re.search(r"_direct_csrf_token=([^;,\s]+)", response.headers.get("Set-Cookie", ""))
    return match.group(1) if match else None


def _post_grid(cookie: str, login: str, payload: dict) -> dict:
    params = {"operationName": "CubeReportFacts", "ulogin": login}
    for attempt in range(1, 4):
        response = requests.post(
            GRID_URL,
            params=params,
            headers=_make_headers(cookie, login),
            json=payload,
            timeout=60,
            verify=False,
        )
        if response.status_code == 403:
            csrf = _extract_csrf(response)
            if csrf:
                response = requests.post(
                    GRID_URL,
                    params=params,
                    headers=_make_headers(cookie, login, csrf),
                    json=payload,
                    timeout=60,
                    verify=False,
                )
        if response.status_code == 200:
            data = response.json()
            if data.get("errors"):
                raise RuntimeError(json.dumps(data["errors"], ensure_ascii=False)[:500])
            return data
        if response.status_code == 429 and attempt < 3:
            time.sleep(min(int(response.headers.get("Retry-After", "10")), 60))
            continue
        if attempt < 3 and response.status_code in {500, 502, 503, 504}:
            time.sleep(3 * attempt)
            continue
        raise RuntimeError(f"Grid API {response.status_code}: {response.text[:300]}")
    raise RuntimeError("Grid API: retries exhausted")


def _load_oauth_tokens() -> list[str]:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".secret" / "loader.py").exists():
            sys.path.insert(0, str(parent / ".secret"))
            break
    from loader import load_yandex_direct  # noqa: PLC0415

    direct = load_yandex_direct()
    return [item["oauth_token"] for item in direct.get("tokens", {}).values() if item.get("oauth_token")]


def _fetch_client_id(login: str, oauth_tokens: list[str]) -> str | None:
    body = {"method": "get", "params": {"FieldNames": ["Login", "ClientId"]}}
    for token in oauth_tokens:
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Login": login,
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Language": "ru",
        }
        try:
            response = requests.post(CLIENTS_URL, headers=headers, json=body, timeout=30)
            data = response.json()
            if data.get("error"):
                continue
            clients = data.get("result", {}).get("Clients") or []
            if clients:
                return str(clients[0]["ClientId"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] client_id fetch failed with one token: %s", login, exc)
    return None


def _parse_cube_rows(data: dict, login: str) -> list[CubePlacement]:
    axes = data["data"]["client"]["cubeQueryReport"]["axes"]
    if not axes:
        return []
    values = axes[0].get("values") or []
    if len(values) < 2:
        return []

    rows: list[CubePlacement] = []
    for item in values[1]:
        inner = item[0] if item and isinstance(item[0], list) else item
        if len(inner) < len(PAGE_ATTRS) + len(PAGE_MEASURES):
            continue
        page_group = str(inner[1] or "").strip()
        link = str(inner[2] or "").strip() or None
        campaign_id_raw = str(inner[3] or "").strip()
        spend = Decimal(str(inner[5] or "0"))
        if not page_group or not campaign_id_raw:
            continue
        try:
            campaign_id = int(campaign_id_raw)
        except ValueError:
            continue
        rows.append(CubePlacement(login, campaign_id, page_group, link, spend))
    return rows


def _fetch_cube_placements(
    login: str,
    manager_key: str,
    cookie: str,
    oauth_tokens: list[str],
    date_from: str,
    date_to: str,
) -> list[CubePlacement]:
    client_id = _fetch_client_id(login, oauth_tokens)
    if not client_id:
        raise RuntimeError("client_id not found")
    user_id = AGENCY_USER_IDS.get(manager_key)
    if not user_id:
        raise RuntimeError(f"unknown Direct operator userId for {manager_key}")

    rows: list[CubePlacement] = []
    offset = 0
    while True:
        payload = _build_pages_payload(login, client_id, user_id, date_from, date_to, offset)
        data = _post_grid(cookie, login, payload)
        batch = _parse_cube_rows(data, login)
        rows.extend(batch)
        if len(batch) < DEFAULT_PAGE_LIMIT:
            break
        offset += DEFAULT_PAGE_LIMIT
        time.sleep(0.2)
    return rows


def _load_raw_placements(client, only_login: str | None = None) -> list[RawPlacement]:
    only_sql = "AND client_login = {only_login:String}" if only_login else ""
    params = {"only_login": only_login} if only_login else None
    # Площадка чинится ДО группировки: битая и целая запись одного канала должны схлопнуться в одну
    # строку с общим расходом, иначе расход делится пополам и ломает сверку с расходом из Grid.
    fixed_placement = fix_mojibake_sql("ifNull(placement, '')")
    placement_sql = f"trim(BOTH ' ' FROM {fixed_placement})"
    rows = client.query(
        f"""
        SELECT
            client_login,
            replaceRegexpOne(any(manager_login), '@yandex\\\\.ru$', '') AS manager_key,
            campaign_id,
            any(campaign_name) AS campaign_name_any,
            placement,
            sum(cost) AS spend,
            min(day) AS period_from,
            max(day) AS period_to
        FROM (
            SELECT
                client_login,
                manager_login,
                campaign_id,
                ifNull(campaign_name, '') AS campaign_name,
                {placement_sql} AS placement,
                ifNull(cost, toDecimal128(0, 9)) AS cost,
                toDate(day) AS day
            FROM raw_data.yandex_direct_report_rows
            WHERE match(ifNull(campaign_name, ''), '(?i)tp(8|9|10)')
              AND trim(BOTH ' ' FROM ifNull(placement, '')) != ''
              AND campaign_id != 0
              {only_sql}
        )
        GROUP BY client_login, campaign_id, placement
        ORDER BY client_login, campaign_id, placement
        """,
        parameters=params,
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    return [
        RawPlacement(
            client_login=row[0],
            manager_key=row[1],
            campaign_id=int(row[2]),
            campaign_name=row[3],
            placement=row[4],
            spend=row[5],
            period_from=row[6].isoformat(),
            period_to=row[7].isoformat(),
        )
        for row in rows
    ]


def _load_existing_links(client, trust_cache: bool) -> dict[str, str | None]:
    if not trust_cache or not table_exists(client, "ad_analytics", "yandex_direct_tp_placement_links"):
        return {}
    # Кэш чинится тем же выражением: иначе битые ключи, накопленные прошлыми прогонами, живут
    # вечно — `_write_final_links` начинает сборку именно с них. `max` вместо `any` схлопывает
    # битого и целого близнеца в одну ссылку детерминированно (NULL игнорируется).
    rows = client.query(
        f"""
        SELECT placement, max(placement_link)
        FROM (
            SELECT {fix_mojibake_sql("ifNull(placement, '')")} AS placement, placement_link
            FROM {TARGET_TABLE}
            WHERE ifNull(placement, '') != ''
        )
        GROUP BY placement
        """,
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    return {row[0]: row[1] for row in rows}


def _match_existing(raw_rows: list[RawPlacement], known_links: dict[str, str | None]) -> list[PlacementMatch]:
    matches: list[PlacementMatch] = []
    for raw in raw_rows:
        link = known_links.get(raw.placement)
        if not link:
            continue
        matches.append(
            PlacementMatch(
                raw=raw,
                cube_page_group=None,
                placement_link=link,
                cube_spend=None,
                spend_diff=None,
                match_status="skipped_existing",
                match_reason="existing placement_link cache",
                candidate_count=0,
            )
        )
    return matches


def _cookie_for_manager(cookies: dict[str, str], manager_key: str) -> str | None:
    return cookies.get(manager_key) or cookies.get(manager_key.lower())


def _group_by_account(raw_rows: list[RawPlacement]) -> dict[tuple[str, str], list[RawPlacement]]:
    grouped: dict[tuple[str, str], list[RawPlacement]] = defaultdict(list)
    for row in raw_rows:
        grouped[(row.client_login, row.manager_key)].append(row)
    return grouped


def _fetch_and_match_account(
    account_key: tuple[str, str],
    raw_rows: list[RawPlacement],
    cookies: dict[str, str],
    oauth_tokens: list[str],
) -> list[PlacementMatch]:
    login, manager_key = account_key
    cookie = _cookie_for_manager(cookies, manager_key)
    if not cookie:
        return [_empty_match(row, "missing_cookie", f"no cookie for {manager_key}") for row in raw_rows]

    date_from = min(row.period_from for row in raw_rows)
    date_to = max(row.period_to for row in raw_rows)
    try:
        cube_rows = _fetch_cube_placements(login, manager_key, cookie, oauth_tokens, date_from, date_to)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Direct Grid fetch failed: %s", login, exc)
        return [_empty_match(row, "grid_fetch_failed", str(exc)[:250]) for row in raw_rows]
    return match_raw_placements(raw_rows, cube_rows)


def _create_final_table(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        f"""
        CREATE TABLE {target}
        (
            placement String,
            placement_link Nullable(String)
        )
        ENGINE = MergeTree
        ORDER BY placement
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _create_staging_table(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        f"""
        CREATE TABLE {target}
        (
            client_login LowCardinality(String),
            manager_key LowCardinality(String),
            campaign_id Int64,
            campaign_name String,
            raw_placement String,
            cube_page_group Nullable(String),
            placement_link Nullable(String),
            raw_spend Decimal(38, 9),
            cube_spend Nullable(Decimal(38, 9)),
            spend_diff Nullable(Decimal(38, 9)),
            match_status LowCardinality(String),
            match_reason String,
            candidate_count UInt16,
            period_from Date,
            period_to Date,
            updated_at DateTime
        )
        ENGINE = MergeTree
        ORDER BY (client_login, campaign_id, raw_placement)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _write_matches(client, matches: list[PlacementMatch]) -> None:
    shadow = f"{STAGING_TABLE}_new"
    _create_staging_table(client, shadow)
    if matches:
        now = datetime.now()
        client.insert(
            shadow,
            [
                [
                    match.raw.client_login,
                    match.raw.manager_key,
                    match.raw.campaign_id,
                    match.raw.campaign_name,
                    match.raw.placement,
                    match.cube_page_group,
                    match.placement_link,
                    match.raw.spend,
                    match.cube_spend,
                    match.spend_diff,
                    match.match_status,
                    match.match_reason,
                    match.candidate_count,
                    _as_date(match.raw.period_from),
                    _as_date(match.raw.period_to),
                    now,
                ]
                for match in matches
            ],
            column_names=[
                "client_login",
                "manager_key",
                "campaign_id",
                "campaign_name",
                "raw_placement",
                "cube_page_group",
                "placement_link",
                "raw_spend",
                "cube_spend",
                "spend_diff",
                "match_status",
                "match_reason",
                "candidate_count",
                "period_from",
                "period_to",
                "updated_at",
            ],
        )
    swap_shadow(client, STAGING_TABLE, shadow)


def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _write_final_links(client, raw_rows: list[RawPlacement], existing_links: dict[str, str | None], matches: list[PlacementMatch]) -> int:
    final_links = dict(existing_links)
    for placement, link in collapse_placement_links(matches):
        final_links[placement] = link
    for row in raw_rows:
        final_links.setdefault(row.placement, None)

    shadow = f"{TARGET_TABLE}_new"
    _create_final_table(client, shadow)
    rows = [[placement, link] for placement, link in sorted(final_links.items(), key=lambda item: item[0].lower())]
    if rows:
        client.insert(shadow, rows, column_names=["placement", "placement_link"])
    swap_shadow(client, TARGET_TABLE, shadow)
    return count_rows(client, TARGET_TABLE)


def build(client, only_login: str | None = None, refresh_cookies: bool = True) -> dict[str, int]:
    cookies = ensure_cookies_alive_or_stop("big_analytics_v6_ch.direct_placement_links") if refresh_cookies else {}
    if not cookies:
        cookies = ensure_cookies_alive_or_stop("big_analytics_v6_ch.direct_placement_links")

    raw_rows = _load_raw_placements(client, only_login=only_login)
    trust_cache = only_login is None and table_exists(client, "ad_analytics", "yandex_direct_tp_placement_link_matches")
    existing_links = _load_existing_links(client, trust_cache=trust_cache)
    skipped_matches = _match_existing(raw_rows, existing_links)
    unknown_rows = filter_unknown_placements(raw_rows, existing_links)
    oauth_tokens = _load_oauth_tokens()
    if not oauth_tokens and unknown_rows:
        raise RuntimeError("Yandex Direct OAuth tokens not configured")

    matches = list(skipped_matches)
    accounts = _group_by_account(unknown_rows)
    workers = max(1, int(os.getenv("YD_TP_PLACEMENT_LINK_WORKERS", "6")))
    logger.info(
        "direct placement links: raw=%d skipped_existing=%d unknown=%d accounts=%d workers=%d",
        len(raw_rows),
        len(skipped_matches),
        len(unknown_rows),
        len(accounts),
        workers,
    )

    if accounts:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_fetch_and_match_account, account_key, rows, cookies, oauth_tokens): account_key
                for account_key, rows in accounts.items()
            }
            for future in as_completed(futures):
                account_key = futures[future]
                try:
                    account_matches = future.result()
                except Exception as exc:  # noqa: BLE001
                    login, _manager = account_key
                    logger.warning("[%s] account matching failed: %s", login, exc)
                    account_matches = [_empty_match(row, "account_failed", str(exc)[:250]) for row in accounts[account_key]]
                matches.extend(account_matches)

    if only_login is None:
        _write_matches(client, matches)
        final_rows = _write_final_links(client, raw_rows, existing_links, matches)
    else:
        final_rows = 0

    matched = sum(1 for match in matches if match.placement_link)
    missing = len(matches) - matched
    return {"raw": len(raw_rows), "matched": matched, "missing": missing, "final_rows": final_rows}


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("direct_placement_links v6_ch: tp8-tp10 PagesReport links")
    client = get_client()
    t0 = time.perf_counter()
    stats = build(client)
    details = (
        f"placement_links={stats['final_rows']:,}, raw={stats['raw']:,}, "
        f"matched={stats['matched']:,}, missing={stats['missing']:,}"
    )
    logger.info("direct_placement_links завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": stats["final_rows"], "details": details}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", help="Dry-run one Direct client login without writing ClickHouse tables.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    client = get_client()
    stats = build(client, only_login=args.login)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
