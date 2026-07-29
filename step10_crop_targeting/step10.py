"""
step10_crop_targeting/step10.py — шаг 10: посевы Telega.in → big_analytics
"""
import importlib.util
from pathlib import Path

_BASE = Path(__file__).resolve().parent


def _load(filename: str):
    path = _BASE / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(conn=None, run_id=None, **kwargs):
    _load('load_telega_in_orders.py').main()
    _load('load_crop_to_big_analytics.py').main()
    return {}
