"""SGCC API 客户端代理."""
from __future__ import annotations

import asyncio
import random
import time

from .const import DOMAIN, LOGGER
from .sgcc import SgccAssetLoader

RK001_COOLDOWN_STEP = 600
RK001_COOLDOWN_MAX = 12 * 3600

def _account_limit() -> int:
    try:
        return len(DOMAIN.split('_')[0])
    except Exception:
        return 3

class LoginCooldown(Exception):
    """RK001 封印冷却: 封印期内不重复起课."""

class SgccClientProxy:
    """占测代理类 (轻量化壳)."""

    def __init__(self, hass, username, password):
        self.hass = hass
        self.username = username
        self.password = password
        self.core_instance = None
        self.rk001_locked_until = 0

    async def _ensure_core(self):
        if self.core_instance is None:
            core_class = await self.hass.async_add_executor_job(
                SgccAssetLoader.load_core
            )
            if core_class:
                self.core_instance = core_class(logger=self)
                self.debug("核心推演逻辑已成功载入内存")
            else:
                raise Exception("天机不可泄露：核心资产读取失败或文件不存在")
        return self.core_instance

    def _log_占い(self, msg: str): LOGGER.info(f"【六壬推演】{msg}")

    def step(self, n, m): self._log_占い(f"第{n}步: {m}")
    def ok(self, m): self._log_占い(f"✓ {m}")
    def err(self, m): LOGGER.error(f"【六壬推演】✗ {m}")
    def info(self, m): self._log_占い(m)
    def debug(self, m): LOGGER.debug(f"【六壬推演】{m}")
    def data(self, m): LOGGER.debug(f"【六壬推演】→ {m}")
    def warning(self, m): LOGGER.warning(m)
    def error(self, m): LOGGER.error(m)

    async def _request_shift(self):
        """多账户相位位移: 条目数超限时随机延迟错峰."""
        try:
            entries_count = len(self.hass.config_entries.async_entries(DOMAIN))
        except Exception:
            return
        if entries_count > _account_limit():
            delay = random.randint(10, 20)
            self.warning(f"【六壬推演】因果纠缠过多，执行相位延迟 {delay} 秒")
            await asyncio.sleep(delay)

    def _apply_rk001_penalty(self) -> int:
        now = int(time.time())
        base = max(self.rk001_locked_until, now)
        self.rk001_locked_until = min(
            base + RK001_COOLDOWN_STEP, now + RK001_COOLDOWN_MAX
        )
        return self.rk001_locked_until - now

    async def _login_cooldown(self) -> bool:
        now = int(time.time())
        if self.rk001_locked_until > now:
            remaining = self._apply_rk001_penalty()
            self.warning(
                f"【六壬推演】RK001 封印中强行起课，封印累加，需静默 {remaining // 60} 分钟"
            )
            return True
        return False

    async def async_get_full_data(self, session):
        await self._request_shift()
        if await self._login_cooldown():
            raise LoginCooldown("RK001 封印中，定力未复")

        core = await self._ensure_core()

        try:
            await core.do_login_chain(session, self.username, self.password)

            user_list = await core.get_bind_info(session, self.username, self.password)

            results = []
            for user in user_list:
                cons_no = user.get("consNo_dst") or user.get("consNo")
                if not cons_no:
                    self.warning(f"跳过无户号的绑定记录: {str(user)[:120]}")
                    continue
                self.info(f"开始测算户号: {cons_no[:4]}***")

                balance = await core.get_balance(session, user, self.username, self.password)
                daily_raw = await core.get_daily_usage_raw(session, user, self.username, self.password)
                monthly = await core.get_monthly_usage_raw(session, user, self.username, self.password)

                this_month = core.calculate_current_month(daily_raw)

                results.append({
                    "cons_no": cons_no,
                    "cons_name": user.get("consName_dst") or user.get("consName") or user.get("realName"),
                    "address": user.get("elecAddr_dst") or user.get("elecAddr") or user.get("address"),
                    "org_name": user.get("orgName") or "",
                    "pro_code": user.get("proNo") or user.get("provinceId") or user.get("provinceCode") or "32101",
                    "balance": balance,
                    "this_month": this_month,
                    "monthly_history": monthly,
                    "daily_history": daily_raw.get("sevenEleList", []) if isinstance(daily_raw, dict) else []
                })

            self.rk001_locked_until = 0
            return results

        except Exception as err:
            err_msg = str(err)
            if "RK001" in err_msg:
                remaining = self._apply_rk001_penalty()
                self.err(f"触发 RK001 风控封印，登录限制 {remaining // 60} 分钟")
            else:
                self.err(f"推演格局中断: {err_msg}")
            raise err
