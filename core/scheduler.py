# -*- coding: utf-8 -*-
"""
并行调度器 v2（优化版）

优化点：
- 单任务超时：每个平台独立超时（默认 600s），不再无限等待
- 完成即保存：as_completed 逐个处理，不因一个慢任务阻塞所有结果
- Future 取消：pool.shutdown(cancel_futures=True) 取消未完成任务
- 进度回调：支持 progress_callback 实时上报状态
- 日志增强：记录每个任务耗时
"""

import threading
import time
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

import requests

from core.task_manager import TaskManager
from core.logging import get_logger
from core.config import get as config_get
from adapters.base import CrawlerAdapter

logger = get_logger("scheduler")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AdapterProcessTimeout(RuntimeError):
    """适配器隔离进程超过平台时限。"""


class Scheduler:
    """并行采集调度器

    并发模型：
        ThreadPoolExecutor 并行执行各平台适配器
        每个平台有独立超时，完成即保存，不等所有任务
    """

    _lock = threading.Lock()
    _busy = False

    def __init__(self, task_mgr: TaskManager = None, max_workers: int = 12,
                 single_task_timeout: int = 600, max_retries: int = None,
                 process_isolation: bool = False):
        """
        Args:
            task_mgr: 任务管理器实例
            max_workers: 线程池最大工作线程数
            single_task_timeout: 单个平台采集超时秒数（默认 10 分钟）
            max_retries: 临时网络错误最大重试次数；None 时读取 scheduler.max_retries
        """
        self.task_mgr = task_mgr or TaskManager()
        self.max_workers = max_workers
        self.single_task_timeout = single_task_timeout
        self.process_isolation = bool(process_isolation)
        if max_retries is None:
            max_retries = config_get("scheduler.max_retries", 0)
        try:
            self.max_retries = max(0, int(max_retries))
        except (TypeError, ValueError):
            self.max_retries = 0
        self._progress_callback: Optional[Callable] = None

    @staticmethod
    def _is_retryable_error(error: Exception) -> bool:
        """仅判断临时网络错误；凭证/映射/数据格式错误不重试。"""
        if isinstance(error, AdapterProcessTimeout):
            return False
        if isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
        if isinstance(error, requests.exceptions.HTTPError):
            response = getattr(error, "response", None)
            return bool(response is not None and response.status_code in (429, 500, 502, 503, 504))
        text = str(error).lower()
        transient_markers = (
            "timeout", "timed out", "超时", "connection reset", "连接重置",
            "connection refused", "连接被拒绝", "temporarily unavailable",
            "service unavailable", "status code: 429", "status code: 5",
        )
        return any(marker in text for marker in transient_markers)

    def set_progress_callback(self, callback: Optional[Callable]):
        """设置进度回调函数 callback(platform: str, status: str, detail: str)"""
        self._progress_callback = callback

    def _resolve_timeout(self, platform: str) -> int:
        """单平台任务超时：优先取 config 里 platforms.<name>.task_timeout，否则用全局默认。
        注意：平台自身的 HTTP 请求超时字段是 timeout，不能混用。"""
        try:
            return int(
                config_get(
                    "platforms.{}.task_timeout".format(platform),
                    self.single_task_timeout,
                )
            )
        except (TypeError, ValueError):
            return self.single_task_timeout

    def run_range(
        self,
        start_date: str,
        end_date: str,
        adapters: List[CrawlerAdapter],
    ) -> Dict[str, int]:
        """
        并行执行所有平台采集，完成即保存

        关键优化：as_completed 逐个处理，单个任务超时后放弃继续下一个，
        不因美团等慢平台阻塞所有其他平台的结果写入。

        """
        # 任务清单：[(adapter, start, end, tid)]
        jobs = []
        for adapter in adapters:
            tid = self.task_mgr.create(
                adapter.platform_name,
                end_date,
                start_date=start_date,
            )
            jobs.append((adapter, start_date, end_date, tid))

        total = len(jobs)
        completed = 0
        failed = 0
        start_time = time.time()

        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        futures = {}

        try:
            # 提交所有任务
            for adapter, _s, _e, tid in jobs:
                fut = pool.submit(self._run_one, adapter, _s, _e, tid)
                futures[fut] = (adapter.platform_name, tid)

            # 每个任务独立的超时截止时间（提交时刻 + 平台超时）
            timeouts = {
                fut: self._resolve_timeout(platform)
                for fut, (platform, _tid) in futures.items()
            }
            deadlines = {
                fut: time.time() + timeouts[fut]
                for fut in futures
            }
            pending = set(futures)

            while pending:
                now = time.time()
                earliest = min(deadlines[fut] for fut in pending)

                # 到达截止时间的任务按超时处理（线程无法强杀，但不再等待其结果）
                if earliest <= now:
                    for fut in [
                        f for f in pending if deadlines[f] <= now
                    ]:
                        platform, tid = futures[fut]
                        failed += 1
                        if hasattr(self.task_mgr, "request_timeout"):
                            self.task_mgr.request_timeout(tid, timeouts[fut])
                        else:
                            self.task_mgr.mark_failed(
                                tid,
                                "超时 {}s".format(timeouts[fut]),
                            )
                        logger.warning(
                            "[Scheduler] %s 超时（%ss），已请求停止并等待底层调用释放",
                            platform,
                            timeouts[fut],
                        )
                        if self._progress_callback:
                            self._progress_callback(
                                platform,
                                "timeout",
                                "超时 {}s".format(timeouts[fut]),
                            )
                    pending = {
                        f for f in pending if deadlines[f] > now
                    }
                    continue

                done, _ = wait(
                    pending,
                    timeout=earliest - now,
                    return_when=FIRST_COMPLETED,
                )
                for fut in done:
                    platform, tid = futures[fut]
                    try:
                        committed = fut.result()
                        if not committed:
                            pending.discard(fut)
                            continue
                        completed += 1
                        elapsed = time.time() - (
                            deadlines[fut] - timeouts[fut]
                        )
                        logger.info(
                            "[Scheduler] %s 完成 (%s/%s)，耗时 %.0fs",
                            platform,
                            completed,
                            total,
                            elapsed,
                        )
                        if self._progress_callback:
                            self._progress_callback(
                                platform,
                                "success",
                                "完成，耗时 {:.0f}s".format(
                                    elapsed
                                ),
                            )
                    except Exception as e:
                        failed += 1
                        logger.error(
                            "[Scheduler] %s 异常: %s",
                            platform,
                            e,
                        )
                        if self._progress_callback:
                            self._progress_callback(
                                platform,
                                "error",
                                str(e)[:100],
                            )
                    pending.discard(fut)

        finally:
            # 取消排队任务，并等待已运行的线程退出。
            # 线程无法强杀；等待其结束可避免 _busy 提前释放后发生并发采集。
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                # Python 3.8 兼容
                pool.shutdown(wait=True)

        total_elapsed = time.time() - start_time
        logger.info(
            "[Scheduler] 全部处理完成：%s 成功 / %s 失败，总耗时 %.0fs",
            completed,
            failed,
            total_elapsed,
        )
        return {
            "total": total,
            "completed": completed,
            "failed": max(failed, total - completed),
        }

    def _run_one(self, adapter: CrawlerAdapter, start_date: str, end_date: str, task_id: str):
        """执行单个平台的采集任务"""
        self.task_mgr.mark_running(task_id)
        try:
            if hasattr(self.task_mgr, "is_active") and not self.task_mgr.is_active(task_id):
                self._finalize_inactive(task_id)
                return False
            if not adapter.check_credential():
                raise RuntimeError("凭证失效，请更新")
            if hasattr(self.task_mgr, "is_active") and not self.task_mgr.is_active(task_id):
                self._finalize_inactive(task_id)
                return False

            # 进度回调：写入任务步骤 + 上报全局回调
            def platform_progress(msg):
                self.task_mgr.update_step(task_id, str(msg))
                if self._progress_callback:
                    self._progress_callback(
                        adapter.platform_name,
                        "progress",
                        str(msg),
                    )

            venue_data = None
            for attempt in range(self.max_retries + 1):
                if attempt and hasattr(self.task_mgr, "is_active"):
                    if not self.task_mgr.is_active(task_id):
                        logger.info("[Scheduler] %s 已取消，停止重试", adapter.platform_name)
                        return False
                try:
                    if self.process_isolation:
                        platform_progress("已启动隔离采集进程")
                        venue_data = self._run_adapter_isolated(
                            adapter, start_date, end_date, task_id,
                            timeouts=self._resolve_timeout(adapter.platform_name),
                        )
                    else:
                        venue_data = adapter.run(
                            start_date,
                            end_date,
                            progress_callback=platform_progress,
                        )
                    break
                except Exception as error:
                    if attempt >= self.max_retries or not self._is_retryable_error(error):
                        raise
                    delay = min(30, 2 ** attempt)
                    message = "临时网络错误，第 {}/{} 次重试，{} 秒后继续".format(
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    logger.warning("[Scheduler] %s：%s", adapter.platform_name, message)
                    if hasattr(self.task_mgr, "increment_retry"):
                        self.task_mgr.increment_retry(task_id)
                    self.task_mgr.update_step(task_id, message)
                    if self._progress_callback:
                        self._progress_callback(adapter.platform_name, "retry", message)
                    if not self._wait_for_retry(task_id, delay):
                        logger.info("[Scheduler] %s 已取消，停止重试", adapter.platform_name)
                        self._finalize_inactive(task_id)
                        return False

            if hasattr(self.task_mgr, "is_active") and not self.task_mgr.is_active(task_id):
                logger.info("[Scheduler] %s 调用已释放，丢弃取消后的结果", adapter.platform_name)
                self._finalize_inactive(task_id)
                return False

            # 规范化后，在一个事务中替换该平台该日期的全部结果，
            # 避免旧门店残留或只写入半批数据。
            if not isinstance(venue_data, list) or not venue_data:
                raise RuntimeError("适配器未返回有效的场地数据")
            rows = []
            for item in venue_data:
                if not isinstance(item, dict):
                    raise RuntimeError("适配器返回了非对象数据")
                item_copy = dict(item)
                venue = str(item_copy.pop("场地", "")).strip()
                if not venue:
                    raise RuntimeError("适配器返回数据缺少场地")
                rows.append((
                    venue,
                    json.dumps(item_copy, ensure_ascii=False),
                ))

            if not self.task_mgr.complete_with_summary(
                task_id,
                adapter.platform_name,
                end_date,
                rows,
            ):
                logger.info("[Scheduler] %s 已取消，丢弃迟到结果", adapter.platform_name)
                self._finalize_inactive(task_id)
                return False
            return True
        except Exception as e:
            if hasattr(self.task_mgr, "is_active") and not self.task_mgr.is_active(task_id):
                self._finalize_inactive(task_id)
            else:
                self.task_mgr.mark_failed(task_id, str(e))
            raise

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen) -> None:
        """结束隔离 worker 及其可能启动的浏览器子进程。"""
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _run_adapter_isolated(
        self,
        adapter: CrawlerAdapter,
        start_date: str,
        end_date: str,
        task_id: str,
        *,
        timeouts: int,
    ) -> list:
        """在独立 Python 进程运行适配器，超时可真正结束底层调用。"""
        with tempfile.TemporaryDirectory(prefix="workbuddy-adapter-") as temp_dir:
            result_file = Path(temp_dir) / "result.json"
            progress_file = Path(temp_dir) / "progress.jsonl"
            command = [
                sys.executable,
                "-u",
                "-m",
                "core.adapter_worker",
                adapter.platform_name,
                str(start_date),
                str(end_date),
                str(result_file),
                str(progress_file),
            ]
            process = subprocess.Popen(
                command,
                cwd=str(_PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            deadline = time.monotonic() + max(0.01, float(timeouts))
            progress_offset = 0

            def drain_progress() -> None:
                nonlocal progress_offset
                if not progress_file.is_file():
                    return
                try:
                    with progress_file.open("r", encoding="utf-8") as handle:
                        handle.seek(progress_offset)
                        while True:
                            line_start = handle.tell()
                            line = handle.readline()
                            if not line:
                                break
                            if not line.endswith("\n"):
                                handle.seek(line_start)
                                break
                            progress_offset = handle.tell()
                            try:
                                message = str(json.loads(line).get("message") or "").strip()
                            except (ValueError, TypeError):
                                continue
                            if not message:
                                continue
                            self.task_mgr.update_step(task_id, message)
                            if self._progress_callback:
                                self._progress_callback(
                                    adapter.platform_name, "progress", message
                                )
                except OSError:
                    return

            try:
                while process.poll() is None:
                    drain_progress()
                    if hasattr(self.task_mgr, "is_active") and not self.task_mgr.is_active(task_id):
                        self._terminate_process_tree(process)
                        return []
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._terminate_process_tree(process)
                        raise AdapterProcessTimeout(
                            "平台 %s 隔离进程超过 %ss，已强制结束"
                            % (adapter.platform_name, timeouts)
                        )
                    time.sleep(min(0.2, remaining))

                drain_progress()
                if process.returncode != 0:
                    error = "隔离进程退出码 %s" % process.returncode
                    if result_file.is_file():
                        try:
                            payload = json.loads(result_file.read_text(encoding="utf-8"))
                            error = str(payload.get("error") or error)
                        except (OSError, ValueError):
                            pass
                    raise RuntimeError(error)
                payload = json.loads(result_file.read_text(encoding="utf-8"))
                if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
                    raise RuntimeError(str(payload.get("error") or "隔离进程未返回有效数据"))
                return payload["data"]
            finally:
                self._terminate_process_tree(process)

    def _wait_for_retry(self, task_id: str, delay: float) -> bool:
        """以短轮询替代不可取消 sleep；False 表示任务已收到停止请求。"""
        deadline = time.monotonic() + max(0.0, float(delay))
        while True:
            if hasattr(self.task_mgr, "is_active") and not self.task_mgr.is_active(task_id):
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.2, remaining))

    def _finalize_inactive(self, task_id: str) -> None:
        if hasattr(self.task_mgr, "finalize_inactive"):
            self.task_mgr.finalize_inactive(task_id)

    def _find_task_id(self, task_ids: dict, platform: str) -> str:
        for tid, p in task_ids.items():
            if p == platform:
                return tid
        return ""

    def launch_background(self, start_date: str, end_date: str,
                          adapters: List[CrawlerAdapter],
                          progress_callback: Optional[Callable] = None,
                          completion_callback: Optional[
                              Callable[[Dict[str, Any]], None]
                          ] = None) -> bool:
        """
        启动 daemon 后台线程运行采集

        Args:
            start_date: 开始日期
            end_date: 结束日期
            adapters: 平台适配器列表
            progress_callback: 可选进度回调 (platform, status, detail)
            completion_callback: 整批结束回调，接收 total/completed/failed
        Returns:
            是否成功启动
        """
        with Scheduler._lock:
            if Scheduler._busy:
                return False
            Scheduler._busy = True

        self.set_progress_callback(progress_callback)
        t = threading.Thread(
            target=self._run_with_lock,
            args=(start_date, end_date, adapters, completion_callback),
            daemon=True
        )
        t.start()
        return True

    def _run_with_lock(
        self,
        start_date: str,
        end_date: str,
        adapters: List[CrawlerAdapter],
        completion_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        result: Dict[str, Any] = {
            "total": len(adapters),
            "completed": 0,
            "failed": len(adapters),
        }
        try:
            result = self.run_range(start_date, end_date, adapters)
        except Exception as e:
            import traceback
            logger.error(
                "[Scheduler] 致命错误: %s\n%s",
                e,
                traceback.format_exc(),
            )
            result["fatal_error"] = str(e)[:500]
        finally:
            with Scheduler._lock:
                Scheduler._busy = False
            if completion_callback:
                try:
                    completion_callback(dict(result))
                except Exception:
                    logger.exception("[Scheduler] 整批完成回调失败")
