#!/usr/bin/env python3
"""
refresh_powerbi.py — только обновление отчётов Power BI Service.

Запуск:
    ~/venv/bin/python3 refresh_powerbi.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── Loader discovery: работает и на Mac (корень репо), и на Victory (home) ────
# FIX-LOADER-DISCOVERY-2026-06-15
_SECRET_DIR: Path | None = None
for _p in Path(__file__).resolve().parents:
    if (_p / '.secret' / 'loader.py').exists():
        _SECRET_DIR = _p / '.secret'
        break
if _SECRET_DIR is None:
    raise RuntimeError(
        'refresh_powerbi: .secret/loader.py не найден ни в одном из родительских каталогов. '
        'Запускай из корня репо (Mac) или из ~/big_analytics_v5 (Victory).'
    )
if str(_SECRET_DIR) not in sys.path:
    sys.path.insert(0, str(_SECRET_DIR))

from loader import load_powerbi, load_db  # type: ignore  # noqa: E402

from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

_NULL_GATEWAY_ID = '00000000-0000-0000-0000-000000000000'

# Канонический хост Victory для облачного датасета Power BI.
# Голый IP 103.88.240.90 падает по «remote certificate invalid» (CN сертификата
# Let's Encrypt = analytics-marketing.ru), а внутренний Parallels-IP 10.211.55.2
# (наследуется при публикации модели из Desktop) недостижим из облачного шлюза.
# Поэтому ребайнд всегда на доменное имя. database всегда ad_analytics_bi.
CANONICAL_PG_HOST = 'analytics-marketing.ru'
CANONICAL_PG_DATABASE = 'ad_analytics_bi'


def _send_telegram(text: str) -> None:
    """Отправка в Telegram: direct first, proxy fallback.

    Локальные SOCKS-прокси на Mac часто не подняты. Для Power BI refresh это не
    ошибка самого refresh, поэтому не шумим warning на каждый отказ прокси.
    """
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    payload = json.dumps({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
    }).encode()
    proxy_chain = [None] + [p for p in TELEGRAM_PROXY_VARIANTS if p is not None]
    last_error = None
    for proxies in proxy_chain:
        try:
            r = requests.post(url, data=payload,
                              headers={'Content-Type': 'application/json'},
                              proxies=proxies, timeout=10)
            if r.status_code == 200:
                return
            last_error = f'HTTP {r.status_code}: {r.text[:200]}'
        except Exception as e:
            last_error = str(e)
    logger.warning('Telegram send failed after %d attempts: %s', len(proxy_chain), last_error)


def _load_powerbi_config() -> dict:
    # FIX-LOADER-DISCOVERY-2026-06-15: больше не tokens.json, читаем из .env через load_powerbi()
    return load_powerbi()


def _get_powerbi_token(pbi: dict) -> str:
    logger.info('Power BI: получение токена ...')
    resp = requests.post(
        f"https://login.microsoftonline.com/{pbi['tenant_id']}/oauth2/v2.0/token",
        data={
            'grant_type': 'client_credentials',
            'client_id': pbi['client_id'],
            'client_secret': pbi['client_secret'],
            'scope': 'https://analysis.windows.net/powerbi/api/.default',
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['access_token']


def _load_pg_credentials() -> tuple[str, str]:
    # FIX-LOADER-DISCOVERY-2026-06-15: используем load_db через уже найденный _SECRET_DIR
    db = load_db('victory')
    return db['user'], db['password']


def _take_over_dataset(pbi: dict, token: str) -> None:
    """Захватить владение датасетом (нужно для UpdateDatasources и PATCH credentials)."""
    workspace_id = pbi['workspace_id']
    dataset_id = pbi['dataset_id']
    try:
        to_resp = requests.post(
            f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}'
            f'/datasets/{dataset_id}/Default.TakeOver',
            headers={'Authorization': f'Bearer {token}'},
            timeout=30,
        )
        if to_resp.status_code == 200:
            logger.info('Power BI: TakeOver выполнен успешно')
        else:
            logger.warning('Power BI: TakeOver вернул %d — %s',
                           to_resp.status_code, to_resp.text[:200])
    except Exception as e:
        logger.warning('Power BI: TakeOver ошибка: %s', e)


def _get_pg_datasources(pbi: dict, token: str) -> list[dict]:
    """Возвращает список PostgreSQL-датасорсов датасета (GET /datasources)."""
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    workspace_id = pbi['workspace_id']
    dataset_id = pbi['dataset_id']
    ds_url = (
        f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}'
        f'/datasets/{dataset_id}/datasources'
    )
    r = requests.get(ds_url, headers=headers, timeout=30)
    r.raise_for_status()
    return [ds for ds in r.json().get('value', []) if ds.get('datasourceType', '').lower() == 'postgresql']


def _ensure_datasource_host(pbi: dict, token: str) -> bool:
    """Идемпотентно чинит host-mismatch облачного датасета (KNOWN_ISSUES #15).

    При публикации модели из Power BI Desktop облачный датасет наследует хост
    публикующей машины (например Parallels-IP 10.211.55.2, localhost или голый
    103.88.240.90) — недостижимый/невалидный по SSL из облачного шлюза → credential
    PATCH 400 (MashupDataAccessError) → refresh Failed (DMTS_DatasourceHasNoCredentialError).

    Эта функция получает datasources датасета и, если у PostgreSQL-датасорса
    server != CANONICAL_PG_HOST, ребайндит его через Default.UpdateDatasources
    (server → analytics-marketing.ru, database остаётся ad_analytics_bi). Если хост
    уже канонический — no-op (только лог). Вызывать ВСЕГДА ПЕРЕД credentials/refresh
    (rebind меняет datasourceId, поэтому credentials привязываются уже после него).

    ВАЖНО: datasourceSelector.connectionDetails должен быть dict-объектом, НЕ
    JSON-строкой — иначе API вернёт 400 BadRequest «Invalid value».

    Возвращает True если rebind был выполнен (нужно перечитать datasources перед
    PATCH credentials), False если rebind не потребовался.
    """
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    workspace_id = pbi['workspace_id']
    dataset_id = pbi['dataset_id']

    try:
        pg_sources = _get_pg_datasources(pbi, token)
    except Exception as e:
        logger.warning('Power BI host-rebind: не удалось получить datasources: %s', e)
        return False

    if not pg_sources:
        logger.info('Power BI host-rebind: PostgreSQL datasources не найдены — пропуск')
        return False

    rebind_details = []
    for ds in pg_sources:
        conn = ds.get('connectionDetails', {}) or {}
        cur_server = conn.get('server', '')
        cur_db = conn.get('database', '') or CANONICAL_PG_DATABASE
        if cur_server == CANONICAL_PG_HOST:
            logger.info('Power BI host-rebind: host OK (server=%s)', cur_server)
            continue
        logger.info('Power BI host-rebind: rebind %s -> %s', cur_server or '<empty>', CANONICAL_PG_HOST)
        # ВАЖНО: connectionDetails в datasourceSelector — dict, не json.dumps(dict).
        # API ожидает объект; строка даёт 400 "Invalid value target=connectionDetails".
        # FIX-REBIND-CONNDETAILS-2026-06-14
        rebind_details.append({
            'datasourceSelector': {
                'datasourceType': 'PostgreSql',
                'connectionDetails': {'server': cur_server, 'database': cur_db},
            },
            'connectionDetails': {
                'server': CANONICAL_PG_HOST,
                'database': CANONICAL_PG_DATABASE,
            },
        })

    if not rebind_details:
        return False

    update_url = (
        f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}'
        f'/datasets/{dataset_id}/Default.UpdateDatasources'
    )
    body = json.dumps({'updateDetails': rebind_details}).encode()
    try:
        ur = requests.post(update_url, data=body, headers=headers, timeout=60)
        if ur.status_code in (200, 202):
            logger.info('Power BI host-rebind: UpdateDatasources OK (HTTP %d) — %d источник(ов) -> %s',
                        ur.status_code, len(rebind_details), CANONICAL_PG_HOST)
            return True
        else:
            logger.warning('Power BI host-rebind: UpdateDatasources вернул %d — %s',
                           ur.status_code, ur.text[:300])
            return False
    except Exception as e:
        logger.warning('Power BI host-rebind: ошибка UpdateDatasources: %s', e)
        return False


def _ensure_powerbi_datasource_credentials(pbi: dict, token: str) -> None:
    try:
        pg_user, pg_pass = _load_pg_credentials()
    except Exception as e:
        logger.warning('Power BI credentials setup: не удалось загрузить pg credentials: %s', e)
        return

    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    workspace_id = pbi['workspace_id']
    dataset_id = pbi['dataset_id']

    ds_url = (
        f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}'
        f'/datasets/{dataset_id}/datasources'
    )
    try:
        r = requests.get(ds_url, headers=headers, timeout=30)
        r.raise_for_status()
        datasources = r.json().get('value', [])
    except Exception as e:
        logger.warning('Power BI credentials setup: не удалось получить datasources: %s', e)
        return

    pg_sources = [ds for ds in datasources if ds.get('datasourceType', '').lower() == 'postgresql']
    if not pg_sources:
        logger.info('Power BI credentials setup: PostgreSQL datasources не найдены — пропуск')
        return

    logger.info('Power BI credentials setup: найдено %d PostgreSQL datasource(s)', len(pg_sources))

    credential_body = json.dumps({
        'credentialDetails': {
            'credentialType': 'Basic',
            'credentials': json.dumps({
                'credentialData': [
                    {'name': 'username', 'value': pg_user},
                    {'name': 'password', 'value': pg_pass},
                ]
            }),
            'encryptedConnection': 'NotEncrypted',
            'encryptionAlgorithm': 'None',
            'privacyLevel': 'Organizational',
            'useEndUserOAuth2Credentials': False,
        }
    })

    for ds in pg_sources:
        gateway_id = ds.get('gatewayId', _NULL_GATEWAY_ID)
        datasource_id = ds.get('datasourceId', '')
        if not datasource_id:
            logger.warning('Power BI credentials setup: datasource без datasourceId, пропуск')
            continue

        if not gateway_id or gateway_id == _NULL_GATEWAY_ID:
            patch_url = (
                f'https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}'
                f'/datasets/{dataset_id}/datasources/{datasource_id}'
            )
            endpoint_type = 'cloud'
        else:
            patch_url = (
                f'https://api.powerbi.com/v1.0/myorg/gateways/{gateway_id}'
                f'/datasources/{datasource_id}'
            )
            endpoint_type = 'gateway'

        logger.info('Power BI credentials setup: PATCH %s datasource %s ...',
                    endpoint_type, datasource_id[:8] + '...')
        try:
            pr = requests.patch(patch_url, data=credential_body, headers=headers, timeout=30)
            if pr.status_code in (200, 204):
                logger.info('Power BI credentials setup: credentials установлены (HTTP %d)', pr.status_code)
            else:
                logger.warning('Power BI credentials setup: PATCH вернул %d — %s',
                               pr.status_code, pr.text[:300])
        except Exception as e:
            logger.warning('Power BI credentials setup: ошибка PATCH: %s', e)


_ALL_TABLES = [
    'big_analytics_full', 'big_analytics_full_arrival',
    'Dim_Date', 'Dim_Campaign', 'Dim_AdGroup', 'Dim_Site',
    'fact_vk_ads',                  # VK Ads датамарт (star build_vk_ads_fact, VK_ADS_FACT_2026-07-10) — parity с PBI_TABLES
    'analytics_report_placement',   # партиция теперь читает star.arp_fact (репойнт в Этапе 3)
    'direct_history', 'check_utm_fuck_direct',
    'yandex_direct_korrektirovki', 'yandex_direct_404_errors',
    'yandex_direct_return_commission_report',
    'pixel_score', 'yandex_direct_cookie_analytics_website_pages',
    # DATAMARTS_LIVE_TABS_2026-07-14: датамарты живых вкладок Формат/Ключи/расстояние/Фиды.
    # Без них облачные копии *_spend оставались ПУСТЫМИ (вкладки «Формат»/«Ключевые слова» пустые),
    # т.к. refresh обновляет только объекты из _ALL_TABLES. Dim_Distance НЕ включаем (calculated).
    'fact_adformat_spend',                      # «Формат» (расход по ad_format)
    'fact_criterion_spend', 'fact_criterion_zayavki',   # «Ключевые слова» (расход + воронка по критерию)
    'dim_criterion', 'analytics_report_criterion',      # справочник + отчёт по критериям
    'fact_region_spend', 'fact_region_zayavki', 'Dim_Location',  # «расстояние»/область
    'Dim_PlacementFeed', 'fact_direct_feed_funnel', 'analytics_report_feed',  # «Фиды»
    # MINUS_TABLES_2026-07-18: «Я.Директ проверки → Количество минус слов».
    # Обе присутствуют в опубликованном датасете (проверено executeQueries 18.07),
    # обе — mode: import над PG (delta — VIEW на стороне PG, для PBI обычная таблица).
    'yandex_direct_minus_snapshot', 'v_yandex_direct_minus_delta',
    # ML_KORREKTIROVKI_PBI_2026-07-31: таблица есть в admin_ch SemanticModel,
    # поэтому selective refresh должен обновлять её вместе с остальными PBI-объектами.
    'fact_ml_korrektirovki',
]


def refresh_powerbi(tables: list[str] | None = None) -> None:
    pbi = _load_powerbi_config()
    token = _get_powerbi_token(pbi)

    logger.info('Power BI: захват владения датасетом (TakeOver) ...')
    _take_over_dataset(pbi=pbi, token=token)

    logger.info('Power BI: проверка/ребайнд хоста датасорса ...')
    rebind_done = _ensure_datasource_host(pbi=pbi, token=token)
    if rebind_done:
        # После UpdateDatasources PBI пересоздаёт datasource с новым dsId.
        # Даём сервису 5 секунд на propagation перед GET datasources в credentials.
        logger.info('Power BI: rebind выполнен — пауза 5 сек перед credentials ...')
        time.sleep(5)

    logger.info('Power BI: установка datasource credentials ...')
    _ensure_powerbi_datasource_credentials(pbi=pbi, token=token)

    if tables is not None:
        body = json.dumps({
            'type': 'full',
            'notifyOption': 'NoNotification',
            'objects': [{'table': t} for t in tables],
        }).encode()
        headers_refresh = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }
        logger.info('Power BI: обновление %d таблиц: %s', len(tables), tables)
    else:
        body = None
        headers_refresh = {'Authorization': f'Bearer {token}'}
        logger.info('Power BI: полное обновление всех таблиц ...')

    logger.info('Power BI: триггер обновления датасета ...')
    refresh_resp = requests.post(
        f"https://api.powerbi.com/v1.0/myorg/groups/{pbi['workspace_id']}"
        f"/datasets/{pbi['dataset_id']}/refreshes",
        headers=headers_refresh,
        data=body,
        timeout=30,
    )
    if refresh_resp.status_code in (200, 202):
        logger.info('Power BI: обновление запущено (HTTP %d)', refresh_resp.status_code)
    else:
        logger.error('Power BI: ошибка %d — %s', refresh_resp.status_code, refresh_resp.text)
        raise RuntimeError(f'Power BI refresh failed: {refresh_resp.status_code}')

    status_url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/{pbi['workspace_id']}"
        f"/datasets/{pbi['dataset_id']}/refreshes?$top=1"
    )
    headers = {'Authorization': f'Bearer {token}'}
    poll_interval = 60
    poll_timeout = 3600

    logger.info('Power BI: ждём завершения (таймаут %d мин) ...', poll_timeout // 60)
    start = time.time()
    while True:
        time.sleep(poll_interval)
        try:
            r = requests.get(status_url, headers=headers, timeout=30)
            r.raise_for_status()
            items = r.json().get('value', [])
        except Exception as e:
            logger.warning('Power BI: ошибка опроса статуса: %s', e)
            if time.time() - start > poll_timeout:
                _send_telegram('⚠️ Power BI: не удалось получить статус обновления (таймаут опроса)')
                return
            continue

        if not items:
            continue

        status = items[0].get('status', '')
        logger.info('Power BI: статус = %s', status)

        if status == 'Completed':
            elapsed = int(time.time() - start)
            _send_telegram(f'✅ Power BI: обновление завершено ({elapsed // 60} мин {elapsed % 60} сек)')
            return
        if status in ('Failed', 'Cancelled', 'Disabled'):
            error = items[0].get('serviceExceptionJson', '')
            request_id = items[0].get('requestId', '')
            logger.error(
                'Power BI: обновление завершилось со статусом %s (requestId=%s): %s',
                status, request_id, error or '<нет serviceExceptionJson>',
            )
            _send_telegram(
                f'❌ Power BI: обновление завершилось со статусом <b>{status}</b>\n'
                f'<code>{error[:300]}</code>'
            )
            raise RuntimeError(f'Power BI refresh {status}')

        if time.time() - start > poll_timeout:
            _send_telegram('⚠️ Power BI: таймаут ожидания обновления (60 мин)')
            return


if __name__ == '__main__':
    try:
        refresh_powerbi(tables=_ALL_TABLES)
    except Exception as e:
        logger.error('refresh_powerbi завершился с ошибкой: %s', e)
        _send_telegram(f'❌ refresh_powerbi: ошибка\n<code>{e}</code>')
        sys.exit(1)
