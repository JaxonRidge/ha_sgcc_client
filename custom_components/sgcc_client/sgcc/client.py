"""SGCC App 异步客户端."""
from __future__ import annotations

import asyncio
import calendar
import json
import random
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from ..const import LOGGER
from .const import CHINA_TZ
from .crypto import build_request_envelope, decrypt_response_envelope
from .login import (
    LoginMapContext,
    build_device_sms_payload,
    build_login_sms_payload,
    build_password_login_map,
    build_sms_login_map,
    login_header_md5,
)
from .models import (
    AccountBalance,
    AccountUsage,
    DailyReading,
    DeviceProfile,
    LoginSession,
    MeterReading,
    MonthlyBill,
    PowerAccount,
    YearlyBilling,
)

LOGIN_PATH = "emss-uia-center-front/member/c2/f01"
DEVICE_SMS_PATH = "emss-uia-center-front/member/c1/f01"
SMS_LOGIN_PATH = "emss-uia-center-front/member/c2/f02"
DAILY_USAGE_PATH = "emss-bia-bill-front/member/c11/f01"
MONTHLY_BILLS_PATH = "emss-bia-bill-front/member/c51/f04"
ACCOUNT_BALANCE_PATH = "emss-bia-balance-front/member/c16/f01"
METER_LIST_PATH = "emss-bia-bill-front/member/c11/f09"
METER_DETAIL_PATH = "emss-bia-bill-front/member/c11/f10"
AUTH_ERROR_CODES = {"-200", "-201"}
DEVICE_VERIFICATION_CODE = "4006"
INTERACTIVE_CHALLENGE_CODES = {"RK008"}
# 设备指纹令牌 4 小时新鲜窗 (与 synthetic_device.TOKEN_CACHE_MS 对齐); 提前 5 分钟刷新
_PROFILE_TOKEN_TTL_MS = 14_400_000
_PROFILE_REFRESH_SKEW_MS = 300_000

class SgccError(Exception):
    pass

class SgccNetworkError(SgccError):
    pass

class SgccApiError(SgccError):
    def __init__(self, code: str, message: str, *, source: str = "service") -> None:
        self.code = code
        self.message = message
        self.source = source
        super().__init__(f"State Grid API {source} error: {code} {message}".strip())

class SgccAuthError(SgccApiError):
    pass

class SgccDeviceVerificationRequired(SgccAuthError):
    pass

class SgccInteractiveChallengeRequired(SgccAuthError):
    pass

def _app_guid_new() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    prefix = "".join(secrets.choice(alphabet) for _ in range(40))
    stamp = datetime.now(CHINA_TZ).strftime("%Y%m%d%H%M%S%f")[:17]
    return f"{prefix}{stamp}{secrets.randbelow(900) + 100}"

def _request_timestamp() -> str:
    # 对齐 App DateUtil.getCurrentTimeSSS: 17 位日期 + 6 位随机
    stamp = datetime.now(CHINA_TZ).strftime("%Y%m%d%H%M%S%f")[:17]
    suffix = "".join(str(secrets.randbelow(10)) for _ in range(6))
    return stamp + suffix

def _month_period(base: date, offset: int) -> tuple[date, date]:
    month_index = base.year * 12 + base.month - 1 - offset
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

def _mask(value: str, head: int = 3) -> str:
    # 日志脱敏: 仅保留前缀
    return value[:head] + "***" if value else ""

def build_daily_usage_payload(
    account: PowerAccount, start_date: date, end_date: date
    ) -> dict[str, Any]:
    return {
        "serviceCode": "BCP_000026",
        "source": "app",
        "target": account.pro_no,
        "data": {
            "acctId": "acctid01",
            "channelCode": "SGAPP",
            # 历史接口须用明文源户号; 加密变体被拒 (code:0/consNo:null)
            "consNo": account.cons_no_src,
            "consNosrc": account.cons_no_src,
            "endTime": end_date.isoformat(),
            "consType": account.cons_type,
            "funcCode": "ALIPAY_01",
            "orgNo": account.org_no,
            "proCode": account.pro_no,
            "promotCode": "1",
            "promotType": "1",
            "serialNo": "",
            "srvCode": "",
            "startTime": start_date.isoformat(),
            "userName": "acctid01",
        },
    }

def build_monthly_bills_payload(account: PowerAccount, year: int) -> dict[str, Any]:
    return {
        "serviceCode": "BCP_000026",
        "source": "app",
        "target": account.pro_no,
        "data": {
            "year": year,
            "consNo": account.cons_no_src,
            "provinceCode": account.pro_no,
            "startYm": f"{year}01",
            "endYm": f"{year}12",
            "funcCode": "ALIPAY_01",
        },
    }

def build_account_balance_payload(
    account: PowerAccount, user_id: str
) -> dict[str, Any]:
    return {
        "serviceCode": "0101143",
        "source": "app",
        "target": account.pro_no,
        "data": {
            "srvCode": "",
            "serialNo": "",
            "channelCode": "0902",
            "funcCode": "A1007200",
            "acctId": user_id,
            "userName": "acctid01",
            "promotType": "1",
            "promotCode": "1",
            "userAccountId": user_id,
            "list": [
                {
                    "consNoSrc": account.cons_no_src,
                    "proCode": account.pro_no,
                    "sceneType": account.elec_type,
                    "consNo": account.cons_no,
                    "orgNo": account.org_no,
                }
            ],
        },
    }

def build_meter_payload(
    account: PowerAccount,
    reading_date: date,
    *,
    meter_bar_code: str = "",
) -> dict[str, Any]:
    data = {
        "promotCode": "1",
        "promotType": "1",
        "funcCode": "A10071400",
        "acctId": "acctid01",
        "userName": "acctid01",
        "serialNo": "",
        "srvCode": "123",
        "channelCode": "SGAPP",
        "consNo": account.cons_no_src,
        "proCode": account.pro_no,
        "ymd": reading_date.isoformat(),
    }
    if meter_bar_code:
        data["meterBarCode"] = meter_bar_code
    return {
        "serviceCode": "0102719",
        "source": "app",
        "target": account.pro_no,
        "data": data,
    }

def _srvrt(response: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    data = response.get("data")
    if not isinstance(data, Mapping):
        return "", str(response.get("message", "")), {}
    server = data.get("srvrt")
    if not isinstance(server, Mapping):
        return "", str(response.get("message", "")), data
    return (
        str(server.get("resultCode", "")),
        str(server.get("resultMessage", "")),
        data,
    )

def _power_accounts(user_info: Mapping[str, Any]) -> list[PowerAccount]:
    raw_accounts = user_info.get("powerUserList")
    if not isinstance(raw_accounts, list):
        raw_accounts = []
    result: list[PowerAccount] = []
    seen: set[str] = set()
    for value in raw_accounts:
        if not isinstance(value, Mapping):
            continue
        try:
            account = PowerAccount.from_api(value)
        except ValueError:
            continue
        if account.account_id not in seen:
            result.append(account)
            seen.add(account.account_id)
    return result

def _minimize_user_info(user_info: Mapping[str, Any]    ) -> dict[str, Any]:
    result = {
        key: user_info[key]
        for key in ("userId", "addressProvince", "addressCity", "addressRegion")
        if key in user_info
    }
    account_keys = {
        "id",
        "userId",
        "powerUserNo",
        "consNo",
        "powerUserNo_dst",
        "consNo_dst",
        "proNo",
        "provinceId",
        "orgNo",
        "elecType",
        "constType",
        "consName",
        "consName_dst",
        "userName",
        "realName",
        "nickname",
        "loginAccount",
        "elecAddr",
        "elecAddr_dst",
        "address",
        "consAddress",
    }
    raw_accounts = user_info.get("powerUserList")
    if isinstance(raw_accounts, list):
        result["powerUserList"] = [
            {key: value[key] for key in account_keys if key in value}
            for value in raw_accounts
            if isinstance(value, Mapping)
        ]
    return result

class SgccAppClient:
    def __init__(
        self,
        http: ClientSession,
        *,
        username: str,
        password: str,
        profile: DeviceProfile,
        login_session: LoginSession | None = None,
        profile_provider: Callable[[], Awaitable[DeviceProfile]] | None = None,
    ) -> None:
        self.http = http
        self.username = username
        self.password = password
        self.profile = profile
        self._base_url = self.profile.base_url.rstrip("/") + "/"
        self.login_session = login_session
        self._profile_provider = profile_provider
        self._profile_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._meter_cache: dict[tuple[str, date], MeterReading] = {}
        # 令牌新鲜截止; 命中窗口则 _post 不再串行重建设备指纹
        self._profile_fresh_until_ms = self._profile_expiry_ms()

    @property
    def context(self) -> LoginMapContext:
        return LoginMapContext(
            push_id=self.profile.push_id,
            push_token_ali=self.profile.push_token_ali,
            city_id=self.profile.address_city,
            province_id=self.profile.address_province,
            district_id=self.profile.address_region,
            device_ip=self.profile.device_ip,
            device_id=self.profile.device_id,
            android_release=self.profile.android_release,
            operator_type=self.profile.operator_type,
            device_model=self.profile.device_model,
        )

    @property
    def accounts(self) -> list[PowerAccount]:
        if self.login_session is None:
            return []
        return _power_accounts(self.login_session.user_info)

    def _province(self) -> str:
        if self.login_session:
            value = self.login_session.user_info.get("addressProvince")
            if value not in (None, ""):
                return str(value)
        return self.profile.province_header or self.profile.address_province

    def _headers(
        self,
        *,
        login_params: Mapping[str, Any] | None = None,
        authenticated: bool,
        request_session: LoginSession | None = None,
    ) -> dict[str, str]:
        current = request_session if authenticated else None
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "timeStamp": (
                datetime.now(CHINA_TZ).strftime("%Y%m%d%H%M%S")
                if login_params is not None
                else _request_timestamp()
            ),
            "t": current.token if current else "",
            "userid": current.user_id if current else "0",
            "AppGuid": self.profile.app_guid,
            "AppGuidNew": _app_guid_new(),
            "security": "android",
            "appcode": "WSGW-SG1001-APP",
            "datacenter": self.profile.datacenter,
            "AccessMethod": "App",
            "deviceTokenTX": self.profile.device_token_tx,
            "deviceTokenTXTime": self.profile.device_token_tx_time,
            "province": self._province() if authenticated else "",
            "version": "3.2.3",
            "wsgwType": "android",
            "ip": self.profile.device_ip,
            "os": "android",
            "User-Agent": "okhttp/3.14.9",
        }
        if login_params is not None:
            headers["md5"] = login_header_md5(login_params)
        return headers

    async def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        authenticated: bool,
        login_params: Mapping[str, Any] | None = None,
        request_session: LoginSession | None = None,
    ) -> dict[str, Any]:
        # 在 await provider 前定住会话, 避免并发登录改掉本次请求令牌
        if authenticated and request_session is None:
            request_session = self.login_session
        # 设备指纹令牌 4 小时新鲜窗; 仅临近过期才刷新, 命中窗口不加锁不重建
        await self._ensure_fresh_profile()
        envelope = build_request_envelope(payload, self.profile.server_public_key)
        url = urljoin(self._base_url, path)
        LOGGER.debug("【六壬推演】请求发出: %s", path)
        started = time.perf_counter()
        try:
            async with self.http.post(
                url,
                data=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
                headers=self._headers(
                    login_params=login_params,
                    authenticated=authenticated,
                    request_session=request_session,
                ),
                timeout=ClientTimeout(total=30),
            ) as response:
                status = response.status
                outer = await self._response_json(response)
        except (TimeoutError, ClientError) as error:
            LOGGER.debug("【六壬推演】请求中断: %s (连接层异常: %s)", path, error)
            raise SgccNetworkError(
                "cannot reach the State Grid App gateway"
            ) from error
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            if "respKey" not in outer or "encryptData" not in outer:
                raise ValueError("encrypted response fields are missing")
            plain = decrypt_response_envelope(outer, self.profile.client_private_key)
        except (KeyError, TypeError, ValueError, UnicodeError) as error:
            LOGGER.debug(
                "【六壬推演】响应解密失败: %s (HTTP %s, %d ms)", path, status, elapsed_ms
            )
            raise SgccNetworkError(
                "cannot decrypt the State Grid App response"
            ) from error
        if not isinstance(plain, dict):
            LOGGER.debug("【六壬推演】响应非 JSON 对象: %s (HTTP %s)", path, status)
            raise SgccNetworkError("State Grid App response is not a JSON object")
        LOGGER.debug(
            "【六壬推演】响应解密完成: %s (HTTP %s, %d ms)", path, status, elapsed_ms
        )
        return plain

    def _profile_expiry_ms(self) -> int:
        # 令牌生成时间 (秒级, 见 device_token.token_time) + 4h 缓存窗, 提前 5 分钟刷新
        try:
            born_ms = int(self.profile.device_token_tx_time) * 1000
        except (ValueError, TypeError):
            born_ms = int(time.time() * 1000)
        return born_ms + _PROFILE_TOKEN_TTL_MS - _PROFILE_REFRESH_SKEW_MS

    async def _ensure_fresh_profile(self) -> None:
        # 命中新鲜窗口则跳过; 仅临近/已过期的并发首到者刷新, 避免每次请求串行重建指纹
        if self._profile_provider is None:
            return
        now_ms = int(time.time() * 1000)
        if self._profile_fresh_until_ms is not None and now_ms < self._profile_fresh_until_ms:
            return
        async with self._profile_lock:
            now_ms = int(time.time() * 1000)
            if self._profile_fresh_until_ms is not None and now_ms < self._profile_fresh_until_ms:
                return
            self.profile = await self._profile_provider()
            self._profile_fresh_until_ms = self._profile_expiry_ms()
            LOGGER.debug("【六壬推演】设备指纹令牌已刷新")

    @staticmethod
    async def _response_json(response: ClientResponse) -> dict[str, Any]:
        text = await response.text()
        if response.status >= 500:
            LOGGER.debug("【六壬推演】网关返回 HTTP %s", response.status)
            raise SgccNetworkError(
                f"State Grid gateway returned HTTP {response.status}"
            )
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise SgccNetworkError(
                "State Grid gateway returned invalid JSON"
            ) from error
        if not isinstance(value, dict):
            raise SgccNetworkError(
                "State Grid gateway returned an invalid envelope"
            )
        return value

    @staticmethod
    def _raise_for_error(response: Mapping[str, Any]) -> Mapping[str, Any]:
        top_code = str(response.get("code", ""))
        srv_code, message, data = _srvrt(response)
        code = srv_code or top_code
        source = "srvrt" if srv_code else "gateway"
        if code == DEVICE_VERIFICATION_CODE:
            LOGGER.debug("【六壬推演】需设备验证: code=%s, msg=%s", code, message)
            raise SgccDeviceVerificationRequired(code, message, source=source)
        if code in INTERACTIVE_CHALLENGE_CODES:
            LOGGER.debug("【六壬推演】交互式验证拦截: code=%s, msg=%s", code, message)
            raise SgccInteractiveChallengeRequired(code, message, source=source)
        if code in AUTH_ERROR_CODES:
            LOGGER.debug("【六壬推演】认证失效: code=%s, msg=%s", code, message)
            raise SgccAuthError(code, message, source=source)
        if srv_code and srv_code != "0000":
            LOGGER.debug("【六壬推演】业务错误: code=%s, msg=%s (srvrt)", srv_code, message)
            raise SgccApiError(srv_code, message, source="srvrt")
        if top_code not in {"", "1"}:
            LOGGER.debug("【六壬推演】网关错误: code=%s, msg=%s", top_code, message)
            raise SgccApiError(
                top_code,
                message or str(response.get("message", "")),
                source="gateway",
            )
        return data

    async def async_login(
        self, *, verification_code: str = "", code_key: str = ""
    ) -> LoginSession:
        async with self._login_lock:
            return await self._async_password_login(
                verification_code=verification_code, code_key=code_key
            )

    async def _async_password_login(
        self, *, verification_code: str = "", code_key: str = ""
    ) -> LoginSession:
        if bool(verification_code) != bool(code_key):
            raise ValueError("verification_code and code_key must be provided together")
        if verification_code and (
            len(verification_code) != 6 or not verification_code.isdigit()
        ):
            raise ValueError("verification_code must contain exactly six digits")
        base = self.context
        context = LoginMapContext(
            **{
                **base.__dict__,
                "code": verification_code,
                "code_key": code_key,
            }
        )
        params = build_password_login_map(self.username, self.password, context=context)
        self.login_session = None
        LOGGER.debug("【六壬推演】密码登录开始: %s", _mask(self.username))
        response = await self._post(
            LOGIN_PATH,
            params,
            authenticated=False,
            login_params=params,
        )
        try:
            data = self._raise_for_error(response)
        except (
            SgccDeviceVerificationRequired,
            SgccInteractiveChallengeRequired,
        ):
            raise
        except SgccApiError as error:
            raise SgccAuthError(
                error.code, error.message, source=error.source
            ) from error
        bizrt = data.get("bizrt")
        if not isinstance(bizrt, Mapping) or not bizrt.get("token"):
            raise SgccAuthError(
                "invalid_auth", "login returned no token"
            )
        session = self._save_login_session(bizrt)
        LOGGER.debug(
            "【六壬推演】密码登录成功: %s, userId=%s, 绑定户号 %d 个",
            _mask(self.username),
            session.user_id,
            len(_power_accounts(session.user_info)),
        )
        return session

    def _save_login_session(self, bizrt: Mapping[str, Any]) -> LoginSession:
        raw_user_info = bizrt.get("userInfo")
        if isinstance(raw_user_info, list):
            user_info = next(
                (dict(item) for item in raw_user_info if isinstance(item, Mapping)),
                {},
            )
        elif isinstance(raw_user_info, Mapping):
            user_info = dict(raw_user_info)
        else:
            user_info = {}
        user_info = _minimize_user_info(user_info)
        user_id = str(user_info.get("userId") or bizrt.get("userId") or "0")
        # 认证有效期固定 24h, 与数据 TTL 对齐; 降低登录频率以规避风控
        self.login_session = LoginSession(
            token=str(bizrt["token"]),
            user_id=user_id,
            expires_at=time.time() + 86400,
            user_info=user_info,
        )
        return self.login_session

    async def async_send_login_sms(self) -> str:
        LOGGER.debug("【六壬推演】请求发送登录短信: %s", _mask(self.username))
        response = await self._post(
            DEVICE_SMS_PATH,
            build_login_sms_payload(self.username, self.context),
            authenticated=False,
        )
        data = self._raise_for_error(response)
        bizrt = data.get("bizrt")
        code_key = str(bizrt.get("codeKey", "")) if isinstance(bizrt, Mapping) else ""
        if not code_key:
            raise SgccApiError(
                "missing_code_key",
                "SMS response did not contain codeKey",
                source="srvrt",
            )
        return code_key

    async def async_sms_login(self, code: str, code_key: str) -> LoginSession:
        params = build_sms_login_map(
            self.username, code, code_key, context=self.context
        )
        response = await self._post(
            SMS_LOGIN_PATH,
            params,
            authenticated=False,
            login_params=params,
        )
        try:
            data = self._raise_for_error(response)
        except SgccApiError as error:
            raise SgccAuthError(
                error.code, error.message, source=error.source
            ) from error
        bizrt = data.get("bizrt")
        if not isinstance(bizrt, Mapping) or not bizrt.get("token"):
            raise SgccAuthError(
                "invalid_auth", "SMS login returned no token"
            )
        session = self._save_login_session(bizrt)
        LOGGER.debug(
            "【六壬推演】短信登录成功: %s, userId=%s",
            _mask(self.username),
            session.user_id,
        )
        return session

    async def async_send_device_verification_sms(self) -> str:
        LOGGER.debug("【六壬推演】请求发送设备验证短信: %s", _mask(self.username))
        response = await self._post(
            DEVICE_SMS_PATH,
            build_device_sms_payload(self.username, self.context),
            authenticated=False,
        )
        data = self._raise_for_error(response)
        bizrt = data.get("bizrt")
        code_key = str(bizrt.get("codeKey", "")) if isinstance(bizrt, Mapping) else ""
        if not code_key:
            raise SgccApiError(
                "missing_code_key",
                "SMS response did not contain codeKey",
                source="srvrt",
            )
        return code_key

    async def async_ensure_login(self) -> LoginSession:
        async with self._login_lock:
            if self.login_session and self.login_session.expires_at > time.time() + 300:
                LOGGER.debug("【六壬推演】登录会话有效, 复用现有令牌")
                return self.login_session
            LOGGER.debug("【六壬推演】登录会话缺失或临近过期, 触发静默重登")
            self.login_session = None
            if not self.password:
                raise SgccAuthError(
                    "saved_password_required", "a saved password is required"
                )
            return await self._async_password_login()

    async def _async_authenticated_data(
        self, path: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        await self.async_ensure_login()
        for attempt in range(2):
            request_session = self.login_session
            response = await self._post(
                path, payload, authenticated=True, request_session=request_session
            )
            try:
                return self._raise_for_error(response)
            except SgccAuthError as error:
                # A challenge is not an expired token; preserve it for reauth.
                if error.code not in AUTH_ERROR_CODES:
                    raise
                LOGGER.debug(
                    "【六壬推演】令牌失效 (code=%s), 重登后重试", error.code
                )
                if self.login_session is request_session:
                    self.login_session = None
                if attempt or not self.password:
                    raise
                try:
                    await self.async_ensure_login()
                except SgccError as login_error:
                    raise login_error from error
        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def _raise_for_business_error(data: Mapping[str, Any], operation: str) -> None:
        code = str(data.get("rtnCode", data.get("returnCode", "")))
        if code not in {"", "0", "1", "0000", "000000"}:
            raise SgccApiError(
                code,
                str(
                    data.get("rtnMsg") or data.get("returnMsg") or f"{operation} failed"
                ),
                source="business",
            )

    async def async_query_daily_usage(
        self, account: PowerAccount, start_date: date, end_date: date
    ) -> tuple[list[DailyReading], float | None]:
        payload = build_daily_usage_payload(account, start_date, end_date)
        data = await self._async_authenticated_data(DAILY_USAGE_PATH, payload)
        self._raise_for_business_error(data, "daily usage query")
        raw_readings = data.get("sevenEleList")
        readings: list[DailyReading] = []
        if isinstance(raw_readings, list):
            for value in raw_readings:
                if not isinstance(value, Mapping):
                    continue
                try:
                    readings.append(DailyReading.from_api(value))
                except ValueError:
                    continue
        total: float | None
        try:
            total = (
                float(data["totalPq"])
                if data.get("totalPq") not in (None, "", "-")
                else None
            )
        except (TypeError, ValueError):
            total = None
        LOGGER.debug(
            "【六壬推演】日明细取回: %s (%s~%s), %d 条",
            _mask(account.account_id, 4),
            start_date,
            end_date,
            len(readings),
        )
        return readings, total

    async def async_query_monthly_bills(
        self, account: PowerAccount, year: int
    ) -> YearlyBilling:
        payload = build_monthly_bills_payload(account, year)
        data = await self._async_authenticated_data(MONTHLY_BILLS_PATH, payload)
        self._raise_for_business_error(data, "monthly bill query")
        billing = YearlyBilling.from_api(data, year)
        LOGGER.debug(
            "【六壬推演】月度账单取回: %s (%d 年), %d 条",
            _mask(account.account_id, 4),
            year,
            len(billing.bills),
        )
        return billing

    async def async_query_account_balance(
        self, account: PowerAccount
    ) -> AccountBalance | None:
        session = await self.async_ensure_login()
        payload = build_account_balance_payload(account, session.user_id)
        data = await self._async_authenticated_data(ACCOUNT_BALANCE_PATH, payload)
        self._raise_for_business_error(data, "account balance query")
        raw_items = data.get("list")
        if not isinstance(raw_items, list):
            LOGGER.debug(
                "【六壬推演】余额取回: %s, 响应无列表", _mask(account.account_id, 4)
            )
            return None
        item = next((value for value in raw_items if isinstance(value, Mapping)), None)
        balance = AccountBalance.from_api(item) if item is not None else None
        LOGGER.debug(
            "【六壬推演】余额取回: %s, %s",
            _mask(account.account_id, 4),
            "命中" if balance else "无数据",
        )
        return balance

    async def async_query_month_end_meter(
        self, account: PowerAccount, bill: MonthlyBill
    ) -> MeterReading | None:
        cache_key = (account.account_id, bill.month)
        if cache_key in self._meter_cache:
            LOGGER.debug(
                "【六壬推演】表码读数命中缓存: %s (%s)",
                _mask(account.account_id, 4),
                bill.month,
            )
            return self._meter_cache[cache_key]
        reading_date = bill.end_date or date(
            bill.month.year,
            bill.month.month,
            calendar.monthrange(bill.month.year, bill.month.month)[1],
        )
        meter_data = await self._async_authenticated_data(
            METER_LIST_PATH,
            build_meter_payload(account, reading_date),
        )
        self._raise_for_business_error(meter_data, "meter list query")
        raw_meters = meter_data.get("list")
        if not isinstance(raw_meters, list):
            return None
        meter = next(
            (value for value in raw_meters if isinstance(value, Mapping)), None
        )
        if meter is None or not meter.get("meterBarCode"):
            LOGGER.debug(
                "【六壬推演】表码列表无有效表计: %s (%s)",
                _mask(account.account_id, 4),
                reading_date,
            )
            return None

        detail_data = await self._async_authenticated_data(
            METER_DETAIL_PATH,
            build_meter_payload(
                account,
                reading_date,
                meter_bar_code=str(meter["meterBarCode"]),
            ),
        )
        self._raise_for_business_error(detail_data, "meter detail query")
        raw_readings = detail_data.get("list")
        if not isinstance(raw_readings, list):
            return None
        readings: list[tuple[float, float]] = []
        for value in raw_readings:
            if not isinstance(value, Mapping):
                continue
            try:
                readings.append((float(value.get("time", 0)), float(value["readPq"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not readings:
            LOGGER.debug(
                "【六壬推演】表码明细无读数: %s (%s)",
                _mask(account.account_id, 4),
                reading_date,
            )
            return None
        result = MeterReading(
            day=reading_date,
            reading=max(readings, key=lambda value: value[0])[1],
        )
        self._meter_cache[cache_key] = result
        LOGGER.debug(
            "【六壬推演】表码读数取回: %s (%s), 抄表值 %s",
            _mask(account.account_id, 4),
            reading_date,
            result.reading,
        )
        return result

    async def async_query_history(
        self, *, months: int = 2, today: date | None = None
    ) -> dict[str, AccountUsage]:
        if months < 1 or months > 6:
            raise ValueError("months must be between 1 and 3")
        today = today or datetime.now(CHINA_TZ).date()
        await self.async_ensure_login()
        if not self.accounts:
            raise SgccApiError(
                "no_power_account", "login returned no bound power account"
            )
        LOGGER.debug(
            "【六壬推演】历史推演开始: %d 个月, %d 个户号", months, len(self.accounts)
        )
        result: dict[str, AccountUsage] = {}
        for index, account in enumerate(self.accounts):
            # 户号间随机停顿, 拟人翻页节奏 (首个户号不停)
            if index:
                pause = random.uniform(3, 8)
                LOGGER.debug("【六壬推演】户号间停顿 %.1f 秒", pause)
                await asyncio.sleep(pause)
            LOGGER.debug(
                "【六壬推演】开始取数: %s (%d/%d)",
                _mask(account.account_id, 4),
                index + 1,
                len(self.accounts),
            )
            # 单账户内互不依赖的请求并发取数 (daily × months + 月度账单 + 余额)
            by_day: dict[date, DailyReading] = {}
            current_total: float | None = None
            daily_tasks = [
                self.async_query_daily_usage(account, *_month_period(today, offset))
                for offset in range(months)
            ]
            daily_results, monthly, billing_account = await asyncio.gather(
                asyncio.gather(*daily_tasks),
                self._safe_monthly_bills(account, today),
                self._safe_account_balance(account),
            )
            for index, (readings, total) in enumerate(daily_results):
                by_day.update({reading.day: reading for reading in readings})
                if index == 0:
                    current_total = total
            current_billing, previous_billing, monthly_bills = monthly

            latest_month_meter: MeterReading | None = None
            if monthly_bills:
                try:
                    latest_month_meter = await self.async_query_month_end_meter(
                        account, monthly_bills[-1]
                    )
                except SgccAuthError:
                    raise
                except (SgccApiError, SgccNetworkError):
                    # App 仅在支持地区返回表码明细
                    LOGGER.debug(
                        "【六壬推演】表码明细不支持, 跳过: %s",
                        _mask(account.account_id, 4),
                    )
                    pass
            result[account.account_id] = AccountUsage(
                account=account,
                readings=tuple(by_day[key] for key in sorted(by_day)),
                current_month_total=current_total,
                as_of=today,
                monthly_bills=monthly_bills,
                current_year_usage=(
                    current_billing.usage if current_billing is not None else None
                ),
                current_year_charge=(
                    current_billing.charge if current_billing is not None else None
                ),
                billing_account=billing_account,
                previous_year_billing=previous_billing,
                latest_month_meter=latest_month_meter,
            )
        LOGGER.debug("【六壬推演】历史推演完成: %d 个户号", len(result))
        return result

    async def _safe_monthly_bills(
        self, account: PowerAccount, today: date
    ) -> tuple[YearlyBilling | None, YearlyBilling | None, tuple[MonthlyBill, ...]]:
        # 今年+去年账单供年汇总; 历史账单当年空则回退去年; 部分地区不返回已结算账单, 日用电仍可用
        current: YearlyBilling | None = None
        previous: YearlyBilling | None = None
        try:
            current = await self.async_query_monthly_bills(account, today.year)
        except SgccAuthError:
            raise
        except (SgccApiError, SgccNetworkError):
            LOGGER.debug("【六壬推演】%d 年账单不可用, 跳过", today.year)
            current = None
        try:
            previous = await self.async_query_monthly_bills(account, today.year - 1)
        except SgccAuthError:
            raise
        except (SgccApiError, SgccNetworkError):
            LOGGER.debug("【六壬推演】%d 年账单不可用, 跳过", today.year - 1)
            previous = None
        bills = (current.bills if current else ()) or (
            previous.bills if previous else ()
        )
        return current, previous, bills

    async def _safe_account_balance(
        self, account: PowerAccount
    ) -> AccountBalance | None:
        # 余额为补充数据, 按省份/户号类型而异
        try:
            return await self.async_query_account_balance(account)
        except SgccAuthError:
            raise
        except (SgccApiError, SgccNetworkError):
            LOGGER.debug(
                "【六壬推演】余额查询失败, 按无余额处理: %s",
                _mask(account.account_id, 4),
            )
            return None