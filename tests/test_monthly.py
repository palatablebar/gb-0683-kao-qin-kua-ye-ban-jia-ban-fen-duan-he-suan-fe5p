import json
from decimal import Decimal

import pytest

from app.errors import DomainError
from app.workflow import load, run


def roster():
    return json.loads(open('sample_data/roster.json').read())


def monthly_state():
    return run(load('monthly.csv'), {'roster': roster()})


def test_hourly_base_equals_clock_rate_and_matches_m5():
    s = monthly_state()
    h001 = next(r for r in s['monthly']['rows'] if r['employee'] == 'H001')
    assert h001['pay_type'] == 'hourly' and h001['base_hourly'] is None
    m5_total = sum((Decimal(c['amount']) for c in s['calculations'] if c['employee'] == 'H001'), Decimal('0'))
    assert Decimal(h001['payable']) == m5_total == Decimal('480.00')


def test_monthly_base_is_salary_over_174_independent_of_days_and_shifts():
    s = monthly_state()
    by_id = {r['employee']: r for r in s['monthly']['rows']}
    assert by_id['M001']['base_hourly'] == '50.0000'        # 8700/174
    assert by_id['M002']['base_hourly'] == '40.0000'        # 6960/174，入职半月基数不被压低
    assert by_id['M003']['base_hourly'] == '60.0000'        # 10440/174，离职半月基数不被压低
    # 少排班的新员工 M004 没有任何班次，加班基数仍由月薪决定
    assert by_id['M004']['base_hourly'] == '32.0690'


def test_fixed_salary_prorated_by_calendar_days_in_employment_window():
    s = monthly_state()
    by_id = {r['employee']: r for r in s['monthly']['rows']}
    assert by_id['M001']['employed_days'] == 31 and by_id['M001']['fixed_pay'] == '8700.00'
    # 10-08 入职：含入职日到月末共 24 个自然日
    assert by_id['M002']['employed_days'] == 24
    assert by_id['M002']['fixed_pay'] == (Decimal('6960') * 24 / 31).quantize(Decimal('.01')).__str__()
    # 10-09 离职：含离职日共 9 个自然日
    assert by_id['M003']['employed_days'] == 9


def test_monthly_premiums_only_on_overtime_rest_holiday_parts():
    s = monthly_state()
    m001 = next(r for r in s['monthly']['rows'] if r['employee'] == 'M001')
    # 跨夜越假日：10-01 假日 120 分钟按 3 倍（加 2 倍）；10-09 超 8 小时 60 分钟按 1.5 倍（加 0.5）
    premium = Decimal('50') * 120 / 60 * 2 + Decimal('50') * 60 / 60 * Decimal('0.5')
    assert Decimal(m001['payable']) == Decimal('8700.00') + premium.quantize(Decimal('.01'))
    m003 = next(r for r in s['monthly']['rows'] if r['employee'] == 'M003')
    assert Decimal(m003['payable']) == Decimal('3030.97') + Decimal('30.00')
    # 正常工时被月薪覆盖，不重复计费
    assert all(c['amount'] == '0.00' for c in s['calculations']
               if c['employee'].startswith('M') and c['code'] == 'REGULAR')


def test_actual_shifts_minutes_and_payables_correspond_and_balance():
    s = monthly_state()
    assert s['monthly']['balanced'] is True
    total_minutes = sum(r['actual_minutes'] for r in s['monthly']['rows'])
    assert total_minutes == s['business']['minutes']
    total_payable = sum((Decimal(r['payable']) for r in s['monthly']['rows']), Decimal('0'))
    assert total_payable == Decimal(s['monthly']['totals']['payable']) == Decimal(s['business']['total'])


def test_roster_change_voids_old_settlement_but_keeps_history(client):
    first = client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': roster()}}).get_json()
    assert client.get('/api/settlements').get_json()[0]['status'] == '有效'
    changed = roster()
    changed[1]['monthly_salary'] = '9000.00'
    second = client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': changed}}).get_json()
    assert first['run_id'] != second['run_id']
    ledger = {x['run_id']: x for x in client.get('/api/settlements').get_json()}
    assert ledger[first['run_id']]['status'] == '作废'
    assert ledger[first['run_id']]['superseded_by'] == second['run_id']
    assert ledger[second['run_id']]['status'] == '有效'
    # 旧批次原件仍可取回，原始打卡与人事记录不删除
    old = client.get('/api/runs/' + first['run_id'])
    assert old.status_code == 200 and old.get_json()['source_b64'] == first['source_b64']
    assert old.get_json()['monthly']['roster_snapshot'][1]['monthly_salary'] == '8700.00'


def test_schedule_backfill_changes_run_id_but_old_reviewed_result_retained(client):
    first = client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': roster()}}).get_json()
    client.post('/api/runs/%s/review' % first['run_id'], json={'version': 1, 'decision': '通过'})
    raw = load('monthly.csv').decode('utf-8-sig')
    # 补录一条新班次（排班补录）：输入字节变化 -> 新 run_id -> 旧月结作废，旧版本体保留
    extra = '01017,M002,2026-10-12,1,IN,2026-10-12T09:00:00+08:00,09:00,18:00,12:00,13:00,0,0,补录\n' \
            '01018,M002,2026-10-12,1,OUT,2026-10-12T18:00:00+08:00,09:00,18:00,12:00,13:00,0,0,补录\n'
    second = client.post('/api/runs', json={'csv': raw + extra, 'parameters': {'roster': roster()}}).get_json()
    assert second['run_id'] != first['run_id']
    old = client.get('/api/runs/' + first['run_id']).get_json()
    assert old['decision'] == '通过' and old['review_version'] == 2
    ledger = {x['run_id']: x for x in client.get('/api/settlements').get_json()}
    assert ledger[first['run_id']]['status'] == '作废' and ledger[second['run_id']]['status'] == '有效'


def test_failed_generation_keeps_previous_settlement_effective(client):
    client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': roster()}})
    bad_roster = roster() + [{'employee': 'M001', 'pay_type': 'monthly', 'monthly_salary': '1',
                              'hire_date': None, 'leave_date': None}]
    r = client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': bad_roster}})
    assert r.status_code == 422 and r.get_json()['code'] == 'ROSTER'
    ledger = client.get('/api/settlements').get_json()
    assert len(ledger) == 1 and ledger[0]['status'] == '有效'


def test_write_failure_rolls_back_and_keeps_old_settlement(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'rollback.db'))
    from app import store
    state = monthly_state()
    store.save(state['run_id'], state)
    real_connection = store.connection

    class BadConn:
        def __init__(self, real):
            self.real = real

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.real.__exit__(*args)

        def execute(self, sql, params=()):
            if sql.startswith('INSERT INTO settlements'):
                raise RuntimeError('simulated disk failure')
            return self.real.execute(sql, params)

        def rollback(self):
            return self.real.rollback()

    monkeypatch.setattr(store, 'connection', lambda: BadConn(real_connection()))
    with pytest.raises(RuntimeError):
        store.save('brand-new-run-id', state)
    monkeypatch.setattr(store, 'connection', real_connection)
    ledger = store.list_settlements()
    assert len(ledger) == 1 and ledger[0]['status'] == '有效' and ledger[0]['run_id'] == state['run_id']


def test_payment_is_idempotent_and_voided_run_cannot_be_paid(client):
    first = client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': roster()}}).get_json()
    pay1 = client.post('/api/settlements/%s/payment' % first['run_id'])
    pay2 = client.post('/api/settlements/%s/payment' % first['run_id'])
    assert pay1.status_code == pay2.status_code == 200
    assert pay1.get_json() == pay2.get_json()
    # 同源重跑复用 run_id，不会产生第二笔付款
    client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': roster()}})
    assert len([x for x in client.get('/api/settlements').get_json() if x['paid']]) == 1
    changed = roster(); changed[1]['monthly_salary'] = '9000.00'
    second = client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': changed}}).get_json()
    blocked = client.post('/api/settlements/%s/payment' % first['run_id'])
    assert blocked.status_code == 409 and blocked.get_json()['code'] == 'VOID'
    assert client.post('/api/settlements/%s/payment' % second['run_id']).status_code == 200


def test_export_only_serves_effective_settlement(client):
    first = client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': roster()}}).get_json()
    bundle = client.get('/api/archive/%s/export.csv' % first['run_id'])
    assert bundle.status_code == 200 and '付款' in bundle.data.decode('utf-8-sig')
    changed = roster(); changed[1]['monthly_salary'] = '9000.00'
    client.post('/api/runs', json={'sample': 'monthly.csv', 'parameters': {'roster': changed}})
    blocked = client.get('/api/archive/%s/export.csv' % first['run_id'])
    assert blocked.status_code == 409 and blocked.get_json()['code'] == 'VOID'


def test_roster_validation_rules():
    from app.modules.m8_monthly.rules import parse_roster
    with pytest.raises(DomainError) as e:
        parse_roster([{'employee': 'X', 'pay_type': 'monthly'}])
    assert e.value.code == 'ROSTER'
    with pytest.raises(DomainError):
        parse_roster([{'employee': 'X', 'pay_type': 'monthly', 'monthly_salary': '0',
                       'hire_date': '2026-10-01', 'leave_date': None}])
    with pytest.raises(DomainError):
        parse_roster([{'employee': 'X', 'pay_type': 'monthly', 'monthly_salary': '100',
                       'hire_date': '2026-10-02', 'leave_date': '2026-10-01'}])
    with pytest.raises(DomainError):
        parse_roster('not-a-list')


def test_shift_outside_employment_window_fails_whole_settlement():
    bad = roster()
    for entry in bad:
        if entry['employee'] == 'M001':
            entry['leave_date'] = '2026-09-30'
    with pytest.raises(DomainError) as e:
        run(load('monthly.csv'), {'roster': bad})
    assert e.value.code == 'ROSTER'


def test_roster_hash_changes_with_snapshot_and_is_embedded():
    a = monthly_state()['monthly']
    changed = roster(); changed[1]['monthly_salary'] = '9000.00'
    b = run(load('monthly.csv'), {'roster': changed})['monthly']
    assert a['roster_hash'] != b['roster_hash']
    assert a['roster_snapshot'] and len(a['roster_snapshot']) == 5
