"""Fetch real Yandex Direct feed URLs through the Direct web UI cookie API.

Official feeds.get does not expose the source URL. The Direct web UI uses the
internal Grid GraphQL API and returns GdFeed.url in the FeedsGrid operation.

This module only enriches feed metadata:
  - public.yandex_direct_feed_urls
  - public.yandex_direct_feeds_report.feed_url
  - public.yandex_direct_feeds_report.feed_url_key
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import requests
import urllib3

from config.settings import DB_DST

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("pipeline.direct_feed_urls_cookie")

GRID_URL = "https://direct.yandex.ru/web-api/grid/api"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

AGENCY_LOGINS = [
    "y-direct-victory",
    "victoryagency14",
    "victorylotsofads1",
    "victoryagency-direct1618440",
    "victoryagencydirect",
    "useful-call-agency",
]

GLAVPOTOK_LOGINS = [
    "victorylotsofads1",
    "victoryagency-direct1618440",
    "victoryagency14",
]

FEEDS_GRID_QUERY = (
    "query FeedsGrid($login:String!$limitOffset:GdLimitOffsetInput"
    "$filter:GdFeedsFilterInput$ordersBy:[GdFeedsOrderByInput!]){"
    "reqId:getReqId client(searchBy:{login:$login}){"
    "feeds(limitOffset:$limitOffset filter:$filter ordersBy:$ordersBy){"
    "totalCount rowset{"
    "id name feedType updateStatus url lastChange offersCount listingsCount "
    "campaigns{id name flowType} isReadOnly isRefreshingAllowed source login "
    "hasPassword fileName isRemoveUtm landingUrl access{canDelete canEdit} "
    "errors{code messageRu messageEn} warnings{code messageRu messageEn}"
    "}}}}"
)


def _secret_path() -> None:
    root = Path(__file__).resolve().parents[2]
    secret = root.parent / ".secret"
    if secret.exists():
        sys.path.insert(0, str(secret))
    home_secret = Path.home() / ".secret"
    if home_secret.exists():
        sys.path.insert(0, str(home_secret))


def _load_cookies(refresh: bool) -> dict[str, str]:
    _secret_path()
    from loader import find_cookies  # type: ignore

    cookies_path = find_cookies()
    if refresh:
        try:
            from glavpotok_cookies import refresh_glavpotok_cookies  # type: ignore

            refresh_glavpotok_cookies(GLAVPOTOK_LOGINS, cookies_path)
            logger.info("cookies refreshed from glavpotok")
        except Exception as exc:
            logger.warning("cookies refresh failed, using existing cookies: %s", exc)

    with open(cookies_path, encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): str(v) for k, v in data.items() if v}


def _extract_csrf(resp: requests.Response) -> str | None:
    csrf = resp.cookies.get("_direct_csrf_token")
    if csrf:
        return csrf
    match = re.search(r"_direct_csrf_token=([^;,\s]+)", resp.headers.get("Set-Cookie", ""))
    return match.group(1) if match else None


def _post_feeds_grid(login: str, cookie: str, csrf: str | None = None) -> requests.Response:
    payload = {
        "operationName": "FeedsGrid",
        "query": FEEDS_GRID_QUERY,
        "variables": {
            "login": login,
            "limitOffset": {"limit": 500, "offset": 0},
            "filter": {},
            "ordersBy": [],
        },
    }
    headers = {
        "Cookie": cookie,
        "dna-operation-name": "FeedsGrid",
        "x-direct-api": "1",
        "x-detected-locale": "ru",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": f"https://direct.yandex.ru/wizard/feeds/?ulogin={login}",
        "User-Agent": USER_AGENT,
    }
    if csrf:
        headers["x-csrf-token"] = csrf
    url = f"{GRID_URL}?operationName=FeedsGrid&ulogin={login}"
    return requests.post(url, json=payload, headers=headers, timeout=60, verify=False)


def _feed_url_key(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url.strip())
    path = parsed.path or ""
    part = path.rstrip("/").rsplit("/", 1)[-1].strip().lower()
    if not part:
        return None
    return part or None


def _feed_url_key_for_source(url: str | None, source: str | None) -> str | None:
    """Compute feed_url_key with special handling for source='SITE'.

    SITE_FEED_URL_KEY_2026-06-30: фиды source='SITE' («Товары с сайта») имеют
    URL = корень домена (напр. https://ladaavtos-chlb.ru/) без файлового пути,
    поэтому _feed_url_key возвращает None. Для них возвращаем
    'Товары с сайта (<домен>)' вместо None.
    XML-фиды (source != 'SITE') используют прежнюю логику без изменений.
    """
    base = _feed_url_key(url)
    if base is not None:
        return base
    if source == "SITE" and url:
        parsed = urlparse(url.strip())
        netloc = parsed.netloc
        if netloc:
            domain = re.sub(r"^www\.", "", netloc).lower()
            return f"Товары с сайта ({domain})"
    return None


def fetch_login(login: str, cookies: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    last_error = ""
    for account in AGENCY_LOGINS:
        cookie = cookies.get(account)
        if not cookie:
            continue
        try:
            resp = _post_feeds_grid(login, cookie)
            if resp.status_code == 403:
                csrf = _extract_csrf(resp)
                if csrf:
                    resp = _post_feeds_grid(login, cookie, csrf)
            if resp.status_code != 200:
                last_error = f"{account}: HTTP {resp.status_code}"
                continue
            data = resp.json()
        except Exception as exc:
            last_error = f"{account}: {exc}"
            continue

        if data.get("errors"):
            last_error = f"{account}: {json.dumps(data.get('errors'), ensure_ascii=False)[:300]}"
            continue
        feeds = (((data.get("data") or {}).get("client") or {}).get("feeds") or {})
        rowset = feeds.get("rowset") or []
        rows: list[dict[str, Any]] = []
        for f in rowset:
            feed_id = f.get("id")
            if feed_id is None:
                continue
            feed_url = f.get("url")
            rows.append(
                {
                    "login_key": login,
                    "feed_id": int(feed_id),
                    "feed_name": f.get("name"),
                    "feed_url": feed_url,
                    "feed_url_key": _feed_url_key_for_source(feed_url, f.get("source")),
                    "source": f.get("source"),
                    "feed_type": f.get("feedType"),
                    "update_status": f.get("updateStatus"),
                    "offers_count": int(f["offersCount"]) if f.get("offersCount") is not None else None,
                    "listings_count": int(f["listingsCount"]) if f.get("listingsCount") is not None else None,
                    "cookie_account": account,
                }
            )
        return account, rows
    raise RuntimeError(f"{login}: no working cookie/API response; last_error={last_error}")


def _ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.yandex_direct_feed_urls (
                login_key text NOT NULL,
                feed_id bigint NOT NULL,
                feed_name text,
                feed_url text,
                feed_url_key text,
                source text,
                feed_type text,
                update_status text,
                offers_count integer,
                listings_count integer,
                cookie_account text,
                fetched_at timestamp without time zone NOT NULL DEFAULT now(),
                PRIMARY KEY (login_key, feed_id)
            );
            ALTER TABLE public.yandex_direct_feeds_report
                ADD COLUMN IF NOT EXISTS feed_url text,
                ADD COLUMN IF NOT EXISTS feed_url_key text;
            CREATE INDEX IF NOT EXISTS idx_yandex_direct_feeds_report_login_feed
                ON public.yandex_direct_feeds_report (login_key, feed_id);
            -- INDEX_AUDIT_2026-06-27: удалён мёртвый idx_yandex_direct_feeds_report_feed_url_key (idx_scan=0).
            """
        )
    conn.commit()


def _upsert_mapping(conn, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = """
        INSERT INTO public.yandex_direct_feed_urls (
            login_key, feed_id, feed_name, feed_url, feed_url_key, source,
            feed_type, update_status, offers_count, listings_count,
            cookie_account
        )
        VALUES %s
        ON CONFLICT (login_key, feed_id) DO UPDATE SET
            feed_name = EXCLUDED.feed_name,
            feed_url = EXCLUDED.feed_url,
            feed_url_key = EXCLUDED.feed_url_key,
            source = EXCLUDED.source,
            feed_type = EXCLUDED.feed_type,
            update_status = EXCLUDED.update_status,
            offers_count = EXCLUDED.offers_count,
            listings_count = EXCLUDED.listings_count,
            cookie_account = EXCLUDED.cookie_account,
            fetched_at = now()
    """
    values = [
        (
            r["login_key"],
            r["feed_id"],
            r["feed_name"],
            r["feed_url"],
            r["feed_url_key"],
            r["source"],
            r["feed_type"],
            r["update_status"],
            r["offers_count"],
            r["listings_count"],
            r["cookie_account"],
        )
        for r in rows
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, values, page_size=500)


def _update_report(conn, logins: list[str]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.yandex_direct_feeds_report r
            SET
                feed_url = u.feed_url,
                feed_url_key = u.feed_url_key
            FROM public.yandex_direct_feed_urls u
            WHERE r.login_key = u.login_key
              AND r.feed_id = u.feed_id
              AND r.login_key = ANY(%s)
              AND (
                    r.feed_url IS DISTINCT FROM u.feed_url
                 OR r.feed_url_key IS DISTINCT FROM u.feed_url_key
              )
            """,
            (logins,),
        )
        return int(cur.rowcount)


def _source_logins(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT login_key
            FROM public.yandex_direct_feeds_report
            WHERE login_key IS NOT NULL
              AND feed_id IS NOT NULL
            ORDER BY login_key
            """
        )
        return [r[0] for r in cur.fetchall()]


def run(logins: list[str] | None, all_logins: bool, apply: bool, refresh_cookies: bool, sleep: float) -> None:
    conn = psycopg2.connect(**DB_DST)
    try:
        _ensure_schema(conn)
        if all_logins:
            logins = _source_logins(conn)
        if not logins:
            raise RuntimeError("pass --login or --all-logins")

        cookies = _load_cookies(refresh_cookies)
        all_rows: list[dict[str, Any]] = []
        for i, login in enumerate(logins, 1):
            account, rows = fetch_login(login, cookies)
            logger.info("[%s/%s] %s: %s feeds via %s", i, len(logins), login, len(rows), account)
            all_rows.extend(rows)
            if sleep and i < len(logins):
                time.sleep(sleep)

        logger.info("fetched feed urls: %s rows", len(all_rows))
        if not apply:
            for row in all_rows[:50]:
                logger.info(
                    "dry row login=%s feed_id=%s name=%r url=%r key=%r",
                    row["login_key"],
                    row["feed_id"],
                    row["feed_name"],
                    row["feed_url"],
                    row["feed_url_key"],
                )
            logger.info("dry-run only; pass --apply to write")
            return

        _upsert_mapping(conn, all_rows)
        updated = _update_report(conn, list(logins))
        conn.commit()
        logger.info("updated yandex_direct_feeds_report rows: %s", updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="append", dest="logins", help="Direct client login. Can be repeated.")
    parser.add_argument("--all-logins", action="store_true", help="Fetch URLs for all logins from yandex_direct_feeds_report.")
    parser.add_argument("--apply", action="store_true", help="Write mapping and update yandex_direct_feeds_report.")
    parser.add_argument("--no-refresh-cookies", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(
        logins=args.logins,
        all_logins=args.all_logins,
        apply=args.apply,
        refresh_cookies=not args.no_refresh_cookies,
        sleep=args.sleep,
    )


if __name__ == "__main__":
    main()
