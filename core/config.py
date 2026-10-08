# -*- coding: utf-8 -*-
"""全局配置加载器

统一管理 config.yaml，提供类型安全的配置访问。
所有模块从此获取配置，禁止在代码中硬编码敏感信息。
"""

import os
from typing import Any, Dict, Optional

import yaml

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config.yaml",
)

_config: Dict[str, Any] = {}


def _load() -> Dict[str, Any]:
    global _config
    if _config:
        return _config
    # 加载项目根目录 .env（若存在），使环境变量可覆盖 config.yaml
    _load_dotenv_file()
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _config = yaml.safe_load(f) or {}
    except Exception:
        _config = {}
    return _config


def _load_dotenv_file() -> None:
    """加载项目根目录 .env；优先用 python-dotenv，缺失时手动解析兜底"""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".env",
    )
    if not os.path.exists(env_path):
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except Exception:
        pass

    # 兜底：手动解析（不覆盖已有环境变量）
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def load_env() -> None:
    """加载项目根目录 .env（幂等）；供依赖环境变量的模块在读取前显式调用"""
    _load_dotenv_file()


def get(key: str, default: Any = None) -> Any:
    """获取配置项（点号分隔路径），如 config.get('scheduler.single_task_timeout')"""
    cfg = _load()
    parts = key.split(".")
    node: Any = cfg
    for part in parts:
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return default
    return node if node is not None else default


def get_mysql_config() -> Dict[str, Any]:
    """获取 MySQL 连接配置（用于 pymysql.connect）"""
    cfg = _load()
    mysql = cfg.get("mysql", {})
    return {
        "host": os.environ.get("MYSQL_HOST", mysql.get("host", "localhost")),
        "port": int(os.environ.get("MYSQL_PORT", mysql.get("port", 3306))),
        "user": os.environ.get("MYSQL_USER", mysql.get("user", "root")),
        "password": os.environ.get(
            "MYSQL_PASSWORD",
            mysql.get("password", ""),
        ),
        "database": os.environ.get(
            "MYSQL_DATABASE",
            mysql.get("database", ""),
        ),
        "charset": mysql.get("charset", "utf8mb4"),
        "connect_timeout": mysql.get("connect_timeout", 10),
        "read_timeout": mysql.get("read_timeout", 30),
        "write_timeout": mysql.get("write_timeout", 30),
    }


def get_scheduler_config() -> Dict[str, Any]:
    cfg = _load()
    s = cfg.get("scheduler", {})
    return {
        "max_workers": s.get("max_workers", 12),
        "single_task_timeout": s.get("single_task_timeout", 600),
        "max_retries": s.get("max_retries", 3),
    }


def get_platform_config(platform: str) -> Dict[str, Any]:
    cfg = _load()
    return cfg.get("platforms", {}).get(platform, {})


def reload() -> None:
    """强制重新加载配置"""
    global _config
    _config = {}
    _load()
