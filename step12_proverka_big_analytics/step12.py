"""
step12_proverka_big_analytics/step12.py — v8 (2026-06-16)

Сверка big_analytics vs campaign_stats_daily по грани CRM.
Фильтр ниши: учитываются ТОЛЬКО домены с niche='Авто' в local_gsheet_sites.

ИСТОЧНИКИ:
  BI-сторона   — public.fact_big_analytics WHERE _source_table IN ('direct','tp8','tp9','tp10')
  CRM-сторона  — ad_analytics.campaign_stats_daily GROUP BY campaign_id
  CRM-map      — dominant-by-spend из fact_big_analytics ("Название crm"), 1:1 домен→CRM
  Ниша-фильтр  — local_gsheet_sites WHERE niche='Авто' (lower/trim нормализация)

ПОЧЕМУ v5 КОРРЕКТЕН (история ошибок):
  v3: домен → "Название crm" из lead-level строки → один домен мог попасть в несколько CRM
      (метка стоит на лиде, а лиды одного домена тегируются в разные CRM) → задвоение;
      Σ csd по CRM ~996M > полного расхода ~890M.

  v4: источник состава CRM — local_gsheet_sites.crm, но это ТОИТ CRM-СОФТА (PLEX/Битрикс/One CRM),
      НЕ агентство. Genzes и Фаиг оба работают в PLEX → схлопываются в одну строку. НЕВЕРНО.

  v5: CRM-map = "Название crm" из ФАКТА агрегированный dominant-by-spend PER DOMAIN.
      Один domain → один CRM (самый «тяжёлый» по расходу). 1:1, нет задвоений.
      BI-сторона = direct+tp8+tp9+tp10 (tp8/tp9/tp10 = посевы, Яндекс биллит через csd → должны входить).
      CRM-сторона: cid → dominant_domain → crm; несматченные cid (~15.9M non-auto/вне периметра)
      — ОТДЕЛЬНАЯ справочная строка, НЕ раскидываются по CRM.
      Δ% считается только по matched-периметру (apples-to-apples).

  v6: добавлен явный фильтр ниши Авто через local_gsheet_sites.niche='Авто'.
      Вселенная доменов сверки ограничена авто-доменами gsheet на всех трёх уровнях:
      - CRM-map строится только по авто-доменам;
      - BI-агрегация — только по авто-доменам;
      - cid→domain→crm маппинг — только если dominant_domain в авто-множестве.
      Домены вне авто-ниши (даже если есть в факте) → попадают в non-auto residual.

  v7: дедупликация csd по (campaign_id, day).
      campaign_stats_daily содержит дубли daily-строк (~2.5% ключей, апрельская загрузка).
      Без дедупа: SUM(total_spend) суммирует дублированные строки → задвоение расхода.
      Фикс: MAX(total_spend) per (campaign_id, day) как внутренний CTE, затем SUM снаружи.
      Доказано director на живой БД: после дедупа csd совпадает с BI/Я.Директ API до копейки
      (cid 702792661: csd-raw ×2 из дубля → MAX = фактический расход из API).
      Также: индикатор доверия в Telegram — число задвоенных (cid,day)-ключей, сумма дублей,
      концентрация остаточного Δ по top-1 cid.

GOLDEN (Кудерко): step12 пишет только analytics_proverka_big_analytics + TG.
  Таблицы fact_big_analytics, big_analytics_full, big_analytics_direct НЕ затрагиваются.

Запуск:
  ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 -m step12_proverka_big_analytics.step12"

# PATCH_MARKER: step12_v7_csd_dedup_trust_indicator_2026-06-16
"""

import calendar
import logging
import time
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import requests

logger = logging.getLogger('pipeline.step12')

T_OUT  = 'analytics_proverka_big_analytics'
T_FACT = 'fact_big_analytics'   # стабильный источник BI (public schema, ad_analytics_bi)

DATE_START = date(2026, 1, 1)

SPEND_THRESHOLD_PCT = 5.0    # порог |Δ%| расхода для вердикта ⚠️
LEADS_THRESHOLD_PCT = 10.0   # порог |Δ%| заявок для вердикта ⚠️

# Метка для non-auto/несматченных csd-кампаний (справочная строка)
NON_AUTO_LABEL = '__non_auto_residual__'


# ── DDL ──────────────────────────────────────────────────────────────────────
_DDL_SQL = f"""
DROP TABLE IF EXISTS {T_OUT};
CREATE TABLE {T_OUT} (
    crm_name             TEXT     NOT NULL,
    month                DATE     NOT NULL,
    -- CRM-сторона (campaign_stats_daily, только matched cid)
    csd_spend            NUMERIC,
    -- BI-сторона (fact_big_analytics direct+tp8)
    bi_spend             NUMERIC,
    bi_direct_spend      NUMERIC,
    bi_tp8_spend         NUMERIC,
    bi_leads             BIGINT,
    bi_direct_leads      BIGINT,
    bi_tp8_leads         BIGINT,
    bi_korr              BIGINT,
    bi_visits            BIGINT,
    bi_sales             BIGINT,
    -- Дельта (matched csd vs bi direct+tp8)
    diff_spend           NUMERIC,
    -- Количество BI-доменов этой CRM
    bi_domain_count      INTEGER,
    generated_at         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (crm_name, month)
);
CREATE INDEX IF NOT EXISTS idx_proverka_crm   ON {T_OUT} (crm_name);
CREATE INDEX IF NOT EXISTS idx_proverka_month ON {T_OUT} (month);
"""


# ── 0) АВТО-ДОМЕНЫ из gsheet: множество разрешённых доменов ─────────────────
# Нормализация: lower(trim(niche))='авто'.
# Из-за Postgres регистрочувствительности сравниваем с 'авто' (уже lower).
_AUTO_DOMAINS_SQL = """
SELECT DISTINCT lower(trim(domain)) AS dom
FROM local_gsheet_sites
WHERE lower(trim(niche)) = 'авто'
  AND domain IS NOT NULL
  AND domain <> '';
"""


# ── 1) АВТОРИТЕТНАЯ CRM-MAP: dominant-by-spend per domain ────────────────────
# Поле "Название crm" из факта, агрегированный по домену.
# DISTINCT ON (dom) + ORDER BY cost DESC даёт 1:1 без задвоений.
# Включаем только авто-домены из gsheet — явная фильтрация ниши.
# %(auto_domains)s — список авто-доменов (lower/trim).
_CRM_MAP_SQL = f"""
WITH d AS (
    SELECT
        lower(trim(domain))   AS dom,
        "Название crm"        AS crm,
        SUM(total_cost)       AS cost,
        SUM(kol_vo_zayavok)   AS zay
    FROM {T_FACT}
    WHERE _source_table = 'direct'
      AND domain IS NOT NULL AND domain <> ''
      AND "Название crm" IS NOT NULL AND "Название crm" <> ''
      AND lower(trim(domain)) = ANY(%(auto_domains)s)
    GROUP BY 1, 2
)
SELECT DISTINCT ON (dom) dom, crm
FROM d
ORDER BY dom, cost DESC, zay DESC;
"""


# ── 2) BI-СТОРОНА: расход+воронка по доменам CRM (direct+tp8+tp9+tp10 вместе) ──
# Агрегируем direct и tp8/tp9/tp10 раздельно (tp8/tp9/tp10 суммируются в tp8_spend/tp8_leads).
# tp9 (Max) и tp10 (Telegram+Max) — billит Яндекс через csd, поэтому входят в BI-сторону.
# Домены уже отфильтрованы через crm_domains (все в авто-нише).
_BI_PERIOD_SQL = f"""
SELECT
    COALESCE(SUM(CASE WHEN _source_table='direct'                      THEN total_cost     ELSE 0 END), 0) AS direct_spend,
    COALESCE(SUM(CASE WHEN _source_table IN ('tp8','tp9','tp10')       THEN total_cost     ELSE 0 END), 0) AS tp8_spend,
    COALESCE(SUM(CASE WHEN _source_table='direct'                      THEN kol_vo_zayavok ELSE 0 END), 0) AS direct_leads,
    COALESCE(SUM(CASE WHEN _source_table IN ('tp8','tp9','tp10')       THEN kol_vo_zayavok ELSE 0 END), 0) AS tp8_leads,
    COALESCE(SUM(CASE WHEN _source_table='direct'                      THEN korr           ELSE 0 END), 0) AS korr,
    COALESCE(SUM(CASE WHEN _source_table='direct'                      THEN priezd         ELSE 0 END), 0) AS visits,
    COALESCE(SUM(CASE WHEN _source_table='direct'                      THEN prodazhi       ELSE 0 END), 0) AS sales
FROM {T_FACT}
WHERE lower(trim(domain)) = ANY(%(domains)s)
  AND _source_table IN ('direct', 'tp8', 'tp9', 'tp10')
  AND "Date" >= %(date_start)s
  AND "Date" <= %(date_end)s;
"""


# ── 3) CRM-СТОРОНА: dominant_domain per cid из BI (direct+tp8) ───────────────
# cid → dominant_domain → crm через CRM-map.
# Несматченные cid (не в BI direct+tp8 авто-доменов) → non-auto residual.
# campaign_stats_daily.campaign_id — VARCHAR; "CampaignId" в факте — bigint → каст ::text.
#
# v7: дедуп по (campaign_id, day) через внутренний CTE.
# Дубли в campaign_stats_daily — повторённые одинаковые строки за один день.
# MAX(total_spend) per (campaign_id, day) = фактический дневной расход (совпадает с Я.Директ API).
# Внешний SUM суммирует уже дедуп-строки.
# Дополнительно возвращаем raw-сумму (до дедупа) для расчёта "срезанных дублей" в индикаторе доверия.
#
# Возвращает 4 колонки:
#   cid_text      — campaign_id::text
#   csd_spend     — расход ПОСЛЕ дедупа (SUM of MAX per day)
#   csd_raw       — расход ДО дедупа (SUM of all rows per campaign)
#   dup_keys      — число (campaign_id,day)-ключей с дублями (> 1 строки в день)
_CSD_PERIOD_SQL = """
WITH daily AS (
    SELECT
        campaign_id::text               AS cid_text,
        day,
        MAX(total_spend)                AS spend_day,
        SUM(total_spend)                AS spend_day_raw,
        COUNT(*)                        AS row_cnt
    FROM campaign_stats_daily
    WHERE total_spend > 0
      AND day >= %(date_start)s
      AND day <= %(date_end)s
    GROUP BY campaign_id, day
)
SELECT
    cid_text,
    SUM(spend_day)                      AS csd_spend,
    SUM(spend_day_raw)                  AS csd_raw,
    COUNT(*) FILTER (WHERE row_cnt > 1) AS dup_keys
FROM daily
GROUP BY cid_text;
"""

# Dominant domain per cid из BI (direct+tp8+tp9+tp10) — строим один раз за весь период.
# Нужен для маппинга cid → CRM.
# Фильтр по авто-доменам: cid → dom → если dom в авто-нише → mapped; иначе → residual.
# %(auto_domains)s — список авто-доменов.
_CID_DOMAIN_MAP_SQL = f"""
WITH cid_dom AS (
    SELECT
        "CampaignId"::text            AS cid_text,
        lower(trim(domain))           AS dom,
        SUM(total_cost)               AS cost
    FROM {T_FACT}
    WHERE _source_table IN ('direct', 'tp8', 'tp9', 'tp10')
      AND "CampaignId" IS NOT NULL
      AND domain IS NOT NULL AND domain <> ''
      AND lower(trim(domain)) = ANY(%(auto_domains)s)
    GROUP BY 1, 2
)
SELECT DISTINCT ON (cid_text) cid_text, dom
FROM cid_dom
ORDER BY cid_text, cost DESC;
"""

# ── 3b) BI расход per cid за ВЕСЬ период — для концентрации Δ ────────────────
# Только авто-домены (via cid_domain_map), только matched cid в нужных CRM.
# Запрашивается один раз (весь период), не помесячно.
# %(auto_domains)s — список авто-доменов.
# %(date_start)s / %(date_end)s — границы периода.
_BI_CID_PERIOD_SQL = f"""
SELECT
    "CampaignId"::text          AS cid_text,
    SUM(total_cost)             AS bi_spend
FROM {T_FACT}
WHERE _source_table IN ('direct', 'tp8', 'tp9', 'tp10')
  AND "CampaignId" IS NOT NULL
  AND lower(trim(domain)) = ANY(%(auto_domains)s)
  AND "Date" >= %(date_start)s
  AND "Date" <= %(date_end)s
GROUP BY 1;
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _month_ranges(start: date, end: date):
    """[(month_start, month_end_inclusive), ...]. Текущий месяц обрезан до end."""
    out = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        last_day = calendar.monthrange(cur.year, cur.month)[1]
        m_end = min(date(cur.year, cur.month, last_day), end)
        out.append((cur, m_end))
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return out


def _to_int(v):
    return int(v) if v is not None else 0


def _to_dec(v):
    return Decimal(str(v)) if v is not None else Decimal('0')


def _pct(a, b):
    """Δ% = (a - b) / b * 100. None если b==0."""
    fa, fb = float(a), float(b)
    if fb == 0:
        return None
    return 100.0 * (fa - fb) / fb


def _verdict(pct_val, threshold):
    if pct_val is None:
        return '?'
    return '✅' if abs(pct_val) <= threshold else '⚠️'


def _fmt_money(v):
    return f'{float(v):,.0f}'.replace(',', ' ')


def _fmt_int(v):
    return f'{int(v):,}'.replace(',', ' ')


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _fetch_auto_domains(bi_conn):
    """Строим множество авто-доменов из local_gsheet_sites WHERE niche='Авто'."""
    with bi_conn.cursor() as cur:
        cur.execute(_AUTO_DOMAINS_SQL)
        rows = cur.fetchall()
    return {row[0] for row in rows}


def _fetch_crm_map(bi_conn, auto_domains):
    """Строим авторитетный dict dom → crm (dominant-by-spend из факта, только авто-домены)."""
    with bi_conn.cursor() as cur:
        cur.execute(_CRM_MAP_SQL, {'auto_domains': list(auto_domains)})
        rows = cur.fetchall()
    return {dom: crm for dom, crm in rows}


def _fetch_cid_domain_map(bi_conn, auto_domains):
    """Dominant domain per cid из BI (direct+tp8), только авто-домены. dict cid_text → dom."""
    with bi_conn.cursor() as cur:
        cur.execute(_CID_DOMAIN_MAP_SQL, {'auto_domains': list(auto_domains)})
        rows = cur.fetchall()
    return {cid: dom for cid, dom in rows}


def _fetch_bi_cid_period(bi_conn, auto_domains, d_start, d_end):
    """BI расход per cid за весь период (только авто-домены). dict cid_text → Decimal."""
    with bi_conn.cursor() as cur:
        cur.execute(_BI_CID_PERIOD_SQL, {
            'auto_domains': list(auto_domains),
            'date_start': d_start,
            'date_end': d_end,
        })
        rows = cur.fetchall()
    return {cid: _to_dec(sp) for cid, sp in rows}


def _fetch_bi_period(bi_conn, domains, d_start, d_end):
    """BI direct+tp8 за период по списку доменов. Returns dict полей."""
    with bi_conn.cursor() as cur:
        cur.execute(_BI_PERIOD_SQL, {
            'domains': list(domains),
            'date_start': d_start,
            'date_end': d_end,
        })
        r = cur.fetchone()
    return {
        'direct_spend': _to_dec(r[0]),
        'tp8_spend':    _to_dec(r[1]),
        'direct_leads': _to_int(r[2]),
        'tp8_leads':    _to_int(r[3]),
        'korr':         _to_int(r[4]),
        'visits':       _to_int(r[5]),
        'sales':        _to_int(r[6]),
    }


def _fetch_csd_period(src_conn, d_start, d_end):
    """CSD расход за период (v7 дедуп по (cid,day)).

    Возвращает dict cid_text → {
        'spend':    Decimal — дедуп-расход (SUM of MAX per day),
        'raw':      Decimal — сырой расход (без дедупа),
        'dup_keys': int    — число (cid,day)-ключей с дублями,
    }
    """
    with src_conn.cursor() as cur:
        cur.execute(_CSD_PERIOD_SQL, {'date_start': d_start, 'date_end': d_end})
        rows = cur.fetchall()
    return {
        cid: {
            'spend':    _to_dec(sp),
            'raw':      _to_dec(raw),
            'dup_keys': int(dk) if dk is not None else 0,
        }
        for cid, sp, raw, dk in rows
    }


# ── Telegram ──────────────────────────────────────────────────────────────────

def _send_telegram(text, bot_token, chat_id):
    """Разбиваем на чанки по 4000 символов, шлём прямым Bot API."""
    try:
        from config.tokens import TELEGRAM_PROXY as _proxy
    except Exception:
        _proxy = None
    url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
    chunks = []
    cur_chunk = ''
    for line in text.split('\n'):
        if len(cur_chunk) + len(line) + 1 > 4000:
            chunks.append(cur_chunk)
            cur_chunk = ''
        cur_chunk += line + '\n'
    if cur_chunk:
        chunks.append(cur_chunk)
    proxy_variants = [{'https': _proxy}] if _proxy else []
    for c in chunks:
        sent = False
        for proxies in proxy_variants + [None]:
            try:
                resp = requests.post(
                    url,
                    json={'chat_id': chat_id, 'text': c,
                          'disable_web_page_preview': True},
                    timeout=15,
                    proxies=proxies,
                )
                if resp.ok:
                    sent = True
                    break
                logger.warning('Telegram %s: %s', resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning('Telegram (proxies=%s): %s', proxies, e)
        if not sent:
            logger.warning('Telegram чанк не отправлен')


# ── Telegram-текст ─────────────────────────────────────────────────────────────

def _build_telegram_text(by_crm, non_auto_total, run_id, end_date, auto_domain_count,
                         trust_concentration=None):
    """
    Структура:
      [шапка — методология v7 с дедупом csd]
      ━━━ РАСХОД (csd matched vs BI direct+tp8) ━━━
        per CRM строки + вердикт + trust-индикатор (дубли csd + концентрация Δ)
        ИТОГО matched
      ━━━ ЗАЯВКИ BI (direct-грань) ━━━
        per CRM
      ━━━ СПРАВОЧНО ━━━
        BI воронка (визиты/продажи) + разбивка direct/tp8 + non-auto residual
    """
    NOISE_PCT = 2.0  # порог шума для показа trust-индикатора (не показываем для ✅ <2%)
    CONC_THRESHOLD = 0.20  # top-1 cid > 20% |Δ| → концентрация ⚠

    lines = []
    lines.append('📊 Сверка big_analytics vs CSD (v7 — дедуп csd по (cid,day), ниша Авто)')
    lines.append(f'Период: {DATE_START.isoformat()} .. {end_date.isoformat()}')
    if run_id:
        lines.append(f'run_id: {run_id}')
    lines.append('')
    lines.append('Методология v7:')
    lines.append(f'  Ниша     = ТОЛЬКО Авто (local_gsheet_sites niche=\'Авто\', {auto_domain_count} доменов)')
    lines.append('  CRM-map  = dominant "Название crm" per domain из fact_big_analytics (1:1, только авто)')
    lines.append('  BI       = _source_table IN (\'direct\',\'tp8\') → расход+заявки (только авто-домены)')
    lines.append('  CSD      = MAX(spend) per (cid,day) → SUM (дедуп дублей campaign_stats_daily)')
    lines.append('  Периметр = apples-to-apples: только matched авто-cid (вне → non-auto residual)')
    lines.append('  Δ%       = (csd_dedup − bi_direct+tp8) / bi_direct+tp8 × 100')
    lines.append('')

    # Сортировка CRM по убыванию BI-расхода
    crm_list = sorted(by_crm.keys(), key=lambda c: float(by_crm[c]['bi_spend']), reverse=True)

    # ── РАСХОД ───────────────────────────────────────────────────────────────
    lines.append('━━━ РАСХОД (CSD dedup vs BI direct+tp8) ━━━')

    total_csd = Decimal('0')
    total_bi  = Decimal('0')

    for crm in crm_list:
        a = by_crm[crm]
        csd_sp = a['csd_spend']
        bi_sp  = a['bi_spend']
        total_csd += csd_sp
        total_bi  += bi_sp
        pct = _pct(csd_sp, bi_sp)
        verd = _verdict(pct, SPEND_THRESHOLD_PCT)
        d = float(csd_sp) - float(bi_sp)
        pct_s = f'{pct:+.1f}%' if pct is not None else '—'
        n_dom = a['domain_count']

        if verd == '✅':
            lines.append(
                f'  {crm}: {verd} CSD {_fmt_money(csd_sp)} / BI {_fmt_money(bi_sp)} / '
                f'Δ {d:+,.0f} ({pct_s})  [{n_dom} дом.]'
            )
        else:
            lines.append(f'  {crm}: {verd}  [{n_dom} дом.]')
            lines.append(f'    CSD {_fmt_money(csd_sp)} / BI {_fmt_money(bi_sp)} / Δ {d:+,.0f} ({pct_s})')
            # Разбивка BI direct / tp8 при ⚠️ для диагностики
            d_sp = a['bi_direct_spend']
            t_sp = a['bi_tp8_spend']
            if t_sp > 0:
                lines.append(f'    (BI: direct {_fmt_money(d_sp)} + tp8 {_fmt_money(t_sp)})')

        # ── Trust-индикатор: показываем если |Δ%| > NOISE_PCT ────────────────
        show_trust = (pct is not None and abs(pct) > NOISE_PCT)
        if show_trust:
            # а) Дубли csd
            dup_keys = a.get('trust_dup_keys', 0)
            dedup_cut = a.get('trust_dedup_cut', Decimal('0'))
            if dup_keys > 0:
                lines.append(
                    f'    csd дубли: {dup_keys} (cid,day)-ключей, −{_fmt_money(dedup_cut)} ₽ срезано дедупом'
                )
            else:
                lines.append('    csd дубли: не обнаружены')

            # б) Концентрация остаточного Δ
            if trust_concentration and crm in trust_concentration:
                tc = trust_concentration[crm]
                n_cid = tc['n_cid']
                top1_diff = tc.get('top1_diff', Decimal('0'))
                top1_cid  = tc.get('top1_cid', '?')
                total_delta = Decimal(str(d))
                if n_cid > 0 and abs(float(total_delta)) > 1:
                    frac = abs(float(top1_diff)) / abs(float(total_delta)) if float(total_delta) != 0 else 0
                    # CONC_FLAG_EXPLAIN_2026-07-02: пояснение что значит «концентрация» и нужно ли действие.
                    # концентрация = >20% Δ в 1 кампании → легче найти причину (разобрать эту кампанию).
                    # размазано = расхождение равномерно по многим кампаниям → системная разница (НДС, период).
                    if frac > CONC_THRESHOLD:
                        conc_flag = f'⚠ концентрация: {frac*100:.0f}% Δ в 1 кампании → нужен разбор cid'
                    else:
                        conc_flag = 'размазано по кампаниям (системная разница, не аномалия)'
                    lines.append(
                        f'    Δ по {n_cid} cid; top-1 cid={top1_cid}: {float(top1_diff):+,.0f} ₽; '
                        f'{conc_flag}'
                    )

    lines.append('')
    sp_pct = _pct(total_csd, total_bi)
    sp_pct_s = f'{sp_pct:+.1f}%' if sp_pct is not None else '—'
    sp_verd = _verdict(sp_pct, SPEND_THRESHOLD_PCT)
    lines.append(
        f'  ИТОГО matched: CSD {_fmt_money(total_csd)} / BI {_fmt_money(total_bi)} / '
        f'Δ {sp_pct_s} {sp_verd}'
    )
    # RECON_SUMMARY_2026-07-02: явная итоговая строка — понятно сразу есть проблема или нет.
    problem_crms = [
        crm for crm in crm_list
        if _verdict(_pct(by_crm[crm]['csd_spend'], by_crm[crm]['bi_spend']), SPEND_THRESHOLD_PCT) != '✅'
    ]
    if not problem_crms:
        lines.append(f'  ✅ Все агентства в допуске (порог ±{SPEND_THRESHOLD_PCT:.0f}%) — действий не требуется')
    else:
        lines.append(f'  ⚠️ Вне допуска (>{SPEND_THRESHOLD_PCT:.0f}%): {", ".join(problem_crms)} — нужен разбор')
    lines.append('')

    # ── ЗАЯВКИ BI (direct-грань) ──────────────────────────────────────────────
    lines.append('━━━ ЗАЯВКИ BI (direct-грань) ━━━')
    lines.append('  Примечание: BI по дате клика; CRM — по дате создания. Разница до 10% нормальна.')
    for crm in crm_list:
        a = by_crm[crm]
        ld = a['bi_direct_leads']
        ko = a['bi_korr']
        lines.append(f'  {crm}: заявки {_fmt_int(ld)} / квал {_fmt_int(ko)}')
    lines.append('')

    # ── СПРАВОЧНО ─────────────────────────────────────────────────────────────
    lines.append('━━━ СПРАВОЧНО (без Δ) ━━━')
    lines.append('')

    lines.append('BI воронка (fact_big_analytics direct, пиксельная атрибуция):')
    for crm in crm_list:
        a = by_crm[crm]
        lines.append(
            f'  {crm}: визиты {_fmt_int(a["bi_visits"])} / продажи {_fmt_int(a["bi_sales"])}'
        )
    lines.append('  * Маркар: продажи обогащены из gsheet_priezdi_marcar — CRM этого не видит.')
    lines.append('')

    lines.append('BI расход direct vs tp8 (справочно):')
    for crm in crm_list:
        a = by_crm[crm]
        t_sp = a['bi_tp8_spend']
        t_ld = a['bi_tp8_leads']
        if t_sp > 0 or t_ld > 0:
            lines.append(
                f'  {crm}: direct {_fmt_money(a["bi_direct_spend"])} / '
                f'tp8 {_fmt_money(t_sp)} (заявки tp8: {_fmt_int(t_ld)})'
            )
    lines.append('')

    lines.append('CSD non-auto residual (вне авто-периметра gsheet, справочно — в Δ НЕ входит):')
    lines.append(f'  Расход non-auto: {_fmt_money(non_auto_total)} ₽')
    lines.append('  (cid→dom не в авто-нише gsheet — SEO, brand-кампании вне CRM и т.д.)')
    lines.append('')

    lines.append('Источники: fact_big_analytics (BI) | campaign_stats_daily (CSD, ad_analytics) | local_gsheet_sites (ниша Авто)')
    return '\n'.join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(conn, run_id=None, **kwargs) -> dict:
    """conn — ad_analytics_bi (read+write). ad_analytics — отдельный коннект."""
    t0 = time.perf_counter()

    from config.settings import DB_SRC
    from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    today = date.today()
    months = _month_ranges(DATE_START, today)
    logger.info('Шаг 12 v6 niche-auto dominant-CRM: %d месяцев', len(months))

    # 1) DDL
    with conn.cursor() as cur:
        cur.execute(_DDL_SQL)
    conn.commit()

    # 2) Авто-домены из gsheet
    auto_domains = _fetch_auto_domains(conn)
    logger.info('Авто-доменов из gsheet (niche=Авто): %d', len(auto_domains))
    if not auto_domains:
        logger.warning('Авто-домены пусты — local_gsheet_sites пустой или niche не совпадает?')
        return {'rows': 0, 'details': 'auto_domains empty'}

    # 3) Авторитетная CRM-map из факта: dom → crm (только авто-домены)
    crm_map = _fetch_crm_map(conn, auto_domains)
    logger.info('CRM-map (авто): %d доменов', len(crm_map))
    if not crm_map:
        logger.warning('CRM-map пуст — нет пересечения факта с авто-доменами?')
        return {'rows': 0, 'details': 'crm_map empty'}

    # Инвертируем: crm → set(domains)
    crm_domains: dict[str, set] = {}
    for dom, crm in crm_map.items():
        crm_domains.setdefault(crm, set()).add(dom)

    crm_list = sorted(crm_domains.keys())
    logger.info('Уникальных CRM: %d', len(crm_list))

    # 4) cid → dominant_domain map из BI (direct+tp8), только авто-домены
    cid_domain_map = _fetch_cid_domain_map(conn, auto_domains)
    # cid → crm (через dom → crm_map)
    cid_crm_map: dict[str, str] = {}
    for cid, dom in cid_domain_map.items():
        crm = crm_map.get(dom)
        if crm:
            cid_crm_map[cid] = crm
    logger.info('cid→crm map (авто): %d matched из %d cid', len(cid_crm_map), len(cid_domain_map))

    # 5) Сборка строк по месяцам
    src_conn = psycopg2.connect(**DB_SRC)
    src_conn.set_session(readonly=True, autocommit=True)

    # Структура накопления: crm → dict с суммарными значениями
    by_crm: dict[str, dict] = {}
    for crm in crm_list:
        by_crm[crm] = {
            'csd_spend':       Decimal('0'),
            'csd_raw':         Decimal('0'),   # v7: сырой расход до дедупа
            'bi_direct_spend': Decimal('0'),
            'bi_tp8_spend':    Decimal('0'),
            'bi_spend':        Decimal('0'),   # direct+tp8
            'bi_direct_leads': 0,
            'bi_tp8_leads':    0,
            'bi_leads':        0,
            'bi_korr':         0,
            'bi_visits':       0,
            'bi_sales':        0,
            'domain_count':    len(crm_domains[crm]),
            # v7 trust-индикатор
            'trust_dup_keys':  0,   # общее число (cid,day)-ключей с дублями по CRM
            'trust_dedup_cut': Decimal('0'),  # csd_raw - csd_spend по CRM (срезано дедупом)
        }
    non_auto_total = Decimal('0')  # CSD расход вне авто-периметра (несматченные cid)

    # v7: накапливаем csd per cid за весь период для концентрации Δ
    # csd_by_cid_total[cid] = dedup spend за период
    csd_by_cid_total: dict[str, Decimal] = {}

    rows_out = []
    try:
        for m_start, m_end in months:
            logger.info('Месяц %s..%s', m_start, m_end)

            # BI per CRM (только авто-домены этой CRM)
            bi_by_crm_month: dict[str, dict] = {}
            for crm in crm_list:
                domains = list(crm_domains[crm])
                bi = _fetch_bi_period(conn, domains, m_start, m_end)
                bi_by_crm_month[crm] = bi

            # CSD за месяц (v7): cid_text → {spend, raw, dup_keys}
            csd_month = _fetch_csd_period(src_conn, m_start, m_end)

            # Раскидываем CSD по CRM через cid_crm_map (только авто cid)
            csd_by_crm_month: dict[str, Decimal] = {c: Decimal('0') for c in crm_list}
            csd_raw_by_crm_month: dict[str, Decimal] = {c: Decimal('0') for c in crm_list}
            dup_keys_by_crm_month: dict[str, int] = {c: 0 for c in crm_list}
            non_auto_month = Decimal('0')
            for cid, info in csd_month.items():
                sp = info['spend']
                raw = info['raw']
                dk = info['dup_keys']
                crm = cid_crm_map.get(cid)
                if crm and crm in csd_by_crm_month:
                    csd_by_crm_month[crm] += sp
                    csd_raw_by_crm_month[crm] += raw
                    dup_keys_by_crm_month[crm] += dk
                    # накапливаем cid total за период
                    csd_by_cid_total[cid] = csd_by_cid_total.get(cid, Decimal('0')) + sp
                else:
                    non_auto_month += sp

            non_auto_total += non_auto_month

            # Аккумулируем и собираем строки для INSERT
            for crm in crm_list:
                bi  = bi_by_crm_month[crm]
                csd = csd_by_crm_month[crm]
                csd_raw = csd_raw_by_crm_month[crm]
                d_sp = bi['direct_spend']
                t_sp = bi['tp8_spend']
                bi_sp = d_sp + t_sp
                d_ld = bi['direct_leads']
                t_ld = bi['tp8_leads']

                by_crm[crm]['csd_spend']       += csd
                by_crm[crm]['csd_raw']         += csd_raw
                by_crm[crm]['bi_direct_spend']  += d_sp
                by_crm[crm]['bi_tp8_spend']     += t_sp
                by_crm[crm]['bi_spend']         += bi_sp
                by_crm[crm]['bi_direct_leads']  += d_ld
                by_crm[crm]['bi_tp8_leads']     += t_ld
                by_crm[crm]['bi_leads']         += d_ld + t_ld
                by_crm[crm]['bi_korr']          += bi['korr']
                by_crm[crm]['bi_visits']        += bi['visits']
                by_crm[crm]['bi_sales']         += bi['sales']
                by_crm[crm]['trust_dup_keys']   += dup_keys_by_crm_month[crm]
                by_crm[crm]['trust_dedup_cut']  += (csd_raw - csd)

                rows_out.append({
                    'crm':            crm,
                    'month':          m_start,
                    'csd_spend':      csd,
                    'bi_spend':       bi_sp,
                    'bi_direct_spend': d_sp,
                    'bi_tp8_spend':   t_sp,
                    'bi_leads':       d_ld + t_ld,
                    'bi_direct_leads': d_ld,
                    'bi_tp8_leads':   t_ld,
                    'bi_korr':        bi['korr'],
                    'bi_visits':      bi['visits'],
                    'bi_sales':       bi['sales'],
                    'diff_spend':     csd - bi_sp,
                    'domain_count':   len(crm_domains[crm]),
                })

        # non-auto residual — добавляем как агрегированную строку справочно
        # Сохраняем с month=DATE_START чтобы был уникальный PK
        rows_out.append({
            'crm':            NON_AUTO_LABEL,
            'month':          DATE_START,
            'csd_spend':      non_auto_total,
            'bi_spend':       Decimal('0'),
            'bi_direct_spend': Decimal('0'),
            'bi_tp8_spend':   Decimal('0'),
            'bi_leads':       0,
            'bi_direct_leads': 0,
            'bi_tp8_leads':   0,
            'bi_korr':        0,
            'bi_visits':      0,
            'bi_sales':       0,
            'diff_spend':     non_auto_total,
            'domain_count':   0,
        })

    finally:
        src_conn.close()

    # 6) BI per cid за весь период (для концентрации Δ в trust-индикаторе)
    bi_cid_total = _fetch_bi_cid_period(conn, auto_domains, DATE_START, today)

    # Строим trust-индикатор концентрации: per CRM → top-1 cid по |csd_dedup_cid - bi_cid|
    trust_concentration: dict[str, dict] = {}
    for crm in crm_list:
        # все cid этой CRM
        crm_cids = [cid for cid, c in cid_crm_map.items() if c == crm]
        if not crm_cids:
            trust_concentration[crm] = {'n_cid': 0, 'top1_cid': None, 'top1_diff': Decimal('0')}
            continue
        diffs = []
        for cid in crm_cids:
            csd_cid = csd_by_cid_total.get(cid, Decimal('0'))
            bi_cid  = bi_cid_total.get(cid, Decimal('0'))
            diffs.append((cid, csd_cid - bi_cid))
        diffs.sort(key=lambda x: abs(x[1]), reverse=True)
        top1_cid, top1_diff = diffs[0]
        trust_concentration[crm] = {
            'n_cid':     len(crm_cids),
            'top1_cid':  top1_cid,
            'top1_diff': top1_diff,
        }

    # 7) INSERT
    with conn.cursor() as cur:
        cur.executemany(f"""
            INSERT INTO {T_OUT} (
                crm_name, month,
                csd_spend,
                bi_spend, bi_direct_spend, bi_tp8_spend,
                bi_leads, bi_direct_leads, bi_tp8_leads,
                bi_korr, bi_visits, bi_sales,
                diff_spend, bi_domain_count
            ) VALUES (
                %(crm)s, %(month)s,
                %(csd_spend)s,
                %(bi_spend)s, %(bi_direct_spend)s, %(bi_tp8_spend)s,
                %(bi_leads)s, %(bi_direct_leads)s, %(bi_tp8_leads)s,
                %(bi_korr)s, %(bi_visits)s, %(bi_sales)s,
                %(diff_spend)s, %(domain_count)s
            )
        """, rows_out)
    conn.commit()
    logger.info('INSERT %d строк в %s', len(rows_out), T_OUT)

    # 8) Telegram
    text = _build_telegram_text(
        by_crm, non_auto_total, run_id, today, len(auto_domains),
        trust_concentration=trust_concentration,
    )
    _send_telegram(text, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    elapsed = time.perf_counter() - t0
    n = len(rows_out)
    logger.info('Шаг 12 v7 csd-dedup trust-indicator завершён: %d строк за %.1f сек', n, elapsed)
    return {'rows': n, 'details': f'{T_OUT}: {n} строк (v7 csd-dedup trust-indicator)'}


if __name__ == '__main__':
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config.db as db_module

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )
    db_module.init_pool()
    _conn = db_module.get_conn()
    try:
        rid = datetime.now().strftime('%Y%m%d_%H%M%S')
        result = run(_conn, run_id=rid)
        print(result)
    finally:
        db_module.put_conn(_conn)
        db_module.close_pool()
