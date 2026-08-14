"""Recheck URLs from ClickHouse yandex_direct_404_errors with real HTTP codes."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.ch_db import get_client  # noqa: E402
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, table_exists  # noqa: E402

logger = logging.getLogger("pipeline.recheck_404")

DEFAULT_DAYS = 30
WORKERS = 12
PER_HOST = 2
TIMEOUT = 15
RETRIES = 2
BODY_LIMIT = 64 * 1024
DELETE_BATCH_SIZE = 500

UA = "Mozilla/5.0 (compatible; VictoryLinkChecker/1.0; +https://victoryagency.ru/) Python-requests"
DROP_PARAMS = re.compile(r"^(utm_\w+|yclid|gclid|ysclid|fbclid|_openstat|from|erid|roistat\w*)$", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

_host_sem: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(PER_HOST))
_sem_lock = threading.Lock()


def _sem(host: str) -> threading.Semaphore:
    with _sem_lock:
        return _host_sem[host]


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not DROP_PARAMS.match(k)]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def is_soft_404(html: str) -> bool:
    match = TITLE_RE.search(html)
    return bool(match) and "404" in match.group(1)


def check_url(url: str) -> tuple[str, str, str]:
    target = clean_url(url)
    parsed = urlsplit(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return url, "unknown", "bad-url"

    last_err = ""
    for _ in range(RETRIES):
        try:
            with _sem(parsed.netloc):
                response = requests.get(
                    target,
                    headers={"User-Agent": UA, "Accept": "text/html,*/*"},
                    timeout=TIMEOUT,
                    allow_redirects=True,
                    stream=True,
                )
                code = response.status_code
                body = ""
                if code == 200 and "html" in response.headers.get("Content-Type", "").lower():
                    body = response.raw.read(BODY_LIMIT, decode_content=True).decode(
                        response.encoding or "utf-8",
                        errors="ignore",
                    )
                response.close()

            if code in (404, 410):
                return url, "broken", str(code)
            if code >= 500 or code in (401, 403, 429):
                return url, "unknown", str(code)
            if code == 200 and body and is_soft_404(body):
                return url, "broken", "200-soft404"
            if 200 <= code < 400:
                return url, "alive", str(code)
            return url, "unknown", str(code)
        except Exception as exc:  # noqa: BLE001
            last_err = type(exc).__name__
    return url, "unknown", last_err or "error"


def _fetch_urls(client, days: int | None) -> list[str]:
    if days is None:
        query = """
            SELECT DISTINCT url
            FROM ad_analytics.yandex_direct_404_errors
            WHERE ifNull(url, '') != ''
        """
    else:
        query = f"""
            SELECT DISTINCT url
            FROM ad_analytics.yandex_direct_404_errors
            WHERE ifNull(url, '') != ''
              AND ifNull(visit_date, toDate('1970-01-01')) >= today() - {int(days)}
        """
    return [row[0] for row in client.query(query, settings=SAFE_QUERY_SETTINGS).result_rows]


def _delete_alive(client, urls: list[str]) -> int:
    deleted = 0
    for idx in range(0, len(urls), DELETE_BATCH_SIZE):
        batch = urls[idx : idx + DELETE_BATCH_SIZE]
        before = count_rows(client, "ad_analytics.yandex_direct_404_errors")
        client.command(
            """
            DELETE FROM ad_analytics.yandex_direct_404_errors
            WHERE url IN {urls:Array(String)}
            """,
            parameters={"urls": batch},
            settings={**SAFE_QUERY_SETTINGS, "mutations_sync": 1},
        )
        after = count_rows(client, "ad_analytics.yandex_direct_404_errors")
        deleted += max(0, before - after)
    return deleted


def run(conn=None, run_id: str | None = None, days: int | None = DEFAULT_DAYS, dry_run: bool = False) -> dict:  # noqa: ARG001
    client = get_client()
    if not table_exists(client, "ad_analytics", "yandex_direct_404_errors"):
        return {"rows": 0, "details": "yandex_direct_404_errors отсутствует"}

    rows_before = count_rows(client, "ad_analytics.yandex_direct_404_errors")
    urls = _fetch_urls(client, days)
    if not urls:
        return {"rows": rows_before, "details": f"recheck_404: urls=0, rows={rows_before:,}"}

    alive: list[tuple[str, str]] = []
    broken: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for url, verdict, detail in pool.map(check_url, urls):
            {"alive": alive, "broken": broken, "unknown": unknown}[verdict].append((url, detail))

    deleted = 0
    if alive and not dry_run:
        deleted = _delete_alive(client, [url for url, _ in alive])
    rows_after = count_rows(client, "ad_analytics.yandex_direct_404_errors")
    details = (
        f"recheck_404 urls={len(urls):,}, alive={len(alive):,}, broken={len(broken):,}, "
        f"unknown={len(unknown):,}, deleted={deleted:,}, rows={rows_before:,}->{rows_after:,}"
    )
    logger.info(details)
    return {"rows": rows_after, "details": details}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перепроверка 404-URL реальным HTTP-кодом")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    result = run(days=None if args.all else args.days, dry_run=args.dry_run)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
