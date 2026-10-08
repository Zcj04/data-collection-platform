"""
将图片中的财务营收数据表格转换为 Excel 文件
- 保留双层合并表头
- 应用原表格配色
- 添加数字格式（千位分隔符、百分比）
"""
from openpyxl import Workbook
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side
)
from openpyxl.utils import get_column_letter

OUTPUT = r"C:\Users\youruser\WorkBuddy\数据采集平台\data\2025-2026_营收数据汇总.xlsx"

# ---------- 数据 ----------
TITLE = "2025年~2026年6月营收数据汇总"
SUBTITLE = "明细数据，"

# 列定义：(列号, 子表头或主表头, 主表头, 宽度)
# A 年月份 | B 店名 | C 收入 | D-E 货款 | F-G 租金+管理费+水电 | H-I 工资 | J 报销 | K 经营管理费 | L 合计 | M 利润 | N 毛利率
COL_WIDTHS = {
    "A": 11, "B": 26, "C": 13, "D": 12, "E": 9,
    "F": 14, "G": 9, "H": 12, "I": 9,
    "J": 12, "K": 12, "L": 13, "M": 13, "N": 11,
}

# 数据行  (None 表示留空)
ROWS = [
    ["2025年6月", "宝坻宝京广场",          None,        None,       None,
     None,         None,        37205.56, None, 17050.39, None,    54255.94, -54255.94, None],
    ["2026年1月", "宝坻宝京广场tiggyfinds", 662840.29,   262723.45,  0.3964,
     260728.67,    0.3933,      91776.69,  0.1385, 36911.40, None, 389416.76,  10708.09,  0.6036],
    ["2026年2月", "宝坻宝京广场tiggyfinds", 701330.69,   207286.66,  0.2956,
     187521.74,    0.2674,      98203.23,  0.1400, 18321.46, None, 304046.43, 190005.60,  0.7044],
    ["2026年3月", "宝坻宝京广场tiggyfinds", 555458.78,   244203.99,  0.4396,
     207677.61,    0.3739,      73446.55,  0.1322, 18134.77, None, 299258.93,  11995.86,  0.5604],
    ["2026年4月", "宝坻宝京广场tiggyfinds", 480579.51,   229069.58,  0.4767,
     198876.05,    0.4138,      97056.69,  0.2020, 11104.85, None, 307037.59, -55527.66,  0.5233],
    ["2026年5月", "宝坻宝京广场tiggyfinds", 599939.00,   253369.58,  0.4223,
     245030.84,    0.4084,      82757.77,  0.1379, 37493.80, None, 365282.41, -18712.91,  0.5777],
    ["2026年6月", "深圳宝京广场tiggyfinds", 456844.39,   15905.51,   0.0348,
     579645.72,    1.2686,      79729.62,  0.1745, 23747.23, 40762.85, None,  -201946.74, 0.9652],
]

# ---------- 样式 ----------
HEADER_FILL = PatternFill("solid", fgColor="F4DFD0")   # 主表头米色
SUB_FILL    = PatternFill("solid", fgColor="F4DFD0")   # 子表头同色
TITLE_FILL  = PatternFill("solid", fgColor="FFFFFF")
LABEL_FILL  = PatternFill("solid", fgColor="F4DFD0")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
THIN   = Side(style="thin", color="B89E8C")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

def apply_border(ws, rng):
    for row in ws[rng]:
        for cell in row:
            cell.border = BORDER

# ---------- 创建工作簿 ----------
wb = Workbook()
ws = wb.active
ws.title = "营收数据汇总"

# 列宽
for col, w in COL_WIDTHS.items():
    ws.column_dimensions[col].width = w

# ===== Row 1: 总标题（合并 A1:N1）=====
ws.merge_cells("A1:N1")
ws["A1"] = TITLE
ws["A1"].font = Font(name="微软雅黑", size=13, bold=True)
ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
ws["A1"].fill = TITLE_FILL

# ===== Row 2: "明细数据，" 标签（仅 A2 一个单元格，左上角注脚）=====
ws["A2"] = SUBTITLE
ws["A2"].font = Font(name="微软雅黑", size=11, italic=True, color="6B5B4B")
ws["A2"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
ws["A2"].fill = LABEL_FILL
# 合并 B2:N2 作为空白预留，保持视觉整齐
ws.merge_cells("B2:N2")

# ===== Row 3: 主表头 =====
# A3 年月份 | B3 店名 | C3 收入 | D3 货款 (合并 D3:E3)
# F3 支出项 (合并 F3:L3)  | M3 利润 | N3 毛利率
ws["A3"] = "年月份"
ws["B3"] = "店名"
ws["C3"] = "收入"
ws.merge_cells("D3:E3")
ws["D3"] = "货款"
ws.merge_cells("F3:L3")
ws["F3"] = "支出项"
ws["M3"] = "利润"
ws["N3"] = "毛利率"

# ===== Row 4: 子表头 =====
sub_headers = {
    "A4": "", "B4": "", "C4": "",
    "D4": "金额", "E4": "占比",
    "F4": "金额", "G4": "占比",
    "H4": "金额", "I4": "占比",
    "J4": "报销",
    "K4": "经营管理费",
    "L4": "合计",
    "M4": "", "N4": "",
}
for ref, val in sub_headers.items():
    ws[ref] = val

# 行 3 / 行 4 样式
for ref in ["A3","B3","C3","D3","F3","M3","N3"]:
    c = ws[ref]
    c.fill = HEADER_FILL
    c.font = Font(name="微软雅黑", size=10, bold=True)
    c.alignment = CENTER
for ref, val in sub_headers.items():
    c = ws[ref]
    c.fill = SUB_FILL
    c.font = Font(name="微软雅黑", size=10, bold=True)
    c.alignment = CENTER

# 给所有表头加边框
apply_border(ws, "A3:N4")

# 行高
ws.row_dimensions[1].height = 26
ws.row_dimensions[2].height = 18
ws.row_dimensions[3].height = 22
ws.row_dimensions[4].height = 22

# ===== 数据行 =====
data_start = 5
for ri, row in enumerate(ROWS, start=data_start):
    ws.row_dimensions[ri].height = 20
    for ci, val in enumerate(row, start=1):
        col_letter = get_column_letter(ci)
        cell = ws.cell(row=ri, column=ci, value=val if val is not None else "-")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.font = Font(name="微软雅黑", size=10)
        cell.border = BORDER
        # 数字格式
        if val is not None:
            if ci in (5, 7, 9, 14):          # 占比列 -> 百分比
                cell.number_format = "0.00%"
            elif ci == 1:
                cell.number_format = "@"     # 年月份按文本
            else:                            # 金额列 -> 千位分隔、两位小数
                cell.number_format = "#,##0.00"
        # 为空的位置显示 "-"
        if val is None:
            cell.value = "-"
            cell.alignment = Alignment(horizontal="center", vertical="center")

# 冻结表头（冻结到第 4 行下方、第 2 列右侧）
ws.freeze_panes = "C5"

wb.save(OUTPUT)
print(f"已保存：{OUTPUT}")
