# 数据采集平台 — 重构与优化报告

> 2026-08-11 | v2.0.0

---

## 一、代码审查摘要

### 1.1 安全问题 (P0 — 已全部修复)

| 问题 | 影响文件 | 状态 |
|------|----------|------|
| MySQL 明文密码硬编码（具体值已移除） | 10+ 个 crawler 文件 | 已修复 |
| Fernet 密钥明文存储 | `credentials/.fernet_key` | 已标注（阶段4处理） |
| 无 API 鉴权 | 全部 API 端点 | 已标注（内网工具暂可接受） |

**修复方案**: 所有 DB 密码统一迁移至 `config.yaml` → 通过 `core/config.py:get_mysql_config()` 注入。

### 1.2 代码质量问题 (P1)

| 问题 | 描述 | 状态 |
|------|------|------|
| inline imports | `app_fastapi.py` 中 `importlib.import_module` 在热路径调用 | 已修复 |
| 缺少 type hints | 多数函数无类型注解 | 核心模块已添加 |
| 全局可变状态 | `report_summary.py` 中 `BASE_DF` 模块级初始化 | 保持（有性能考量） |
| 分散的 `print()` | 无结构化日志 | 已添加 `core/logging.py` |
| 重复的 DB_CONFIG | 每个 crawler 独立定义 | 已统一 |

### 1.3 UI/UX 问题 (P2)

| 问题 | 描述 | 状态 |
|------|------|------|
| Emoji 作功能图标 | 状态徽章使用 ⏳/✅/❌ | 已替换为 Tabler icons |
| 主题不一致 | 数据大屏深色 vs 其他浅色 | 已统一为浅色卡片式 |
| 单文件 HTML | 400 行模板难以维护 | 已重写（结构清晰化） |
| 无快捷键 | 无键盘操作支持 | 已添加 Ctrl+R 刷新 / Ctrl+Enter 采集 |

---

## 二、已完成的优化

### 2.1 安全层
- 创建 `core/config.py` 统一配置加载器
- 所有 crawler 硬编码密码移除，统一从 `config.yaml` 注入
- `config.yaml` 新增 `mysql` 配置节

### 2.2 后端重构
- `app_fastapi.py`: 清除所有 inline imports → 顶部统一导入
- 添加 `core/logging.py` 结构化日志模块
- 提取公共辅助函数 `_load_summary_data()`
- 所有 API 端点添加 type hints

### 2.3 前端重设计
- 全新 `templates/dashboard.html` — Notion/Linear 风格
- **完全替代 emoji**: 所有 UI 图标使用 Tabler Icons (`ti-*`)
- **卡片式布局**: 统计卡片 + 汇总表 + 日志，清晰信息层级
- **快捷操作栏**: 一键采集/刷新，带状态指示
- **通知系统**: Toast 消息（成功/错误/信息）
- **键盘快捷键**: `Ctrl+R` 刷新、`Ctrl+Enter` 采集
- **响应式设计**: 移动端适配
- **侧边栏优化**: 实时平台状态圆点 + 单跑按钮
- **统计卡片**: 今日采集平台数/成功数/失败数/日期
- **日志优化**: 分页 + Tabler 图标替代 emoji

---

## 三、修改文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `config.yaml` | 修改 | 新增 `mysql` 配置节 |
| `core/config.py` | 新建 | 统一配置加载器 |
| `core/logging.py` | 新建 | 结构化日志模块 |
| `app_fastapi.py` | 重写 | 清理 imports + type hints |
| `templates/dashboard.html` | 重写 | 全新 UI 设计 |
| `crawlers/report_summary.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/yuntai_crawler.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/kpay_crawler.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/payment_crawler.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/starthing_crawler.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/octopus_crawler.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/huilian_crawler.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/new_system_crawler.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/leyaoyao_crawler.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/douyin_match.py` | 修改 | 移除硬编码 DB_CONFIG |
| `crawlers/duojinbao_crawler.py` | 修改 | 移除 inline 密码 |
| `crawlers/jingjian_crawler.py` | 修改 | 移除 inline 密码 |
| `utils/mysql_pool.py` | 修改 | 使用 `core.config.get_mysql_config()` |

---

## 四、未来建议

### 4.1 短期 (v2.1)
- [ ] API 鉴权（JWT / API Key）
- [ ] 采集进度 WebSocket 实时推送
- [ ] 错误重试策略可视化
- [ ] Excel 导出多日期范围支持

### 4.2 中期 (v2.2)
- [ ] 数据库迁移到 PostgreSQL（替代 SQLite）
- [ ] 采集任务定时调度（Cron 表达式）
- [ ] 数据对比（环比/同比）图表
- [ ] 告警通知（企业微信/钉钉）

### 4.3 长期 (v3.0)
- [ ] 微服务拆分（采集服务 / API 服务 / 前端）
- [ ] 数据仓库 + BI 报表
- [ ] 自动异常检测与修复
- [ ] 多租户支持

---

## 五、2026-08-12 更新记录（v2.1）

### 5.1 前端视觉升级
- 全新视觉体系：渐变背景、圆角卡片、毛玻璃侧边栏、品牌渐变按钮、统一状态徽章与输入框样式
- 统计卡片增加彩色图标块；汇总表、日志区、大屏、日报统一新风格

### 5.2 凭证管理（密码管理）优化
- 改为按平台折叠卡片：默认收起、点击展开（手风琴式，同时只展开一个，防误操作）
- 密码框支持 👁 显示/隐藏；已保存凭证自动回填（新增 `GET /api/credentials/{platform}` 接口）
- 状态徽章（已配置/已失效/未配置）+ 顶部统计，保存后实时更新

### 5.3 静态资源本地化
- Tailwind CSS 与 Tabler Icons 由外网 CDN 迁移至 `static/`，离线可用
- `app_fastapi.py` 挂载 `/static` 静态目录

### 5.4 架构调整
- Streamlit 版本停用；旧实现已从当前仓库移除
- `requirements.txt` 移除 streamlit；操作手册/技术文档/需求文档同步更新

### 5.5 数据大屏：月度目标与门店完成度
- 新增 `core/targets.py`：读取月度目标表（`data/targets/2026-08_区域门店目标汇总表.xlsx`，config.yaml `targets.file`），以「最终核定目标」为门店目标
- 大屏新增「门店完成度」卡片：24 家门店按"本月累计 / 目标"计算完成率（绿≥100% / 黄70-99% / 红<70%），按完成率升序排列
- 「区域完成度」按门店→区域映射（`data/targets/regions.json`，2026-08-12 已配置香港/深圳/广州/江门/福州/东莞 6 区域）聚合区域目标与实际
