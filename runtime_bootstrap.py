"""Select a WorkBuddy Python runtime with the dependencies this service needs."""

import os
import subprocess
import sys
from typing import Iterable, List


_RUNTIME_READY = "WORKBENCH_RUNTIME_READY"
_IMPORT_CHECK = (
    "import numpy,pandas,fastapi,yaml,openai,openpyxl,requests,uvicorn,"
    "cryptography,pymysql,playwright.sync_api,python_multipart; numpy.rec"
)


def _runtime_is_usable(executable: str) -> bool:
    """Return whether an interpreter can import the service's core packages."""
    try:
        result = subprocess.run(
            [executable, "-c", _IMPORT_CHECK],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _candidate_paths() -> Iterable[str]:
    """Yield stable and versioned WorkBuddy interpreters, newest first."""
    root = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "python")
    candidates: List[str] = [
        os.path.join(root, "envs", "default", "Scripts", "python.exe"),
        os.path.join(root, "envs", "default", "python.exe"),
    ]
    versions = os.path.join(root, "versions")
    try:
        version_names = sorted(os.listdir(versions), reverse=True)
    except OSError:
        version_names = []
    for version_name in version_names:
        version_root = os.path.join(versions, version_name)
        candidates.extend(
            (
                os.path.join(version_root, "python.exe"),
                os.path.join(version_root, "Scripts", "python.exe"),
            )
        )
    candidates.append(sys.executable)

    seen = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen or not os.path.isfile(candidate):
            continue
        seen.add(normalized)
        yield candidate


def ensure_supported_runtime() -> None:
    """Re-exec the service with a usable interpreter when an update broke one."""
    if os.environ.get(_RUNTIME_READY) == "1":
        return
    current = os.path.abspath(sys.executable)
    if _runtime_is_usable(current):
        return

    for candidate in _candidate_paths():
        if os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(current):
            continue
        if _runtime_is_usable(candidate):
            environment = os.environ.copy()
            environment[_RUNTIME_READY] = "1"
            sys.stderr.write(f"[workbench] switching Python runtime to {candidate}\n")
            sys.stderr.flush()
            os.execve(candidate, [candidate, *sys.argv], environment)
            return

    raise RuntimeError(
        "No usable WorkBuddy Python runtime found; "
        "numpy, pandas, fastapi and yaml must be importable."
    )
