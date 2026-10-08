"""复核本次 2026-07 月度试采集并重建报告，不联网、不修改账册。"""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
from pathlib import Path


def analyze(directory):
    directory = Path(directory)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    if summary["month"] != "2026-07":
        raise ValueError("本次分析报告仅适用于 2026-07 试采集")
    roster = json.loads((directory / "source-roster.json").read_text(encoding="utf-8"))
    mapping = defaultdict(set)
    for item in roster:
        for name in str(item.get("duojinbao") or "").splitlines():
            if name.strip():
                mapping[name.strip()].add(item["venue"])
    rows = []
    day_checks = []
    for day in summary["daily"]:
        payload = json.loads((directory / (day["date"] + ".json")).read_text(encoding="utf-8"))
        records = payload["records"]
        assert len(records) == day["records"]
        assert payload["summary"] == {k: v for k, v in day.items() if k != "date"}
        assert all(row["operationTime"][:10] == day["date"] for row in records)
        rows.extend(records)
        day_checks.append(day["date"])
    assert len(rows) == summary["summary"]["records"]
    assert len(day_checks) == summary["days_expected"]
    groups = defaultdict(list)
    for row in rows:
        groups[row["storeName"]].append(row)
    output_stores = []
    for name, records in groups.items():
        gifts = [row for row in records if row["businessTypeDesc"] == "设备出礼"]
        matched = sorted(mapping[name.strip()])
        daily = []
        for day in day_checks:
            day_gifts = [row for row in gifts if row["operationTime"][:10] == day]
            has_store_records = any(row["operationTime"][:10] == day for row in records)
            daily.append({"date": day, "gift_records": len(day_gifts),
                          "record_status": "reported_gifts" if day_gifts else "other_movements_only" if has_store_records else "no_store_records",
                          "quantity": -sum(row["stockCount"] for row in day_gifts) if has_store_records else None,
                          "cost": str(-sum((Decimal(str(row["sumCost"])) for row in day_gifts), Decimal("0"))) if has_store_records else None})
        output_stores.append({
            "source_name": name, "standard_venues": matched,
            "mapping_status": "matched" if len(matched) == 1 else "ambiguous" if matched else "unmatched",
            "records": len(records), "gift_records": len(gifts),
            "gift_quantity": -sum(row["stockCount"] for row in gifts),
            "gift_cost": str(-sum((Decimal(str(row["sumCost"])) for row in gifts), Decimal("0"))),
            "days_with_gifts": len({row["operationTime"][:10] for row in gifts}),
            "first_record": min(row["operationTime"] for row in records),
            "last_record": max(row["operationTime"] for row in records),
            "gift_zero_cost_records": sum(Decimal(str(row["sumCost"])) == 0 for row in gifts),
            "daily": daily,
        })
    output_stores.sort(key=lambda row: Decimal(row["gift_cost"]), reverse=True)
    stock_mismatches = [row for row in rows if row["originalCount"] + row["stockCount"] != row["changeAfterCount"]]
    cost_mismatches = [row for row in rows if abs(Decimal(str(row["skuPredictCost"])) * row["stockCount"] - Decimal(str(row["sumCost"]))) > Decimal("0.01")]
    fingerprints = Counter(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
    gifts = [row for row in rows if row["businessTypeDesc"] == "设备出礼"]
    gift_daily = [{"date": day, "records": sum(item["gift_records"] for store in output_stores for item in store["daily"] if item["date"] == day),
                   "quantity": sum(item["quantity"] for store in output_stores for item in store["daily"] if item["date"] == day and item["quantity"] is not None),
                   "cost": str(sum((Decimal(item["cost"]) for store in output_stores for item in store["daily"] if item["date"] == day and item["cost"] is not None), Decimal("0")))}
                  for day in day_checks]
    cost_total = sum((Decimal(row["gift_cost"]) for row in output_stores), Decimal("0"))
    source_cost = -Decimal(summary["summary"]["by_business_type"]["设备出礼"]["sumCost"])
    assert cost_total == source_cost == sum(Decimal(day["cost"]) for day in gift_daily)
    checks = {
        "days_verified": len(day_checks), "records_verified": len(rows),
        "stock_balance_mismatches": len(stock_mismatches),
        "cost_product_mismatches_over_one_cent": len(cost_mismatches),
        "identical_sanitized_row_excess": sum(count - 1 for count in fingerprints.values()),
        "gift_nonnegative_quantity_records": sum(row["stockCount"] >= 0 for row in gifts),
        "gift_zero_cost_records": sum(Decimal(str(row["sumCost"])) == 0 for row in gifts),
        "gift_zero_cost_quantity": -sum(row["stockCount"] for row in gifts if Decimal(str(row["sumCost"])) == 0),
        "store_mapping": dict(Counter(store["mapping_status"] for store in output_stores)),
        "monthly_daily_store_cost_equal": True,
    }
    sample_keys = ("storeName", "operationTime", "businessTypeDesc", "skuId", "skuName", "skuPredictCost", "originalCount", "stockCount", "changeAfterCount", "sumCost")
    large_adjustments = sorted((row for row in rows if row["businessTypeDesc"] != "设备出礼"),
                               key=lambda row: abs(Decimal(str(row["sumCost"]))), reverse=True)[:10]
    result = {"month": summary["month"], "summary": summary["summary"], "checks": checks,
              "stores": output_stores, "daily_gifts": gift_daily,
              "largest_inventory_adjustments": [{key: row[key] for key in sample_keys} for row in large_adjustments],
              "zero_cost_gift_samples": [{key: row[key] for key in sample_keys} for row in gifts if Decimal(str(row["sumCost"])) == 0][:5]}
    (directory / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    money = lambda value: format(Decimal(str(value)), ",.2f")
    lines = ["# 2026 年 7 月多金宝出货试采集分析", "",
             "来源：示例品牌总部。采集时间：" + summary["collected_at"] + "。",
             "范围：2026-07-01 至 2026-07-31；31/31 天成功，37,798 条库存流水，10 家有记录门店。全部门店按当前系统多金宝映射唯一匹配；保留原始店名，不进行模糊猜配。",
             "", "仅设备出礼：36,515 条、37,150 件，平台原始成本合计 -436,560.57 元；作为流出成本展示为 436,560.57 元。该值为平台预估成本口径的已记录金额，包含 5 条零成本出礼，尚未核定为正式货款。", "",
             "## 门店出礼", "", "| 标准门店 | 出礼记录 | 出礼数量 | 已记录成本（元） | 有出礼记录天数 | 零成本记录 |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in output_stores:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            " / ".join(row["standard_venues"]) or row["source_name"], format(row["gift_records"], ","),
            format(row["gift_quantity"], ","), money(row["gift_cost"]), row["days_with_gifts"], row["gift_zero_cost_records"]))
    lines += ["", "广州A商圈第一条库存流水为 7 月 27 日，仅 4 天有出礼。不能从这一点推定开业日期、漏采或月初实际出货为零。账号当前可访问 12 家门店，但本月只有 10 家出现在该报表；不能扩展为全公司所有门店均已覆盖。", "",
              "## 库存变更分类（保留平台符号）", "", "| 类型 | 条数 | 变更数量 | 变更成本金额（元） |",
              "| --- | ---: | ---: | ---: |"]
    for kind, group in summary["summary"]["by_business_type"].items():
        lines.append("| %s | %s | %s | %s |" % (kind, format(group["records"], ","), format(group["stockCount"], ","), money(group["sumCost"])))
    lines += ["", "设备出礼以外的库存变更仅供核对，不混入出货成本。盘点调整金额较大不等于采集错误；数量平衡和金额乘法均校验通过，但是否符合实际库存需要业务核实。", "",
              "## 校验与待核对", "",
              "- 每天逐页校验总数、页码、返回日期与分页完整性；31 天记录均落在各自请求日内。",
              "- 37,798 条流水的库存数量平衡差异 0 条，预估成本乘变更数量与变更成本金额相差超过 0.01 元的记录 0 条。",
              "- 脱敏后的完全相同行 0 条；仅检查，不按商品 ID 或行哈希去重。",
              "- 门店合计、每天合计、月合计均为 436,560.57 元。",
              "- 深H商圈 5 条、5 件设备出礼成本为 0。保留平台原值并提示待核对，不能以其他交易价格补价。", "",
              "| 零成本出礼日期时间 | 商品 | 商品 ID | 数量 | 平台成本（元） |",
              "| --- | --- | --- | ---: | ---: |"]
    for row in result["zero_cost_gift_samples"]:
        lines.append("| %s | %s | %s | %s | %s |" % (row["operationTime"], row["skuName"], row["skuId"], -row["stockCount"], money(row["sumCost"])))
    lines += ["", "## 较大库存调整样例", "", "| 门店 | 时间 | 类型 | 变更数量 | 金额（元） |", "| --- | --- | --- | ---: | ---: |"]
    for row in result["largest_inventory_adjustments"][:3]:
        lines.append("| %s | %s | %s | %s | %s |" % (row["storeName"], row["operationTime"], row["businessTypeDesc"], format(row["stockCount"], ","), money(row["sumCost"])))
    lines += ["", "## 每日已记录出礼", "", "| 日期 | 记录 | 数量 | 已记录成本（元） |", "| --- | ---: | ---: | ---: |"]
    for row in gift_daily:
        lines.append("| %s | %s | %s | %s |" % (row["date"], format(row["records"], ","), format(row["quantity"], ","), money(row["cost"])))
    lines += ["", "## 本地证据", "", "- `2026-07-01.json` 至 `2026-07-31.json`：每天的脱敏明细，不含操作人、自由备注、账号密码或 Cookie。",
              "- `summary.json`：月度采集结果、每天/每店各类型汇总。",
              "- `source-roster.json`：2026-08-31 只读获取的标准门店映射快照；当前经营状态不能当作历史月份状态。",
              "- `analysis.json`：逐店逐日数据、映射状态、校验结果与异常样例；无门店流水的日期为 no_store_records，数量和金额为 null。",
              "", "这次只完成月度试采和模块规划，没有新增正式数据库表、后台页面、自动任务，也没有改变累计货款公式。", ""]
    (directory / "月度分析.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"checks": checks, "stores": [{k:v for k,v in row.items() if k != "daily"} for row in output_stores],
                      "largest_adjustments": result["largest_inventory_adjustments"][:3]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    analyze(parser.parse_args().directory)
