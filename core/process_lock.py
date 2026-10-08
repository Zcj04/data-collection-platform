# -*- coding: utf-8 -*-
"""Prevent multiple web processes from mutating the same SQLite task state."""

import atexit
import os
from typing import Dict, IO


_LOCK_HANDLES: Dict[str, IO[bytes]] = {}


def _lock_file(handle: IO[bytes]) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: IO[bytes]) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_single_process_lock(database_path: str) -> str:
    """Lock one byte beside the database before startup performs any writes."""
    normalized_db = os.path.normcase(os.path.abspath(database_path))
    lock_path = normalized_db + ".single-process.lock"
    if lock_path in _LOCK_HANDLES:
        return lock_path

    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        if os.path.getsize(lock_path) == 0:
            handle.write(b"0")
            handle.flush()
        _lock_file(handle)
    except OSError as error:
        handle.close()
        raise RuntimeError(
            "同一 SQLite 数据库只允许一个 Web 进程（single worker）；"
            "请关闭 uvicorn workers/reload，并确认没有旧服务仍在运行。"
        ) from error

    _LOCK_HANDLES[lock_path] = handle
    return lock_path


def release_single_process_lock(database_path: str) -> None:
    """Release a lock explicitly; production normally relies on process exit."""
    lock_path = os.path.normcase(os.path.abspath(database_path)) + ".single-process.lock"
    handle = _LOCK_HANDLES.pop(lock_path, None)
    if handle is None:
        return
    try:
        _unlock_file(handle)
    finally:
        handle.close()


def is_single_process_lock_held(database_path: str) -> bool:
    """Return whether this process currently owns the database lock."""
    lock_path = os.path.normcase(os.path.abspath(database_path)) + ".single-process.lock"
    handle = _LOCK_HANDLES.get(lock_path)
    return handle is not None and not handle.closed


def _release_all() -> None:
    for lock_path, handle in list(_LOCK_HANDLES.items()):
        try:
            _unlock_file(handle)
        finally:
            handle.close()
            _LOCK_HANDLES.pop(lock_path, None)


atexit.register(_release_all)
