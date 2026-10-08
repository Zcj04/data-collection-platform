# -*- coding: utf-8 -*-
"""全部平台适配器集合（支持日期范围采集）"""
import importlib
from adapters.base import CrawlerAdapter, CrawlerError
from core.credential_manager import CredentialManager


# === 标准同步适配器基类 ===
class _SyncAdapter(CrawlerAdapter):
    MODULE = ""
    def __init__(self): self._cred_mgr = CredentialManager()
    @property
    def platform_name(self) -> str: return self.PLATFORM
    def check_credential(self) -> bool: return bool(self._cred_mgr.get(self.PLATFORM, "账号"))
    def run(self, start_date: str, end_date: str, progress_callback=None):
        try:
            mod = importlib.import_module(self.MODULE)
            return mod.main(start_date, end_date, self._cred_mgr.get(self.PLATFORM, "账号"), self._cred_mgr.get(self.PLATFORM, "密码"))
        except Exception as e: raise CrawlerError(self.PLATFORM, str(e))


# === 美团（凭证管理注入，支持2账号） ===
class MeituanAdapter(CrawlerAdapter):
    PLATFORM = "meituan"
    def __init__(self): self._cred_mgr = CredentialManager()
    @property
    def platform_name(self) -> str: return self.PLATFORM
    def check_credential(self) -> bool:
        import requests
        dl = importlib.import_module("crawlers.meituan_download")
        self._inject_creds(dl)
        if not dl.MTGSIGS or not dl.COOKIES: return False
        p = "yodaReady=h5&csecplatform=4&csecversion=4.2.0"
        r = requests.post(f"https://e.dianping.com/gateway/merchant/general/shopinfo?{p}&mtgsig={dl.MTGSIGS[0]}",
                          headers={"Cookie": dl.COOKIES[0]}, timeout=10, json={"bizType":"pc-shouye","device":"pc","currentTab":"city"})
        if r.status_code == 401: return False
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError as error:
            raise CrawlerError(self.PLATFORM, "凭证校验返回非 JSON 响应，无法确认凭证状态") from error
        if not isinstance(payload, dict) or payload.get("data") is None:
            raise CrawlerError(self.PLATFORM, "凭证校验未返回有效数据，请检查平台响应或登录状态")
        return True
    def _inject_creds(self, dl):
        """从凭证管理注入最多2组账号"""
        cookies, sigs, pids = [], [], []
        for i in range(1, 3):
            c = self._cred_mgr.get(self.PLATFORM, f"Cookie{i}")
            s = self._cred_mgr.get(self.PLATFORM, f"mtgsig{i}")
            p = self._cred_mgr.get(self.PLATFORM, f"partner_id{i}")
            if c and s: cookies.append(c); sigs.append(s); pids.append(p or "")
        if cookies: dl.COOKIES = cookies; dl.MTGSIGS = sigs; dl.partners_id = pids
    def run(self, start_date: str, end_date: str, progress_callback=None):
        try:
            dl = importlib.import_module("crawlers.meituan_download")
            self._inject_creds(dl)
            shop = dl.main(
                start_date,
                end_date,
                progress_callback=progress_callback,
            )
            if not shop: raise CrawlerError(self.PLATFORM, "空数据")
            mt = importlib.import_module("crawlers.meituan_match")
            return mt.main(shop)
        except CrawlerError: raise
        except Exception as e: raise CrawlerError(self.PLATFORM, str(e))


# === 多金宝（单账号注入） ===
class DuojinbaoAdapter(CrawlerAdapter):
    PLATFORM = "duojinbao"
    def __init__(self): self._cred_mgr = CredentialManager()
    @property
    def platform_name(self) -> str: return self.PLATFORM
    def check_credential(self) -> bool: return bool(self._cred_mgr.get(self.PLATFORM, "账号1"))
    def run(self, start_date: str, end_date: str, progress_callback=None):
        try:
            mod = importlib.import_module("crawlers.duojinbao_crawler")
            u = self._cred_mgr.get(self.PLATFORM, "账号1")
            p = self._cred_mgr.get(self.PLATFORM, "密码1")
            accounts = [{"username": u, "password": p}] if u and p else []
            if accounts: mod.ACCOUNTS = accounts
            return mod.main(start_date, end_date)
        except Exception as e: raise CrawlerError(self.PLATFORM, str(e))


# === 标准同步平台 ===
class YuntaiAdapter(_SyncAdapter):
    PLATFORM = "yuntai"; MODULE = "crawlers.yuntai_crawler"
class LeyaoyaoAdapter(_SyncAdapter):
    PLATFORM = "leyaoyao"; MODULE = "crawlers.leyaoyao_crawler"
class JingjianAdapter(_SyncAdapter):
    PLATFORM = "jingjian"; MODULE = "crawlers.jingjian_crawler"
class StarThingAdapter(_SyncAdapter):
    PLATFORM = "starthing"; MODULE = "crawlers.starthing_crawler"
class NewSystemAdapter(_SyncAdapter):
    PLATFORM = "new_system"; MODULE = "crawlers.new_system_crawler"
    def run(self, start_date: str, end_date: str, progress_callback=None):
        try:
            mod = importlib.import_module(self.MODULE)
            access_key = self._cred_mgr.get(self.PLATFORM, "access_key")
            secret_key = self._cred_mgr.get(self.PLATFORM, "secret_key")
            if access_key and secret_key:
                mod.CLOUDBASE_CONFIG = {
                    **mod.CLOUDBASE_CONFIG,
                    "access_key": access_key,
                    "secret_key": secret_key,
                }
            return mod.main(
                start_date,
                end_date,
                self._cred_mgr.get(self.PLATFORM, "账号"),
                self._cred_mgr.get(self.PLATFORM, "密码"),
            )
        except Exception as e: raise CrawlerError(self.PLATFORM, str(e))
class HuilianAdapter(_SyncAdapter):
    PLATFORM = "huilian"; MODULE = "crawlers.huilian_crawler"
class KPayAdapter(_SyncAdapter):
    PLATFORM = "kpay"; MODULE = "crawlers.kpay_crawler"
class YoucaihuaAdapter(_SyncAdapter):
    PLATFORM = "youcaihua"; MODULE = "crawlers.youcaihua_crawler"


# === 本地文件（无凭证） ===
class OctopusAdapter(CrawlerAdapter):
    PLATFORM = "octopus"
    @property
    def platform_name(self) -> str: return self.PLATFORM
    def check_credential(self) -> bool: return True
    def run(self, start_date: str, end_date: str, progress_callback=None):
        try:
            mod = importlib.import_module("crawlers.octopus_crawler")
            return mod.main(start_date, end_date)
        except Exception as e: raise CrawlerError(self.PLATFORM, str(e))

class PaymentAdapter(CrawlerAdapter):
    PLATFORM = "payment"
    @property
    def platform_name(self) -> str: return self.PLATFORM
    def check_credential(self) -> bool: return True
    def run(self, start_date: str, end_date: str, progress_callback=None):
        try:
            mod = importlib.import_module("crawlers.payment_crawler")
            # 货款表按目标日期区分日/月：平时用日货款表，月末最后一天用月货款表
            return mod.main(target_date=end_date)
        except Exception as e: raise CrawlerError(self.PLATFORM, str(e))


# === 抖音来客 ===
class DouyinAdapter(CrawlerAdapter):
    PLATFORM = "douyin"
    def __init__(self): self._cred_mgr = CredentialManager()
    @property
    def platform_name(self) -> str: return self.PLATFORM
    def check_credential(self) -> bool:
        from pathlib import Path
        cookie_file = (
            Path(__file__).resolve().parent.parent
            / "credentials" / "douyin" / "cookie_header.txt"
        )
        has_cookie = cookie_file.exists()
        has_account = bool(
            self._cred_mgr.get(self.PLATFORM, "账号")
            and self._cred_mgr.get(self.PLATFORM, "密码")
        )
        # 有 Cookie 或已填账号密码都算有凭证；首次采集会弹出浏览器自动登录
        return has_cookie or has_account
    def run(self, start_date: str, end_date: str, progress_callback=None):
        try:
            dl = importlib.import_module("crawlers.douyin_download")
            shop = dl.main(start_date, end_date,
                           self._cred_mgr.get(self.PLATFORM, "账号"),
                           self._cred_mgr.get(self.PLATFORM, "密码"))
            if not shop: raise CrawlerError(self.PLATFORM, "空数据")
            mt = importlib.import_module("crawlers.douyin_match")
            return mt.match(shop)
        except CrawlerError: raise
        except Exception as e: raise CrawlerError(self.PLATFORM, str(e))


# === 兑币机（八爪鱼导出 JSON 本地读取） ===
class CoinExchangeAdapter(CrawlerAdapter):
    PLATFORM = "coin_exchange"
    @property
    def platform_name(self) -> str: return self.PLATFORM
    def check_credential(self) -> bool: return True
    def run(self, start_date: str, end_date: str, progress_callback=None):
        try:
            mod = importlib.import_module("crawlers.coin_exchange_crawler")
            # 平台以 end_date 为汇总日，返回截至该日的累计值
            return mod.main(end_date)
        except Exception as e: raise CrawlerError(self.PLATFORM, str(e))
