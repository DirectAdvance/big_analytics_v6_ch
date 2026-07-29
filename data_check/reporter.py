"""
step_data_check/reporter.py

Форматирование отчёта + отправка в Telegram.
"""

from __future__ import annotations
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


def format_report(results: dict) -> str:
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
        cost = r['total_cost']
        critical.append(f'· {r["проджект"]} — расход {cost:,.0f}₽, лидов 0 за 7 дней')

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
    """Отправка в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct).

    proxy_variants — цепочка [{https:...}, ..., None], предпочтительный способ.
    proxy — одиночный прокси (backward-compat, используется если proxy_variants не задан).
    """
    # TG_PROXY_CHAIN_ROTATION_2026-06-17
    if proxy_variants is None:
        proxy_variants = [{'https': proxy, 'http': proxy}] if proxy else [None]
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    chunks = _split_chunks(report)
    for chunk in chunks:
        sent = False
        for proxies in proxy_variants:
            try:
                resp = requests.post(
                    url,
                    json={'chat_id': chat_id, 'text': chunk},
                    proxies=proxies,
                    timeout=30,
                )
                if resp.ok:
                    sent = True
                    break
                logger.warning('Telegram send failed (proxies=%s): %s', proxies, resp.text)
            except Exception as e:
                logger.warning('Telegram (proxies=%s): %s', proxies, e)
        if not sent:
            logger.warning('Telegram chunk не доставлен ни через один прокси')


def _split_chunks(text: str, limit: int = 4096) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks, buf, buf_len = [], [], 0
    for line in text.split('\n'):
        line_len = len(line) + 1
        if buf_len + line_len > limit and buf:
            chunks.append('\n'.join(buf))
            buf, buf_len = [], 0
        if line_len > limit:
            # flush buf first, then hard-split the oversized line
            if buf:
                chunks.append('\n'.join(buf))
                buf, buf_len = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i:i+limit])
            continue
        buf.append(line)
        buf_len += line_len
    if buf:
        chunks.append('\n'.join(buf))
    return chunks
