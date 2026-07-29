#!/usr/bin/env python3
"""_set_serverhost_domain.py — сменить ServerHost параметр датасета на домен
analytics-marketing.ru (LE-серт выписан на этот CN) и подтвердить применение.
Временный helper, удаляется после задачи.
"""
from __future__ import annotations
import json
import pathlib
import sys

import requests

NEW_HOST = 'analytics-marketing.ru'


def load_pbi() -> dict:
    return json.load(open(pathlib.Path.home() / '.secret' / 'tokens.json'))['powerbi']


def get_token(pbi: dict) -> str:
    r = requests.post(
        f"https://login.microsoftonline.com/{pbi['tenant_id']}/oauth2/v2.0/token",
        data={
            'grant_type': 'client_credentials',
            'client_id': pbi['client_id'],
            'client_secret': pbi['client_secret'],
            'scope': 'https://analysis.windows.net/powerbi/api/.default',
        }, timeout=30)
    r.raise_for_status()
    return r.json()['access_token']


def main() -> int:
    pbi = load_pbi()
    token = get_token(pbi)
    h = {'Authorization': f'Bearer {token}'}
    hj = {**h, 'Content-Type': 'application/json'}
    ws = pbi['workspace_id']
    ds = pbi['dataset_id']
    base = f'https://api.powerbi.com/v1.0/myorg/groups/{ws}/datasets/{ds}'

    # TakeOver (нужно для смены параметров SP-принципалом)
    r = requests.post(f'{base}/Default.TakeOver', headers=h, timeout=30)
    print(f'[TakeOver] HTTP {r.status_code} {r.text[:150]}')

    # GET параметров ДО
    r = requests.get(f'{base}/parameters', headers=h, timeout=30)
    r.raise_for_status()
    params_before = r.json().get('value', [])
    print('[parameters BEFORE]')
    for p in params_before:
        print(f"   name={p.get('name')} type={p.get('type')} "
              f"currentValue={p.get('currentValue')!r} isRequired={p.get('isRequired')}")

    has_serverhost = any(p.get('name') == 'ServerHost' for p in params_before)
    if not has_serverhost:
        print('[ERR] параметр ServerHost не найден — не применяю UpdateParameters')
        return 3

    # POST UpdateParameters
    body = json.dumps({'updateDetails': [{'name': 'ServerHost', 'newValue': NEW_HOST}]})
    r = requests.post(f'{base}/Default.UpdateParameters', headers=hj, data=body, timeout=30)
    print(f'[UpdateParameters] HTTP {r.status_code} {r.text[:300]}')
    if r.status_code not in (200, 202):
        print('[ERR] UpdateParameters не вернул 200/202')
        return 4

    # GET параметров ПОСЛЕ
    r = requests.get(f'{base}/parameters', headers=h, timeout=30)
    r.raise_for_status()
    params_after = r.json().get('value', [])
    print('[parameters AFTER]')
    applied = False
    for p in params_after:
        cur = p.get('currentValue')
        print(f"   name={p.get('name')} currentValue={cur!r}")
        if p.get('name') == 'ServerHost' and cur == NEW_HOST:
            applied = True
    print(f'[applied ServerHost=={NEW_HOST}] {applied}')

    # GET datasources — какой server теперь в conn (datasource мог пересоздаться)
    r = requests.get(f'{base}/datasources', headers=h, timeout=30)
    r.raise_for_status()
    for d in r.json().get('value', []):
        cd = d.get('connectionDetails', {})
        print(f"[datasource] type={d.get('datasourceType')} id={d.get('datasourceId')} "
              f"gatewayId={d.get('gatewayId')} server={cd.get('server')} database={cd.get('database')}")

    return 0 if applied else 5


if __name__ == '__main__':
    sys.exit(main())
