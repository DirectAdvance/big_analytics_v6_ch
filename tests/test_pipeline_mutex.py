import pipeline
from step_cron_night import pipeline_night


def test_main_pipeline_exits_before_db_when_ba6_lock_is_busy(monkeypatch):
    def busy(*args, **kwargs):
        raise pipeline.pipeline_mutex.PipelineBusy("ba6_night pid=123")

    monkeypatch.setattr(pipeline.pipeline_mutex, "acquire", busy)
    monkeypatch.setattr(pipeline, "get_client", lambda: (_ for _ in ()).throw(AssertionError("db touched")))

    assert pipeline.main(["--only-step", "0"]) == pipeline.BUSY_EXIT_CODE


def test_night_pipeline_skips_before_db_when_ba6_lock_is_busy(monkeypatch):
    def busy(*args, **kwargs):
        raise pipeline_night.pipeline_mutex.PipelineBusy("ba6_pipeline pid=456")

    monkeypatch.setattr(pipeline_night.pipeline_mutex, "acquire", busy)
    monkeypatch.setattr(pipeline_night, "get_client", lambda: (_ for _ in ()).throw(AssertionError("db touched")))

    assert pipeline_night.main(["--no-tg"]) == 0
