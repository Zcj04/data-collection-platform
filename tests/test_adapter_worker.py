# -*- coding: utf-8 -*-
"""隔离 worker 的离线 fixture 协议测试。"""

import json
from unittest import mock

from core import adapter_worker


class _FixtureAdapter:
    platform_name = "fixture"

    def run(self, start_date, end_date, progress_callback=None):
        if progress_callback:
            progress_callback("fixture 已读取 password=fixture-secret")
        return [{"场地": "离线测试店", "日期": start_date, "结束": end_date}]


class _FailingAdapter:
    platform_name = "fixture"

    def run(self, _start_date, _end_date, progress_callback=None):
        raise RuntimeError("上游 password=fixture-secret 连接失败")


def test_worker_writes_result_and_redacted_progress(tmp_path):
    result_path = tmp_path / "result.json"
    progress_path = tmp_path / "progress.jsonl"
    with mock.patch.object(adapter_worker, "build_adapters", return_value=[_FixtureAdapter()]):
        code = adapter_worker.main([
            "fixture", "2026-09-01", "2026-09-02", str(result_path), str(progress_path)
        ])

    assert code == 0
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload == {
        "status": "success",
        "data": [{"场地": "离线测试店", "日期": "2026-09-01", "结束": "2026-09-02"}],
    }
    progress = json.loads(progress_path.read_text(encoding="utf-8").splitlines()[0])
    assert progress["message"] == "fixture 已读取 password=[REDACTED]"


def test_worker_writes_redacted_error_payload(tmp_path):
    result_path = tmp_path / "result.json"
    progress_path = tmp_path / "progress.jsonl"
    with mock.patch.object(adapter_worker, "build_adapters", return_value=[_FailingAdapter()]):
        code = adapter_worker.main([
            "fixture", "2026-09-01", "2026-09-01", str(result_path), str(progress_path)
        ])

    assert code == 1
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert "fixture-secret" not in payload["error"]
    assert "password=[REDACTED]" in payload["error"]


def test_worker_reports_unknown_platform(tmp_path):
    result_path = tmp_path / "result.json"
    progress_path = tmp_path / "progress.jsonl"
    with mock.patch.object(adapter_worker, "build_adapters", return_value=[]):
        code = adapter_worker.main([
            "missing", "2026-09-01", "2026-09-01", str(result_path), str(progress_path)
        ])

    assert code == 1
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload == {"status": "error", "error": "未知平台：missing"}
