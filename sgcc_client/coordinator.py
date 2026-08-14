"""SGCC 数据协调器."""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_SCAN_INTERVAL,
    DATA_TTL_SECONDS,
    DOMAIN,
    LOGGER,
    MAX_REQUESTS_PER_DAY,
)

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default

def _month_key(m: dict) -> str:
    s = re.sub(r"\D", "", str(m.get("月份") or ""))
    if len(s) == 5:
        return s[:4] + "0" + s[4:]
    return s

class SgccCoordinator(DataUpdateCoordinator):
    """数据异步占验、风控拦截与数理推演协调器."""

    def __init__(self, hass: HomeAssistant, api, entry, version):
        self.api = api
        self.entry = entry
        self.version = version
        scan_interval_hours = int(entry.options.get(CONF_SCAN_INTERVAL, 12))

        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=scan_interval_hours),
        )

        safe_id = hashlib.md5(self.api.username.encode()).hexdigest()[:16]
        self.storage_key = f"{DOMAIN}.{safe_id}_cache"
        self._store = Store(hass, 1, self.storage_key)
        self.storage_data = {
            "account_locked_until": 0,
            "rk001_count": 0,
            "login_history": [],
            "request_history": [],
            "last_success_ts": 0,
            "last_data": {},
            "removed_cons": [],
        }

    def _get_gate_limit(self) -> int:
        """动态计算环境定力上限."""
        try:
            return len(DOMAIN.split('_')[0])
        except Exception:
            return 3

    def _is_cons_removed(self, cons_no: str) -> bool:
        """判定某户号是否已被用户移除."""
        return cons_no in self.storage_data.get("removed_cons", [])

    async def async_remove_cons(self, cons_no: str) -> None:
        """移除单个户号设备及其缓存, 并在刷新时持续屏蔽."""
        self.storage_data["last_data"].pop(cons_no, None)
        removed = self.storage_data.setdefault("removed_cons", [])
        if cons_no not in removed:
            removed.append(cons_no)
        self.data = self.storage_data["last_data"]
        await self._store.async_save(self.storage_data)
        LOGGER.info("【六壬推演】已注销户号 %s 的推演轨迹", cons_no[:4] + "***")

    async def async_reset_removed(self) -> None:
        """重新配置账户时清空移除列表, 让所有户号重新显现."""
        if self.storage_data.get("removed_cons"):
            self.storage_data["removed_cons"] = []
            await self._store.async_save(self.storage_data)

    async def _async_setup(self):
        cache = await self._store.async_load()
        if cache:
            if not cache.get("request_history") and cache.get("login_history"):
                cache["request_history"] = list(cache["login_history"])
            self.storage_data.update(cache)
            if self.storage_data.get("last_data"):
                self.data = self.storage_data["last_data"]

    async def _async_update_data(self):
        """执行异步占验，严守门卫规制."""
        current_entries = self.hass.config_entries.async_entries(DOMAIN)
        if len(current_entries) > self._get_gate_limit():
            LOGGER.critical("因果失衡：环境定力不足以承载过多推演任务")
            raise UpdateFailed("三才失衡，推演终止")

        now_ts = int(time.time())
        TTL = DATA_TTL_SECONDS
        MAX_REQ = MAX_REQUESTS_PER_DAY

        last_data = self.storage_data.get("last_data", {})
        last_success_ts = self.storage_data.get("last_success_ts", 0)

        if self.storage_data.get("account_locked_until", 0) > now_ts:
            remaining = (self.storage_data["account_locked_until"] - now_ts) // 3600
            LOGGER.warning("【六壬推演】风控封印中，拦截请求，剩余 %d 小时", remaining)
            return last_data

        if last_data and last_success_ts and (now_ts - last_success_ts) < TTL:
            LOGGER.debug("【六壬推演】数据未过期，直接返回缓存")
            return last_data

        req_history = [
            ts for ts in self.storage_data.get("request_history", [])
            if now_ts - ts < TTL
        ]
        self.storage_data["request_history"] = req_history
        if len(req_history) >= MAX_REQ:
            LOGGER.warning("【六壬推演】请求配额已耗尽 (%dh/%d次)，拦截请求", TTL // 3600, MAX_REQ)
            return last_data

        req_history.append(now_ts)
        self.storage_data["request_history"] = req_history
        LOGGER.info("【六壬推演】发起同步请求 (%d/%d)", len(req_history), MAX_REQ)

        try:
            session = async_get_clientsession(self.hass)
            raw_results = await self.api.async_get_full_data(session)

            self.storage_data["last_success_ts"] = now_ts
            self.storage_data["rk001_count"] = 0

            return await self._process_and_save(raw_results)

        except Exception as err:
            err_msg = str(err)
            if "RK001" in err_msg:
                self.storage_data["rk001_count"] += 1
                lock_hours = 48 if self.storage_data["rk001_count"] >= 3 else 24
                self.storage_data["account_locked_until"] = now_ts + (lock_hours * 3600)
                LOGGER.error("【六壬推演】触发 RK001 封印 %d 小时", lock_hours)

            LOGGER.error("【六壬推演】同步异常: %s，回溯旧数据", err_msg)

            await self._store.async_save(self.storage_data)

            if not last_data:
                raise
            return last_data

    async def _process_and_save(self, raw_results: list) -> dict:
        new_data_map = {}
        for entry in raw_results:
            cons_no = entry["cons_no"]
            if not cons_no:
                continue
            if self._is_cons_removed(cons_no):
                LOGGER.debug("【六壬推演】户号 %s 已被注销，跳过推演", cons_no[:4] + "***")
                continue
            bal = entry.get("balance", {})
            tm = entry.get("this_month", {})
            daily_raw = entry.get("daily_history", [])
            monthly_raw = entry.get("monthly_history", {}).get("mothEleList", [])
            year_info = entry.get("monthly_history", {}).get("dataInfo", {})

            base_info = {
                "户号": cons_no,
                "户主": entry.get("cons_name"),
                "地址": entry.get("address"),
                "供电单位": entry.get("org_name") or entry.get("orgName") or "",
                "省代码": entry.get("pro_code") or "",
                "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "数据来源": entry.get("source", "fresh")
            }

            daily_list = []
            for d in daily_raw:
                day_pq = str(d.get("dayElePq") or "")
                if day_pq == "-":
                    continue
                daily_list.append({
                    "日期": d.get("day"),
                    "总电量": _safe_float(day_pq),
                    "峰": _safe_float(d.get("thisPPq")),
                    "平": _safe_float(d.get("thisNPq")),
                    "谷": _safe_float(d.get("thisVPq")),
                })

            month_list = [{"月份": m.get("month"), "月用电量(kWh)": _safe_float(m.get("monthEleNum")),
                           "月电费(元)": _safe_float(m.get("monthEleCost"))} for m in monthly_raw]

            latest_month = max(month_list, key=_month_key) if month_list else None

            new_data_map[cons_no] = {
                "balance_entity": {
                    "state": _safe_float(bal.get("sumMoney")),
                    "attrs": {
                        **base_info,
                        "预付余额": bal.get("prepayBal"),
                        "账户余额": bal.get("sumMoney"),
                        "上期电量": bal.get("totalPq"),
                        "预计可用": bal.get("estiAmt"),
                        "本期电量": _safe_float(tm.get("total")),
                        "每日明细": daily_list,
                        "每月明细": month_list,
                        "年度汇总": {
                            "总电量": year_info.get("totalEleNum"),
                            "总电费": year_info.get("totalEleCost"),
                            "年份": year_info.get("year")
                        }
                    }
                },
                "month_acc_entity": {
                    "state": _safe_float(tm.get("total")),
                    "attrs": {
                        "统计月份": tm.get("month"),
                        "本月峰电量": tm.get("peak"),
                        "本月平电量": tm.get("flat"),
                        "本月谷电量": tm.get("valley"),
                        "40天趋势": daily_list
                    }
                },
                "monthly_bill_entity": {
                    "state": latest_month.get("月用电量(kWh)", 0) if latest_month else 0,
                    "attrs": {
                        "统计月份": latest_month.get("月份") if latest_month else None,
                        "历史账单": month_list,
                    }
                },
                "yearly_summary_entity": {
                    "state": _safe_float(year_info.get("totalEleNum")),
                    "attrs": { "年度总电费": year_info.get("totalEleCost"), "年份": year_info.get("year") }
                }
            }

        old_data = self.storage_data.get("last_data", {})
        for old_no, old_val in old_data.items():
            if old_no in new_data_map:
                continue
            if self._is_cons_removed(old_no):
                continue
            new_data_map[old_no] = old_val

        self.storage_data["last_data"] = new_data_map
        await self._store.async_save(self.storage_data)

        return new_data_map

    def device_info(self, cons_no) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.api.username}_{cons_no}")},
            name=f"户号：{cons_no}",
            manufacturer="SGCC DLR.",
            model="SGCC Client",
            entry_type=DeviceEntryType.SERVICE,
            sw_version=self.version,
            configuration_url="https://github.com/JaxonRidge/ha_sgcc_client",
        )
