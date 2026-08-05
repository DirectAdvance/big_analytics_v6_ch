#!/usr/bin/env python3
"""02_status_mapping_ab_2026-08-05 — правки справочника `raw_data.crm_status_mapping`.

Справочник статусов живёт ДАННЫМИ в ClickHouse, а не файлом в репозитории, поэтому
обе правки оформлены здесь — воспроизводимо и обратимо одной операцией.

  python3 migrations/02_status_mapping_ab_2026-08-05.py --check      # что сейчас в БД
  python3 migrations/02_status_mapping_ab_2026-08-05.py --apply      # применить A и B
  python3 migrations/02_status_mapping_ab_2026-08-05.py --rollback   # вернуть как было
  ... --apply --only=A|B  /  --rollback --only=A|B                   # по одной правке

────────────────────────────────────────────────────────────────────────────────
ПРАВКА A — маркер MARCAR_GSHEET_STATUS_2026-08-05
  Добавляет 3 строки `marcar`: «Продажа»→sale, «Дошел в КО»→visit, «Одобрение»→visit.
  Зачем: `step1_load_raw/step1.py` (тот же маркер) проставляет эти статусы лидам
  Маркара по `raw_data.gsheet_priezdi_marcar` — порт v5 `_patch_marcar_statuses()`.
  Без строк в справочнике патч статусов не даёт воронки: в CH-маппинге нет
  general-ветки (`crm_name='default'`), которая в v5 покрывала все CRM сразу.
  Откат: DELETE ровно этих 3 троек (строка `marcar`/«Приехал»/visit — не наша, не трогаем).

ПРАВКА B — маркер PLEX_OTKAZ_QUALIFIED_2026-08-05
  Переводит 47 строк `plex` / «Отказ клиента» из категории `correct` в `qualified`.
  Зачем: паритет с прод-контуром v5, где «Отказ клиента» лежит в общей ветке
  `local_crm_statuses` как `qualified`, а у `plex` нет CRM-override в этой категории.
  Решение Семёна 2026-08-05. korr при этом НЕ меняется: категория `qualified`
  входит в список категорий метрики korr (step3.py::_metric_expr).
  Откат: обратный UPDATE `qualified`→`correct` по тем же 47 строкам.
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client

TABLE = "raw_data.crm_status_mapping"

MARCAR_ROWS = [
    ("marcar", "", "Продажа", "", "sale"),
    ("marcar", "", "Дошел в КО", "", "visit"),
    ("marcar", "", "Одобрение", "", "visit"),
]
MARCAR_STATUSES_SQL = ", ".join(f"'{row[2]}'" for row in MARCAR_ROWS)

A_APPLY = f"""
INSERT INTO {TABLE} (crm, salon, status, reason, category)
SELECT crm, salon, status, reason, category
FROM
(
    {" UNION ALL ".join(
        f"SELECT '{crm}' AS crm, '{salon}' AS salon, '{status}' AS status, "
        f"'{reason}' AS reason, '{category}' AS category"
        for crm, salon, status, reason, category in MARCAR_ROWS
    )}
) AS src
WHERE (src.crm, src.salon, src.status, src.reason) NOT IN
(
    SELECT crm, salon, status, reason FROM {TABLE}
)
"""

A_ROLLBACK = f"""
ALTER TABLE {TABLE}
DELETE WHERE crm = 'marcar' AND salon = '' AND reason = '' AND status IN ({MARCAR_STATUSES_SQL})
"""

B_WHERE = "crm = 'plex' AND status = 'Отказ клиента'"
B_APPLY = f"ALTER TABLE {TABLE} UPDATE category = 'qualified' WHERE {B_WHERE} AND category = 'correct'"
B_ROLLBACK = f"ALTER TABLE {TABLE} UPDATE category = 'correct' WHERE {B_WHERE} AND category = 'qualified'"

CHECK_SQL = f"""
SELECT 'A: marcar sale/visit (ожидается 3 после apply, 0 до)' AS what,
       countIf(crm = 'marcar' AND salon = '' AND reason = '' AND status IN ({MARCAR_STATUSES_SQL})) AS n
FROM {TABLE}
UNION ALL
SELECT 'B: plex «Отказ клиента» category=qualified (ожидается 47 после apply, 0 до)', countIf({B_WHERE} AND category = 'qualified') FROM {TABLE}
UNION ALL
SELECT 'B: plex «Отказ клиента» category=correct (ожидается 0 после apply, 47 до)', countIf({B_WHERE} AND category = 'correct') FROM {TABLE}
UNION ALL
SELECT 'дубли троек (crm,salon,status,reason) — всегда 0', (SELECT count() FROM (SELECT crm, salon, status, reason FROM {TABLE} GROUP BY crm, salon, status, reason HAVING count() > 1)) FROM {TABLE} LIMIT 1
UNION ALL
SELECT 'всего строк справочника', count() FROM {TABLE}
"""

# mutations_sync=2 — не возвращать управление, пока мутация реально не применена.
MUTATION_SETTINGS = {"mutations_sync": 2}


def _check(client) -> None:
    for what, n in client.query(CHECK_SQL).result_rows:
        print(f"  {what}: {n}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true")
    group.add_argument("--rollback", action="store_true")
    group.add_argument("--check", action="store_true")
    parser.add_argument("--only", choices=("A", "B"), help="применить/откатить только одну правку")
    args = parser.parse_args()

    client = get_client()
    print("ДО:")
    _check(client)

    if args.check:
        return 0

    steps = []
    if args.apply:
        if args.only in (None, "A"):
            steps.append(("A apply (MARCAR_GSHEET_STATUS_2026-08-05)", A_APPLY, None))
        if args.only in (None, "B"):
            steps.append(("B apply (PLEX_OTKAZ_QUALIFIED_2026-08-05)", B_APPLY, MUTATION_SETTINGS))
    else:
        if args.only in (None, "B"):
            steps.append(("B rollback (PLEX_OTKAZ_QUALIFIED_2026-08-05)", B_ROLLBACK, MUTATION_SETTINGS))
        if args.only in (None, "A"):
            steps.append(("A rollback (MARCAR_GSHEET_STATUS_2026-08-05)", A_ROLLBACK, MUTATION_SETTINGS))

    for label, sql, settings in steps:
        print(f"\n>>> {label}")
        client.command(sql, settings=settings)

    print("\nПОСЛЕ:")
    _check(client)
    print(
        "\n⚠️ Справочник — вход шага 3. Чтобы изменения доехали до витрины, нужен прогон "
        "pipeline.py (step1 → step3 → corrections → step5/6 → build_star)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
