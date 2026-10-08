# -*- coding: utf-8 -*-
"""
KPay（kpay-group.com）数据采集（原八爪鱼脚本整理）

主入口：main(start_date, end_date, account, password) -> List[Dict]
    输入：起止日期 + KPay账号密码（参数传入）
    输出：[{"场地": str, "Kpay收款":..., "Kpay手续费":...}, ...]（场地维度，香港9场地）

流程：
  生成RSA密钥对→登录(AES加密密码)→获取服务端RSA公钥→双向RSA加密通信→
  查询商户列表→按TARGET_VENUES匹配→按月分段查询结算统计→汇总收款/手续费

技术特点：
- RSA 2048 双向加密（OAEP + SHA1）+ RSA签名(PKCS1v15 + SHA256)
- AES-CBC 密码加密（PBKDF2派生密钥）
- 日期按月拆分查询（API限制）
- 依赖 cryptography 库
- 9个香港目标场地硬编码（TARGET_VENUES）
"""

import base64
import hashlib
import json
import os
import re
import secrets
import time as time_module
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests
from utils.http import create_retry_session
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from utils.mapping import add_unique_mapping
from utils.mysql_pool import fetch_all_cached
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE_URL = "https://pc.kpay-group.com"
SID = os.getenv("KPAY_SID", "")
VERSION = "2.1.0"
HK_TIMEZONE = timezone(timedelta(hours=8))
NONCE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "123456789"
)

TARGET_VENUES = (
    "香港A店",
    "香港B店",
    "香港宝林FF",
    "香港E店",
    "香港H店",
    "香港F店",
    "香港C店",
    "香港D店",
    "香港G店",
)
RECEIPT_FIELD = "totalTransactionAmount"
FEE_FIELDS = (
    "totalHandlingFee",
    "totalSettlementHandlingFee",
)


def _compact_json(data):
    return json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _as_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _plain_number(value):
    number = _as_decimal(value)
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _normalize_merchant_name(value):
    return re.sub(
        r"\s+",
        "",
        str(value or "").strip().lower(),
    )


def _get_kpay_merchant_mapping():
    sql = (
        "SELECT venue, kpay "
        "FROM company_organizational_structure "
        "WHERE kpay IS NOT NULL "
        "AND TRIM(kpay) <> '' "
        "ORDER BY venue"
    )
    rows = fetch_all_cached(sql)

    mapping = {}
    ambiguous = set()
    venue_order = []
    for venue, merchant_names in rows:
        venue = str(venue or "").strip()
        if not venue:
            continue
        if venue not in venue_order:
            venue_order.append(venue)
        for merchant_name in str(
            merchant_names or ""
        ).splitlines():
            normalized = _normalize_merchant_name(
                merchant_name
            )
            if normalized:
                add_unique_mapping(mapping, ambiguous, normalized, venue, "kpay")

    configured_venues = set(venue_order)
    missing_venues = [
        venue
        for venue in TARGET_VENUES
        if venue not in configured_venues
    ]
    if missing_venues:
        raise RuntimeError(
            "数据库缺少KPay商户配置：{}".format(
                "、".join(missing_venues)
            )
        )

    ordered_venues = list(TARGET_VENUES) + [
        venue
        for venue in venue_order
        if venue not in TARGET_VENUES
    ]

    return mapping, ordered_venues


def _build_result_item(venue, receipt_total, fee_total):
    item = {
        "场地": venue,
    }
    if venue in TARGET_VENUES:
        item["Kpay收款"] = _plain_number(receipt_total)
    item["Kpay手续费"] = _plain_number(fee_total)
    return item


def _api_path(url_path):
    return re.sub(
        r"^(?:https?://[^/]+)?/api(?:/(?:auth|kbank|kc|onboarding))?",
        "",
        url_path,
    )


def _is_success(data):
    if not isinstance(data, dict):
        return False

    return data.get("code") in (
        None,
        0,
        "0",
        200,
        "200",
        10000,
        "10000",
        "SUCCESS",
        "success",
    )


def _message(data):
    if not isinstance(data, dict):
        return str(data)

    return str(
        data.get("message")
        or data.get("msg")
        or data.get("errorMessage")
        or ""
    )


class KPayClient:
    def __init__(self):
        self.session = create_retry_session()
        self.session.trust_env = False
        self.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        self.server_public_key = None
        self.access_token = ""
        self.refresh_token = ""
        self.merchant_id = ""
        self.account = ""
        self.expires_at = 0

    def _public_key_body(self):
        public_der = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(public_der).decode("ascii")

    @staticmethod
    def _encrypt_password(account, password, timestamp):
        # 服务端协议用时间戳取模作为迭代次数；至少为 1，避免 0 迭代导致崩溃
        iterations = max(int(timestamp) % 65535, 1)
        key = hashlib.pbkdf2_hmac(
            "sha256",
            account.encode("utf-8"),
            account.encode("utf-8"),
            iterations,
            dklen=32,
        )

        password_bytes = password.encode("utf-8")
        block_size = algorithms.AES.block_size // 8
        password_bytes += b"\x00" * (
            (-len(password_bytes)) % block_size
        )

        cipher = Cipher(
            algorithms.AES(key),
            modes.CBC(b"\x00" * block_size),
        )
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(password_bytes)
        encrypted += encryptor.finalize()
        return base64.b64encode(encrypted).decode("ascii")

    @staticmethod
    def _terminal_serial_number(account):
        machine_value = "{}|{}|{}".format(
            account.strip().lower(),
            uuid.getnode(),
            os.environ.get("COMPUTERNAME", ""),
        )
        return hashlib.sha256(
            machine_value.encode("utf-8")
        ).hexdigest()

    def _sign(
        self,
        method,
        url_path,
        timestamp,
        nonce,
        body,
        params=None,
    ):
        method = method.upper()
        path = _api_path(url_path)

        if method == "GET" and params:
            prepared = requests.Request(
                "GET",
                "https://placeholder.invalid" + path,
                params=params,
            ).prepare()
            path = prepared.path_url

        body_text = (
            ""
            if method == "GET"
            else _compact_json(body or {})
        )
        canonical = "\n".join(
            [method, path, timestamp, nonce, body_text, ""]
        )
        signature = self.private_key.sign(
            canonical.encode("utf-8"),
            rsa_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def _headers(
        self,
        method,
        url_path,
        body=None,
        signing_body=None,
        params=None,
        merchant_id="",
    ):
        timestamp = str(int(time_module.time() * 1000))
        nonce = "".join(
            secrets.choice(NONCE_ALPHABET)
            for _ in range(32)
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "sid": SID,
            "K-Version": VERSION,
            "K-Terminal": "Windows%2CChrome",
            "K-Nonce-Str": nonce,
            "K-Timestamp": timestamp,
            "K-Language": "zh_HK",
        }

        if self.access_token:
            headers["K-Access-Token"] = self.access_token
        if merchant_id or self.merchant_id:
            headers["K-Merchant-Id"] = (
                merchant_id or self.merchant_id
            )

        headers["K-Signature"] = self._sign(
            method,
            url_path,
            timestamp,
            nonce,
            body if signing_body is None else signing_body,
            params,
        )
        return headers

    def _decrypt_chunks(self, encrypted_base64):
        encrypted = base64.b64decode(encrypted_base64)
        chunk_size = self.private_key.key_size // 8
        chunks = []

        for index in range(0, len(encrypted), chunk_size):
            chunk = encrypted[index:index + chunk_size]
            chunks.append(
                self.private_key.decrypt(
                    chunk,
                    rsa_padding.OAEP(
                        mgf=rsa_padding.MGF1(
                            algorithm=hashes.SHA1()
                        ),
                        algorithm=hashes.SHA1(),
                        label=None,
                    ),
                )
            )

        return b"".join(chunks)

    def _decode_response(self, response):
        try:
            result = response.json()
        except ValueError as error:
            raise RuntimeError(
                "KPay返回的不是JSON：HTTP {}，{}".format(
                    response.status_code,
                    response.text[:300],
                )
            ) from error

        if not isinstance(result, dict):
            return result

        if result.get("isEncrypted") is True:
            decrypted = self._decrypt_chunks(
                result.get("data", "")
            )
            try:
                return json.loads(decrypted.decode("utf-8"))
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as error:
                raise RuntimeError(
                    "KPay响应解密后不是有效JSON"
                ) from error

        if (
            result.get("isEncrypted") is False
            and isinstance(result.get("data"), str)
        ):
            try:
                return json.loads(result["data"])
            except json.JSONDecodeError:
                pass

        return result

    def _load_server_public_key(self, encrypted_public_key):
        public_key_body = self._decrypt_chunks(
            encrypted_public_key
        )
        try:
            public_der = base64.b64decode(
                public_key_body.decode("ascii"),
                validate=True,
            )
        except (UnicodeDecodeError, ValueError):
            public_der = public_key_body

        self.server_public_key = (
            serialization.load_der_public_key(public_der)
        )

    def _encrypt_request_data(self, data):
        if self.server_public_key is None:
            raise RuntimeError(
                "尚未取得KPay服务端公钥，请先登录"
            )

        plain = _compact_json(data).encode("utf-8")
        encrypted_chunks = []

        for index in range(0, len(plain), 214):
            chunk = plain[index:index + 214]
            encrypted_chunks.append(
                self.server_public_key.encrypt(
                    chunk,
                    rsa_padding.OAEP(
                        mgf=rsa_padding.MGF1(
                            algorithm=hashes.SHA1()
                        ),
                        algorithm=hashes.SHA1(),
                        label=None,
                    ),
                )
            )

        return {
            "data": base64.b64encode(
                b"".join(encrypted_chunks)
            ).decode("ascii")
        }

    def request(
        self,
        method,
        url_path,
        data=None,
        params=None,
        encrypt=False,
        merchant_id="",
    ):
        outgoing_data = (
            self._encrypt_request_data(data or {})
            if encrypt and method.upper() == "POST"
            else data
        )
        headers = self._headers(
            method,
            url_path,
            outgoing_data,
            data,
            params,
            merchant_id,
        )

        response = self.session.request(
            method,
            BASE_URL + url_path,
            headers=headers,
            params=params,
            data=(
                _compact_json(outgoing_data)
                if outgoing_data is not None
                else None
            ),
            timeout=30,
        )
        return self._decode_response(response)

    def login(self, account, password, verification_code=""):
        account = str(account).strip()
        timestamp = str(int(time_module.time() * 1000))
        payload = {
            "account": account,
            "password": self._encrypt_password(
                account,
                str(password),
                timestamp,
            ),
            "timestamp": timestamp,
            "terminalSerialNumber": (
                self._terminal_serial_number(account)
            ),
            "terminalSystemInfo": "Windows,Chrome",
            "publicKey": self._public_key_body(),
            "clientType": 3,
        }

        if str(verification_code).strip():
            payload["code"] = str(
                verification_code
            ).strip()

        result = self.request(
            "POST",
            "/api/auth/v3/user/login",
            data=payload,
            encrypt=False,
        )

        if not _is_success(result):
            return result

        login_data = result.get("data", result)
        if not isinstance(login_data, dict):
            return result

        self.account = str(
            login_data.get("account") or account
        )
        self.access_token = str(
            login_data.get("accessToken") or ""
        )
        self.refresh_token = str(
            login_data.get("refreshToken") or ""
        )
        self.merchant_id = str(
            login_data.get("merchantId") or ""
        )
        self.expires_at = int(
            login_data.get("expired") or 0
        )

        encrypted_public_key = login_data.get("publicKey")
        if encrypted_public_key:
            self._load_server_public_key(
                encrypted_public_key
            )

        return result


def _parse_date(value, field_name):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    for date_format in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(
                text,
                date_format,
            ).date()
        except ValueError:
            pass

    raise ValueError(
        "{}格式错误，应为 YYYY-MM-DD 或 "
        "YYYY-MM-DD HH:MM:SS，当前值：{}".format(
            field_name,
            text,
        )
    )


def _next_month(value):
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _split_by_month(start_date, end_date):
    current = start_date

    while current <= end_date:
        month_end = _next_month(current)
        month_end -= timedelta(days=1)
        section_end = min(end_date, month_end)
        yield current, section_end
        current = section_end + timedelta(days=1)


def _start_milliseconds(value):
    dt = datetime.combine(value, time.min)
    dt = dt.replace(tzinfo=HK_TIMEZONE)
    return int(dt.timestamp() * 1000)


def _end_milliseconds(value):
    dt = datetime.combine(value, time.max)
    dt = dt.replace(tzinfo=HK_TIMEZONE)
    return int(dt.timestamp() * 1000)


def _require_success(result, operation):
    if _is_success(result):
        return

    code = (
        result.get("code", "")
        if isinstance(result, dict)
        else ""
    )
    raise RuntimeError(
        "KPay{}失败：code={}，message={}".format(
            operation,
            code,
            _message(result),
        )
    )


def _get_all_merchants(client):
    result = client.request(
        "POST",
        "/api/auth/v3/index/merchant/list",
        data={},
        encrypt=True,
    )
    _require_success(result, "查询商户列表")

    result_data = result.get("data") or {}
    merchants = result_data.get("data") or []
    total_count = int(
        result_data.get("totalCount") or len(merchants)
    )

    if total_count > len(merchants):
        result = client.request(
            "POST",
            "/api/auth/v3/index/merchant/list",
            data={
                "current": 1,
                "pageSize": total_count,
            },
            encrypt=True,
        )
        _require_success(result, "查询完整商户列表")
        merchants = (result.get("data") or {}).get(
            "data"
        ) or []

    normalized = []
    seen_ids = set()

    for merchant in merchants:
        merchant_id = str(
            merchant.get("merchantId") or ""
        ).strip()
        merchant_name = str(
            merchant.get("merchantName") or merchant_id
        ).strip()

        if merchant_id and merchant_id not in seen_ids:
            normalized.append(
                {
                    "merchantId": merchant_id,
                    "merchantName": merchant_name,
                }
            )
            seen_ids.add(merchant_id)

    if not normalized:
        raise RuntimeError(
            "KPay账号下没有查询到可用商户"
        )

    return normalized


def _query_section(
    client,
    merchant_id,
    section_start,
    section_end,
):
    start_ms = _start_milliseconds(section_start)
    end_ms = _end_milliseconds(section_end)
    common_data = {
        "transactionStartDate": start_ms,
        "transactionEndDate": end_ms,
    }

    statistics_result = client.request(
        "POST",
        "/api/v3/settlement/statistics",
        data=common_data,
        encrypt=True,
        merchant_id=merchant_id,
    )
    _require_success(
        statistics_result,
        "查询结算统计",
    )

    return statistics_result.get("data") or {}


def main(
    start_date,
    end_date,
    account,
    password,
):
    start_value = _parse_date(
        start_date,
        "start_date",
    )
    end_value = _parse_date(
        end_date,
        "end_date",
    )

    if start_value > end_value:
        raise ValueError(
            "start_date不能晚于end_date"
        )

    client = KPayClient()
    login_result = client.login(
        account,
        password,
    )
    _require_success(login_result, "登录")

    if not client.access_token:
        raise RuntimeError(
            "KPay登录响应中没有accessToken"
        )
    if client.server_public_key is None:
        raise RuntimeError(
            "KPay登录后未能初始化服务端公钥"
        )

    merchant_mapping, venue_order = _get_kpay_merchant_mapping()
    merchants_by_venue = {
        venue: []
        for venue in venue_order
    }

    for merchant in _get_all_merchants(client):
        venue = merchant_mapping.get(
            _normalize_merchant_name(
                merchant["merchantName"]
            )
        )
        if venue:
            merchants_by_venue[venue].append(merchant)

    missing_venues = [
        venue
        for venue in TARGET_VENUES
        for merchants in [merchants_by_venue.get(venue, [])]
        if not merchants
    ]
    if missing_venues:
        raise RuntimeError(
            "KPay账号下未找到数据库配置的商户：{}".format(
                "、".join(missing_venues)
            )
        )

    result = []
    for venue in venue_order:
        receipt_total = Decimal("0")
        fee_total = Decimal("0")

        for merchant in merchants_by_venue[venue]:
            for section_start, section_end in _split_by_month(
                start_value,
                end_value,
            ):
                statistics = _query_section(
                    client,
                    merchant["merchantId"],
                    section_start,
                    section_end,
                )
                receipt_total += _as_decimal(
                    statistics.get(RECEIPT_FIELD)
                )
                fee_total += sum(
                    (
                        _as_decimal(statistics.get(field))
                        for field in FEE_FIELDS
                    ),
                    Decimal("0"),
                )

        result.append(
            _build_result_item(
                venue,
                receipt_total,
                fee_total,
            )
        )

    print(result)
    return result


if __name__ == "__main__":
    import sys
    from datetime import datetime as dt
    account = os.environ.get("KPAY_ACCOUNT", "")
    password = os.environ.get("KPAY_PASSWORD", "")
    if not account or not password:
        print("请设置环境变量 KPAY_ACCOUNT 和 KPAY_PASSWORD")
        sys.exit(1)
    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
    else:
        d = dt.now().strftime("%Y-%m-%d")
        start, end = d, d
    data = main(start, end, account, password)
    print(f"\n共返回 {len(data)} 条场地数据")
