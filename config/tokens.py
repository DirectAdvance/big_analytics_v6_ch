"""
tokens.py — API-ключи и адреса внешних сервисов
"""
import sys
from pathlib import Path

for _p in Path(__file__).resolve().parents:
    if (_p / '.secret' / 'loader.py').exists():
        sys.path.insert(0, str(_p / '.secret'))
        break
from loader import (  # noqa: E402
    load_auto_bi_analytics_telegram, load_yandex_direct, load_yandex_direct_reviews, load_yandex_metrika,
    tg_proxy_variants,
)

# ── Telegram (прямой API, без gateway) ───────────────────────────────────────
_TG = load_auto_bi_analytics_telegram()
TELEGRAM_BOT_TOKEN = _TG['bot_token']
TELEGRAM_CHAT_ID   = _TG['chat_id']
# Цепочка для ротации: Amsterdam(10808)→DE(10830)→NL(10831)→FR(10832)→direct
# Формат: [{'https':p,'http':p}, ..., None] — готово для requests.post(proxies=...)
TELEGRAM_PROXY_VARIANTS = tg_proxy_variants()  # TG_PROXY_CHAIN_ROTATION_2026-06-17
# Первый/основной прокси как строка (backward-compat для step12/pipeline_night).
# load_auto_bi_analytics_telegram не возвращает 'proxy', берём из цепочки.
TELEGRAM_PROXY = _TG.get('proxy') or (TELEGRAM_PROXY_VARIANTS[0] or {}).get('https')

# Оставляем для обратной совместимости (step7 импортирует)
TELEGRAM_GATEWAY_URL = None
TELEGRAM_GATEWAY_KEY = None

# ── Яндекс.Директ ─────────────────────────────────────────────────────────────
_D = load_yandex_direct()
_t = _D['tokens']

OAUTH_TOKEN_1 = _t['victorylotsofads1']['oauth_token']
OAUTH_TOKEN_2 = _t['victoryagency-direct1618440']['oauth_token']
OAUTH_TOKEN_3 = _t['y-direct-victory']['oauth_token']
OAUTH_TOKEN_4 = _t['victoryagency14']['oauth_token']
OAUTH_TOKEN_5 = _t['useful-call-agency']['oauth_token']

# Агентские токены для direct_account_reviews (отдельная секция в .env)
# Формат: [(oauth_token, login), ...]
REVIEWS_TOKENS = load_yandex_direct_reviews()

# ── Яндекс.Метрика ─────────────────────────────────────────────────────────────
_M = load_yandex_metrika()
METRIKA_TOKEN       = _M['oauth_token']
METRIKA_TOKEN_AUDIT = _M['audit_token']  # victorylotsofads04 — отдельная квота для utm_direct_audit
METRIKA_TOKEN_3     = _M['token_3']      # skuderko1 — третий токен для round-robin
