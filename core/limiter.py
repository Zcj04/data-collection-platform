# -*- coding: utf-8 -*-
"""请求限流器（各平台最小间隔控制，避免触发反爬风控）"""

import time
import threading
from collections import defaultdict


class RateLimiter:
    """线程安全的请求限流器

    用法：
        limiter = RateLimiter()
        limiter.wait("meituan", min_interval=2)  # 美团每次请求间隔 >= 2秒
    """

    def __init__(self):
        self._last_request = defaultdict(float)
        self._lock = threading.Lock()

    def wait(self, platform: str, min_interval: float = 1.0):
        """等待直到距上次请求至少 min_interval 秒"""
        with self._lock:
            elapsed = time.time() - self._last_request[platform]
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_request[platform] = time.time()

    def reset(self, platform: str = None):
        """重置某平台（或全部）的计时"""
        with self._lock:
            if platform:
                self._last_request.pop(platform, None)
            else:
                self._last_request.clear()


# 全局单例
_limiter = RateLimiter()

def get_limiter() -> RateLimiter:
    return _limiter
