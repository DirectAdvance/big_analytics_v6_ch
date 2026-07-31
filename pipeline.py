#!/usr/bin/env python3
"""ClickHouse orchestrator for big_analytics_v6_ch."""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib
import logging
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.ch_db import get_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")

STEPS = [
    (0, "step0_sync_local.step0", "step0"),
    (1, "step1_load_raw.step1", "step1"),
    (2, "step2_indexes.step2", "step2"),
    (4, "step4_campaign_status.step4", "step4"),
    (3, "step3_build_sources.step3", "step3"),
    (31, "corrections", "corrections"),
    (5, "step5_build_pixel.build_pixel", "step5"),
    (6, "step6_build_full.step6", "step6"),
    (7, "step7_finalize.step7", "step7"),
    (9, "step9_direct_history.step9", "step9"),
    (10, "step10_crop_targeting.step10", "step10"),
    (11, "step11_pixel_score.step11", "step11"),
    (12, "step12_proverka_big_analytics.step12", "step12"),
    (13, "step13_arrival.step13", "step13"),
    (131, "step13_arrival.build_unified", "build_unified"),
    (141, "region_spend.build_region_spend", "region_spend"),
    (142, "adformat_spend.build_adformat_spend", "adformat_spend"),
    (143, "criterion_spend.build_criterion_spend", "criterion_spend"),
    (1431, "region_spend.build_region_zayavki", "region_zayavki"),
    (1432, "criterion_spend.build_criterion_zayavki", "criterion_zayavki"),
    (144, "direct_feed_funnel.build", "direct_feed_funnel"),
    (145, "star_refactor.build_star", "build_star"),
    (1451, "star_refactor.build_star_extensions", "build_star_extensions"),
    (146, "star_refactor.build_pbi_compat", "build_pbi_compat"),
    (14, "step14_minus_snapshot.step14", "step14"),
    (8, "step8_stats.step8", "step8"),
    (900, "data_check.verify_big_analytics", "verify"),
]

MAINTENANCE_STEPS = {2, 5, 7, 10}
HEAVY_PBI_STEPS = {141, 142, 143, 1431, 1432, 144, 145, 1451, 146}
PARALLEL_BACKGROUND_STEPS = {14}
STEP14_START_AT = {13, 131, 141, 142, 143, 1431, 1432, 144, 145, 1451, 146, 14, 8, 900}


def ensure_quality_log(client) -> None:
    client.command(
        """
        CREATE TABLE IF NOT EXISTS ad_analytics.data_quality_log
        (
            run_id String,
            run_at DateTime DEFAULT now(),
            step String,
            status LowCardinality(String),
            rows_affected Nullable(Int64),
            duration_sec Nullable(Float64),
            details Nullable(String)
        )
        ENGINE = MergeTree
        ORDER BY (run_at, run_id, step)
        """
    )


def log_step(client, run_id: str, step: str, status: str, rows=None, duration=None, details=None) -> None:
    client.insert(
        "ad_analytics.data_quality_log",
        [[run_id, step, status, rows, duration, details]],
        column_names=["run_id", "step", "status", "rows_affected", "duration_sec", "details"],
    )


def run_step(
    client,
    run_id: str,
    step_num: int,
    module_path: str,
    label: str,
    verify_no_star: bool = False,
) -> bool:
    logger.info("━" * 60)
    logger.info("Шаг %s: %s", step_num, module_path)
    t0 = time.perf_counter()
    try:
        mod = importlib.import_module(module_path)
        if label == "verify":
            code = mod.run(full=True, no_star=verify_no_star)
            if code:
                raise RuntimeError(f"verify_big_analytics exit={code}")
            details = "PASS no_star" if verify_no_star else "PASS"
            result = {"rows": None, "details": details}
        elif hasattr(mod, "apply"):
            result = mod.apply(conn=None, run_id=run_id)
        else:
            result = mod.run(conn=None, run_id=run_id)
        elapsed = time.perf_counter() - t0
        rows = result.get("rows") if isinstance(result, dict) else None
        details = result.get("details") if isinstance(result, dict) else None
        log_step(client, run_id, label, "OK", rows, elapsed, details)
        logger.info("Шаг %s OK за %.1f сек: %s", step_num, elapsed, details)
        return True
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        log_step(client, run_id, label, "FAIL", None, elapsed, str(exc))
        logger.exception("Шаг %s FAIL за %.1f сек", step_num, elapsed)
        return False


def run_step_isolated(run_id: str, step_num: int, module_path: str, label: str, verify_no_star: bool = False) -> bool:
    client = get_client()
    ensure_quality_log(client)
    return run_step(client, run_id, step_num, module_path, label, verify_no_star=verify_no_star)


def selected_steps(
    from_step: int | None,
    only_step: int | None,
    include_maintenance: bool,
    skip_heavy_pbi: bool,
):
    if only_step is not None:
        return [item for item in STEPS if item[0] == only_step]
    skip_steps = set()
    if skip_heavy_pbi:
        skip_steps.update(HEAVY_PBI_STEPS)
    if from_step is not None:
        selected = []
        started = False
        for item in STEPS:
            if item[0] == from_step:
                started = True
            if started and item[0] not in skip_steps and (include_maintenance or item[0] not in MAINTENANCE_STEPS):
                selected.append(item)
        return selected
    if include_maintenance:
        return [item for item in STEPS if item[0] not in skip_steps]
    return [item for item in STEPS if item[0] not in MAINTENANCE_STEPS and item[0] not in skip_steps]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-step", type=int)
    parser.add_argument("--only-step", type=int)
    parser.add_argument("--include-maintenance", action="store_true")
    parser.add_argument(
        "--skip-heavy-pbi",
        action="store_true",
        default=os.getenv("PIPELINE_SKIP_HEAVY_PBI", "").lower() in {"1", "true", "yes"},
        help="Skip spend/feed/star/PBI compatibility rebuilds for low-memory debug runs.",
    )
    parser.add_argument(
        "--no-parallel-safe",
        action="store_true",
        default=os.getenv("PIPELINE_PARALLEL_SAFE", "").lower() in {"0", "false", "no"},
        help="Disable safe background steps such as Direct minus snapshot.",
    )
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:12]
    client = get_client()
    ensure_quality_log(client)
    logger.info("big_analytics_v6_ch ClickHouse pipeline start run_id=%s", run_id)

    failed = False
    if args.skip_heavy_pbi:
        logger.info("skip-heavy-pbi enabled: пропускаю шаги %s", sorted(HEAVY_PBI_STEPS))
    parallel_safe = not args.no_parallel_safe and args.only_step is None
    if parallel_safe:
        logger.info("parallel-safe enabled: step14 can run in background")

    steps = selected_steps(
        args.from_step,
        args.only_step,
        args.include_maintenance,
        args.skip_heavy_pbi,
    )
    step14_item = next((item for item in steps if item[0] == 14), None)
    step14_future: concurrent.futures.Future | None = None
    executor: concurrent.futures.ThreadPoolExecutor | None = None

    def start_step14_background() -> None:
        nonlocal executor, step14_future
        if not parallel_safe or step14_item is None or step14_future is not None:
            return
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline-step14")
        step_num, module_path, label = step14_item
        logger.info("parallel-safe: запускаю step14 в фоне")
        step14_future = executor.submit(run_step_isolated, run_id, step_num, module_path, label, False)

    def wait_step14_background() -> bool:
        nonlocal executor, step14_future
        if step14_future is None:
            return True
        logger.info("parallel-safe: жду завершения step14")
        ok = bool(step14_future.result())
        if executor is not None:
            executor.shutdown(wait=True)
        executor = None
        step14_future = None
        return ok

    for step_num, module_path, label in steps:
        if parallel_safe and step_num in STEP14_START_AT:
            start_step14_background()
        if parallel_safe and step_num in {8, 900} and step14_item is not None:
            if not wait_step14_background():
                failed = True
                break
        if parallel_safe and step_num in PARALLEL_BACKGROUND_STEPS:
            continue
        verify_no_star = bool(args.skip_heavy_pbi and label == "verify")
        ok = run_step(client, run_id, step_num, module_path, label, verify_no_star=verify_no_star)
        if not ok:
            failed = True
            break
    if not failed and step14_future is not None:
        failed = not wait_step14_background()
    logger.info("big_analytics_v6_ch pipeline %s", "FAIL" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
