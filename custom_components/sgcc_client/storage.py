"""SGCC 登录态持久化."""
from __future__ import annotations

import hashlib

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORE_VERSION = 1

def auth_store_key(username: str) -> str:
    """按账号生成鉴权仓库键 (与协调器数据缓存 _cache 区分)."""
    safe_id = hashlib.md5(username.encode()).hexdigest()[:16]
    return f"{DOMAIN}.{safe_id}_auth"

def get_auth_store(hass: HomeAssistant, username: str) -> Store:
    """获取账号级鉴权仓库实例."""
    return Store(hass, STORE_VERSION, auth_store_key(username))

async def async_save_session(
    hass: HomeAssistant,
    username: str,
    *,
    device_state: dict,
    login_session: dict | None,
) -> None:
    """持久化合成设备态与登录态 (一次落盘)."""
    await get_auth_store(hass, username).async_save(
        {"device_state": device_state, "login_session": login_session}
    )

async def async_load_session(hass: HomeAssistant, username: str) -> dict | None:
    """读取账号级鉴权数据; 无数据返回 None."""
    return await get_auth_store(hass, username).async_load()

async def async_clear_session(hass: HomeAssistant, username: str) -> None:
    """清理账号级鉴权仓库."""
    await get_auth_store(hass, username).async_remove()