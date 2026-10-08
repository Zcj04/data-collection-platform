# -*- coding: utf-8 -*-
"""数据模型（dataclass）"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime


@dataclass
class Metric:
    """单个指标"""
    metric: str        # 指标名，如 "美团收款"
    value: Any         # 指标值（int/float）
    unit: str = ""     # 单位，如 "元"/"次"/"积分"


@dataclass
class CrawlerResult:
    """适配器统一返回结构"""
    platform: str                     # 平台名
    date: str                         # 采集日期 YYYY-MM-DD
    metrics: List[Metric]             # 标准化指标列表
    venue: str = ""                   # 场地名
    raw_file: Optional[str] = None    # 原始文件路径（便于排查）
    extra: Dict = field(default_factory=dict)


@dataclass
class Task:
    """任务记录"""
    id: str
    platform: str
    date: str
    status: str = "pending"  # pending/running/success/failed
    step: Optional[str] = None
    progress: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    retry_count: int = 0
    error_msg: Optional[str] = None


@dataclass
class Credential:
    """凭证记录"""
    platform: str
    encrypted_value: str
    status: str = "unknown"  # active/expired/unknown
    last_check_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    updated_at: datetime = field(default_factory=datetime.now)
