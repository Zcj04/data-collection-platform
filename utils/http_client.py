# -*- coding: utf-8 -*-
"""
HTTP 客户端工具（连接池复用 + 自动重试）

优化点：
- requests.Session 连接池复用（减少 TCP 三次握手和 TLS 握手）
- 自动重试 + 指数退避
- 统一超时控制
- 线程安全

用法：
    from utils.http_client import get_session

    session = get_session()
    resp = session.get("https://api.example.com/data", timeout=15)
    data = resp.json()
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===================== 默认配置 =====================
DEFAULT_TIMEOUT = 30      # 默认请求超时（秒）
CONNECT_TIMEOUT = 10      # TCP 连接超时
POOL_CONNECTIONS = 10     # 连接池基大小
POOL_MAXSIZE = 20         # 连接池最大大小
MAX_RETRIES = 2           # 自动重试次数
BACKOFF_FACTOR = 0.5      # 重试退避因子（0.5s, 1s, 2s...）


def create_session(
    pool_connections: int = POOL_CONNECTIONS,
    pool_maxsize: int = POOL_MAXSIZE,
    max_retries: int = MAX_RETRIES,
    backoff_factor: float = BACKOFF_FACTOR,
) -> requests.Session:
    """
    创建带连接池和自动重试的 requests.Session

    Args:
        pool_connections: 连接池基连接数
        pool_maxsize: 连接池最大连接数
        max_retries: 最大自动重试次数
        backoff_factor: 退避因子
    Returns:
        配置好的 requests.Session 实例
    """
    session = requests.Session()

    # 配置重试策略
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PUT", "DELETE"],
    )

    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=retry_strategy,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # 设置默认请求头
    session.headers.update({
        'Accept-Encoding': 'gzip, deflate',
        'Accept': 'application/json, text/plain, */*',
        'Connection': 'keep-alive',
    })

    return session


def get_session() -> requests.Session:
    """获取全局共享 Session 实例（线程安全，连接池复用）"""
    global _global_session
    if _global_session is None:
        _global_session = create_session()
    return _global_session


_global_session = None


def request_with_timeout(method: str, url: str, timeout: float = DEFAULT_TIMEOUT,
                         connect_timeout: float = CONNECT_TIMEOUT, **kwargs):
    """
    发送 HTTP 请求（带超时控制）

    Args:
        method: HTTP 方法 (get/post/put/delete)
        url: 请求 URL
        timeout: 总超时秒数（响应读取）
        connect_timeout: 连接超时秒数
        **kwargs: 传递给 requests.Session.request 的额外参数
    Returns:
        requests.Response
    """
    session = get_session()
    return session.request(
        method=method,
        url=url,
        timeout=(connect_timeout, timeout),
        **kwargs
    )
