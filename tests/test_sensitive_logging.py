# -*- coding: utf-8 -*-
"""凭据不能通过 HTTP 异常、日志或任务步骤泄露。"""

import io
import hashlib
import logging
import os
import tempfile
import traceback
from unittest import mock

import pytest
import requests

import core.db as db
from core.logging import close_log_file, setup_logging
from core.task_manager import TaskManager
from crawlers import leyaoyao_crawler
from utils.http import create_retry_session
from utils.redaction import redact_sensitive_text


URL = "https://example.invalid/login?userName=review-user&password=review-secret&date=2026-08-31"


def _assert_safe(text):
    assert "review-user" not in text
    assert "review-secret" not in text
    assert "date=2026-08-31" in text


def test_child_logger_redacts_arguments_and_exception_traceback(tmp_path):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    log_file = tmp_path / "sensitive.log"
    root = logging.getLogger()
    with mock.patch.object(root, "handlers", [handler]), mock.patch.object(root, "level", logging.INFO):
        try:
            setup_logging(log_file=str(log_file))
            logger = logging.getLogger("urllib3.connectionpool")
            logger.warning("Retrying URL %s", URL)
            try:
                raise RuntimeError(URL)
            except RuntimeError:
                logger.exception("request failed")
        finally:
            close_log_file(str(log_file))
    _assert_safe(output.getvalue())
    assert "Traceback" in output.getvalue()


def test_task_failure_and_progress_are_redacted_before_storage():
    with tempfile.TemporaryDirectory(prefix="workbuddy-redaction-") as temp:
        with mock.patch.object(db, "DB_PATH", os.path.join(temp, "app.db")):
            db.init_db()
            manager = TaskManager()
            task_id = manager.create("leyaoyao", "2026-08-31")
            manager.mark_running(task_id)
            manager.update_step(task_id, URL)
            manager.mark_failed(task_id, URL)
            row = manager.get(task_id)
            _assert_safe(row["step"])
            _assert_safe(row["error_msg"])


def test_http_exception_is_safe_without_changing_retry_type():
    session = mock.Mock()
    session.request.side_effect = requests.exceptions.Timeout(URL)
    with pytest.raises(requests.exceptions.Timeout) as caught:
        leyaoyao_crawler._request_json(session, "POST", "/login")
    _assert_safe("".join(traceback.format_exception(caught.type, caught.value, caught.tb)))


def test_http_status_error_keeps_response_for_retry_classification():
    response = requests.Response()
    response.status_code = 503
    response.url = URL
    session = mock.Mock()
    session.request.return_value = response
    with pytest.raises(requests.exceptions.HTTPError) as caught:
        leyaoyao_crawler._request_json(session, "POST", "/login")
    assert caught.value.response.status_code == 503
    _assert_safe(str(caught.value))


def test_retry_logger_is_redacted_even_with_its_own_handler():
    output = io.StringIO()
    logger = logging.getLogger("urllib3.connectionpool")
    with mock.patch.object(logger, "handlers", [logging.StreamHandler(output)]), \
            mock.patch.object(logger, "propagate", False):
        session = create_retry_session()
        try:
            logger.warning("retrying %s", URL)
        finally:
            session.close()
    _assert_safe(output.getvalue())


def test_redaction_handles_encoded_values_case_and_repeated_application():
    message = "Password=a%26b&USERNAME=some%40mail&token=token-value&income=120"
    safe = redact_sensitive_text(message)
    assert safe == "Password=[REDACTED]&USERNAME=[REDACTED]&token=[REDACTED]&income=120"
    assert redact_sensitive_text(safe) == safe


def test_login_keeps_upstream_credentials_and_ticket_protocol():
    session = mock.Mock()
    session.headers = {}
    session.request.return_value.json.return_value = {"code": 0, "data": {"ticket": "test-ticket"}}
    leyaoyao_crawler.login(session, "review-user", "review-secret")
    kwargs = session.request.call_args.kwargs
    assert kwargs["method"] == "POST"
    assert kwargs["params"]["userName"] == "review-user"
    assert kwargs["params"]["password"] == hashlib.md5(b"review-secret").hexdigest()
    assert session.headers["authorization-bar"] == "test-ticket"
