"""Config flow for Smart Climate integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AWAY_MAX,
    CONF_AWAY_MIN,
    CONF_HOME_MAX,
    CONF_HOME_MIN,
    CONF_INSIDE_SENSOR,
    CONF_INSIDE_SENSOR_AWAY,
    CONF_INSIDE_SENSOR_SLEEP,
    CONF_OUTSIDE_SENSOR,
    CONF_REAL_CLIMATE,
    CONF_SLEEP_MAX,
    CONF_SLEEP_MIN,
    DEFAULT_AWAY_MAX,
    DEFAULT_AWAY_MIN,
    DEFAULT_HOME_MAX,
    DEFAULT_HOME_MIN,
    DEFAULT_SLEEP_MAX,
    DEFAULT_SLEEP_MIN,
    DOMAIN,
    MAX_TEMP,
    MIN_TEMP,
    TEMP_STEP,
)

_TEMP_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_TEMP,
        max=MAX_TEMP,
        step=TEMP_STEP,
        unit_of_measurement="°C",
        mode=NumberSelectorMode.BOX,
    )
)


class SmartClimateConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow for Smart Climate."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the first (and only) user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(
                title=user_input["name"],
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required("name", default="Smart Climate"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(CONF_REAL_CLIMATE): EntitySelector(
                    EntitySelectorConfig(domain=CLIMATE_DOMAIN)
                ),
                vol.Required(CONF_INSIDE_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain=SENSOR_DOMAIN)
                ),
                vol.Optional(CONF_INSIDE_SENSOR_SLEEP): EntitySelector(
                    EntitySelectorConfig(domain=SENSOR_DOMAIN)
                ),
                vol.Optional(CONF_INSIDE_SENSOR_AWAY): EntitySelector(
                    EntitySelectorConfig(domain=SENSOR_DOMAIN)
                ),
                vol.Optional(CONF_OUTSIDE_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain=SENSOR_DOMAIN)
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SmartClimateOptionsFlow:
        """Return the options flow handler."""
        return SmartClimateOptionsFlow(config_entry)


class SmartClimateOptionsFlow(OptionsFlow):
    """Handle options (preset temperatures) for Smart Climate."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage sensors and preset temperature options for an existing entry.

        Includes the per-preset inside sensors so existing installs can wire
        up Sleep / Away zones without deleting and re-adding the integration.
        Pre-populates every field from the union of ``entry.data`` (initial
        setup values) and ``entry.options`` (previous edits via this flow)
        so the form always shows the currently effective configuration.
        """
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self._config_entry.data, **self._config_entry.options}

        # Build optional EntitySelector entries with suggested_value rather
        # than `default=...` so clearing a field in the form removes the
        # key from user_input (default= would re-fill the previous value
        # and make optional sensors un-clearable).
        def _optional_sensor(key: str) -> dict:
            value = current.get(key)
            description = {"suggested_value": value} if value else {}
            return {
                vol.Optional(key, description=description): EntitySelector(
                    EntitySelectorConfig(domain=SENSOR_DOMAIN)
                )
            }

        sensor_schema: dict = {
            vol.Required(
                CONF_INSIDE_SENSOR,
                default=current.get(CONF_INSIDE_SENSOR),
            ): EntitySelector(EntitySelectorConfig(domain=SENSOR_DOMAIN)),
        }
        sensor_schema.update(_optional_sensor(CONF_INSIDE_SENSOR_SLEEP))
        sensor_schema.update(_optional_sensor(CONF_INSIDE_SENSOR_AWAY))
        sensor_schema.update(_optional_sensor(CONF_OUTSIDE_SENSOR))

        preset_schema = {
            vol.Required(
                CONF_HOME_MIN,
                default=current.get(CONF_HOME_MIN, DEFAULT_HOME_MIN),
            ): _TEMP_SELECTOR,
            vol.Required(
                CONF_HOME_MAX,
                default=current.get(CONF_HOME_MAX, DEFAULT_HOME_MAX),
            ): _TEMP_SELECTOR,
            vol.Required(
                CONF_SLEEP_MIN,
                default=current.get(CONF_SLEEP_MIN, DEFAULT_SLEEP_MIN),
            ): _TEMP_SELECTOR,
            vol.Required(
                CONF_SLEEP_MAX,
                default=current.get(CONF_SLEEP_MAX, DEFAULT_SLEEP_MAX),
            ): _TEMP_SELECTOR,
            vol.Required(
                CONF_AWAY_MIN,
                default=current.get(CONF_AWAY_MIN, DEFAULT_AWAY_MIN),
            ): _TEMP_SELECTOR,
            vol.Required(
                CONF_AWAY_MAX,
                default=current.get(CONF_AWAY_MAX, DEFAULT_AWAY_MAX),
            ): _TEMP_SELECTOR,
        }

        schema = vol.Schema({**sensor_schema, **preset_schema})

        return self.async_show_form(step_id="init", data_schema=schema)
