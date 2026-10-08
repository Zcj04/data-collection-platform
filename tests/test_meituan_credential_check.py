from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from adapters.all_adapters import MeituanAdapter
from adapters.base import CrawlerError


@pytest.fixture
def adapter():
    instance = MeituanAdapter.__new__(MeituanAdapter)
    module = SimpleNamespace(MTGSIGS=["test"], COOKIES=["test"])
    with mock.patch.object(instance, "_inject_creds"), mock.patch(
        "adapters.all_adapters.importlib.import_module", return_value=module
    ):
        yield instance


@pytest.mark.parametrize("error", [requests.ConnectionError("connection blocked"), requests.Timeout("timeout")])
def test_transport_failure_is_not_invalid_credential(adapter, error):
    with mock.patch.object(requests, "post", side_effect=error):
        with pytest.raises(type(error)):
            adapter.check_credential()


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_http_failure_is_not_invalid_credential(adapter, status):
    response = requests.Response()
    response.status_code = status
    with mock.patch.object(requests, "post", return_value=response):
        with pytest.raises(requests.HTTPError):
            adapter.check_credential()


def test_explicit_unauthorized_is_invalid_credential(adapter):
    with mock.patch.object(requests, "post", return_value=SimpleNamespace(status_code=401)):
        assert adapter.check_credential() is False


@pytest.mark.parametrize("payload", [None, [], {}, {"data": None}])
def test_unknown_response_does_not_claim_invalid_credential(adapter, payload):
    response = mock.Mock(status_code=200)
    response.json.return_value = payload
    with mock.patch.object(requests, "post", return_value=response):
        with pytest.raises(CrawlerError, match="未返回有效数据"):
            adapter.check_credential()


def test_non_json_response_reports_validation_failure(adapter):
    response = mock.Mock(status_code=200)
    response.json.side_effect = ValueError("bad JSON")
    with mock.patch.object(requests, "post", return_value=response):
        with pytest.raises(CrawlerError, match="非 JSON"):
            adapter.check_credential()


def test_valid_response_passes(adapter):
    response = mock.Mock(status_code=200)
    response.json.return_value = {"data": {"shopId": 1}}
    with mock.patch.object(requests, "post", return_value=response):
        assert adapter.check_credential() is True
