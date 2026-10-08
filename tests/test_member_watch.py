from datetime import date, datetime, timedelta

from core.monitoring import build_monitor_snapshot, get_monitor_snapshot


def event(minute=0, member='member', asset='余币', source='source', kind='balance_grant', amount=10, venue='A'):
    return {'source': source, 'member_ref': member, 'venue': venue,
            'external_id': str(minute), 'occurred_at': datetime(2026,9,14,12) + timedelta(minutes=minute),
            'event_type': kind, 'amount': amount, 'balance_after': 100,
            'raw_json': {'store_value_name': asset, 'operation_channel': '微信小程序'}}


def watch(events):
    return build_monitor_snapshot(events, 1, date(2026,9,14), 'live')['member_watch']


def test_rolling_window_includes_boundary_and_preserves_missing_price():
    result = watch([event(8), event(12), event(18)])
    member = result['members'][0]
    assert result['flagged_members'] == 1
    assert member['clues'][0]['count'] == 3
    assert member['clues'][0]['start'].endswith('12:08:00')
    assert member['coin_value'] is None
    assert watch([event(0), event(5), event(11)])['flagged_members'] == 0


def test_members_sources_assets_dates_and_anonymous_do_not_merge():
    events = [event(), event(1), event(2, asset='娃娃积分'), event(2, source='other'),
              event(2, member='other'), event(2, venue='B'), event(2, member='匿名会员'), event(-1440)]
    result = watch(events)
    assert result['flagged_members'] == 0
    assert result['excluded_anonymous_events'] == 1
    assert sum(m['event_count'] for m in result['members']) == 6


def test_consumption_frequency_and_backend_adjustment():
    events = [event(i/10, kind='balance_consume', amount=-1) for i in range(30)]
    adjustment = event(9, kind='balance_adjust', amount=20)
    adjustment['raw_json']['operation_channel'] = '管理后台'
    events.append(adjustment)
    result = watch(events)['members'][0]
    assert {c['rule'] for c in result['clues']} == {'backend_adjustment'}
    assert result['assets'][0]['changes']['balance_consume'] == -30


def test_member_watch_respects_server_scope(monkeypatch):
    import core.monitoring as monitoring
    monkeypatch.setattr(monitoring, '_load_live_events', lambda *args: [event(i, venue=v) for v in ['A','B'] for i in range(3)])
    monkeypatch.setattr(monitoring, '_get_monitor_syncs_for_date', lambda *args: {})
    result = get_monitor_snapshot(target_date='2026-09-14', venue_scope={'A'})
    assert result['member_watch']['member_count'] == 1
    assert all(m['venue']=='A' for m in result['member_watch']['members'])
    assert get_monitor_snapshot(target_date='2026-09-14', venue_scope=set())['member_watch']['members'] == []


def test_night_boundary_and_non_consumption_exclusion():
    events = []
    for hour, minute, record in [(0,0,'设备消费'), (5,59,'设备消费'), (6,0,'设备消费'), (2,0,'扣减'), (3,0,'转账转出')]:
        e = event(kind='balance_consume', amount=-1)
        e['occurred_at'] = datetime(2026,9,14,hour,minute)
        e['raw_json']['record_type'] = record
        events.append(e)
    clues = watch(events)['members'][0]['clues']
    assert len(clues) == 1
    assert clues[0]['rule'] == 'night_consumption'
    assert clues[0]['count'] == 2


def test_large_transfer_keeps_asset_and_direction_distinct_from_consumption():
    events = []
    for amount, asset, record in [(999,'余币','转账转入'),(-1000,'余币','转账转出'),(2000,'娃娃积分','转账转入'),(2000,'余币','转换转入')]:
        e = event(kind='balance_consume' if amount<0 else 'balance_adjust', amount=amount, asset=asset)
        e['raw_json']['record_type'] = record
        events.append(e)
    clues = watch(events)['members'][0]['clues']
    assert len(clues) == 1
    assert clues[0]['rule'] == 'large_transfer'
    assert clues[0]['count'] == 1 and clues[0]['amount'] == 1000


def test_single_backend_grant_is_reviewed_without_frequency():
    e = event()
    e['raw_json']['operation_channel'] = '管理后台'
    assert watch([e])['members'][0]['clues'][0]['rule'] == 'backend_grant'


def test_small_transfers_accumulate_separately_by_direction():
    events = []
    for amount in [400, 600, -400, -300, -300]:
        e = event(kind='balance_adjust', amount=amount)
        e['raw_json']['record_type'] = '转账转入' if amount > 0 else '转账转出'
        events.append(e)
    clues = watch(events)['members'][0]['clues']
    assert {c['rule'] for c in clues} == {'split_transfer_in', 'split_transfer_out', 'repeated_transfer_out'}
    assert all(c['amount'] == 1000 for c in clues)
    assert watch(events[:1] + events[2:3])['flagged_members'] == 0


def test_frequent_receipts_detected_even_below_total_threshold():
    events = []
    for i in range(3):
        e = event(i, kind='balance_grant', amount=10)
        e['raw_json']['record_type'] = '会员转赠转入'
        events.append(e)
    clues = watch(events)['members'][0]['clues']
    assert {c['rule'] for c in clues} == {'repeated_transfer_in'}
