"""已授权范围内的会员行为线索，不计算缺少实付证据的币值。"""
from collections import defaultdict
from datetime import timedelta


def _peak(events, minutes=10):
    ordered = sorted(events, key=lambda e: e['occurred_at'])
    left = 0
    best_left, best_right = 0, 0
    for right, event in enumerate(ordered):
        while event['occurred_at'] - ordered[left]['occurred_at'] > timedelta(minutes=minutes):
            left += 1
        if right - left + 1 > best_right - best_left:
            best_left, best_right = left, right + 1
    best = ordered[best_left:best_right]
    return {'count': len(best),
            'start': best[0]['occurred_at'].isoformat() if best else None,
            'end': best[-1]['occurred_at'].isoformat() if best else None}


def member_watch(events):
    grouped = defaultdict(list)
    excluded = 0
    for event in events:
        member = str(event.get('member_ref') or '').strip()
        if not member or member == '匿名会员':
            excluded += 1
            continue
        grouped[(event.get('source', ''), event.get('venue', ''), member)].append(event)
    members = []
    for (source, venue, member), records in grouped.items():
        assets = defaultdict(list)
        for event in records:
            assets[(event['asset_name'], event['asset_group'], event['unit'])].append(event)
        summaries, clues = [], []
        for (name, group, unit), items in assets.items():
            transfers = [e for e in items if any(word in e.get('record_type', '') for word in ('转账', '转赠'))]
            grants = [e for e in items if e['event_type'] == 'balance_grant' and e['amount'] > 0 and e not in transfers]
            consumes = [e for e in items if e['event_type'] in ('balance_consume', 'points_redeem') and e['amount'] < 0
                        and e not in transfers and (not e.get('record_type') or '消费' in e['record_type'])]
            adjustments = [e for e in items if e['event_type'] in ('balance_adjust', 'points_adjust') and e.get('operation_channel') == '管理后台']
            if group == 'coin':
                for direction, direction_label, sign in (('in', '转入', 1), ('out', '转出', -1)):
                    directed = [e for e in transfers if e['amount'] * sign > 0]
                    small = [e for e in directed if abs(e['amount']) < 1000]
                    for rule, label, selected, triggered in (
                        ('repeated_transfer_' + direction, '所选期间多次' + direction_label + '待核查', directed, len(directed) >= 3),
                        ('split_transfer_' + direction, '多笔小额' + direction_label + '累计至少1000币', small,
                         len(small) >= 2 and sum(abs(e['amount']) for e in small) >= 1000),
                    ):
                        if triggered:
                            clues.append({'rule': rule, 'label': label, 'asset_name': name,
                                          'count': len(selected), 'amount': round(sum(abs(e['amount']) for e in selected), 2), 'unit': unit,
                                          'start': min(e['occurred_at'] for e in selected).isoformat(),
                                          'end': max(e['occurred_at'] for e in selected).isoformat()})
            for rule, label, selected, threshold in (
                ('concentrated_grant', '10分钟内集中赠送', grants, 3),
            ):
                peak = _peak(selected)
                if peak['count'] >= threshold:
                    clues.append({'rule': rule, 'label': label, 'asset_name': name,
                                  'threshold': threshold, **peak})
            for rule, label, selected in (
                ('night_consumption', '凌晨00:00—06:00消费待核查', [e for e in consumes if 0 <= e['occurred_at'].hour < 6]),
                ('large_transfer', '单笔至少1000币的转账/转赠待核查', [e for e in transfers if group == 'coin' and abs(e['amount']) >= 1000]),
                ('backend_grant', '管理后台赠送待核对', [e for e in grants if e.get('operation_channel') == '管理后台']),
            ):
                if selected:
                    clues.append({'rule': rule, 'label': label, 'asset_name': name,
                                  'count': len(selected), 'amount': round(sum(abs(e['amount']) for e in selected), 2), 'unit': unit,
                                  'start': min(e['occurred_at'] for e in selected).isoformat(),
                                  'end': max(e['occurred_at'] for e in selected).isoformat()})
            if adjustments:
                clues.append({'rule': 'backend_adjustment', 'label': '管理后台调整待核对',
                              'asset_name': name, 'count': len(adjustments),
                              'start': min(e['occurred_at'] for e in adjustments).isoformat(),
                              'end': max(e['occurred_at'] for e in adjustments).isoformat()})
            totals = defaultdict(float)
            for e in items:
                kind = ('transfer_in' if e['amount'] >= 0 else 'transfer_out') if e in transfers else e['event_type']
                totals[kind] += e['amount']
            summaries.append({'asset_name': name, 'asset_group': group, 'unit': unit,
                              'event_count': len(items),
                              'changes': {k: round(v, 2) for k, v in sorted(totals.items())}})
        members.append({'source': source, 'venue': venue, 'member_ref': member,
                        'event_count': len(records), 'assets': summaries, 'clues': clues,
                        'coin_value': None, 'coin_value_status': 'missing_payment_data'})
    members.sort(key=lambda m: (-len(m['clues']), -m['event_count'], m['venue'], m['member_ref']))
    return {'member_count': len(members), 'flagged_members': sum(bool(m['clues']) for m in members),
            'excluded_anonymous_events': excluded, 'members': members[:100],
            'truncated': len(members) > 100,
            'rule_note': '核查规则：同来源、同店、同会员、同资产，10分钟赠送至少3笔；后台赠送及调整；转账/转赠单笔至少1000币，或所选期间同方向至少3笔，或同方向多笔单笔不足1000币累计至少1000币；凌晨00:00至06:00（不含06:00）消费。高频消费不提醒。夜间按源平台时间，正常夜间营业需人工排除；转入/转出分别核查，同一流水可触发多个线索，不等同于已识别交易双方。',
            'coin_value_note': '币值=会员实付人民币÷获得币数。当前缺少实付数据，约0.6元/币仅作业务参考，尚未用于判定。'}
