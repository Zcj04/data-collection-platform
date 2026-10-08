# -*- coding: utf-8 -*-
"""
MySQL 连接池与场地映射缓存

优化点：
- 连接池：预创建连接复用，避免每次查询都新建/销毁TCP连接
- 场地映射缓存：避免所有爬虫重复查询 company_organizational_structure 表
- 线程安全：queue.Queue + threading.Lock

用法：
    from utils.mysql_pool import get_pool, get_venue_map

    conn = get_pool().get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT ...")
        result = cursor.fetchall()
    finally:
        get_pool().release_connection(conn)

    # 场地映射缓存
    venue_map = get_venue_map()  # 自动从缓存或 DB 加载
"""

import threading
import time
from queue import Queue, Empty, Full
from typing import Optional

import pymysql

from core.config import get_mysql_config, get as config_get
from utils.mapping import add_unique_mapping

POOL_SIZE = config_get("mysql_pool.pool_size", 5)
POOL_MAX_OVERFLOW = config_get("mysql_pool.max_overflow", 3)
CACHE_TTL = config_get("cache.venue_map_ttl", 3600)


class MySQLConnectionPool:
    """轻量级 MySQL 连接池（无需 DBUtils 依赖）"""

    def __init__(self, config: Optional[dict] = None, pool_size: int = POOL_SIZE,
                 max_overflow: int = POOL_MAX_OVERFLOW):
        self.config = config or get_mysql_config()
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self._pool = Queue(maxsize=pool_size + max_overflow)
        self._created = 0
        # 用可重入锁：get_connection 的空池分支会在持锁状态下创建连接，
        # 普通 Lock 会造成永久死锁（任务卡在"进行中"）
        self._lock = threading.RLock()

        # 预创建连接
        try:
            for _ in range(pool_size):
                self._pool.put(self._create_connection())
        except Exception:
            self.close_all()
            raise

    def _create_connection(self):
        """创建新的 MySQL 连接"""
        with self._lock:
            self._created += 1
        try:
            conn = pymysql.connect(
                host=self.config['host'],
                port=self.config.get('port', 3306),
                user=self.config['user'],
                password=self.config['password'],
                database=self.config['database'],
                charset=self.config.get('charset', 'utf8mb4'),
                connect_timeout=self.config.get('connect_timeout', 10),
                read_timeout=self.config.get('read_timeout', 30),
                write_timeout=self.config.get('write_timeout', 30),
                autocommit=True,
            )
            return conn
        except Exception as e:
            with self._lock:
                self._created -= 1
            raise e

    def get_connection(self, timeout: float = 5.0):
        """
        从连接池获取连接

        Args:
            timeout: 等待超时秒数
        Returns:
            pymysql.Connection
        """
        try:
            conn = self._pool.get(timeout=timeout)
        except Empty:
            # 池空，尝试创建溢出连接
            with self._lock:
                if self._created < self.pool_size + self.max_overflow:
                    return self._create_connection()
            # 再试一次从池中获取
            conn = self._pool.get(timeout=timeout)

        try:
            conn.ping(reconnect=True)
        except Exception:
            # 将释放旧名额与预留替换名额放在同一锁内，避免并发溢出连接抢占容量。
            with self._lock:
                self._discard_connection(conn)
                conn = self._create_connection()
        return conn

    def _discard_connection(self, conn):
        """连接失效或关闭时归还容量，即使 close 本身失败也不占用名额。"""
        try:
            conn.close()
        except Exception:
            pass
        finally:
            with self._lock:
                self._created -= 1

    def release_connection(self, conn):
        """归还连接到池"""
        if conn is None:
            return
        try:
            # 如果池未满，放回；否则关闭
            self._pool.put_nowait(conn)
        except Full:
            self._discard_connection(conn)

    def close_all(self):
        """关闭空闲连接；借出的连接仍占用容量名额。"""
        while True:
            try:
                conn = self._pool.get_nowait()
            except Empty:
                break
            self._discard_connection(conn)


class VenueMapCache:
    """
    场地映射缓存（线程安全，带 TTL）

    美团/抖音/芸苔等所有平台都需要查询 company_organizational_structure 表
    获取场地到平台店铺名的映射，加入缓存避免重复查询
    """

    def __init__(self, pool: MySQLConnectionPool, ttl: int = CACHE_TTL):
        self._pool = pool
        self._ttl = ttl
        self._cache = {}
        self._timestamps = {}
        self._lock = threading.Lock()

    def get_venue_map(self, platform: str = None):
        """
        获取场地映射

        Args:
            platform: 平台名（如 'meituan', 'douyin'），None 则返回所有
        Returns:
            dict: {shop_name: venue} 或 {platform: {shop_name: venue}}
        """
        now = time.time()
        with self._lock:
            # 检查缓存是否过期
            if 'all' in self._cache and (now - self._timestamps.get('all', 0)) < self._ttl:
                result = self._cache['all']
                return result.get(platform) if platform else result

        # 缓存过期或不存在：在锁外执行 DB 加载，避免慢查询阻塞所有调用
        venue_map = self._load_from_db()
        with self._lock:
            self._cache['all'] = venue_map
            self._timestamps['all'] = time.time()
            return venue_map.get(platform) if platform else venue_map

    def _load_from_db(self):
        """从数据库加载所有平台的场地映射"""
        conn = self._pool.get_connection()
        venue_map = {}
        try:
            cursor = conn.cursor()

            # 查询场地与各类平台店铺名的映射列
            # 美团
            cursor.execute("""
                SELECT venue, meituan_sjxg AS col_value
                FROM company_organizational_structure
                WHERE meituan_sjxg IS NOT NULL AND meituan_sjxg != ''
                UNION
                SELECT venue, meituan_sjxg1234 AS col_value
                FROM company_organizational_structure
                WHERE meituan_sjxg1234 IS NOT NULL AND meituan_sjxg1234 != ''
            """)
            mapping = {}
            ambiguous = set()
            for venue, col_value in cursor.fetchall():
                add_unique_mapping(mapping, ambiguous, col_value, venue, "meituan")
            venue_map['meituan'] = mapping

            # 抖音
            cursor.execute("""
                SELECT venue, douyin_4630 AS col_value
                FROM company_organizational_structure
                WHERE douyin_4630 IS NOT NULL AND douyin_4630 != ''
                UNION
                SELECT venue, douyin_2358 AS col_value
                FROM company_organizational_structure
                WHERE douyin_2358 IS NOT NULL AND douyin_2358 != ''
            """)
            mapping = {}
            ambiguous = set()
            for venue, col_value in cursor.fetchall():
                add_unique_mapping(mapping, ambiguous, col_value, venue, "douyin")
            venue_map['douyin'] = mapping

        finally:
            self._pool.release_connection(conn)

        return venue_map

    def invalidate(self):
        """主动使缓存失效"""
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()

    def get_meituan_map(self):
        """获取美团店铺名 -> 场地 映射（便捷方法）"""
        return self.get_venue_map('meituan') or {}

    def get_douyin_map(self):
        """获取抖音店铺名 -> 场地 映射（便捷方法）"""
        return self.get_venue_map('douyin') or {}


# ===================== 全局单例 =====================
_pool_instance = None
_cache_instance = None
_lock = threading.Lock()
_query_cache = {}
_query_cache_lock = threading.Lock()


def get_pool() -> MySQLConnectionPool:
    """获取全局连接池单例"""
    global _pool_instance
    if _pool_instance is None:
        with _lock:
            if _pool_instance is None:
                _pool_instance = MySQLConnectionPool()
    return _pool_instance


def fetch_all(sql: str, args=None):
    """使用全局连接池执行只读查询并返回元组列表。"""
    pool = get_pool()
    conn = pool.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, args or ())
            return cursor.fetchall()
    finally:
        pool.release_connection(conn)


def fetch_all_cached(sql: str, args=None, ttl: int = CACHE_TTL):
    """执行只读查询并按 SQL/参数缓存结果，默认缓存 1 小时。"""
    normalized_args = tuple(args or ())
    key = (sql, normalized_args)
    now = time.time()
    with _query_cache_lock:
        cached = _query_cache.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]

    rows = fetch_all(sql, normalized_args)
    with _query_cache_lock:
        _query_cache[key] = (time.time(), rows)
    return rows


def invalidate_query_cache() -> None:
    """清空只读查询缓存；映射配置变更后可主动调用。"""
    with _query_cache_lock:
        _query_cache.clear()


def get_cache() -> VenueMapCache:
    """获取全局场地映射缓存单例"""
    global _cache_instance
    if _cache_instance is None:
        # 先建池（get_pool 自行加锁），再在锁内建缓存，避免嵌套获取模块锁导致死锁
        pool = get_pool()
        with _lock:
            if _cache_instance is None:
                _cache_instance = VenueMapCache(pool)
    return _cache_instance


def get_venue_map(platform: str = None) -> dict:
    """便捷方法：获取场地映射（带缓存）"""
    return get_cache().get_venue_map(platform)


def invalidate_cache():
    """便捷方法：使所有缓存失效"""
    get_cache().invalidate()
    invalidate_query_cache()
