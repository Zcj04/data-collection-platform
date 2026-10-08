# -*- coding: utf-8 -*-
"""
美团数据场地匹配（优化版）

优化点（v2）：
- 使用 MySQL 连接池复用连接，避免每次调用都新建/销毁 TCP 连接
- 使用场地映射缓存，避免重复查询 company_organizational_structure 表
- 缓存 TTL 1 小时，多平台采集时只查一次 DB

主入口：main(meituan_data) -> List[Dict]
    输入：meituan_download.main() 的输出（店铺维度字典列表）
    输出：[{"场地": str, "美团收款": float, "美团实收": float, "美团手续费": float}, ...]
"""

import sys
import os

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.mysql_pool import get_venue_map


def main(meituan_data):
    """
    匹配美团数据与数据库中的场地信息（使用连接池+缓存）

    Args:
        meituan_data: 美团数据列表，格式为字典列表（meituan_download.main 的输出）
    Returns:
        场地维度字典列表
    """
    # 从缓存获取美团场地映射（首次自动从 DB 加载）
    match_dict = get_venue_map('meituan')
    if not match_dict:
        print("[美团匹配] 警告：未获取到场地映射数据，请检查 MySQL 连接和 company_organizational_structure 表")

    # 构建 match_tuples（用于数据库有但美团无的场地补 0）
    match_tuples = list(match_dict.items())

    # 获取美团数据中除了店铺名之外的所有键
    if meituan_data:
        keys_to_copy = [key for key in meituan_data[0].keys() if key != '美团店铺名']
    else:
        keys_to_copy = []

    # 存储最终结果
    result = []
    matched_shop_names = set()

    # 处理美团数据，匹配数据库中的场地
    for meituan_item in meituan_data:
        shop_name = meituan_item.get('美团店铺名')

        if shop_name in match_dict:
            new_dict = {'场地': match_dict[shop_name]}
            for key in keys_to_copy:
                new_dict[key] = meituan_item.get(key, 0)
            result.append(new_dict)
            matched_shop_names.add(shop_name)

    # 处理数据库中有但美团数据中没有的场地（填0）
    for col_value, venue in match_tuples:
        if col_value not in matched_shop_names:
            new_dict = {'场地': venue}
            for key in keys_to_copy:
                new_dict[key] = 0
            result.append(new_dict)

    # 收集美团数据中未被匹配到的店铺名
    unmatched_meituan = []
    for meituan_item in meituan_data:
        shop_name = meituan_item.get('美团店铺名')
        if shop_name not in matched_shop_names:
            unmatched_meituan.append(shop_name)

    if unmatched_meituan:
        print("[美团匹配] 未被匹配到的店铺名:")
        for name in unmatched_meituan:
            print(f"  - {name}")

    return result


if __name__ == "__main__":
    from crawlers.meituan_download import main as download_main
    from datetime import datetime
    d = datetime.now().strftime("%Y-%m-%d")
    shop_data = download_main(d, d)
    venue_data = main(shop_data)
    print(f"\n共返回 {len(venue_data)} 条场地数据")
