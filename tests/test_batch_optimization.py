import inspect

from step10_crop_targeting import step10


def test_crop_overlay_full_uses_global_pipeline_batches():
    source = inspect.getsource(step10._overlay_full)

    assert "day_ranges(DATE_FROM)" in source
    assert "range_batches(DATE_FROM, days=1)" not in source
