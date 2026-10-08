# 02 · 核心模块 core/

`core/` 是平台的中枢，承担六大职责：配置加载、数据库、调度、任务管理、凭证加密、数据消费（每日经营数据、大屏、预测与分析）。AI 能力（`analyst/`）见 [03-AI能力](03-AI能力.md)。

## 模块依赖总览

```
config.py ──── 被几乎所有模块使用（get/get_mysql_config/get_scheduler_config）
db.py ──────── 被 task_manager/credential_manager/dashboard_data/
               auto_collection/analyst.* 使用
logging.py ─── 被 scheduler/analyst.* 使用
models.py ──── 被 adapters/base.py 使用（CrawlerResult/Metric）
credential_manager.py ── 被 adapters/base.py 使用
targets.py ─── 被 dashboard_data/analyst.forecast 使用
task_manager.py ── 被 scheduler 使用
scheduler.py ── 被 app_fastapi.py 调用
auto_collection.py ── 被 app_fastapi.py 启动，复用 scheduler/task_manager
```

---

## 2.1 config.py — 统一配置加载器

**职责**：懒加载并缓存 `config.yaml`，支持 `.env` 环境变量覆盖，提供点号路径访问。

| 函数 | 签名 | 说明 |
|------|------|------|
| 懒加载 | `_load() -> Dict[str, Any]` | 加载 config.yaml 并缓存；先调 `_load_dotenv_file()` |
| 环境变量 | `_load_dotenv_file() -> None` | 优先 python-dotenv，缺失时手动逐行解析（`os.environ.setdefault` 不覆盖已有值） |
| 显式加载 | `load_env() -> None` | 幂等加载 .env，供依赖环境变量的模块调用 |
| **读配置** | `get(key: str, default=None) -> Any` | 点号路径，如 `get('scheduler.single_task_timeout')` |
| MySQL 配置 | `get_mysql_config() -> Dict` | host/port/user/password/database/charset/超时；环境变量优先于 yaml |
| 调度配置 | `get_scheduler_config() -> Dict` | max_workers=12 / single_task_timeout=600 / max_retries=3 |
| 平台配置 | `get_platform_config(platform: str) -> Dict` | 读 `platforms.<name>` 节 |
| 重载 | `reload() -> None` | 清缓存强制重读 |

> 安全设计：MySQL 密码已从 config.yaml 迁移到 `.env` 的 `MYSQL_PASSWORD`（v2.0 重构成果，原先 10+ 爬虫硬编码明文密码）。

经营数据自动排程通过点号路径直接读取 `scheduler.auto_collect.enabled` / `check_interval_seconds` / `slots`；默认配置见 2.8。

## 2.2 db.py — SQLite 连接与建表

**职责**：连接工厂（WAL + 外键 + Row factory）+ 全部建表语句。表结构详见 [08-数据存储设计](08-数据存储设计.md)。

| 函数 | 签名 | 说明 |
|------|------|------|
| 连接 | `get_connection() -> sqlite3.Connection` | 每次新建连接；确保 `data/` 目录存在；`PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`；`row_factory=sqlite3.Row` |
| 建表 | `init_db()` | `BEGIN IMMEDIATE` 事务内 executescript 创建业务、排程、监控、认证和 AI 相关表与索引，异常回滚 |

常量：`DB_PATH = <项目根>/data/app.db`。

## 2.3 models.py — 数据模型（dataclass）

| 模型 | 字段 | 用途 |
|------|------|------|
| `Metric` | `metric: str`、`value: Any`、`unit: str = ""` | 单个指标（如 美团收款 / 1234.5 / 元） |
| `CrawlerResult` | `platform`、`date`、`metrics: List[Metric]`、`venue=""`、`raw_file=None`、`extra: Dict` | 适配器统一返回结构 |
| `Task` | `id/platform/date/status/step/progress/started_at/finished_at/created_at/retry_count/error_msg` | 对应 tasks 表 |
| `Credential` | `platform/encrypted_value/status/last_check_at/last_success_at/updated_at` | 对应 credentials 表 |

## 2.4 credential_manager.py — 凭证加密存储

**职责**：Fernet 对称加密各平台凭证（JSON），UPSERT 进 `credentials` 表。

**加密机制**：
- 密钥文件 `credentials/.fernet_key`；不存在时 `Fernet.generate_key()` 生成并写回
- 加密链：`credential_dict → json.dumps(ensure_ascii=False) → utf-8 → Fernet.encrypt → ascii` 入库
- 解密链反之。Fernet = AES-128-CBC + HMAC-SHA256（防篡改）

**CredentialManager 类**：

| 方法 | 说明 |
|------|------|
| `save(platform, credential_dict)` | 加密 UPSERT（`BEGIN IMMEDIATE` 事务），状态置 active |
| `load(platform) -> Optional[dict]` | 解密读取 active 凭证 |
| `get(platform, field, default="") -> str` | 读单字段便捷方法（适配器最常用） |
| `check_status(platform) -> str` | active / expired / unknown |
| `mark_expired(platform)` | 标记失效 |

## 2.5 limiter.py — 请求限流器

**职责**：控制各平台最小请求间隔，防反爬风控。线程安全（`threading.Lock` + `defaultdict` 平台→上次请求时间戳）。

- `RateLimiter.wait(platform, min_interval=1.0)`：阻塞等待直到间隔满足
- `RateLimiter.reset(platform=None)`：重置计时
- 全局单例：`get_limiter() -> RateLimiter`

## 2.6 logging.py — 结构化日志

- `setup_logging(level=INFO)`：全局 basicConfig
- `get_logger(name=None)`：获取命名 Logger（默认 `datapipeline`）
- 模块级便捷函数：`info/warning/error/debug`

## 2.7 scheduler.py — 并行采集调度器（核心）

**职责**：ThreadPoolExecutor 并行执行各平台适配器；单任务独立超时；完成即保存，慢平台不阻塞其他结果。

### Scheduler 类

类级属性（全局互斥）：
- `_lock = threading.Lock()`
- `_busy = False` —— 保证同一时刻只有一批采集在跑

| 方法 | 签名 | 说明 |
|------|------|------|
| 构造 | `__init__(task_mgr=None, max_workers=12, single_task_timeout=600, max_retries=None)` | 默认 TaskManager；`max_retries=None` 时读 `scheduler.max_retries` |
| 进度回调 | `set_progress_callback(callback)` | `callback(platform, status, detail)` |
| 超时解析 | `_resolve_timeout(platform) -> int` | 优先 `platforms.<name>.task_timeout`（美团 2400s），否则全局 600s |
| **核心调度** | `run_range(start_date, end_date, adapters)` | 见下文机制详解；返回 total/completed/failed |
| 单任务 | `_run_one(adapter, start_date, end_date, task_id)` | mark_running → check_credential → 对临时网络错误重试 adapter.run() → `complete_with_summary` 原子替换平台当日整批数据；异常 mark_failed + raise |
| **后台启动** | `launch_background(..., completion_callback=None) -> bool` | daemon 线程跑 `_run_with_lock`；已在跑返回 False；整批结束时向回调传 total/completed/failed |

### 调度机制详解

1. **提交**：一次性 `pool.submit(self._run_one, ...)` 全部 jobs，记录 `futures[fut]=(platform, tid)` 与各自 `deadlines[fut]`
2. **等待循环**：`while pending` 中计算 `earliest = min(deadlines)`：
   - 已到期 → 所有到期 future 批量 `mark_failed(tid, "超时 Xs")` 并**放弃等待**（Python 线程无法强杀，但不再阻塞结果落库）
   - 未到期 → `wait(pending, timeout=earliest-now, return_when=FIRST_COMPLETED)`
3. **完成处理**：`fut.result()` 成功计数 / 异常计数 + error 回调
4. **清理**：finally `pool.shutdown(cancel_futures=True)`（Py3.8 兼容降级）

> `scheduler.max_retries` **已实现**：默认最多重试 3 次，仅适用于超时、连接失败、429 和部分 5xx 临时错误，并记入 `tasks.retry_count`；凭证、映射和数据格式错误不会自动重试。

## 2.8 auto_collection.py — 经营数据自动排程

**职责**：在 Web 服务进程内按业务时段触发 13 平台经营数据采集，用 SQLite 事务和唯一 `job_key` 防止服务重启或多进程同时检查时重复触发。该排程与 `monitor_sync.py` 的多金宝会员资产监控完全独立。

### 默认时段

| 时间（`Asia/Shanghai`） | 标签 | 目标截止日 | 采集范围 |
|------|------|------|------|
| 10:10 | 昨日正式数据 | 当日 - 1 天 | 截止日所在月 1 日至截止日 |

- `scheduler.auto_collect.enabled` 默认 `true`，排程每 30 秒检查一次；开关、间隔、时段标签、时间和目标日偏移均可由 `config.yaml` 调整。
- `BUSINESS_TIMEZONE` 显式固定为 `Asia/Shanghai` UTC+8，不依赖服务器本地时区。
- `trigger_due()` 遍历当日已到时段，因此服务在时段之后启动时会尝试补跑；传给 `Scheduler` 的开始日期始终由目标截止日推导为当月 1 日。
- `job_key=<slot_key>:<排程日期>` 在 `scheduled_collection_runs` 中唯一；同时只允许一个 pending/running 自动批次。
- 启动时会把旧 pending/running 批次标为 `expired`，同一 job_key 可受控重试并增加 retry_count。
- 结束回调将批次置为 `success` / `partial` / `failed`；服务中断状态为 `expired`。
- `get_collection_freshness(date)` 返回真实 `daily_summary.updated_at`、当前配置的 13 源覆盖、最近尝试时间、运行/失败数和下次排程，供管理后台与门户展示。

> 这是进程内排程：服务必须持续运行。生产初期应使用单 worker；即使 `job_key` 能防止同一时段重复领取，当前 `Scheduler._busy` 仍是单进程互斥。

## 2.9 task_manager.py — 任务状态机

**职责**：封装 `tasks` 状态变更和 `daily_summary` 整批/单行写入与查询。

### 任务生命周期

```
pending ──mark_running──> running ──mark_success──> success (progress=100)
   │                        │
   │                        ├──mark_failed──> failed (error_msg)
   └──mark_failed───────────┤
                            └──mark_stopped──> failed ('用户手动终止'，无状态守卫可强制覆盖)
```

- `mark_success` / `mark_failed` 带 `WHERE status IN ('running','pending')` 守卫，防终态翻转
- `update_step(task_id, step, progress=None)`：仅 running 态更新步骤

### 主要方法

| 方法 | 说明 |
|------|------|
| `create(platform, date, venue="") -> str` | uuid4 任务号，状态 pending |
| `get(task_id)` / `list_by_date(date)` / `list_running_and_recent(limit=50)` | 查询 |
| **`complete_with_summary(task_id, platform, date, rows)`** | 事务内重新检查任务状态，原子替换平台当日整批数据并标记成功 |
| **`save_summary(platform, date, venue, metrics_json, raw_file=None)`** | UPSERT daily_summary（`BEGIN IMMEDIATE`，键 UNIQUE(date,venue,platform)） |
| `get_summary(date)` | 查某日汇总 |

## 2.10 targets.py — 月度目标读取

| 函数 | 说明 |
|------|------|
| `find_target_file() -> str` | 优先 config `targets.file`，否则取 `data/targets/` 下 mtime 最新 xlsx |
| `load_store_targets() -> dict` | openpyxl 读「最终核定」列 → `{场地: 目标(元)}`，忽略合计/空行 |
| `load_store_regions() -> dict` | 读 `data/targets/regions.json` → `{场地: 区域}` |

## 2.11 dashboard_data.py — 数据大屏计算

**核心口径**：daily_summary 存的是**当月累计值**，单日收入 = 当日累计 − 前一采集日累计（跨月重置，月内首个采集日的单日值 = 该日累计自身）。

| 函数 | 说明 |
|------|------|
| `get_dashboard_status(target_date) -> dict` | 检查大屏所需 4 个日期（当日/前日/前 2 日/上月同日）数据是否齐全 |
| `get_dashboard_data(target_date) -> dict` | 大屏全量数据：当日单日、前日单日、本月累计、上月同日、环比、目标完成率、门店/区域完成度、内地/香港分拆、TOP3、每日趋势 |
| `_load_venue_income(date)` | 动态 `importlib` 调 `crawlers.report_summary.main()` 计算收入汇总 |
| `_load_daily_trend(start, end)` | 每日趋势（累计差分） |

常量：`FALLBACK_MONTHLY_TARGET = 561.2`（万元，目标表缺失时兜底）。

完成度颜色口径：绿 ≥100% / 黄 70–99% / 红 <70%。

## 2.12 daily_operations.py — 每日经营数据

`get_daily_operations(date, venue="")` 读取目标日和前一自然日的 `daily_summary`，按收入字段计算平台/门店差值；每月1日使用月初基线。它同时检查采集范围、平台任务状态、门店平台缺行和累计回退，旧快照没有 `period_start` 时返回 `unverified`。

收入字段沿用 `crawlers.report_summary.INCOME_COLUMNS` 的平台拆分口径，香港指定门店同步扣除已包含在鲸舰现金中的 KPay，避免总收入重复计算。

## 跨模块数据流速查

```
手动采集：app_fastapi → Scheduler.launch_background → ThreadPoolExecutor → _run_one
        → CredentialManager.load（解密）→ adapter.run（RateLimiter.wait 限流）
        → TaskManager.complete_with_summary（原子替换 daily_summary，并记录 period_start/source_task_id）

自动采集：app_fastapi startup → CollectionAutoScheduler → scheduled_collection_runs 事务领取
        → Scheduler.launch_background → completion_callback 更新 success/partial/failed

每日经营数据：app_fastapi → daily_operations.get_daily_operations
        → daily_summary（目标日/前一日）+ 缺失和口径校验

大屏：app_fastapi → dashboard_data.get_dashboard_data
        → daily_summary（4 个日期）+ targets（目标/区域）+ report_summary.main
```

### SQLite 表读写映射

| 表 | 写入方 | 读取方 |
|----|--------|--------|
| tasks | task_manager | task_manager / scheduler / API 层 |
| daily_summary | task_manager.complete_with_summary / save_summary | auto_collection 新鲜度 / daily_operations / dashboard_data / analyst.tools / analyst.forecast |
| scheduled_collection_runs | auto_collection | auto_collection / 采集状态 API |
| credentials | credential_manager | credential_manager（+ API 凭证页） |
| analyst_sessions / analyst_messages | analyst.agent | analyst.agent |
