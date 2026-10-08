# -*- coding: utf-8 -*-
"""
汇总报表生成（原八爪鱼脚本整理，所有平台采集完成后统一计算）

主入口：main(data_list) -> List[List]
    输入：所有平台输出数据列表（各平台 main() 返回的场地维度字典列表的集合）
    输出：2D数组 [表头行, ...数据行...] 按 table_sequence_config_v2 列序

流程：
  MySQL table_sequence_config_v2 读列定义 + company_organizational_structure/principal 读场地负责
  → 构建基础DataFrame → 各平台数据按场地填充 → 计算派生指标(收入汇总/积分货款/总货款/现金/投币/出币/出货/存货)
  → 计算财务比率(存货比率/币值/出货率/货款比%/扣除手续费后) → 加合计行 → 按列序输出

依赖：MySQL table_sequence_config_v2 表（列定义+排序）、company_organizational_structure（场地）、principal（负责人）
"""

import ast
import json
import re
import unicodedata
from collections.abc import Mapping

import numpy as np
import pandas as pd
from core.logging import get_logger
from utils.mysql_pool import fetch_all

logger = get_logger("report_summary")

# 香港门店已撤店，当前经营界面和报表暂停展示；
# 历史采集数据与平台适配器保留，恢复时只需调整此处口径。
PAUSED_VENUE_KEYWORDS = ("香港",)


def is_current_operating_venue(venue) -> bool:
    """当前经营视图是否展示该门店。"""
    venue_name = str(venue or "").strip()
    return bool(venue_name) and not any(
        keyword in venue_name for keyword in PAUSED_VENUE_KEYWORDS
    )


def _load_base_dataframe():
    column_definitions = fetch_all(
        """
        SELECT id, system_tag, column_alias
        FROM table_sequence_config_v2
        ORDER BY id ASC
        """
    )
    base_rows = fetch_all(
        """
        SELECT
            p.person_in_charge,
            cos.venue
        FROM company_organizational_structure cos
        INNER JOIN principal p
            ON cos.person_in_charge_id = p.person_in_charge_id
        ORDER BY
            p.person_in_charge_id ASC,
            cos.id ASC
        """
    )

    columns = [
        item[2]
        for item in sorted(
            column_definitions,
            key=lambda item: item[0],
        )
    ]

    rows = []
    for index, (person, venue) in enumerate(
        base_rows,
        start=1,
    ):
        row = [index, person, venue]
        row.extend([np.nan] * max(0, len(columns) - 3))
        rows.append(row[:len(columns)])

    return pd.DataFrame(rows, columns=columns), columns


def _get_base_df():
    """读取最新的门店基础数据，确保新增/撤店状态无需重启即可生效。"""
    return _load_base_dataframe()


def get_operating_venues():
    """返回数据看板负责人标记为在营的门店；无法读取时返回 None。"""
    try:
        dataframe, _ = _get_base_df()
    except Exception:
        logger.exception("读取数据看板门店负责人失败")
        return None

    column_lookup = {
        _normalize_name(column): column
        for column in dataframe.columns
    }
    venue_column = column_lookup.get(_normalize_name("场地"))
    owner_column = column_lookup.get(_normalize_name("负责人"))
    if not venue_column or not owner_column:
        logger.warning("数据看板基础数据缺少场地或负责人列")
        return None

    operating_venues = set()
    for venue, owner in dataframe[[venue_column, owner_column]].itertuples(index=False):
        venue_name = str(venue or "").strip()
        owner_name = str(owner or "").strip()
        if (
            is_current_operating_venue(venue_name)
            and owner_name
            and "撤店" not in owner_name
        ):
            operating_venues.add(venue_name)
    return operating_venues


def _normalize_name(value):
    text = unicodedata.normalize(
        "NFKC",
        str(value or ""),
    )
    return re.sub(r"\s+", "", text).casefold()


def _parse_text_value(value):
    text = str(value or "").strip()
    if not text:
        return None

    candidates = [text]

    for opening, closing in (("[", "]"), ("{", "}")):
        start = text.find(opening)
        end = text.rfind(closing)
        if start >= 0 and end > start:
            candidates.append(text[start:end + 1])

    checked = set()
    for candidate in candidates:
        if candidate in checked:
            continue
        checked.add(candidate)

        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            pass

        try:
            return ast.literal_eval(candidate)
        except (ValueError, SyntaxError, TypeError):
            pass

    return None


def _extract_records(value, depth=0):
    if depth > 20 or value is None:
        return []

    if isinstance(value, pd.DataFrame):
        return value.to_dict("records")

    if isinstance(value, str):
        parsed = _parse_text_value(value)
        if parsed is None or parsed == value:
            return []
        return _extract_records(parsed, depth + 1)

    if isinstance(value, Mapping):
        normalized_keys = {
            _normalize_name(key)
            for key in value
        }
        if _normalize_name("场地") in normalized_keys:
            return [dict(value)]

        records = []
        for nested_value in value.values():
            records.extend(
                _extract_records(
                    nested_value,
                    depth + 1,
                )
            )
        return records

    if isinstance(value, (list, tuple, set)):
        records = []
        for item in value:
            records.extend(
                _extract_records(
                    item,
                    depth + 1,
                )
            )
        return records

    return []


def _numeric_series(dataframe, column):
    if column not in dataframe.columns:
        return pd.Series(
            0.0,
            index=dataframe.index,
            dtype="float64",
        )
    return pd.to_numeric(
        dataframe[column],
        errors="coerce",
    ).fillna(0)


def _sum_columns(dataframe, columns):
    result = pd.Series(
        0.0,
        index=dataframe.index,
        dtype="float64",
    )
    for column in columns:
        result = result.add(
            _numeric_series(dataframe, column),
            fill_value=0,
        )
    return result


def _safe_divide(numerator, denominator):
    return pd.Series(
        np.where(
            denominator == 0,
            np.nan,
            numerator / denominator,
        ),
        index=numerator.index,
    )


def _merge_input_records(dataframe, data_list):
    result = dataframe.copy()
    records = _extract_records(data_list)

    column_lookup = {
        _normalize_name(column): column
        for column in result.columns
    }
    venue_column = column_lookup.get(
        _normalize_name("场地"),
        "场地",
    )

    venue_lookup = {}
    if venue_column in result.columns:
        for value in result[venue_column].dropna().unique():
            venue_lookup[_normalize_name(value)] = value

    matched_count = 0
    unmatched_venues = []
    ignored_columns = set()

    for record in records:
        record_lookup = {
            _normalize_name(key): (key, value)
            for key, value in record.items()
        }
        venue_item = record_lookup.get(
            _normalize_name("场地")
        )
        if venue_item is None:
            continue

        incoming_venue = venue_item[1]
        actual_venue = venue_lookup.get(
            _normalize_name(incoming_venue)
        )
        if actual_venue is None:
            unmatched_venues.append(str(incoming_venue))
            continue

        mask = (
            result[venue_column].map(_normalize_name)
            == _normalize_name(actual_venue)
        )

        updated_columns = []
        for incoming_column, value in record.items():
            normalized_column = _normalize_name(
                incoming_column
            )
            if normalized_column == _normalize_name("场地"):
                continue

            actual_column = column_lookup.get(
                normalized_column
            )
            if actual_column is None:
                ignored_columns.add(str(incoming_column))
                continue

            result.loc[mask, actual_column] = value
            updated_columns.append(actual_column)

        matched_count += 1

        if any(
            _normalize_name(column).startswith("kpay")
            for column in updated_columns
        ):
            print(
                "KPay匹配成功：场地={}，更新字段={}".format(
                    actual_venue,
                    updated_columns,
                )
            )

    print(
        "计算字段输入：解析到{}条，成功匹配{}条".format(
            len(records),
            matched_count,
        )
    )
    if unmatched_venues:
        print(
            "未匹配场地：{}".format(
                sorted(set(unmatched_venues))
            )
        )
    if ignored_columns:
        print(
            "数据库未配置字段：{}".format(
                sorted(ignored_columns)
            )
        )

    return result


INCOME_COLUMNS = [
    "芸苔非团购",
    "芸苔远程取币",
    "油菜花现金",
    "油菜花微信",
    "油菜花支付宝",
    "油菜花盈客宝",
    "抖音收款",
    "美团收款",
    "乐摇摇非现金",
    "乐摇摇现金",
    "多金宝现金",
    "多金宝非现金",
    "StarThing非现金",
    "StarThing现金",
    "鲸舰非现金",
    "鲸舰现金",
    "其他收入",
    "汇联现金",
    "汇联非现金",
    "Kpay收款",
    "八达通收款",
    "兑币机收款",
]

# 平台侧数据修正：
# 香港部分门店的鲸舰后台把 KPay 收款合并计入了"鲸舰现金"字段，
# 而 KPay 又由独立 kpay 平台采集，若两处都计入会造成重复统计。
# 依据 8/1-8/12 数据验证：以下门店 Kpay收款 < 鲸舰现金，判定 KPay 已被包含，报表层扣减修正。
WHALE_CASH_INCLUDES_KPAY_VENUES = [
    "香港H店",
    "香港E店",
]


def kpay_of_metrics(metrics: Mapping) -> float:
    """读取单行指标中的「Kpay收款」数值。

    供下游模块（AI分析师）读取长表 daily_summary 时，
    按白名单门店扣减 KPay，口径与报表层 _correct_whale_cash_includes_kpay 保持一致。
    """
    try:
        return float(metrics.get("Kpay收款", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def has_jingjian_cash(metrics: Mapping) -> bool:
    """单行指标中是否存在「鲸舰现金」。

    长表场景中 KPay 位于独立 platform 行，而鲸舰现金（已含 KPay）位于 jingjian 行，
    该函数用于定位承载鲸舰现金的行，以便在其上扣减本场地/日期的 KPay。
    """
    try:
        return float(metrics.get("鲸舰现金", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _correct_whale_cash_includes_kpay(dataframe):
    """报表层修正：对鲸舰现金已含 KPay 的门店，从鲸舰现金中扣减 Kpay收款，避免重复统计"""
    venue_col = "场地"
    jj_cash_col = "鲸舰现金"
    kpay_col = "Kpay收款"
    if (
        venue_col not in dataframe.columns
        or jj_cash_col not in dataframe.columns
        or kpay_col not in dataframe.columns
    ):
        return dataframe

    mask = dataframe[venue_col].isin(WHALE_CASH_INCLUDES_KPAY_VENUES)
    if not mask.any():
        return dataframe

    corrected = (
        _numeric_series(dataframe, jj_cash_col)
        - _numeric_series(dataframe, kpay_col)
    )
    dataframe.loc[mask, jj_cash_col] = corrected[mask]
    logger.info(
        "报表层修正：鲸舰现金已含 KPay 的门店(%s)已扣减 Kpay收款",
        ",".join(WHALE_CASH_INCLUDES_KPAY_VENUES),
    )
    return dataframe


def main(data_list, active_venues=None):
    base_df, output_columns = _get_base_df()
    if active_venues is not None:
        active_lookup = {_normalize_name(value) for value in active_venues}
        venue_column = next(
            (column for column in base_df.columns if _normalize_name(column) == _normalize_name("场地")),
            None,
        )
        if venue_column:
            existing_lookup = {
                _normalize_name(value)
                for value in base_df[venue_column].dropna().unique()
            }
            missing_venues = [
                str(value).strip()
                for value in active_venues
                if str(value).strip()
                and _normalize_name(value) not in existing_lookup
            ]
            if missing_venues:
                missing_rows = []
                owner_column = next(
                    (
                        column for column in base_df.columns
                        if _normalize_name(column) == _normalize_name("负责人")
                    ),
                    None,
                )
                for venue in sorted(set(missing_venues)):
                    row = {column: np.nan for column in base_df.columns}
                    row[venue_column] = venue
                    if owner_column:
                        row[owner_column] = ""
                    missing_rows.append(row)
                base_df = pd.concat(
                    [base_df, pd.DataFrame(missing_rows)],
                    ignore_index=True,
                )
            base_df = base_df[
                base_df[venue_column].map(_normalize_name).isin(active_lookup)
            ].reset_index(drop=True)
            if "序号" in base_df.columns:
                base_df["序号"] = range(1, len(base_df) + 1)
    current_df = _merge_input_records(
        base_df,
        data_list,
    )

    # 报表层修正：香港部分门店鲸舰现金已含 KPay，扣减避免重复统计（须在收入汇总/现金计算之前）
    current_df = _correct_whale_cash_includes_kpay(current_df)

    current_df["收入汇总"] = _sum_columns(
        current_df,
        INCOME_COLUMNS,
    )

    current_df["StarThing积分货款"] = (
        _numeric_series(current_df, "StarThing积分增加")
        - _numeric_series(current_df, "StarThing积分减少")
    )
    current_df["鲸舰积分货款"] = (
        _numeric_series(current_df, "鲸舰积分增加")
        - _numeric_series(current_df, "鲸舰积分减少")
    )
    current_df["新系统积分货款"] = (
        _numeric_series(current_df, "新系统积分增加")
        - _numeric_series(current_df, "新系统积分减少")
    ) * 1.5
    current_df["芸苔积分货款"] = (
        _numeric_series(current_df, "芸苔积分增加")
        - _numeric_series(current_df, "芸苔积分减少")
    ) * 1.5

    current_df["总货款"] = _sum_columns(
        current_df,
        [
            "基础货款",
            "StarThing积分货款",
            "鲸舰积分货款",
            "新系统积分货款",
            "芸苔积分货款",
        ],
    )

    cash_columns = [
        column
        for column in current_df.columns
        if (
            "现金" in column
            and column != "现金"
            and "非现金" not in column
        )
    ]

    extra_cash_columns = [
        "兑币机收款",
    ]

    for column in extra_cash_columns:
        if column not in cash_columns:
            cash_columns.append(column)
        current_df["现金"] = _sum_columns(
            current_df,
            cash_columns,
        )

    coin_in_columns = [
        column
        for column in current_df.columns
        if "投币" in column and column != "投币合计"
    ]
    current_df["投币合计"] = _sum_columns(
        current_df,
        coin_in_columns,
    )

    coin_out_columns = [
        column
        for column in current_df.columns
        if "出币" in column and column != "出币合计"
    ]
    current_df["出币合计"] = _sum_columns(
        current_df,
        coin_out_columns,
    )

    goods_out_columns = [
        column
        for column in current_df.columns
        if "出货" in column and column != "出货合计"
    ]
    current_df["出货合计"] = _sum_columns(
        current_df,
        goods_out_columns,
    )

    stock_columns = [
        column
        for column in current_df.columns
        if "积分增加" in column
    ]
    current_df["存货合计"] = _sum_columns(
        current_df,
        stock_columns,
    )

    sum_row = {}
    for column in current_df.columns:
        sum_row[column] = _numeric_series(
            current_df,
            column,
        ).sum()

    if "序号" in current_df.columns:
        sum_row["序号"] = ""
    if "负责人" in current_df.columns:
        sum_row["负责人"] = ""
    if "场地" in current_df.columns:
        sum_row["场地"] = "合计"
    if "货款比%" in current_df.columns:
        sum_row["货款比%"] = ""

    current_df = pd.concat(
        [
            current_df,
            pd.DataFrame([sum_row]),
        ],
        ignore_index=True,
    )

    income_total = _numeric_series(
        current_df,
        "收入汇总",
    )
    payment_total = _numeric_series(
        current_df,
        "总货款",
    )
    coin_in_total = _numeric_series(
        current_df,
        "投币合计",
    )
    goods_out_total = _numeric_series(
        current_df,
        "出货合计",
    )
    stock_total = _numeric_series(
        current_df,
        "存货合计",
    )

    current_df["存货比率（存货与出货比）"] = (
        _safe_divide(stock_total, goods_out_total)
    )
    current_df["币值（收入与投币比）"] = (
        _safe_divide(income_total, coin_in_total)
    )
    current_df["出货率（投币与出货比）"] = (
        _safe_divide(coin_in_total, goods_out_total)
    )
    current_df["货款比%"] = _safe_divide(
        payment_total,
        income_total,
    )

    current_df["扣除手续费后"] = (
        income_total
        - _numeric_series(current_df, "抖音手续费")
        - _numeric_series(current_df, "芸苔手续费")
        - _numeric_series(current_df, "美团手续费")
        - _numeric_series(current_df, "乐摇摇手续费")
        - _numeric_series(current_df, "多金宝手续费")
        - _numeric_series(current_df, "汇联手续费")
        - _numeric_series(current_df, "Kpay手续费")
        - _numeric_series(current_df, "StarThing手续费")
        - _numeric_series(current_df, "八达通手续费")
    )

    output_df = current_df.reindex(
        columns=output_columns,
    )
    rows = []
    for row in output_df.itertuples(
        index=False,
        name=None,
    ):
        rows.append(
            [
                ""
                if pd.isna(value)
                else value
                for value in row
            ]
        )

    result = [output_columns] + rows
    logger.debug("汇总结果：%s", result)
    return result


if __name__ == "__main__":
    # 测试：传入空数据（只有基础框架含合计行）
    result = main([])
    print(f"\n共 {len(result) - 1} 行数据")
