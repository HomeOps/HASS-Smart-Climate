"""Pytest configuration and shared fixtures for Smart Climate tests."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from homeassistant.components.climate import HVACMode, HVACAction
from homeassistant.components.climate.const import PRESET_HOME, PRESET_SLEEP, PRESET_AWAY, PRESET_NONE
from homeassistant.core import HomeAssistant

from custom_components.smart_climate.const import (
    CONF_REAL_CLIMATE,
    CONF_INSIDE_SENSOR,
    CONF_OUTSIDE_SENSOR,
    CONF_HOME_MIN,
    CONF_HOME_MAX,
    CONF_SLEEP_MIN,
    CONF_SLEEP_MAX,
    CONF_AWAY_MIN,
    CONF_AWAY_MAX,
    DEFAULT_HOME_MIN,
    DEFAULT_HOME_MAX,
    DEFAULT_SLEEP_MIN,
    DEFAULT_SLEEP_MAX,
    DEFAULT_AWAY_MIN,
    DEFAULT_AWAY_MAX,
)


REAL_CLIMATE_ENTITY = "climate.real_ac"
INSIDE_SENSOR_ENTITY = "sensor.inside_temp"
OUTSIDE_SENSOR_ENTITY = "sensor.outside_temp"


@pytest.fixture
def base_config():
    """Return a minimal valid configuration dict."""
    return {
        "name": "Smart Thermostat",
        CONF_REAL_CLIMATE: REAL_CLIMATE_ENTITY,
        CONF_INSIDE_SENSOR: INSIDE_SENSOR_ENTITY,
        CONF_HOME_MIN: DEFAULT_HOME_MIN,
        CONF_HOME_MAX: DEFAULT_HOME_MAX,
        CONF_SLEEP_MIN: DEFAULT_SLEEP_MIN,
        CONF_SLEEP_MAX: DEFAULT_SLEEP_MAX,
        CONF_AWAY_MIN: DEFAULT_AWAY_MIN,
        CONF_AWAY_MAX: DEFAULT_AWAY_MAX,
    }


@pytest.fixture
def config_with_outside(base_config):
    """Return a configuration that includes an outside temperature sensor."""
    return {**base_config, CONF_OUTSIDE_SENSOR: OUTSIDE_SENSOR_ENTITY}
