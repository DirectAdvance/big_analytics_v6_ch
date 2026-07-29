"""
set_pixel_price.py — управление дата-эффективной историей цен пикселей.

Таблица local_pixel_price_history (НЕ truncate'ится пайплайном) хранит override-тарифы
поверх baseline из local_pixel_config. build_pixel.py для каждого лида берёт override
по дате лида если есть интервал, иначе baseline.

Модель: override замещает пару (cost_per_lead, cost_total) целиком. Новые тарифы
вставляются как cost_total=<цена>, cost_per_lead=NULL (эффективная цена = COALESCE
(cost_per_lead, cost_total, 0), значит достаточно cost_total).

Логика set_price (одна транзакция):
  a) если у пикселя ещё НЕТ строк в history → снапшот baseline: прочитать текущую
     эффективную цену из local_pixel_config (COALESCE(cost_per_lead, cost_total)) и
     вставить ЗАКРЫТУЮ строку (valid_from='2000-01-01', valid_to=valid_from_param-1,
     cost_total=old_eff, cost_per_lead=NULL, note='snapshot baseline').
  b) если есть ОТКРЫТАЯ строка (valid_to IS NULL) с valid_from < valid_from_param →
     закрыть её (valid_to = valid_from_param - 1).
  c) вставить новую ОТКРЫТУЮ строку (cost_total=new_price, valid_from=valid_from_param,
     valid_to=NULL).

EXCLUDE-constraint lpph_no_overlap гарантирует отсутствие перекрытий интервалов.

CLI:
  python3 -m step5_build_pixel.set_pixel_price --pixel "LeadV ..." --price 1300 \
      --from 2026-05-14 --by "Семён" --note "ретро-правка"
  python3 -m step5_build_pixel.set_pixel_price --list "LeadV ..."
"""

import argparse
import datetime as dt
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger('pixel.set_price')

T_PIXEL_CONFIG = 'local_pixel_config'
T_PIXEL_PRICE_HISTORY = 'local_pixel_price_history'
SNAPSHOT_FROM = dt.date(2000, 1, 1)


def _parse_date(s) -> dt.date:
    if isinstance(s, dt.date):
        return s
    return dt.datetime.strptime(s, '%Y-%m-%d').date()


def set_price(conn, pixel_name: str, new_price, valid_from,
              changed_by: str = None, note: str = None) -> dict:
    """Применить новый дата-эффективный тариф для пикселя.

    Возвращает dict с описанием выполненных действий. Всё в одной транзакции;
    при ошибке вызывающий код должен сделать rollback (или conn в autocommit=False).
    """
    valid_from = _parse_date(valid_from)
    day_before = valid_from - dt.timedelta(days=1)
    actions = []

    with conn.cursor() as cur:
        # Сколько строк history у пикселя уже есть
        cur.execute(
            f'SELECT COUNT(*) FROM {T_PIXEL_PRICE_HISTORY} WHERE pixel_name = %s',
            (pixel_name,),
        )
        existing = cur.fetchone()[0]

        # a) первый override для пикселя — снапшот baseline
        if existing == 0:
            cur.execute(
                f"""SELECT salon, project, COALESCE(cost_per_lead, cost_total)
                    FROM {T_PIXEL_CONFIG} WHERE pixel_name = %s LIMIT 1""",
                (pixel_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f'pixel_name={pixel_name!r} нет в {T_PIXEL_CONFIG} — '
                    f'нельзя снять baseline-снапшот'
                )
            salon, project, old_eff = row
            cur.execute(
                f"""INSERT INTO {T_PIXEL_PRICE_HISTORY}
                    (pixel_name, salon, project, cost_per_lead, cost_total,
                     valid_from, valid_to, changed_by, note)
                    VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s)""",
                (pixel_name, salon, project, old_eff,
                 SNAPSHOT_FROM, day_before, changed_by, 'snapshot baseline'),
            )
            actions.append(
                f'snapshot baseline: cost_total={old_eff} '
                f'[{SNAPSHOT_FROM}..{day_before}]'
            )
        else:
            # b) закрыть открытую строку (valid_to IS NULL) если её valid_from раньше
            cur.execute(
                f"""SELECT id, valid_from FROM {T_PIXEL_PRICE_HISTORY}
                    WHERE pixel_name = %s AND valid_to IS NULL""",
                (pixel_name,),
            )
            open_row = cur.fetchone()
            if open_row is not None:
                open_id, open_from = open_row
                if open_from < valid_from:
                    cur.execute(
                        f'UPDATE {T_PIXEL_PRICE_HISTORY} SET valid_to = %s WHERE id = %s',
                        (day_before, open_id),
                    )
                    actions.append(
                        f'closed open row id={open_id} '
                        f'(valid_from={open_from}) -> valid_to={day_before}'
                    )
                elif open_from == valid_from:
                    raise ValueError(
                        f'у {pixel_name!r} уже есть открытая строка с valid_from='
                        f'{open_from} == запрошенному valid_from — нечего сдвигать'
                    )
                else:
                    raise ValueError(
                        f'у {pixel_name!r} открытая строка valid_from={open_from} '
                        f'позже запрошенного {valid_from} — обратный порядок не поддержан'
                    )

        # c) вставить новую открытую строку
        # salon/project для удобства подтянем из config (если есть)
        cur.execute(
            f'SELECT salon, project FROM {T_PIXEL_CONFIG} WHERE pixel_name = %s LIMIT 1',
            (pixel_name,),
        )
        cfg = cur.fetchone()
        salon, project = (cfg if cfg else (None, None))
        cur.execute(
            f"""INSERT INTO {T_PIXEL_PRICE_HISTORY}
                (pixel_name, salon, project, cost_per_lead, cost_total,
                 valid_from, valid_to, changed_by, note)
                VALUES (%s, %s, %s, NULL, %s, %s, NULL, %s, %s)
                RETURNING id""",
            (pixel_name, salon, project, new_price, valid_from, changed_by, note),
        )
        new_id = cur.fetchone()[0]
        actions.append(
            f'new open row id={new_id}: cost_total={new_price} [{valid_from}..∞]'
        )

    return {'pixel_name': pixel_name, 'actions': actions}


def list_prices(conn, pixel_name: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id, valid_from, valid_to, cost_per_lead, cost_total,
                       COALESCE(cost_per_lead, cost_total) AS effective,
                       changed_by, note
                FROM {T_PIXEL_PRICE_HISTORY}
                WHERE pixel_name = %s
                ORDER BY valid_from""",
            (pixel_name,),
        )
        return cur.fetchall()


def _print_scale(conn, pixel_name: str):
    rows = list_prices(conn, pixel_name)
    print(f'\n=== шкала цен: {pixel_name} ===')
    if not rows:
        print('  (нет override-строк, действует baseline из local_pixel_config)')
        return
    print('  id | valid_from |  valid_to  | per_lead | total | eff  | by | note')
    for r in rows:
        rid, vf, vt, cpl, ct, eff, by, note = r
        print(f'  {rid:>3} | {vf} | {str(vt) if vt else "    ∞     "} | '
              f'{str(cpl):>8} | {str(ct):>5} | {str(eff):>4} | {by or ""} | {note or ""}')


def _get_conn():
    import config.db as db_module
    db_module.init_pool()
    return db_module, db_module.get_conn()


def main():
    ap = argparse.ArgumentParser(description='Дата-эффективные цены пикселей')
    ap.add_argument('--pixel', help='pixel_name (CampaignName)')
    ap.add_argument('--price', type=float, help='новая эффективная цена за заявку (cost_total)')
    ap.add_argument('--from', dest='valid_from', help='valid_from YYYY-MM-DD')
    ap.add_argument('--by', dest='changed_by', default=None, help='changed_by')
    ap.add_argument('--note', default=None, help='note')
    ap.add_argument('--list', dest='list_pixel', help='показать шкалу цен пикселя и выйти')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    db_module, conn = _get_conn()
    conn.autocommit = False
    try:
        if args.list_pixel:
            _print_scale(conn, args.list_pixel)
            return
        if not (args.pixel and args.price is not None and args.valid_from):
            ap.error('нужны --pixel, --price, --from (или --list)')
        res = set_price(conn, args.pixel, args.price, args.valid_from,
                        changed_by=args.changed_by, note=args.note)
        conn.commit()
        print(f'OK {res["pixel_name"]}:')
        for a in res['actions']:
            print('  -', a)
        _print_scale(conn, args.pixel)
    except Exception:
        conn.rollback()
        raise
    finally:
        db_module.put_conn(conn)
        db_module.close_pool()


if __name__ == '__main__':
    main()
