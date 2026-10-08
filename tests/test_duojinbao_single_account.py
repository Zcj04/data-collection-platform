from types import SimpleNamespace
from unittest.mock import patch

from adapters.all_adapters import DuojinbaoAdapter
from core.monitor_sync import MonitorSyncManager


class FakeCredentialManager:
    values = {
        "账号1": "15210000000",
        "密码1": "primary-password",
        "账号2": "13380000000",
        "密码2": "retired-password-2",
        "账号3": "13530000000",
        "密码3": "retired-password-3",
    }

    def get(self, platform, field):
        assert platform == "duojinbao"
        return self.values.get(field, "")


def test_adapter_injects_only_primary_account():
    adapter = DuojinbaoAdapter.__new__(DuojinbaoAdapter)
    adapter._cred_mgr = FakeCredentialManager()
    crawler = SimpleNamespace(ACCOUNTS=[], main=lambda start, end: (start, end))

    with patch("adapters.all_adapters.importlib.import_module", return_value=crawler):
        result = adapter.run("2026-09-01", "2026-09-01")

    assert result == ("2026-09-01", "2026-09-01")
    assert crawler.ACCOUNTS == [
        {"username": "15210000000", "password": "primary-password"}
    ]


def test_monitor_sync_uses_only_primary_account():
    manager = MonitorSyncManager.__new__(MonitorSyncManager)

    with patch("core.monitor_sync.CredentialManager", return_value=FakeCredentialManager()):
        accounts = manager._accounts()

    assert accounts == [
        {"username": "15210000000", "password": "primary-password"}
    ]
