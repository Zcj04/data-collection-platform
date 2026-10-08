# 本地测试入口

项目的 `.venv-tests` 只隔离 pytest 等测试工具，共享选定 WorkBuddy Python 的业务依赖，不是完全独立的业务运行环境。基础运行时被移除或更新后，应重新创建虚拟环境；脚本会先检查依赖，失败时明确退出。

首次准备（使用能导入本项目业务依赖的 Python 3.10 及以上版本）：

```powershell
& 'C:\Users\youruser\.workbuddy\binaries\python\versions\3.13.12.old.9068\python.exe' -m venv --system-site-packages .venv-tests
.\.venv-tests\Scripts\python.exe -m pip install pytest==8.3.5
```

运行本轮回归：

```powershell
.\scripts\run_tests.ps1
```

运行其他明确指定的测试：

```powershell
.\scripts\run_tests.ps1 -TestPaths tests/test_payment_accounting.py,tests/test_backup.py,tests/test_health.py
```

pytest.ini 将默认发现范围限制为 tests，避免扫描 outputs 中旧版本测试。入口禁用自动采集，并在运行前验证 Python 版本及依赖。测试脚本退出后不会改变已运行服务的环境。

# 货款修订与上传限制

上传及删除会在同一数据库事务保存操作前后快照、操作者账号 ID、源文件名和时间。失败的写入不留下成功历史。历史从本功能启用后开始记录；首次变更也会保存此前已有数据，无法补回此前已丢失的版本。

具有 payment.view 权限的全量账号可通过 GET `/api/admin/payment-data/history/2026-09-01` 读取最近 50 次记录。限定门店账号无法读取整日快照。历史仅提供核对依据，不自动恢复旧账；恢复需核对后重新导入。

上传限制：文件最多 10 MB、ZIP 声明解压总量最多 50 MB、条目最多 1000、首个工作表明细最多 10000 行。超限或无效文件返回 400，不改写原数据。这些是解析阶段限制，并非网络接收阶段限流或硬性解析超时。
