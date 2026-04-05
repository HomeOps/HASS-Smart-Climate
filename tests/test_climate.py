"""Tests for the Smart Climate entity."""
from __future__ import annotations

import math
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from homeassistant.components.climate import HVACMode, HVACAction
from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
)
from homeassistant.core import HomeAssistant

from custom_components.smart_climate.climate import SmartClimateEntity, SUPPORTED_PRESETS
from custom_components.smart_climate.const import (
    CONF_REAL_CLIMATE,
    CONF_INSIDE_SENSOR,
    CONF_OUTSIDE_SENSOR,
    DEFAULT_HOME_MIN,
    DEFAULT_HOME_MAX,
    DEFAULT_SLEEP_MIN,
    DEFAULT_SLEEP_MAX,
    DEFAULT_AWAY_MIN,
    DEFAULT_AWAY_MAX,
    MIN_TEMP_DIFF,
)

REAL_CLIMATE_ID = "climate.real_ac"
INSIDE_SENSOR_ID = "sensor.inside_temp"
OUTSIDE_SENSOR_ID = "sensor.outside_temp"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entity(hass_mock, config=None) -> SmartClimateEntity:
    """Create a SmartClimateEntity with a mocked hass instance."""
    if config is None:
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
    entity = SmartClimateEntity(hass_mock, "test_entry_id", "Smart Thermostat", config)
    return entity


def _make_hass_mock(
    real_climate_state: str = HVACMode.OFF.value,
    real_climate_action: str | None = None,
    real_climate_temp: float | None = 22.0,
    inside_temp: float | None = 22.0,
    outside_temp: float | None = None,
) -> MagicMock:
    """Build a minimal hass mock with the given state values."""
    hass = MagicMock()
    hass.config.units.temperature_unit = "°C"

    def _state_getter(entity_id: str):
        if entity_id == REAL_CLIMATE_ID:
            state = MagicMock()
            state.state = real_climate_state
            state.attributes = {
                "hvac_action": real_climate_action,
                "temperature": real_climate_temp,
            }
            return state
        if entity_id == INSIDE_SENSOR_ID:
            if inside_temp is None:
                return None
            state = MagicMock()
            state.state = str(inside_temp)
            return state
        if entity_id == OUTSIDE_SENSOR_ID:
            if outside_temp is None:
                return None
            state = MagicMock()
            state.state = str(outside_temp)
            return state
        return None

    hass.states.get = _state_getter
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock()
    return hass


# ---------------------------------------------------------------------------
# Unit tests – _desired_real_mode
# ---------------------------------------------------------------------------

class TestDesiredRealMode:
    """Tests for the internal mode-selection logic."""

    def _entity(self, inside: float | None, outside: float | None = None):
        hass = _make_hass_mock(inside_temp=inside, outside_temp=outside)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        if outside is not None:
            config[CONF_OUTSIDE_SENSOR] = OUTSIDE_SENSOR_ID
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = inside
        entity._outside_temperature = outside
        return entity

    def test_off_mode_returns_off(self):
        entity = self._entity(22.0)
        entity._hvac_mode = HVACMode.OFF
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_heat_mode_passes_through(self):
        entity = self._entity(22.0)
        entity._hvac_mode = HVACMode.HEAT
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_cool_mode_passes_through(self):
        entity = self._entity(22.0)
        entity._hvac_mode = HVACMode.COOL
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_auto_below_low_returns_heat(self):
        """Inside temp below the low setpoint → HEAT."""
        entity = self._entity(inside=DEFAULT_HOME_MIN - 1)
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_auto_above_high_returns_cool(self):
        """Inside temp above the high setpoint → COOL."""
        entity = self._entity(inside=DEFAULT_HOME_MAX + 1)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_auto_in_range_cold_outside_returns_heat(self):
        """Inside in range, cold outside → HEAT (ready to reheat if needed)."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=mid - 5)
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_auto_in_range_warm_outside_returns_cool(self):
        """Inside in range, warm outside → COOL."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=mid + 5)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_auto_in_range_no_outside_sensor_below_mid_returns_heat(self):
        """No outside sensor, inside below midpoint → HEAT."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid - 0.1)
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_auto_in_range_no_outside_sensor_above_mid_returns_cool(self):
        """No outside sensor, inside above midpoint → COOL."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid + 0.1)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_auto_no_inside_sensor_returns_auto(self):
        """No inside sensor reading → keep AUTO (don't flip the device)."""
        entity = self._entity(inside=None)
        entity._current_temperature = None
        assert entity._desired_real_mode() == HVACMode.AUTO


# ---------------------------------------------------------------------------
# Unit tests – preset range helpers
# ---------------------------------------------------------------------------

class TestPresetRanges:
    """Tests for preset temperature range helpers."""

    def _entity(self):
        hass = _make_hass_mock()
        return _make_entity(hass)

    def test_active_range_home(self):
        entity = self._entity()
        entity._preset_mode = PRESET_HOME
        assert entity._active_range() == (DEFAULT_HOME_MIN, DEFAULT_HOME_MAX)

    def test_active_range_sleep(self):
        entity = self._entity()
        entity._preset_mode = PRESET_SLEEP
        assert entity._active_range() == (DEFAULT_SLEEP_MIN, DEFAULT_SLEEP_MAX)

    def test_active_range_away(self):
        entity = self._entity()
        entity._preset_mode = PRESET_AWAY
        assert entity._active_range() == (DEFAULT_AWAY_MIN, DEFAULT_AWAY_MAX)

    def test_active_range_manual_uses_stored_values(self):
        entity = self._entity()
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = 20.0
        entity._target_temp_high = 25.0
        assert entity._active_range() == (20.0, 25.0)

    def test_preset_midpoint(self):
        entity = self._entity()
        entity._preset_mode = PRESET_HOME
        expected_mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2.0
        assert entity._preset_midpoint() == pytest.approx(expected_mid)


# ---------------------------------------------------------------------------
# Unit tests – ClimateEntity properties
# ---------------------------------------------------------------------------

class TestClimateProperties:
    """Tests for the ClimateEntity property accessors."""

    def _entity(self):
        hass = _make_hass_mock()
        return _make_entity(hass)

    def test_supported_presets(self):
        entity = self._entity()
        assert PRESET_HOME in entity.preset_modes
        assert PRESET_SLEEP in entity.preset_modes
        assert PRESET_AWAY in entity.preset_modes
        assert PRESET_NONE in entity.preset_modes

    def test_supported_hvac_modes(self):
        entity = self._entity()
        assert HVACMode.OFF in entity.hvac_modes
        assert HVACMode.AUTO in entity.hvac_modes
        assert HVACMode.HEAT in entity.hvac_modes
        assert HVACMode.COOL in entity.hvac_modes

    def test_target_temperature_low_high_only_in_auto(self):
        entity = self._entity()
        entity._hvac_mode = HVACMode.AUTO
        assert entity.target_temperature_low is not None
        assert entity.target_temperature_high is not None
        assert entity.target_temperature is None

    def test_single_target_temperature_in_heat_mode(self):
        entity = self._entity()
        entity._hvac_mode = HVACMode.HEAT
        assert entity.target_temperature is not None
        assert entity.target_temperature_low is None
        assert entity.target_temperature_high is None

    def test_current_temperature_from_sensor(self):
        entity = self._entity()
        entity._current_temperature = 22.5
        assert entity.current_temperature == 22.5


# ---------------------------------------------------------------------------
# Unit tests – temperature setpoint enforcement
# ---------------------------------------------------------------------------

class TestSetTemperatureValidation:
    """Tests for MIN_TEMP_DIFF enforcement when setting range targets."""

    def _entity(self):
        hass = _make_hass_mock()
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = 20.0
        entity._target_temp_high = 25.0
        return entity

    @pytest.mark.asyncio
    async def test_low_adjusted_when_too_close_to_high(self):
        entity = self._entity()
        entity.async_write_ha_state = MagicMock()
        await entity.async_set_temperature(target_temp_low=25.0, target_temp_high=25.0)
        diff = entity._target_temp_high - entity._target_temp_low
        assert diff >= MIN_TEMP_DIFF

    @pytest.mark.asyncio
    async def test_high_adjusted_when_too_close_to_low(self):
        entity = self._entity()
        entity.async_write_ha_state = MagicMock()
        await entity.async_set_temperature(target_temp_low=25.0, target_temp_high=25.0)
        diff = entity._target_temp_high - entity._target_temp_low
        assert diff >= MIN_TEMP_DIFF


# ---------------------------------------------------------------------------
# Unit tests – state change callbacks
# ---------------------------------------------------------------------------

class TestStateCallbacks:
    """Tests for the _on_*_update callbacks."""

    def _entity_with_sensors(self):
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_action=HVACAction.HEATING.value,
            inside_temp=22.0,
            outside_temp=10.0,
        )
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
            CONF_OUTSIDE_SENSOR: OUTSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = 22.0
        entity._outside_temperature = 10.0
        entity.async_write_ha_state = MagicMock()
        return entity, hass

    def test_inside_sensor_update_sets_current_temperature(self):
        entity, hass = self._entity_with_sensors()
        # Mock state to return new temperature
        new_state = MagicMock()
        new_state.state = "23.5"
        hass.states.get = lambda eid: new_state if eid == INSIDE_SENSOR_ID else MagicMock()
        entity._on_inside_sensor_update()
        assert entity._current_temperature == pytest.approx(23.5)

    def test_outside_sensor_update_stores_temperature(self):
        entity, hass = self._entity_with_sensors()
        new_state = MagicMock()
        new_state.state = "5.0"
        orig_get = hass.states.get

        def patched_get(eid):
            if eid == OUTSIDE_SENSOR_ID:
                return new_state
            return orig_get(eid)

        hass.states.get = patched_get
        entity._on_outside_sensor_update()
        assert entity._outside_temperature == pytest.approx(5.0)

    def test_real_climate_action_synced(self):
        entity, hass = self._entity_with_sensors()
        state = MagicMock()
        state.state = HVACMode.HEAT.value
        state.attributes = {"hvac_action": HVACAction.HEATING.value, "temperature": 22.5}
        hass.states.get = lambda eid: state if eid == REAL_CLIMATE_ID else MagicMock()
        entity._on_real_climate_update()
        assert entity._hvac_action == HVACAction.HEATING

    def test_external_real_temperature_change_exits_preset(self):
        """When real device setpoint drifts far from preset midpoint, go manual."""
        entity, hass = self._entity_with_sensors()
        midpoint = entity._preset_midpoint()
        state = MagicMock()
        state.state = HVACMode.COOL.value
        state.attributes = {
            "hvac_action": HVACAction.COOLING.value,
            "temperature": midpoint + 3.0,  # More than 0.5 away from expected
        }
        hass.states.get = lambda eid: state if eid == REAL_CLIMATE_ID else MagicMock()
        entity._on_real_climate_update()
        assert entity._preset_mode == PRESET_NONE


# ---------------------------------------------------------------------------
# Unit tests – supported presets list
# ---------------------------------------------------------------------------

def test_supported_presets_list():
    """Verify SUPPORTED_PRESETS contains the four expected values."""
    assert PRESET_HOME in SUPPORTED_PRESETS
    assert PRESET_SLEEP in SUPPORTED_PRESETS
    assert PRESET_AWAY in SUPPORTED_PRESETS
    assert PRESET_NONE in SUPPORTED_PRESETS
    assert len(SUPPORTED_PRESETS) == 4
