# -*- coding: utf-8 -*-
"""只读核对本地美团导出文件与 daily_summary 快照。

这份核对验证的是本地导出文件经过解析、门店映射后是否原样进入快照，
不能替代登录美团后台后的独立源头复核。脚本不会写入业务数据库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawlers.meituan_download import get_dict_list


METRICS = ("美团收款", "美团实收", "美团手续费")
PILOT_STORES = {
    "东莞A店": "Demo Finds娃娃市集(东莞莞A商圈万象汇店)",
    "深圳J店": "Demo Finds示例玩坊玩具工坊（深J区店）",
    "深圳C店": "Demo Finds娃娃市集(深C广场店)",
}
PERIODS = (
    ("2026-08", "2026-08-31"),
    ("2026-09", "2026-09-13"),
)
TOLERANCE = 0.01


def _number(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        number = float(value)
        return round(number, 2) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _source_exports(period: str, target_date: str) -> Dict[str, Any]:
    start = period.replace("-", "") + "01"
    end = target_date.replace("-", "")
    folder = ROOT / "data" / "downloads" / "meituan"
    files = sorted(folder.glob(f"*{start}~{end}_*.xlsx"))
    records: Dict[str, Dict[str, Any]] = {}
    duplicates: Dict[str, List[str]] = {}
    errors: List[Dict[str, str]] = []
    file_info = []
    for path in files:
        file_info.append({
            "path": _relative(path),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        })
        try:
            rows = get_dict_list(str(path))
        except Exception as error:  # pragma: no cover - depends on a damaged export file
            errors.append({"path": _relative(path), "error": str(error)})
            continue
        file_info[-1]["parsed_rows"] = len(rows)
        for row in rows:
            source_shop = str(row.get("美团店铺名") or "").strip()
            if not source_shop:
                continue
            if source_shop in records:
                duplicates.setdefault(source_shop, [records[source_shop]["file"]]).append(_relative(path))
                continue
            records[source_shop] = {
                "file": _relative(path),
                **{metric: _number(row.get(metric)) for metric in METRICS},
            }
    return {
        "files": file_info,
        "records": records,
        "duplicates": duplicates,
        "errors": errors,
    }


def _latest_snapshots(
    connection: sqlite3.Connection,
    period: str,
    target_date: str,
) -> Dict[str, Dict[str, Any]]:
    rows = connection.execute(
        "WITH latest AS ("
        "  SELECT venue, platform, MAX(date) AS date "
        "  FROM daily_summary WHERE platform='meituan' AND date>=? AND date<=? "
        "  GROUP BY venue, platform"
        ") "
        "SELECT d.venue, d.date, d.metrics_json, d.raw_file, d.period_start, d.source_task_id "
        "FROM daily_summary d INNER JOIN latest l "
        "ON l.venue=d.venue AND l.platform=d.platform AND l.date=d.date",
        (f"{period}-01", target_date),
    ).fetchall()
    result = {}
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metrics = {}
        if not isinstance(metrics, dict):
            metrics = {}
        result[str(row["venue"])] = {
            "date": str(row["date"]),
            "metrics": {metric: _number(metrics.get(metric)) for metric in METRICS},
            "raw_file": row["raw_file"],
            "period_start": row["period_start"],
            "source_task_id": row["source_task_id"],
        }
    return result


def _compare(
    source: Dict[str, Any],
    snapshot: Dict[str, Any],
    target_date: str,
    expected_period_start: str,
) -> Dict[str, Any]:
    source_values = {metric: _number(source.get(metric)) for metric in METRICS}
    snapshot_values = {metric: _number(snapshot["metrics"].get(metric)) for metric in METRICS}
    values_complete = all(
        source_values[metric] is not None and snapshot_values[metric] is not None
        for metric in METRICS
    )
    delta = {
        metric: (_number(snapshot_values[metric] - source_values[metric])
                 if snapshot_values[metric] is not None and source_values[metric] is not None
                 else None)
        for metric in METRICS
    }
    same_values = values_complete and all(
        abs(delta[metric] or 0) <= TOLERANCE for metric in METRICS
    )
    lineage_ok = (
        str(snapshot.get("period_start") or "") == expected_period_start
        and bool(str(snapshot.get("source_task_id") or "").strip())
    )
    status = "match" if snapshot["date"] == target_date and same_values and lineage_ok else "mismatch"
    if snapshot["date"] != target_date and same_values and lineage_ok:
        status = "stale_snapshot"
    elif snapshot["date"] == target_date and same_values and not lineage_ok:
        status = "missing_lineage"
    elif snapshot["date"] == target_date and not values_complete:
        status = "invalid_values"
    return {
        "status": status,
        "values_complete": values_complete,
        "source": source_values,
        "snapshot": snapshot,
        "delta_snapshot_minus_source": delta,
    }


def build_report() -> Dict[str, Any]:
    database = ROOT / "data" / "app.db"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        periods = []
        for period, target_date in PERIODS:
            exports = _source_exports(period, target_date)
            snapshots = _latest_snapshots(connection, period, target_date)
            comparisons = []
            for venue, source_shop in PILOT_STORES.items():
                source = exports["records"].get(source_shop)
                snapshot = snapshots.get(venue)
                if source_shop in exports["duplicates"]:
                    comparisons.append({
                        "venue": venue,
                        "source_shop": source_shop,
                        "status": "duplicate_source",
                        "source_files": exports["duplicates"][source_shop],
                    })
                    continue
                if source is None:
                    comparisons.append({
                        "venue": venue,
                        "source_shop": source_shop,
                        "status": "missing_source",
                    })
                    continue
                if snapshot is None:
                    comparisons.append({
                        "venue": venue,
                        "source_shop": source_shop,
                        "source_file": source["file"],
                        "status": "missing_snapshot",
                    })
                    continue
                comparisons.append({
                    "venue": venue,
                    "source_shop": source_shop,
                    "source_file": source["file"],
                    **_compare(source, snapshot, target_date, f"{period}-01"),
                })
            periods.append({
                "period": period,
                "target_date": target_date,
                "source_files": exports["files"],
                "source_file_errors": exports["errors"],
                "duplicate_source_shops": exports["duplicates"],
                "comparisons": comparisons,
                "summary": {
                    "pilot_count": len(comparisons),
                    "matches": sum(item["status"] == "match" for item in comparisons),
                    "issues": sum(item["status"] != "match" for item in comparisons) + len(exports["errors"]),
                },
            })
    finally:
        connection.close()

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "只读本地证据；不写入业务数据库；不替代上游后台独立复核",
        "platform": "meituan",
        "metrics": list(METRICS),
        "tolerance": TOLERANCE,
        "pilot_stores": [
            {"venue": venue, "source_shop": source_shop}
            for venue, source_shop in PILOT_STORES.items()
        ],
        "periods": periods,
    }


def _markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# 美团源头对账试点报告",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        "本报告只核对本地美团导出文件经过现有解析器后是否与 `daily_summary` 快照一致；"
        "导出文件与采集快照属于同一采集链路，不能替代登录上游后台后的独立金额复核。",
        "",
    ]
    for period in report["periods"]:
        lines.extend([
            "",
            "| 期间 | 门店 | 快照日 | 美团收款差额 | 美团实收差额 | 手续费差额 | 结果 |",
            "|---|---|---|---:|---:|---:|---|",
        ])
        for item in period["comparisons"]:
            delta = item.get("delta_snapshot_minus_source", {})
            amounts = {metric: "待核对" if delta.get(metric) is None else f"{delta[metric]:.2f}" for metric in METRICS}
            lines.append(
                "| {period} | {venue} | {date} | {income} | {settled} | {fee} | {status} |".format(
                    period=period["period"],
                    venue=item["venue"],
                    date=(item.get("snapshot") or {}).get("date", "—"),
                    income=amounts["美团收款"],
                    settled=amounts["美团实收"],
                    fee=amounts["美团手续费"],
                    status=item["status"],
                )
            )
        lines.append("")
        lines.append(
            f"{period['period']}：{period['summary']['matches']}/{period['summary']['pilot_count']} 家金额一致，"
            f"问题 {period['summary']['issues']} 项。"
        )
        for error in period.get("source_file_errors", []):
            lines.append(f"源文件解析失败：{error['path']}；请核查原文件。")
    lines.extend([
        "",
        "后续动作：金额一致的样本可作为解析与入库链路通过；仍需由负责人在上游后台抽查原始订单、退款/调整和门店映射，"
        "并把差异原因记录到核对台账。",
        "",
    ])
    return "\n".join(lines)


def main(output_dir: Optional[Path] = None) -> Dict[str, Any]:
    report = build_report()
    destination = output_dir or ROOT / "outputs" / "source-reconciliation-20260914"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (destination / "report.md").write_text(_markdown(report), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读核对本地美团导出与 daily_summary")
    parser.add_argument("--output-dir", type=Path, help="报告输出目录")
    args = parser.parse_args()
    result = main(args.output_dir)
    totals = [period["summary"] for period in result["periods"]]
    print(json.dumps({
        "output_dir": str(args.output_dir or ROOT / "outputs" / "source-reconciliation-20260914"),
        "matches": sum(item["matches"] for item in totals),
        "issues": sum(item["issues"] for item in totals),
    }, ensure_ascii=False))
