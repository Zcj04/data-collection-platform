# -*- coding: utf-8 -*-
"""月度目标读取：目标表「最终核定目标」→ 门店目标；区域归属 → 区域目标

同时负责「每月目标」导入：预测分析页上传的 xlsx 统一保存为
data/targets/{YYYY-MM}_区域门店目标汇总表.xlsx，作为该月目标来源
（老板报表「每月目标」子表的数据源）。
"""

import io
import json
import os
import re
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGETS_DIR = os.path.join(_PROJECT_ROOT, "data", "targets")

# 文件名中的年月（2026-09 / 2026年9），用于推断上传目标所属月份
_MONTH_IN_NAME_RE = re.compile(r"(?<!\d)(\d{4})[-年](\d{1,2})(?!\d)")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _config_path(key: str, default: str) -> str:
    """读取 config.yaml 中的路径配置（相对项目根），无配置或异常时返回默认"""
    try:
        from core.config import get as config_get
        val = config_get(key, "") or ""
    except Exception:
        val = ""
    p = val if val else default
    if not os.path.isabs(p):
        p = os.path.join(_PROJECT_ROOT, p)
    return p


def find_target_file(month: str = None) -> str:
    """优先配置文件；指定月份时仅选择文件名中年月匹配的目标表。"""
    p = _config_path("targets.file", "")
    def matches(path):
        if month is None:
            return True
        match = re.search(r"(?<!\d)(\d{4})[-年](\d{1,2})(?!\d)", os.path.basename(path))
        return bool(match and f"{match[1]}-{int(match[2]):02d}" == month)

    if p and os.path.isfile(p) and matches(p):
        return p
    if os.path.isdir(_TARGETS_DIR):
        files = [
            os.path.join(_TARGETS_DIR, f)
            for f in os.listdir(_TARGETS_DIR)
            if f.lower().endswith((".xlsx", ".xls")) and not f.startswith("~$") and matches(f)
        ]
        if files:
            return max(files, key=os.path.getmtime)
    return ""


def _parse_target_workbook(source, aliases: dict = None) -> dict:
    """解析目标表内容（路径或字节流），返回 {场地: 最终核定目标(元)}。

    与 load_store_targets 共用同一套列识别逻辑：表头含「最终核定」的列
    为目标列，表头为「场地」的列为门店列；金额必须为正数，门店不可重复。
    aliases 在查重与返回前生效（只作用于同名目标文件）。
    """
    import openpyxl

    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    wb = openpyxl.load_workbook(source, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        return {}

    venue_col, target_col = 1, 12
    for i, h in enumerate(header):
        if h and "最终核定" in str(h):
            target_col = i
        if h and str(h).strip() == "场地":
            venue_col = i

    aliases = aliases or {}
    targets = {}
    for row in rows:
        if not row or venue_col >= len(row):
            continue
        venue = str(row[venue_col] or "").strip()
        if not venue or venue == "合计":
            continue
        if target_col >= len(row):
            continue
        val = row[target_col]
        if isinstance(val, (int, float)) and val > 0:
            venue = aliases.get(venue, venue)
            if venue in targets:
                raise ValueError(f"目标表存在重复门店：{venue}")
            targets[venue] = float(val)
    wb.close()
    return targets


def parse_target_workbook(content: bytes) -> dict:
    """解析上传的目标表字节流；无有效数据时抛 ValueError。"""
    targets = _parse_target_workbook(content)
    if not targets:
        raise ValueError(
            "目标表中没有有效的「最终核定目标」数据；"
            "请确认表头含「场地」与「最终核定目标」列，且金额为正数"
        )
    return targets


def infer_month_from_filename(filename: str) -> str:
    """从文件名推断 YYYY-MM；无法识别时返回空字符串。"""
    match = _MONTH_IN_NAME_RE.search(os.path.basename(filename or ""))
    if not match:
        return ""
    return f"{match[1]}-{int(match[2]):02d}"


def save_target_file(month: str, content: bytes) -> str:
    """将上传的目标表保存为该月标准文件名，返回保存路径。

    同月已有目标表时先改名备份为 .bak-<时间戳>（不带 .xlsx 后缀，
    不会被 find_target_file 选中），再写入新文件。
    """
    os.makedirs(_TARGETS_DIR, exist_ok=True)
    filename = f"{month}_区域门店目标汇总表.xlsx"
    path = os.path.join(_TARGETS_DIR, filename)
    if os.path.isfile(path):
        backup = path + ".bak-" + datetime.now().strftime("%Y%m%d%H%M%S")
        os.replace(path, backup)
    with open(path, "wb") as target:
        target.write(content)
    return path


def target_source_name(month: str) -> str:
    """该月当前生效的目标表文件名；未配置时返回空字符串。"""
    path = find_target_file(month)
    return os.path.basename(path) if path else ""


def system_venue_names() -> set:
    """系统中出现过的门店名（daily_summary 去重），用于导入时校验名称口径。"""
    from core.db import get_connection

    conn = get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT venue FROM daily_summary").fetchall()
    finally:
        conn.close()
    return {str(row["venue"]).strip() for row in rows if str(row["venue"] or "").strip()}


def load_store_targets(month: str = None) -> dict:
    """返回 {场地: 最终核定目标(元)}；指定月份无匹配表时返回空字典。"""
    path = find_target_file(month) if month is not None else find_target_file()
    if not path:
        return {}

    # 别名只作用于同名目标文件，保留用户原表及其他月份的名称口径。
    alias_path = os.path.splitext(path)[0] + ".aliases.json"
    aliases = {}
    if os.path.isfile(alias_path):
        with open(alias_path, encoding="utf-8") as source:
            aliases = json.load(source)

    targets = _parse_target_workbook(path, aliases)
    return targets


def load_store_regions() -> dict:
    """读取门店→区域映射（data/targets/regions.json），未配置时返回空 dict"""
    p = _config_path(
        "targets.store_regions_file",
        os.path.join("data", "targets", "regions.json"),
    )
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_daily_active_venues() -> set[str]:
    """读取每日经营的在营门店补充清单，不要求同时配置月度目标。"""
    p = _config_path(
        "targets.daily_active_venues_file",
        os.path.join("data", "targets", "daily_active_venues.json"),
    )
    if not os.path.isfile(p):
        return set()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return set()
    if isinstance(data, dict):
        values = data.keys()
    elif isinstance(data, (list, tuple, set)):
        values = data
    else:
        return set()
    return {str(venue).strip() for venue in values if str(venue).strip()}
