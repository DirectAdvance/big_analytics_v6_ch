from pathlib import Path

import cron_run


def test_main_skips_powerbi_refresh_when_flag_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("BA6_POWERBI_REFRESH", raising=False)
    monkeypatch.setattr(cron_run, "LOG_DIR", tmp_path)
    monkeypatch.setattr(cron_run, "rotate_logs", lambda: None)

    def fake_run_pipeline(log_path):
        log_path.write_text("10:00:00 [INFO] verify_big_analytics: PASS\n", encoding="utf-8")
        return 0

    refresh_calls = []
    monkeypatch.setattr(cron_run, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(cron_run, "run_powerbi_refresh", lambda log_path: refresh_calls.append(log_path) or 1)
    monkeypatch.setattr(cron_run, "send_html", lambda *a, **kw: True)

    rc = cron_run.main()

    assert refresh_calls == []
    assert rc == 0


def test_build_message_reports_raw_delta_final_checks_golden_and_step_times(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_run, "LOG_DIR", Path(tmp_path))
    previous = tmp_path / "cron_20260820_100000.log"
    current = tmp_path / "cron_20260821_100000.log"
    previous.write_text(
        "\n".join([
            "10:00:00 INFO raw_yandex=100",
            "10:00:00 INFO raw_leads=50",
            "10:00:00 INFO raw_calls=7",
        ]),
        encoding="utf-8",
    )
    current.write_text(
        "\n".join([
            "10:00:00 INFO run_id=abc123",
            "10:00:01 INFO Шаг 0: step0_sync_local.step0",
            "10:00:02 INFO Шаг 0 OK за 1.2 сек: rows=1",
            "10:00:03 INFO Шаг 1: step1_load_raw.step1",
            "10:01:05 INFO Шаг 1 OK за 62.4 сек: rows=2",
            "10:01:06 INFO raw_yandex=120",
            "10:01:06 INFO raw_leads=49",
            "10:01:06 INFO raw_calls=7",
            "10:01:06 INFO big_analytics_full=10",
            "10:01:06 INFO big_analytics_full_arrival=2",
            "10:01:06 INFO big_analytics_unified=12",
            "10:01:06 INFO fact_big_analytics=12",
            "10:01:06 INFO pbi_big_analytics_full=12",
            "10:01:06 INFO pbi_import_big_analytics_full=12",
            "10:01:06 INFO unified_count_mismatch=0",
            "10:01:06 INFO fact_unified_count_mismatch=0",
            "10:01:06 INFO full_before_2026=0",
            "10:01:06 INFO full_null_source=0",
            "10:01:06 INFO full_funnel_korr_lt_kval=0",
            "10:01:06 INFO full_funnel_kval_lt_priezd=0",
            "10:01:06 INFO full_funnel_priezd_lt_prodazhi=0",
            "10:01:06 INFO kuderko_raw_coverage: present_any_day=67 present_pre_cutoff=67 total=67 (cutoff=2026-04-10)",
            "10:01:06 INFO golden_kuderko cost=25422774.00 delta=+0.00 sales=57 floor=54",
            "10:01:06 [INFO] verify_big_analytics: PASS",
        ]),
        encoding="utf-8",
    )

    message = cron_run.build_message(0, current, 2)

    assert "raw_leads: 49 (-1 строк) <b>ПРОСАДКА</b>" in message
    assert "full+arrival=12, unified=12, fact=12 OK" in message
    assert "инварианты: OK" in message
    assert "raw Кудерко: 67/67, до cutoff 67/67" in message
    assert "1 step1_load_raw.step1" in message
    assert "1м02с" in message
    assert "verify: PASS" in message


def test_verify_pass_does_not_match_fail_line():
    assert cron_run.RE_VERIFY_PASS.search(
        "10:01:06 [ERROR] verify_big_analytics: FAIL: full_before_2026=3"
    ) is None
    assert cron_run.RE_VERIFY_PASS.search(
        "10:01:06 [INFO] verify_big_analytics: PASS"
    ) is not None
