# -*- coding: utf-8 -*-
"""SQLite 连接的并发写入等待策略。"""

import sqlite3
import threading
import time
from unittest import mock

import core.db as db


def test_connections_use_explicit_busy_timeout(tmp_path):
    database = tmp_path / "app.db"
    with mock.patch.object(db, "DB_PATH", str(database)):
        conn = db.get_connection()
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        finally:
            conn.close()


def test_writer_waits_for_a_short_immediate_transaction(tmp_path):
    database = tmp_path / "app.db"
    with mock.patch.object(db, "DB_PATH", str(database)):
        bootstrap = db.get_connection()
        try:
            bootstrap.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT)")
            bootstrap.commit()
        finally:
            bootstrap.close()

        first_writer_started = threading.Event()
        results = []

        def first_writer():
            conn = db.get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                first_writer_started.set()
                conn.execute("INSERT INTO events(value) VALUES ('first')")
                time.sleep(0.25)
                conn.commit()
            finally:
                conn.close()

        def second_writer():
            assert first_writer_started.wait(2)
            conn = db.get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO events(value) VALUES ('second')")
                conn.commit()
                results.append("ok")
            finally:
                conn.close()

        threads = [threading.Thread(target=first_writer), threading.Thread(target=second_writer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert all(not thread.is_alive() for thread in threads)
        assert results == ["ok"]
        check = sqlite3.connect(database)
        try:
            assert check.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        finally:
            check.close()
