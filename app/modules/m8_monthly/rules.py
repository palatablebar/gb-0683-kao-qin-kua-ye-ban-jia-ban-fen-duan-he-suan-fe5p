"""付款月结规则。

裁决要点（见 docs/DATA-DICTIONARY.md「付款月结」节）：
- 月薪员工的加班基数 = 月结生效时人事快照里的月薪 / 21.75 / 8（即月薪/174），
  与当月自然日和实际排班都无关，因此少排班不会压低基数，事后补录排班也改不动已复核月结。
- 月薪部分按在职自然日 / 期间自然日折算（仅依赖入离职日期，同样不受排班补录影响）。
- 计薪片段沿用 m5 切段：工作日8小时内归 REGULAR（月薪已覆盖，不再重复计费），
  超出部分按 1.5 倍计；周末 2 倍、法定假日 3 倍（跨夜按实际日历日归属）。
- 计时员工每个片段按打卡时薪 × 倍率计费，与 m5 金额完全一致。
全部金额用 Decimal 算到分。
"""
import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from app.errors import DomainError

CENT = Decimal('.01')
MONTHLY_HOURS = Decimal('174')          # 21.75 计薪日 × 8 小时
PREMIUM = {'REGULAR': Decimal('1'), 'OVERTIME': Decimal('1.5'),
           'REST': Decimal('2'), 'HOLIDAY': Decimal('3')}
REQUIRED_FIELDS = ('employee', 'pay_type')


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _parse_day(value, field, employee):
    if value in (None, ''):
        return None
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise DomainError('ROSTER', '员工 %s 的 %s 不是 ISO 日期' % (employee, field))


def parse_roster(raw):
    """校验人事快照，返回 {employee: 档案}。原始入参永不被删除或改写。"""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise DomainError('ROSTER', '人事档案必须是数组')
    roster = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise DomainError('ROSTER', '人事档案条目必须是对象')
        for field in REQUIRED_FIELDS:
            if str(entry.get(field, '')).strip() in ('', '-', 'N/A', 'NULL', '未知'):
                raise DomainError('ROSTER', '人事档案缺少 %s' % field)
        employee = str(entry['employee'])
        pay_type = str(entry['pay_type']).strip().lower()
        if pay_type not in ('monthly', 'hourly'):
            raise DomainError('ROSTER', '员工 %s 计薪类型无效' % employee)
        hire = _parse_day(entry.get('hire_date'), 'hire_date', employee)
        leave = _parse_day(entry.get('leave_date'), 'leave_date', employee)
        if leave and hire and leave < hire:
            raise DomainError('ROSTER', '员工 %s 离职早于入职' % employee)
        salary = None
        if pay_type == 'monthly':
            try:
                salary = Decimal(str(entry['monthly_salary']))
            except (ValueError, TypeError):
                raise DomainError('ROSTER', '员工 %s 月薪无效' % employee)
            except KeyError:
                raise DomainError('ROSTER', '员工 %s 缺少月薪' % employee)
            if salary <= 0:
                raise DomainError('ROSTER', '员工 %s 月薪必须为正' % employee)
        if employee in roster:
            raise DomainError('ROSTER', '员工 %s 人事档案重复' % employee)
        roster[employee] = {'employee': employee, 'pay_type': pay_type,
                            'monthly_salary': str(salary) if salary is not None else None,
                            'hire_date': hire.isoformat() if hire else None,
                            'leave_date': leave.isoformat() if leave else None}
    return roster


def _period(state):
    days = sorted({r['shift_day'] for r in state.get('rows', [])} |
                  {s['shift_day'] for s in state.get('segments', [])})
    if not days:
        raise DomainError('PERIOD', '没有可结算的班次日，无法确定月结期间')
    return days[0][:7], date.fromisoformat(days[0]), date.fromisoformat(days[-1])


def _employment_window(profile, period_start, period_end):
    """在职自然日区间与期间取交集。"""
    hire = date.fromisoformat(profile['hire_date']) if profile['hire_date'] else period_start
    leave = date.fromisoformat(profile['leave_date']) if profile['leave_date'] else period_end
    left = max(hire, period_start)
    right = min(leave, period_end)
    return left, right


def settle(state):
    roster = parse_roster(state.get('parameters', {}).get('roster'))
    period, first_day, last_day = _period(state)
    period_start = first_day.replace(day=1)
    if last_day.month == 12:
        period_end = last_day.replace(day=31)
    else:
        period_end = last_day.replace(month=last_day.month + 1, day=1)
        period_end = date.fromordinal(period_end.toordinal() - 1)
    calendar_days = period_end.toordinal() - period_start.toordinal() + 1

    employees = sorted({r['employee'] for r in state['rows']} |
                       {r['employee'] for r in state['segments']} | set(roster))
    segments = state.get('calculations', [])
    rows_out = []
    for employee in employees:
        profile = roster.get(employee)
        emp_segments = [s for s in segments if s['employee'] == employee]
        minutes = sum(s['minutes'] for s in emp_segments)
        normal_minutes = sum(s['minutes'] for s in emp_segments if s['code'] == 'REGULAR')
        premium_minutes = minutes - normal_minutes
        if profile is None or profile['pay_type'] == 'hourly':
            pay_type = 'hourly'
            base_hourly = None
            fixed = Decimal('0')
            employed_days = None
            payable = sum((Decimal(s['amount']) for s in emp_segments), Decimal('0'))
            basis = '打卡时薪 × 倍率，逐片段计'
        else:
            pay_type = 'monthly'
            left, right = _employment_window(profile, period_start, period_end)
            if right < left:
                raise DomainError('ROSTER', '员工 %s 在 %s 无在职自然日' % (employee, period))
            employed_days = right.toordinal() - left.toordinal() + 1
            monthly_salary = Decimal(profile['monthly_salary'])
            fixed = money(monthly_salary * Decimal(employed_days) / Decimal(calendar_days))
            base_hourly = (monthly_salary / MONTHLY_HOURS).quantize(Decimal('0.0001'),
                                                                     rounding=ROUND_HALF_UP)
            # 月薪已覆盖 REGULAR 工时；其余片段只付加成部分（倍率-1）×基数。
            premium = Decimal('0')
            for s in emp_segments:
                extra = PREMIUM[s['code']] - Decimal('1')
                if extra > 0:
                    premium += base_hourly * Decimal(s['minutes']) / Decimal('60') * extra
            premium = money(premium)
            payable = money(fixed + premium)
            basis = '月薪/174 得时基；月薪按在职自然日 %d/%d 折算' % (employed_days, calendar_days)
            # 班次落在在职窗口之外说明人事与考勤对不上，整批失败，不产出半份月结。
            for s in emp_segments:
                d = date.fromisoformat(s['calendar_day'])
                if d < left or d > right:
                    raise DomainError('ROSTER', '员工 %s 的 %s 班次落在在职区间外' % (employee, d))
        rows_out.append({
            'employee': employee, 'pay_type': pay_type, 'period': period,
            'base_hourly': str(base_hourly) if base_hourly is not None else None,
            'monthly_salary': profile['monthly_salary'] if profile else None,
            'employed_days': employed_days, 'calendar_days': calendar_days,
            'fixed_pay': str(fixed), 'actual_minutes': minutes,
            'normal_minutes': normal_minutes, 'premium_minutes': premium_minutes,
            'payable': str(payable), 'basis': basis,
        })

    total_payable = sum((Decimal(r['payable']) for r in rows_out), Decimal('0'))
    total_minutes = sum(r['actual_minutes'] for r in rows_out)
    # 计时员工金额必须与 m5 逐分逐元对应；分钟总数必须守恒。
    hourly_from_m5 = sum((Decimal(s['amount']) for s in segments
                          if roster.get(s['employee'], {}).get('pay_type') != 'monthly'),
                         Decimal('0'))
    hourly_from_m8 = sum((Decimal(r['payable']) for r in rows_out if r['pay_type'] == 'hourly'),
                         Decimal('0'))
    balanced = (money(hourly_from_m5) == money(hourly_from_m8)
                and total_minutes == (state.get('business', {}).get('minutes') or total_minutes))
    return {
        'period': period,
        'rule_version': state['manifest']['rule_version'] if 'manifest' in state else None,
        'roster_hash': _roster_hash(roster),
        'roster_snapshot': list(roster.values()),
        'rows': rows_out,
        'totals': {'employees': len(rows_out), 'minutes': total_minutes,
                   'payable': str(money(total_payable)), 'unit': 'CNY'},
        'balanced': balanced,
    }


def _roster_hash(roster):
    raw = json.dumps(roster, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


def apply(state):
    state['monthly'] = settle(state)
    # 月结口径覆盖业务汇总：应发=本单可付款合计，差额相对打卡实发重新给出。
    paid = sum((Decimal(str(r['paid'])) for r in state['rows']), Decimal('0'))
    state['business'] = dict(state.get('business', {}))
    state['business']['total'] = state['monthly']['totals']['payable']
    state['business']['paid'] = str(paid)
    state['business']['difference'] = str(money(Decimal(state['business']['total']) - paid))
    return state
