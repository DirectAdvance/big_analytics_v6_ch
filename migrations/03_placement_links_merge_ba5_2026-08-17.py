#!/usr/bin/env python3
"""03_placement_links_merge_ba5_2026-08-17 — влить ссылки площадок из снимка БА5 в живой словарь v6.

Зачем. Модель Power BI читала ручную копию БА5 `raw_new_tp_placement_links` вместо живой
`yandex_direct_tp_placement_links` (шаг 139). Копия не обновляется, поэтому в отчёт не доехал фикс
двойной кодировки площадок. Прямое переключение отчёта на живую таблицу теряло данные: 299 площадок
имели ссылку в снимке БА5 и не имеют её в v6, ещё 48 площадок в v6 отсутствуют совсем (БА5 знал
более długую историю).

Что делает. Разово пересобирает живой словарь как объединение: ссылка v6 в приоритете, пустая
ссылка добирается из снимка БА5, площадки, которых v6 не знает, добавляются целиком. Дальше
удерживать их будет сам шаг 139: `_load_existing_links` читает эту же таблицу как кэш и переносит
её содержимое в каждую следующую пересборку.

После этого `raw_new_tp_placement_links` больше не нужна ни отчёту, ни пайплайну.

Запуск (идемпотентно, можно повторять):
    .venv/bin/python3 migrations/03_placement_links_merge_ba5_2026-08-17.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from config.ch_db import get_client  # noqa: E402
from config.ch_utils import count_rows, swap_shadow, table_exists  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("placement_links_merge")

TARGET = "ad_analytics.yandex_direct_tp_placement_links"
BA5_SNAPSHOT = "ad_analytics.raw_new_tp_placement_links"

MERGE_SQL = f"""
SELECT
    placement,
    argMax(placement_link, priority) AS placement_link
FROM
(
    SELECT placement, placement_link, 2 AS priority
    FROM {TARGET}
    WHERE ifNull(placement_link, '') != ''
    UNION ALL
    SELECT placement, CAST(placement_link, 'Nullable(String)') AS placement_link, 1 AS priority
    FROM {BA5_SNAPSHOT}
    WHERE ifNull(placement_link, '') != ''
    UNION ALL
    -- площадки без ссылки ни там, ни там: остаются в словаре с NULL, чтобы шаг 139 продолжал
    -- искать по ним ссылку в Grid
    SELECT placement, CAST(NULL, 'Nullable(String)') AS placement_link, 0 AS priority
    FROM {TARGET}
)
GROUP BY placement
"""


def stats(client, relation: str) -> tuple[int, int]:
    rows = client.query(
        f"SELECT count(), countIf(ifNull(placement_link, '') != '') FROM {relation}"
    ).result_rows[0]
    return int(rows[0]), int(rows[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="показать план без записи")
    args = parser.parse_args(argv)

    client = get_client()
    if not table_exists(client, "ad_analytics", "raw_new_tp_placement_links"):
        log.info("снимок БА5 уже удалён — миграция не нужна")
        return 0

    before_rows, before_links = stats(client, TARGET)
    after_rows, after_links = stats(client, f"({MERGE_SQL})")
    log.info("до:    строк=%d со ссылкой=%d", before_rows, before_links)
    log.info("после: строк=%d со ссылкой=%d", after_rows, after_links)
    if after_links < before_links or after_rows < before_rows:
        log.error("объединение уменьшает словарь — прерываю")
        return 1
    if args.dry_run:
        return 0

    shadow = f"{TARGET}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"CREATE TABLE {shadow} ENGINE = MergeTree ORDER BY placement AS {MERGE_SQL}"
    )
    swap_shadow(client, TARGET, shadow)
    log.info("готово: %s строк в %s", count_rows(client, TARGET), TARGET)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
