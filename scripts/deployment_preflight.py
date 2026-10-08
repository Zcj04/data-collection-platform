"""Read-only local deployment checks; never imports the application or starts jobs."""
import importlib
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    checks = []

    def record(name, ok, detail=""):
        checks.append({"check": name, "status": "pass" if ok else "blocked", "detail": detail})

    record("python", sys.version_info[:2] == (3, 13), "Production baseline: Python 3.13")
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, required = line.strip().split("==", 1)
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        record(f"dependency:{name}", actual == required, f"required={required}, actual={actual}")
    for module in ("numpy", "pandas", "fastapi", "yaml", "openai", "openpyxl", "requests",
                   "uvicorn", "cryptography", "pymysql", "playwright.sync_api", "python_multipart"):
        try:
            imported = importlib.import_module(module)
            if module == "numpy":
                imported.rec
            record(f"import:{module}", True)
        except Exception as error:
            record(f"import:{module}", False, type(error).__name__)
    if any(c["status"] == "blocked" and c["check"].startswith("import:") for c in checks):
        print(json.dumps(checks, indent=2))
        return 1

    from core.config import get
    from cryptography.fernet import Fernet
    for setting in ("backup.enabled", "backup.required_on_startup", "scheduler.process_isolation"):
        record(setting, get(setting) is True)
    database = ROOT / "data" / "app.db"
    key = ROOT / "credentials" / ".fernet_key"
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            record("database_integrity", connection.execute("PRAGMA quick_check").fetchone()[0] == "ok")
            users = connection.execute("SELECT permissions_json FROM users WHERE is_active=1").fetchall()
            record("active_administrator", any("users.manage" in json.loads(row[0]) for row in users), "Initialize locally before exposing the proxy")
            cipher = Fernet(key.read_bytes())
            for row in connection.execute("SELECT encrypted_value FROM credentials WHERE status='active'"):
                cipher.decrypt(row[0].encode("ascii"))
            record("credential_key_matches_database", True)
        finally:
            connection.close()
    except Exception as error:
        record("database_and_key", False, type(error).__name__)
    paths = ["platforms.octopus.file_path", "platforms.payment.file_path", "platforms.payment.file_path_daily",
             "targets.file", "targets.store_regions_file"]
    for setting in paths:
        value = get(setting, "")
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        record(f"source:{setting}", bool(value) and path.is_file(), "Verify this source or explicitly disable the corresponding feature before release")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        record("chromium_launch", True)
    except Exception as error:
        record("chromium_launch", False, type(error).__name__)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return int(any(c["status"] == "blocked" for c in checks))


if __name__ == "__main__":
    raise SystemExit(main())
