"""step8_stats/pipeline_run_snapshot.py — снимок воронки прогона по (месяц × направление).

ЗАЧЕМ:
  Метрики воронки пересчитываются каждый прогон: Яндекс дофинализирует расход задним
  числом, CRM-статусы дозревают. Снимок копит историю, а вкладка «Дельта пайплайна»
  рабочего дашборда (`/api/pipeline-delta`) сравнивает два выбранных прогона.

  Это BA6-порт v5-модулей `pipeline_log_snapshot` (public.data_pipeline_log) и
  `funnel_drift_snapshot` (public.data_funnel_drift_log). Обе таблицы в BA6 не
  существуют — контракт BA6 читает ClickHouse, а не Postgres Victory.

ИСТОЧНИК:
  ad_analytics.fact_big_analytics + Dim_Salon. НЕ big_analytics_unified: та живёт
  как VIEW только после cleanup_wide_intermediates (шаг 148), а звезда персистентна
  и стоит один LEFT JOIN вместо двадцати.

  Ось — ТОЛЬКО заявочная (`атрибуция='По дате заявки'`). Визитная ось в тот же
  агрегат складываться не должна: в v5 это дало двойной счёт priezd/prodazhi
  (DELTA_AXIS_FIX_2026-07-10).

  Пиксель НЕ отфильтрован: он лежит отдельным `направление='Пиксель'`, и тоггл
  «Исключить Пиксель» на дашборде вычитает его из строки месяца. Фильтровать здесь —
  значит потерять компонент.

ИДЕМПОТЕНТНОСТЬ:
  ReplacingMergeTree(recorded_at) + ORDER BY (run_id, month, направление): повторный
  вызов с тем же run_id перезапишет строки. Читать только через VIEW
  `pipeline_run_snapshot_v` (FINAL) — правило секции 2 migrations/01_init_schema.sql.

ДРОБНОСТЬ:
  cost и счётчики воронки — Decimal, НЕ Int: пиксельная атрибуция даёт дробные
  priezd/prodazhi, округление построчно теряет копейки и десятые доли визита.

TELEGRAM-АЛЕРТ v5 (`funnel_drift_snapshot`) сюда не переносится — таблица снимков
это требование, алерт был отдельной надстройкой над Postgres-вьюхой.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS

logger = logging.getLogger("pipeline.run_snapshot")

TABLE = "ad_analytics.pipeline_run_snapshot"
VIEW = "ad_analytics.pipeline_run_snapshot_v"

DDL_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE}
(
    run_id        String,
    recorded_at   DateTime DEFAULT now(),
    month         Date,
    `направление` LowCardinality(String),
    cost          Decimal(38, 6),
    obrashenia    Decimal(38, 6),
    zayavki       Decimal(38, 6),
    kval          Decimal(38, 6),
    priezd        Decimal(38, 6),
    prodazhi      Decimal(38, 6)
)
ENGINE = ReplacingMergeTree(recorded_at)
ORDER BY (run_id, month, `направление`)
"""

DDL_VIEW = f"""
CREATE VIEW IF NOT EXISTS {VIEW} AS
SELECT run_id, recorded_at, month, `направление`,
       cost, obrashenia, zayavki, kval, priezd, prodazhi
FROM {TABLE} FINAL
"""

INSERT_SQL = f"""
INSERT INTO {TABLE}
    (run_id, month, `направление`, cost, obrashenia, zayavki, kval, priezd, prodazhi)
SELECT
    {{run_id:String}}                                              AS run_id,
    toStartOfMonth(f.`Date`)                                       AS month,
    coalesce(nullIf(trim(s.`направление`), ''), '(неизвестно)')    AS `направление`,
    sum(f.total_cost)                                              AS cost,
    sum(f.kol_vo_zayavok)                                          AS obrashenia,
    sum(f.korr)                                                    AS zayavki,
    sum(f.kval)                                                    AS kval,
    sum(f.priezd)                                                  AS priezd,
    sum(f.prodazhi)                                                AS prodazhi
FROM ad_analytics.fact_big_analytics AS f
LEFT JOIN ad_analytics.Dim_Salon AS s ON s.salon_key = f.salon_key
WHERE f.`атрибуция` = 'По дате заявки'
GROUP BY month, `направление`
"""


def ensure_tables(client) -> None:
    """DDL снимка. Отдельно от run(): дашборд должен уметь читать VIEW до первого прогона."""
    client.command(DDL_TABLE, settings=SAFE_QUERY_SETTINGS)
    client.command(DDL_VIEW, settings=SAFE_QUERY_SETTINGS)


def run(conn=None, run_id: str | None = None, **kwargs) -> dict:  # noqa: ARG001
    """Записать снимок воронки прогона. Возвращает {'rows', 'details'}."""
    rid = (run_id or "").strip()
    if not rid:
        raise ValueError("pipeline_run_snapshot.run: run_id обязателен")
    client = get_client()
    t0 = time.perf_counter()
    ensure_tables(client)
    client.command(INSERT_SQL, parameters={"run_id": rid}, settings=SAFE_QUERY_SETTINGS)
    rows = _count_run(client, rid)
    logger.info(
        "pipeline_run_snapshot: run_id=%s строк=%d за %.1f сек",
        rid, rows, time.perf_counter() - t0,
    )
    return {"rows": rows, "details": f"{TABLE} run_id={rid} rows={rows}"}


def _count_run(client, run_id: str) -> int:
    return int(
        client.query(
            f"SELECT count() FROM {VIEW} WHERE run_id = {{run_id:String}}",
            parameters={"run_id": run_id},
            settings=SAFE_QUERY_SETTINGS,
        ).result_rows[0][0]
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run(run_id="manual-test"))
