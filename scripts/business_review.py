"""只读生成 7、8 月同店经营复盘；不作为财务核准结果。"""
import calendar
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawlers import report_summary


def main():
    output = ROOT / 'outputs/business-review-20260914'
    output.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f'file:{(ROOT / "data/app.db").as_posix()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('BEGIN')
    periods = {}
    try:
        lifecycle = {r['venue']: dict(r) for r in conn.execute('SELECT venue,opened_on,closed_on FROM venue_lifecycle')}
        for month in ('2026-07', '2026-08'):
            runs = [dict(r) for r in conn.execute('SELECT id,status,days_expected FROM outbound_collection_runs WHERE month=? AND is_current=1', (month,))]
            if len(runs) != 1 or runs[0]['status'] != 'succeeded':
                raise ValueError(f'{month} 没有唯一成功批次')
            run = runs[0]
            days = [dict(r) for r in conn.execute('SELECT business_date,status FROM outbound_collection_days WHERE run_id=?', (run['id'],))]
            expected = {f'{month}-{d:02d}' for d in range(1, calendar.monthrange(int(month[:4]), int(month[5:]))[1]+1)}
            complete = {r['business_date'] for r in days if r['status'] == 'succeeded'} == expected
            gifts = [dict(r) for r in conn.execute("SELECT venue,mapping_status,COUNT(*) AS records,-SUM(stock_count) AS quantity,-SUM(cost_cents)/100.0 AS cost,SUM(cost_cents=0) AS zero_cost,SUM(review) AS review_count FROM outbound_stock_records WHERE run_id=? AND business_type='设备出礼' GROUP BY venue,mapping_status", (run['id'],))]
            venues = {g['venue'] for g in gifts if g['mapping_status'] == 'matched' and g['venue']}
            date = max(expected)
            snapshots = [dict(r) for r in conn.execute('SELECT venue,platform,metrics_json,source_task_id FROM daily_summary WHERE date=?', (date,)) if r['venue'] in venues]
            inputs = [{**json.loads(r['metrics_json']), '场地': r['venue']} for r in snapshots]
            table = report_summary.main(inputs, active_venues=venues) if inputs else []
            income = {r[table[0].index('场地')]: r[table[0].index('收入汇总')] for r in table[1:]} if table else {}
            for g in gifts:
                rows = [r for r in snapshots if r['venue'] == g['venue']]
                g['income'] = income.get(g['venue']) if rows else None
                g['sources'] = sorted({r['platform'] for r in rows})
                g['source_task_ids'] = sorted({r['source_task_id'] for r in rows if r['source_task_id']})
            periods[month] = {'run_id': run['id'], 'days_complete': complete, 'days': days, 'stores': gifts}
    finally:
        conn.close()
    before = {r['venue']: r for r in periods['2026-07']['stores']}
    after = {r['venue']: r for r in periods['2026-08']['stores']}
    rows = []
    for venue in sorted(before.keys() & after.keys()):
        a, b = before[venue], after[venue]
        reasons = []
        life = lifecycle.get(venue, {})
        if not life.get('opened_on'):
            reasons.append('开业日期缺失，全月营业待确认')
        elif life['opened_on'] > '2026-07-01':
            reasons.append('比较期间内开业，不宜直接同店比较')
        if life.get('closed_on') and life['closed_on'] < '2026-08-31':
            reasons.append('比较期间内闭店，不宜直接同店比较')
        if a['sources'] != b['sources'] or not a['sources']:
            reasons.append('月末来源集合不一致或缺失')
        if not all(p['days_complete'] for p in periods.values()):
            reasons.append('出礼采集日期未完整验收')
        if a['zero_cost'] or b['zero_cost'] or a['review_count'] or b['review_count']:
            reasons.append('存在零成本或待核对出礼')
        valid = all(isinstance(x['income'], (int, float)) and x['income'] > 0 for x in (a, b))
        if not valid:
            reasons.append('收入缺失或非正数')
        rows.append({'venue': venue, 'lifecycle': life, 'july': a, 'august': b, 'issues': reasons,
                     'income_change': round(b['income']-a['income'], 2) if valid and a['sources']==b['sources'] else None,
                     'cost_rate_july': a['cost']/a['income'] if not reasons else None,
                     'cost_rate_august': b['cost']/b['income'] if not reasons else None})
    report = {'generated_at': datetime.now().isoformat(), 'status': '经营诊断草稿，来源适用性和金额尚需业务核准', 'periods': periods, 'comparisons': rows}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    fmt = lambda value: '待核对' if value is None else f'{value:,.2f}'
    lines = ['# 7—8 月同店收入与出礼成本复盘', '', '经营诊断草稿：月末收入沿用现有报表公式，只取月末当日快照。来源集合相同不证明来源完整、无重叠或两期全月营业；收入变化仅为候选线索。出礼成本仅包含设备出礼，不含采购、调拨、盘点和房租人工，不能解释为净利润。', '', '| 门店 | 7月收入 | 8月收入 | 候选收入变化 | 7月出礼成本 | 8月出礼成本 | 核查事项 |', '|---|---:|---:|---:|---:|---:|---|']
    for r in sorted(rows, key=lambda r: r['income_change'] if r['income_change'] is not None else float('inf')):
        a,b=r['july'],r['august']
        lines.append(f"| {r['venue']} | {fmt(a['income'])} | {fmt(b['income'])} | {fmt(r['income_change'])} | {fmt(a['cost'])} | {fmt(b['cost'])} | {'；'.join(r['issues']) or '待确认来源适用性、全月营业和源头金额'} |")
    lines += ['', '## 先处理的三项工作', '', '1. 财务与采集维护者确认各店两个月应有收入来源、全月营业情况及来源是否重叠，再核准增长结论。', '2. 库存负责人按 JSON 中各店 zero_cost、review_count 优先复核零成本出礼及待核对记录；修订前暂不发布相关成本率。', '3. 店长对候选收入降幅最大的门店核对原始订单、退款、活动和营业天数，先确认金额，再判断经营原因。', '', '详细批次、逐日采集状态、来源集合与任务关联见 report.json。']
    lines += ['', '## 出礼异常核查清单', '', '| 门店 | 月份 | 零成本记录 | 待核对记录（可能重叠） |', '|---|---|---:|---:|']
    for r in rows:
        for key, month in (('july', '2026-07'), ('august', '2026-08')):
            g = r[key]
            if g['zero_cost'] or g['review_count']:
                lines.append(f"| {r['venue']} | {month} | {g['zero_cost']} | {g['review_count']} |")
    (output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'stores':len(rows),'days_complete':{m:p['days_complete'] for m,p in periods.items()},'output':str(output)},ensure_ascii=False))


if __name__ == '__main__':
    main()
