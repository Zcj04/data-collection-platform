# -*- coding: utf-8 -*-
"""生产运行时清单和启动入口的静态契约。"""

from pathlib import Path

import runtime_bootstrap


ROOT = Path(__file__).resolve().parents[1]


def test_production_requirements_are_pinned_and_complete():
    lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert all("==" in line for line in lines)
    assert "python-multipart==0.0.32" in lines


def test_runtime_probe_checks_every_startup_critical_dependency():
    for module in (
        "uvicorn",
        "cryptography",
        "pymysql",
        "playwright.sync_api",
        "python_multipart",
    ):
        assert module in runtime_bootstrap._IMPORT_CHECK


def test_deployment_guide_uses_only_guarded_single_process_entry():
    guide = (ROOT / "docs" / "wiki" / "09-部署与运行.md").read_text(encoding="utf-8")
    assert "python -u app_fastapi.py" in guide
    assert "python -m uvicorn" not in guide


def test_windows_service_runner_uses_guarded_entry_without_multi_worker_flags():
    runner = (ROOT / "scripts" / "run_fastapi_service.ps1").read_text(encoding="utf-8")
    assert '"app_fastapi.py"' in runner
    assert "--workers" not in runner
    assert "--reload" not in runner
    assert "Set-Location -LiteralPath $projectRoot" in runner
    assert "$serviceExitCode = $LASTEXITCODE" in runner
    assert "if ($serviceExitCode -eq 0)" in runner
    assert "Start-Sleep -Seconds 15" in runner


def test_windows_service_stop_targets_only_verified_8010_listener():
    stopper = (ROOT / "scripts" / "stop_fastapi_service.ps1").read_text(encoding="utf-8")
    assert '"WorkBuddy-Data-Collection-8010"' in stopper
    assert 'Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 8010' in stopper
    assert 'CommandLine -notmatch "app_fastapi\\.py"' in stopper
    assert "Stop-Process -Id $processId" in stopper
    assert "Get-Process python" not in stopper
