# 05 · 爬虫层 crawlers/

18 个采集/匹配脚本。**核心原则：保持原始业务计算逻辑，统一补充必要的安全边界**，由 `adapters/` 层通过 `importlib` 加载调用。统一约定：每个采集器暴露 `main(...)`，返回「场地维度字典列表」`[{场地: "深圳XX店", <指标名>: 值, ...}]`。

## 5.0 总览

| 脚本 | 平台 | 采集方式 | 认证方式 | 指标数 |
|------|------|----------|----------|--------|
| yuntai_crawler.py | 芸苔 rapa.vip | HTTP API | Base64 密码 + Token（多主体多 token） | 11 |
| leyaoyao_crawler.py | 乐摇摇 b.leyaoyao.com | HTTP API | MD5 密码 + ticket 头 | 5 |
| duojinbao_crawler.py | 多金宝 djb.leyaoyao.com | HTTP API | 3 账号登录 + 逐店切换 | 6 |
| jingjian_crawler.py | 鲸舰 jingjianx.vip | HTTP API（默认阻断；支持 HTTPS 网关） | Bearer Token（逐店切换取新 token） | 9 |
| starthing_crawler.py | StarThing | HTTP API | 双重 MD5：`md5(md5(pwd)+VERIFY_CODE)` | 9 |
| new_system_crawler.py | 新系统 | HTTP API ×2 | 公共 API + CloudBase HMAC-SHA256 签名 | 2 |
| huilian_crawler.py | 汇联 iotbox.cn | HTTP API | Bearer Token | 5 |
| kpay_crawler.py | KPay（香港） | HTTP API | RSA 双向加密 + AES 密码 + 签名 | 2 |
| meituan_download.py | 美团 e.dianping.com | HTTP API（异步下载） | Cookie + mtgsig 签名 | 3 |
| meituan_match.py | 美团匹配 | MySQL 读取 | — | — |
| douyin_download.py | 抖音来客 | HTTP API + Playwright 兜底登录 | Cookie（本地落盘）/账号密码 | 3 |
| douyin_fetch.py | 抖音来客（独立完整版，未被适配器使用） | HTTP API + Playwright | 环境变量账号密码 | 3 |
| douyin_match.py | 抖音匹配 | MySQL 读取 | — | — |
| octopus_crawler.py | 八达通 | 本地 CSV | 无 | 2 |
| payment_crawler.py | 货款 | 本地 Excel | 无 | 1 |
| coin_exchange_crawler.py | 兑币机 | Playwright 金山文档 / 本地 JSON | 公开分享链接 | 1 |
| report_summary.py | 汇总计算器（非采集器） | MySQL + pandas | — | 派生 |

**MySQL 交互统一结论**：所有爬虫对 MySQL `store_mapping_db` **只读不写**——读 `company_organizational_structure` 表做「平台店铺名 → 场地」映射（各平台对应不同列，见文末映射总表）；`report_summary.py` 额外读 `table_sequence_config` 与 `principal`。

---

## 5.1 yuntai_crawler.py — 芸苔

- **入口**：`main(start_time, end_time, account, password)`；时间转毫秒时间戳（结束日自动到 23:59:59）
- **流程**：`_login`（可能返回多 token，多主体 MerchantList→LoginById）→ 逐 token `_collect_token_data` → `_get_match_tuples` 映射 → `_match_yuntai_lists`（跨 token tenant_id 去重）
- **数据接口**：`/GetTenantAnalysisDetail`（按支付名分类收入）、`/GetAcrossSettlementList`（远程取币，排除"福州A广场"）、`/GetDeviceAnalysisDetail`（GameMachine 投币/出货 + CoinExchange 出币）、`/GetGoodsAnalysisDetail`（积分 Recovery/Exchange）
- **派生**：芸苔手续费=(微信+支付宝)×0.006；芸苔非团购=现金+微信+支付宝
- **风控**：图形验证码（code 2009000002 等）时提示去官网人工验证
- **映射列**：`yuntai`、`yuntai2`（均支持换行多店名）

## 5.2 leyaoyao_crawler.py — 乐摇摇

- **入口**：`main(start_date, end_date, username, password)`
- **流程**：`login`（`POST /lyy/rest/group/distributor/login`，密码 MD5，ticket 写入 `authorization-bar` 头）→ `get_equipment_groups`（设备组=场地）→ 逐组 `get_group_order_data`（日报模式 latitude=1 / 自定义模式 latitude=6，取 onlinePayAmount/cashPayAmount/coinsSumNumber/giftConsumptionNumber）→ 匹配聚合
- **派生**：手续费=非现金×0.006
- **映射列**：`leyaoyao`（支持换行多组名）

## 5.3 duojinbao_crawler.py — 多金宝

- **入口**：`main(start_date, end_date)`；日期按 5 天分段（`split_time_by_five_days`）
- **流程**：遍历模块级 `ACCOUNTS`（3 账号，适配器注入）→ `login`（`POST /gw/venue/login`）→ `get_all_stores` → 逐店 `switch_store` + `get_one_store_data`（支付分类：小程序/在线=非现金；设备：receiveCoinNum/sellCoinNum/giftNum）→ 匹配聚合
- **派生**：手续费=非现金×0.006
- **别名**：`SHOP_NAME_ALIASES`（如"香港柿柿喜物"→"香港柿柿喜物总部"）
- **映射列**：`duojinbao`

## 5.4 jingjian_crawler.py — 鲸舰

- **入口**：`main(start_date, end_date, username, password)`；**日期特殊处理**：end_date+1 天，营收接口再整体提前 1 天
- **流程**：`login`（双域名：登录 `21359-grabmono...`、数据 `grabmono...`）→ `get_all_shops` → 逐店 `switch_shop`（拿新 token）→ `get_one_shop_data`
- **数据接口**：营收 `/finance/manager/revenueoverview/revenue`（dataXs 末行 realMoney/cashRealMoney + recordPaymentExecutor* 求和 POS 机）、机台 `/device/manager/machineplaylog/getsummarylist`、积分 `/member/manager/memberstore/getstorechangelog`（flowType 1/2）
- **映射列**：`whale_ship`
- **传输安全**：默认拒绝向 HTTP 地址发送账号、密码和 Token；优先配置公司 HTTPS 网关。仅在专线、VPN 或受控内网隔离后，显式设置 `WORKBUDDY_ALLOW_INSECURE_JINGJIAN_HTTP=1` 才启用兼容模式

## 5.5 starthing_crawler.py — StarThing

- **入口**：`main(start_date, end_date, account, password)`；密码 `md5(md5(password)+VERIFY_CODE)` 双重加密
- **流程**：`_login` → `_get_all_stores`（`_walk_orgs` 递归遍历 tenantOrgList，type=4 为门店）→ 逐店 `_get_store_data`（营收 overview + 支付 payment + 币 coinBenefit + 积分 pointBenefit + 机台分页 size=100）→ **弹珠机拆分**：`_get_pinball_payment`/`_split_pinball_payment` 从"香港C店"/"香港C站"拆出"香港B店"独立记录 → 匹配（支持 `_fallback_venue` 兜底）
- **派生**：手续费=非现金×0.014
- **映射列**：`star_thing`

## 5.6 new_system_crawler.py — 新系统（双 API 体系）

- **入口**：`main(start_date, end_date, account, password)`；4 次重试指数退避
- **公共 API**：`PUBLIC_API_URL`，`action=statistics` 查积分增减；失败直接抛错停止
- **CloudBase API**：`CloudBaseClient` — `_signed_headers` 构造 HMAC-SHA256 签名（7 个签名头 + body SHA256）；`invoke` 调云函数；`login`（mobile/email/username）；`query_verified_coupon_orders` 查已核销抵扣券（point_cost 计入积分减少）；`_query_records_by_where` 分页（DCloud-clientDB `$` 命令链）
- **归属兜底**：`_attribute_uncovered_points` 未匹配积分记录 → 店名模糊匹配 → 手机号查商品/抵扣券订单（按主题匹配 + 时间距离排序）
- **凭证**：适配器注入 `CLOUDBASE_CONFIG`（access_key/secret_key）+ 账号/密码；独立运行用环境变量 NEWSYSTEM_*
- **映射列**：`new_system`（支持换行多店，精确+包含匹配）

## 5.7 huilian_crawler.py — 汇联

- **入口**：`main(startTime, endTime, account, password)`
- **流程**：`_login`（`POST /login` 返 token）→ `_get_all_store_data`（`/statsStore/findAllStatsStoreList` + 逐店 `/statsWriteOffServiceFee/findBillingDetailsData` 查手续费）→ 映射 → 聚合；一对一兜底（数据 1 条且映射 1 条时强制匹配）
- **派生**：实收=总收入−手续费；非现金=实收−现金
- **映射列**：`huilian`（支持换行多店）

## 5.8 kpay_crawler.py — KPay（加密最复杂）

- **入口**：`main(start_date, end_date, account, password)`；日期按月分段（`_split_by_month`）
- **加密体系**：`KPayClient` —— 客户端生成 RSA 密钥对提交公钥；密码 AES-CBC 加密（PBKDF2 派生密钥，时间戳取模作迭代数）；请求 RSA-OAEP+SHA1 分块加密 + PKCS1v15+SHA256 签名；响应 `_decrypt_chunks` 分块解密
- **流程**：login → `_get_kpay_merchant_mapping`（DB 商户映射，**校验 9 个 TARGET_VENUES 全部配置否则抛错**）→ `_get_all_merchants` 匹配场地 → 按月 `POST /api/v3/settlement/statistics` 汇总 totalTransactionAmount 与手续费
- **特殊**：收款字段仅对 `TARGET_VENUES`（9 个香港场地）输出；手续费对所有场地；商户名小写去空格归一化匹配
- **映射列**：`kpay`（支持换行多商户）

## 5.9 meituan_download.py — 美团下载（异步多步）

- **入口**：`main(begin_date, end_date, progress_callback=None)`；多账号 `ThreadPoolExecutor` 并行
- **流程**（单账号 `_process_partner`）：本地缓存检查 → `get_shop_id_list` → `request_download` 提交 → `get_latest_download_id` → `get_file_url` **智能渐进式轮询**（5s/10s/20s/30s 退避，总 100 次，超时默认 1800s，config 可至 2400s）→ `download_excel`（存 `data/downloads/meituan/`）→ `get_dict_list`（pandas skiprows=1，按"账户名称"拆店铺，groupby 求和总收入/结算价）
- **凭证**：模块级 `MTGSIGS=[]` / `COOKIES=[]` / `partners_id=[]` 占位，适配器注入（最多 2 组）
- **已知风险**：`mtgsig` 为硬编码签名（含时间戳 a2 字段会失效），失效需手动抓包更新（见 docs/爬虫代码审查报告.md）
- **HTTP**：模块级单例 Session（HTTPAdapter pool 5/10，retries=2）

## 5.10 meituan_match.py — 美团场地匹配

- `main(meituan_data)`：`get_venue_map('meituan')`（utils.mysql_pool，**TTL 缓存**）→ 匹配 → DB 有但美团无的场地**补 0 行**
- 映射列：`meituan_sjxg`、`meituan_sjxg1234`

## 5.11 douyin_download.py — 抖音（适配器实际使用的版本）

- **入口**：`main(start_date, end_date, account=None, password=None)`
- **流程**：`get_auth_headers()` 用本地 Cookie 调 `POST /life/settle/v2/daily_income/classify/`；捕获 `NeedLogin` → `auto_login()`（**Playwright 有头模式**：填手机号/密码/勾协议/点登录，轮询 90s 等 Cookie）→ 重试；`fetch_all_accounts` 对 2 个 `ROOT_LIFE_ACCOUNT_IDS` 分页拉取（分→元换算）
- **Cookie 落盘**：`credentials/douyin/`（cookie_header.txt / cookies.json / storage_state.json / browser_profile/ 完整浏览器指纹目录）
- 注：`douyin_fetch.py` 是功能更完整的独立版（含滑块风控检测 LoginBlocked、完整请求头），凭证仅走环境变量，当前**未被适配器使用**，Cookie 落在 crawlers/ 目录

## 5.12 douyin_match.py — 抖音场地匹配

- `match(data_list)`：查 `douyin_4630` UNION `douyin_2358` 两列构建店铺→场地映射；未匹配且金额非 0 记入 unmatched
- 每次直连 pymysql（未用连接池）

## 5.13 octopus_crawler.py — 八达通（本地 CSV）

- `main(start_date, end_date, file_path=DEFAULT_FILE_PATH)`：读 CSV → `_read_cashier_totals`（列名 NFKC 归一化，按"交易日期"筛选，按"收銀機/收银机"分组求和"金額"）→ 收银机→场地映射 → 按场地汇总 → `_build_result_item`
- **特殊**：仅 `FULL_FIELD_VENUES`（香港B店/香港D店）输出收款字段；其余场地仅手续费（=收款×1.3%）
- 默认路径：`E:\八爪鱼项目配置数据\...\八达通\report.csv`（config 可改）
- 映射列：`Octopus`（支持换行/逗号多收银机名）

## 5.14 payment_crawler.py — 货款（本地 Excel）

- `main(file_path=DEFAULT_FILE_PATH)`：读 Excel → 取 `店名.1` + `求和项:金额` → 清洗（去 nan/空白/总计）→ 匹配聚合
- 默认路径：`C:\公司数据\货款表\月货款表\门店货款数据.xlsx`
- 映射列：`payment`

## 5.15 coin_exchange_crawler.py — 兑币机

- `main(date_str, data=None)`：按 `date_str.day` 截取前 N 天金额求和，返回 `[{"场地", "兑币机收款"}]`（累计口径）
- **三模式**（config `fetch_mode`）：`online` = Playwright 无头读金山文档（名称框跳转起始单元格 → Shift+方向键扩选区 → Ctrl+C → 粘贴隐藏 textarea 读 TSV）；`file` = 读 `data/coin_exchange.json`（八爪鱼导出）；`auto` = 先在线失败回落文件
- 场地：config `venue`（默认"香港A店"）；无 MySQL 交互

## 5.16 report_summary.py — 汇总计算器（被大量复用）

**非采集器**，是所有平台采集结果的汇总透视器，被 dashboard_data / analyst.tools / analyst.forecast 引用。

- `main(data_list) -> List[List]`：返回 2D 数组（表头行+数据行+合计行），列序由 MySQL `table_sequence_config` 定义
- `_get_base_df`（lru_cache）：读 `table_sequence_config`（列定义）+ `company_organizational_structure` INNER JOIN `principal`（负责人）构建基础 DataFrame
- `_merge_input_records`：各平台数据按"场地"填入对应列（列名 NFKC 归一化匹配）
- **派生指标**：`收入汇总`（`INCOME_COLUMNS` 17 列求和，已剔除 Kpay收款——全系统收入口径的唯一定义处）、各平台积分货款、总货款、现金、投币/出币/出货/存货合计、财务比率（存货比率/币值/出货率/货款比%/扣除手续费后）
- 导出常量：`INCOME_COLUMNS`（收入指标清单）被 analyst 引用

---

## 附：company_organizational_structure 列映射总表（全部只读）

| 平台 | 爬虫文件 | 映射列 | 多值支持 |
|------|----------|--------|----------|
| 美团 | meituan_match.py（经 utils 缓存） | `meituan_sjxg` + `meituan_sjxg1234` | — |
| 抖音 | douyin_match.py | `douyin_4630` + `douyin_2358` | — |
| 多金宝 | duojinbao_crawler.py | `duojinbao` | — |
| 汇联 | huilian_crawler.py | `huilian` | 换行多店 |
| 鲸舰 | jingjian_crawler.py | `whale_ship` | — |
| KPay | kpay_crawler.py | `kpay` | 换行多商户（小写归一化） |
| 乐摇摇 | leyaoyao_crawler.py | `leyaoyao` | 换行多组 |
| 新系统 | new_system_crawler.py | `new_system` | 换行多店（精确+模糊） |
| 八达通 | octopus_crawler.py | `Octopus` | 换行/逗号多收银机 |
| 货款 | payment_crawler.py | `payment` | — |
| StarThing | starthing_crawler.py | `star_thing` | 换行多店 |
| 芸苔 | yuntai_crawler.py | `yuntai` + `yuntai2` | 两列均换行多店 |
| 汇总 | report_summary.py | `venue`（+ person_in_charge_id 关联 principal；列序 table_sequence_config） | — |
