# -*- coding: utf-8 -*-
"""SQLite 数据库初始化与连接"""

import sqlite3
import os
from datetime import datetime
from typing import Optional


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_PROJECT_ROOT, "data", "app.db")
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _adapt_datetime(value: datetime) -> str:
    """显式保留 SQLite 现有的本地时间字符串格式，兼容 Python 3.12+。"""
    return value.isoformat(sep=" ", timespec="seconds")


sqlite3.register_adapter(datetime, _adapt_datetime)


def get_connection() -> sqlite3.Connection:
    """获取数据库连接（每次新建，SQLite轻量）"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=%d" % SQLITE_BUSY_TIMEOUT_MS)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """为已有本地库补充新字段；新库由 CREATE TABLE 直接创建。"""
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    }
    if column not in columns:
        conn.execute(
            "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition)
        )


def init_db():
    """初始化数据库表"""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript("""
            -- 任务状态表
            CREATE TABLE IF NOT EXISTS tasks (
                id           TEXT PRIMARY KEY,
                platform     TEXT NOT NULL,
                date         TEXT NOT NULL,
                start_date   TEXT,
                venue        TEXT DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending',
                step         TEXT,
                progress     INTEGER DEFAULT 0,
                started_at   TIMESTAMP,
                finished_at  TIMESTAMP,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                retry_count  INTEGER DEFAULT 0,
                error_msg    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(date);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

            -- 平台批量采集开关；没有记录的平台默认开启，兼容已有数据库。
            CREATE TABLE IF NOT EXISTS platform_collection_settings (
                platform   TEXT PRIMARY KEY,
                enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 货款数据导入表（按日期存放用户上传的货款明细，采集时优先读取）
            CREATE TABLE IF NOT EXISTS payment_imports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                shop_name   TEXT NOT NULL,
                amount      REAL NOT NULL DEFAULT 0,
                source_file TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, shop_name)
            );
            CREATE INDEX IF NOT EXISTS idx_payment_imports_date
                ON payment_imports(date);

            CREATE TABLE IF NOT EXISTS payment_import_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('replace', 'delete')),
                actor_user_id TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_payment_history_date
                ON payment_import_history(date, id);

            -- 进场货款修订记录。每次保存追加一个版本，历史金额不被覆盖。
            CREATE TABLE IF NOT EXISTS entry_payment_revisions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                venue         TEXT NOT NULL,
                revision      INTEGER NOT NULL,
                entry_date    TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL CHECK (amount_cents >= 0),
                note          TEXT NOT NULL DEFAULT '',
                actor_user_id TEXT NOT NULL DEFAULT '',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(venue, revision)
            );
            CREATE INDEX IF NOT EXISTS idx_entry_payment_venue_revision
                ON entry_payment_revisions(venue, revision DESC);

            -- 汇总数据表（长表：date+venue+platform唯一）
            CREATE TABLE IF NOT EXISTS daily_summary (
                id            TEXT PRIMARY KEY,
                date          TEXT NOT NULL,
                venue         TEXT NOT NULL,
                platform      TEXT NOT NULL,
                metrics_json  TEXT NOT NULL,
                raw_file      TEXT,
                period_start  TEXT,
                source_task_id TEXT,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, venue, platform)
            );
            CREATE INDEX IF NOT EXISTS idx_summary_date ON daily_summary(date);

            -- 门店生命周期。日期边界用于历史经营页面，开业日和闭店日均计入营业期。
            CREATE TABLE IF NOT EXISTS venue_lifecycle (
                venue       TEXT PRIMARY KEY,
                opened_on   TEXT,
                closed_on   TEXT,
                updated_by  TEXT NOT NULL DEFAULT '',
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (opened_on IS NULL OR length(opened_on) = 10),
                CHECK (closed_on IS NULL OR length(closed_on) = 10),
                CHECK (opened_on IS NULL OR closed_on IS NULL OR opened_on <= closed_on)
            );

            -- 经营数据自动采集批次。job_key 对每个排程日期唯一，避免服务重启
            -- 或多进程同时检查时重复触发同一个外部采集任务。
            CREATE TABLE IF NOT EXISTS scheduled_collection_runs (
                id              TEXT PRIMARY KEY,
                job_key         TEXT NOT NULL UNIQUE,
                slot_key        TEXT NOT NULL,
                slot_label      TEXT NOT NULL,
                target_date     TEXT NOT NULL,
                scheduled_for   TIMESTAMP NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                total_tasks     INTEGER NOT NULL DEFAULT 0,
                success_count   INTEGER NOT NULL DEFAULT 0,
                failed_count    INTEGER NOT NULL DEFAULT 0,
                retry_count     INTEGER NOT NULL DEFAULT 0,
                started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at     TIMESTAMP,
                message         TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_collection_runs_time
                ON scheduled_collection_runs(scheduled_for DESC);
            CREATE INDEX IF NOT EXISTS idx_scheduled_collection_runs_status
                ON scheduled_collection_runs(status);

            -- 会员资产变更事件（积分 / 储值）。爬虫完成后只需按统一契约写入此表。
            CREATE TABLE IF NOT EXISTS monitor_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source         TEXT NOT NULL,
                external_id    TEXT NOT NULL,
                occurred_at    TIMESTAMP NOT NULL,
                venue          TEXT DEFAULT '',
                member_ref     TEXT DEFAULT '',
                event_type     TEXT NOT NULL,
                amount         REAL NOT NULL DEFAULT 0,
                balance_after  REAL,
                operator       TEXT DEFAULT '',
                raw_json       TEXT DEFAULT '{}',
                collected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_monitor_events_time
                ON monitor_events(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_monitor_events_type
                ON monitor_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_monitor_events_venue
                ON monitor_events(venue);

            -- 监控数据源同步记录。即使某个日期没有流水，也能区分“真实空数据”和演示模式。
            CREATE TABLE IF NOT EXISTS monitor_sync_runs (
                id               TEXT PRIMARY KEY,
                source           TEXT NOT NULL,
                category         TEXT NOT NULL,
                start_date       TEXT NOT NULL,
                end_date         TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'running',
                stores_total     INTEGER DEFAULT 0,
                stores_succeeded INTEGER DEFAULT 0,
                event_count      INTEGER DEFAULT 0,
                error_count      INTEGER DEFAULT 0,
                error_msg        TEXT DEFAULT '',
                started_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at      TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_monitor_sync_source_time
                ON monitor_sync_runs(source, started_at DESC);

            -- 凭证元信息表
            CREATE TABLE IF NOT EXISTS credentials (
                platform        TEXT PRIMARY KEY,
                encrypted_value TEXT NOT NULL,
                status          TEXT DEFAULT 'unknown',
                last_check_at   TIMESTAMP,
                last_success_at TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- AI 分析师会话与消息
            CREATE TABLE IF NOT EXISTS analyst_sessions (
                id         TEXT PRIMARY KEY,
                title      TEXT DEFAULT '新对话',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS analyst_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_analyst_messages_session
                ON analyst_messages(session_id);

            -- 企业内部账号与登录会话。密码只保存带盐哈希，浏览器仅持有随机会话令牌。
            CREATE TABLE IF NOT EXISTS users (
                id               TEXT PRIMARY KEY,
                username         TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name     TEXT NOT NULL,
                position         TEXT NOT NULL DEFAULT '',
                role_key         TEXT NOT NULL DEFAULT 'custom',
                password_hash    TEXT NOT NULL,
                permissions_json TEXT NOT NULL DEFAULT '[]',
                scope_type       TEXT NOT NULL DEFAULT 'all',
                venue_scope_json TEXT NOT NULL DEFAULT '[]',
                is_active        INTEGER NOT NULL DEFAULT 1,
                last_login_at    TIMESTAMP,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (is_active IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                csrf_token TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                ON auth_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry
                ON auth_sessions(expires_at);

            -- 登录失败限流。只保存来源地址与账号组合的哈希，不保存原始 IP。
            CREATE TABLE IF NOT EXISTS auth_login_attempts (
                scope_key      TEXT PRIMARY KEY,
                failed_count   INTEGER NOT NULL DEFAULT 0,
                window_started TIMESTAMP NOT NULL,
                blocked_until  TIMESTAMP,
                updated_at     TIMESTAMP NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_login_attempts_updated
                ON auth_login_attempts(updated_at);

            -- 运维告警 outbox。故障先落本地，再由后台线程异步投递 webhook。
            CREATE TABLE IF NOT EXISTS alert_events (
                id              TEXT PRIMARY KEY,
                event_type      TEXT NOT NULL,
                severity        TEXT NOT NULL,
                title           TEXT NOT NULL,
                message         TEXT NOT NULL DEFAULT '',
                dedupe_key      TEXT NOT NULL UNIQUE,
                status          TEXT NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT NOT NULL DEFAULT '',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at         TIMESTAMP,
                next_attempt_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_alert_events_delivery
                ON alert_events(status, next_attempt_at, created_at);

        """)
        _ensure_column(conn, "tasks", "start_date", "TEXT")
        _ensure_column(conn, "daily_summary", "period_start", "TEXT")
        _ensure_column(conn, "daily_summary", "source_task_id", "TEXT")
        _ensure_column(conn, "users", "scope_type", "TEXT NOT NULL DEFAULT 'all'")
        _ensure_column(conn, "users", "venue_scope_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "users", "position", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "users", "role_key", "TEXT NOT NULL DEFAULT 'custom'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_scope_type ON users(scope_type)"
        )
        from core.outbound_payments import SCHEMA as outbound_schema
        conn.executescript(outbound_schema)
        conn.commit()
        print(f"数据库初始化完成：{os.path.abspath(DB_PATH)}")
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
