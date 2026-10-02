"""SGCC 配置流."""
from __future__ import annotations

from typing import Any
import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ACCOUNT_LIMIT,
    CONF_ENABLE_CARD,
    CONF_PASSWORD,
    CONF_USERNAME,
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
from .sgcc.synthetic_device import build_device_profile, create_device_state
from .storage import async_load_session, async_save_session

_DEVICE_STATE_KEY = "device_state"

DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
})

def _password_selector() -> selector.TextSelector:
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )

def _set_flow_error(errors, placeholders, key, error, *, operation, unexpected=False):
    if isinstance(error, SgccApiError):
        source = error.source
        code = error.code
        message = error.message
    elif isinstance(error, SgccNetworkError):
        source = "transport"
        code = type(error).__name__
        message = str(error)
    else:
        source = "client"
        code = type(error).__name__
        message = str(error)
    placeholders.update({
        "error_source": str(source),
        "error_code": str(code),
        "error_type": type(error).__name__,
        "error_message": str(message),
    })
    errors["base"] = key
    log = LOGGER.exception if unexpected else LOGGER.warning
    log(
        "%s failed: source=%s code=%s type=%s message=%s",
        operation, source, code, type(error).__name__, message,
    )

class SgccConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """处理集成配置逻辑."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self):
        self._api: SgccAppClient | None = None
        self._pending: dict[str, Any] = {}
        self._code_key = ""
        self._is_reauth = False
        self._reauth_entry = None
        self._reauth_error = ""

    # ---- 客户端构建 ----
    async def _build_api(self, *, username, password, device_state):
        self._pending = {
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
            _DEVICE_STATE_KEY: dict(device_state),
        }
        profile = await self._async_device_profile()
        self._api = SgccAppClient(
            async_get_clientsession(self.hass),
            username=username,
            password=password,
            profile=profile,
            profile_provider=self._async_device_profile,
        )

    async def _async_device_profile(self):
        profile, state = await self.hass.async_add_executor_job(
            build_device_profile, self._pending[_DEVICE_STATE_KEY]
        )
        self._pending[_DEVICE_STATE_KEY] = state
        return profile

    async def _send_device_verification_sms(self):
        assert self._api is not None
        self._code_key = await self._api.async_send_device_verification_sms()
        return await self.async_step_device_verification()

    async def _persist_session(self):
        assert self._api is not None
        await async_save_session(
            self.hass,
            self._pending[CONF_USERNAME],
            device_state=self._pending.get(_DEVICE_STATE_KEY) or create_device_state(),
            login_session=(
                self._api.login_session.as_dict() if self._api.login_session else None
            ),
        )

    async def _finish(self):
        assert self._api is not None and self._api.login_session is not None
        await self._persist_session()
        data = {
            CONF_USERNAME: self._pending[CONF_USERNAME],
            CONF_PASSWORD: self._pending[CONF_PASSWORD],
        }
        unique_id = self._pending[CONF_USERNAME]
        await self.async_set_unique_id(unique_id)
        if self._reauth_entry is not None:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._reauth_entry, data_updates=data
            )
        self._abort_if_unique_id_configured()
        username = str(self._pending[CONF_USERNAME])
        return self.async_create_entry(
            title=f"SGCC · {username[-4:]}",
            data=data,
            options={},
        )

    # ---- 用户步骤 (免责协议勾选) ----
    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("accept_terms") is True:
                return await self.async_step_account()
            errors["base"] = "terms_not_accepted"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("accept_terms", default=False): bool}),
            errors=errors,
        )

    # ---- 账号步骤 (默认密码登录, 风控才转短信) ----
    async def async_step_account(self, user_input=None):
        if self.source != config_entries.SOURCE_REAUTH:
            if len(self._async_current_entries()) >= ACCOUNT_LIMIT:
                return self.async_abort(reason="limit_exceeded")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                if not user_input[CONF_USERNAME].isdigit():
                    errors["base"] = "invalid_username_format"
                else:
                    username = user_input[CONF_USERNAME]
                    password = user_input[CONF_PASSWORD]
                    cached = await async_load_session(self.hass, username) or {}
                    device_state = dict(cached.get(_DEVICE_STATE_KEY) or create_device_state())
                    await self._build_api(
                        username=username, password=password, device_state=device_state
                    )
                    assert self._api is not None
                    await self._api.async_login()
            except SgccDeviceVerificationRequired:
                try:
                    return await self._send_device_verification_sms()
                except SgccNetworkError as error:
                    _set_flow_error(errors, placeholders, "cannot_connect", error,
                                    operation="send device verification SMS")
                except SgccApiError as error:
                    _set_flow_error(errors, placeholders, "cannot_send_verification_code", error,
                                    operation="send device verification SMS")
            except SgccInteractiveChallengeRequired as error:
                try:
                    return await self._send_device_verification_sms()
                except SgccNetworkError as err:
                    _set_flow_error(errors, placeholders, "cannot_connect", err,
                                    operation="send device verification SMS")
                except SgccApiError as err:
                    _set_flow_error(errors, placeholders, "cannot_send_verification_code", err,
                                    operation="send device verification SMS")
            except SgccAuthError as error:
                _set_flow_error(errors, placeholders, "invalid_auth", error,
                                operation="initial password login")
            except SgccNetworkError as error:
                _set_flow_error(errors, placeholders, "cannot_connect", error,
                                operation="initial password login")
            except SgccApiError as error:
                _set_flow_error(errors, placeholders, "server_error", error,
                                operation="initial password login")
            except ValueError as error:
                _set_flow_error(errors, placeholders, "local_error", error,
                                operation="build initial login request")
            except Exception as error:  # noqa: BLE001
                _set_flow_error(errors, placeholders, "unknown", error,
                                operation="initial password login", unexpected=True)
            else:
                return await self._finish()

        schema = DATA_SCHEMA
        if self.source == config_entries.SOURCE_REAUTH:
            default_user = (self._reauth_entry.data.get(CONF_USERNAME)
                            if self._reauth_entry else "")
            schema = vol.Schema({
                vol.Required(CONF_USERNAME, default=default_user): cv.string,
                vol.Required(CONF_PASSWORD): _password_selector(),
            })
        return self.async_show_form(step_id="account", data_schema=schema,
                                    errors=errors)

    # ---- 设备验证步骤 (风控触发后的短信验证码) ----
    async def async_step_device_verification(self, user_input=None):
        errors: dict[str, str] = {}
        placeholders = {"phone_suffix": str(self._pending.get(CONF_USERNAME, ""))[-4:]}
        if user_input is not None:
            assert self._api is not None
            try:
                await self._api.async_login(
                    verification_code=user_input["verification_code"],
                    code_key=self._code_key,
                )
            except SgccInteractiveChallengeRequired as error:
                _set_flow_error(errors, placeholders, "interactive_challenge_unsupported", error,
                                operation="password login after device verification")
            except SgccAuthError as error:
                _set_flow_error(errors, placeholders, "invalid_verification_code", error,
                                operation="password login after device verification")
            except SgccNetworkError as error:
                _set_flow_error(errors, placeholders, "cannot_connect", error,
                                operation="password login after device verification")
            except SgccApiError as error:
                _set_flow_error(errors, placeholders, "server_error", error,
                                operation="password login after device verification")
            except ValueError as error:
                _set_flow_error(errors, placeholders, "local_error", error,
                                operation="validate device verification code")
            except Exception as error:  # noqa: BLE001
                _set_flow_error(errors, placeholders, "unknown", error,
                                operation="password login after device verification",
                                unexpected=True)
            else:
                self._code_key = ""
                return await self._finish()
        return self.async_show_form(
            step_id="device_verification",
            data_schema=vol.Schema({vol.Required("verification_code"): str}),
            errors=errors,
            description_placeholders=placeholders,
        )

    # ---- 重新认证 ----
    async def async_step_reauth(self, entry_data: dict[str, Any]):
        self._reauth_entry = self._get_reauth_entry()
        data = self._reauth_entry.data
        self._reauth_error = str(data.get("auth_error") or "")
        username = str(data.get(CONF_USERNAME) or "")
        password = str(data.get(CONF_PASSWORD) or "")
        cached = await async_load_session(self.hass, username) or {}
        device_state = dict(cached.get(_DEVICE_STATE_KEY) or create_device_state())
        await self._build_api(
            username=username, password=password, device_state=device_state
        )
        self._pending[CONF_USERNAME] = username
        if not password:
            return await self.async_step_reauth_credentials()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"auth_error": self._reauth_error}
        if user_input is not None:
            assert self._api is not None
            try:
                await self._api.async_login()
            except SgccDeviceVerificationRequired:
                try:
                    return await self._send_device_verification_sms()
                except SgccNetworkError as error:
                    _set_flow_error(errors, placeholders, "cannot_connect", error,
                                    operation="send reauthentication SMS")
                except SgccApiError as error:
                    _set_flow_error(errors, placeholders, "cannot_send_verification_code", error,
                                    operation="send reauthentication SMS")
            except SgccInteractiveChallengeRequired:
                try:
                    return await self._send_device_verification_sms()
                except SgccNetworkError as err:
                    _set_flow_error(errors, placeholders, "cannot_connect", err,
                                    operation="send reauthentication SMS")
                except SgccApiError as err:
                    _set_flow_error(errors, placeholders, "cannot_send_verification_code", err,
                                    operation="send reauthentication SMS")
            except SgccAuthError as error:
                return await self.async_step_reauth_credentials(previous_error=error)
            except SgccNetworkError as error:
                _set_flow_error(errors, placeholders, "cannot_connect", error,
                                operation="automatic password reauthentication")
            except SgccApiError as error:
                _set_flow_error(errors, placeholders, "server_error", error,
                                operation="automatic password reauthentication")
            except Exception as error:  # noqa: BLE001
                _set_flow_error(errors, placeholders, "unknown", error,
                                operation="automatic password reauthentication", unexpected=True)
            else:
                return await self._finish()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("continue", default=True): bool}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth_credentials(self, user_input=None, *,
                                           previous_error=None):
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if previous_error is not None:
            _set_flow_error(errors, placeholders, "invalid_auth", previous_error,
                            operation="automatic password reauthentication")
        if user_input is not None:
            assert self._api is not None
            password = str(user_input[CONF_PASSWORD])
            self._api.password = password
            self._pending[CONF_PASSWORD] = password
            try:
                await self._api.async_login()
            except SgccDeviceVerificationRequired:
                try:
                    return await self._send_device_verification_sms()
                except SgccNetworkError as error:
                    _set_flow_error(errors, placeholders, "cannot_connect", error,
                                    operation="send device verification SMS after password update")
                except SgccApiError as error:
                    _set_flow_error(errors, placeholders, "cannot_send_verification_code", error,
                                    operation="send device verification SMS after password update")
            except SgccInteractiveChallengeRequired:
                try:
                    return await self._send_device_verification_sms()
                except SgccNetworkError as err:
                    _set_flow_error(errors, placeholders, "cannot_connect", err,
                                    operation="send device verification SMS after password update")
                except SgccApiError as err:
                    _set_flow_error(errors, placeholders, "cannot_send_verification_code", err,
                                    operation="send device verification SMS after password update")
            except SgccAuthError as error:
                _set_flow_error(errors, placeholders, "invalid_auth", error,
                                operation="password login with updated credentials")
            except SgccNetworkError as error:
                _set_flow_error(errors, placeholders, "cannot_connect", error,
                                operation="password login with updated credentials")
            except SgccApiError as error:
                _set_flow_error(errors, placeholders, "server_error", error,
                                operation="password login with updated credentials")
            except ValueError as error:
                _set_flow_error(errors, placeholders, "local_error", error,
                                operation="build login request with updated credentials")
            except Exception as error:  # noqa: BLE001
                _set_flow_error(errors, placeholders, "unknown", error,
                                operation="password login with updated credentials",
                                unexpected=True)
            else:
                return await self._finish()
        return self.async_show_form(
            step_id="reauth_credentials",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _password_selector()}),
            errors=errors,
            description_placeholders=placeholders,
        )

    # ---- 重新配置 ----
    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            try:
                if not user_input[CONF_USERNAME].isdigit():
                    errors["base"] = "invalid_username_format"
                else:
                    username = user_input[CONF_USERNAME]
                    password = user_input[CONF_PASSWORD]
                    cached = await async_load_session(self.hass, username) or {}
                    device_state = dict(cached.get(_DEVICE_STATE_KEY) or create_device_state())
                    await self._build_api(
                        username=username, password=password, device_state=device_state
                    )
                    assert self._api is not None
                    await self._api.async_login()
            except SgccDeviceVerificationRequired:
                try:
                    return await self._send_device_verification_sms()
                except SgccNetworkError as error:
                    _set_flow_error(errors, placeholders, "cannot_connect", error,
                                    operation="send device verification SMS (reconfigure)")
                except SgccApiError as error:
                    _set_flow_error(errors, placeholders, "cannot_send_verification_code", error,
                                    operation="send device verification SMS (reconfigure)")
            except SgccInteractiveChallengeRequired:
                try:
                    return await self._send_device_verification_sms()
                except SgccNetworkError as err:
                    _set_flow_error(errors, placeholders, "cannot_connect", err,
                                    operation="send device verification SMS (reconfigure)")
                except SgccApiError as err:
                    _set_flow_error(errors, placeholders, "cannot_send_verification_code", err,
                                    operation="send device verification SMS (reconfigure)")
            except SgccAuthError as error:
                _set_flow_error(errors, placeholders, "invalid_auth", error,
                                operation="reconfigure login")
            except SgccNetworkError as error:
                _set_flow_error(errors, placeholders, "cannot_connect", error,
                                operation="reconfigure login")
            except SgccApiError as error:
                _set_flow_error(errors, placeholders, "server_error", error,
                                operation="reconfigure login")
            except Exception as error:  # noqa: BLE001
                _set_flow_error(errors, placeholders, "unknown", error,
                                operation="reconfigure login", unexpected=True)
            else:
                await self._persist_session()
                return await self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )
        schema = vol.Schema({
            vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME)): str,
            vol.Required(CONF_PASSWORD): _password_selector(),
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema,
                                    errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return SgccOptionsFlowHandler()

class SgccOptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """处理集成选项更新 (保存后自动重载条目)."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options_schema = vol.Schema({
            vol.Required(CONF_ENABLE_CARD): selector.BooleanSelector(),
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