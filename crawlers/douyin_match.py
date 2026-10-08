# -*- coding: utf-8 -*-
"""抖音店铺名→场地名匹配（从 MySQL company_organizational_structure 取映射）"""

from utils.mapping import add_unique_mapping
from utils.mysql_pool import fetch_all_cached


def get_mapping_list():
    return list(fetch_all_cached(
        "SELECT venue, douyin_4630 FROM company_organizational_structure WHERE douyin_4630 IS NOT NULL "
        "UNION SELECT venue, douyin_2358 FROM company_organizational_structure WHERE douyin_2358 IS NOT NULL"
    ))


def match(data_list):
    """输入: [{"抖音店铺名":..., "抖音收款":..., "抖音手续费":..., "抖音实收":...}]
       输出: [{"场地":..., "抖音收款":..., "抖音手续费":...}]"""
    mapping = get_mapping_list()
    shop_map = {}
    ambiguous = set()
    for venue, shop_name in mapping:
        add_unique_mapping(shop_map, ambiguous, shop_name, venue, "douyin")

    result = []
    unmatched = []
    for item in data_list:
        shop = item["抖音店铺名"]
        if shop in shop_map:
            result.append({"场地": shop_map[shop], "抖音收款": item["抖音收款"], "抖音手续费": item["抖音手续费"]})
        elif item["抖音收款"] != 0 or item["抖音手续费"] != 0:
            unmatched.append(shop)

    if unmatched:
        print(f"抖音未匹配店铺：{unmatched}")

    return result
