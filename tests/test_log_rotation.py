# -*- coding: utf-8 -*-
"""应用日志必须轮转、幂等并在写盘前脱敏。"""

import logging
from logging.handlers import RotatingFileHandler

from core.logging import close_log_file, setup_logging


def test_rotating_log_is_bounded_idempotent_and_redacted(tmp_path):
    log_file = tmp_path / "app.log"
    absolute = setup_logging(
        log_file=str(log_file),
        max_bytes=300,
        backup_count=2,
        console=False,
    )
    setup_logging(
        log_file=str(log_file),
        max_bytes=300,
        backup_count=2,
        console=False,
    )
    root = logging.getLogger()
    matching = [
        handler
        for handler in root.handlers
        if isinstance(handler, RotatingFileHandler)
        and handler.baseFilename == absolute
    ]
    assert len(matching) == 1
    logger = logging.getLogger("rotation-contract")
    for index in range(30):
        logger.warning(
            "rotation line %s password=secret-value token=secret-token padding-padding",
            index,
        )
    matching[0].flush()

    files = list(tmp_path.glob("app.log*"))
    assert 2 <= len(files) <= 3
    combined = "".join(path.read_text(encoding="utf-8") for path in files)
    assert "secret-value" not in combined
    assert "secret-token" not in combined
    assert "[REDACTED]" in combined

    close_log_file(str(log_file))
