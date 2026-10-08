# -*- coding: utf-8 -*-
"""
美团商户后台日报下载与解析（优化版）

主入口：main(begin_date, end_date, progress_callback=None) -> List[Dict]
    输入：起止日期字符串 YYYY-MM-DD
    输出：[{"美团店铺名": str, "美团收款": float, "美团实收": float, "美团手续费": float}, ...]

优化点（v2）：
- 智能轮询：渐进式退避（5s -> 10s -> 20s -> 30s），非固定 30s
- 并行下载：多组账号(partner)使用 ThreadPoolExecutor 并行处理
- Session 复用：requests.Session 减少 TCP 握手开销
- 超时兜底：可配置的最大轮询时间
- 进度回调：支持 progress_callback(partner_index, step, detail) 上报状态
"""

import requests
import json
import os
import threading
from time import sleep, time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import warnings

from core.config import get as config_get
from utils.redaction import redact_sensitive_text

# 过滤 openpyxl 样式警告
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl.styles.stylesheet")

# ===================== 模块级配置（适配器可覆盖） =====================
# 使用绝对路径，避免依赖进程工作目录（计划任务启动时工作目录为 System32）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.path.join(_PROJECT_ROOT, "data", "downloads", "meituan")

# 轮询策略：[(轮询次数上限, 间隔秒数), ...]
# 前 3 次间隔 5s，之后 10s/20s，最后 30s 长轮询
POLL_SCHEDULE = [
    (3, 5),
    (7, 10),
    (15, 20),
    (75, 30),
]
MAX_POLL_TIMES = sum(n for n, _ in POLL_SCHEDULE)  # 总计 100 次

# 总轮询超时秒数（读配置，默认 30 分钟兜底）
POLL_TIMEOUT_SECONDS = int(
    config_get("platforms.meituan.poll_max_seconds", 1800)
)

# 美团基础 URL 常量
BASE_URL = 'https://e.dianping.com/finance/ajax/downloadManagement'
COMMON_PARAMS = 'yodaReady=h5&csecplatform=4&csecversion=4.2.0'

# 每线程独立 Session，避免多账号并发时共享 Cookie 或连接状态
_session_local = threading.local()


def _get_session():
    """获取或创建当前线程的 requests.Session（复用连接并隔离账号状态）"""
    session = getattr(_session_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            max_retries=2,
        )
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        _session_local.session = session
    return session


def get_shop_id_list(MTGSIG, COOKIE):
    """获取店铺ID列表（使用 Session 复用连接）"""
    session = _get_session()
    url = f"https://e.dianping.com/gateway/merchant/general/shopinfo?{COMMON_PARAMS}&mtgsig={MTGSIG}"

    payload = json.dumps({
        "bizType": "pc-shouye",
        "device": "pc",
        "currentTab": "city",
        "shopIds": os.getenv("MEITUAN_SHOP_IDS", "")
    })
    headers = {
        'Cookie': COOKIE,
        'User-Agent': 'Apifox/1.0.0 (https://apifox.com)',
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'Host': 'e.dianping.com',
        'Connection': 'keep-alive'
    }

    response = session.post(url, headers=headers, data=payload, timeout=15)
    data = response.json()
    shopid_list = []
    if data.get('data', {}).get('shopInfoList'):
        shop_list = data['data']['shopInfoList']
        for shop in shop_list:
            if shop['shopId'] == '0':
                continue
            shopid_list.append(int(shop['shopId']))
    return shopid_list


def request_download(begin_date, end_date, MTGSIG, COOKIE, shopIdList):
    """提交下载请求（使用 Session 复用连接）"""
    session = _get_session()
    url = f"{BASE_URL}/request?{COMMON_PARAMS}&mtgsig={MTGSIG}"
    payload = {
        "beginDate": begin_date,
        "endDate": end_date,
        "productCodeList": [1],
        "shopIdList": shopIdList,
        "downloadFileType": 201
    }
    headers = {
        'Cookie': COOKIE,
        'User-Agent': 'Apifox/1.0.0 (https://apifox.com)',
        'Content-Type': 'application/json'
    }

    response = session.post(url, headers=headers, json=payload, timeout=30)
    data = response.json()
    return '成功' in data.get('message', '')


def get_latest_download_id(begin_date, MTGSIG, COOKIE):
    """获取最新的下载任务ID（使用 Session 复用连接）"""
    session = _get_session()
    today = datetime.now().strftime("%Y-%m-%d")
    url = f"{BASE_URL}/list?pageSize=10&pageNum=1&beginDate={begin_date}&endDate={today}&{COMMON_PARAMS}&mtgsig={MTGSIG}"
    headers = {'Cookie': COOKIE, 'User-Agent': 'Apifox/1.0.0 (https://apifox.com)'}

    response = session.get(url, headers=headers, timeout=15)
    data = response.json()

    if data.get('data'):
        return data['data'][0]['id']
    return None


def get_file_url(download_id, MTGSIG, COOKIE, progress_callback=None):
    """
    获取真实下载链接（智能渐进式轮询）

    优化前：固定 sleep(30)，60 次 = 30 分钟
    优化后：渐进式退避 — 前 3 次 5s，之后 10s，再 20s，最后 30s
    美团后台通常 2-5 分钟生成文件，新策略在前 5 分钟就会密集探测，大幅减少等待时间

    Args:
        progress_callback: 可选回调 (step_detail: str)，用于上报轮询进度
    """
    session = _get_session()
    start_time = time()
    polls = 0

    for batch_count, interval in POLL_SCHEDULE:
        for _ in range(batch_count):
            polls += 1

            # 检查总超时
            if time() - start_time > POLL_TIMEOUT_SECONDS:
                raise RuntimeError(
                    "美团报表生成超时（{}s），请到美团商户后台确认该账号报表状态后重试".format(
                        POLL_TIMEOUT_SECONDS
                    )
                )

            url = f"{BASE_URL}/downloadLink?id={download_id}&{COMMON_PARAMS}&mtgsig={MTGSIG}"
            headers = {'Cookie': COOKIE, 'User-Agent': 'Apifox/1.0.0 (https://apifox.com)'}

            try:
                response = session.get(url, headers=headers, timeout=15)
                data = response.json()
                download_link = data.get('data', {}).get('downloadLink') or ""

                if len(download_link) >= 10:
                    elapsed = int(time() - start_time)
                    print(f"[美团] 下载链接就绪，耗时 {elapsed}s（轮询 {polls} 次）")
                    return download_link

                # 上报进度
                elapsed = int(time() - start_time)
                if progress_callback:
                    progress_callback(f"等待美团生成报表... 已等待 {elapsed}s（第 {polls} 次检查）")
                else:
                    elapsed_min = elapsed // 60
                    elapsed_sec = elapsed % 60
                    print(f"[美团] 报表生成中... 已等待 {elapsed_min}分{elapsed_sec}秒（第 {polls}/{MAX_POLL_TIMES} 次）")

            except Exception as e:
                print(f"[美团] 轮询请求异常: {e}，{interval}s 后重试...")

            sleep(interval)

    raise RuntimeError(
        "美团报表生成超时（{}s），请到美团商户后台确认该账号报表状态后重试".format(
            POLL_TIMEOUT_SECONDS
        )
    )


def download_excel(file_url, filename):
    """下载并保存Excel文件（使用独立 session 避免复用冲突）"""
    headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://e.dianping.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
    }

    response = requests.get(file_url, headers=headers, timeout=120)

    if response.status_code == 200:
        os.makedirs(os.path.dirname(filename), exist_ok=True) if os.path.dirname(filename) else None
        with open(filename, "wb") as f:
            f.write(response.content)
        print(f"[美团] 保存成功：{filename}")
        return True
    else:
        print(f"[美团] 下载失败，状态码：{response.status_code}")
        return False


def get_dict_list(file_name):
    """解析Excel，按店铺分组求和，返回字典列表"""
    df = pd.read_excel(file_name, skiprows=1, engine="openpyxl")

    df['美团店铺名'] = df['账户名称'].astype(str).str.rsplit(':', n=1).str[-1].str.strip()

    total_income_col = '总收入（元）'
    settlement_col = '结算价(总收入-美团点评技术服务费-商家营销费用-消费后退-其他调整)（元）'

    shop_summary = df.groupby('美团店铺名', as_index=False).agg(
        美团收款=pd.NamedAgg(column=total_income_col, aggfunc='sum'),
        美团实收=pd.NamedAgg(column=settlement_col, aggfunc='sum')
    )

    shop_summary['美团手续费'] = shop_summary['美团收款'] - shop_summary['美团实收']

    total_row = pd.DataFrame({
        '美团店铺名': ['全量总合计'],
        '美团收款': [shop_summary['美团收款'].sum()],
        '美团实收': [shop_summary['美团实收'].sum()],
        '美团手续费': [shop_summary['美团手续费'].sum()]
    })

    final_result = pd.concat([shop_summary, total_row], ignore_index=True)
    final_result[['美团收款', '美团实收', '美团手续费']] = final_result[['美团收款', '美团实收', '美团手续费']].round(2)

    result_dict_list = (
        final_result[final_result['美团店铺名'] != '全量总合计']
        .to_dict('records')
    )

    return result_dict_list


def search_file_by_keyword(folder_path, keyword):
    """遍历指定文件夹，返回文件名包含关键字的文件"""
    if not os.path.isdir(folder_path):
        return None
    for file_name in os.listdir(folder_path):
        full_path = os.path.join(folder_path, file_name)
        if os.path.isfile(full_path) and keyword in file_name:
            return full_path
    return None


def _process_partner(MTGSIG, COOKIE, partner_id, begin_date, end_date, folder,
                     partner_index, total_partners, progress_callback=None):
    """
    处理单个合作伙伴账号的下载流程

    请求失败必须抛错；仅成功解析的报表允许返回真实空数据。
    """
    keyword = partner_id + '_团购收益明细_' + begin_date.replace("-", "") + '~' + end_date.replace("-", "") + "_"

    # 1. 检查本地缓存
    cached_file = search_file_by_keyword(folder, keyword)
    if cached_file:
        msg = f"[美团({partner_index}/{total_partners})] 发现本地缓存，跳过下载：{os.path.basename(cached_file)}"
        print(msg)
        if progress_callback:
            progress_callback(msg)
        return get_dict_list(cached_file)

    # 2. 获取店铺ID列表
    msg = f"[美团({partner_index}/{total_partners})] 获取店铺ID列表..."
    print(msg)
    if progress_callback:
        progress_callback(msg)
    shopid_list = get_shop_id_list(MTGSIG, COOKIE)
    if not shopid_list:
        raise RuntimeError(f"[美团({partner_index}/{total_partners})] 未获取到店铺ID")

    # 3. 提交下载请求
    msg = f"[美团({partner_index}/{total_partners})] 提交下载请求（{len(shopid_list)} 个店铺）..."
    print(msg)
    if progress_callback:
        progress_callback(msg)

    if not request_download(begin_date, end_date, MTGSIG, COOKIE, shopid_list):
        raise RuntimeError(f"[美团({partner_index}/{total_partners})] 下载请求提交失败")

    # 4. 获取下载任务ID
    download_id = get_latest_download_id(begin_date, MTGSIG, COOKIE)
    if not download_id:
        raise RuntimeError(f"[美团({partner_index}/{total_partners})] 未找到可用下载任务")

    # 5. 轮询获取下载链接（智能渐进式）
    def partner_progress(detail):
        full_msg = f"[美团({partner_index}/{total_partners})] {detail}"
        if progress_callback:
            progress_callback(full_msg)
        else:
            print(full_msg)

    file_url = get_file_url(download_id, MTGSIG, COOKIE, progress_callback=partner_progress)
    if not file_url:
        raise RuntimeError(f"[美团({partner_index}/{total_partners})] 未获取到下载链接")

    # 6. 下载Excel
    file_name = os.path.join(folder, keyword + download_id + ".xlsx")
    msg = f"[美团({partner_index}/{total_partners})] 下载文件中..."
    print(msg)
    if progress_callback:
        progress_callback(msg)

    if not download_excel(file_url, file_name):
        raise RuntimeError(f"[美团({partner_index}/{total_partners})] 下载文件失败")

    # 7. 解析数据
    return get_dict_list(file_name)


def main(begin_date, end_date, progress_callback=None):
    """
    主入口：并行下载并解析美团日报，返回店铺维度字典列表

    优化（v2）：
    - 多组账号并行处理（ThreadPoolExecutor），2 个 partner 同时跑
    - 智能渐进式轮询替代固定 30s 等待
    - requests.Session 连接池复用

    Args:
        begin_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
        progress_callback: 可选回调 (msg: str)，用于上报进度
    Returns:
        店铺维度字典列表
    """
    folder = DOWNLOAD_DIR
    os.makedirs(folder, exist_ok=True)

    # 确保目录存在
    result_dict_list = []

    # 凭证由适配器在运行前注入模块属性（见 adapters/all_adapters.py MeituanAdapter._inject_creds）
    mtgsigs = MTGSIGS
    cookies = COOKIES
    pids = partners_id

    total = len(mtgsigs)
    if total == 0:
        raise RuntimeError(
            "[美团] 未配置凭证，请在凭证管理页填写 Cookie/mtgsig/partner_id"
        )

    # 单账号：直接串行处理
    if total == 1:
        result = _process_partner(
            mtgsigs[0], cookies[0], pids[0],
            begin_date, end_date, folder,
            1, 1, progress_callback
        )
        result_dict_list.extend(result)
        return result_dict_list

    # 多账号：并行处理
    print(f"[美团] 启动并行下载，{total} 组账号同时处理...")
    if progress_callback:
        progress_callback(f"美团并行下载启动，{total} 组账号")

    failures = []
    with ThreadPoolExecutor(max_workers=min(total, 4)) as executor:
        futures = {}
        for i, (mtgsig, cookie, pid) in enumerate(zip(mtgsigs, cookies, pids), 1):
            future = executor.submit(
                _process_partner,
                mtgsig, cookie, pid,
                begin_date, end_date, folder,
                i, total, progress_callback
            )
            futures[future] = i

        for future in as_completed(futures):
            idx = futures[future]
            try:
                data = future.result(timeout=POLL_TIMEOUT_SECONDS + 300)  # 额外 5 分钟下载时间
                result_dict_list.extend(data or [])
                print(f"[美团] 账号 {idx}/{total} 完成，获取 {len(data or [])} 条数据")
                if progress_callback:
                    progress_callback(f"美团账号 {idx}/{total} 完成")
            except Exception as e:
                error = redact_sensitive_text(str(e))
                failures.append(f"账号 {idx}/{total}: {error}")
                print(f"[美团] 账号 {idx}/{total} 异常: {error}")
                if progress_callback:
                    progress_callback(f"美团账号 {idx}/{total} 失败: {error}")

    if failures:
        raise RuntimeError("[美团] 账号采集不完整，保留原有快照：" + "；".join(failures))
    print(f"[美团] 全部完成，共 {len(result_dict_list)} 条店铺数据")
    return result_dict_list


# ===================== 模块级凭证（仅占位，禁止硬编码） =====================
# 真实凭证由适配器从凭证管理页加密存储中读取后注入（见 adapters/all_adapters.py）
MTGSIGS = []
COOKIES = []
partners_id = []


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        begin, end = sys.argv[1], sys.argv[2]
    else:
        d = datetime.now().strftime("%Y-%m-%d")
        begin, end = d, d
    data = main(begin, end)
    print(f"\n共返回 {len(data)} 条店铺数据")
