"""
step_data_check/reporter.py

Форматирование отчёта + отправка в Telegram.
"""

from __future__ import annotations
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def format_report(results: dict) -> str:
    from notifications.telegram import format_ru_amount

    lines = []
    today = datetime.now().strftime('%d.%m.%Y %H:%M')
    lines.append(f'🔍 Data Integrity Check — {today}')
    lines.append('')

    critical = []
    warnings = []

    # Projects gap
    proj = results.get('projects', {})
    for domain in proj.get('only_in_sheets', []):
        critical.append(f'· {domain} — есть в Sheets, нет в БД')
    for domain in proj.get('no_analytics_data', []):
        critical.append(f'· {domain} — есть в БД, нет данных в big_analytics_full')
    if proj.get('only_in_db'):
        warnings.append(f'· {len(proj["only_in_db"])} доменов только в БД (не в Sheets): '
                        + ', '.join(proj['only_in_db'][:5])
                        + ('...' if len(proj['only_in_db']) > 5 else ''))

    # Spending
    spend = results.get('spending', {})
    for r in spend.get('spend_no_leads_7d', []):
        critical.append(f'· {r["проджект"]} — расход {format_ru_amount(r["total_cost"])} ₽, лидов 0 за 7 дней')

    # Funnel violations
    funnel = results.get('funnel', {})
    for v in funnel.get('invariant_violations', []):
        issues_str = '; '.join(v['issues'])
        critical.append(f'· {v["проджект"]} — инвариант воронки: {issues_str}')

    # Fields
    fields = results.get('fields', {})
    nd = fields.get('null_directologist', [])
    if nd:
        warnings.append(f'· {len(nd)} доменов: NULL directologist')
    nm = fields.get('null_manager_login', [])
    if nm:
        warnings.append(f'· {len(nm)} доменов: NULL manager_login')
    nk = fields.get('null_login_key', [])
    if nk:
        warnings.append(f'· {len(nk)} доменов: NULL/Нет login_key')

    # No spend 7d
    no_spend = spend.get('no_spend_7d', [])
    if no_spend:
        warnings.append(f'· {len(no_spend)} проджектов: нет расходов за 7 дней')

    # Zero funnel
    zero = funnel.get('zero_funnel_active', [])
    if zero:
        warnings.append(f'· {len(zero)} проджектов: воронка = 0 за 30 дней')

    # PBI freshness
    fresh = results.get('freshness', {})
    if fresh.get('stale'):
        h = fresh.get('hours_ago', '?')
        ts = fresh.get('last_pipeline_run', '?')
        warnings.append(f'· Pipeline последний раз: {ts} ({h}ч назад) — PBI устарел')

    # Сборка
    if critical:
        lines.append(f'❌ КРИТИЧНО ({len(critical)}):')
        lines.extend(critical)
        lines.append('')
    if warnings:
        lines.append(f'⚠️ ПРЕДУПРЕЖДЕНИЯ ({len(warnings)}):')
        lines.extend(warnings)
        lines.append('')

    if not critical and not warnings:
        lines.append('✅ Всё OK — расхождений не найдено')
    else:
        per_project = len(funnel.get('per_project', []))
        ok_count = per_project - len(funnel.get('invariant_violations', []))
        if ok_count > 0:
            lines.append(f'✅ OK: ~{ok_count} проджектов без проблем воронки')

    return '\n'.join(lines)


def send_telegram(report: str, bot_token: str, chat_id: str,
                  proxy: str | None = None,
                  proxy_variants: list | None = None) -> None:
    """`report` is a plain-text digest (no HTML markup) — one sender for the
    project: `notifications.telegram.send_html` (sanitize + chunk + proxy retry).

    proxy_variants — цепочка [{https:...}, ..., None], предпочтительный способ.
    proxy — одиночный прокси (backward-compat, используется если proxy_variants не задан).
    """
    # TG_PROXY_CHAIN_ROTATION_2026-06-17
    if proxy_variants is None:
        proxy_variants = [{'https': proxy, 'http': proxy}] if proxy else [None]
    from notifications.telegram import send_html
    # timeout=30 (was silently 10s default post-migration) — this report can run
    # long chunked sends; restoring the original per-request timeout, not the
    # shared default other (short) senders use.
    if not send_html(report, bot_token=bot_token, chat_id=chat_id, proxy_variants=proxy_variants, timeout=30):
        logger.warning('Telegram report не доставлен ни через один прокси')
