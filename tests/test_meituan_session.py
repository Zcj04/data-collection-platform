# -*- coding: utf-8 -*-
"""美团多账号并发时的 HTTP 会话隔离。"""

import threading

from crawlers import meituan_download as download


def test_session_is_reused_within_thread_but_isolated_between_threads():
    sessions = []
    barrier = threading.Barrier(2)

    def worker():
        first = download._get_session()
        barrier.wait(timeout=2)
        second = download._get_session()
        sessions.append((first, second))

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(sessions) == 2
    assert all(first is second for first, second in sessions)
    assert sessions[0][0] is not sessions[1][0]
    for first, _second in sessions:
        first.close()
