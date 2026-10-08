# -*- coding: utf-8 -*-
"""出货报表批次、原子发布及只读分析；不参与累计货款公式。"""
import calendar
from contextlib import contextmanager, closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import uuid

from core.db import get_connection
from crawlers.duojinbao_equipment_stock_crawler import HEADQUARTERS_NAME, month_dates


SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_collection_runs (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, month TEXT NOT NULL,
 headquarters TEXT NOT NULL, headquarters_id TEXT NOT NULL DEFAULT '',
 account_fingerprint TEXT NOT NULL DEFAULT '', mapping_json TEXT NOT NULL DEFAULT '{}',
 stores_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'queued',
 is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1)),
 days_expected INTEGER NOT NULL, current_date TEXT NOT NULL DEFAULT '',
 current_page INTEGER NOT NULL DEFAULT 0, current_pages INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', actor_user_id TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_current
 ON outbound_collection_runs(headquarters,month) WHERE is_current=1;
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_inflight
 ON outbound_collection_runs(headquarters,month) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS outbound_collection_days (
 run_id TEXT NOT NULL REFERENCES outbound_collection_runs(id), business_date TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', source_total INTEGER, source_pages INTEGER,
 cost_cents INTEGER, record_count INTEGER, error TEXT NOT NULL DEFAULT '', finished_at TEXT,
 PRIMARY KEY(run_id,business_date)
);
CREATE TABLE IF NOT EXISTS outbound_stock_records (
 run_id TEXT NOT NULL REFERENCES outbound_collection_runs(id), business_date TEXT NOT NULL,
 row_no INTEGER NOT NULL, source_store TEXT NOT NULL, venue TEXT,
 mapping_status TEXT NOT NULL, operation_time TEXT NOT NULL, business_type TEXT NOT NULL,
 sku_id TEXT NOT NULL, sku_name TEXT NOT NULL, sku_category TEXT NOT NULL,
 equipment_no TEXT NOT NULL, equipment_name TEXT NOT NULL, equipment_region TEXT NOT NULL,
 original_count INTEGER NOT NULL, stock_count INTEGER NOT NULL, after_count INTEGER NOT NULL,
 predict_cost TEXT NOT NULL, cost_cents INTEGER NOT NULL,
 issues_json TEXT NOT NULL, review INTEGER NOT NULL,
 PRIMARY KEY(run_id,business_date,row_no)
);
CREATE INDEX IF NOT EXISTS idx_outbound_store_date
 ON outbound_stock_records(run_id,source_store,business_date);
CREATE INDEX IF NOT EXISTS idx_outbound_type ON outbound_stock_records(run_id,business_type);
"""
KNOWN_TYPES = {"设备出礼", "调入设备", "设备调出", "盘盈", "盘亏", "设备库存初始化", "设备清零"}


def _now():
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def _transaction():
    with closing(get_connection()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _month(month):
    start = datetime.strptime(month, "%Y-%m").date()
    if start.strftime("%Y-%m") != month or start.year < 2000:
        raise ValueError("月份格式必须为 YYYY-MM，年份不得早于 2000")
    return start


def month_sequence(start_month, end_month):
    start, end = _month(start_month), _month(end_month)
    if start > end:
        raise ValueError("开始月份不能晚于结束月份")
    result = []
    while start <= end:
        result.append(start.strftime("%Y-%m"))
        start = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def queue_months(start_month, end_month, actor_user_id=""):
    months = month_sequence(start_month, end_month)
    expected = {month: month_dates(month) for month in months}
    batch = uuid.uuid4().hex
    with _transaction() as conn:
        if conn.execute("SELECT 1 FROM outbound_collection_runs WHERE status IN ('queued','running')").fetchone():
            raise ValueError("已有出货采集任务，请等待完成或先停止任务")
        ids = []
        for month in months:
            run_id = uuid.uuid4().hex
            ids.append(run_id)
            conn.execute("INSERT INTO outbound_collection_runs "
                         "(id,batch_id,month,headquarters,days_expected,actor_user_id,created_at) VALUES (?,?,?,?,?,?,?)",
                         (run_id, batch, month, HEADQUARTERS_NAME, len(expected[month]), actor_user_id, _now()))
            conn.executemany("INSERT INTO outbound_collection_days(run_id,business_date) VALUES (?,?)",
                             [(run_id, day) for day in expected[month]])
    return ids


def get_run(run_id):
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT * FROM outbound_collection_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise ValueError("采集批次不存在")
    return dict(row)


def public_run(row):
    return {key: value for key, value in dict(row).items()
            if key not in ("mapping_json", "stores_json", "account_fingerprint", "actor_user_id")}


def begin_run(run_id):
    with _transaction() as conn:
        return conn.execute("UPDATE outbound_collection_runs SET status='running',started_at=?,finished_at=NULL,error='' "
                            "WHERE id=? AND status='queued'", (_now(), run_id)).rowcount == 1


def active(run_id):
    return get_run(run_id)["status"] == "running"


def bind_source(run_id, headquarters_id, account_fingerprint, mapping, stores):
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM outbound_collection_runs WHERE id=?", (run_id,)).fetchone()
        if not row or row["status"] != "running":
            raise ValueError("采集任务已停止")
        if row["headquarters_id"]:
            if row["headquarters_id"] != str(headquarters_id) or row["account_fingerprint"] != account_fingerprint:
                raise ValueError("总部或账号已变化，请重新采集整月，不能混用已有日期")
            return json.loads(row["mapping_json"])
        conn.execute("UPDATE outbound_collection_runs SET headquarters_id=?,account_fingerprint=?,mapping_json=?,stores_json=? WHERE id=?",
                     (str(headquarters_id), account_fingerprint, json.dumps(mapping, ensure_ascii=False),
                      json.dumps(stores, ensure_ascii=False), run_id))
    return mapping


def update_progress(run_id, day, page, pages):
    with _transaction() as conn:
        changed = conn.execute("UPDATE outbound_collection_runs SET current_date=?,current_page=?,current_pages=? "
                               "WHERE id=? AND status='running'", (day, page, pages, run_id)).rowcount
    if not changed:
        raise ValueError("采集任务已停止")


def completed_days(run_id):
    with closing(get_connection()) as conn:
        return {row[0] for row in conn.execute("SELECT business_date FROM outbound_collection_days WHERE run_id=? AND status='succeeded'", (run_id,))}


def _decimal(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("库存流水缺少有效的%s" % field) from None
    if not number.is_finite():
        raise ValueError("库存流水%s不是有限数值" % field)
    return number


def _record(run_id, day, index, row, mapping):
    occurred = datetime.strptime(str(row.get("operationTime")), "%Y-%m-%d %H:%M:%S")
    if occurred.date().isoformat() != day:
        raise ValueError("流水日期超出请求范围")
    name = str(row.get("storeName") or "").strip()
    kind = str(row.get("businessTypeDesc") or "").strip()
    if not name or not kind:
        raise ValueError("库存流水缺少门店或变更类型")
    quantities = [row.get(key) for key in ("originalCount", "stockCount", "changeAfterCount")]
    if any(type(value) is not int for value in quantities):
        raise ValueError("库存数量必须是整数，缺失值不能填零")
    cost = _decimal(row.get("sumCost"), "变更成本")
    predict = _decimal(row.get("skuPredictCost"), "预估单价")
    cents = cost * 100
    if cents != cents.to_integral_value() or abs(cents) > 10**15:
        raise ValueError("变更成本精度或范围异常")
    candidates = mapping.get(name, [])
    venue = candidates[0] if len(candidates) == 1 else None
    mapping_status = "matched" if venue else "ambiguous" if candidates else "unmatched"
    before, change, after = quantities
    issues = []
    if not venue:
        issues.append("门店待匹配")
    if before + change != after:
        issues.append("库存数量不平衡")
    if abs(predict * change - cost) > Decimal("0.01"):
        issues.append("成本乘法差异")
    if kind == "设备出礼":
        if cost == 0:
            issues.append("零成本出礼")
        if change >= 0 or cost > 0:
            issues.append("出礼方向待核对")
    if kind not in KNOWN_TYPES:
        issues.append("未知变更类型")
    return (run_id, day, index, name, venue, mapping_status, row["operationTime"], kind,
            str(row.get("skuId") or ""), str(row.get("skuName") or ""), str(row.get("skuCategory") or ""),
            str(row.get("iotEquipmentNo") or ""), str(row.get("equipmentName") or ""), str(row.get("equipmentRegion") or ""),
            before, change, after, str(predict), int(cents), json.dumps(issues, ensure_ascii=False), int(bool(issues)))


def save_day(run_id, day, rows, metadata, mapping):
    records = [_record(run_id, day, index, row, mapping) for index, row in enumerate(rows, 1)]
    if metadata["total"] != len(records):
        raise ValueError("已采集条数与源总数不一致")
    with _transaction() as conn:
        run = conn.execute("SELECT status FROM outbound_collection_runs WHERE id=?", (run_id,)).fetchone()
        existing = conn.execute("SELECT status FROM outbound_collection_days WHERE run_id=? AND business_date=?", (run_id, day)).fetchone()
        if not run or run["status"] != "running" or not existing:
            raise ValueError("采集任务已停止或日期不属于该月份")
        conn.execute("DELETE FROM outbound_stock_records WHERE run_id=? AND business_date=?", (run_id, day))
        conn.executemany("INSERT INTO outbound_stock_records VALUES (" + ",".join(["?"] * 21) + ")", records)
        conn.execute("UPDATE outbound_collection_days SET status='succeeded',source_total=?,source_pages=?,record_count=?,cost_cents=?,error='',finished_at=? "
                     "WHERE run_id=? AND business_date=?",
                     (len(records), metadata["pages"], len(records), sum(row[18] for row in records), _now(), run_id, day))


def publish(run_id):
    with _transaction() as conn:
        run = conn.execute("SELECT * FROM outbound_collection_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "running":
            raise ValueError("已停止的批次不能发布")
        days = conn.execute("SELECT * FROM outbound_collection_days WHERE run_id=? ORDER BY business_date", (run_id,)).fetchall()
        if ([row["business_date"] for row in days] != month_dates(run["month"])
                or any(row["status"] != "succeeded" for row in days)):
            raise ValueError("月份日期未全部完成，不能替换旧版本")
        totals = conn.execute("SELECT COUNT(*),COALESCE(SUM(cost_cents),0) FROM outbound_stock_records WHERE run_id=?", (run_id,)).fetchone()
        if totals[0] != sum(row["source_total"] for row in days) or totals[1] != sum(row["cost_cents"] for row in days):
            raise ValueError("月度记录数或金额对账失败")
        conn.execute("UPDATE outbound_collection_runs SET is_current=0 WHERE headquarters=? AND month=?", (run["headquarters"], run["month"]))
        conn.execute("UPDATE outbound_collection_runs SET status='succeeded',is_current=1,finished_at=?,error='' WHERE id=?", (_now(), run_id))


def fail_run(run_id, error):
    # 调用方传递已经去敏的诊断，不持久化平台登录响应。
    with _transaction() as conn:
        run = conn.execute("SELECT current_date FROM outbound_collection_runs WHERE id=? AND status='running'", (run_id,)).fetchone()
        if run:
            conn.execute("UPDATE outbound_collection_days SET status='failed',error=? WHERE run_id=? AND business_date=? AND status!='succeeded'", (error, run_id, run[0]))
            conn.execute("UPDATE outbound_collection_runs SET status='failed',error=?,finished_at=? WHERE id=?", (error, _now(), run_id))


def cancel_batch(run_id, reason="用户停止采集"):
    with _transaction() as conn:
        run = conn.execute("SELECT batch_id FROM outbound_collection_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            raise ValueError("采集批次不存在")
        conn.execute("UPDATE outbound_collection_runs SET status='cancelled',finished_at=?,error=? "
                     "WHERE batch_id=? AND status IN ('queued','running')", (_now(), reason, run[0]))


def retry_run(run_id):
    with _transaction() as conn:
        run = conn.execute("SELECT status,is_current FROM outbound_collection_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run[0] not in ("failed", "cancelled") or run[1]:
            raise ValueError("只能重试未完成且未发布的批次")
        if conn.execute("SELECT 1 FROM outbound_collection_runs WHERE status IN ('queued','running')").fetchone():
            raise ValueError("已有出货采集任务，请等待完成")
        conn.execute("UPDATE outbound_collection_runs SET status='queued',error='',finished_at=NULL WHERE id=?", (run_id,))
        conn.execute("UPDATE outbound_collection_days SET status='pending',error='' WHERE run_id=? AND status!='succeeded'", (run_id,))


def interrupt_stale_runs():
    with _transaction() as conn:
        conn.execute("UPDATE outbound_collection_runs SET status='failed',error='服务重启，任务中断，可重试剩余日期',finished_at=? "
                     "WHERE status IN ('queued','running')", (_now(),))


def status():
    with closing(get_connection()) as conn:
        rows = conn.execute("SELECT r.*, (SELECT COUNT(*) FROM outbound_collection_days d WHERE d.run_id=r.id AND d.status='succeeded') AS days_succeeded,"
                            "(SELECT COALESCE(SUM(record_count),0) FROM outbound_collection_days d WHERE d.run_id=r.id) AS record_count "
                            "FROM outbound_collection_runs r ORDER BY (r.status IN ('queued','running')) DESC,r.created_at DESC,r.rowid DESC LIMIT 60").fetchall()
        busy = bool(conn.execute("SELECT 1 FROM outbound_collection_runs WHERE status IN ('queued','running') LIMIT 1").fetchone())
    return {"runs": [public_run(row) for row in rows], "busy": busy}


AGGREGATE = """COUNT(*) AS record_count,
 SUM(CASE WHEN business_type='设备出礼' THEN 1 ELSE 0 END) AS gift_records,
 -SUM(CASE WHEN business_type='设备出礼' THEN stock_count ELSE 0 END) AS gift_quantity,
 -SUM(CASE WHEN business_type='设备出礼' THEN cost_cents ELSE 0 END) AS gift_cost_cents,
 SUM(CASE WHEN business_type='设备出礼' AND cost_cents=0 THEN 1 ELSE 0 END) AS zero_cost_count,
 SUM(review) AS review_count"""


def _aggregate(row):
    result = dict(row)
    for key in ("record_count", "gift_records", "gift_quantity", "gift_cost_cents", "zero_cost_count", "review_count"):
        if key in result and result[key] is None:
            result[key] = 0
    if "gift_cost_cents" in result:
        result["gift_cost"] = result["gift_cost_cents"] / 100
    return result


def overview(month, source_store="", venue_scope=None):
    start = _month(month)
    with closing(get_connection()) as conn:
        current = conn.execute("SELECT * FROM outbound_collection_runs WHERE month=? AND is_current=1 AND headquarters=?", (month, HEADQUARTERS_NAME)).fetchone()
        latest = conn.execute("SELECT * FROM outbound_collection_runs WHERE month=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (month,)).fetchone()
        if not current:
            return {"month": month, "status": "uncollected", "run": None, "latest_run": public_run(latest) if latest else None,
                    "summary": None, "stores": [], "daily": [], "types": []}
        run_id = current["id"]
        scope = None if venue_scope is None else {str(value).strip() for value in venue_scope if str(value).strip()}
        clause, params = "run_id=?", [run_id]
        if source_store:
            clause += " AND source_store=?"
            params.append(source_store)
        if scope is not None:
            if not scope:
                return {"month": month, "status": "complete", "run": public_run(current), "latest_run": public_run(latest), "summary": None, "stores": [], "daily": [], "types": []}
            placeholders = ",".join("?" for _ in scope)
            clause += " AND venue IN (" + placeholders + ")"
            params.extend(sorted(scope))
        stores = [_aggregate(row) for row in conn.execute("SELECT source_store,venue,mapping_status," + AGGREGATE +
                 ",COUNT(DISTINCT CASE WHEN business_type='设备出礼' THEN business_date END) AS days_with_gifts "
                 "FROM outbound_stock_records WHERE " + clause + " GROUP BY source_store,venue,mapping_status ORDER BY gift_cost_cents DESC", params)]
        summary = _aggregate(conn.execute("SELECT " + AGGREGATE + ",COUNT(DISTINCT source_store) AS store_count "
                                         "FROM outbound_stock_records WHERE " + clause, params).fetchone())
        if not summary["record_count"]:
            summary["gift_cost"] = summary["gift_cost_cents"] = summary["gift_quantity"] = None
        observed = {row["business_date"]: _aggregate(row) for row in conn.execute(
            "SELECT business_date," + AGGREGATE + " FROM outbound_stock_records WHERE " + clause + " GROUP BY business_date", params)}
        daily = []
        for index in range(calendar.monthrange(start.year, start.month)[1]):
            day = (start + timedelta(days=index)).isoformat()
            daily.append({"date": day, "source_status": "reported" if day in observed else "no_source_records",
                          **observed.get(day, {"record_count": 0, "gift_quantity": None, "gift_cost": None, "zero_cost_count": 0})})
        types = [dict(row) for row in conn.execute("SELECT business_type,COUNT(*) AS records,SUM(stock_count) AS stock_count,"
                "SUM(cost_cents)/100.0 AS cost FROM outbound_stock_records WHERE " + clause + " GROUP BY business_type", params)]
    authorized = json.loads(current["stores_json"])
    if venue_scope is not None:
        authorized = [name for name in authorized if name in {row.get("venue") for row in stores}]
    present_names = {row["source_store"] for row in stores}
    return {"month": month, "status": "complete", "run": public_run(current), "latest_run": public_run(latest),
            "summary": summary, "stores": stores, "daily": daily, "types": types,
            "authorized_store_count": len(authorized), "stores_without_records": [name for name in authorized if name not in present_names]}


def list_months(source_store="", venue_scope=None):
    last = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    with closing(get_connection()) as conn:
        first = conn.execute("SELECT MIN(month) FROM outbound_collection_runs").fetchone()[0] or last
    result = []
    for month in reversed(month_sequence(first, last)):
        item = overview(month, source_store, venue_scope)
        result.append({"month": month, "status": item["status"], "summary": item["summary"],
                       "run": item["run"], "latest_run": item["latest_run"]})
    return {"months": result, "last_closed_month": last}


def details(month, source_store="", day="", business_type="设备出礼", sku_id="", equipment_no="", attention=False,
            group="records", page=1, page_size=50, venue_scope=None):
    _month(month)
    if day and datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m") != month:
        raise ValueError("明细日期不属于选中月份")
    if group not in ("records", "sku", "equipment") or page < 1 or not 1 <= page_size <= 200:
        raise ValueError("明细分组或分页参数无效")
    with closing(get_connection()) as conn:
        current = conn.execute("SELECT id FROM outbound_collection_runs WHERE month=? AND is_current=1", (month,)).fetchone()
        if not current:
            return {"rows": [], "total": 0, "page": page, "page_size": page_size, "status": "uncollected"}
        clause, params = "run_id=?", [current[0]]
        for field, value in (("source_store", source_store), ("business_date", day), ("sku_id", sku_id), ("equipment_no", equipment_no)):
            if value:
                clause += " AND " + field + "=?"
                params.append(value)
        if venue_scope is not None:
            scope = {str(value).strip() for value in venue_scope if str(value).strip()}
            if not scope:
                return {"rows": [], "total": 0, "page": page, "page_size": page_size, "status": "complete"}
            placeholders = ",".join("?" for _ in scope)
            clause += " AND venue IN (" + placeholders + ")"
            params.extend(sorted(scope))
        if business_type == "__adjustments__":
            clause += " AND business_type!='设备出礼'"
        elif business_type:
            clause += " AND business_type=?"
            params.append(business_type)
        if attention:
            clause += " AND review=1"
        if group == "records":
            query = "SELECT *,cost_cents/100.0 AS cost FROM outbound_stock_records WHERE " + clause
            order = " ORDER BY ABS(cost_cents) DESC,operation_time DESC,row_no" if business_type == "__adjustments__" else " ORDER BY operation_time DESC,row_no"
        else:
            columns = "sku_id,sku_name" if group == "sku" else "source_store,venue,equipment_no,equipment_name"
            query = "SELECT " + columns + ",COUNT(*) AS records,SUM(stock_count) AS stock_count,SUM(cost_cents)/100.0 AS cost,"
            query += "SUM(review) AS review_count FROM outbound_stock_records WHERE " + clause + " GROUP BY " + columns
            order = " ORDER BY ABS(SUM(cost_cents)) DESC," + columns
        total = conn.execute("SELECT COUNT(*) FROM (" + query + ")", params).fetchone()[0]
        rows = [dict(row) for row in conn.execute(query + order + " LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size])]
    for row in rows:
        if "issues_json" in row:
            row["issues"] = json.loads(row.pop("issues_json"))
    return {"rows": rows, "total": total, "page": page, "page_size": page_size, "status": "complete"}
