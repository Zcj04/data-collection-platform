# 部署准备与放行条件（2026-09-10）

当前基线为 Windows、Python 3.13、单个 FastAPI 进程、同机 SQLite。部署目标机器和域名尚未指定，本次不启动代理、不对外发布、不重启现有服务。Linux 迁移需要额外验证本地文件路径、采集浏览器与进程回收，不能直接视为已支持。

## 1. 准备独立运行环境

在目标项目目录执行，使用正式 Python 3.13，不依赖开发电脑的 WorkBuddy 安装：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m playwright install chromium
```

将 `.env.example` 复制为 `.env` 后在目标机填写。配置不会自动让所有 YAML 项支持环境变量覆盖，按代码已支持的变量填写。MySQL 使用只读业务账号；逐一核对八达通 CSV、日/月货款 Excel、目标表和区域映射的目标机路径。月度目标表当前配置为 2026-08，不能据此认定九月目标已齐备。

代码迁移采用目录白名单：`adapters`、`core`、`crawlers`、`utils`、`templates`、`static`、`scripts`，以及根目录 `app_fastapi.py`、`runtime_bootstrap.py`、`build_excel.py`、`config.yaml`、`requirements.txt`。测试另带 `tests`、`tools`、`requirements-dev.txt`、`pytest.ini`。不要直接打包整个工作目录：`.env`、`credentials`、`data`、`outputs`、`logs`、`workbench.db`、浏览器配置和历史备份含业务或认证资料。业务文件和凭证通过受控渠道单独迁移，并限制为运行账号可读写。

## 2. 数据、密钥与回滚

数据库必须使用 `core.backup.create_backup` 在线备份，并以 `verify_backup` 校验完整性、哈希和密钥解密。不要在服务运行时直接复制 `app.db`，WAL 中可能还有未合并数据。数据库与 `.fernet_key` 必须来自同一个备份包；包内 `fernet.key` 恢复为目标机 `credentials/.fernet_key`，`app.db` 恢复为 `data/app.db`。

本轮快照只能作为准备时点的恢复演练；正式切换前停止写入并重新备份。切换时只运行一个写入节点，不能让新旧机器同时采集。迁移后的旧登录会话也在数据库中，正式切换时应清理 `auth_sessions` 并让用户重新登录（本轮未修改现有会话）。

另行迁移所需的目标/区域文件、手工货款文件及必要的浏览器登录资料；数据库备份不包含这些文件。保留上一版代码和切换前成对备份，故障时停止新进程再一并恢复代码、数据库和密钥。上线后新增数据必须先核对，不能无条件覆盖回旧库。备份还需复制到独立受控存储；同盘备份无法应对磁盘损坏。

## 3. 本机预检及首次初始化

```powershell
.\.venv\Scripts\python.exe scripts/deployment_preflight.py
# 如为全新库，首次预检会因缺少数据库/管理员而阻止放行，这是预期行为。
.\scripts\run_fastapi_service.ps1 -PythonPath .\.venv\Scripts\python.exe -DisableAutoCollection
```

启动命令为前台长期运行命令，由运维安排执行。初始化先用本机 `http://127.0.0.1:8010/setup`，完成后重新预检。脚本退出码 0 只证明本机依赖、文件、数据库与密钥检查通过，不代表网络、证书或真实采集成功。运行方式仍然只能使用 `python -u app_fastapi.py`；禁止多 worker、reload、两份共享数据库实例。

新机器的托管任务需指向此启动脚本，并使用最小权限账号。现有 `stop_fastapi_service.ps1` 校验的是原 WorkBuddy 任务与解释器，不能直接当成新机器通用停止工具。正式托管需验证退出登录/重启后的恢复行为，避免仅在交互登录期间可用。

## 4. HTTPS 入口

提供 `deploy/Caddyfile.example` 作为同机反向代理模板。尚未安装或启动 Caddy，也未绑定域名或签发证书。先将实际域名解析到目标机器，并准备证书签发条件；仅向需要的内网/VPN用户开放入口，8010 保持回环监听。

```powershell
$env:SITE_ADDRESS='实际域名'
caddy validate --config deploy/Caddyfile.example --adapter caddyfile
# 验证成功后由运维按托管方式启动，勿把开发测试作为正式服务。
```

模板禁止从代理初始化管理员，并在代理处限制请求体为 12 MB（给应用的 10 MB 文件上限留出 multipart 开销）。默认代理转发客户端地址和 HTTPS 协议，应用只应信任同机代理；不得把 `FORWARDED_ALLOW_IPS` 配成 `*`。在实际 HTTPS 登录后必须确认 Cookie 带 `Secure`、`HttpOnly`、`SameSite=Strict`，写操作的 Origin/CSRF 校验正常，客户端 IP 没有全部变成回环地址。超大/分块上传必须验证返回 413，不能只检查 Content-Length。

配置依据：[Caddy 请求体限制](https://caddyserver.com/docs/caddyfile/directives/request_body)、[反向代理与转发头](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。此模板尚未在目标代理程序中验证，不能据此直接判定入口已就绪。

## 5. 放行清单

- 目标机独立安装和 `pip check` 成功，预检所有项通过；全量测试通过。
- `/health/live` 和 `/health/ready` 均正常，成对备份能够在隔离目录恢复；至少一份异机备份。
- 未登录请求被拒绝；查看者不能写入；门店账号只能查询/下载分配门店；停用后会话立即失效。
- 手机真机 Safari/Chrome 与电脑浏览器验证登录、导航、日期筛选、下载、上传和表格滚动。当前 Chromium 仿真结果不能替代真机测试。
- 执行一个已授权的平台采集批次并检查入库日期、完整性、实际记录和错误提示；网络失败不能当成零收入。鲸舰明文链路开关应保持关闭，除非已落实受控链路。
- 验证单实例托管、任务日志、容量告警、外部告警接收与恢复流程。当前应用登录限流按 IP+用户名组合，不能替代入口总体并发/抗攻击策略。
- 最后由运维去掉 `-DisableAutoCollection`，检查 10:30 的昨日正式采集排程和会员监控；首次恢复联网可能补跑错过的任务，应观察批次。

以上未完成前，只能称为已完成本地部署准备，不能称为已上线或无重大风险。
