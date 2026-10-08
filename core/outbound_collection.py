# -*- coding: utf-8 -*-
"""总部出货按月顺序采集。线程仅做读取与批次写入，不改变货款总额。"""
import hashlib
import logging
import threading

from core import outbound_payments as store
from core.credential_manager import CredentialManager
from crawlers import duojinbao_equipment_stock_crawler as crawler
from crawlers.duojinbao_store_value_crawler import get_all_stores
from utils.mysql_pool import fetch_all
from utils.redaction import redact_sensitive_text


logger = logging.getLogger("outbound_collection")
ACCOUNT_FIELD = "总部账号"
PASSWORD_FIELD = "总部密码"


def load_mapping():
    result = {}
    for venue, names in fetch_all("SELECT venue,duojinbao FROM company_organizational_structure WHERE duojinbao IS NOT NULL"):
        for name in str(names or "").splitlines():
            name, venue = name.strip(), str(venue or "").strip()
            if name and venue:
                result.setdefault(name, set()).add(venue)
    return {name: sorted(venues) for name, venues in result.items()}


def credentials_configured():
    credentials = CredentialManager().load("duojinbao") or {}
    return bool(credentials.get(ACCOUNT_FIELD) and credentials.get(PASSWORD_FIELD))


def collect_run(run_id):
    """供后台和命令行共用。旧完整版本只在 publish 的事务内替换。"""
    if not store.begin_run(run_id):
        return
    username = password = ""
    try:
        credentials = CredentialManager().load("duojinbao") or {}
        username, password = credentials.get(ACCOUNT_FIELD, ""), credentials.get(PASSWORD_FIELD, "")
        if not username or not password:
            raise ValueError("请在凭证管理的多金宝中填写总部账号、总部密码")
        with crawler.create_session() as session:
            try:
                crawler.login(session, username, password)
            except Exception:
                raise ValueError("多金宝登录失败，请检查总部凭证和网络") from None
            shops = get_all_stores(session)
            headquarters = crawler.select_headquarters(session)
            run = store.get_run(run_id)
            # 首次冻结映射；重试已完成日期时不能悄悄换成另一版映射。
            mapping = load_mapping() if not run["headquarters_id"] else {}
            mapping = store.bind_source(run_id, headquarters["storeId"],
                                        hashlib.sha256(str(username).encode("utf-8")).hexdigest(),
                                        mapping, [shop["store_name"] for shop in shops])
            finished = store.completed_days(run_id)
            for day in crawler.month_dates(run["month"]):
                if day in finished:
                    continue
                if not store.active(run_id):
                    return
                metadata = {}
                records = crawler.fetch_records(
                    session, day, metadata=metadata,
                    progress_callback=lambda page, pages: store.update_progress(run_id, day, page, pages),
                )
                store.save_day(run_id, day, records, metadata, mapping)
                logger.info("出货采集 %s %s 完成，%s 条", run["month"], day, len(records))
            store.publish(run_id)
            logger.info("出货采集 %s 完整版本已保存", run["month"])
    except Exception as exc:
        message = redact_sensitive_text(str(exc))
        for secret in (username, password):
            if secret:
                message = message.replace(str(secret), "[已隐藏]")
        store.fail_run(run_id, message[:300] or "出货采集失败")
        logger.warning("出货采集批次 %s 未完成：%s", run_id, message[:300])


class OutboundCollectionManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None

    def _available(self):
        if self._thread and self._thread.is_alive():
            raise ValueError("采集线程正在完成当前请求，请稍后再试")
        if not credentials_configured():
            raise ValueError("请在凭证管理的多金宝中填写总部账号、总部密码")

    def _launch(self, ids):
        self._thread = threading.Thread(target=self._run, args=(ids,), name="outbound-month-collection", daemon=True)
        self._thread.start()

    def start(self, start_month, end_month, actor_user_id=""):
        with self._lock:
            self._available()
            ids = store.queue_months(start_month, end_month, actor_user_id)
            self._launch(ids)
            return ids

    def retry(self, run_id):
        with self._lock:
            self._available()
            store.retry_run(run_id)
            self._launch([run_id])

    @staticmethod
    def _run(ids):
        for run_id in ids:
            collect_run(run_id)
            if store.get_run(run_id)["status"] == "failed":
                # 不对错误的登录凭证逐月反复尝试；后面的月份保留可重试状态。
                store.cancel_batch(run_id, "前序月份未完成，后续月份暂停，可重试")
                break


outbound_manager = OutboundCollectionManager()
