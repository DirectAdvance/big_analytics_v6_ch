"""Telegram notifications rendered as sanitized Telegram HTML (parse_mode='HTML').

TELEGRAM_HTML_RESTORE_2026-08-14: markup is produced by the renderers below and every
dynamic value is escaped on the way in (`_safe` / `sanitize_telegram_html`), instead of
transporting plain text with markup stripped. Plain-text rendering stays as the
fail-closed fallback when an HTML send fails end-to-end (`_post_message`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from html import escape, unescape
from typing import Callable, Iterable

import requests


TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_TIMEOUT_SECONDS = 10
PARSE_MODE = 'HTML'

# Headroom reserved on each chunk when a message is split across the 4096-char
# boundary, so balancing tags (closing at the end of a chunk / reopening at the
# start of the next) always fits without re-splitting.
_CHUNK_BOUNDARY_RESERVE = 64


@dataclass(frozen=True)
class TelegramSection:
    title: str
    lines: list[str] = field(default_factory=list)
    rows: list[tuple[str, str]] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    open: bool = False


@dataclass(frozen=True)
class TelegramMessage:
    title: str
    meta: list[str] = field(default_factory=list)
    sections: list[TelegramSection] = field(default_factory=list)
    summary: str | None = None


PostFunc = Callable[..., requests.Response]


def render_plain_text(message: TelegramMessage) -> str:
    lines = [message.title]
    if message.meta:
        lines.append(' | '.join(str(item) for item in message.meta if str(item).strip()))
    if message.summary:
        lines.extend(['', str(message.summary)])

    for section in message.sections:
        section_lines = _plain_section_lines(section)
        if section_lines:
            lines.extend(['', *section_lines])

    return _normalize_plain_text('\n'.join(lines))


def render_html(message: TelegramMessage) -> str:
    """Structural HTML renderer for `TelegramMessage` — the primary send path.

    title -> <b>title</b>; meta -> <i>a | b</i>; summary -> plain line; each
    section -> blank line, <b>Section title:</b>, rows (label: value, value in
    <code> when it looks like data), code_blocks as <pre>...</pre>, then plain lines.
    """
    parts = [f'<b>{_safe(message.title)}</b>']
    if message.meta:
        meta_text = ' | '.join(str(item) for item in message.meta if str(item).strip())
        if meta_text:
            parts.append(f'<i>{_safe(meta_text)}</i>')
    if message.summary:
        parts.append(_safe(message.summary))
    parts.extend(_render_html_section(section) for section in message.sections)
    return '\n\n'.join(part for part in parts if part)


def send_notification(
    message: TelegramMessage,
    *,
    bot_token: str,
    chat_id: str,
    proxy_variants: Iterable[dict | None],
    post: PostFunc = requests.post,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    return _post_message(
        render_html(message),
        bot_token=bot_token,
        chat_id=chat_id,
        proxy_variants=proxy_variants,
        post=post,
        timeout=timeout,
    )


def send_html(
    html: str,
    *,
    bot_token: str,
    chat_id: str,
    proxy_variants: Iterable[dict | None],
    post: PostFunc = requests.post,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    collapse_whitespace: bool = True,
) -> bool:
    return _post_message(
        sanitize_telegram_html(html, collapse_whitespace=collapse_whitespace),
        bot_token=bot_token,
        chat_id=chat_id,
        proxy_variants=proxy_variants,
        post=post,
        timeout=timeout,
    )


def html_to_plain_text(html: str) -> str:
    return _html_to_plain_text(html)


def sanitize_telegram_html(html: str, *, collapse_whitespace: bool = True) -> str:
    """Clean caller-supplied HTML down to Telegram's whitelist.

    Keeps `b, strong, i, em, u, ins, s, strike, del, code, pre, a, blockquote`
    (attribute-free, except `a href`), turns layout tags into newlines/spaces, and
    escapes everything else (including tag-shaped dynamic values like
    `<GOLDEN>` or `<class 'ValueError'>`) as literal text. Blank-line normalisation
    matches `_normalize_plain_text`, but `<pre>` spans and multi-line `<code>`
    spans are protected first — both carry column alignment that must survive
    verbatim (AAB4C_PROTECT_MULTILINE_CODE_2026-08-14: a `<code>` block built
    from `'\\n'.join(...)`, e.g. a diagnostic table, used to lose its indent the
    same way an unprotected `<pre>` would).

    `collapse_whitespace=False` (WHITESPACE_IS_CONTENT_2026-08-14): a small set
    of pre-built-HTML report senders (funnel drift, watch_pipeline ticker) use
    plain leading/internal space runs as a lightweight column layout — no
    `<pre>`/`<code>` block (Семён rejected the copy-button chrome that puts on
    the whole message). For them `' '.join(line.split())` was silently
    flattening that layout to one space everywhere, indentation included. This
    flag skips `_normalize_plain_text` and returns the tag-cleaned text as-is;
    default stays `True` — every other caller (arbitrary/HTML-templated text)
    keeps today's whitespace cleanup.
    """
    if not html:
        return ''
    cleaned = _clean_tags(html)
    if not collapse_whitespace:
        return cleaned
    protected, blocks = _protect_preformatted_spans(cleaned)
    normalized = _normalize_plain_text(protected)
    return _restore_preformatted_spans(normalized, blocks)


def build_pipeline_error_message(
    *,
    pipeline_name: str,
    run_id: str,
    failed_step: str,
    timestamp: str | None = None,
) -> TelegramMessage:
    """VERDICT_FIRST_FACT_ONLY_2026-08-14: Telegram gets the fact — which step
    stopped the pipeline — never the exception class/message/traceback (Семён:
    that is noise on a phone and the reader still can't act on it). The full
    traceback is not lost: `pipeline.py::run_step` logs it server-side with
    `exc_info=True` before this message is built (see the runtime log path in
    'Где смотреть' below)."""
    when = timestamp or datetime.now().strftime('%Y-%m-%d %H:%M')
    return TelegramMessage(
        title=f'🔴 {pipeline_name} остановлен',
        meta=[when, f'run_id: {run_id}'],
        summary=f'⚠️ Пайплайн остановлен на шаге {failed_step}',
        sections=[
            TelegramSection(
                'Где смотреть',
                rows=[
                    ('Лог', 'runtime-лог пайплайна на Victory (полный traceback)'),
                    ('БД', 'последняя error-запись в data_quality_log по этому run_id'),
                ],
            ),
        ],
    )


def format_ru_amount(value: float) -> str:
    """`25422798.0 -> '25 422 798'`, `900.0 -> '900'`, `100.5 -> '100,5'`.

    Narrow no-break space (U+202F) as the thousands separator, comma as the
    decimal separator, no trailing `,0` on whole numbers — Семён's format
    decision for money/count values in Telegram digests.
    """
    integer_part, _, frac_part = f'{value:,.2f}'.partition('.')
    integer_part = integer_part.replace(',', ' ')
    frac_part = frac_part.rstrip('0')
    return integer_part if not frac_part else f'{integer_part},{frac_part}'


def format_ru_percent(value: float) -> str:
    """`10.0 -> '10,0%'`, `0.09 -> '0,09%'`, `0.877 -> '0,88%'`.

    Always keeps at least one decimal (unlike `format_ru_amount`) so a small
    percentage never collapses to a misleading whole number.
    """
    integer_part, _, frac_part = f'{value:.2f}'.partition('.')
    frac_part = frac_part.rstrip('0') or '0'
    return f'{integer_part},{frac_part}%'


def ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Russian noun/adjective agreement: `ru_plural(1, 'строка', 'строки', 'строк')
    -> 'строка'`, `ru_plural(2, ...) -> 'строки'`, `ru_plural(5, ...) -> 'строк'`,
    `ru_plural(11, ...) -> 'строк'` (11-14 are always `many`, the standard exception)."""
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 14:
        return many
    n1 = n_abs % 10
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def build_parity_gate_message(
    *,
    failed: list[tuple[str, float, float, float, float]],
    ok: list[tuple[str, float]],
    spend: float | None = None,
    stopped_step: str | None = None,
    context: str | None = None,
    timestamp: str | None = None,
) -> TelegramMessage:
    """Verdict-first reconciliation message (DATASET_PARITY_GATE-style: DB sum
    vs Power BI DAX sum, per metric/type).

    `failed` — out-of-tolerance metrics as `(label, db_value, pbi_value, diff,
    diff_pct)`; `ok` — in-tolerance metrics as `(label, diff_pct)`. Tolerance
    itself is the CALLER's business rule (not defined here — no caller in this
    repo wires this in yet; kept for a future DB-sum-vs-PBI-DAX-sum parity gate)
    — this function only renders an already-classified split, so the render
    layer never duplicates or drifts from wherever the gate's threshold lives.

    In-tolerance metrics collapse into ONE summary line; only failing metrics
    get a detail block, so the reader sees the problem first, not a wall of
    equal-weight rows. All-OK degrades to a green verdict + that one summary
    line (no empty "problems" section); all-fail degrades to no green line at
    all (nothing was in tolerance).
    """
    total = len(failed) + len(ok)
    when = timestamp or datetime.now().strftime('%d.%m.%Y')
    meta_text = when if not context else f'{when} · {context}'

    lines: list[str] = []
    if failed:
        title = f'🔴 СВЕРКА НЕ ПРОШЛА — {len(failed)} из {total} метрик'
        for label, db_val, pbi_val, diff, pct in failed:
            direction = 'не хватает' if db_val > pbi_val else 'лишние'
            lines.append(f'❗ {label}')
            lines.append(f'   БД {format_ru_amount(db_val)} → PBI {format_ru_amount(pbi_val)}')
            lines.append(f'   {direction} {format_ru_amount(diff)} ({format_ru_percent(pct)})')
            lines.append('')
    else:
        title = f'🟢 СВЕРКА OK — {total} из {total} метрик в допуске'

    if ok:
        ok_text = ', '.join(f'{label} ({format_ru_percent(pct)})' for label, pct in ok)
        lines.append(f'✅ {ok_text} — в допуске')
        lines.append('')

    if spend is not None:
        lines.append(f'Расход за период: {format_ru_amount(spend)} ₽')
        lines.append('')

    if stopped_step:
        lines.append(f'⚠️ Пайплайн остановлен на шаге {stopped_step}')

    return TelegramMessage(title=title, meta=[meta_text], summary='\n'.join(lines).strip('\n'))


def split_chunks(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    raw_chunks = _split_by_lines(text, max(limit - _CHUNK_BOUNDARY_RESERVE, 1))
    if len(raw_chunks) <= 1:
        return raw_chunks
    return _balance_tags_across_chunks(raw_chunks)


def _split_by_lines(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer: list[str] = []
    buffer_len = 0

    for line in text.split('\n'):
        line_len = len(line) + 1
        if buffer and buffer_len + line_len > limit:
            chunks.append('\n'.join(buffer))
            buffer = []
            buffer_len = 0
        if line_len > limit:
            if buffer:
                chunks.append('\n'.join(buffer))
                buffer = []
                buffer_len = 0
            chunks.extend(_hard_split_line(line, limit))
            continue
        buffer.append(line)
        buffer_len += line_len

    if buffer:
        chunks.append('\n'.join(buffer))
    return chunks


def _hard_split_line(line: str, limit: int) -> list[str]:
    """Split a single overlong line into <=limit pieces without cutting a
    `<tag>` token in half (e.g. `<co` + `de>`), which would make Telegram
    reject the chunk with HTTP 400. Falls back to a raw cut only if a single
    tag itself exceeds `limit` (pathological — avoids an infinite loop)."""
    if limit <= 0 or len(line) <= limit:
        return [line]

    tag_spans = [(m.start(), m.end()) for m in _TAG_TOKEN_RE.finditer(line)]
    parts: list[str] = []
    start = 0
    n = len(line)
    while start < n:
        end = min(start + limit, n)
        for span_start, span_end in tag_spans:
            if span_start < end < span_end:
                end = span_start if span_start > start else end
                break
        if end <= start:
            end = min(start + limit, n)
        parts.append(line[start:end])
        start = end
    return parts


# ── Tag balancing across chunk boundaries ──────────────────────────────────────
# Only attribute-free tags are carried over (reopened) at the start of the next
# chunk; `a` (which may carry `href`) is closed at the boundary but never reopened.
_CARRYABLE_TAGS = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'code', 'pre', 'blockquote'}
_TRACKED_TAGS = _CARRYABLE_TAGS | {'a'}


def _balance_tags_across_chunks(chunks: list[str]) -> list[str]:
    balanced: list[str] = []
    open_stack: list[str] = []

    for chunk in chunks:
        prefix = ''.join(f'<{tag}>' for tag in open_stack)
        stack = list(open_stack)

        for m in _TAG_TOKEN_RE.finditer(chunk):
            parsed = _TAG_PARSE_RE.match(m.group(0))
            if not parsed:
                continue
            closing, name = parsed.group(1), parsed.group(2).lower()
            if name not in _TRACKED_TAGS:
                continue
            if closing:
                if name in stack:
                    # pop the innermost matching occurrence
                    for i in range(len(stack) - 1, -1, -1):
                        if stack[i] == name:
                            del stack[i]
                            break
            else:
                stack.append(name)

        suffix = ''.join(f'</{tag}>' for tag in reversed(stack))
        balanced.append(prefix + chunk + suffix)
        open_stack = [tag for tag in stack if tag != 'a']

    return balanced


def _render_html_section(section: TelegramSection) -> str:
    lines = [f'<b>{_safe(section.title)}:</b>']
    for label, value in section.rows:
        value_str = str(value)
        rendered_value = _safe(value_str)
        if _looks_like_data_value(value_str):
            rendered_value = f'<code>{rendered_value}</code>'
        lines.append(f'{_safe(label)}: {rendered_value}')
    lines.extend(f'<pre>{_safe(block)}</pre>' for block in section.code_blocks)
    lines.extend(_safe(line) for line in section.lines)
    return '\n'.join(lines)


_NUMERIC_VALUE_RE = re.compile(r'^[\d\s.,:+\-/%₽]+$')


def _looks_like_data_value(value: str) -> bool:
    """AAB4C_TIGHTEN_CODE_HEURISTIC_2026-08-14: restricted to genuinely numeric
    values (digits with only numeric punctuation/currency/percent). A looser
    "has a digit" test previously monospaced durations with letters
    ('2ч 12м') and arrow-joined ranges ('03:00 → 05:12') — nobody asked for that."""
    value = value.strip()
    if not value or len(value) > 60:
        return False
    if not _NUMERIC_VALUE_RE.match(value):
        return False
    return any(ch.isdigit() for ch in value)


def _post_message(
    html_text: str,
    *,
    bot_token: str,
    chat_id: str,
    proxy_variants: Iterable[dict | None],
    post: PostFunc = requests.post,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Send `html_text` chunk by chunk with `parse_mode='HTML'`; a chunk Telegram
    rejects end-to-end (e.g. malformed markup) is retried once as plain text
    (tags stripped, no `parse_mode`) — a notification must never be lost
    because of markup.

    DOUBLE_DELIVERY_FIX_2026-08-14: the retry is per-CHUNK, not per-message.
    The previous version re-sent the WHOLE message as plain text whenever ANY
    chunk failed, so a message split across chunks that delivered chunk 1 fine
    and failed on chunk 2 sent chunk 1 twice. Falling back per chunk keeps the
    same guarantee (nothing lost to markup) without redelivering what already
    arrived.
    """
    proxies = list(proxy_variants)
    for chunk in split_chunks(html_text):
        if _post_chunk(chunk, bot_token, chat_id, proxies, post, timeout, parse_mode=PARSE_MODE):
            continue
        # Deliberate — do NOT "fix" this into resending the rest as plain text.
        # A chunk failing BOTH HTML and plain text only happens when every proxy
        # is down both times; bailing out here (instead of also trying later
        # chunks) is the same fail-stop the old whole-message code had, just
        # scoped to what's left. Director-reviewed 2026-08-14: acceptable,
        # rare, not worth a bigger retry matrix.
        if not _post_chunk(html_to_plain_text(chunk), bot_token, chat_id, proxies, post, timeout, parse_mode=None):
            return False
    return True


def _post_chunk(
    text: str,
    bot_token: str,
    chat_id: str,
    proxy_variants: Iterable[dict | None],
    post: PostFunc,
    timeout: int,
    parse_mode: str | None,
) -> bool:
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    payload: dict = {'chat_id': chat_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    return _post_once_per_proxy(url, payload, proxy_variants, post, timeout)


def _post_once_per_proxy(
    url: str,
    payload: dict,
    proxy_variants: Iterable[dict | None],
    post: PostFunc,
    timeout: int,
) -> bool:
    for proxies in proxy_variants:
        try:
            response = post(url, json=payload, timeout=timeout, proxies=proxies)
        except Exception:
            continue
        if response.ok:
            return True
    return False


def _safe(value: object) -> str:
    return escape(str(value), quote=False)


def _plain_section_lines(section: TelegramSection) -> list[str]:
    lines = [f'{section.title}:']
    lines.extend(f'{label}: {value}' for label, value in section.rows)
    for block in section.code_blocks:
        lines.extend(str(block).strip('\n').splitlines())
    lines.extend(str(line) for line in section.lines)
    return [line for line in lines if line.strip()]


def _html_to_plain_text(html: str) -> str:
    text = re.sub(r'(?i)<br\s*/?>', '\n', html)
    text = re.sub(
        r'(?i)</?(p|div|blockquote|details|summary|tr|table|pre|h[1-4]|li)\b[^>]*>',
        '\n',
        text,
    )
    text = re.sub(r'(?i)</?(td|th)\b[^>]*>', ' ', text)
    text = re.sub(r'(?i)</?(a|b|strong|i|em|code|span|footer)\b[^>]*>', '', text)
    return _normalize_plain_text(unescape(text))


def _normalize_plain_text(text: str) -> str:
    normalized_lines: list[str] = []
    blank_pending = False

    for raw_line in text.splitlines():
        line = ' '.join(raw_line.split())
        if not line:
            blank_pending = bool(normalized_lines)
            continue
        if blank_pending:
            normalized_lines.append('')
            blank_pending = False
        normalized_lines.append(line)

    return '\n'.join(normalized_lines).strip()


# ── sanitize_telegram_html internals ───────────────────────────────────────────

_TAG_TOKEN_RE = re.compile(r'<[^<>]*>')
_TAG_PARSE_RE = re.compile(r'^<\s*(/)?\s*([a-zA-Z][a-zA-Z0-9]*)([^<>]*?)\s*/?\s*>$')
_BR_RE = re.compile(r'(?i)^<\s*/?\s*br\s*/?\s*>$')
_HREF_RE = re.compile(r'(?i)href\s*=\s*("([^"]*)"|\'([^\']*)\')')
_AMP_RE = re.compile(r'&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)')

_LAYOUT_TAGS = {
    'p', 'div', 'details', 'summary', 'table', 'tr',
    'h1', 'h2', 'h3', 'h4', 'li', 'ul', 'ol', 'footer', 'header',
}
_CELL_TAGS = {'td', 'th'}
_INLINE_WHITELIST = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'pre', 'blockquote'}

_PRE_BLOCK_RE = re.compile(r'(?is)<pre>.*?</pre>')
_CODE_BLOCK_RE = re.compile(r'(?is)<code>(.*?)</code>')
_PREFORMATTED_PLACEHOLDER = '\x00PRE{}\x00'


def _escape_text(text: str) -> str:
    text = _AMP_RE.sub('&amp;', text)
    return text.replace('<', '&lt;').replace('>', '&gt;')


def _clean_tags(html: str) -> str:
    pieces: list[str] = []
    pos = 0
    for m in _TAG_TOKEN_RE.finditer(html):
        if m.start() > pos:
            pieces.append(_escape_text(html[pos:m.start()]))
        pieces.append(_render_tag_token(m.group(0)))
        pos = m.end()
    if pos < len(html):
        pieces.append(_escape_text(html[pos:]))
    return ''.join(pieces)


def _render_tag_token(raw: str) -> str:
    if _BR_RE.match(raw):
        return '\n'

    parsed = _TAG_PARSE_RE.match(raw)
    if not parsed:
        return _escape_text(raw)

    closing = bool(parsed.group(1))
    name = parsed.group(2).lower()
    attrs = parsed.group(3) or ''

    if name in _LAYOUT_TAGS:
        return '\n'
    if name in _CELL_TAGS:
        return ' '
    if name == 'a':
        if closing:
            return '</a>'
        href_m = _HREF_RE.search(attrs)
        if not href_m:
            return _escape_text(raw)
        href = href_m.group(2) if href_m.group(2) is not None else href_m.group(3)
        return f'<a href="{escape(href or "", quote=True)}">'
    if name in _INLINE_WHITELIST:
        return f'</{name}>' if closing else f'<{name}>'
    return _escape_text(raw)


def _protect_preformatted_spans(text: str) -> tuple[str, list[str]]:
    """Shield `<pre>` (always) and multi-line `<code>` (has an internal
    newline) spans from `_normalize_plain_text`'s blank-line/whitespace
    collapse. A single-line `<code>OK</code>` is untouched by normalisation
    anyway, so only the multi-line case needs protecting."""
    blocks: list[str] = []

    def _store(raw: str) -> str:
        blocks.append(raw)
        return _PREFORMATTED_PLACEHOLDER.format(len(blocks) - 1)

    protected = _PRE_BLOCK_RE.sub(lambda m: _store(m.group(0)), text)
    protected = _CODE_BLOCK_RE.sub(
        lambda m: _store(m.group(0)) if '\n' in m.group(1) else m.group(0),
        protected,
    )
    return protected, blocks


def _restore_preformatted_spans(text: str, blocks: list[str]) -> str:
    for i, block in enumerate(blocks):
        text = text.replace(_PREFORMATTED_PLACEHOLDER.format(i), block)
    return text
