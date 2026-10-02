"""SGCC 数据协调器."""
from __future__ import annotations

import hashlib
import random
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    LOGGER,
    MAX_REQUESTS_PER_DAY,
    QUOTA_WINDOW_SECONDS,
    SYNC_ANCHOR_HOUR,
    SYNC_ANCHOR_WINDOW_SECONDS,
)
from .sgcc.const import CHINA_TZ
from .sgcc.models import is_placeholder_field

def _display_cons(cons_no: str) -> str:
    # 正常数据 key 已是真实户号; 若仍混入加密复合串(含":"), 显示空白而非乱码
    return cons_no if cons_no and ":" not in cons_no else ""

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

class SgccCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """数据异步占验、风控拦截与数理推演协调器."""

    def __init__(self, hass: HomeAssistant, api, entry, version):
        self.api = api
        self.entry = entry
        self.version = version

        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            # 占位值: 每次 _async_update_data 结束都会按锚点/封印/重试重设
            update_interval=timedelta(minutes=1),
        )

        safe_id = hashlib.md5(self.api.username.encode()).hexdigest()[:16]
        # 数据缓存与登录态 (_auth, storage.py) 刻意分文件: 丢失代价/写入频率不同, 清缓存不伤登录态
        self.storage_key = f"{DOMAIN}.{safe_id}_cache"
        self._store = Store(hass, 1, self.storage_key)
        self.storage_data = {
            "account_locked_until": 0,
            "rk001_count": 0,
            "login_history": [],
            "request_history": [],
            "last_sync_date": "",
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

    def _anchor_for(self, day: date) -> datetime:
        """账号在指定日期的同步锚点 (9 点起 12h 窗口内秒级散布, 每日恒定)."""
        digest = int(hashlib.md5(self.api.username.encode()).hexdigest(), 16)
        offset = digest % SYNC_ANCHOR_WINDOW_SECONDS
        return datetime.combine(
            day, dtime(SYNC_ANCHOR_HOUR, tzinfo=CHINA_TZ)
        ) + timedelta(seconds=offset)

    def _synced_today(self, now: datetime) -> bool:
        return self.storage_data.get("last_sync_date") == now.date().isoformat()

    def _should_sync(self, now: datetime) -> bool:
        """未同步过或断连 ≥2 天 → 立即; 昨天同步过 → 等今天锚点; 今天已同步 → 跳过."""
        last = self.storage_data.get("last_sync_date")
        if not last:
            return True
        try:
            stale_days = (now.date() - date.fromisoformat(last)).days
        except ValueError:
            return True
        if stale_days >= 2:
            return True
        if stale_days <= 0:
            return False
        return now >= self._anchor_for(now.date())

    def _schedule_anchor_wake(self, now: datetime) -> None:
        base = self._anchor_for(now.date())
        if now >= base or self._synced_today(now):
            base = self._anchor_for(now.date() + timedelta(days=1))
        wake = base + timedelta(seconds=random.randint(0, 59))
        self.update_interval = max(wake - now, timedelta(seconds=30))
        LOGGER.debug("【六壬推演】下次推演锚点: %s", wake.strftime("%Y-%m-%d %H:%M:%S"))

    def _schedule_seal_wake(self, until_ts: float) -> None:
        # 封印期内不空转, 一觉睡到解封后 1 分钟
        self.update_interval = timedelta(seconds=max(60, until_ts - time.time() + 60))

    def _schedule_retry(self) -> None:
        # 失败/受阻后的短周期重试, 与锚点节奏解耦
        self.update_interval = timedelta(minutes=random.randint(30, 60))

    async def _async_update_data(self):
        """执行异步占验，严守门卫规制."""
        current_entries = self.hass.config_entries.async_entries(DOMAIN)
        if len(current_entries) > self._get_gate_limit():
            LOGGER.critical("因果失衡：环境定力不足以承载过多推演任务")
            self._schedule_retry()
            raise UpdateFailed("三才失衡，推演终止")

        now = datetime.now(CHINA_TZ)
        now_ts = int(time.time())
        MAX_REQ = MAX_REQUESTS_PER_DAY

        last_data = self.storage_data.get("last_data", {})

        if self.storage_data.get("account_locked_until", 0) > now_ts:
            remaining = (self.storage_data["account_locked_until"] - now_ts) // 3600
            LOGGER.warning("【六壬推演】风控封印中，拦截请求，剩余 %d 小时", remaining)
            self._schedule_seal_wake(self.storage_data["account_locked_until"])
            return last_data

        if not self._should_sync(now):
            LOGGER.debug("【六壬推演】今日推演已完成或未到锚点，直接返回缓存")
            self._schedule_anchor_wake(now)
            return last_data

        req_history = [
            ts for ts in self.storage_data.get("request_history", [])
            if now_ts - ts < QUOTA_WINDOW_SECONDS
        ]
        self.storage_data["request_history"] = req_history
        if len(req_history) >= MAX_REQ:
            LOGGER.warning("【六壬推演】请求配额已耗尽 (%dh/%d次)，拦截请求", QUOTA_WINDOW_SECONDS // 3600, MAX_REQ)
            self.update_interval = timedelta(hours=2)
            return last_data

        req_history.append(now_ts)
        self.storage_data["request_history"] = req_history
        LOGGER.info("【六壬推演】发起同步请求 (%d/%d)", len(req_history), MAX_REQ)

        try:
            session = async_get_clientsession(self.hass)
            raw_results = await self.api.async_get_full_data(session)

            self.storage_data["last_sync_date"] = now.date().isoformat()
            self.storage_data["rk001_count"] = 0

            result = await self._process_and_save(raw_results)
            self._schedule_anchor_wake(datetime.now(CHINA_TZ))
            return result

        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            err_msg = str(err)
            if "RK001" in err_msg or "RK007" in err_msg:
                self.storage_data["rk001_count"] += 1
                lock_hours = 48 if self.storage_data["rk001_count"] >= 3 else 24
                self.storage_data["account_locked_until"] = now_ts + (lock_hours * 3600)
                LOGGER.error("【六壬推演】触发 RK 风控封印 %d 小时", lock_hours)
                self._schedule_seal_wake(now_ts + lock_hours * 3600)
            else:
                self._schedule_retry()

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
                "户号": _display_cons(cons_no),
                "户主": "" if is_placeholder_field(entry.get("cons_name")) else (entry.get("cons_name") or ""),
                "地址": "" if is_placeholder_field(entry.get("address")) else (entry.get("address") or ""),
                "供电单位": entry.get("org_name") or entry.get("orgName") or "",
                "省代码": entry.get("pro_code") or "",
                "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
                    "尖峰": _safe_float(d.get("thisTPq")),
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
                            "年份": year_info.get("year"),
                            "去年总电量": year_info.get("prevTotalEleNum"),
                            "去年总电费": year_info.get("prevTotalEleCost"),
                            "去年年份": year_info.get("prevYear")
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
                        "本月尖峰电量": tm.get("tip"),
                        "每天趋势": daily_list
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
                    "attrs": {
                        "年度总电费": year_info.get("totalEleCost"),
                        "年份": year_info.get("year"),
                        "去年总电量": year_info.get("prevTotalEleNum"),
                        "去年总电费": year_info.get("prevTotalEleCost"),
                        "去年年份": year_info.get("prevYear")
                    }
                }
            }

        old_data = self.storage_data.get("last_data", {})
        for old_no, old_val in old_data.items():
            if old_no in new_data_map:
                continue
            # 历史缓存曾以加密复合串为 key, 跳过以免僵尸设备复活
            if ":" in old_no:
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
            name=f"户号：{_display_cons(cons_no)}",
            manufacturer="SGCC DLR.",
            model="SGCC Client",
            entry_type=DeviceEntryType.SERVICE,
            sw_version=self.version,
        )