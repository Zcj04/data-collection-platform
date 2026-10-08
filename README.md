# WorkBuddy 数据采集平台

面向门店经营数据的本地采集、核对与分析平台。项目以 FastAPI 提供管理后台和普通用户门户，统一管理多平台采集任务、月度累计数据、经营看板、货款核算、会员监控与资料导入。

## 主要功能

- 多平台经营数据采集与任务调度
- 月度累计数据大屏和异常回退检查
- 管理后台、普通用户门户与 RBAC 权限控制
- 货款、库存、会员储值和每日经营数据核对
- 本地资料导入、来源归档与只读 reconciliation
- AI 经营分析与告警记录
- SQLite 自动备份、健康检查和敏感日志脱敏

## 项目结构

```text
adapters/          平台适配层
core/              业务逻辑、权限、调度、数据质量与存储
crawlers/          各上游平台采集实现
templates/         FastAPI 页面模板
static/            前端样式、脚本与本地化资源
tests/             后端、前端契约与业务回归测试
scripts/           测试、预检和本地运维脚本
docs/              技术文档、操作手册与设计说明
```

## 本地运行

要求 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -u app_fastapi.py
```

服务默认仅监听 `127.0.0.1:8010`：

- 登录页：<http://127.0.0.1:8010/login>
- 管理后台：<http://127.0.0.1:8010/>
- 普通用户门户：<http://127.0.0.1:8010/portal>

不要使用多 worker 或 `--reload` 启动该服务；SQLite 调度器要求单进程运行。

## 配置与凭证安全

1. 将 `.env.example` 复制为 `.env`，只在本机填写真实环境变量。
2. 平台账号和密码通过管理后台保存，由 `CredentialManager` 加密写入本地数据库。
3. `data/app.db` 与 `credentials/.fernet_key` 必须成对备份，否则加密凭证无法恢复。
4. 不要向 Git 提交 `.env`、`credentials/`、数据库、备份、Cookie、浏览器登录状态、日志或导入原始资料。

仓库的 `.gitignore` 已排除上述敏感内容；提交前仍应执行一次密钥扫描并检查暂存文件。

## 测试

项目测试入口会关闭自动采集，避免测试期间访问真实上游平台：

```powershell
.\scripts\run_tests.ps1
```

也可以运行指定测试：

```powershell
.\scripts\run_tests.ps1 -TestPaths tests/test_health.py,tests/test_auth_rbac.py
```

更多说明见 [docs/testing.md](docs/testing.md) 和 [docs/技术文档.md](docs/技术文档.md)。

## 部署提醒

公网部署前需完成 HTTPS、反向代理、备份恢复演练、外部告警以及目标服务器上的安全检查。不要把本地测试通过等同于生产环境已就绪。
