# -*- coding: utf-8 -*-
"""适配器抽象基类"""

from abc import ABC, abstractmethod
from typing import List, Optional, Callable

from core.models import CrawlerResult


class CrawlerError(RuntimeError):
    """爬虫异常（统一异常类）"""
    def __init__(self, platform: str, detail: str):
        self.platform = platform
        self.detail = detail
        super().__init__(f"[{platform}] {detail}")


class CrawlerAdapter(ABC):
    """所有平台适配器的基类

    每个平台实现一个适配器子类，封装 importlib 加载原脚本 -> 调用 run -> 标准化输出 的全流程。
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """平台标识名，如 'yuntai'"""

    @abstractmethod
    def run(
        self,
        start_date: str,
        end_date: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> list:
        """执行采集，传入日期范围，返回场地维度字典列表

        progress_callback: 可选进度回调，爬虫在关键步骤调用 progress_callback(msg)
        """

    @abstractmethod
    def check_credential(self) -> bool:
        """校验凭证是否有效"""
