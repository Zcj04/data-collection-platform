# -*- coding: utf-8 -*-
"""断线重连失败不能永久占用连接池容量。"""

from unittest import mock

import pytest

from utils.mysql_pool import MySQLConnectionPool


CONFIG = {"host": "mock", "user": "mock", "password": "mock", "database": "mock"}


class Connection:
    def __init__(self, broken=False):
        self.broken = broken
        self.closed = False

    def ping(self, reconnect=True):
        if self.broken:
            raise RuntimeError("disconnected")

    def close(self):
        self.closed = True


def test_failed_replacement_releases_capacity_and_recovers():
    broken, recovered = Connection(True), Connection()
    with mock.patch("utils.mysql_pool.pymysql.connect", side_effect=[broken, RuntimeError("offline"), recovered]):
        pool = MySQLConnectionPool(CONFIG, pool_size=1, max_overflow=0)
        with pytest.raises(RuntimeError, match="offline"):
            pool.get_connection(timeout=.001)
        assert broken.closed
        assert pool._created == 0
        assert pool.get_connection(timeout=.001) is recovered
        assert pool._created == 1
        pool.release_connection(recovered)
        pool.close_all()
        assert pool._created == 0


def test_repeated_replacements_do_not_inflate_default_pool_count():
    with mock.patch("utils.mysql_pool.pymysql.connect", side_effect=lambda **kw: Connection(True)):
        pool = MySQLConnectionPool(CONFIG, pool_size=5, max_overflow=3)
        for _ in range(10):
            conn = pool.get_connection(timeout=.001)
            pool.release_connection(conn)
            assert pool._created == 5
        pool.close_all()
        assert pool._created == 0


def test_failed_initialization_closes_already_created_connections():
    first = Connection()
    with mock.patch("utils.mysql_pool.pymysql.connect", side_effect=[first, RuntimeError("offline")]):
        with pytest.raises(RuntimeError, match="offline"):
            MySQLConnectionPool(CONFIG, pool_size=2, max_overflow=0)
    assert first.closed


def test_close_all_keeps_checked_out_connections_counted():
    with mock.patch("utils.mysql_pool.pymysql.connect", side_effect=lambda **kw: Connection()):
        pool = MySQLConnectionPool(CONFIG, pool_size=2, max_overflow=0)
        checked_out = pool.get_connection(timeout=.001)
        pool.close_all()
        assert pool._created == 1
        pool.release_connection(checked_out)
        pool.close_all()
        assert pool._created == 0
