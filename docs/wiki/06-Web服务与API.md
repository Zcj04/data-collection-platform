# 06 · Web 服务与 API：app_fastapi.py

**职责**：FastAPI 表现层入口——首次管理员初始化、登录与权限校验、数据门户/管理后台页面渲染、用户管理、兼容 API 及 Portal/Admin 分组别名、静态资源挂载、模块初始化和僵尸任务清理。

## 6.1 应用初始化（模块导入即执行）

```python
app = FastAPI(title="企业内部数据平台", version="3.0.0")
app.mount("/static", StaticFiles(...))        # 本地化 Tailwind / Tabler Icons
_PORTAL_HTML_TEMPLATE = open("templates/portal.html").read()
_ADMIN_HTML_TEMPLATE = open("templates/dashboard.html").read()
_LOGIN_HTML_TEMPLATE = open("templates/login.html").read()
init_db()                                     # 建表（SQLite WAL）
_mark_stale_tasks_interrupted()               # 上次异常退出的 running 任务 → failed（"服务重启，任务中断"）
expire_stale_collection_runs()                # 未结束自动批次 → expired，允许同 job_key 受控补跑
cred_mgr = CredentialManager()                # 凭证管理单例
```

- `authentication_and_rbac` HTTP 中间件统一验证数据库 Session、权限、同源来源和 CSRF
- startup/shutdown 钩子分别启停两套独立排程：经营数据自动采集和会员资产自动同步；当前仍无 CORS/GZip 中间件
- `PLATFORM_LIST`：13 个平台 (标识, 中文名) 元组
- `SCHEDULER_TIMEOUT = config('scheduler.single_task_timeout', 600)`；`SCHEDULER_WORKERS = config('scheduler.max_workers', 12)`

经营数据排程默认启用，用 `Asia/Shanghai` 时区每日 10:10 采集目标月份 1 日至昨日的正式累计数据，不采集当天未结束数据；服务在时段后启动会补跑。时段与开关均可在 `config.yaml` 的 `scheduler.auto_collect` 调整。

## 6.2 辅助函数

| 函数 | 说明 |
|------|------|
| `_load_summary_data(date) -> List[Dict]` | 读 daily_summary 展开 metrics_json，附加"场地"键 |
| `_validate_date_range(start_date, end_date)` | 校验 YYYY-MM-DD 与顺序，非法抛 ValueError（API 转 400） |

## 6.3 后台线程 / 异步机制

- 路由多为 `async def` 协程；采集走 **daemon 线程 + ThreadPoolExecutor** 双层异步
- `POST /api/collect*` → `scheduler.launch_background()` 立即返回；已在跑返回 **409 busy**
- `CollectionAutoScheduler` 每 30 秒检查已到时段，在 `scheduled_collection_runs` 事务领取唯一 `job_key` 后复用同一 `Scheduler`；整批回调更新 success/partial/failed
- `CollectionAutoScheduler.status()` 返回 `next_start_date/next_target_date`，并为最近批次补充推导出的 `start_date` 与 `resolved`；原异常目标日期的最新平台任务已全部成功时视为恢复
- 前端 **8 秒轮询** `GET /api/tasks/{date}` + `GET /api/summary/{date}`（fetch + AbortController 可中断）刷新进度
- AI 分析师走 FastAPI `StreamingResponse` + SSE（meta/tool/delta/done/error 五种事件）

## 6.4 页面与 API 边界

- `/setup`：仅在尚无用户时开放，且首个超级管理员只能从本机创建；系统没有默认账号或默认密码。
- `/login`：统一登录页；管理员进入 `/admin`，全量门户账号进入 `/portal`，指定门店范围账号进入 `/store`。
- `/` 与 `/portal`：需要 `portal.view` 的企业数据门户；`/store` 是门店负责人专用的同源只读入口。
- `/admin`：需要 `admin.access` 的数据采集工作台；用户管理区还需要 `users.manage`。
- `/api/portal/*`：门户读取命名空间；下载接口需要 `portal.download`，其余接口需要 `portal.view`。
- `/api/admin/users*`：用户管理接口，需要 `users.manage`。
- 老板报表及其检查接口需要 `boss_report.download`；监控概览、同步状态和质量检查 GET 需要 `monitoring.view`；货款读取 GET 需要 `payment.view`，货款上传、修订和出货任务写操作需要 `payment.manage`。
- 其余 `/api/admin/*`：任务、凭证、未拆分的质量/监控/货款/AI 管理接口需要 `admin.access`；`admin.access` 对上述拆分权限具有覆盖能力。
- 原 `/api/*` 兼容路径与新命名空间执行相同的身份和权限校验，不存在未鉴权的旧路径旁路。
- 未被清单识别的新 `/api/*` 路径按 `admin.access` 失败关闭。

`GET /api/collection/status` 及其 `/api/portal` 别名是门户可读状态，只需 `portal.view`；所有全量、单平台和缺失/失败补采均是管理写操作，只有 `portal.view` 的用户不能触发。

`GET /api/platform-links` 及其 `/api/portal/platform-links` 别名同样只需 `portal.view`。接口只返回服务端白名单中的平台标识、名称、入口类型和 HTTPS 页面地址，不读取或下发爬虫凭证。

### 页面

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 1 | GET | `/setup` | 首次本机初始化页面；已有用户后跳转 `/login` |
| 2 | GET | `/login` | 登录页面；已有有效 Session 时按权限跳转 |
| 3 | GET | `/`、`/portal` | 验证 `portal.view` 后渲染 portal.html |
| 4 | GET | `/admin` | 验证 `admin.access` 后渲染 dashboard.html 管理工作台 |

## 6.5 认证、权限与用户管理

### 八项权限

| 权限 | 保护范围 | 依赖关系 |
|------|----------|----------|
| `portal.view` | 数据门户页面及普通门户读取 API | — |
| `portal.download` | 门户和兼容路径的汇总下载 | 自动包含 `portal.view` |
| `boss_report.download` | 老板报表及检查接口 | 自动包含 `portal.view` |
| `monitoring.view` | 监控概览、同步状态和质量检查 GET | — |
| `payment.view` | 货款数据 GET 接口 | — |
| `payment.manage` | 货款上传、修订和出货任务写接口 | 自动包含 `payment.view` |
| `admin.access` | 管理后台及未拆分管理 API，并覆盖上述业务模块 | 自动包含 `portal.view` |
| `users.manage` | 用户列表、创建、启停、改权限、重置他人密码 | 自动包含 `admin.access`、`portal.view` |

权限在写入前由 `core.auth.normalize_permissions()` 规范化，未知权限会被拒绝，且至少需要一项权限。首次 `/setup` 创建的超级管理员拥有全部八项权限；`admin.access` 对已拆分的业务权限具有服务端覆盖能力，但 `users.manage` 仍需显式授予。

### 登录与 Session

- 密码以 PBKDF2-SHA256（随机盐、600,000 次迭代）哈希保存，不可解密回显。
- 登录成功后浏览器获得 `workbuddy_session` Cookie；Cookie 为 HttpOnly、SameSite=Strict、Path=/，有效期 12 小时。
- 数据库 `auth_sessions` 仅保存会话令牌 SHA-256 哈希、用户、CSRF token 和到期时间，不保存浏览器持有的原始令牌。
- 请求方案为 HTTPS 时 Cookie 自动带 `Secure`；反向代理必须正确转发原始 HTTPS 方案。
- `/api/auth/me` 返回当前用户、权限定义、CSRF token 和到期时间。所有已登录的 POST/PUT/PATCH/DELETE API 必须在 `X-CSRF-Token` 头携带该 token，并通过同源 Origin/Referer 校验。
- `/api/auth/setup` 与 `/api/auth/login` 尚无登录 Session，因此不要求 CSRF token，但仍执行同源来源校验。
- 用户被停用、权限变化或密码被重置时，服务端撤销该用户全部现有 Session。

### 认证 API

| 方法 | 路径 | 请求/响应要点 |
|------|------|---------------|
| POST | `/api/auth/setup` | 首次本机创建超级管理员；body 为 username/display_name/password；仅可成功一次 |
| POST | `/api/auth/login` | body 为 username/password；成功写入 Session Cookie 并返回用户、CSRF token、跳转路径 |
| GET | `/api/auth/me` | 返回当前用户、八项权限定义、CSRF token、Session 到期时间 |
| POST | `/api/auth/logout` | 删除数据库 Session 并清除 Cookie |

### 用户管理 API

| 方法 | 路径 | 请求/响应要点 |
|------|------|---------------|
| GET | `/api/admin/users` | 用户列表（含手动设置的 `position`、`scope_type`/`venues`）、权限定义和当前账号标识；不返回密码哈希或会话令牌 |
| GET | `/api/admin/venues` | 返回数据范围选择器使用的标准门店名称清单；`items` 同时标识是否在营，供前端优先展示 |
| POST | `/api/admin/users` | 创建账号；body 为 username/display_name/password/permissions，可选 `position`、`scope_type`（`all`/`venues`）和 `venues` 门店名称数组 |
| PATCH | `/api/admin/users/{user_id}` | 修改姓名、职位、权限、数据范围或启停状态；门店范围至少包含一家门店；至少保留一个有效用户管理员 |
| DELETE | `/api/admin/users/{user_id}` | 永久删除其他账号并撤销其全部 Session；不能删除当前登录账号或最后一个有效用户管理员 |
| POST | `/api/admin/users/{user_id}/reset-password` | 重置他人密码并撤销其全部 Session；不能用于当前登录账号 |

## 6.6 兼容 API 端点清单

### 任务管理

| # | 方法 | 路径 | 参数 | 说明 |
|---|------|------|------|------|
| 2 | GET | `/api/tasks/{date}` | date 路径 | 某日各平台最新任务（同平台取 created_at 最大），返回 id/platform/status/error_msg/created_at |
| 3 | POST | `/api/collect` | start_date, end_date (Query) | 日期范围全平台并行采集；非法 400；忙 409；成功 `{status:started}` |
| 4 | POST | `/api/collect/single` | platform, start_date, end_date (Query) | 单平台重跑（max_workers=1）；未知平台 404；忙 409 |
| 5 | POST | `/api/tasks/stop/{task_id}` | task_id 路径 | 标记任务为用户手动终止 |
| 6 | GET | `/api/logs/{date}` | date 路径 | 某日全量任务日志（created_at 排序） |

### 汇总报表

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 7 | GET | `/api/summary/{date}` | daily_summary → report_summary.main() 生成汇总宽表 `{columns, rows, meta}`；`meta.platforms` 返回各来源实际最新数据日、门店数和状态 |
| 8 | GET | `/api/download/{date}` | 汇总导出 xlsx（StreamingResponse，RFC 5987 中文文件名）；另附“数据状态”工作表 |

### 数据大屏

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 9 | GET | `/api/dashboard/status?date=` | 大屏所需 4 日期数据齐备状态 |
| 10 | GET | `/api/dashboard/data?date=` | 大屏明细（总收入/完成率/区域与单日 TOP3/门店区域完成度/趋势） |
| 10a | GET | `/api/collection/status?date=` | 门户可读的采集新鲜度：真实入库时间、13 源覆盖、最近尝试、运行/失败数、下次排程及最近批次是否已恢复；不暴露平台错误详情 |

### 数据质量与站内异常闭环

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 10b | GET | `/api/quality/{date}` | 管理侧质量报告；`missing_platforms` 保持兼容，`retry_platforms` 为缺失 + 最新 `failed/stopped` 平台；已有旧数据但最新任务失败/停止时生成 `platform_failed` |
| 10c | POST | `/api/quality/collect-missing` | 路径为兼容旧前端保留，实际优先按 `retry_platforms` 补采缺失/失败平台；现在按当月1日至目标日补采；需要 `admin.access` 和 CSRF |

最近自动批次为 `partial` / `failed` / `expired` 且 `resolved=false` 时，管理后台展示站内异常提示并把目标日期带入质量中心。该提示不发送外部消息、不自动调用补采接口；只有管理员点击补采按钮才创建任务。后续最新平台任务全部成功后，`resolved=true`，提示自动解除。

### 每日经营数据

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/daily-operations?date=&venue=` | 相邻月累计快照差分、平台/门店收入、缺失和口径状态；门户只读别名为 `/api/portal/daily-operations` |

### 源平台入口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/platform-links` | 返回 13 个采集来源的核查入口状态；门户只读别名为 `/api/portal/platform-links` |

入口由 `config.yaml` 的 `platform_links` 显式维护，后端仅接受无账号信息、查询参数和片段的 HTTPS 页面地址。当前 10 个来源可跳转；精简、八达通、货款是 API 或本地文件来源，没有安全的网页入口，响应保留平台项但不返回 URL。

### 货款核算

货款核算 API 已按读取与管理拆分：查询接口需要 `payment.view`，上传、修订和出货任务写接口需要 `payment.manage`；兼容路径、`/api/portal` 只读别名与 `/api/admin` 别名执行相同的权限和 CSRF 校验。`/admin` 货款工作台页面仍需要 `admin.access`，只读账号从门户查看。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/payment-accounting/entry` | 标准门店、负责人状态及各门店最新进场货款版本 |
| POST | `/api/payment-accounting/entry/{venue}` | 追加一版进场日期、进场货款和备注；金额按分保存 |
| GET | `/api/payment-accounting/month/{year}/{month}` | 只读取该月最后一个自然日的累计基础货款；返回缺少门店和未匹配原始店名 |
| GET | `/api/payment-accounting/summary` | 按门店汇总进场货款、已结束月份的自然月末基础货款和自然月末净积分；返回逐月覆盖缺口 |

月末接口不会回退到更早日期。门店缺少记录返回 `amount=null`，与明确导入的 `0` 元区分。累计汇总中的积分来自 `daily_summary.metrics_json`，按各平台“积分增加 − 积分减少”合并，并固定按 **1 分 = 1.5 元**换算；不能累加每日快照。出货货款当前返回空值且不参与已知总货款。

### 凭证管理

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 11 | GET | `/api/credentials` | 全平台凭证健康状态（不解密） |
| 12 | POST | `/api/credentials/{platform}` | 保存凭证（body: dict；Fernet 加密入库） |
| 13 | GET | `/api/credentials/{platform}` | 读取凭证配置状态（不返回密钥；默认仅本机访问） |

### AI 分析师

| # | 方法 | 路径 | 说明 |
|---|------|------|------|
| 14 | POST | `/api/analyst/ask` | 同步问答（完整工具循环后返回答案） |
| 15 | POST | `/api/analyst/ask/stream` | SSE 流式问答（meta/tool/delta/done/error） |
| 16 | GET | `/api/analyst/config` | LLM 配置展示（不含密钥） |
| 17 | GET | `/api/analyst/sessions` | 历史会话列表 |
| 18 | GET | `/api/analyst/session/{session_id}` | 单会话消息记录 |
| 19 | GET | `/api/analyst/forecast?date=&horizon=3` | 本月完成率推演 + 月度趋势 |
| 20 | GET | `/api/analyst/forecast/anomaly?date=&threshold=2.0` | 单日收入异常检测 |

## 6.7 对核心模块的调用关系

| 模块 | 用途 |
|------|------|
| `adapters.factory.build_adapters` | 构建适配器列表传入 Scheduler |
| `core.auto_collection` | 经营数据 10:10 排程、job_key 防重、批次状态与门户新鲜度 |
| `core.config.get` | 调度参数 |
| `core.auth` | 用户、PBKDF2 密码哈希、权限规范化和数据库 Session |
| `core.credential_manager.CredentialManager` | 凭证加解密（API 11~13） |
| `core.dashboard_data` | 大屏（API 9~10） |
| `core.db` | SQLite 连接/建表 |
| `core.platform_links` | 校验并输出源平台入口白名单 |
| `core.scheduler.Scheduler` | 并行调度（API 3/4/27） |
| `core.task_manager.TaskManager` | 任务状态（API 2/5/6） |
| `core.analyst.agent` | 问答/会话（API 14/15/17/18） |
| `core.analyst.llm` | 配置展示（API 16） |
| `core.analyst.forecast` | 预测/异常（API 19/20） |
| `crawlers.report_summary` | 汇总宽表（API 7/8） |

## 6.8 进程入口

```python
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8010)
```

> 服务默认仅监听 `127.0.0.1`。身份认证和八项权限校验已覆盖旧 `/api/*` 与新命名空间；如确需远程访问，仍必须使用 HTTPS 反向代理，并确保应用感知 HTTPS 方案，使 Session Cookie 带 `Secure`。

> 自动排程依赖 Web 服务持续运行。当前未部署；生产初期请使用单 worker，不要仅因 `job_key` 防重就假设所有采集并发路径已支持多 worker。

## 6.9 附：utils/ 基础设施

### http.py — HTTP 会话封装

- `create_retry_session()`：requests.Session + HTTPAdapter + urllib3 Retry；仅对 GET/HEAD/OPTIONS 自动重试（status_forcelist=[429,500,502,503,504]）
- 重试次数与退避由 `config.yaml` 的 `http.max_retries`、`http.retry_backoff` 控制；各请求仍需显式传入 timeout
- 已接入主要 API 型爬虫，避免网络抖动导致单次采集直接失败
- 调度器还会按 `scheduler.max_retries` 对临时网络错误重试整个平台任务，业务错误不会重复执行

### mysql_pool.py — MySQL 连接池 + 场地映射缓存

- `MySQLConnectionPool`：自实现轻量池（Queue + RLock，预建 5 连接 + 溢出 3）；`get_connection(timeout=5)` 取连接并 `ping(reconnect=True)` 探活；`release_connection` 归还
- `VenueMapCache`：`get_venue_map(platform)` TTL 缓存（默认 3600s，config `cache.venue_map_ttl`）；仅缓存 **meituan 与 douyin** 两平台映射；`invalidate()` 主动失效
- 双单例（双重检查锁）：`get_pool()` / `get_cache()`；`fetch_all()` 为只读查询提供统一连接归还，`fetch_all_cached()` 按 TTL 缓存只读结果
- 主要场地映射查询已接入连接池和 TTL 缓存；报表配置查询使用自身进程级缓存，写入型查询不进入缓存
