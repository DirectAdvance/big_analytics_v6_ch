"""Правила выбора кодека сжатия (config/ch_utils.apply_storage_codecs).

Кодеки замерены на живых партициях, см. OPTIMIZATION_PLAN.md фаза 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_utils import _codec_for


def test_metrics_get_zstd_ids_get_t64_and_hash_keys_stay_untouched():
    assert _codec_for("cost", "Decimal(18, 6)") == "ZSTD(3)"
    assert _codec_for("total_cost", "Nullable(Decimal(18, 9))") == "ZSTD(3)"
    assert _codec_for("CampaignId", "Int64") == "T64, ZSTD(3)"
    assert _codec_for("criterion_id", "Nullable(Int64)") == "T64, ZSTD(3)"
    assert _codec_for("key3", "String") == "ZSTD(3)"
    # PBI-проекции держат метрики во Float64: замерено ZSTD(3) −15.7%, Gorilla — хуже, чем без кодека
    assert _codec_for("total_cost", "Float64") == "ZSTD(3)"

    # хэш-ключи: T64 на случайном UInt64 только мешает
    assert _codec_for("site_key", "UInt64") is None
    # «id» в конце слова — не идентификатор
    assert _codec_for("valid", "Int64") is None
    assert _codec_for("uid", "Int64") is None
    assert _codec_for("placement_feed_key_hash", "UInt64") is None
    # уже словарные и не-сжимаемые типы не трогаем
    assert _codec_for("Device", "LowCardinality(Nullable(String))") is None
    assert _codec_for("Date", "Date") is None
