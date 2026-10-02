"""SGCC API 客户端代理."""
from __future__ import annotations

import asyncio
import random
import time
from functools import partial
from typing import Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ACCOUNT_LIMIT,
    DEFAULT_HISTORY_MONTHS,
    DOMAIN,
    LOGGER,
)
from .sgcc.client import (
    SgccApiError,
    SgccAppClient,
    SgccAuthError,
    SgccDeviceVerificationRequired,
    SgccInteractiveChallengeRequired,
    SgccNetworkError,
)
from .sgcc.models import AccountUsage, LoginSession
from .sgcc.synthetic_device import build_device_profile, create_device_state
from .storage import async_load_session, async_save_session

RK001_COOLDOWN_STEP = 600
RK001_COOLDOWN_MAX = 12 * 3600

class LoginCooldown(Exception):
    """RK001 封印冷却."""

class SgccClientProxy:
    """占测代理类: 包装 SgccAppClient 完成国密直连登录与数据取数."""

    def __init__(self, hass, username, password, entry=None):
        self.hass = hass
        self.username = username
        self.password = password
        self.entry = entry
        self._api: SgccAppClient | None = None
        self._device_state: dict[str, Any] = {}
        self.rk001_locked_until = 0

    def info(self, msg: str) -> None:
        LOGGER.info(f"【六壬推演】{msg}")

    def err(self, msg: str) -> None:
        LOGGER.error(f"【六壬推演】✗ {msg}")

    async def _request_shift(self):
        try:
            entries_count = len(self.hass.config_entries.async_entries(DOMAIN))
        except Exception:
            return
        if entries_count > ACCOUNT_LIMIT:
            delay = random.randint(10, 20)
            LOGGER.warning(f"【六壬推演】因果纠缠过多，执行相位延迟 {delay} 秒")
            await asyncio.sleep(delay)

    def _apply_rk001_penalty(self) -> int:
        now = int(time.time())
        base = max(self.rk001_locked_until, now)
        self.rk001_locked_until = min(base + RK001_COOLDOWN_STEP, now + RK001_COOLDOWN_MAX)
        return self.rk001_locked_until - now

    async def _login_cooldown(self) -> bool:
        now = int(time.time())
        if self.rk001_locked_until > now:
            remaining = self._apply_rk001_penalty()
            LOGGER.warning(f"【六壬推演】RK001 封印中强行起课，需静默 {remaining // 60} 分钟")
            return True
        return False

    async def _build_client(self) -> None:
        if self._api is not None:
            return
        cached = await async_load_session(self.hass, self.username) or {}
        self._device_state = dict(cached.get("device_state") or create_device_state())
        profile, self._device_state = await self.hass.async_add_executor_job(
            partial(build_device_profile, self._device_state)
        )
        login_session = LoginSession.from_dict(cached.get("login_session"))
        self._api = SgccAppClient(
            async_get_clientsession(self.hass),
            username=self.username,
            password=self.password,
            profile=profile,
            login_session=login_session,
            profile_provider=self._profile_provider,
        )

    async def _profile_provider(self):
        profile, self._device_state = await self.hass.async_add_executor_job(
            partial(build_device_profile, dict(self._device_state))
        )
        return profile

    async def _persist_session(self) -> None:
        if self._api is None:
            return
        await async_save_session(
            self.hass,
            self.username,
            device_state=self._device_state,
            login_session=(
                self._api.login_session.as_dict() if self._api.login_session else None
            ),
        )

    # ---- 数据取数 ----
    async def async_get_full_data(self, session):
        """登录(或复用令牌)并取回全部户号数据, 映射为协调器期望的形状."""
        await self._request_shift()
        if await self._login_cooldown():
            raise LoginCooldown("RK001 封印中，定力未复")

        await self._build_client()
        assert self._api is not None
        try:
            history = await self._api.async_query_history(months=DEFAULT_HISTORY_MONTHS)
        except (SgccDeviceVerificationRequired, SgccInteractiveChallengeRequired) as err:
            raise ConfigEntryAuthFailed(f"需要重新认证 (新设备/交互式验证): {err}") from err
        except SgccAuthError as err:
            # RK007 (伪装"网络超时"的风控拦截) 是临时冷却, 封印等待自愈, 不作废认证
            if "RK007" in str(err):
                self._apply_rk001_penalty()
                raise LoginCooldown(f"RK007 风控冷却: {err}") from err
            raise ConfigEntryAuthFailed(f"认证失败: {err}") from err
        except (SgccNetworkError, SgccApiError) as err:
            raise

        if self._api.login_session is not None:
            await self._persist_session()

        results = [self._map_usage(u) for u in history.values()]
        self.rk001_locked_until = 0
        self.info(f"推演格局已成，共测得 {len(results)} 户")
        return results

    @staticmethod
    def _map_usage(usage: AccountUsage) -> dict[str, Any]:
        """把 SgccAppClient 的 AccountUsage 映射回协调器 _process_and_save 期望的旧数据形状."""
        account = usage.account
        bal = usage.billing_account
        today = usage.as_of
        balance = {
            "sumMoney": bal.sum_money if bal else None,
            "prepayBal": bal.prepay_balance if bal else None,
            "totalPq": bal.total_pq if bal else None,
            "estiAmt": bal.esti_amt if bal else None,
        }
        this_month = {
            # 直连响应常无 totalPq, 回退为当月日用量累加 (对齐旧版实测行为)
            "total": (
                usage.current_month_total
                if usage.current_month_total is not None
                else usage.month_sum("usage", today)
            ),
            "peak": usage.month_sum("peak", today),
            "flat": usage.month_sum("flat", today),
            "valley": usage.month_sum("valley", today),
            "tip": usage.month_sum("tip", today),
            "month": today.strftime("%Y-%m"),
        }
        moth_ele_list = [
            {
                "month": bill.month.strftime("%Y%m"),
                "monthEleNum": bill.usage,
                "monthEleCost": bill.charge,
            }
            for bill in usage.monthly_bills
        ]
        prev_billing = usage.previous_year_billing
        data_info = {
            "totalEleNum": usage.current_year_usage,
            "totalEleCost": usage.current_year_charge,
            "year": today.year,
            "prevTotalEleNum": prev_billing.usage if prev_billing else None,
            "prevTotalEleCost": prev_billing.charge if prev_billing else None,
            "prevYear": prev_billing.year if prev_billing else None,
        }
        daily = [
            {
                "day": r.day.isoformat(),
                "dayElePq": r.usage,
                "thisPPq": r.peak,
                "thisNPq": r.flat,
                "thisVPq": r.valley,
                "thisTPq": r.tip,
            }
            for r in usage.readings
        ]
        return {
            "cons_no": account.cons_no_src,
            "cons_name": account.name,
            "address": account.address,
            "org_name": account.org_no,
            "pro_code": account.pro_no,
            "balance": balance,
            "this_month": this_month,
            "monthly_history": {"mothEleList": moth_ele_list, "dataInfo": data_info},
            "daily_history": daily,
        }