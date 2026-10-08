# -*- coding: utf-8 -*-
"""
兑币机数据统计（自动读取金山文档在线表格）

主入口：main(date_str, data=None) -> List[Dict]
    输入：日期 YYYY-MM-DD
    输出：[{"场地": venue, "兑币机收款": total}]（累计口径：截至当日金额合计）

数据来源（config.yaml platforms.coin_exchange）：
  - source_url：金山文档分享链接
  - cell_range：要读取的单元格范围（默认 B40:B70，每天一行）
  - sheet：工作表名（留空用默认表；如 "8月A店现金表"）
  - venue：场地名
  - fetch_mode：online=只在线读取；file=只读八爪鱼导出文件；auto=先在线，失败回落文件
  - data_file：八爪鱼导出的 JSON 文件（fetch_mode=file 或 auto 兜底时使用）

在线读取实现：无头浏览器打开分享页 -> 名称框跳转到起始单元格 ->
Shift+方向键扩展选区 -> Ctrl+C -> 粘贴到隐藏文本框读出 TSV 文本。
非数字单元格（如 "/" 分隔行）按 0 处理。
"""

import json
import os
import re
from datetime import datetime

from core.config import get as config_get


_PLATFORM_CFG = config_get("platforms.coin_exchange") or {}
DEFAULT_VENUE = _PLATFORM_CFG.get("venue") or "香港A店"
SOURCE_URL = _PLATFORM_CFG.get("source_url") or "https://www.kdocs.cn/l/cl3QDSIivSfi"
CELL_RANGE = _PLATFORM_CFG.get("cell_range") or "B40:B70"
SHEET_NAME = str(_PLATFORM_CFG.get("sheet") or "").strip()
FETCH_MODE = str(_PLATFORM_CFG.get("fetch_mode") or "auto").strip().lower()
DEFAULT_DATA_FILE = _PLATFORM_CFG.get("data_file") or "data/coin_exchange.json"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_path(path):
    """相对路径基于项目根目录解析，避免依赖启动目录"""
    if os.path.isabs(path):
        return path
    return os.path.join(_PROJECT_ROOT, path)


def _parse_amount(value):
    """解析单元格金额；非数字返回 None（用于识别表头/分隔行）"""
    if value is None:
        return 0
    text = str(value).strip()
    if text in ("", "-", "--", "/", "\\"):
        return 0
    text = text.replace(",", "").replace("¥", "").replace("$", "")
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_rows(data):
    """把传入 JSON 统一为行值列表（每行取第 1 列）"""
    if isinstance(data, dict):
        values = (
            data.get("Values")
            or data.get("values")
            or data.get("data")
            or []
        )
    elif isinstance(data, list):
        values = data
    else:
        raise ValueError(
            "兑币机数据格式不正确：应为 JSON 对象 "
            '(格式如 {{"Values": [...]}}) 或数组，当前为 {}'.format(
                type(data).__name__
            )
        )

    rows = []
    for item in values:
        if isinstance(item, (list, tuple)):
            rows.append(item[0] if len(item) > 0 else None)
        else:
            rows.append(item)
    return rows


def _amounts_from_rows(rows):
    """把行值转为金额列表：跳过开头非数字表头，中间非数字按 0"""
    amounts = [_parse_amount(value) for value in rows]
    start = 0
    while start < len(amounts) and amounts[start] is None:
        start += 1
    amounts = amounts[start:]
    return [0 if value is None else value for value in amounts]


def _parse_cell_range(cell_range):
    """解析 "B40:B70" -> (col, start_row, end_row)"""
    match = re.match(
        r"^([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)$",
        str(cell_range or "").strip(),
    )
    if not match:
        raise ValueError(
            "cell_range 格式不正确（应为 B40:B70）：{}".format(cell_range)
        )
    col = match.group(1).upper()
    start = int(match.group(2))
    end = int(match.group(4))
    if end < start:
        raise ValueError("cell_range 结束行不能小于起始行")
    return col, start, end


def _load_data_file():
    """从配置的 data_file 读取八爪鱼导出的 JSON"""
    path = _resolve_path(DEFAULT_DATA_FILE)
    if not os.path.exists(path):
        raise FileNotFoundError(
            "未找到兑币机数据文件：{}（可设置 config.yaml "
            "platforms.coin_exchange.data_file）".format(path)
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fetch_kdocs_amounts():
    """无头浏览器在线读取金山文档 cell_range 单元格（自动方案）"""
    from playwright.sync_api import sync_playwright

    col, start, end = _parse_cell_range(CELL_RANGE)
    count = end - start + 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("input.edit-box", timeout=60000)
            page.wait_for_timeout(15000)

            if SHEET_NAME:
                page.locator(
                    ".et-status-sheet-item",
                    has_text=SHEET_NAME,
                ).first.click(timeout=8000, force=True)
                page.wait_for_timeout(3000)

            # 名称框跳转到起始单元格
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            box = page.locator("input.edit-box")
            b = box.bounding_box()
            if not b:
                raise RuntimeError("未找到金山文档名称框（页面可能未加载完成）")
            page.mouse.click(
                b["x"] + b["width"] / 2,
                b["y"] + b["height"] / 2,
            )
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.type("{}{}".format(col, start), delay=15)
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)

            # Shift+方向键扩展选区
            page.keyboard.down("Shift")
            for _ in range(count - 1):
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(40)
            page.keyboard.up("Shift")
            page.wait_for_timeout(300)

            page.keyboard.press("Control+c")
            page.wait_for_timeout(1000)

            # 粘贴到隐藏文本框读取 TSV 文本
            text = page.evaluate(
                """() => {
                let ta = document.getElementById('clipdump');
                if (!ta) {
                    ta = document.createElement('textarea');
                    ta.id = 'clipdump';
                    ta.style.cssText = 'position:fixed;left:0;top:0;opacity:0.01;width:1px;height:1px;';
                    document.body.appendChild(ta);
                }
                ta.focus();
            }"""
            )
            page.wait_for_timeout(300)
            page.keyboard.press("Control+v")
            page.wait_for_timeout(500)
            text = page.evaluate("document.getElementById('clipdump').value")
        finally:
            browser.close()

    if not text:
        raise RuntimeError("金山文档复制结果为空，请检查 cell_range 与表格内容")
    lines = [line.strip() for line in text.splitlines()]
    rows = [line.split("\t")[0] if line else "" for line in lines]
    return _amounts_from_rows(rows)


def main(date_str, data=None):
    """
    Args:
        date_str: "yyyy-mm-dd"，返回截至该日（含）的累计金额
        data: 可选，外部传入的 JSON（兼容八爪鱼导出格式）；
              缺省时按 fetch_mode 在线读取或读取 data_file
    Returns:
        [{"场地": venue, "兑币机收款": total}]
    """
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    # 当前兑币机表格仅包含 2026 年 8 月数据，其他月份不读取。
    if (date_obj.year, date_obj.month) != (2026, 8):
        return []
    day = date_obj.day

    if data is not None:
        amounts = _amounts_from_rows(_normalize_rows(data))
    elif FETCH_MODE in ("online", "auto"):
        try:
            amounts = _fetch_kdocs_amounts()
        except Exception as online_error:
            if FETCH_MODE == "online":
                raise RuntimeError("在线读取兑币机数据失败：{}".format(online_error))
            # auto 模式回落本地文件
            try:
                amounts = _amounts_from_rows(
                    _normalize_rows(_load_data_file())
                )
            except Exception as file_error:
                raise RuntimeError(
                    "在线读取失败（{}），且本地文件也读取失败（{}）".format(
                        online_error,
                        file_error,
                    )
                )
    else:
        amounts = _amounts_from_rows(_normalize_rows(_load_data_file()))

    total = sum(amounts[:day])
    return [{"场地": DEFAULT_VENUE, "兑币机收款": total}]


if __name__ == "__main__":
    # 测试：兼容带表头与不带表头两种布局
    test_data = {
        "ColumnNames": ["B"],
        "Values": [["header"], ["1400"], ["990"], [""], ["2580"]],
    }
    print("带表头 day=4 ->", main("2026-08-04", test_data))
    print("不带表头 day=1 ->", main("2026-08-01", ["1400", "990", "", "2580"]))
