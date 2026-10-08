# -*- coding: utf-8 -*-
"""采集适配器隔离进程的结果与超时契约。"""

import json
from unittest import mock

import pytest

from core.scheduler import AdapterProcessTimeout, Scheduler


class _Adapter:
    platform_name = "test-platform"


class _CompletedProcess:
    pid = 12345
    returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class _HangingProcess:
    pid = 12346
    returncode = None

    def poll(self):
        return None


class _ActiveTaskManager:
    def __init__(self):
        self.updates = []

    @staticmethod
    def is_active(_task_id):
        return True

    def update_step(self, _task_id, message):
        self.updates.append(message)


def test_isolated_worker_reads_json_result(tmp_path):
    def fake_popen(command, **_kwargs):
        with open(command[-2], "w", encoding="utf-8") as handle:
            json.dump({"status": "success", "data": [{"场地": "A"}]}, handle)
        return _CompletedProcess()

    scheduler = Scheduler(process_isolation=True)
    with mock.patch("core.scheduler.subprocess.Popen", side_effect=fake_popen):
        result = scheduler._run_adapter_isolated(
            _Adapter(), "2026-09-01", "2026-09-01", "task-id", timeouts=1
        )
    assert result == [{"场地": "A"}]


def test_isolated_worker_timeout_is_terminal_and_terminates_process():
    scheduler = Scheduler(process_isolation=True)
    scheduler.task_mgr = _ActiveTaskManager()
    process = _HangingProcess()
    with mock.patch("core.scheduler.subprocess.Popen", return_value=process), \
            mock.patch.object(scheduler, "_terminate_process_tree") as terminate:
        with pytest.raises(AdapterProcessTimeout, match="隔离进程超过"):
            scheduler._run_adapter_isolated(
                _Adapter(), "2026-09-01", "2026-09-01", "task-id", timeouts=0.01
            )
    terminate.assert_any_call(process)
    assert terminate.call_count >= 1


def test_isolated_worker_forwards_progress_messages():
    manager = _ActiveTaskManager()
    scheduler = Scheduler(process_isolation=True, task_mgr=manager)
    progress = []
    scheduler.set_progress_callback(lambda platform, status, message: progress.append(
        (platform, status, message)
    ))

    def fake_popen(command, **_kwargs):
        with open(command[-1], "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"message": "正在下载报表"}, ensure_ascii=False) + "\n")
        with open(command[-2], "w", encoding="utf-8") as handle:
            json.dump({"status": "success", "data": [{"场地": "A"}]}, handle)
        return _CompletedProcess()

    with mock.patch("core.scheduler.subprocess.Popen", side_effect=fake_popen):
        scheduler._run_adapter_isolated(
            _Adapter(), "2026-09-01", "2026-09-01", "task-id", timeouts=1
        )

    assert manager.updates == ["正在下载报表"]
    assert progress == [("test-platform", "progress", "正在下载报表")]
