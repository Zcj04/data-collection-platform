# -*- coding: utf-8 -*-
"""多金宝总部设备库存流水，只读试采集，不写入货款账册。

运行：python -m crawlers.duojinbao_equipment_stock_crawler --date 2026-08-30
整月：python -m crawlers.duojinbao_equipment_stock_crawler --month 2026-07 --output-dir outputs/new-month-trial
账号、密码通过隐藏输入读取，不落盘。输出仅含按变更类型汇总的数据。
接口同时包含设备出礼、调拨和盘点，不能将全部 sumCost 当作出货货款。
"""

import argparse
import calendar
import getpass
import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from crawlers.duojinbao_store_value_crawler import create_session, login, request_json


RECORD_PATH = "/gw/venue/venue-report/api/v1/storage/record/equipment/page"
HEADQUARTERS_NAME = "示例品牌"
PAGE_SIZE = 200


def select_headquarters(session, name=HEADQUARTERS_NAME):
    """精确匹配账号可访问的 type=3 总部，不沿用普通门店 type=2 筛选。"""
    merchants = request_json(
        session, "GET", "/gw/venue/api/v1/merchant/store/staff/merchant",
    ).get("data") or []
    if isinstance(merchants, dict):
        merchants = [merchants]
    matches = {}
    for merchant in merchants:
        for org in merchant.get("tenantOrgList") or []:
            if org.get("name") == name and org.get("type") == 3:
                key = (merchant.get("merchantId"), org.get("id"))
                matches[key] = {
                    "name": name,
                    "storeId": org.get("id"),
                    "merchantId": merchant.get("merchantId"),
                    "adOrganizationId": org.get("adOrganizationId"),
                }
    if len(matches) != 1:
        raise ValueError("总部名称必须唯一匹配，实际匹配 %s 个" % len(matches))
    headquarters = next(iter(matches.values()))
    params = {key: headquarters[key] for key in
              ("storeId", "merchantId", "adOrganizationId")}
    if not all(params.values()):
        raise ValueError("总部缺少切换所需标识")
    request_json(
        session, "GET", "/gw/venue/api/v1/merchant/store/staff/resources",
        params=params,
    )
    return headquarters


def fetch_records(session, report_date, progress_callback=None, metadata=None):
    """读取已选总部一天的全部流水，保留平台金额符号，不静默截断或去重。"""
    day = datetime.strptime(report_date, "%Y-%m-%d").date()
    rows = []
    signatures = set()
    expected_meta = None
    for page_index in range(1, 1001):
        if progress_callback:
            progress_callback(page_index, expected_meta[1] if expected_meta else 0)
        payload = request_json(
            session, "POST", RECORD_PATH,
            json_data={
                "startTime": "%s 00:00:00" % day,
                "endTime": "%s 23:59:59" % day,
                "pageIndex": page_index,
                "pageSize": PAGE_SIZE,
            },
            read_retry_count=2,
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("设备库存流水缺少 data.items")
        total, pages = data.get("total"), data.get("pages")
        if (type(total) is not int or type(pages) is not int or total < 0
                or pages < 0 or data.get("pageIndex") != page_index
                or data.get("pageSize") != PAGE_SIZE
                or pages != (total + PAGE_SIZE - 1) // PAGE_SIZE):
            raise ValueError("设备库存流水分页信息不符合约定")
        if expected_meta is not None and expected_meta != (total, pages):
            raise ValueError("采集期间记录总数发生变化，请重新采集")
        expected_meta = (total, pages)
        if metadata is not None:
            metadata.update(total=total, pages=pages, page_size=PAGE_SIZE)
        items = data["items"]
        if len(items) != min(PAGE_SIZE, max(0, total - len(rows))):
            raise ValueError("分页记录不完整，已停止")
        if items:
            signature = hashlib.sha256(
                json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if signature in signatures:
                raise ValueError("接口返回重复页面，已停止")
            signatures.add(signature)
        for row in items:
            if not isinstance(row, dict):
                raise ValueError("库存流水不是对象")
            occurred = datetime.strptime(str(row.get("operationTime")), "%Y-%m-%d %H:%M:%S")
            if occurred.date() != day:
                raise ValueError("接口返回请求日期之外的流水")
            # 操作人和自由文本备注与本次成本验证无关，不对外返回。
            rows.append({key: value for key, value in row.items()
                         if key not in ("operationPerson", "description")})
        if page_index >= pages:
            if len(rows) != total:
                raise ValueError("已采集条数与平台总条数不一致")
            return rows
    raise ValueError("分页超过安全上限，未返回不完整数据")


def summarize_records(rows):
    """按变更类型汇总；缺少数量/金额时报错，不将未知值视为零。"""
    groups = {}
    for row in rows:
        kind = row.get("businessTypeDesc")
        if not kind:
            raise ValueError("库存流水缺少变更类型")
        quantity = row.get("stockCount")
        if type(quantity) is not int:
            raise ValueError("库存流水缺少有效的变更数量")
        try:
            cost = Decimal(str(row.get("sumCost")))
        except InvalidOperation as exc:
            raise ValueError("库存流水缺少有效的变更成本金额") from exc
        if not cost.is_finite():
            raise ValueError("变更成本金额不是有限数字")
        group = groups.setdefault(kind, {"records": 0, "stockCount": 0, "sumCost": Decimal("0")})
        group["records"] += 1
        group["stockCount"] += quantity
        group["sumCost"] += cost
    return {
        "records": len(rows),
        "stores_with_records": len({row["storeName"] for row in rows}),
        "by_business_type": {
            kind: dict(group, sumCost=str(group["sumCost"]))
            for kind, group in sorted(groups.items())
        },
    }


def month_dates(month, today=None):
    """试采集仅接受已结束自然月，不能把当月部分日期标为整月。"""
    start = datetime.strptime(month, "%Y-%m").date()
    if start.strftime("%Y-%m") != month:
        raise ValueError("月份格式必须为 YYYY-MM")
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    if end >= (today or date.today()):
        raise ValueError("月度试采集仅支持已结束的自然月")
    return [(start + timedelta(days=index)).isoformat() for index in range(end.day)]


def fetch_month(session, month, output_dir):
    """按天完整分页保存脱敏流水；只有所有日期成功才生成月度汇总。"""
    days = month_dates(month)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=False)
    all_rows, daily = [], []
    for day in days:
        records = fetch_records(session, day)
        summary = summarize_records(records)
        (destination / (day + ".json")).write_text(
            json.dumps({"date": day, "summary": summary, "records": records},
                       ensure_ascii=False), encoding="utf-8",
        )
        all_rows.extend(records)
        daily.append({"date": day, **summary})
        print("%s 完成 %s/%s 天，%s 条" %
              (day, len(daily), len(days), len(records)), flush=True)
    store_rows = {}
    for row in all_rows:
        store_rows.setdefault(row["storeName"], []).append(row)
    result = {
        "month": month, "headquarters": HEADQUARTERS_NAME,
        "start_date": days[0], "end_date": days[-1],
        "days_expected": len(days), "days_succeeded": len(daily),
        "status": "complete", "collected_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize_records(all_rows), "daily": daily,
        "stores": [{"storeName": name, **summarize_records(records),
                    "days_with_records": len({r["operationTime"][:10] for r in records})}
                   for name, records in sorted(store_rows.items())],
    }
    (destination / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    period = parser.add_mutually_exclusive_group(required=True)
    period.add_argument("--date", help="报表日期 YYYY-MM-DD")
    period.add_argument("--month", help="已结束的自然月 YYYY-MM")
    parser.add_argument("--output-dir", help="月度试采集输出目录（必须是新目录）")
    args = parser.parse_args()
    if args.month:
        month_dates(args.month)
        if not args.output_dir:
            parser.error("--month 必须同时提供 --output-dir")
        if Path(args.output_dir).exists():
            parser.error("输出目录已存在，请使用新目录保留历史试采集")
    else:
        datetime.strptime(args.date, "%Y-%m-%d")
    with create_session() as session:
        username = getpass.getpass("多金宝账号（隐藏输入）: ")
        password = getpass.getpass("多金宝密码（隐藏输入）: ")
        try:
            login(session, username, password)
        except Exception:
            raise RuntimeError("多金宝登录失败，请检查网络及凭证") from None
        finally:
            del username, password
        headquarters = select_headquarters(session)
        if args.month:
            result = fetch_month(session, args.month, args.output_dir)
            print(json.dumps({"month": args.month, "status": result["status"],
                              **result["summary"]}, ensure_ascii=False, indent=2))
            return
        records = fetch_records(session, args.date)
        print(json.dumps({"headquarters": headquarters["name"], "date": args.date,
                          **summarize_records(records)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
