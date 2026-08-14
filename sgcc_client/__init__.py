"""SGCC 集成入口."""
from __future__ import annotations

import asyncio
import hashlib
import os
import random

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .api import SgccClientProxy
from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN, LOGGER, PLATFORMS
from .coordinator import SgccCoordinator

type SgccConfigEntry = ConfigEntry[SgccCoordinator]

def _account_limit() -> int:
    try:
        return len(DOMAIN.split('_')[0])
    except Exception:
        return 3

async def async_setup_entry(hass: HomeAssistant, entry: SgccConfigEntry) -> bool:
    """集成启动入口."""
    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version) or "1.0.0"

    current_entries = hass.config_entries.async_entries(DOMAIN)
    if len(current_entries) > _account_limit():
        LOGGER.error("环境定力不足以承载过多推演任务，请保持三才平衡。")
        return False

    if f"{DOMAIN}_assets_registered" not in hass.data:
        local_path = hass.config.path("custom_components", DOMAIN, "www")
        if os.path.exists(local_path):
            await hass.http.async_register_static_paths([
                StaticPathConfig(f"/{DOMAIN}-local", local_path, False)
            ])
            frontend.add_extra_js_url(hass, f"/{DOMAIN}-local/sgcc-client-card.js?v={version}")
            hass.data[f"{DOMAIN}_assets_registered"] = True

    api = SgccClientProxy(
        hass,
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    coordinator = SgccCoordinator(hass, api, entry, version)

    if len(current_entries) > 1:
        jitter = random.randint(5, 45)
        LOGGER.debug("多账号并发保护，执行相位偏移: %s 秒", jitter)
        await asyncio.sleep(jitter)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True

async def async_reload_entry(hass: HomeAssistant, entry: SgccConfigEntry) -> None:
    """重载集成."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: SgccConfigEntry) -> bool:
    """卸载集成."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """彻底删除集成并清理本地持久化 Store."""
    username = entry.data.get(CONF_USERNAME)
    if username:
        safe_id = hashlib.md5(username.encode()).hexdigest()[:16]
        storage_key = f"{DOMAIN}.{safe_id}_cache"
        store = Store(hass, 1, storage_key)
        await store.async_remove()
        LOGGER.info("已清理账号 [%s] 的本地持久化数据", username[:3] + "****")

async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """移除单个设备 (单个户号), 不删除整个账户."""
    coordinator: SgccCoordinator | None = config_entry.runtime_data
    if coordinator is None:
        return False

    username = config_entry.data.get(CONF_USERNAME, "")
    target_cons = None
    for ident in device_entry.identifiers:
        if len(ident) == 2 and ident[0] == DOMAIN:
            parts = ident[1].split("_", 1)
            if len(parts) == 2 and parts[0] == username:
                target_cons = parts[1]
                break

    if not target_cons:
        return False

    await coordinator.async_remove_cons(target_cons)
    return True
