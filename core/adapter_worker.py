# -*- coding: utf-8 -*-
"""隔离运行单个平台适配器，避免超时线程无法被强制结束。"""

import json
import sys
import threading
from pathlib import Path

from adapters.factory import build_adapters
from utils.redaction import redact_sensitive_text


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 5:
        return 2
    platform, start_date, end_date, result_path, progress_path = args
    output = Path(result_path)
    progress = Path(progress_path)
    progress_lock = threading.Lock()

    def report(message) -> None:
        payload = {"message": redact_sensitive_text(str(message or ""))[:1000]}
        with progress_lock:
            with progress.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()

    try:
        adapter = next(
            (item for item in build_adapters() if item.platform_name == platform),
            None,
        )
        if adapter is None:
            raise ValueError("未知平台：%s" % platform)
        data = adapter.run(start_date, end_date, progress_callback=report)
        payload = {"status": "success", "data": data}
        code = 0
    except Exception as error:
        payload = {"status": "error", "error": redact_sensitive_text(str(error))[:1000]}
        code = 1
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
