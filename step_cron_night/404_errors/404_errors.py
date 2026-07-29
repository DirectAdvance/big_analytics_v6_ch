import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import gspread
import psycopg2
import requests
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import DB_DST
for _p in Path(__file__).resolve().parents:
    if (_p / '.secret' / 'loader.py').exists():
        sys.path.insert(0, str(_p / '.secret'))
        break
from loader import direct_token, find_google_sa  # noqa: E402

# ============================================================
# ⚙️  НАСТРОЙКИ
# ============================================================
TOKEN     = direct_token('victoryagency-direct1618440')
DATE_FROM_DEFAULT = '2026-01-01'

SERVICE_ACCOUNT_FILE = str(find_google_sa())

# Google Sheets — источник сайтов
SOURCE_SHEET_ID       = '1Hw0sfNjb3BHrSs6ARmuPaN7C7t0JSAqV7b6TuUlT3ow'
SOURCE_WORKSHEET_NAME = 'ВСЕ САЙТЫ'

DB_CONFIG = DB_DST
# ============================================================


def _get_date_range() -> tuple[str, str]:
    today = date.today().isoformat()
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'yandex_direct_404_errors'
                )
            """)
            exists = cur.fetchone()[0]
            if not exists:
                print(f"📅 Таблица не существует — загружаем с {DATE_FROM_DEFAULT}")
                return DATE_FROM_DEFAULT, today
            cur.execute("SELECT MAX(visit_date) FROM yandex_direct_404_errors")
            max_date = cur.fetchone()[0]
        if max_date is None:
            print(f"📅 Таблица пустая — загружаем с {DATE_FROM_DEFAULT}")
            return DATE_FROM_DEFAULT, today
        date_from = (max_date - timedelta(days=7)).isoformat()
        print(f"📅 MAX visit_date={max_date}, загружаем с {date_from}")
        return date_from, today
    finally:
        if conn:
            conn.close()


def _week_start(date_str) -> str | None:
    if not date_str:
        return None
    d = date.fromisoformat(str(date_str))
    return (d - timedelta(days=d.weekday())).isoformat()


def get_gspread_client(retries=5, delay=3):
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    for attempt in range(1, retries + 1):
        try:
            return gspread.authorize(creds)
        except Exception as e:
            print(f"⚠️  Попытка {attempt}/{retries} — ошибка авторизации Google: {e}")
            if attempt < retries:
                print(f"   Повтор через {delay} сек...")
                time.sleep(delay)
    print("❌ Не удалось авторизоваться в Google после нескольких попыток.")
    return None


def extract_campaign_id(utm_campaign):
    if not utm_campaign:
        return ''
    m = re.match(r'^(\d+)', utm_campaign.strip())
    return m.group(1) if m else ''


def extract_group_id(utm_content):
    if not utm_content:
        return ''
    m = re.search(r'g:(\d+)\|', utm_content)
    return m.group(1) if m else ''


def get_sites_from_sheet(client, retries=3, delay=3):
    print("📋 Читаем список сайтов из Google Sheets...")

    for attempt in range(1, retries + 1):
        try:
            sheet = client.open_by_key(SOURCE_SHEET_ID).worksheet(SOURCE_WORKSHEET_NAME)
            rows  = sheet.get_all_values()
            break
        except Exception as e:
            print(f"⚠️  Попытка {attempt}/{retries} — ошибка чтения таблицы: {e}")
            if attempt < retries:
                time.sleep(delay)
            else:
                print("❌ Не удалось прочитать таблицу.")
                return {}

    if not rows:
        print("❌ Таблица пустая.")
        return {}

    sites = {}
    for row in rows[1:]:
        site       = row[0].strip().lower().rstrip('/') if len(row) > 0 else ''
        status     = row[1].strip()                      if len(row) > 1 else ''
        специалист = row[7].strip()                      if len(row) > 7 else ''

        if status == 'Контекст активно' and site:
            sites[site] = специалист

    print(f"✅ Активных сайтов (Контекст активно): {len(sites)}")
    return sites


def get_all_counters():
    url     = "https://api-metrika.yandex.net/management/v1/counters"
    headers = {"Authorization": f"OAuth {TOKEN}"}

    print("\n🔍 Получаем счётчики из Яндекс.Метрики...")
    r = requests.get(url, headers=headers, params={"per_page": 1000}, timeout=30)

    if r.status_code != 200:
        print(f"❌ Ошибка получения счётчиков ({r.status_code}): {r.text}")
        return []

    counters = r.json().get('counters', [])
    print(f"✅ Всего счётчиков в аккаунте: {len(counters)}")
    return counters


def match_counters_to_sites(counters, sites_map):
    matched = []
    for c in counters:
        site       = c.get('site', '').strip().lower().rstrip('/')
        site_clean = site.replace('www.', '')

        for sheet_site, специалист in sites_map.items():
            sheet_clean = sheet_site.replace('www.', '')
            if site_clean == sheet_clean or site_clean.endswith('.' + sheet_clean):
                matched.append({
                    'id':         c['id'],
                    'name':       c.get('name', str(c['id'])),
                    'site':       site,
                    'специалист': специалист,
                })
                break

    print(f"\n🔗 Совпало счётчиков с таблицей: {len(matched)}")
    return matched


def get_404_data(counter, date_from: str, date_to: str):
    url     = "https://api-metrika.yandex.net/stat/v1/data"
    headers = {"Authorization": f"OAuth {TOKEN}"}

    params = {
        "ids":        counter['id'],
        "metrics":    "ym:pv:pageviews",
        "dimensions": "ym:pv:URL,ym:pv:title,ym:pv:UTMCampaign,ym:pv:UTMContent,ym:pv:date",
        "filters":    "ym:pv:title=@'404'",
        "date1":      date_from,
        "date2":      date_to,
        "limit":      10000
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            print(f"  ⚠️  Ошибка API ({r.status_code})")
            return []

        rows = []
        for item in r.json().get('data', []):
            dims       = item['dimensions']
            title      = dims[1].get('name', '')
            utm_camp   = dims[2].get('name') or ''
            utm_cont   = dims[3].get('name') or ''
            visit_date = dims[4].get('name') or None

            if not title.startswith('404'):
                continue

            title = title.replace('404 - Страница не найдена', '404 ошибка')

            rows.append((
                str(counter['id']),             # 0 — № счетчика
                counter['name'],                # 1 — counter_name
                counter['site'],                # 2 — site
                counter['специалист'],          # 3 — специалист
                dims[0].get('name', ''),        # 4 — url
                title,                          # 5 — page_title
                utm_camp,                       # 6 — utm_campaign
                extract_campaign_id(utm_camp),  # 7 — № кампании
                utm_cont,                       # 8 — utm_content
                extract_group_id(utm_cont),     # 9 — № группы
                visit_date,                     # 10 — visit_date
                _week_start(visit_date),        # 11 — week_start
            ))
        return rows

    except Exception as e:
        print(f"  ❌ Исключение: {e}")
        return []


def save_to_postgres(all_data, date_from: str):
    if not all_data:
        print("⏭ Пропускаем запись в БД: нет данных.")
        return

    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur  = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS yandex_direct_404_errors (
                id               SERIAL PRIMARY KEY,
                "№ счетчика"     TEXT,
                counter_name     TEXT,
                site             TEXT,
                специалист       TEXT,
                url              TEXT,
                page_title       TEXT,
                utm_campaign     TEXT,
                "№ кампании"     TEXT,
                utm_content      TEXT,
                "№ группы"       TEXT,
                visit_date       DATE,
                week_start       DATE,
                detected_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("ALTER TABLE yandex_direct_404_errors ADD COLUMN IF NOT EXISTS week_start DATE;")

        cur.execute("DELETE FROM yandex_direct_404_errors WHERE visit_date >= %s", (date_from,))
        print(f"🗑 Удалено {cur.rowcount} строк (visit_date >= {date_from})")

        cur.executemany("""
            INSERT INTO yandex_direct_404_errors
                ("№ счетчика", counter_name, site, специалист,
                 url, page_title, utm_campaign, "№ кампании", utm_content, "№ группы", visit_date, week_start)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, all_data)

        conn.commit()
        print(f"🐘 PostgreSQL: записано {len(all_data)} строк (таблица 'yandex_direct_404_errors')")

    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
    finally:
        if conn:
            cur.close()
            conn.close()


if __name__ == "__main__":
    date_from, date_to = _get_date_range()

    print("=" * 60)
    print(f"🚀 Парсер 404-ошибок | Период: {date_from} — {date_to}")
    print("=" * 60)

    client = get_gspread_client()
    if not client:
        print("❌ Нет подключения к Google. Завершение.")
        exit()

    sites_map = get_sites_from_sheet(client)
    if not sites_map:
        print("❌ Список сайтов пуст.")
        exit()

    counters = get_all_counters()
    if not counters:
        print("❌ Счётчики не найдены. Проверь токен.")
        exit()

    matched = match_counters_to_sites(counters, sites_map)
    if not matched:
        print("⚠️  Ни один счётчик не совпал с сайтами из таблицы.")
        exit()

    print("\n📊 Собираем 404-ошибки...")
    print("-" * 60)

    all_rows = []
    for counter in matched:
        print(f"\n🌐 {counter['name']} ({counter['site']}) [ID: {counter['id']}]")
        rows = get_404_data(counter, date_from, date_to)
        if rows:
            print(f"  ✅ Найдено: {len(rows)} строк")
            for r in rows[:2]:
                print(f"     URL:        {r[4]}")
                print(f"     Title:      {r[5]}")
                print(f"     № кампании: {r[7]}")
                print(f"     № группы:   {r[9]}")
        else:
            print(f"  ℹ️  404-ошибок не найдено")
        all_rows.extend(rows)

    print("\n" + "=" * 60)
    print(f"📦 Итого записей: {len(all_rows)}")

    save_to_postgres(all_rows, date_from)

    print("=" * 60)
    print("✅ Готово!")
    print("=" * 60)
