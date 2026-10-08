# -*- coding: utf-8 -*-
"""美团各账号必须完成后才能替换整个平台快照。"""

import json
import os
import tempfile
from unittest import mock

import pytest

import core.db as db
from core.scheduler import Scheduler
from core.task_manager import TaskManager
from crawlers import meituan_download as download


def test_account_failure_preserves_previous_complete_snapshot():
    with tempfile.TemporaryDirectory(prefix="workbuddy-meituan-") as temp:
        with mock.patch.object(db, "DB_PATH", os.path.join(temp, "app.db")):
            db.init_db()
            manager = TaskManager()
            for venue in ("A", "B"):
                manager.save_summary("meituan", "2026-08-31", venue, json.dumps({"income": 100}))
            task_id = manager.create("meituan", "2026-08-31", start_date="2026-08-01")

            def partner(sig, cookie, partner_id, *args, **kwargs):
                if partner_id == "B":
                    raise RuntimeError("account B failed")
                return [{"场地": "A", "income": 120}]

            class Adapter:
                platform_name = "meituan"

                def check_credential(self):
                    return True

                def run(self, start, end, progress_callback=None):
                    return download.main(start, end, progress_callback)

            with mock.patch.multiple(download, MTGSIGS=["a", "b"], COOKIES=["a", "b"],
                                     partners_id=["A", "B"], DOWNLOAD_DIR=temp), \
                    mock.patch.object(download, "_process_partner", side_effect=partner):
                with pytest.raises(RuntimeError, match="B|2/2"):
                    Scheduler(manager, max_retries=0)._run_one(Adapter(), "2026-08-01", "2026-08-31", task_id)
            assert manager.get(task_id)["status"] == "failed"
            rows = manager.get_summary("2026-08-31")
            assert {row["venue"] for row in rows} == {"A", "B"}
            assert all(json.loads(row["metrics_json"])["income"] == 100 for row in rows)


@pytest.mark.parametrize("failed_stage", ["shops", "request", "id", "url", "download"])
def test_partner_request_failures_are_not_reported_as_empty_success(failed_stage):
    with mock.patch.object(download, "search_file_by_keyword", return_value=None), \
            mock.patch.object(download, "get_shop_id_list", return_value=[] if failed_stage == "shops" else [1]), \
            mock.patch.object(download, "request_download", return_value=failed_stage != "request"), \
            mock.patch.object(download, "get_latest_download_id", return_value=None if failed_stage == "id" else "report-id"), \
            mock.patch.object(download, "get_file_url", return_value=None if failed_stage == "url" else "https://example.invalid/report"), \
            mock.patch.object(download, "download_excel", return_value=failed_stage != "download"), \
            mock.patch.object(download, "get_dict_list", return_value=[]):
        with pytest.raises(RuntimeError):
            download._process_partner("sig", "cookie", "A", "2026-08-01", "2026-08-31", "unused", 1, 1)


def test_successful_accounts_keep_results_including_verified_empty_report():
    with tempfile.TemporaryDirectory(prefix="workbuddy-meituan-") as temp:
        with mock.patch.multiple(download, MTGSIGS=["a", "b"], COOKIES=["a", "b"],
                                 partners_id=["A", "B"], DOWNLOAD_DIR=temp), \
                mock.patch.object(download, "_process_partner", side_effect=lambda sig, *a: [] if sig == "b" else [{"场地": "A"}]):
            assert download.main("2026-08-01", "2026-08-31") == [{"场地": "A"}]
