from pathlib import Path

import pytest

import cron_run
import refresh_powerbi


class Response:
    def __init__(self, payload=None, status_code=200, headers=None, text=""):
        self.payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_refresh_rejects_ba5_postgresql_dataset(monkeypatch):
    monkeypatch.setattr(
        refresh_powerbi.requests,
        "get",
        lambda *args, **kwargs: Response({"value": [{"datasourceType": "PostgreSql"}]}),
    )

    with pytest.raises(refresh_powerbi.PowerBIRefreshError, match="BA5/PostgreSQL"):
        refresh_powerbi._assert_ba6_datasource("https://example.test", {})


def test_refresh_runs_selective_ba6_refresh_and_waits_for_completion(monkeypatch):
    config = {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "secret",
        "workspace_id": "workspace",
        "dataset_id": "dataset",
    }
    responses = iter([
        Response({"value": [{"datasourceType": "Extension"}]}),
        Response({"value": [{"status": "Completed"}]}),
        Response({"status": "Completed"}),
    ])
    posts = []

    monkeypatch.setattr(refresh_powerbi, "load_powerbi", lambda: config)
    monkeypatch.setattr(refresh_powerbi, "_token", lambda _: "token")
    monkeypatch.setattr(refresh_powerbi, "_plan_refresh_tables", lambda dataset_id: (refresh_powerbi._ALL_TABLES, []))
    monkeypatch.setattr(refresh_powerbi, "_record_loaded_fingerprints", lambda *args: None)
    monkeypatch.setattr(refresh_powerbi.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        refresh_powerbi.requests,
        "post",
        lambda *args, **kwargs: posts.append((args, kwargs))
        or Response(status_code=202, headers={"Location": "https://status.test/1"}),
    )

    assert refresh_powerbi.refresh_powerbi() >= 0
    assert posts[0][1]["json"] == {
        "type": "full",
        "commitMode": "transactional",
        "maxParallelism": 1,
        "retryCount": 0,
        "notifyOption": "NoNotification",
        "objects": [{"table": table} for table in refresh_powerbi._ALL_TABLES],
    }


def test_refresh_contract_includes_city_tier_and_direct_ads_texts():
    assert "Dim_City_Tier" in refresh_powerbi._ALL_TABLES
    assert "yandex_direct_ads_texts" in refresh_powerbi._ALL_TABLES


def test_refresh_does_not_report_success_while_previous_refresh_runs(monkeypatch):
    responses = iter([
        Response({"value": [{"datasourceType": "Extension"}]}),
        Response({"value": [{"status": "Unknown"}]}),
    ])
    monkeypatch.setattr(refresh_powerbi, "load_powerbi", lambda: {
        "workspace_id": "workspace", "dataset_id": "dataset",
    })
    monkeypatch.setattr(refresh_powerbi, "_token", lambda _: "token")
    monkeypatch.setattr(refresh_powerbi, "_plan_refresh_tables", lambda dataset_id: (refresh_powerbi._ALL_TABLES, []))
    monkeypatch.setattr(refresh_powerbi.requests, "get", lambda *args, **kwargs: next(responses))

    with pytest.raises(refresh_powerbi.PowerBIRefreshError, match="ещё выполняется"):
        refresh_powerbi.refresh_powerbi()


def test_refresh_retries_transient_polling_timeout(monkeypatch):
    config = {"workspace_id": "workspace", "dataset_id": "dataset"}
    calls = 0

    def get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Response({"value": [{"datasourceType": "Extension"}]})
        if calls == 2:
            return Response({"value": [{"status": "Completed"}]})
        if calls == 3:
            raise refresh_powerbi.requests.Timeout("temporary")
        return Response({"status": "Completed"})

    monkeypatch.setattr(refresh_powerbi, "load_powerbi", lambda: config)
    monkeypatch.setattr(refresh_powerbi, "_token", lambda _: "token")
    monkeypatch.setattr(refresh_powerbi, "_plan_refresh_tables", lambda dataset_id: (refresh_powerbi._ALL_TABLES, []))
    monkeypatch.setattr(refresh_powerbi, "_record_loaded_fingerprints", lambda *args: None)
    monkeypatch.setattr(refresh_powerbi.requests, "get", get)
    monkeypatch.setattr(
        refresh_powerbi.requests,
        "post",
        lambda *args, **kwargs: Response(status_code=202, headers={"Location": "status"}),
    )
    monkeypatch.setattr(refresh_powerbi.time, "sleep", lambda _: None)

    assert refresh_powerbi.refresh_powerbi() >= 0
    assert calls == 4


def test_refresh_logs_failed_request_details(monkeypatch, caplog):
    responses = iter([
        Response({"value": [{"datasourceType": "Extension"}]}),
        Response({"value": [{"status": "Completed"}]}),
        Response({
            "status": "Failed",
            "requestId": "request-42",
            "serviceExceptionJson": "diagnostic detail",
        }),
    ])
    monkeypatch.setattr(refresh_powerbi, "load_powerbi", lambda: {
        "workspace_id": "workspace", "dataset_id": "dataset",
    })
    monkeypatch.setattr(refresh_powerbi, "_token", lambda _: "token")
    monkeypatch.setattr(refresh_powerbi, "_plan_refresh_tables", lambda dataset_id: (refresh_powerbi._ALL_TABLES, []))
    monkeypatch.setattr(refresh_powerbi.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        refresh_powerbi.requests,
        "post",
        lambda *args, **kwargs: Response(status_code=202, headers={"Location": "status"}),
    )

    with pytest.raises(refresh_powerbi.PowerBIRefreshError, match="request-42"):
        refresh_powerbi.refresh_powerbi()

    assert "diagnostic detail" in caplog.text


def test_refresh_skips_powerbi_post_when_fingerprints_are_unchanged(monkeypatch):
    monkeypatch.setattr(refresh_powerbi, "load_powerbi", lambda: {
        "workspace_id": "workspace", "dataset_id": "dataset",
    })
    monkeypatch.setattr(refresh_powerbi, "_plan_refresh_tables", lambda dataset_id: ([], []))
    monkeypatch.setattr(refresh_powerbi, "_token", lambda _: pytest.fail("token should not be requested"))
    monkeypatch.setattr(refresh_powerbi.requests, "post", lambda *args, **kwargs: pytest.fail("POST should not run"))

    assert refresh_powerbi.refresh_powerbi() == -1


def test_refresh_posts_only_changed_tables(monkeypatch):
    config = {"workspace_id": "workspace", "dataset_id": "dataset"}
    responses = iter([
        Response({"value": [{"datasourceType": "Extension"}]}),
        Response({"value": [{"status": "Completed"}]}),
        Response({"status": "Completed"}),
    ])
    posts = []
    changed = ["big_analytics_full", "Dim_Date"]
    fingerprints = [refresh_powerbi.TableFingerprint("big_analytics_full", "bi_pbi_big_analytics_full", 10, "fp")]

    monkeypatch.setattr(refresh_powerbi, "load_powerbi", lambda: config)
    monkeypatch.setattr(refresh_powerbi, "_token", lambda _: "token")
    monkeypatch.setattr(refresh_powerbi, "_plan_refresh_tables", lambda dataset_id: (changed, fingerprints))
    monkeypatch.setattr(refresh_powerbi, "_record_loaded_fingerprints", lambda *args: None)
    monkeypatch.setattr(refresh_powerbi.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        refresh_powerbi.requests,
        "post",
        lambda *args, **kwargs: posts.append((args, kwargs))
        or Response(status_code=202, headers={"Location": "https://status.test/1"}),
    )

    assert refresh_powerbi.refresh_powerbi() >= 0
    assert posts[0][1]["json"]["objects"] == [{"table": "big_analytics_full"}, {"table": "Dim_Date"}]


def test_force_refresh_posts_all_tables_and_records_baseline(monkeypatch):
    config = {"workspace_id": "workspace", "dataset_id": "dataset"}
    responses = iter([
        Response({"value": [{"datasourceType": "Extension"}]}),
        Response({"value": [{"status": "Completed"}]}),
        Response({"status": "Completed"}),
    ])
    fingerprint = refresh_powerbi.TableFingerprint("Dim_Date", "bi_Dim_Date", 238, "fp")
    recorded = []

    monkeypatch.setattr(refresh_powerbi, "load_powerbi", lambda: config)
    monkeypatch.setattr(refresh_powerbi, "_token", lambda _: "token")
    monkeypatch.setattr(refresh_powerbi, "_plan_refresh_tables", lambda dataset_id: pytest.fail("planner should not run"))
    monkeypatch.setattr(refresh_powerbi, "_current_fingerprints", lambda client: [fingerprint])
    monkeypatch.setattr(refresh_powerbi, "get_client", lambda: object())
    monkeypatch.setattr(refresh_powerbi, "_record_loaded_fingerprints", lambda *args: recorded.append(args))
    monkeypatch.setattr(refresh_powerbi.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        refresh_powerbi.requests,
        "post",
        lambda *args, **kwargs: Response(status_code=202, headers={"Location": "https://status.test/1"}),
    )

    assert refresh_powerbi.refresh_powerbi(force=True) >= 0
    assert recorded[0][1] == [fingerprint]


def test_cron_refreshes_powerbi_only_after_successful_pipeline(tmp_path, monkeypatch):
    calls = []
    # Крон Victory задаёт `BA6_POWERBI_REFRESH=1` в самой строке расписания; без него
    # `cron_run.main()` намеренно не трогает Power BI (docstring `cron_run.py`).
    monkeypatch.setenv("BA6_POWERBI_REFRESH", "1")
    monkeypatch.setattr(cron_run, "LOG_DIR", Path(tmp_path))
    monkeypatch.setattr(cron_run, "rotate_logs", lambda: None)
    monkeypatch.setattr(cron_run, "run_pipeline", lambda _: calls.append("pipeline") or 0)
    monkeypatch.setattr(cron_run, "run_powerbi_refresh", lambda _: calls.append("powerbi") or 0)
    monkeypatch.setattr(cron_run, "build_message", lambda *args, **kwargs: "ok")
    monkeypatch.setattr(cron_run, "send_html", lambda *args, **kwargs: True)

    assert cron_run.main() == 0
    assert calls == ["pipeline", "powerbi"]

    calls.clear()
    monkeypatch.setattr(cron_run, "run_pipeline", lambda _: calls.append("pipeline") or 1)
    assert cron_run.main() == 1
    assert calls == ["pipeline"]

    calls.clear()
    monkeypatch.setattr(cron_run, "run_pipeline", lambda _: calls.append("pipeline") or 0)
    monkeypatch.delenv("BA6_POWERBI_REFRESH")
    assert cron_run.main() == 0
    assert calls == ["pipeline"]
