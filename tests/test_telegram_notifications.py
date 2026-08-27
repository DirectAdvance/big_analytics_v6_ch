"""Tests for notifications/telegram.py — ported from big_analytics_v5 (commit
bd192a2, VERDICT_FIRST_FACT_ONLY_2026-08-14): verdict-first title, no <pre>/<code>
block, no python-repr quotes on labels, RU number formatting, fact-only step
failure, and HTML escaping of dangerous dynamic values.

ALL_SENDERS_ON_SHARED_MODULE round (2026-08-14): every Telegram call site in this
project (pipeline gate, watch_pipeline ticker, refresh_powerbi notices, funnel
drift/crm/checking-report/golden_reward reports, cookies warnings) now sends
through `notifications.telegram`. This file also covers one message per kind
built by each site's own (now testable) message builder.
"""

import pytest

from notifications.telegram import (
    TelegramMessage,
    build_parity_gate_message,
    build_pipeline_error_message,
    render_html,
    render_plain_text,
    sanitize_telegram_html,
)


def test_build_pipeline_error_message_is_fact_only_no_exception_details():
    # Telegram gets WHICH step stopped the pipeline, never the exception
    # class/message/traceback — that stays in the runtime log + data_quality_log.
    message = build_pipeline_error_message(
        pipeline_name='big_analytics_v6 night',
        run_id='abc12345',
        failed_step='night_korrektirovki',
        timestamp='2026-08-14 11:55',
    )

    text = render_plain_text(message)

    assert text == (
        '🔴 big_analytics_v6 night остановлен\n'
        '2026-08-14 11:55 | run_id: abc12345\n\n'
        '⚠️ Пайплайн остановлен на шаге night_korrektirovki\n\n'
        'Где смотреть:\n'
        'Лог: runtime-лог пайплайна на Victory (полный traceback)\n'
        'БД: последняя error-запись в data_quality_log по этому run_id'
    )
    assert 'ValueError' not in text
    assert 'Traceback' not in text


def test_build_pipeline_error_message_escapes_step_name_with_angle_brackets():
    message = build_pipeline_error_message(
        pipeline_name='big_analytics_v6', run_id='r1', failed_step='<GOLDEN>',
    )

    html = render_html(message)

    assert '⚠️ Пайплайн остановлен на шаге &lt;GOLDEN&gt;' in html
    assert '<GOLDEN>' not in html


def test_build_parity_gate_message_is_verdict_first_no_pre_no_repr():
    message = build_parity_gate_message(
        failed=[('Заявки', 1000.0, 900.0, 100.0, 10.0)],
        ok=[('Квалы', 0.09), ('Приезд-визит', 0.877)],
        spend=25422798.0,
        stopped_step='GOLDEN',
        context='v6 deploy smoke test',
        timestamp='14.08.2026',
    )

    html = render_html(message)
    nnbsp = ' '  # narrow no-break space — Семён's thousands separator

    assert html == (
        '<b>🔴 СВЕРКА НЕ ПРОШЛА — 1 из 3 метрик</b>\n\n'
        '<i>14.08.2026 · v6 deploy smoke test</i>\n\n'
        '❗ Заявки\n'
        f'   БД 1{nnbsp}000 → PBI 900\n'
        '   не хватает 100 (10,0%)\n\n'
        '✅ Квалы (0,09%), Приезд-визит (0,88%) — в допуске\n\n'
        f'Расход за период: 25{nnbsp}422{nnbsp}798 ₽\n\n'
        '⚠️ Пайплайн остановлен на шаге GOLDEN'
    )
    assert '<pre>' not in html
    assert '<code>' not in html
    assert "'Заявки'" not in html


def test_build_parity_gate_message_all_ok_degrades_to_green_summary_line():
    message = build_parity_gate_message(
        failed=[],
        ok=[('Заявки', 0.02), ('Квалы', 0.09), ('Приезд-визит', 0.88)],
        spend=25422798.0,
    )

    html = render_html(message)

    assert '🟢 СВЕРКА OK — 3 из 3 метрик в допуске' in html
    assert '✅ Заявки (0,02%), Квалы (0,09%), Приезд-визит (0,88%) — в допуске' in html
    assert '❗' not in html  # no empty "problems" section


def test_build_parity_gate_message_all_fail_has_no_ok_line():
    message = build_parity_gate_message(
        failed=[
            ('Заявки', 1000.0, 900.0, 100.0, 10.0),
            ('Квалы', 25422798.0, 20000000.0, 5422798.0, 21.32),
        ],
        ok=[],
    )

    html = render_html(message)

    assert '🔴 СВЕРКА НЕ ПРОШЛА — 2 из 2 метрик' in html
    assert '✅' not in html  # nothing was in tolerance — no green line at all
    assert 'в допуске' not in html


def test_build_parity_gate_message_escapes_labels_and_step():
    message = build_parity_gate_message(
        failed=[('<GOLDEN>', 1.0, 2.0, 1.0, 50.0)],
        ok=[],
        stopped_step="<class 'ValueError'>",
    )

    html = render_html(message)

    assert '&lt;GOLDEN&gt;' in html
    assert "&lt;class 'ValueError'&gt;" in html
    assert '<GOLDEN>' not in html
    assert "<class 'ValueError'>" not in html


# ── refresh_powerbi.py: ad-hoc status notices (not a gate — single fact each) ──

def test_refresh_powerbi_run_failed_is_fact_only_no_exception():
    import refresh_powerbi as rp

    message = rp.build_run_failed_message()
    html = render_html(message)

    assert html.startswith('<b>🔴 refresh_powerbi: остановлен ошибкой</b>')
    assert '<pre>' not in html and '<code>' not in html
    assert 'Traceback' not in html


def test_refresh_powerbi_refresh_failed_drops_raw_powerbi_error_text():
    """Round 2 fix: serviceExceptionJson (vendor free-form error) no longer goes
    to Telegram at all — only status + requestId + a pointer to the log, where
    `logger.error` (refresh_powerbi.py, right before this is built) logs it in full."""
    import refresh_powerbi as rp

    message = rp.build_refresh_failed_message('Failed', 'req-1')
    html = render_html(message)

    assert html.startswith('<b>❌ Power BI: обновление завершилось со статусом Failed</b>')
    assert 'requestId: req-1' in html
    assert 'в логе refresh_powerbi на Victory' in html
    assert 'serviceExceptionJson' not in html


def test_refresh_powerbi_done_and_timeout_messages():
    import refresh_powerbi as rp

    assert render_html(rp.build_refresh_done_message(125)) == '<b>✅ Power BI: обновление завершено (2 мин 5 сек)</b>'
    assert '⚠️ Power BI: не удалось получить статус' in render_html(rp.build_poll_timeout_message())


# ── watch_pipeline.py: progress ticker — small pre-rendered HTML fragments ─────

def test_watch_pipeline_ticker_line_is_small_html_fragment_no_pre(monkeypatch):
    """Asserts on the CALLER passing the flag, not on a local re-render:
    monkeypatches `notifications.telegram.send_html` and calls the real
    `wp._send_tg` (WHITESPACE_IS_CONTENT_2026-08-14, round 4 director fix — the
    previous version computed `sanitize_telegram_html(msg, collapse_whitespace=False)`
    itself and never exercised `_send_tg`, so deleting the flag at its call site
    would have stayed green)."""
    import watch_pipeline as wp

    captured = {}

    def fake_send_html(html, **kwargs):
        captured['html'] = html
        captured['kwargs'] = kwargs
        return True

    # watch_pipeline imports send_html eagerly at module top-level (`from
    # notifications.telegram import send_html`), so it must be patched on `wp`
    # itself, not on `notifications.telegram` — that only affects lazy
    # (call-time) `from ... import` sites like cookies.py/funnel_drift.
    monkeypatch.setattr(wp, 'send_html', fake_send_html)

    line = 'Шаг 4 завершён за 12.3 сек (1,000 строк)'
    msg = wp._make_message('step_done', line)
    nnbsp = ' '

    assert msg == f'OK  <b>step4 campaign_status</b> — 12с  1{nnbsp}000 строк'
    assert '1,000' not in msg  # round 2 fix: no comma thousands separator

    wp._send_tg(msg)

    assert captured['kwargs'].get('collapse_whitespace') is False
    assert captured['html'] == msg  # text handed to send_html, unmodified

    shipped = sanitize_telegram_html(captured['html'], collapse_whitespace=False)
    assert '<pre>' not in shipped and '<code>' not in shipped
    assert '<b>step4 campaign_status</b>' in shipped
    assert 'OK  <b>' in shipped  # double space before the name survives
    assert f'— 12с  1{nnbsp}000 строк' in shipped  # double space before the row count survives

    # the regression this test now guards against: default collapsing would flatten it
    collapsed = sanitize_telegram_html(captured['html'])  # collapse_whitespace=True (default)
    assert collapsed != shipped
    assert 'OK  <b>' not in collapsed


def test_watch_pipeline_row_count_plural_agreement_1_2_5():
    """Whitespace-through-transport is covered by the ticker test above (which
    exercises `wp._send_tg`); this one is just plural agreement on the builder."""
    import watch_pipeline as wp

    assert wp._fmt_rows('1') == '1 строка'
    assert wp._fmt_rows('2') == '2 строки'
    assert wp._fmt_rows('5') == '5 строк'
    assert wp._fmt_rows('11') == '11 строк'  # 11-14 exception stays "many"

    line = 'step12: 5230 строк за 3.0 сек'
    msg = wp._make_message('step12', line)
    nnbsp = ' '
    assert msg == f'OK  <b>step12 proverka</b> — 3с  5{nnbsp}230 строк'


# ── yandex_direct_checking_report/report.py: table report, failed-accounts list ─

class _FakeCursor:
    """Minimal cursor stand-in — enough for `run_comparison`'s
    `execute`/`fetchall`, no real DB needed."""

    def execute(self, *args, **kwargs):
        pass

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()


def test_yandex_checking_report_failed_accounts_bullet_indent_survives(monkeypatch):
    """Asserts on the CALLER passing the flag: monkeypatches
    `notifications.telegram.send_html` and calls the real `run_comparison`.
    `'  • login (domain)'` uses a 2-space bullet indent as layout — round 4
    director finding, same class as watch_pipeline/funnel_drift."""
    import notifications.telegram as tg
    from yandex_direct_checking_report import report as ydcr

    captured = {}

    def fake_send_html(html, **kwargs):
        captured['html'] = html
        captured['kwargs'] = kwargs
        return True

    monkeypatch.setattr(tg, 'send_html', fake_send_html)
    monkeypatch.setattr(ydcr, 'TELEGRAM_BOT_TOKEN', 'fake-token')
    monkeypatch.setattr(ydcr, 'TELEGRAM_CHAT_ID', 'fake-chat')

    ydcr.run_comparison(_FakeConn(), failed_list=[('acc1', 'dom1.ru')])

    assert captured['kwargs'].get('collapse_whitespace') is False
    shipped = sanitize_telegram_html(captured['html'], collapse_whitespace=False)
    assert '  • acc1 (dom1.ru)' in shipped  # bullet indent survives

    collapsed = sanitize_telegram_html(captured['html'])  # collapse_whitespace=True (default)
    assert '  • acc1 (dom1.ru)' not in collapsed


# ── config/cookies.py: send_tg is now a thin delegate to send_html ─────────────

def test_cookies_send_tg_delegates_to_shared_sender(monkeypatch):
    import config.cookies as cookies
    import notifications.telegram as tg

    captured = {}

    def fake_send_html(html, **kwargs):
        captured['html'] = html
        return True

    monkeypatch.setattr(tg, 'send_html', fake_send_html)
    cookies.send_tg('<b>test</b>')

    assert captured['html'] == '<b>test</b>'


def test_cookies_read_failure_notice_drops_raw_exception():
    """AAB4C-style bug found in this round: send_tg(f'...\\n{e}') leaked the raw
    exception into Telegram. Fixed to fact-only; exception stays in logger.error."""
    import config.cookies as cookies

    captured = {}
    with pytest.raises(FileNotFoundError):
        cookies.ensure_cookies_alive_or_stop(
            pipeline_name='test_pipeline',
            send_tg=lambda text: captured.setdefault('text', text),
            cookies_path='/nonexistent/cookies.json',
        )

    assert captured['text'] == '❌ test_pipeline: не удалось прочитать cookies.json'
    assert 'nonexistent' not in captured['text']
    assert 'FileNotFoundError' not in captured['text']


# ── crm_mappings_check/check.py: report — verdict-first title, no repr quotes ──

def test_crm_mappings_check_message_no_repr_quotes_no_cnt_debug_style():
    from crm_mappings_check.check import build_message

    unmapped_status = [{'val': "weird'status", 'cnt': 3}]
    unmapped_reason = [{'val': 'no_reason', 'cnt': 1, 'statuses': 'X, Y'}]

    message = build_message(unmapped_status, unmapped_reason, run_id='r42')
    html = render_html(message)

    assert html.startswith('<b>🔴 local_crm_statuses: 2 несопоставленных значения</b>')
    assert "'weird" not in html and "status'" not in html  # no !r python-repr quoting
    assert 'weird&#x27;status' in html or "weird'status" in html  # value itself preserved
    assert 'cnt=' not in html  # round 2 fix: readable phrase, not debug output
    # every original piece of data survives: value, its lead count, statuses list
    assert '3 лида' in html
    assert '1 лид' in html
    assert '[statuses: X, Y]' in html


def test_crm_mappings_check_message_title_plural_agreement_1_2_5():
    from crm_mappings_check.check import build_message

    def title(n):
        rows = [{'val': f'v{i}', 'cnt': 1} for i in range(n)]
        return render_html(build_message(rows, [], run_id=None)).splitlines()[0]

    assert title(1) == '<b>🔴 local_crm_statuses: 1 несопоставленное значение</b>'
    assert title(2) == '<b>🔴 local_crm_statuses: 2 несопоставленных значения</b>'
    assert title(5) == '<b>🔴 local_crm_statuses: 5 несопоставленных значений</b>'


def test_crm_mappings_check_message_all_mapped_is_green():
    from crm_mappings_check.check import build_message

    html = render_html(build_message([], [], run_id=None))
    assert html.startswith('<b>🟢 local_crm_statuses: несопоставленных значений нет</b>')


# ── data_check/golden_reward.py: PASS/HARD FAIL gate ────────────────────────────

def test_golden_reward_message_is_verdict_first_no_exception():
    golden_reward = pytest.importorskip(
        'data_check.golden_reward',
        reason='pre-existing unrelated ImportError: ARRIVAL_ROWS_MIN/GRAND_SALES_LO '
               'missing from data_check.verify_big_analytics (not a telegram issue)',
        exc_type=ImportError,
    )

    r = {
        'hard_fail': False, 'reward': 987.8, 'ts': '2026-08-14T12:00:00Z',
        'cost': 25422786.0, 'cost_ok': True, 'cost_dist': 12.2,
        'sales': 47, 'sales_ok': True, 'sales_slack': 0, 'sales_floor': 47,
        'n_violations': 0, 'data_source': 'durable:fact_big_analytics',
    }
    html = render_html(golden_reward.build_message(r))

    assert html.startswith('<b>🟢 PASS — reward=987.8</b>')
    assert '<pre>' not in html and '<code>' not in html
    assert 'Traceback' not in html


# ── data_check/reporter.py: Data Integrity Check — RU money, report shape ──────

def test_reporter_format_report_uses_ru_money_not_python_comma():
    from data_check.reporter import format_report

    results = {
        'projects': {'only_in_sheets': [], 'only_in_db': [], 'no_analytics_data': []},
        'fields': {'null_directologist': [], 'null_manager_login': [], 'null_login_key': []},
        'spending': {'spend_no_leads_7d': [{'проджект': 'site1.ru', 'total_cost': 1234567.0}],
                     'no_spend_7d': [], 'per_project': []},
        'funnel': {'invariant_violations': [], 'zero_funnel_active': [], 'per_project': []},
        'freshness': {'last_pipeline_run': '2026-08-14 10:00:00', 'hours_ago': 1, 'stale': False},
    }
    report = format_report(results)
    nnbsp = ' '

    assert f'1{nnbsp}234{nnbsp}567 ₽' in report  # round 2 fix: space before ₽
    assert '567₽' not in report  # no glued-on ₽
    assert '1,234,567' not in report


# ── notifications/telegram.py: silent-failure regression guard ─────────────────

def test_post_once_per_proxy_logs_failure_and_cause_when_every_proxy_fails(caplog):
    # Regression guard for the swallowed `except Exception: continue` that made
    # a dead Telegram channel invisible (director finding, 2026-08-14): when
    # every proxy variant fails, the log must say so AND carry the last error.
    from notifications.telegram import _post_once_per_proxy

    def fake_post(url, json, timeout, proxies):
        raise ConnectionError('proxy refused connection')

    with caplog.at_level('ERROR', logger='notifications.telegram'):
        result = _post_once_per_proxy(
            url='https://api.telegram.org/botX/sendMessage',
            payload={'chat_id': '1', 'text': 'hi'},
            proxy_variants=[{'https': 'socks5://a'}, {'https': 'socks5://b'}],
            post=fake_post,
            timeout=5,
        )

    assert result is False
    error_records = [r for r in caplog.records if r.levelname == 'ERROR']
    assert len(error_records) == 1  # one summary line, not one per attempt
    assert 'proxy refused connection' in error_records[0].getMessage()
