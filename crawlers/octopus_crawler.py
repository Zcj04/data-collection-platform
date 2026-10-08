# -*- coding: utf-8 -*-
"""
八达通（Octopus）数据采集（原八爪鱼脚本整理）

主入口：main(start_date, end_date, file_path) -> List[Dict]
    输入：起止日期 + CSV文件路径（用户手动更新文件）
    输出：[{"场地": str, "八达通收款":..., "八达通手续费":...}, ...]

流程：读取本地CSV→筛选日期范围→按收银机分组汇总金额→MySQL Octopus列场地映射→计算手续费(1.3%)

特殊：不调用API，纯本地文件读取。用户自行更新CSV文件。无账号密码。手续费=收款×1.3%。

注意：FULL_FIELD_VENUES ("香港B店","香港D店") 输出收款字段，其余场地仅手续费。
"""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
import unicodedata

import pandas as pd
from core.config import get as config_get
from utils.mapping import add_unique_mapping
from utils.mysql_pool import fetch_all_cached


DEFAULT_FILE_PATH = (
    config_get("platforms.octopus.file_path")
    or r"E:\八爪鱼项目配置数据\日报\资源文件夹\八达通\report.csv"
)


DATE_COLUMNS = ("交易日期",)
AMOUNT_COLUMNS = ("金額", "金额")
CASHIER_COLUMNS = ("收銀機", "收银机")

VENUE_KEY = "场地"
RECEIVE_KEY = "八达通收款"
FEE_KEY = "八达通手续费"
FEE_RATE = Decimal("0.013")

FULL_FIELD_VENUES = ("香港B店","香港D店")


def _parse_date(value, field_name):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value or "").strip()
    for date_format in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            pass

    raise ValueError(
        "{}格式错误，应为YYYY-MM-DD，当前值：{}".format(field_name, text)
    )


def _as_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _round_money(value):
    return _as_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _plain_number(value):
    number = _as_decimal(value)
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _normalize_name(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _normalize_key(value):
    return re.sub(r"\s+", "", _normalize_name(value)).casefold()


def _clean_columns(data_frame):
    data_frame.columns = (
        data_frame.columns
        .str.replace("\ufeff", "", regex=False)
        .str.strip()
        .str.strip('"')
        .str.strip("'")
    )


def _find_column(data_frame, candidates, field_name):
    normalized_to_real = {
        _normalize_key(column): column
        for column in data_frame.columns
    }
    for candidate in candidates:
        real_column = normalized_to_real.get(_normalize_key(candidate))
        if real_column is not None:
            return real_column

    raise KeyError(
        "找不到{}列，候选列={}，当前实际列名={}".format(
            field_name,
            list(candidates),
            data_frame.columns.tolist(),
        )
    )


def _read_cashier_totals(file_path, start_date, end_date):
    csv_path = Path(str(file_path).strip())
    if not csv_path.is_file():
        raise FileNotFoundError("找不到八达通CSV文件：{}".format(csv_path))

    data_frame = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig")
    _clean_columns(data_frame)

    date_column = _find_column(data_frame, DATE_COLUMNS, "交易日期")
    amount_column = _find_column(data_frame, AMOUNT_COLUMNS, "金额")
    cashier_column = _find_column(data_frame, CASHIER_COLUMNS, "收银机")

    data_frame[date_column] = pd.to_datetime(
        data_frame[date_column],
        errors="coerce",
    )
    amount_text = (
        data_frame[amount_column]
        .fillna("")
        .astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("HK$", "", regex=False)
        .str.replace("$", "", regex=False)
        .str.strip()
    )
    data_frame[amount_column] = pd.to_numeric(
        amount_text,
        errors="coerce",
    ).fillna(0)
    data_frame[cashier_column] = (
        data_frame[cashier_column]
        .fillna("")
        .map(_normalize_name)
    )

    start_datetime = datetime.combine(start_date, datetime.min.time())
    exclusive_end_datetime = datetime.combine(
        end_date + timedelta(days=1),
        datetime.min.time(),
    )
    filtered = data_frame[
        (data_frame[date_column] >= start_datetime)
        & (data_frame[date_column] < exclusive_end_datetime)
    ]

    grouped = (
        filtered.groupby(cashier_column, dropna=False)[amount_column]
        .sum()
        .sort_values(ascending=False)
    )

    return {
        _normalize_name(cashier_name): _as_decimal(amount)
        for cashier_name, amount in grouped.items()
        if _normalize_name(cashier_name)
    }


def _split_cashier_names(value):
    text = str(value or "")
    parts = re.split(r"[\r\n,，;；]+", text)
    return [_normalize_name(part) for part in parts if _normalize_name(part)]


def _get_cashier_to_venue():
    rows = fetch_all_cached(
        "SELECT venue, Octopus "
        "FROM company_organizational_structure "
        "WHERE Octopus IS NOT NULL "
        "AND TRIM(Octopus) <> '';"
    )

    mapping = {}
    ambiguous = set()
    for venue, cashier_names in rows:
        venue_name = _normalize_name(venue)
        for cashier_name in _split_cashier_names(cashier_names):
            add_unique_mapping(mapping, ambiguous, cashier_name, venue_name, "octopus")
    return mapping


def _is_full_field_venue(venue):
    return _normalize_name(venue) in FULL_FIELD_VENUES


def _build_result_item(venue, amount):
    fee = _round_money(amount * FEE_RATE)
    item = {
        VENUE_KEY: venue,
    }
    if _is_full_field_venue(venue):
        item[RECEIVE_KEY] = _plain_number(amount)
    item[FEE_KEY] = _plain_number(fee)
    return item


def main(start_date, end_date, file_path=DEFAULT_FILE_PATH):
    start_value = _parse_date(start_date, "start_date")
    end_value = _parse_date(end_date, "end_date")
    if start_value > end_value:
        raise ValueError("start_date不能晚于end_date")

    cashier_totals = _read_cashier_totals(
        file_path,
        start_value,
        end_value,
    )
    cashier_to_venue = _get_cashier_to_venue()
    venue_totals = {}
    unmatched_cashiers = []

    for cashier_name, amount in cashier_totals.items():
        venue = cashier_to_venue.get(cashier_name)
        if venue is None:
            if amount != 0:
                unmatched_cashiers.append(cashier_name)
            continue
        venue_totals[venue] = venue_totals.get(venue, Decimal("0")) + amount

    result = []
    for venue, amount in sorted(
        venue_totals.items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        result.append(_build_result_item(venue, amount))

    if unmatched_cashiers:
        print("未匹配到的八达通收银机：")
        for cashier_name in unmatched_cashiers:
            print("- {}".format(cashier_name))

    print(result)
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        start, end = "2026-06-01", "2026-06-20"
    data = main(start, end)
    print(f"\n共返回 {len(data)} 条场地数据")
