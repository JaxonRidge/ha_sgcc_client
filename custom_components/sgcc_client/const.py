"""SGCC 常量定义."""
from __future__ import annotations

import logging

from homeassistant.const import Platform

PLATFORMS: list[Platform] = [Platform.SENSOR]

LOGGER = logging.getLogger(__package__)

DOMAIN = "sgcc_client"
# 账户条目数量上限 (从域名词根推导, sgcc_client -> "sgcc" -> 4)
ACCOUNT_LIMIT = len(DOMAIN.split("_")[0])
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ENABLE_CARD = "enable_frontend_card"

DEFAULT_ENABLE_CARD = False
DEFAULT_HISTORY_MONTHS = 2
QUOTA_WINDOW_SECONDS = 24 * 3600
MAX_REQUESTS_PER_DAY = 4

SYNC_ANCHOR_HOUR = 9
SYNC_ANCHOR_WINDOW_SECONDS = 12 * 3600