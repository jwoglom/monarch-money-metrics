#!/usr/bin/env python3

import os
import arrow
import logging
import json
import time
import asyncio

from monarchmoney import MonarchMoney, RequireMFAException

from flask import Flask, Response, request, abort, redirect, jsonify
from flask_apscheduler import APScheduler
is_gunicorn = "gunicorn" in os.environ.get("SERVER_SOFTWARE", "")

if is_gunicorn:
    from prometheus_flask_exporter.multiprocess import GunicornInternalPrometheusMetrics as PrometheusMetrics
else:
    from prometheus_flask_exporter import PrometheusMetrics

from prometheus_client import Counter, Gauge

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')

logger = logging.getLogger(__name__)

app = Flask(__name__)
class Config:
    SCHEDULER_API_ENABLED = True

app.config.from_object(Config())

metrics = PrometheusMetrics(app)

monarch_logged_in = Gauge('monarch_logged_in', 'If monarch-money-metrics is logged in', [])
monarch_needs_mfa = Gauge('monarch_needs_mfa', 'If monarch-money-metrics needs mfa', [])

logged_in = []
def set_logged_in(val: bool):
    monarch_logged_in.set(1.0 if val else 0.0)
    if val:
        logged_in.append(True)
    else:
        logged_in.clear()

needs_mfa = []
def set_needs_mfa(val: bool):
    monarch_needs_mfa.set(1.0 if val else 0.0)
    if val:
        needs_mfa.append(True)
    else:
        needs_mfa.clear()


_account_fields = ['account_id', 'account_name', 'account_type', 'account_subtype', 'data_provider', 'institution_name']
monarch_last_update_loop_at = Gauge('monarch_last_update_loop_at', 'monarch-money-metrics last update loop', [])
monarch_update_loops_total = Counter('monarch_update_loops_total', 'Number of update loops completed', [])
monarch_account_current_balance = Gauge('monarch_account_current_balance', 'Account current balance', _account_fields)
monarch_account_display_balance = Gauge('monarch_account_display_balance', 'Account display balance (credits are negative)', _account_fields)
monarch_account_updated_at = Gauge('monarch_account_updated_at', 'Account last updated unix epoch timestamp', _account_fields)
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

monarch_cash_flow_summary_income = Gauge('monarch_cash_flow_summary_income', 'Cash flow summary income', [])
monarch_cash_flow_summary_expense = Gauge('monarch_cash_flow_summary_expense', 'Cash flow summary expense', [])
monarch_cash_flow_summary_savings = Gauge('monarch_cash_flow_summary_savings', 'Cash flow summary savings', [])
monarch_cash_flow_summary_savings_rate = Gauge('monarch_cash_flow_summary_savings_rate', 'Cash flow summary savings_rate', [])

def set_cash_flow_summary_metrics(outer: dict):
    summary = outer.get('summary')[0].get('summary')
    monarch_cash_flow_summary_income.set(summary.get('sumIncome'))
    monarch_cash_flow_summary_expense.set(summary.get('sumExpense'))
    monarch_cash_flow_summary_savings.set(summary.get('savings'))
    monarch_cash_flow_summary_savings_rate.set(summary.get('savingsRate'))

monarch_transactions_summary_sum_income = Gauge('monarch_transactions_summary_sum_income', 'Transactions summary sum of income', [])
monarch_transactions_summary_sum_expense = Gauge('monarch_transactions_summary_sum_expense', 'Transactions summary sum of expense', [])
monarch_transactions_summary_first_at = Gauge('monarch_transactions_summary_first_at', 'Transactions summary first expense unix epoch timestamp', [])
monarch_transactions_summary_last_at = Gauge('monarch_transactions_summary_last_at', 'Transactions summary last expense unix epoch timestamp', [])

def set_transactions_summary_metrics(outer: dict):
    summary = outer.get('aggregates')[0].get('summary')
    monarch_transactions_summary_sum_income.set(summary.get('sumIncome'))
    monarch_transactions_summary_sum_expense.set(summary.get('sumExpense'))
    monarch_transactions_summary_first_at.set(arrow.get(summary.get('first')).float_timestamp)
    monarch_transactions_summary_last_at.set(arrow.get(summary.get('last')).float_timestamp)

monarch_cash_flow_sum_by_category = Gauge('monarch_cash_flow_sum_by_category', 'Cash flow sum by category', ['category', 'group_type'])

def set_cash_flow_metrics(outer: dict):
    by_cat = outer.get('byCategory')
    for item in by_cat:
        name = (item.get('groupBy',{}).get('category',{}) or {}).get('name','')
        group_type = ((item.get('groupBy',{}).get('category',{}) or {}).get('group',{}) or {}).get('type', '')
        sum_amt = (item.get('summary', {}) or {}).get('sum', 0)
        monarch_cash_flow_sum_by_category.labels(
            category=name,
            group_type=group_type
        ).set(sum_amt)

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
        try:
            asyncio.run(mm.login(email, password))
        except RequireMFAException:
            set_needs_mfa(True)
        else:
            if asyncio.run(mm.get_transactions(limit=1)):
                set_logged_in(True)

if not logged_in:
    logger.warning('Please specify a MONARCH_CREDS_FILE with email and password JSON fields to set parameters non-interactively')
    asyncio.run(mm.interactive_login())

    if asyncio.run(mm.get_transactions(limit=1)):
        set_logged_in(True)

async def update_loop():
    logger.info('Starting update_loop')
    accounts = await mm.get_accounts()
    for account in accounts.get('accounts', []):
        set_account_metrics(account)
    
    transactions_summary = await mm.get_transactions_summary()
    set_transactions_summary_metrics(transactions_summary)

    cash_flow = await mm.get_cashflow_summary()
    set_cash_flow_summary_metrics(cash_flow)

    cash_flow_summary = await mm.get_cashflow_summary()
    set_cash_flow_summary_metrics(cash_flow_summary)

    monarch_last_update_loop_at.set(arrow.get().float_timestamp)
    logger.info('Finished update_loop')
    monarch_update_loops_total.inc()

MONARCH_UPDATE_LOOP_MINUTES = os.getenv('MONARCH_UPDATE_LOOP_MINUTES', '15')

scheduler = APScheduler()
scheduler.init_app(app)

@scheduler.task('interval', id='update_loop', seconds=60*int(MONARCH_UPDATE_LOOP_MINUTES))
def scheduler_update_loop():
    logger.info('Running scheduler_update_loop()')
    asyncio.run(update_loop())


scheduler.start()

@app.route('/')
def index():
    return jsonify(
        logged_in = bool(logged_in)
    )

@app.route('/mfa_token/<path:token>')
async def mfa_token_route(token):
    await mm.multi_factor_authenticate(email, password, token)
    set_needs_mfa(False)
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