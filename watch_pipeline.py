#!/usr/bin/env python3
"""
watch_pipeline.py — хвостит /tmp/fast_pipeline.log, детектирует завершение
шагов fast_pipeline.py и шлёт Telegram-уведомление на каждый.

Маркеры (реальные строки из логов fast_pipeline):
  Loop-шаги (run_step): "Шаг N завершён за X.X сек"
  step4 кэш:            "step4 (кэш): ... за X.X сек"
  step11:               "step11_pixel_score завершён за X.X сек"
  step12:               "step12: N строк за X.X сек"
  step13_rebuild:       "step13_arrival (пересборка после step11): ... за X.X сек"
  build_unified:        "build_unified: ... за X.X сек"
  build_region_spend:   "build_region_spend: ... за X.X сек"
  build_adformat_spend: "build_adformat_spend: ... за X.X сек"
  build_criterion_spend:"build_criterion_spend: ... за X.X сек"
  build_star:           "build_star: готово за X.X сек"
  verify:               "verify_big_analytics: завершён за X.X сек"
  golden:               "golden_kuderko: ..."
  DONE OK:              "УСПЕШНО завершено за"
  DONE ERR:             "ЗАВЕРШЕНО С ОШИБКОЙ"

Запуск: nohup python3 watch_pipeline.py > /tmp/watch_pipeline.log 2>&1 &

WATCH_PIPELINE_2026-06-18
"""

from __future__ import annotations

import re
import sys
import time
import os

# ── Telegram ──────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS
from notifications.telegram import format_ru_amount, ru_plural, send_html


def _send_tg(text: str) -> None:
    """`text` is pre-built HTML from `_make_message` (`<b>...</b>` fragments) —
    goes through `send_html`'s `sanitize_telegram_html`, the one render/sanitize
    path shared by every sender in this project.

    collapse_whitespace=False (WHITESPACE_IS_CONTENT_2026-08-14): `_make_message`'s
    `'OK  '` / `'  {rows}'` column gaps are the layout, not accidental HTML
    whitespace — default sanitizing flattened them to one space, director-caught
    round 3."""
    if not send_html(text, bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID,
                     proxy_variants=TELEGRAM_PROXY_VARIANTS, collapse_whitespace=False):
        print(f'[watch] TG send failed on all proxies: {text[:80]}', flush=True)


# ── Паттерны детектирования ────────────────────────────────────────────────────
# Каждый элемент: (regex, human_label, emoji)
# Порядок важен — более специфичные раньше более общих.
PATTERNS = [
    # Loop-шаги по run_step
    (re.compile(r'Шаг (\d+) завершён за ([\d.]+) сек(?:\s+\(([\d,]+) строк\))?'),
     'step_done', None),  # label и emoji вычисляются динамически
    # step4 cache
    (re.compile(r'step4 \(кэш\): .* за ([\d.]+) сек'),
     'step4_cache', None),
    # step11
    (re.compile(r'step11_pixel_score завершён за ([\d.]+) сек'),
     'step11', None),
    # step12
    (re.compile(r'step12: ([\d]+) строк за ([\d.]+) сек'),
     'step12', None),
    # step13_rebuild
    (re.compile(r'step13_arrival \(пересборка после step11\): .* за ([\d.]+) сек'),
     'step13_rebuild', None),
    # build_unified — матчим только финальную строку модуля ("готово за"),
    # чтобы не задваивать: pipeline.py пишет вторую строку с details (rows=...за X сек)
    # WATCHER_DEDUP_BUILD_UNIFIED_2026-06-19
    (re.compile(r'build_unified: готово за ([\d.]+) сек'),
     'build_unified', None),
    # build_region_spend — только финальная строка модуля ("готово за")
    # pipeline.py пишет вторую строку с details → дубль без "готово"
    # WATCHER_DEDUP_BUILD_UNIFIED_2026-06-19
    (re.compile(r'build_region_spend: готово за ([\d.]+) сек'),
     'build_region_spend', None),
    # build_adformat_spend — аналогично
    (re.compile(r'build_adformat_spend: готово за ([\d.]+) сек'),
     'build_adformat_spend', None),
    # build_criterion_spend — аналогично
    (re.compile(r'build_criterion_spend: готово за ([\d.]+) сек'),
     'build_criterion_spend', None),
    # build_region_zayavki — аналогично
    (re.compile(r'build_region_zayavki: готово за ([\d.]+) сек'),
     'build_region_zayavki', None),
    # build_criterion_zayavki — аналогично
    (re.compile(r'build_criterion_zayavki: готово за ([\d.]+) сек'),
     'build_criterion_zayavki', None),
    # build_star
    (re.compile(r'build_star: готово за ([\d.]+) сек'),
     'build_star', None),
    # verify
    (re.compile(r'verify_big_analytics: завершён за ([\d.]+) сек'),
     'verify', None),
    # golden_kuderko
    (re.compile(r'golden_kuderko: (.+)'),
     'golden', None),
    # DONE OK
    (re.compile(r'УСПЕШНО завершено за ([\d]+) сек \(run_id=([a-f0-9]+)\)'),
     'done_ok', None),
    # DONE ERROR
    (re.compile(r'ЗАВЕРШЕНО С ОШИБКОЙ за ([\d]+) сек \(run_id=([a-f0-9]+)\)'),
     'done_err', None),
]

STEP_NAMES = {
    0: 'step0 sync_local',
    1: 'step1 load_raw',
    2: 'step2 indexes',
    3: 'step3 build_sources',
    4: 'step4 campaign_status',
    5: 'step5 build_pixel',
    6: 'step6 build_full',
    7: 'step7 finalize',
    8: 'step8 stats+TG',
    13: 'step13 arrival',
}


def _fmt_sec(sec_str: str) -> str:
    """'123.4' → '2м 03с' или '45.1' → '45с'."""
    try:
        s = float(sec_str)
        if s >= 60:
            m = int(s // 60)
            ss = int(s % 60)
            return f'{m}м {ss:02d}с'
        return f'{s:.0f}с'
    except Exception:
        return sec_str + 'с'


def _fmt_rows(rows_str: str) -> str:
    """'5,231' / '5231' → '5 231 строка/строки/строк' (NNBSP thousands, RU plural)."""
    try:
        n = int(rows_str.replace(',', ''))
    except (ValueError, AttributeError):
        return f'{rows_str} строк'
    return f'{format_ru_amount(n)} {ru_plural(n, "строка", "строки", "строк")}'


def _make_message(label: str, line: str) -> str | None:
    """Строит TG-текст для детектированного маркера."""

    # Loop-шаги: "Шаг N завершён за X.X сек (N строк)"
    if label == 'step_done':
        m = re.search(r'Шаг (\d+) завершён за ([\d.]+) сек(?:\s+\(([\d,]+) строк\))?', line)
        if not m:
            return None
        num = int(m.group(1))
        dur = _fmt_sec(m.group(2))
        rows = f'  {_fmt_rows(m.group(3))}' if m.group(3) else ''
        name = STEP_NAMES.get(num, f'step{num}')
        return f'OK  <b>{name}</b> — {dur}{rows}'

    if label == 'step4_cache':
        m = re.search(r'step4 \(кэш\): .* за ([\d.]+) сек', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        return f'OK  <b>step4 campaign_status</b> (кэш) — {dur}'

    if label == 'step11':
        m = re.search(r'step11_pixel_score завершён за ([\d.]+) сек', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        return f'OK  <b>step11 pixel_score</b> — {dur}'

    if label == 'step12':
        m = re.search(r'step12: ([\d]+) строк за ([\d.]+) сек', line)
        rows = _fmt_rows(m.group(1)) if m else '? строк'
        dur = _fmt_sec(m.group(2)) if m else '?'
        return f'OK  <b>step12 proverka</b> — {dur}  {rows}'

    if label == 'step13_rebuild':
        m = re.search(r'за ([\d.]+) сек', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        return f'OK  <b>step13_arrival (rebuild)</b> — {dur}'

    if label == 'build_unified':
        m = re.search(r'за ([\d.]+) сек', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        return f'OK  <b>build_unified</b> — {dur}'

    if label in ('build_region_spend', 'build_adformat_spend', 'build_criterion_spend',
                 'build_region_zayavki', 'build_criterion_zayavki'):
        m = re.search(r'за ([\d.]+) сек', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        return f'OK  <b>{label}</b> — {dur}'

    if label == 'build_star':
        m = re.search(r'за ([\d.]+) сек', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        return f'OK  <b>build_star</b> — {dur}'

    if label == 'verify':
        m = re.search(r'за ([\d.]+) сек', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        return f'OK  <b>verify_big_analytics</b> — {dur}'

    if label == 'golden':
        # "golden_kuderko: golden Кудерко (unified, По дате заявки): расход=25422774.0 ..."
        short = line.split('golden_kuderko: ', 1)[-1][:200]
        return f'<b>golden_kuderko</b>\n<code>{short}</code>'

    if label == 'done_ok':
        m = re.search(r'УСПЕШНО завершено за ([\d]+) сек \(run_id=([a-f0-9]+)\)', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        rid = m.group(2) if m else '?'
        return (f'DONE  <b>fast_pipeline УСПЕШНО</b>\n'
                f'Общее время: {dur}  run_id: <code>{rid}</code>')

    if label == 'done_err':
        m = re.search(r'ЗАВЕРШЕНО С ОШИБКОЙ за ([\d]+) сек \(run_id=([a-f0-9]+)\)', line)
        dur = _fmt_sec(m.group(1)) if m else '?'
        rid = m.group(2) if m else '?'
        return (f'FAIL  <b>fast_pipeline ОШИБКА</b>\n'
                f'Упал за {dur}  run_id: <code>{rid}</code>')

    return None


LOG_FILE = '/tmp/fast_pipeline.log'
POLL_SEC = 3        # интервал опроса файла
MAX_IDLE_SEC = 7200  # 2 ч без движения лога → завершить watcher (пайплайн завис/закончился)


def tail_forever(path: str):
    """Открывает path, seek в конец, yield новые строки. При ротации файла — переоткрывает."""
    print(f'[watch] открываю {path}', flush=True)
    while not os.path.exists(path):
        print(f'[watch] жду {path}...', flush=True)
        time.sleep(2)

    f = open(path, 'r', encoding='utf-8', errors='replace')
    f.seek(0, 2)  # в конец — не читаем историю
    last_data_ts = time.time()
    inode = os.stat(path).st_ino

    while True:
        line = f.readline()
        if line:
            last_data_ts = time.time()
            yield line.rstrip('\n')
        else:
            time.sleep(POLL_SEC)
            # Проверка ротации (новый inode или файл пропал/пересоздан)
            try:
                new_inode = os.stat(path).st_ino
            except FileNotFoundError:
                new_inode = None
            if new_inode != inode:
                print('[watch] inode изменился — переоткрываю файл', flush=True)
                f.close()
                time.sleep(1)
                f = open(path, 'r', encoding='utf-8', errors='replace')
                inode = os.stat(path).st_ino
                last_data_ts = time.time()
            # Idle timeout
            if time.time() - last_data_ts > MAX_IDLE_SEC:
                print(f'[watch] {MAX_IDLE_SEC}с без новых строк — завершаю watcher', flush=True)
                f.close()
                return


def main():
    print(f'[watch] WATCH_PIPELINE_2026-06-18 запущен, PID={os.getpid()}', flush=True)
    _send_tg('watch_pipeline запущен (WATCH_PIPELINE_2026-06-18)')

    for raw_line in tail_forever(LOG_FILE):
        for pattern, label, _ in PATTERNS:
            if pattern.search(raw_line):
                msg = _make_message(label, raw_line)
                if msg:
                    print(f'[watch] MATCH {label}: {msg[:80]}', flush=True)
                    _send_tg(msg)
                break  # одна строка → один маркер

    print('[watch] завершён (idle timeout или конец файла)', flush=True)


if __name__ == '__main__':
    main()
