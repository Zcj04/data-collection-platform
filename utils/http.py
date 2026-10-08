# -*- coding: utf-8 -*-
"""统一 HTTP 会话：连接复用、幂等请求有限重试与退避。"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import get as config_get
from core.logging import install_sensitive_log_redaction


def create_retry_session() -> requests.Session:
    """创建可复用会话。

    只对 GET/HEAD/OPTIONS 自动重试，避免登录或写操作因网络抖动被重复提交。
    各请求仍必须显式传入 timeout。
    """
    install_sensitive_log_redaction()
    retries = max(0, int(config_get("http.max_retries", 2)))
    backoff = float(config_get("http.retry_backoff", 0.5))
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
