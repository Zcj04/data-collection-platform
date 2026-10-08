import shutil
import subprocess
from pathlib import Path

import pytest


def test_template_javascript_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript regressions require Node.js")
    result = subprocess.run([node, str(Path(__file__).with_name("dashboard_regressions.cjs"))], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
