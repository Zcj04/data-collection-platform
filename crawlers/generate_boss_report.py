# -*- coding: utf-8 -*-
"""
老板报表一键生成

从 daily_summary 读取采集数据，使用 report_summary.main() 计算汇总指标，
以模板 xlsx 为底本（保留格式），填入计算值后输出。

用法：
    python -m crawlers.generate_boss_report <YYYY-MM-DD>
"""

import os
import shutil
import sys
import re
import zipfile
from copy import copy, deepcopy
from datetime import datetime, timedelta

import openpyxl
from openpyxl.workbook.properties import CalcProperties
from openpyxl.workbook.views import BookView
from openpyxl.formula.translate import Translator
from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import column_index_from_string, get_column_letter

# 允许直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.accounting_summary import accounting_venue_scope, load_period_summary_data
from core.targets import load_store_regions, load_store_targets
from core.venue_lifecycle import operating_venues_on
from crawlers import report_summary

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE_PATH = os.path.join(_PROJECT_ROOT, "data", "每月货款比 08-14.xlsx")
_OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "data", "reports")

# ============ 模板列映射 ============
# 门店汇总/内地门店/香港门店 column_letter -> report_summary 列名
# 大部分直接同名，少数需要计算或映射
_SUMMARY_COL_MAP = {
    "D": "乐摇摇非现金",
    "E": "多金宝非现金",
    "F": "汇联非现金",
    "G": "芸苔非团购",
    "H": "抖音收款",
    "I": "美团收款",
    "J": "鲸舰非现金",
    "K": "StarThing非现金",
    "L": "八达通收款",
    "M": None,  # 其余业绩 = 兑币机收款 + 其他收入
    "N": "芸苔远程取币",
    "O": "Kpay收款",
    "P": "收入汇总",
    "Q": None,  # 积分货款 = sum of 积分货款
    "R": "基础货款",
    "S": "总货款",
    "T": "货款比%",
    "U": "__fixed__",  # 货款比指标 = 0.45
    # V 列(预计收入/目标)保留模板已有值，不覆盖
    "W": "预收入完成率",
    "AA": "乐摇摇现金",
    "AB": "多金宝现金",
    "AC": "汇联现金",
    "AD": "芸苔现金",
    "AE": "鲸舰现金",
    "AF": "StarThing现金",
    "AG": "现金",
    "AH": "乐摇摇投币",
    "AI": "多金宝投币",
    "AJ": "芸苔投币",
    "AK": "StarThing投币",
    "AL": "鲸舰投币",
    "AM": "投币合计",
    "AN": "乐摇摇出币",
    "AO": "多金宝出币",
    "AP": "芸苔出币",
    "AQ": "StarThing出币",
    "AR": "鲸舰出币",
    "AS": "出币合计",
    "AT": "乐摇摇出货",
    "AU": "多金宝出货",
    "AV": "芸苔出货",
    "AW": "StarThing出货",
    "AX": "鲸舰出货",
    "AY": "出货合计",
    "AZ": None,  # 出货积分数 - 保留模板已有值
    "BA": "存货合计",  # 积分增加
    "BB": None,  # 积分减少 = sum of 积分减少
    "BC": "存货比率（存货与出货比）",
    "BD": "__fixed__",  # 存货率指标 = 0.45
    "BE": "币值（收入与投币比）",
    "BF": "出货率（投币与出货比）",
    "BI": "存货比率（存货与出货比）",
}

# 源数据 列映射 (report_summary 列名 -> 源数据 template column letter)
# 2026-09-25 起油菜花四列位于 D..G（与芸苔同为前置平台区块），原列整体右移 4。
_SOURCE_COL_MAP = {
    "油菜花现金": "D",
    "油菜花微信": "E",
    "油菜花支付宝": "F",
    "油菜花盈客宝": "G",
    "芸苔微信": "H",
    "芸苔支付宝": "I",
    "芸苔手续费": "J",
    "芸苔非团购": "K",
    "抖音收款": "L",
    "抖音手续费": "M",
    "美团收款": "N",
    "美团手续费": "O",
    "乐摇摇非现金": "P",
    "乐摇摇手续费": "Q",
    "多金宝非现金": "R",
    "多金宝手续费": "S",
    "汇联非现金": "T",
    "汇联手续费": "U",
    "StarThing非现金": "V",
    "鲸舰非现金": "W",
    "Kpay收款": "X",
    "Kpay手续费": "Y",
    "八达通收款": "Z",
    "八达通手续费": "AA",
    "芸苔远程取币": "AB",
    "其他收入": "AC",
    "收入汇总": "AD",
    "扣除手续费后": "AE",
    "基础货款": "AF",
    "StarThing积分货款": "AG",
    "鲸舰积分货款": "AH",
    "新系统积分货款": "AI",
    "芸苔积分货款": "AJ",
    "总货款": "AK",
    "货款比%": "AL",
    "预计收入(万元)": "AM",
    "预收入完成率": "AN",
    "乐摇摇现金": "AO",
    "兑币机收款": "AP",
    "鲸舰现金": "AQ",
    "StarThing现金": "AR",
    "汇联现金": "AS",
    "多金宝现金": "AT",
    "芸苔现金": "AU",
    "现金": "AV",
    "芸苔投币": "AW",
    "多金宝投币": "AX",
    "乐摇摇投币": "AY",
    "StarThing投币": "AZ",
    "鲸舰投币": "BA",
    "投币合计": "BB",
    "芸苔出币": "BC",
    "StarThing出币": "BD",
    "鲸舰出币": "BE",
    "多金宝出币": "BF",
    "出币合计": "BG",
    "芸苔出货": "BH",
    "多金宝出货": "BI",
    "乐摇摇出货": "BJ",
    "StarThing出货": "BK",
    "鲸舰出货": "BL",
    "出货合计": "BM",
    "鲸舰积分增加": "BN",
    "鲸舰积分减少": "BO",
    "新系统积分增加": "BP",
    "新系统积分减少": "BQ",
    "StarThing积分增加": "BR",
    "StarThing积分减少": "BS",
    "芸苔积分增加": "BT",
    "芸苔积分减少": "BU",
    "存货合计": "BV",
    "存货比率（存货与出货比）": "BW",
    "币值（收入与投币比）": "BX",
    "出货率（投币与出货比）": "BY",
    "StarThing手续费": "CA",
}

# 门店汇总/内地门店/香港门店 模板 Q..T 列：油菜花现金/微信/支付宝/盈客宝
# 明细（INDEX/MATCH 引用源数据 D..G）。收入汇总公式会把这四列并入，
# 与 report_summary 的收入口径（INCOME_COLUMNS 含油菜花）保持一致。
# 注意：此处记录的是模板中的列号（17..20），重写收入汇总公式时
# 由 _summary_column_after_fees 换算为费用列插入后的最终列号（28..31）。
_YOUCAIHUA_SUMMARY_COLUMNS = (17, 18, 19, 20)

# 门店汇总原收入列 -> 手续费字段；鲸舰尚无独立手续费来源。
_SUMMARY_FEE_COLUMNS = {
    4: "乐摇摇手续费", 5: "多金宝手续费", 6: "汇联手续费",
    7: "芸苔手续费", 8: "抖音手续费", 9: "美团手续费",
    10: "鲸舰手续费", 11: "StarThing手续费", 12: "八达通手续费",
    15: "Kpay手续费",
}


def _summary_column_after_fees(column: int) -> int:
    # 原 P 列后另加实际收入，后续货款/经营指标再向右移动一列。
    return column + sum(source < column for source in _SUMMARY_FEE_COLUMNS) + (column > 16)


def _remap_summary_range(reference: str) -> str:
    """仅平移列坐标，保留绝对引用和行号。"""
    def replace(match):
        column = column_index_from_string(match.group(2))
        return match.group(1) + get_column_letter(_summary_column_after_fees(column))
    return re.sub(r"(\$?)([A-Z]{1,3})(?=\$?\d|:|$)", replace, reference)


def _limit_data_bar_ranges(ranges: str, data_end: int) -> str:
    """将数据条条件格式限制在实际门店数据行，不覆盖空白行。"""
    limited = []
    for reference in ranges.split():
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", reference)
        if match and int(match.group(2)) == 2:
            reference = (
                f"{match.group(1)}2:{match.group(3)}{data_end}"
            )
        limited.append(reference)
    return " ".join(limited)


def _remap_summary_formula(formula: str, sheet_name: str) -> str:
    """插入列后同步本页及跨页引用；不改源数据列和字符串常量。"""
    tokens = Tokenizer(formula)
    for token in tokens.items:
        if token.type != "OPERAND" or token.subtype != "RANGE":
            continue
        qualifier, separator, reference = token.value.rpartition("!")
        referenced_sheet = qualifier.strip("'") if separator else sheet_name
        if referenced_sheet == "门店汇总":
            token.value = (qualifier + separator) + _remap_summary_range(reference)
    return tokens.render()


def add_summary_fee_layout(wb, total_row: int, store_count: int, source_total_row: int):
    """在门店汇总收入后插入费用列，并保留其余报表的公式依赖。"""
    ws = wb["门店汇总"]
    # openpyxl.insert_cols 不自动调整公式、合并区域或列宽，分别保存后重映射。
    merges = [str(merged) for merged in ws.merged_cells.ranges]
    for merged in merges:
        ws.unmerge_cells(merged)
    dimensions = {}
    for dimension in ws.column_dimensions.values():
        for column in range(dimension.min, dimension.max + 1):
            dimensions[column] = copy(dimension)
    conditional_formats = [
        (str(cf.sqref), deepcopy(ws.conditional_formatting[cf]))
        for cf in ws.conditional_formatting
    ]
    ws.insert_cols(17)  # 原 P（收入汇总）右侧：实际收入。
    for column in sorted(_SUMMARY_FEE_COLUMNS, reverse=True):
        ws.insert_cols(column + 1)
    ws.column_dimensions.clear()
    for column, dimension in dimensions.items():
        new_column = _summary_column_after_fees(column)
        dimension.index = get_column_letter(new_column)
        dimension.min = dimension.max = new_column
        ws.column_dimensions[dimension.index] = dimension
    for merged in merges:
        ws.merge_cells(_remap_summary_range(merged))

    for sheet in wb.worksheets:
        for row in sheet:
            for cell in row:
                formula = _formula_text(cell.value)
                if not isinstance(formula, str) or not formula.startswith("="):
                    continue
                remapped = _remap_summary_formula(formula, sheet.title)
                if isinstance(cell.value, str):
                    cell.value = remapped
                elif ":" not in (cell.value.ref or ""):
                    # 模板中的单格数组公式锚点(ref)与所在单元格错位（历史插列
                    # 遗留），Excel 重算保存时会丢弃这类冲突单元格，WPS 直接
                    # 显示空白（如深C广场行 AF/AG）。这些公式均为标量计算，
                    # 不需要 CSE 数组形态，统一降级为普通公式最稳妥。
                    cell.value = remapped
                else:
                    cell.value.text = remapped
                    if sheet is ws:
                        cell.value.ref = _remap_summary_range(cell.value.ref)
    ws.conditional_formatting._cf_rules.clear()
    for ranges, rules in conditional_formats:
        remapped_ranges = " ".join(_remap_summary_range(r) for r in ranges.split())
        for rule in rules:
            if rule.type == "dataBar":
                remapped_ranges = _limit_data_bar_ranges(
                    remapped_ranges,
                    store_count + 1,
                )
                for cfvo in rule.dataBar.cfvo or []:
                    if cfvo.type == "formula" and cfvo.val:
                        cfvo.val = _remap_summary_formula(
                            "=" + cfvo.val,
                            ws.title,
                        )[1:]
            rule.formula = [
                _remap_summary_formula("=" + formula, ws.title)[1:]
                for formula in (rule.formula or [])
            ]
            ws.conditional_formatting.add(remapped_ranges, rule)
    if ws.auto_filter.ref:
        ws.auto_filter.ref = _remap_summary_range(ws.auto_filter.ref)
    if ws.print_area:
        ws.print_area = _remap_summary_formula("=" + str(ws.print_area), ws.title)[1:]
    ws.freeze_panes = "D2"

    fee_letters = []
    amount_format = '#,##0.00;[Red]-#,##0.00;-'
    data_end = store_count + 1
    for source_column, field in _SUMMARY_FEE_COLUMNS.items():
        income_column = _summary_column_after_fees(source_column)
        fee_column = income_column + 1
        income_letter, fee_letter = map(get_column_letter, (income_column, fee_column))
        fee_letters.append(fee_letter)
        ws.column_dimensions[fee_letter].width = 14 if field.startswith("StarThing") else 12
        for row in range(1, total_row + 1):
            cell = ws.cell(row, fee_column)
            cell._style = copy(ws.cell(row, income_column)._style)
            if row == 1:
                cell.value = field.replace("手续费", "\n手续费")
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                continue
            cell.number_format = amount_format
            cell.fill = PatternFill("solid", fgColor="FFF7E6")
            if 2 <= row <= store_count + 1:
                source_letter = _SOURCE_COL_MAP.get(field)
                if source_letter:
                    cell.value = (
                        f'=IFERROR(INDEX(\'源数据\'!${source_letter}$2:${source_letter}${source_total_row - 1},'
                        f'MATCH($C{row},\'源数据\'!$C$2:$C${source_total_row - 1},0)),0)'
                    )
                else:
                    cell.value = f'=IF({income_letter}{row}=0,0,"未提供")'
            elif row == total_row:
                cell.value = (
                    f'=IF(COUNTIF({fee_letter}2:{fee_letter}{data_end},"未提供")>0,'
                    f'"未提供",SUM({fee_letter}2:{fee_letter}{data_end}))'
                )

    income_letter = get_column_letter(_summary_column_after_fees(16))
    # 收入只加原来的 D:O 收入列，不能把插入其中的手续费再加到收入；
    # 油菜花四列（模板 BJ..BM）也是收入明细，一并并入收入汇总。
    for row in range(2, store_count + 2):
        income_cells = [
            f"{get_column_letter(_summary_column_after_fees(col))}{row}"
            for col in range(4, 16)
        ]
        income_cells.extend(
            f"{get_column_letter(_summary_column_after_fees(col))}{row}"
            for col in _YOUCAIHUA_SUMMARY_COLUMNS
        )
        ws[f"{income_letter}{row}"] = f'=SUM({",".join(income_cells)})'

    net_letter = get_column_letter(_summary_column_after_fees(16) + 1)
    ws.column_dimensions[net_letter].width = 16
    for row in range(1, total_row + 1):
        cell = ws[f"{net_letter}{row}"]
        cell._style = copy(ws[f"{income_letter}{row}"]._style)
        if row == 1:
            cell.value = "实际收入"
            continue
        cell.number_format = amount_format
        cell.fill = PatternFill("solid", fgColor="E2F0D9")
        if 2 <= row <= store_count + 1:
            fees = ','.join(f'{col}{row}' for col in fee_letters)
            cell.value = f'={income_letter}{row}-SUM({fees})'
        elif row == total_row:
            cell.value = f'=SUM({net_letter}2:{net_letter}{data_end})'

    fee_row, net_row = total_row + 3, total_row + 4
    for row, label, color in ((fee_row, "手续费汇总", "FFF7E6"), (net_row, "实际收入", "E2F0D9")):
        ws.row_dimensions[row].height = 25
        for column in range(1, _summary_column_after_fees(16) + 1):
            cell = ws.cell(row, column)
            cell._style = copy(ws.cell(total_row, column)._style)
            cell.fill = PatternFill("solid", fgColor=color)
            cell.number_format = amount_format
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
        ws.cell(row, 1).value = label
    for fee_letter in fee_letters:
        ws[f"{fee_letter}{fee_row}"] = f"={fee_letter}{total_row}"
    ws[f"{income_letter}{fee_row}"] = '=SUM(' + ','.join(f'{col}{total_row}' for col in fee_letters) + ')'
    ws[f"{income_letter}{net_row}"] = f'={income_letter}{total_row}-{income_letter}{fee_row}'
    note_row = net_row + 2
    ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=_summary_column_after_fees(16))
    ws.cell(note_row, 1).value = (
        "说明：手续费汇总仅含已有费用数据；鲸舰未提供独立手续费，未计入扣减。"
        "实际收入 = 原收入汇总 − 已有手续费，沿用原报表收入口径。"
    )
    ws.cell(note_row, 1).alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[note_row].height = 30
    if ws.print_area:
        ws.print_area = f'A1:{get_column_letter(ws.max_column)}{note_row}'


def load_data(date: str, venue_scope=None) -> list:
    """加载所选月截至目标日、各门店平台的最后一份累计数据。"""
    return load_period_summary_data(date, venue_scope)


def _last_month_same_date(dt: datetime) -> datetime:
    """返回上个月同日"""
    prev_last = dt.replace(day=1) - timedelta(days=1)
    return prev_last.replace(day=min(dt.day, prev_last.day))


def build_data_dict(date: str, venue_scope=None) -> dict:
    """
    计算目标日期所有场地维度数据，返回 {场地: {列名: 值}} 字典
    """
    data_list = load_data(date, venue_scope)
    if not data_list:
        return {}
    venue_scope = accounting_venue_scope(data_list)
    if not venue_scope:
        return {}
    result = report_summary.main(
        data_list,
        active_venues=venue_scope,
    )
    if not result or len(result) < 2:
        return {}
    cols = result[0]
    data_dict = {}
    for row in result[1:]:
        venue = row[2] if len(row) > 2 else ""
        if venue == "合计":
            continue
        row_dict = {}
        for i, col_name in enumerate(cols):
            val = row[i] if i < len(row) else ""
            row_dict[col_name] = val
        # 模板"门店汇总"AE/J 列公式会从"鲸舰现金"中扣减 Kpay 收款，
        # 而 report_summary 已在"鲸舰现金"中扣减过（避免收入汇总重复统计）。
        # 写入源数据时需恢复含 KPay 的原始鲸舰现金，交由模板公式扣减，避免双重扣减。
        if venue in report_summary.WHALE_CASH_INCLUDES_KPAY_VENUES:
            kpay = float(row_dict.get("Kpay收款", 0) or 0)
            jj_cash = float(row_dict.get("鲸舰现金", 0) or 0)
            row_dict["鲸舰现金"] = jj_cash + kpay
        data_dict[venue] = row_dict
    return data_dict


def fill_summary_sheet(ws, data_dict: dict, store_list: list,
                       target_map: dict, store_max_row: int,
                       total_row: int):
    """
    填充门店汇总/内地门店/香港门店 数据行

    ws: openpyxl worksheet
    data_dict: {场地: {列名: 值}}
    store_list: [(行号, 场地名)] 列表，保持模板顺序
    region_map: {场地: 区域}
    target_map: {场地: 目标值(元)}
    """
    # 先收集所有场地的数据，便于计算区域级的汇总
    venue_data = {}
    for row_num, venue in store_list:
        d = data_dict.get(venue, {})
        venue_data[venue] = d

    # 区域级指标（X区域预收入完成率 / Y区域货款比 / Z区域存货率）
    # 由模板已有的公式自动计算（X=SUM(P)/SUM(V)，Y=SUM(S)/SUM(P)，Z保留），此处不写入，
    # 避免覆盖模板按区域合并的单元格公式。

    for row_num, venue in store_list:
        d = venue_data.get(venue, {})
        if not d:
            continue

        for col_letter, src_col in _SUMMARY_COL_MAP.items():
            cell = ws[f"{col_letter}{row_num}"]
            if src_col is None:
                # 特殊计算列
                if col_letter == "M":
                    # 其余业绩 = 兑币机收款 + 其他收入
                    v1 = float(d.get("兑币机收款", 0) or 0)
                    v2 = float(d.get("其他收入", 0) or 0)
                    cell.value = v1 + v2
                elif col_letter == "Q":
                    # 积分货款 = sum of 积分货款
                    cols = ["StarThing积分货款", "鲸舰积分货款",
                            "新系统积分货款", "芸苔积分货款"]
                    val = sum(float(d.get(c, 0) or 0) for c in cols)
                    cell.value = val
                elif col_letter == "BB":
                    # 积分减少 = sum of all 积分减少
                    reduction_cols = ["鲸舰积分减少", "新系统积分减少",
                                      "StarThing积分减少", "芸苔积分减少"]
                    val = sum(float(d.get(c, 0) or 0) for c in reduction_cols)
                    cell.value = val
                elif col_letter == "AZ":
                    # 出货积分数 - 保留模板值，不覆盖
                    pass
            elif src_col == "__fixed__":
                if col_letter == "U":
                    cell.value = 0.45
                elif col_letter == "BD":
                    cell.value = 0.45
            else:
                raw = d.get(src_col)
                if raw is not None and raw != "":
                    try:
                        cell.value = float(raw)
                    except (ValueError, TypeError):
                        cell.value = raw

        # 区域级 X/Y/Z 列由模板公式计算，不在此写入

    # 合计行
    if total_row:
        for col_letter in _SUMMARY_COL_MAP:
            col_values = []
            for row_num, venue in store_list:
                d = venue_data.get(venue, {})
                cell_ref = f"{col_letter}{row_num}"
                try:
                    v = ws[cell_ref].value
                    if v is not None and isinstance(v, (int, float)):
                        col_values.append(v)
                except (ValueError, TypeError):
                    pass
            if col_values:
                ws[f"{col_letter}{total_row}"].value = sum(col_values)

        # 合计行 货款比% (T) = 总货款/收入汇总
        total_p = sum(
            float(venue_data.get(v, {}).get("总货款", 0) or 0)
            for _, v in store_list
        )
        total_income = sum(
            float(venue_data.get(v, {}).get("收入汇总", 0) or 0)
            for _, v in store_list
        )
        if total_income > 0:
            ws[f"T{total_row}"].value = total_p / total_income

        # 合计行 预收入完成率 (W)
        total_target = sum(
            float(target_map.get(v, 0) or 0) for _, v in store_list
        )
        if total_target > 0:
            ws[f"W{total_row}"].value = total_income / total_target


def fill_source_sheet(ws, data_dict: dict, store_list: list, total_row: int):
    """填充源数据表"""
    for row_num, venue in store_list:
        # 输出是从旧模板复制而来，先清空整行，避免撤店/无数据门店残留上一次导出的金额。
        for col_letter in set(_SOURCE_COL_MAP.values()):
            ws[f"{col_letter}{row_num}"].value = None
        d = data_dict.get(venue, {})
        if not d:
            continue
        for col_name, col_letter in _SOURCE_COL_MAP.items():
            raw = d.get(col_name)
            if raw is not None and raw != "":
                try:
                    ws[f"{col_letter}{row_num}"].value = float(raw)
                except (ValueError, TypeError):
                    ws[f"{col_letter}{row_num}"].value = raw

    # 合计行
    if total_row:
        for col_letter in set(_SOURCE_COL_MAP.values()):
            col_values = []
            for row_num, venue in store_list:
                try:
                    v = ws[f"{col_letter}{row_num}"].value
                    if v is not None and isinstance(v, (int, float)):
                        col_values.append(v)
                except (ValueError, TypeError):
                    pass
            if col_values:
                ws[f"{col_letter}{total_row}"].value = sum(col_values)


def _formula_text(value):
    return getattr(value, "text", value) if value is not None else None


def _copy_row_layout(ws, source_row: int, target_row: int):
    """复制一行的样式/高度/公式；用于动态新增门店行。"""
    if source_row == target_row:
        return
    if ws.row_dimensions[source_row].height is not None:
        ws.row_dimensions[target_row].height = ws.row_dimensions[source_row].height
    for column in range(1, ws.max_column + 1):
        source = ws.cell(source_row, column)
        target = ws.cell(target_row, column)
        if source.has_style:
            target._style = copy(source._style)
        if source.number_format:
            target.number_format = source.number_format
        if source.alignment:
            target.alignment = copy(source.alignment)
        if source.protection:
            target.protection = copy(source.protection)
        formula = _formula_text(source.value)
        if isinstance(formula, str) and formula.startswith("="):
            try:
                target.value = Translator(
                    formula,
                    origin=f"{source.column_letter}{source_row}",
                ).translate_formula(f"{target.column_letter}{target_row}")
            except Exception:
                target.value = formula


def _ensure_total_row(ws, total_row: int, required_last_row: int) -> int:
    """在合计行前补足动态门店行，并返回移动后的合计行号。"""
    if required_last_row < total_row:
        return total_row
    extra = required_last_row - total_row + 1
    template_row = max(2, total_row - 1)
    ws.insert_rows(total_row, extra)
    for row_num in range(total_row, total_row + extra):
        _copy_row_layout(ws, template_row, row_num)
    return total_row + extra


def _venue_region(venue: str, region_map: dict) -> str:
    return str(region_map.get(venue) or ("香港" if "香港" in str(venue) else "其他")).strip()


def _group_names_by_region(names: list, region_map: dict) -> list:
    """按区域聚合门店顺序，保证同区域门店在汇总页连成一块。

    区域首次出现的顺序保持不变，区域内相对顺序不变；
    "其他"固定排在最后，其余新区域按首次出现顺序追加。
    """
    order, buckets = [], {}
    for name in names:
        region = _venue_region(name, region_map)
        if region not in buckets:
            buckets[region] = []
            order.append(region)
        buckets[region].append(name)
    if "其他" in buckets:
        order = [region for region in order if region != "其他"] + ["其他"]
    return [name for region in order for name in buckets[region]]


def _ordered_names(existing_names, candidates):
    candidates = {str(value).strip() for value in candidates if str(value).strip()}
    result = [name for name in existing_names if name in candidates]
    result.extend(sorted(candidates - set(result)))
    return result


def _load_owner_map() -> dict:
    try:
        dataframe, _ = report_summary._get_base_df()
        columns = {str(column).strip(): column for column in dataframe.columns}
        venue_col = columns.get("场地")
        owner_col = columns.get("负责人")
        if venue_col and owner_col:
            return {
                str(row[venue_col]).strip(): str(row[owner_col] or "").strip()
                for _, row in dataframe.iterrows()
                if str(row[venue_col] or "").strip()
            }
    except Exception:
        pass
    return {}


def _dynamic_venue_lists(wb, data_dict: dict):
    """核算表仅使用所选期间至少有一项非零数值的门店。"""
    source_ws = wb["源数据"]
    existing = [
        str(source_ws.cell(row, 3).value).strip()
        for row in range(2, 51)
        if str(source_ws.cell(row, 3).value or "").strip()
    ]
    summary_existing = [
        str(wb["门店汇总"].cell(row, 3).value).strip()
        for row in range(2, wb["门店汇总"].max_row + 1)
        if str(wb["门店汇总"].cell(row, 3).value or "").strip()
    ]
    historical = {
        str(value).strip() for value in data_dict
        if str(value).strip()
    }
    accounting_names = historical
    source_names = _ordered_names(existing, accounting_names)
    region_map = load_store_regions()
    # 先按模板中的区域块排序，再把新增门店放入对应区域块末尾，避免拆散合并单元格。
    active_existing = _ordered_names(summary_existing, accounting_names)
    region_order = []
    grouped = {}
    for venue in active_existing:
        region = _venue_region(venue, region_map)
        if region not in grouped:
            region_order.append(region)
            grouped[region] = []
        grouped[region].append(venue)
    for venue in sorted(accounting_names - set(active_existing)):
        region = _venue_region(venue, region_map)
        if region not in grouped:
            region_order.append(region)
            grouped[region] = []
        grouped[region].append(venue)
    active_names = [venue for region in region_order for venue in grouped[region]]
    return active_names, source_names


def _remove_empty_hong_kong_sections(wb) -> None:
    """无香港非零数据时移除香港子表，并清理老板总览中的香港区域行。"""
    _remove_empty_region_sections(wb, ("香港门店", "香港排名"), ("香港",))


def _remove_empty_region_sections(wb, sheet_names, overview_regions) -> None:
    """移除无数据区域的子表，并隐藏老板总览中对应区域行。"""
    # 模板表名可能带尾随空格（如"香港排名 "），按 strip 后的名字匹配，
    # 避免漏删导致其公式悬空引用已移除的区域子表。
    for sheet_name in sheet_names:
        target = sheet_name.strip()
        for actual in [s for s in wb.sheetnames if s.strip() == target]:
            wb.remove(wb[actual])

    if "老板总览" not in wb.sheetnames:
        return
    overview = wb["老板总览"]
    for row in range(1, overview.max_row + 1):
        if str(overview.cell(row, 1).value or "").strip() not in overview_regions:
            continue
        for column in range(1, overview.max_column + 1):
            overview.cell(row, column).value = None
        overview.row_dimensions[row].hidden = True


def _restore_tail_section_merges(ws, total_row: int) -> None:
    """恢复合计行以下说明区块（手续费汇总/实际收入/说明行）的合并。

    _rewrite_summary_roster 会先整页取消全部合并再重建区域合并，
    不会还原这些尾行；整页复制出的"撤店门店"页若不补回，
    行首标签会挤在 A 列（宽约 5 字符）里显示不全。
    """
    last_column = _summary_column_after_fees(16)
    for row in range(total_row + 1, ws.max_row + 1):
        label = str(ws.cell(row, 1).value or "")
        if label in ("手续费汇总", "实际收入"):
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
        elif label.startswith("说明："):
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=last_column)


TARGET_SHEET_NAME = "每月目标"


def _create_target_sheet(wb, targets: dict, month: str) -> None:
    """写入「每月目标」子表：按场地名列出当月最终核定目标。

    门店汇总/撤店门店/门店排名的预计收入列通过 VLOOKUP 按场地名
    从本表取数（而非模板静态值），行序调整、门店增减都不会造成
    目标与门店错位。
    """
    if TARGET_SHEET_NAME in wb.sheetnames:
        wb.remove(wb[TARGET_SHEET_NAME])
    ws = wb.create_sheet(TARGET_SHEET_NAME)

    thin = Side(style="thin")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor="D9E2F3")
    total_fill = PatternFill("solid", fgColor="FFF2CC")
    amount_format = '#,##0.00;[Red]-#,##0.00;-'

    for column, title in enumerate(("序号", "场地", "月度目标(元)"), start=1):
        cell = ws.cell(1, column, title)
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 42
    ws.column_dimensions["C"].width = 20

    ordered = sorted(targets.items())
    for index, (venue, target) in enumerate(ordered, start=1):
        row = index + 1
        ws.cell(row, 1, index).alignment = Alignment(horizontal="center")
        ws.cell(row, 2, venue).border = border
        value_cell = ws.cell(row, 3, float(target))
        value_cell.number_format = amount_format
        for column in (1, 2, 3):
            ws.cell(row, column).border = border

    total_row = len(ordered) + 2
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=2)
    ws.cell(total_row, 1, "合计").alignment = Alignment(horizontal="center")
    total_cell = ws.cell(total_row, 3)
    if ordered:
        total_cell.value = f"=SUM(C2:C{total_row - 1})"
    total_cell.number_format = amount_format
    for column in (1, 2, 3):
        ws.cell(total_row, column).border = border
        ws.cell(total_row, column).fill = total_fill

    note_row = total_row + 2
    ws.cell(note_row, 1).value = (
        f"统计月份：{month}　·　数据来源：预测分析-每月目标导入（data/targets）。"
        "主表预计收入列按场地名从本表匹配，门店顺序调整不影响目标对应关系。"
    )
    ws.freeze_panes = "A2"
    ws.sheet_view.tabSelected = False


def _create_closed_summary_sheet(wb, closed_names: list, region_map: dict,
                                 target_map: dict = None):
    """从处理完的"门店汇总"复制出"撤店门店"页，仅保留撤店门店行。

    源数据 4 表仍包含全部门店，撤店门店的 INDEX/MATCH 公式按场地名
    正常取数；月中撤店门店的撤店前累计数据因此得以保留。
    """
    template_ws = wb["门店汇总"]
    if "撤店门店" in wb.sheetnames:
        wb.remove(wb["撤店门店"])
    ws = wb.copy_worksheet(template_ws)
    ws.title = "撤店门店"
    # copy_worksheet 不复制条件格式，手动补齐数据条等规则。
    for conditional_format in template_ws.conditional_formatting:
        for rule in template_ws.conditional_formatting._cf_rules[conditional_format]:
            ws.conditional_formatting.add(
                str(conditional_format.sqref), deepcopy(rule),
            )
    ws.freeze_panes = template_ws.freeze_panes
    total_row = _rewrite_summary_roster(
        ws, closed_names, region_map, shifted=True, target_map=target_map,
    )
    _restore_tail_section_merges(ws, total_row)
    # 排列到"门店汇总"之后，与在营/撤店的阅读顺序一致。
    wb.move_sheet(
        ws,
        offset=wb.sheetnames.index("门店汇总") + 1 - wb.sheetnames.index("撤店门店"),
    )
    return ws


def _rewrite_source_roster(ws, names: list, owner_map: dict) -> list:
    total_row = 51
    total_row = _ensure_total_row(ws, total_row, 1 + len(names))
    for row in range(2, total_row):
        for column in range(1, 4):
            ws.cell(row, column).value = None
        if row > 1 + len(names):
            for col_letter in set(_SOURCE_COL_MAP.values()):
                ws[f"{col_letter}{row}"].value = None
    store_list = []
    for index, venue in enumerate(names, start=1):
        row = index + 1
        ws.cell(row, 1).value = index
        ws.cell(row, 2).value = owner_map.get(venue, "")
        ws.cell(row, 3).value = venue
        store_list.append((row, venue))
    return store_list, total_row


def _rewrite_summary_roster(ws, names: list, region_map: dict,
                            shifted: bool = False, target_map: dict = None):
    """重写汇总页门店行、区域合并和区域公式，支持新增/撤店。

    shifted=True 表示该页是从手续费列插入后的"门店汇总"复制而来的
    （撤店门店页），此时区域三列与公式坐标使用最终布局，无需再平移。

    target_map 非 None 时（当月已导入每月目标），预计收入列改写为
    VLOOKUP 公式，按场地名从「每月目标」子表取数，避免行序偏移导致
    门店之间目标混乱；未导入时保留模板静态值按门店名原样回填。
    """
    if shifted:
        region_columns = (
            _summary_column_after_fees(28),
            _summary_column_after_fees(29),
            _summary_column_after_fees(30),
        )
        income_letter = get_column_letter(_summary_column_after_fees(16))
        target_letter = get_column_letter(_summary_column_after_fees(26))
        payment_letter = get_column_letter(_summary_column_after_fees(23))
    else:
        region_columns = (28, 29, 30)
        income_letter, target_letter, payment_letter = "P", "Z", "W"
    total_row = next(
        (row for row in range(1, ws.max_row + 1) if ws.cell(row, 1).value == "合计"),
        ws.max_row,
    )
    target_column = column_index_from_string(target_letter)
    # 预计收入列在公式模式下统一改写为 VLOOKUP；静态模式下是模板值，
    # 与行序绑定：重排前先记录 门店名 -> 预计收入，重排后按门店名回填。
    # 区域聚合调整行序、撤店页整页复制时都靠它保持对位，
    # 名单里新出现的门店（无历史目标）该列留空。
    previous_targets = {}
    if target_map is None:
        for row in range(2, total_row):
            name = ws.cell(row, 3).value
            if str(name or "").strip():
                previous_targets[str(name).strip()] = ws.cell(row, target_column).value
    for merged in list(ws.merged_cells.ranges):
        ws.unmerge_cells(str(merged))
    old_data_end = total_row - 3
    total_row = _ensure_total_row(ws, total_row, 1 + len(names))
    data_end = max(1, 1 + len(names))
    for row in range(2, total_row):
        if row > data_end:
            for column in range(1, ws.max_column + 1):
                ws.cell(row, column).value = None
    template_row = 2
    for row in range(2, data_end + 1):
        if row > template_row:
            _copy_row_layout(ws, template_row, row)
        ws.cell(row, 1).value = row - 1
        ws.cell(row, 2).value = _venue_region(names[row - 2], region_map)
        ws.cell(row, 3).value = names[row - 2]
        if target_map is not None:
            # 按场地名从「每月目标」子表取数；匹配不到时为 0（该门店当月无核定目标）。
            ws.cell(row, target_column).value = (
                f"=IFERROR(VLOOKUP($C{row},'{TARGET_SHEET_NAME}'!$B:$C,2,0),0)"
            )
        else:
            ws.cell(row, target_column).value = previous_targets.get(str(names[row - 2]).strip())
    # 清理每一行的旧区域公式，随后按当前区域动态合并。
    # 2026-09-25 模板 Q..T 插入油菜花列后，区域三列由 X/Y/Z(24..26) 右移至 AB/AC/AD(28..30)。
    for row in range(2, total_row):
        for col in region_columns:
            ws.cell(row, col).value = None
    groups = []
    start = 2
    while start <= data_end:
        label = _venue_region(names[start - 2], region_map)
        end = start
        while end < data_end and _venue_region(names[end - 1], region_map) == label:
            end += 1
        groups.append((start, end, label))
        start = end + 1
    for start, end, label in groups:
        ws.cell(start, 2).value = label
        if end > start:
            ws.merge_cells(start_row=start, start_column=2, end_row=end, end_column=2)
        # 区域预收入完成率 = SUM(收入汇总)/SUM(预计收入)；区域货款比 = SUM(总货款)/SUM(收入汇总)
        ws.cell(start, region_columns[0]).value = (
            f'=IFERROR(SUM({income_letter}{start}:{income_letter}{end})/'
            f'SUM({target_letter}{start}:{target_letter}{end}),0)'
        )
        ws.cell(start, region_columns[1]).value = (
            f'=IFERROR(SUM({payment_letter}{start}:{payment_letter}{end})/'
            f'SUM({income_letter}{start}:{income_letter}{end}),0)'
        )
        ws.cell(start, region_columns[2]).value = "\\"
        for col in region_columns:
            if end > start:
                ws.merge_cells(start_row=start, start_column=col, end_row=end, end_column=col)
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=3)
    ws.cell(total_row, 1).value = "合计"
    for column in range(4, ws.max_column + 1):
        cell = ws.cell(total_row, column)
        formula = _formula_text(cell.value)
        if isinstance(formula, str) and formula.startswith("="):
            cell.value = re.sub(
                r":([A-Z]{1,3})\d+\b",
                lambda match: f":{match.group(1)}{data_end}",
                formula,
            )
    _repair_summary_borders(ws, total_row, data_end, region_columns)
    return total_row


def _repair_summary_borders(ws, total_row: int, data_end: int, region_columns) -> None:
    """修补重排名单后的边框缺口。

    区域合并经 openpyxl unmerge/merge 循环后，B/C 列非锚点单元格的边框会
    丢失；门店数少于模板行数时，备用行（尤其模板末尾本就无边框的行）会在
    表格中间露出无边框的"空洞"（典型表现：末行门店与下一行之间的横线断开）。
    这里统一补全：数据行补 A/C/区域三列，备用行整行按数据区右边界补齐，
    合计行 A:C 合并框四边补全。合并区域内部边线不会渲染，逐格补边框是安全的。
    """
    thin = Side(style="thin")
    full_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    edge = 1
    for cell in ws[2]:
        border = cell.border
        if border is None:
            continue
        if any(
            (side := getattr(border, name)) is not None and side.style
            for name in ("left", "right", "top", "bottom")
        ):
            edge = max(edge, cell.column)
    normalize_cols = {1, 2, 3, *region_columns}
    for row in range(2, total_row):
        cols = range(1, edge + 1) if row > data_end else normalize_cols
        for col in cols:
            ws.cell(row, col).border = full_border
    for col in range(1, edge + 1):
        ws.cell(total_row, col).border = full_border


def _rewrite_ranking_roster(ws, names: list, data_dict: dict, region_map: dict):
    total_row = next(
        (row for row in range(1, ws.max_row + 1) if ws.cell(row, 1).value == "合计"),
        ws.max_row,
    )
    total_row = _ensure_total_row(ws, total_row, 1 + len(names))
    ranked = sorted(
        names,
        key=lambda venue: -float(data_dict.get(venue, {}).get("收入汇总", 0) or 0),
    )
    previous_region_rank = {}
    for row in range(2, total_row):
        if row > len(ranked) + 1:
            for column in range(1, ws.max_column + 1):
                ws.cell(row, column).value = None
    for index, venue in enumerate(ranked, start=1):
        row = index + 1
        if row > 2:
            _copy_row_layout(ws, 2, row)
        region = _venue_region(venue, region_map)
        previous_region_rank[region] = previous_region_rank.get(region, 0) + 1
        ws.cell(row, 1).value = index
        ws.cell(row, 2).value = f"{region} - {previous_region_rank[region]}"
        ws.cell(row, 3).value = region
        ws.cell(row, 4).value = venue
    for column in range(5, ws.max_column + 1):
        cell = ws.cell(total_row, column)
        formula = _formula_text(cell.value)
        if isinstance(formula, str) and formula.startswith("="):
            cell.value = re.sub(
                r":([A-Z]{1,3})\d+\b",
                lambda match: f":{match.group(1)}{len(ranked) + 1}",
                formula,
            )
    return total_row


def fill_ranking_sheet(ws, data_dict: dict, store_list: list,
                       source_data_dict: dict, prev_data_dict: dict,
                       prev2_data_dict: dict, last_month_data_dict: dict,
                       target_map: dict, total_row: int):
    """
    填充排名表（门店排名/内地排名/香港排名）
    排名按收入汇总降序排列
    """
    # 构建排名列表
    ranked = []
    for row_num, venue in store_list:
        d = data_dict.get(venue, {})
        income = float(d.get("收入汇总", 0) or 0)
        ranked.append((venue, income, d))

    # 按收入降序
    ranked.sort(key=lambda x: -x[1])

    # 填充
    for idx, (venue, income, d) in enumerate(ranked):
        row_num = store_list[0][0] + idx  # 从第2行开始

        ws[f"A{row_num}"] = idx + 1
        ws[f"D{row_num}"] = venue
        ws[f"E{row_num}"] = income

        # 环比上月 = 上月收入汇总
        prev_income = float(
            (last_month_data_dict.get(venue, {})).get("收入汇总", 0) or 0
        )
        ws[f"F{row_num}"] = prev_income
        # 月环比率 = (本月-上月)/上月；无上月可比数据时留空。
        if prev_income > 0:
            ws[f"G{row_num}"] = (income - prev_income) / prev_income

        # 当日收入(元) = 当日累计收入 - 昨日累计收入
        prev_day_income = float(
            (prev_data_dict.get(venue, {})).get("收入汇总", 0) or 0
        )
        daily_income = income - prev_day_income
        ws[f"H{row_num}"] = daily_income

        # 日环比 = 昨日收入 - 前日收入
        prev2_day_income = float(
            (prev2_data_dict.get(venue, {})).get("收入汇总", 0) or 0
        )
        prev_daily = prev_day_income - prev2_day_income if prev2_data_dict else 0
        ws[f"I{row_num}"] = prev_daily

        # 日增长率 = (当日收入-日环比)/日环比
        if prev_daily != 0:
            ws[f"J{row_num}"] = (daily_income - prev_daily) / prev_daily

        # 远程取币(元) = 前日芸苔远程取币
        prev2_yc = float(
            (prev2_data_dict.get(venue, {})).get("芸苔远程取币", 0) or 0
        )
        ws[f"K{row_num}"] = prev2_yc

        # 积分货款
        total_payment = float(d.get("总货款", 0) or 0)
        ws[f"L{row_num}"] = total_payment

        # 投币合计
        coin_in = float(d.get("投币合计", 0) or 0)
        ws[f"Q{row_num}"] = coin_in

        # 出货合计
        goods_out = float(d.get("出货合计", 0) or 0)
        ws[f"S{row_num}"] = goods_out

        # 存货合计
        stock = float(d.get("存货合计", 0) or 0)
        ws[f"T{row_num}"] = stock

        # 目标
        target = float(target_map.get(venue, 0) or 0)
        ws[f"X{row_num}"] = target / 10000 if target else 0
        # 完成率
        if target > 0:
            ws[f"Y{row_num}"] = income / target

        # 货款比
        ws[f"O{row_num}"] = float(d.get("货款比%", 0) or 0)
        # 货款比指标
        ws[f"P{row_num}"] = 0.45
        # 存货比率
        ws[f"U{row_num}"] = float(d.get("存货比率（存货与出货比）", 0) or 0)
        # 存货比指标
        ws[f"V{row_num}"] = 0.45
        # 出货率
        ws[f"W{row_num}"] = float(d.get("出货率（投币与出货比）", 0) or 0)

    # 合计行
    if total_row:
        for col_letter in ["E", "F", "H", "I", "K", "L", "Q", "S", "T", "X"]:
            col_values = []
            for r in range(store_list[0][0], total_row):
                try:
                    v = ws[f"{col_letter}{r}"].value
                    if v is not None and isinstance(v, (int, float)):
                        col_values.append(v)
                except (ValueError, TypeError):
                    pass
            if col_values:
                ws[f"{col_letter}{total_row}"].value = sum(col_values)

        total_income = ws[f"E{total_row}"].value or 0
        total_target = ws[f"X{total_row}"].value or 0
        if total_target:
            ws[f"Y{total_row}"].value = total_income / (total_target * 10000)


def normalize_ranking_formulas(wb) -> None:
    """统一排名页环比口径，并让无可比基数的增长率留空。"""
    for sheet_name in ("门店排名", "内地排名", "香港排名"):
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        total_row = next(
            (row for row in range(1, ws.max_row + 1) if ws.cell(row, 1).value == "合计"),
            ws.max_row,
        )
        start_row, end_row = 2, max(1, total_row - 3)
        for row_num in list(range(start_row, end_row + 1)) + [total_row]:
            ws[f"G{row_num}"] = (
                f'=IF(F{row_num}=0,"",(E{row_num}-F{row_num})/F{row_num})'
            )
            ws[f"J{row_num}"] = (
                f'=IF(I{row_num}=0,"",(H{row_num}-I{row_num})/I{row_num})'
            )


def remove_invalid_conditional_formats(wb) -> None:
    """移除排名页历史遗留的失效条件格式，保留其他正常业务标记。"""
    for sheet_name in ("门店排名", "内地排名", "香港排名"):
        if sheet_name not in wb.sheetnames:
            continue
        conditional_formats = wb[sheet_name].conditional_formatting
        for conditional_format in list(conditional_formats):
            rules = conditional_formats._cf_rules[conditional_format]
            valid_rules = []
            for rule in rules:
                formulas = rule.formula or []
                quoted_expression = any(
                    formula.startswith('"') and formula.endswith('"')
                    for formula in formulas
                )
                oversized_range = "1048576" in str(conditional_format.sqref)
                if not quoted_expression and not oversized_range:
                    valid_rules.append(rule)
            if valid_rules:
                conditional_formats._cf_rules[conditional_format] = valid_rules
            else:
                del conditional_formats._cf_rules[conditional_format]


def normalize_negative_number_formats(wb) -> None:
    """负数统一显示减号，不使用会造成误读的括号。"""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                number_format = cell.number_format
                if not isinstance(number_format, str) or "(" not in number_format:
                    continue
                number_format = re.sub(
                    r"\[Red\]\(([^)]+)\)",
                    r"[Red]-\1",
                    number_format,
                )
                number_format = re.sub(
                    r"\(([^)]+)\)",
                    r"-\1",
                    number_format,
                )
                cell.number_format = number_format


def normalize_income_data_bars(output_path: str) -> None:
    """将收入数据条固定为 0 起点、无轴线、纯色填充。"""
    temporary_path = f"{output_path}.databar-tmp"
    with zipfile.ZipFile(output_path, "r") as source:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml"):
                    text = payload.decode("utf-8")

                    def replace_data_bar(match):
                        attrs = re.sub(r'\s+(?:gradient|axisPosition)="[^"]*"', "", match.group(1))
                        return f'<dataBar gradient="0" axisPosition="none"{attrs}>'

                    text = re.sub(r"<dataBar([^>]*)>", replace_data_bar, text)
                    payload = text.encode("utf-8")
                target.writestr(info, payload)
    try:
        os.replace(temporary_path, output_path)
    except PermissionError:
        # 目标文件正被 Excel/WPS/预览占用：允许写入但不允许替换句柄时，
        # 退化为原地覆写内容（写入权限仍在），随后清理临时文件。
        with open(output_path, "r+b") as dst, open(temporary_path, "rb") as src:
            dst.write(src.read())
            dst.truncate()
        os.remove(temporary_path)


def generate(target_date: str, output_path: str = None, venue_scope=None) -> str:
    """
    生成老板报表

    Args:
        target_date: 目标日期 YYYY-MM-DD
        output_path: 输出路径，默认生成到 data/reports/ 目录

    Returns:
        输出文件路径
    """
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    prev_day = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    prev_day2 = (dt - timedelta(days=2)).strftime("%Y-%m-%d")
    last_month = _last_month_same_date(dt).strftime("%Y-%m-%d")

    print(f"为目标日期 {target_date} 生成报表")
    print(f"  昨日: {prev_day}, 前日: {prev_day2}, 上月同日: {last_month}")

    # 1. 加载数据
    data_dict = build_data_dict(target_date, venue_scope)
    prev_data_dict = build_data_dict(prev_day, venue_scope) if prev_day else {}
    prev2_data_dict = build_data_dict(prev_day2, venue_scope) if prev_day2 else {}
    last_month_data_dict = build_data_dict(last_month, venue_scope) if last_month else {}

    if not data_dict:
        print(f"错误: 目标日期 {target_date} 无数据")
        return ""

    print(f"成功加载 {len(data_dict)} 个场地的数据")

    # 2. 加载目标：只取报表月份的目标表，供「每月目标」子表与主表 VLOOKUP 使用，
    #    主表预计收入列改为按场地名取数，避免目标随行序偏移。
    target_month = dt.strftime("%Y-%m")
    store_targets = load_store_targets(target_month)

    # 3. 复制模板
    os.makedirs(_OUTPUT_DIR, exist_ok=True)
    if not output_path:
        output_path = os.path.join(
            _OUTPUT_DIR,
            f"每月货款比 {target_date}.xlsx",
        )

    shutil.copy2(_TEMPLATE_PATH, output_path)
    wb = openpyxl.load_workbook(output_path)
    # 旧模板遗留的尾随空格会影响部分预览器识别工作表；新导出统一为正常名称。
    if "香港排名 " in wb.sheetnames and "香港排名" not in wb.sheetnames:
        wb["香港排名 "].title = "香港排名"
    normalize_ranking_formulas(wb)
    remove_invalid_conditional_formats(wb)
    normalize_negative_number_formats(wb)

    # 「每月目标」子表：主表预计收入列的 VLOOKUP 数据源，需在重排名单前建好。
    if store_targets:
        _create_target_sheet(wb, store_targets, target_month)
        print(f"月度目标：{target_month} 共 {len(store_targets)} 家，已写入「{TARGET_SHEET_NAME}」子表")
    else:
        print(f"月度目标：{target_month} 未导入目标表，预计收入列沿用模板静态值")

    # 门店清单不再写死：核算表只保留本期至少有一项非零数值的门店，
    # 同时保留月中撤店门店截至撤店前的最后累计数据。
    active_names, source_names = _dynamic_venue_lists(wb, data_dict)
    # 按门店生命周期拆分在营/撤店：首页汇总与排名只保留在营门店，
    # 撤店门店（含月中撤店）的数据单独保留到"撤店门店"页。
    owner_map = _load_owner_map()
    operating_set = operating_venues_on(
        target_date, active_names, fallback_operating=active_names,
    )
    # 与数据看板口径对齐：MySQL 负责人标记为"撤店"的门店一律视为撤店，
    # 即使本地 venue_lifecycle 缺少闭店记录（如K区万象汇solmo）。
    closed_by_owner = {
        name for name in operating_set
        if "撤店" in (owner_map.get(name) or "")
    }
    operating_set -= closed_by_owner
    operating_names = [name for name in active_names if name in operating_set]
    closed_names = [name for name in active_names if name not in operating_set]
    if not operating_names:
        # 兜底：生命周期数据异常导致全部判为撤店时，按原口径输出，避免空表。
        operating_names, closed_names = active_names, []
    print(
        f"门店口径：在营 {len(operating_names)} 家，"
        f"撤店 {len(closed_names)} 家"
        + (f"（{', '.join(closed_names)}）" if closed_names else "")
    )
    region_map = load_store_regions()
    # 区域重划（regions.json 增改）后按区域聚合门店顺序，同区域连成一块，
    # 避免同一区域出现多段合并组；区域内相对顺序保持模板既有顺序。
    operating_names = _group_names_by_region(operating_names, region_map)
    closed_names = _group_names_by_region(closed_names, region_map)
    source_rosters = {}
    source_total_rows = {}
    for source_sheet_name in ("源数据", "上月数据", "昨日数据", "前日数据"):
        source_rosters[source_sheet_name], source_total_rows[source_sheet_name] = _rewrite_source_roster(
            wb[source_sheet_name], source_names, owner_map,
        )
    # 不再区分香港/内地：区域子表整体移除，总览区域行改写为在营/撤店。
    _remove_empty_region_sections(
        wb,
        ("内地门店", "内地排名", "香港门店", "香港排名"),
        ("内地", "香港"),
    )
    summary_store_lists = {
        "门店汇总": operating_names,
    }
    summary_target_map = store_targets if store_targets else None
    summary_total_rows = {
        name: _rewrite_summary_roster(wb[name], names, region_map, target_map=summary_target_map)
        for name, names in summary_store_lists.items()
    }
    ranking_names = {
        "门店排名": operating_names,
    }
    ranking_total_rows = {
        name: _rewrite_ranking_roster(wb[name], names, data_dict, region_map)
        for name, names in ranking_names.items()
    }

    # 5. 填充源数据表
    # 门店汇总/内地门店/香港门店/门店排名/内地排名/香港排名 均为模板公式驱动，
    # 通过 INDEX/MATCH 从"源数据/上月数据/昨日数据/前日数据"自动计算，此处不写值。
    fill_source_sheet(
        wb["源数据"], data_dict, source_rosters["源数据"],
        total_row=source_total_rows["源数据"],
    )

    # 6. 填充上月数据/昨日数据/前日数据
    for ws_name, dd in [("上月数据", last_month_data_dict),
                         ("昨日数据", prev_data_dict),
                         ("前日数据", prev2_data_dict)]:
        if dd:
            fill_source_sheet(
                wb[ws_name], dd, source_rosters[ws_name],
                total_row=source_total_rows[ws_name],
            )

    add_summary_fee_layout(
        wb, summary_total_rows["门店汇总"], len(operating_names), source_total_rows["源数据"],
    )

    # 撤店门店页：从处理完的门店汇总复制（含手续费列布局），重写为撤店名单。
    if closed_names:
        _create_closed_summary_sheet(wb, closed_names, region_map, target_map=summary_target_map)

    # 7. 保存
    # 老板总览不再输出：总览信息已由门店汇总/排名页覆盖，直接移除该页。
    if "老板总览" in wb.sheetnames:
        wb.remove(wb["老板总览"])
    # 模板没有 bookViews，load 后 wb.views 为空列表：
    # 不补建 BookView 的话 activeTab 不会写入文件，报表打开会停在第一张表。
    if not wb.views:
        wb.views = [BookView()]
    first_sheet = wb.sheetnames[0]
    wb.active = 0
    wb.views[0].activeTab = 0
    for sheet in wb.worksheets:
        sheet.sheet_view.tabSelected = sheet.title == first_sheet

    # 强制打开时重算公式，保证 INDEX/MATCH、SUM 等由数据源自动计算
    try:
        if wb.calculation is None:
            wb.calculation = CalcProperties()
        wb.calculation.calcMode = "auto"
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True
    except Exception:
        pass
    wb.save(output_path)
    wb.close()
    normalize_income_data_bars(output_path)
    _bake_formula_cache(output_path)
    print(f"报表已生成: {output_path}")
    return output_path


def _bake_formula_cache(output_path: str) -> None:
    """用本机 Excel 重算全簿公式并保存，把计算结果缓存进文件。

    openpyxl 写出的公式单元格没有缓存结果值（<v/> 为空）：
    Excel 打开会按 fullCalcOnLoad 自动重算，但 WPS / 在线预览 /
    格式转换对这类单元格常直接显示空白（"没有数据"）。
    本步骤把计算结果写死进文件，任何查看器打开即显示数据。
    Excel 不可用、文件被占用等情况静默跳过，不影响报表生成。
    """
    script = os.path.join(_PROJECT_ROOT, "scripts", "bake_formula_cache.ps1")
    if not os.path.isfile(script):
        return
    import subprocess

    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", script, "-Path", os.path.abspath(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"公式缓存烤入跳过（{exc.__class__.__name__}），WPS 打开如空白请按 Ctrl+Alt+F9 重算")
        return
    if result.returncode == 0:
        print("已用 Excel 重算公式并缓存结果（WPS/预览打开即显示数据）")
    else:
        print("公式缓存烤入跳过（Excel 不可用或文件被占用），不影响报表数据")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="生成老板报表")
    parser.add_argument("date", help="目标日期 YYYY-MM-DD")
    parser.add_argument("--output", "-o", help="输出路径", default=None)
    args = parser.parse_args()
    generate(args.date, args.output)
