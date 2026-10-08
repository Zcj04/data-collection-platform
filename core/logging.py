# -*- coding: utf-8 -*-
"""结构化日志模块 — 替代分散的 print()"""

import logging
import os
import traceback
from logging.handlers import RotatingFileHandler
from typing import Optional

from core.config import get as config_get
from utils.redaction import redact_sensitive_text


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_sensitive_text(
                "".join(traceback.format_exception(*record.exc_info)).rstrip()
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_sensitive_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_sensitive_text(record.stack_info)
        return True


_sensitive_filter = SensitiveDataFilter()
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_sensitive_log_redaction() -> None:
    # 过滤 handler 才能覆盖子 logger；urllib3 源头过滤也保护独立爬虫脚本。
    for handler in logging.getLogger().handlers:
        handler.addFilter(_sensitive_filter)
    logging.getLogger("urllib3.connectionpool").addFilter(_sensitive_filter)


def setup_logging(
    level: int = logging.INFO,
    *,
    log_file: Optional[str] = None,
    max_bytes: Optional[int] = None,
    backup_count: Optional[int] = None,
    console: bool = True,
) -> str:
    """初始化全局日志，并幂等挂载带容量上限的 UTF-8 文件 handler。"""
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    if console and not any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, RotatingFileHandler)
        for handler in root.handlers
    ):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    configured_path = log_file or os.environ.get(
        "WORKBUDDY_LOG_FILE",
        str(config_get("logging.file", "data/logs/app_fastapi.log")),
    )
    absolute_path = (
        configured_path
        if os.path.isabs(configured_path)
        else os.path.join(_PROJECT_ROOT, configured_path)
    )
    absolute_path = os.path.abspath(absolute_path)
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
    existing = next(
        (
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and os.path.normcase(handler.baseFilename) == os.path.normcase(absolute_path)
        ),
        None,
    )
    if existing is None:
        file_handler = RotatingFileHandler(
            absolute_path,
            maxBytes=max_bytes if max_bytes is not None else int(
                config_get("logging.max_bytes", 10 * 1024 * 1024)
            ),
            backupCount=backup_count if backup_count is not None else int(
                config_get("logging.backup_count", 10)
            ),
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    logging.captureWarnings(True)
    install_sensitive_log_redaction()
    return absolute_path


def close_log_file(log_file: str) -> None:
    """移除并关闭指定文件 handler；主要用于受控重载和测试清理。"""
    absolute_path = os.path.abspath(log_file)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if (
            isinstance(handler, RotatingFileHandler)
            and os.path.normcase(handler.baseFilename) == os.path.normcase(absolute_path)
        ):
            root.removeHandler(handler)
            handler.close()


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取命名日志器"""
    return logging.getLogger(name or "datapipeline")


# 模块级便捷函数
_default_logger = get_logger()


def info(msg: str, *args) -> None:
    _default_logger.info(msg, *args)


def warning(msg: str, *args) -> None:
    _default_logger.warning(msg, *args)


def error(msg: str, *args) -> None:
    _default_logger.error(msg, *args)


def debug(msg: str, *args) -> None:
    _default_logger.debug(msg, *args)
