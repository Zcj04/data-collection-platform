# -*- coding: utf-8 -*-
"""停止请求必须真实反映底层调用是否已经释放。"""

import time

import requests

from core import db
from core.scheduler import Scheduler
from core.task_manager import TaskManager


def test_task_stop_transitions_from_requested_to_stopped(tmp_path):
    original = db.DB_PATH
    db.DB_PATH = str(tmp_path / "app.db")
    try:
        db.init_db()
        manager = TaskManager()
        task_id = manager.create("fake", "2026-09-01")
        manager.mark_running(task_id)

        assert manager.request_stop(task_id) is True
        requested = manager.get(task_id)
        assert requested["status"] == "stop_requested"
        assert requested["finished_at"] is None
        assert manager.is_active(task_id) is False

        assert manager.finalize_inactive(task_id) == "stopped"
        stopped = manager.get(task_id)
        assert stopped["status"] == "stopped"
        assert stopped["finished_at"] is not None
    finally:
        db.DB_PATH = original


def test_retry_wait_exits_promptly_after_stop_request():
    class Manager:
        def __init__(self):
            self.active_checks = 0
            self.finalized = False

        def mark_running(self, task_id):
            return None

        def is_active(self, task_id):
            self.active_checks += 1
            return self.active_checks < 4

        def increment_retry(self, task_id):
            return None

        def update_step(self, task_id, message):
            return None

        def finalize_inactive(self, task_id):
            self.finalized = True

        def mark_failed(self, task_id, message):
            raise AssertionError("停止请求不应被改写成普通失败")

    class Adapter:
        platform_name = "fake"

        def check_credential(self):
            return True

        def run(self, start_date, end_date, progress_callback=None):
            raise requests.ConnectionError("temporary")

    manager = Manager()
    scheduler = Scheduler(manager, max_retries=2)
    started = time.monotonic()
    committed = scheduler._run_one(Adapter(), "2026-09-01", "2026-09-01", "task")

    assert committed is False
    assert manager.finalized is True
    assert time.monotonic() - started < 0.8
