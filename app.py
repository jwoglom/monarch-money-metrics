#!/usr/bin/env python3

import os
import arrow
import logging
import json
import asyncio

from monarchmoney import MonarchMoney

from flask import Flask, Response, request, abort, redirect, jsonify

from prometheus_flask_exporter.multiprocess import GunicornInternalPrometheusMetrics

from prometheus_client import Counter, Gauge

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')

logger = logging.getLogger(__name__)

app = Flask(__name__)
metrics = GunicornInternalPrometheusMetrics(app)

monarch_logged_in = Gauge('monarch_logged_in', 'If monarch is logged in', [])

logged_in = []
def set_logged_in(val: bool):
    monarch_logged_in.set(1.0 if val else 0.0)
    if val:
        logged_in.append(True)
    else:
        logged_in.clear()

_account_fields = ['account_id', 'account_name', 'account_type', 'account_subtype', 'data_provider', 'institution_name']
monarch_last_update_loop_at = Gauge('monarch_last_update_loop_at', 'monarch-money-metrics last update loop', [])
monarch_account_current_balance = Gauge('monarch_account_current_balance', 'Account current balance', _account_fields)
monarch_account_display_balance = Gauge('monarch_account_display_balance', 'Account display balance (credits are negative)', _account_fields)
monarch_account_updated_at = Gauge('monarch_account_updated_at', 'Account last updated timestamp', _account_fields)
monarch_account_manual = Gauge('monarch_account_manual', '1 if account is manual', _account_fields)
monarch_account_include_in_net_worth = Gauge('monarch_account_include_in_net_worth', '1 if account should be included in net worth', _account_fields)
monarch_account_transactions_count = Gauge('monarch_account_transactions_count', 'Count of transactions for account', _account_fields)
monarch_account_holdings_count = Gauge('monarch_account_holdings_count', 'Count of holdings for account', _account_fields)


def set_account_metrics(account: dict):
    if account.get('isHidden'):
        return
    lbl=dict(
        account_id=account.get('id', ''),
        account_name=account.get('displayName', ''),
        account_type=account.get('type', {}).get('display', ''),
        account_subtype=account.get('subtype', {}).get('display', ''),
        data_provider=account.get('dataProvider', ''),
        institution_name=(account.get('institution', {}) or {}).get('name', '')
    )
    monarch_account_current_balance.labels(**lbl).set(account.get('currentBalance'))
    monarch_account_display_balance.labels(**lbl).set(account.get('displayBalance'))
    monarch_account_updated_at.labels(**lbl).set(arrow.get(account.get('updatedAt')).float_timestamp)
    monarch_account_transactions_count.labels(**lbl).set(account.get('transactionsCount', 0))
    monarch_account_holdings_count.labels(**lbl).set(account.get('holdingsCount', 0))
    monarch_account_manual.labels(**lbl).set(1.0 if account.get('isManual', False) else 0.0)
    monarch_account_include_in_net_worth.labels(**lbl).set(1.0 if account.get('includeInNetWorth', False) else 0.0)


mm = MonarchMoney()
MONARCH_SESSION_FILE = os.getenv('MONARCH_SESSION_FILE', '')
if MONARCH_SESSION_FILE:
    mm = MonarchMoney(session_file=MONARCH_SESSION_FILE)


if MONARCH_SESSION_FILE and os.path.exists(MONARCH_SESSION_FILE):
    mm.load_session(MONARCH_SESSION_FILE)

    if asyncio.run(mm.get_transactions(limit=1)):
        set_logged_in(True)


MONARCH_CREDS_FILE = os.getenv('MONARCH_CREDS_FILE', '')
if not logged_in and MONARCH_CREDS_FILE and os.path.exists(MONARCH_CREDS_FILE):
    with open(MONARCH_CREDS_FILE, 'r') as f:
        _monarch_creds = json.loads(f.read())
        email = _monarch_creds['email']
        password = _monarch_creds['password']
        asyncio.run(mm.login(email, password))

        if asyncio.run(mm.get_transactions(limit=1)):
            set_logged_in(True)

if not logged_in:
    logger.warning('Please specify a MONARCH_CREDS_FILE with email and password JSON fields to set parameters non-interactively')
    asyncio.run(mm.interactive_login())

    if asyncio.run(mm.get_transactions(limit=1)):
        set_logged_in(True)

async def update_loop():
    accounts = await mm.get_accounts()
    for account in accounts.get('accounts', []):
        set_account_metrics(account)

    monarch_last_update_loop_at.set(arrow.get().float_timestamp)

@app.route('/')
def index():
    return jsonify(
        logged_in = bool(logged_in)
    )

@app.route('/2fa_token/<path:token>')
async def route_2fa_token(token):
    await mm.multi_factor_authenticate(email, password, token)
    return 'ok'

@app.route('/update_loop')
async def update_loop_route():
    await update_loop()
    return 'ok'

@app.route('/accounts')
def accounts_route():
    return jsonify(asyncio.run(mm.get_accounts()))


@app.route('/healthz')
def healthz_route():
    return 'ok'