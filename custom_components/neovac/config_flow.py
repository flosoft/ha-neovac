"""Config flow for the NeoVac MyEnergy integration.

Flow steps:
1. User enters email and password
2. Integration authenticates and fetches available usage units
3. User selects which usage unit (metering point) to monitor
4. Config entry is created

Options flow allows changing the polling interval.
Reauth flow handles expired credentials.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .api import (
    NeoVacApiClient,
    NeoVacAuthError,
    NeoVacConnectionError,
)
from .const import (
    CONF_DEBUG_LOGGING,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USAGE_UNIT_ID,
    CONF_USAGE_UNIT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class NeoVacConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NeoVac MyEnergy."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._email: str | None = None
        self._password: str | None = None
        self._usage_units: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step - credentials input."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]

            # Validate credentials
            client = NeoVacApiClient(self._email, self._password)
            try:
                await client.authenticate()
                # Fetch usage units
                self._usage_units = await client.get_usage_units()
                _LOGGER.debug(
                    "Found %d usage units", len(self._usage_units)
                )
            except NeoVacAuthError:
                errors["base"] = "invalid_auth"
            except NeoVacConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during authentication")
                errors["base"] = "unknown"
            finally:
                await client.close()

            if not errors:
                if not self._usage_units:
                    errors["base"] = "no_usage_units"
                elif len(self._usage_units) == 1:
                    # Only one unit, skip selection step
                    unit = self._usage_units[0]
                    return await self._create_entry(unit)
                else:
                    # Multiple units, show selection
                    return await self.async_step_select_unit()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_select_unit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle usage unit selection step."""
        if user_input is not None:
            unit_id = user_input[CONF_USAGE_UNIT_ID]
            # Find the selected unit
            for unit in self._usage_units:
                uid = str(
                    unit.get("usageUnitId")
                    or unit.get("id")
                    or unit.get("unitId")
                )
                if uid == unit_id:
                    return await self._create_entry(unit)

            # Fallback: create with just the ID
            return await self._create_entry(
                {"usageUnitId": unit_id, "customName": unit_id}
            )

        # Build selection options
        unit_options = {}
        for unit in self._usage_units:
            uid = str(
                unit.get("usageUnitId")
                or unit.get("id")
                or unit.get("unitId")
            )
            name = (
                unit.get("customName")
                or unit.get("name")
                or uid
            )
            unit_options[uid] = name

        return self.async_show_form(
            step_id="select_unit",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USAGE_UNIT_ID): vol.In(unit_options),
                }
            ),
        )

    async def _create_entry(
        self, unit: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create a config entry for the selected usage unit."""
        unit_id = str(
            unit.get("usageUnitId")
            or unit.get("id")
            or unit.get("unitId")
        )
        unit_name = (
            unit.get("customName")
            or unit.get("name")
            or unit_id
        )

        # Set unique ID to prevent duplicates
        await self.async_set_unique_id(
            f"{self._email.lower()}_{unit_id}"
        )
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"NeoVac {unit_name}",
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_USAGE_UNIT_ID: unit_id,
                CONF_USAGE_UNIT_NAME: unit_name,
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauthentication confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            client = NeoVacApiClient(email, password)
            try:
                await client.authenticate()
            except NeoVacAuthError:
                errors["base"] = "invalid_auth"
            except NeoVacConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            finally:
                await client.close()

            if not errors:
                entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                    },
                )

        entry = self._get_reauth_entry()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL, default=entry.data.get(CONF_EMAIL)
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> NeoVacOptionsFlow:
        """Return the options flow handler."""
        return NeoVacOptionsFlow()


class NeoVacOptionsFlow(OptionsFlow):
    """Handle options flow for NeoVac MyEnergy."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
            ),
        )

        current_debug_logging = self.config_entry.options.get(
            CONF_DEBUG_LOGGING, False
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=current_interval,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL
                        ),
                    ),
                    vol.Optional(
                        CONF_DEBUG_LOGGING,
                        default=current_debug_logging,
                    ): bool,
                }
            ),
        )
