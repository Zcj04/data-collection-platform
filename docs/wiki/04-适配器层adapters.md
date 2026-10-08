# 04 · 适配器层 adapters/

**职责**：在「保持 crawlers/ 原始脚本零改动」的前提下，提供统一的适配器抽象——负责凭证解密、凭证注入爬虫模块、`importlib` 动态加载爬虫 `main()`、异常统一包装。

## 4.1 base.py — 抽象基类

```python
class CrawlerError(RuntimeError):
    """统一爬虫异常：__init__(platform, detail)，消息格式 "[{platform}] {detail}" """

class CrawlerAdapter(ABC):
    @property
    @abstractmethod
    def platform_name(self) -> str: ...

    @abstractmethod
    def run(self, start_date: str, end_date: str,
            progress_callback: Optional[Callable[[str], None]] = None) -> list:
        """执行采集，返回场地维度字典列表 [{场地: ..., 指标: 值, ...}]"""

    @abstractmethod
    def check_credential(self) -> bool: ...
```

> 基类不定义 `__init__`，子类自行初始化（通常 `self._cred_mgr = CredentialManager()`）。

## 4.2 all_adapters.py — 13 个适配器实现

### _SyncAdapter（标准同步基类）

子类只指定 `MODULE`（爬虫模块路径）与 `PLATFORM`。`run()` 通过 `importlib.import_module(self.MODULE)` 加载爬虫并调 `main(start_date, end_date, 账号, 密码)`，异常包装为 `CrawlerError`；`check_credential()` 检查 `CredentialManager.get(PLATFORM, "账号")` 非空。

| 适配器 | 爬虫模块 | 凭证字段 | 特殊逻辑 |
|--------|----------|----------|----------|
| YuntaiAdapter | crawlers.yuntai_crawler | 账号/密码 | — |
| LeyaoyaoAdapter | crawlers.leyaoyao_crawler | 账号/密码 | — |
| JingjianAdapter | crawlers.jingjian_crawler | 账号/密码 | — |
| StarThingAdapter | crawlers.starthing_crawler | 账号/密码 | — |
| HuilianAdapter | crawlers.huilian_crawler | 账号/密码 | — |
| KPayAdapter | crawlers.kpay_crawler | 账号/密码 | — |
| NewSystemAdapter | crawlers.new_system_crawler | 账号/密码 + access_key/secret_key | 覆写 `run()`：额外注入 AK/SK 到 `mod.CLOUDBASE_CONFIG` |

### 自定义适配器

| 适配器 | 凭证注入方式 | 流程 |
|--------|--------------|------|
| **MeituanAdapter** | `_inject_creds(dl)` 读 Cookie1~2 / mtgsig1~2 / partner_id1~2 注入 `dl.COOKIES / dl.MTGSIGS / dl.partners_id`（最多 2 组账号） | `meituan_download.main()` 下载解析 → `meituan_match.main()` 场地匹配；`check_credential` 用首组 Cookie+mtgsig 调 shopinfo 接口探活 |
| **DuojinbaoAdapter** | 读取单一采集账号/密码并组装 `[{"username","password"}]` 注入 `mod.ACCOUNTS` | `duojinbao_crawler.main(start, end)` |
| **DouyinAdapter** | 账号/密码；Cookie 优先用本地 `credentials/douyin/cookie_header.txt` 落盘状态 | `douyin_download.main(start,end,账号,密码)` → `douyin_match.match()` |
| **OctopusAdapter** | 无凭证（check_credential 恒 True） | `octopus_crawler.main(start, end)` 读本地 CSV |
| **PaymentAdapter** | 无凭证 | `payment_crawler.main()` 读本地 Excel（默认路径） |
| **CoinExchangeAdapter** | 无凭证 | `coin_exchange_crawler.main(end_date)` 以 end_date 为汇总日 |

### 凭证流转链（通用模式）

```
凭证管理页保存 → CredentialManager.save（Fernet 加密入库）
适配器 __init__ → self._cred_mgr = CredentialManager()
check_credential / run → self._cred_mgr.get(platform, 字段)（解密读取）
    → 通过 importlib 把账号/密码/Key 注入爬虫模块级变量
        （mod.ACCOUNTS / dl.COOKIES / mod.CLOUDBASE_CONFIG / main() 参数）
爬虫 main() 使用凭证登录平台
```

## 4.3 factory.py — 工厂

```python
def build_adapters() -> List[CrawlerAdapter]:
    """返回全部 13 个适配器实例（硬编码扁平列表）"""
```

**说明**：没有注册表、没有按 `type`（sync/async/local_csv/local_excel/local_json）的显式分发逻辑。config.yaml 中的 `type` 字段只是概念标注；实际「分发」是各适配器在 `run()` 里各自加载对应爬虫模块，由调度器遍历 `build_adapters()` 列表统一执行。所谓 `async` 类型实际不存在异步适配器（全部同步，靠线程池并行）。

## 4.4 历史实现

早期适配器实现已被当前 `adapters/` 完整取代，并从仓库移除。当前适配器统一通过 `CredentialManager` 注入凭证，使用 ABC 基类、factory 和集中实现；不要恢复旧版硬编码凭证路径。

## 4.5 调用链中的位置

```
app_fastapi.py
  └─ build_adapters() ────────── 适配器实例列表
       └─ Scheduler.launch_background(start, end, adapters)
            └─ ThreadPoolExecutor → adapter.check_credential() → adapter.run()
                 └─ importlib → crawlers.<平台>.main(...)
                      返回 [{场地, 指标...}] → TaskManager.save_summary
```
