"""SGCC 配置流."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SgccClientProxy
from .const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
)

def _account_limit() -> int:
    try:
        return len(DOMAIN.split('_')[0])
    except Exception:
        return 3

DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
})

async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """验证输入有效性并测试登录."""
    session = async_get_clientsession(hass)
    api = SgccClientProxy(
        hass,
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )

    try:
        core_class = await api._ensure_core()
        await core_class.do_login_chain(session, data[CONF_USERNAME], data[CONF_PASSWORD])

        return {
            "title": f"{data[CONF_USERNAME][:3]}****{data[CONF_USERNAME][-4:]}",
        }
    except Exception as err:
        err_msg = str(err)
        LOGGER.error("【调试】验证过程真实报错: %s", err_msg)
        if "RK001" in err_msg:
            raise RiskControlBlocked
        if "password" in err_msg or "失败" in err_msg:
            raise InvalidAuth
        LOGGER.error("推演验证异常: %s", err_msg)
        raise CannotConnect

class SgccConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """处理集成配置逻辑."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """(免责协议勾选)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("accept_terms") is True:
                return await self.async_step_account()
            errors["base"] = "terms_not_accepted"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("accept_terms", default=False): bool,
            }),
            errors=errors,
        )

    async def async_step_account(self, user_input=None) -> FlowResult:
        """配置."""
        if self.source != config_entries.SOURCE_REAUTH:
            if len(self._async_current_entries()) >= _account_limit():
                return self.async_abort(reason="limit_exceeded")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                if not user_input[CONF_USERNAME].isdigit():
                    errors["base"] = "invalid_username_format"
                else:
                    if self.source == config_entries.SOURCE_REAUTH:
                        entry = self._reauth_entry
                        user_input[CONF_USERNAME] = entry.data.get(CONF_USERNAME)
                        info = await validate_input(self.hass, user_input)
                        return self.async_update_reload_and_abort(
                            entry,
                            data={**entry.data, **user_input},
                        )

                    await self.async_set_unique_id(user_input[CONF_USERNAME])
                    self._abort_if_unique_id_configured()

                    info = await validate_input(self.hass, user_input)

                    return self.async_create_entry(
                        title=info["title"],
                        data=user_input,
                        options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL}
                    )

            except RiskControlBlocked:
                errors["base"] = "rk001_blocked"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("推演格局异常")
                errors["base"] = "unknown"

        schema = DATA_SCHEMA
        if self.source == config_entries.SOURCE_REAUTH:
            default_user = (self._reauth_entry.data.get(CONF_USERNAME)
                            if hasattr(self, "_reauth_entry") else "")
            schema = vol.Schema({
                vol.Required(CONF_USERNAME, default=default_user): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
            })

        return self.async_show_form(
            step_id="account",
            data_schema=schema,
            errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """重新认证处理."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_account()

    async def async_step_reconfigure(self, user_input=None) -> FlowResult:
        """重新配置现有条目."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        errors = {}

        if user_input is not None:
            try:
                if not user_input[CONF_USERNAME].isdigit():
                    errors["base"] = "invalid_username_format"
                else:
                    await self.async_set_unique_id(user_input[CONF_USERNAME])
                    self._abort_if_unique_id_configured()
                    await validate_input(self.hass, user_input)
                    coordinator = entry.runtime_data if entry else None
                    if coordinator is not None:
                        await coordinator.async_reset_removed()
                    return self.async_update_reload_and_abort(
                        entry, data={**entry.data, **user_input}
                    )
            except RiskControlBlocked:
                errors["base"] = "rk001_blocked"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("重新配置异常")
                errors["base"] = "reconfigure_failed"

        schema = vol.Schema({
            vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME)): str,
            vol.Required(CONF_PASSWORD): str,
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> SgccOptionsFlowHandler:
        """关联选项流."""
        return SgccOptionsFlowHandler()

class SgccOptionsFlowHandler(config_entries.OptionsFlow):
    """处理集成选项更新."""

    async def async_step_init(self, user_input=None) -> FlowResult:
        """选项界面：配置推演周期."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options_schema = vol.Schema({
            vol.Required(
                CONF_SCAN_INTERVAL
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "3", "label": "3 小时"},
                        {"value": "6", "label": "6 小时"},
                        {"value": "9", "label": "9 小时"},
                        {"value": "12", "label": "12 小时"},
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        })

        options_schema = self.add_suggested_values_to_schema(
            options_schema, self.config_entry.options
        )
        return self.async_show_form(step_id="init", data_schema=options_schema)

class CannotConnect(exceptions.HomeAssistantError):
    """连接错误."""
class InvalidAuth(exceptions.HomeAssistantError):
    """认证失败."""
class RiskControlBlocked(exceptions.HomeAssistantError):
    """触发 RK001 风控封印."""
