# -*- coding: utf-8 -*-
"""
货款数据统计（原八爪鱼脚本整理，本地Excel读取，非平台数据）

主入口：main(file_path) -> List[Dict]
    输入：货款Excel文件路径
    输出：[{"场地": str, "基础货款":...}, ...]（场地维度，1个指标）

流程：读取Excel(店名.1/求和项:金额)→清洗过滤→MySQL payment列场地映射→返回

特殊：非平台数据，本地Excel读取，无账号密码。用户自行更新Excel文件。
"""

import pandas as pd
from datetime import datetime, timedelta

from core.config import get as config_get
from core import payment_store
from utils.mapping import add_unique_mapping
from utils.mysql_pool import fetch_all_cached



DEFAULT_FILE_PATH = (
    config_get("platforms.payment.file_path")
    or r"C:\公司数据\货款表\月货款表\门店货款数据.xlsx"
)

# 平时使用的日货款表路径（月末最后一天才用月货款表）
DEFAULT_FILE_PATH_DAILY = (
    config_get("platforms.payment.file_path_daily")
    or r"C:\公司数据\货款表\日货款表\门店货款数据.xlsx"
)


def resolve_file_path(target_date=None):
    """
    根据目标日期选择货款表路径：
    - 目标日期是当月最后一天 -> 月货款表
    - 其他日期（含未指定） -> 日货款表
    """
    if target_date is None:
        return DEFAULT_FILE_PATH_DAILY
    dt = datetime.strptime(str(target_date)[:10], "%Y-%m-%d")
    next_day = dt + timedelta(days=1)
    if next_day.month != dt.month:
        # 当月最后一天，用月货款表
        return DEFAULT_FILE_PATH
    return DEFAULT_FILE_PATH_DAILY


def get_match_list():
    sql = "SELECT venue,payment FROM company_organizational_structure WHERE payment IS NOT NULL;"
    return list(fetch_all_cached(sql))


def match_payment_lists(list1, list2):
    result = []
    matched_shops = set()
    unmatched_shops = []

    shop_to_site = {}
    ambiguous = set()
    for site_name, shop_name in list2:
        add_unique_mapping(shop_to_site, ambiguous, shop_name, site_name, "payment")

    all_fields = set()
    for item in list1:
        for key in item.keys():
            if key != '货款店铺名':
                all_fields.add(key)
    all_fields = list(all_fields)

    # 后缀别名匹配：日货款表通常使用短后缀 FF/solmo，MySQL 使用完整后缀
    def try_alias_match(shop_name: str) -> str | None:
        """尝试别名匹配，返回匹配到的场地名或 None"""
        # 1. 福建XX -> 福州XX 修正
        if shop_name.startswith('福建'):
            fz_name = '福州' + shop_name[2:]
            if fz_name in shop_to_site:
                return fz_name

        # 2. 深圳福永I商圈假日 -> 福永I商圈假日
        if shop_name == '深圳福永I商圈假日' and '福永I商圈假日' in shop_to_site:
            return '福永I商圈假日'

        # 3. FF后缀替换为 demofinds
        if shop_name.endswith('FF'):
            base = shop_name[:-2]
            for cand in [base + 'demofinds', base + 'figgy', base]:
                if cand in shop_to_site:
                    return cand

        # 4. 广州A商圈 特殊匹配
        if shop_name == '广州广州A商圈' and '广州A店' in shop_to_site:
            return '广州A店'

        # 5. 深H商圈广场 -> 深H商圈爪玩店
        if shop_name == '深圳H广场' and '深圳H店' in shop_to_site:
            return '深圳H店'

        # 6. 深圳I商圈 -> Demo Finds娃娃市集(I店)
        if '深I区' in shop_name and 'I商圈' in shop_name and 'Demo Finds娃娃市集(I店)' in shop_to_site:
            return 'Demo Finds娃娃市集(I店)'

        # 7. 深圳K区域 -> 宝安K区
        if '深圳K区域' in shop_name and '深圳K店' in shop_to_site:
            return '深圳K店'

        return None

    for item in list1:
        shop_name = item['货款店铺名']
        found_site = None
        if shop_name in shop_to_site:
            found_site = shop_to_site[shop_name]
        else:
            # 尝试别名匹配
            alias_cand = try_alias_match(shop_name)
            if alias_cand and alias_cand in shop_to_site:
                found_site = shop_to_site[alias_cand]

        if found_site:
            new_dict = {'场地': found_site}
            for field in all_fields:
                new_dict[field] = item.get(field, 0)
            result.append(new_dict)
            matched_shops.add(shop_name)
        else:
            has_non_zero = False
            for field in all_fields:
                value = item.get(field, 0)
                if isinstance(value, (int, float)) and value != 0:
                    has_non_zero = True
                    break
            if has_non_zero:
                unmatched_shops.append(shop_name)

    return result, unmatched_shops, all_fields


def _pick_col(columns, exact_candidates, keyword):
    """从列名中选择目标列：优先精确匹配，其次按关键词模糊匹配。

    返回原始列名；找不到返回 None。
    """
    col_list = list(columns)
    col_str = [str(c).strip() for c in col_list]
    for cand in exact_candidates:
        if cand in col_str:
            return col_list[col_str.index(cand)]
    # 模糊匹配：优先不含「对账」的列（避免误选 对账金额）
    for i, c in enumerate(col_str):
        if keyword in c and "对账" not in c:
            return col_list[i]
    for i, c in enumerate(col_str):
        if keyword in c:
            return col_list[i]
    return None


def _clean_df(df):
    """从货款Excel提取「店铺名/货款金额」两列并清洗。

    兼容列名变体：优先精确匹配 店名.1/求和项:金额（老板模板透视列），
    找不到再按关键词识别（店名/金额），并剔除空值与合计行。
    返回 [{'货款店铺名','基础货款'}, ...]
    """
    shop_col = _pick_col(df.columns, ["店名.1", "店名"], "店名")
    amt_col = _pick_col(df.columns, ["求和项:金额", "金额"], "金额")
    if shop_col is None or amt_col is None:
        raise ValueError(
            "未识别到店铺列/金额列（需要包含「店名」「金额」的列），"
            f"当前列：{list(df.columns)}"
        )

    df_target = df[[shop_col, amt_col]].copy()
    df_target.columns = ['货款店铺名', '基础货款']

    # 先剔除店铺名为空的原始行
    df_target = df_target[df_target['货款店铺名'].notna()]
    df_target['货款店铺名'] = df_target['货款店铺名'].astype(str).str.strip()
    # 兼容不同 dtype：StringDtype 的空值仍是 NA，object dtype 的空值是 'nan'
    df_target = df_target[
        ~df_target['货款店铺名'].isin(['', 'nan', '<NA>', 'None', '(空白)', '总计', '合计'])
    ]
    # 剔除金额为空的行
    df_target = df_target[df_target['基础货款'].notna()]
    df_target['基础货款'] = df_target['基础货款'].round(2)

    # 兜底：绝不允许空店铺名进入入库环节（防止 NOT NULL 约束报错）
    df_target = df_target[df_target['货款店铺名'].astype(str).str.strip() != '']
    return df_target.to_dict('records')


def _build_result(tenant_list):
    """店铺维度明细 -> 场地维度结果（含 MySQL 场地映射）"""
    match_list = get_match_list()
    matched_data, unmatched_shops, _fields = match_payment_lists(tenant_list, match_list)

    print("\n匹配到的店铺数量:", len(matched_data))
    print("\n未匹配到的店铺（有值且不全为0）:")
    if unmatched_shops:
        for shop in unmatched_shops:
            print(f"- {shop}")
    else:
        print("无未匹配店铺（或所有值全为0）")
    return matched_data


def main(file_path=None, target_date=None):
    if target_date is not None:
        # 优先读取「货款数据」模块导入的数据；未导入则报错（不再读取本地Excel）
        rows = payment_store.get_date_rows(target_date)
        if not rows:
            raise RuntimeError(
                f"货款数据未导入：{target_date} 尚未在「货款数据」页面导入，请先导入再采集"
            )
        print(f"使用「货款数据」模块 {target_date} 导入的数据，共 {len(rows)} 行")
        tenant_list = [{"货款店铺名": r["shop_name"], "基础货款": r["amount"]} for r in rows]
        return _build_result(tenant_list)

    # 兼容：未指定日期时直接读取本地Excel（独立脚本/旧调用）
    if file_path is None:
        file_path = resolve_file_path(None)
    df = pd.read_excel(file_path)

    tenant_list = _clean_df(df)
    print("处理完成，结果如下：")
    print(tenant_list)

    return _build_result(tenant_list)


if __name__ == "__main__":
    import sys
    # 用法: python crawlers/payment_crawler.py [file_path] [target_date]
    file_path = sys.argv[1] if len(sys.argv) >= 2 else None
    target_date = sys.argv[2] if len(sys.argv) >= 3 else None
    data = main(file_path, target_date)
    print(f"\n共返回 {len(data)} 条场地数据")
