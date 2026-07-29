"""
korrektirovki/run.py
====================
Запуск корректировок ставок вручную, вне пайплайна.

Запуск (на сервере из папки big_analytics_v5/):
    ~/venv/bin/python3 korrektirovki/run.py

Запуск через ssh с локального:
    ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 korrektirovki/run.py"
"""

import logging
import os
import sys

_here  = os.path.dirname(os.path.abspath(__file__))
_night = os.path.dirname(_here)   # step_cron_night/
_root  = os.path.dirname(_night)  # big_analytics_v5/ (BASE_DIR)
sys.path.insert(0, _night)  # чтобы from korrektirovki.korrektirovki работало
sys.path.insert(0, _root)   # чтобы from config.* работало

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)

from korrektirovki.korrektirovki import main

if __name__ == '__main__':
    main()
