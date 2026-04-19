"""Tests for the Smart Climate entity."""
from __future__ import annotations

import datetime
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
    INSIDE_DEADBAND,
    MIN_TEMP_DIFF,
    STABLE_IN_BAND_TIMEOUT,
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

    def test_auto_at_low_plus_deadband_returns_heat(self):
        """Inside temp exactly at low + deadband → HEAT (boundary of heat zone)."""
        entity = self._entity(inside=DEFAULT_HOME_MIN + INSIDE_DEADBAND)
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_auto_just_above_low_plus_deadband_returns_off(self):
        """Inside temp just above low + deadband → OFF (entered comfort band)."""
        entity = self._entity(inside=DEFAULT_HOME_MIN + INSIDE_DEADBAND + 0.1)
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_auto_above_high_returns_cool(self):
        """Inside temp above the high setpoint → COOL."""
        entity = self._entity(inside=DEFAULT_HOME_MAX + 1)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_auto_at_high_minus_deadband_returns_cool(self):
        """Inside temp at high - deadband → COOL (engage cooling early)."""
        entity = self._entity(inside=DEFAULT_HOME_MAX - INSIDE_DEADBAND)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_auto_just_below_high_minus_deadband_returns_off(self):
        """Inside temp just below high - deadband → OFF (in comfort band)."""
        entity = self._entity(inside=DEFAULT_HOME_MAX - INSIDE_DEADBAND - 0.1)
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_auto_in_range_returns_off(self):
        """Inside temp in the comfort band → real device OFF."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid)
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_auto_in_range_cold_outside_returns_heat(self):
        """Inside temp in range, cold outside → HEAT to avoid off/heat cycling."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MIN - 5)
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_auto_in_range_warm_outside_returns_cool(self):
        """Inside temp in range, warm outside → COOL to avoid off/cool cycling."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MAX + 5)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_auto_in_range_outside_in_range_returns_off(self):
        """Inside temp in range, outside also in range → OFF."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=mid)
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_auto_in_range_no_outside_sensor_below_mid_returns_off(self):
        """No outside sensor, inside below midpoint but in band → OFF."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid - 0.1)
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_auto_in_range_no_outside_sensor_above_mid_returns_off(self):
        """No outside sensor, inside above midpoint but in band → OFF."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid + 0.1)
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_auto_no_inside_sensor_returns_auto(self):
        """No inside sensor reading → keep AUTO (don't flip the device)."""
        entity = self._entity(inside=None)
        entity._current_temperature = None
        assert entity._desired_real_mode() == HVACMode.AUTO


class TestDesiredRealModeHysteresis:
    """Tests for the band-boundary and OFF behaviour in _desired_real_mode."""

    def _entity(self, inside: float, outside: float | None = None):
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

    def test_in_range_returns_off(self):
        """When inside is within the comfort band, return OFF."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid)
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_below_low_plus_deadband_heats(self):
        """At low + deadband (HEAT boundary) → HEAT."""
        entity = self._entity(inside=DEFAULT_HOME_MIN + INSIDE_DEADBAND)
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_below_low_heats(self):
        """Below low setpoint → HEAT."""
        entity = self._entity(inside=DEFAULT_HOME_MIN - 0.5)
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_above_high_cools(self):
        """Above high setpoint → COOL."""
        entity = self._entity(inside=DEFAULT_HOME_MAX + 0.5)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_21_23_band_cools_at_22_5(self):
        """Issue regression: 21-23 band should switch to COOL at 22.5.

        high - INSIDE_DEADBAND = 23 - 0.5 = 22.5, so at 22.5 the system
        should engage cooling.
        """
        hass = _make_hass_mock(inside_temp=22.5)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = 21.0
        entity._target_temp_high = 23.0
        entity._current_temperature = 22.5
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_21_23_band_in_band_returns_off(self):
        """In 21-23 band, temperature within the band → OFF (real device off)."""
        hass = _make_hass_mock(inside_temp=22.4)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = 21.0
        entity._target_temp_high = 23.0
        entity._current_temperature = 22.4
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_21_23_band_heats_at_21_5(self):
        """In 21-23 band, temperature at low + deadband (21.5) → HEAT.

        low + INSIDE_DEADBAND = 21 + 0.5 = 21.5 is the HEAT trigger boundary.
        The real device is set to 21.5 so its own deadband causes heating to
        start at approximately 21 °C (the configured low setpoint).
        """
        hass = _make_hass_mock(inside_temp=21.5)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = 21.0
        entity._target_temp_high = 23.0
        entity._current_temperature = 21.5
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_at_high_minus_deadband_cools(self):
        """At high - deadband (COOL boundary) → COOL."""
        entity = self._entity(inside=DEFAULT_HOME_MAX - INSIDE_DEADBAND)
        assert entity._desired_real_mode() == HVACMode.COOL


# ---------------------------------------------------------------------------
# Unit tests – stable-in-band timeout (STABLE_IN_BAND_TIMEOUT)
# ---------------------------------------------------------------------------

class TestStableInBandTimeout:
    """Tests for the 15-minute stable-in-band shut-off logic."""

    def _entity(self, inside: float, outside: float | None = None):
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

    def test_first_entry_cold_outside_returns_heat(self):
        """On first entering the band with cold outside, return HEAT (timer not expired)."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MIN - 5)
        # _in_band_since is None → first entry
        assert entity._in_band_since is None
        assert entity._desired_real_mode() == HVACMode.HEAT
        # Timer should now be set
        assert entity._in_band_since is not None

    def test_first_entry_warm_outside_returns_cool(self):
        """On first entering the band with warm outside, return COOL (timer not expired)."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MAX + 5)
        assert entity._desired_real_mode() == HVACMode.COOL
        assert entity._in_band_since is not None

    def test_after_timeout_cold_outside_returns_off(self):
        """After STABLE_IN_BAND_TIMEOUT seconds in band, return OFF even with cold outside."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MIN - 5)
        # Simulate that we entered the band 15+ minutes ago
        entity._in_band_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=STABLE_IN_BAND_TIMEOUT + 1
        )
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_after_timeout_warm_outside_returns_off(self):
        """After STABLE_IN_BAND_TIMEOUT seconds in band, return OFF even with warm outside."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MAX + 5)
        entity._in_band_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=STABLE_IN_BAND_TIMEOUT + 1
        )
        assert entity._desired_real_mode() == HVACMode.OFF

    def test_just_before_timeout_still_heats(self):
        """Just before timeout, HVAC should still keep running."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MIN - 5)
        entity._in_band_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=STABLE_IN_BAND_TIMEOUT - 60
        )
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_timer_resets_when_temp_exits_band_low(self):
        """Timer is cleared when temperature drops into the heating zone."""
        entity = self._entity(inside=DEFAULT_HOME_MIN + INSIDE_DEADBAND - 0.1, outside=DEFAULT_HOME_MIN - 5)
        entity._in_band_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=STABLE_IN_BAND_TIMEOUT + 1
        )
        # Temp is in the heating zone → HEAT, timer should be reset
        result = entity._desired_real_mode()
        assert result == HVACMode.HEAT
        assert entity._in_band_since is None

    def test_timer_resets_when_temp_exits_band_high(self):
        """Timer is cleared when temperature rises into the cooling zone."""
        entity = self._entity(inside=DEFAULT_HOME_MAX - INSIDE_DEADBAND + 0.1, outside=DEFAULT_HOME_MAX + 5)
        entity._in_band_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=STABLE_IN_BAND_TIMEOUT + 1
        )
        result = entity._desired_real_mode()
        assert result == HVACMode.COOL
        assert entity._in_band_since is None

    def test_after_band_exit_and_reentry_timer_restarted(self):
        """Re-entering the band after an exit starts a fresh 15-minute timer."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MIN - 5)

        # Simulate expired timer
        entity._in_band_since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=STABLE_IN_BAND_TIMEOUT + 1
        )
        # Timed out → should return OFF
        assert entity._desired_real_mode() == HVACMode.OFF

        # Now temp exits the band (clearly in heating zone)
        entity._current_temperature = DEFAULT_HOME_MIN + INSIDE_DEADBAND - 0.1
        entity._desired_real_mode()  # resets _in_band_since
        assert entity._in_band_since is None

        # Back in band – timer is fresh
        entity._current_temperature = mid
        result = entity._desired_real_mode()
        assert result == HVACMode.HEAT  # fresh timer, should keep running
        assert entity._in_band_since is not None

    def test_no_outside_sensor_in_band_returns_off_immediately(self):
        """Without outside sensor, in-band temperature returns OFF immediately (no timer needed)."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=None)
        assert entity._desired_real_mode() == HVACMode.OFF


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
        # In AUTO mode target_temperature returns the range midpoint (never null)
        assert entity.target_temperature is not None

    def test_target_temperature_in_auto_is_midpoint(self):
        """target_temperature must equal the midpoint of the active range in AUTO mode."""
        entity = self._entity()
        entity._hvac_mode = HVACMode.AUTO
        entity._target_temp_low = 20.0
        entity._target_temp_high = 24.0
        assert entity.target_temperature == pytest.approx(22.0)

    def test_target_temperature_in_auto_updates_with_range(self):
        """Midpoint changes when the low/high setpoints are adjusted."""
        entity = self._entity()
        entity._hvac_mode = HVACMode.AUTO
        entity._target_temp_low = 18.0
        entity._target_temp_high = 22.0
        assert entity.target_temperature == pytest.approx(20.0)
        entity._target_temp_low = 21.0
        entity._target_temp_high = 25.0
        assert entity.target_temperature == pytest.approx(23.0)

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

    def test_real_setpoint_divergence_does_not_clear_preset(self):
        """Master/slave: real-device state events never mutate preset_mode.

        Smart climate is the sole writer of the real device's setpoint, so any
        divergence reported back (in any hvac state, by any magnitude) must be
        ignored by _on_real_climate_update.  This covers the whole class of
        feedback-loop bugs where smart's own OFF / HEAT / COOL writes were
        misread as external user overrides and wrongly dropped the preset.
        """
        for real_state, real_action, setpoint_offset in [
            (HVACMode.OFF.value, HVACAction.OFF.value, 5.0),
            (HVACMode.HEAT.value, HVACAction.HEATING.value, 10.0),
            (HVACMode.COOL.value, HVACAction.COOLING.value, -10.0),
        ]:
            entity, hass = self._entity_with_sensors()
            state = MagicMock()
            state.state = real_state
            state.attributes = {
                "hvac_action": real_action,
                "temperature": 22.0 + setpoint_offset,
            }
            hass.states.get = lambda eid, s=state: s if eid == REAL_CLIMATE_ID else MagicMock()
            entity._on_real_climate_update()
            assert entity._preset_mode == PRESET_HOME, (
                f"preset wrongly cleared for real_state={real_state}"
            )


# ---------------------------------------------------------------------------
# Unit tests – mode-appropriate setpoints sent to the real device
# ---------------------------------------------------------------------------

class TestSyncedSetpoints:
    """Tests verifying _async_sync_real_climate sends the right setpoint."""

    def _entity(self, inside: float = 22.0, outside: float | None = None):
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

    @pytest.mark.asyncio
    async def test_sync_sends_low_plus_deadband_when_heating(self):
        """_async_sync_real_climate sends low + INSIDE_DEADBAND when in HEAT.

        This compensates for the real device's own internal deadband so it
        starts heating at approximately the configured low setpoint.
        """
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=None,
            inside_temp=DEFAULT_HOME_MIN - 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = DEFAULT_HOME_MIN - 1
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_called_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        assert call_args[0][1] == "set_temperature"
        assert call_args[0][2]["temperature"] == DEFAULT_HOME_MIN + INSIDE_DEADBAND

    @pytest.mark.asyncio
    async def test_sync_sends_off_when_in_band(self):
        """_async_sync_real_climate turns real device OFF when temp is in the band."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=None,
            inside_temp=mid,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = mid
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_called_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        assert call_args[0][1] == "set_hvac_mode"
        assert call_args[0][2]["hvac_mode"] == HVACMode.OFF.value

    @pytest.mark.asyncio
    async def test_sync_no_command_when_already_off_in_band(self):
        """_async_sync_real_climate skips the service call when device is already OFF."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        hass = _make_hass_mock(
            real_climate_state=HVACMode.OFF.value,
            real_climate_temp=None,
            inside_temp=mid,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = mid
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_sends_high_minus_one_when_cooling(self):
        """_async_sync_real_climate sends high-1 when in COOL to avoid integer overshoot."""
        hass = _make_hass_mock(
            real_climate_state=HVACMode.COOL.value,
            real_climate_temp=None,
            inside_temp=DEFAULT_HOME_MAX + 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = DEFAULT_HOME_MAX + 1
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_called_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        assert call_args[0][1] == "set_temperature"
        assert call_args[0][2]["temperature"] == DEFAULT_HOME_MAX - 1

    @pytest.mark.asyncio
    async def test_sync_cool_target_21_23_band(self):
        """Issue regression: 21-23 band → cooling target must be 22, not 23.

        When the band is 21-23 and the real device only accepts integers, using
        23 as the cooling target allows the device's own ±0.5 °C hysteresis to
        reach 23.5, which rounds to 24.  Using 22 (high - 1) keeps the
        effective upper temperature within the configured band.
        """
        low, high = 21.0, 23.0
        hass = _make_hass_mock(
            real_climate_state=HVACMode.COOL.value,
            real_climate_temp=None,
            inside_temp=high + 0.1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = low
        entity._target_temp_high = high
        entity._current_temperature = high + 0.1
        await entity._async_sync_real_climate()
        call_args = hass.services.async_call.call_args
        assert call_args[0][2]["temperature"] == high - 1  # 22, not 23

    @pytest.mark.asyncio
    async def test_sync_cool_target_capped_at_low_for_narrow_band(self):
        """Narrow band: cooling target sent to real device must not drop below low."""
        hass = _make_hass_mock(
            real_climate_state=HVACMode.COOL.value,
            real_climate_temp=None,
            inside_temp=23.0,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = 22.0
        entity._target_temp_high = 22.5  # only 0.5 °C wide
        entity._current_temperature = 23.0  # above high → COOL
        await entity._async_sync_real_climate()
        call_args = hass.services.async_call.call_args
        # high - 1 = 21.5 < low = 22.0, so target must be capped at low
        assert call_args[0][2]["temperature"] == 22.0


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


# ---------------------------------------------------------------------------
# Unit tests – state restoration on startup
# ---------------------------------------------------------------------------

class TestStateRestoration:
    """Tests for async_added_to_hass state restoration and real device sync."""

    def _make_last_state(
        self,
        hvac_mode: str = HVACMode.AUTO.value,
        preset_mode: str = PRESET_SLEEP,
        target_temp_low: float = DEFAULT_SLEEP_MIN,
        target_temp_high: float = DEFAULT_SLEEP_MAX,
        temperature: float | None = None,
    ) -> MagicMock:
        """Build a mock last_state object mimicking HA's RestoreEntity."""
        state = MagicMock()
        state.state = hvac_mode
        attrs = {
            "preset_mode": preset_mode,
            "target_temp_low": target_temp_low,
            "target_temp_high": target_temp_high,
        }
        if temperature is not None:
            attrs["temperature"] = temperature
        state.attributes = attrs
        return state

    async def _setup_entity(
        self,
        last_state: MagicMock | None,
        inside_temp: float = 20.0,
        real_climate_state: str = HVACMode.OFF.value,
        real_climate_temp: float | None = None,
    ) -> tuple[SmartClimateEntity, MagicMock]:
        """Create an entity and run async_added_to_hass with the given last_state."""
        hass = _make_hass_mock(
            real_climate_state=real_climate_state,
            real_climate_temp=real_climate_temp,
            inside_temp=inside_temp,
        )
        entity = _make_entity(hass)

        # Mock the RestoreEntity and lifecycle methods
        with patch.object(
            SmartClimateEntity, "async_get_last_state", return_value=last_state
        ), patch.object(
            SmartClimateEntity, "async_on_remove"
        ), patch(
            "custom_components.smart_climate.climate.async_track_state_change_event"
        ), patch.object(
            SmartClimateEntity, "async_write_ha_state"
        ):
            await entity.async_added_to_hass()

        return entity, hass

    @pytest.mark.asyncio
    async def test_preset_restored_from_last_state(self):
        """Preset (profile) should be restored when HASS restarts."""
        last_state = self._make_last_state(preset_mode=PRESET_SLEEP)
        entity, _ = await self._setup_entity(last_state)
        assert entity._preset_mode == PRESET_SLEEP

    @pytest.mark.asyncio
    async def test_away_preset_restored(self):
        """Away preset should be restored correctly."""
        last_state = self._make_last_state(
            preset_mode=PRESET_AWAY,
            target_temp_low=DEFAULT_AWAY_MIN,
            target_temp_high=DEFAULT_AWAY_MAX,
        )
        entity, _ = await self._setup_entity(last_state)
        assert entity._preset_mode == PRESET_AWAY
        assert entity._target_temp_low == DEFAULT_AWAY_MIN
        assert entity._target_temp_high == DEFAULT_AWAY_MAX

    @pytest.mark.asyncio
    async def test_hvac_mode_restored_from_last_state(self):
        """HVAC mode should be restored from the last saved state."""
        last_state = self._make_last_state(hvac_mode=HVACMode.AUTO.value)
        entity, _ = await self._setup_entity(last_state)
        assert entity._hvac_mode == HVACMode.AUTO

    @pytest.mark.asyncio
    async def test_temperatures_restored_from_last_state(self):
        """Temperature setpoints should be restored from the last saved state."""
        last_state = self._make_last_state(
            target_temp_low=19.5, target_temp_high=23.5, temperature=21.5
        )
        entity, _ = await self._setup_entity(last_state)
        assert entity._target_temp_low == 19.5
        assert entity._target_temp_high == 23.5
        assert entity._target_temperature == 21.5

    @pytest.mark.asyncio
    async def test_real_device_synced_on_restore_auto(self):
        """Real climate device should receive heat/cool on startup when restored to AUTO."""
        last_state = self._make_last_state(
            hvac_mode=HVACMode.AUTO.value,
            preset_mode=PRESET_SLEEP,
        )
        entity, hass = await self._setup_entity(
            last_state,
            inside_temp=DEFAULT_SLEEP_MIN - 1,  # Below low → should HEAT
        )
        # The real device should have been told to heat
        hass.services.async_call.assert_called()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        assert call_args[0][1] == "set_temperature"
        assert call_args[0][2]["hvac_mode"] == HVACMode.HEAT.value

    @pytest.mark.asyncio
    async def test_real_device_synced_on_restore_heat(self):
        """Real climate device should receive HEAT on startup when restored to HEAT mode."""
        last_state = self._make_last_state(
            hvac_mode=HVACMode.HEAT.value,
            preset_mode=PRESET_HOME,
            temperature=22.0,
        )
        entity, hass = await self._setup_entity(
            last_state,
            inside_temp=20.0,
            real_climate_temp=None,
        )
        hass.services.async_call.assert_called()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        assert call_args[0][1] == "set_temperature"
        assert call_args[0][2]["hvac_mode"] == HVACMode.HEAT.value

    @pytest.mark.asyncio
    async def test_real_device_not_synced_when_off(self):
        """Real device should NOT be sent commands when restored state is OFF."""
        last_state = self._make_last_state(hvac_mode=HVACMode.OFF.value)
        entity, hass = await self._setup_entity(last_state)
        assert entity._hvac_mode == HVACMode.OFF
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_last_state_defaults_to_off(self):
        """No saved state should leave entity at default OFF with no real device sync."""
        entity, hass = await self._setup_entity(last_state=None)
        assert entity._hvac_mode == HVACMode.OFF
        assert entity._preset_mode == PRESET_HOME
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_preset_defaults_to_home(self):
        """Invalid preset in last state should fall back to PRESET_HOME."""
        last_state = self._make_last_state(preset_mode="invalid_preset")
        entity, _ = await self._setup_entity(last_state)
        assert entity._preset_mode == PRESET_HOME

    @pytest.mark.asyncio
    async def test_preset_survives_mismatched_real_device_temp(self):
        """Preset is restored verbatim regardless of real device's reported setpoint.

        Smart climate owns preset_mode; real-device setpoint never feeds back
        into it.  This test pins that contract end-to-end through startup:
        even if the wrapped device reports a wildly divergent temperature
        (e.g. stale value from before the restart), the restored preset is
        unaffected.
        """
        last_state = self._make_last_state(
            hvac_mode=HVACMode.AUTO.value,
            preset_mode=PRESET_SLEEP,
            target_temp_low=DEFAULT_SLEEP_MIN,
            target_temp_high=DEFAULT_SLEEP_MAX,
        )
        entity, hass = await self._setup_entity(
            last_state,
            inside_temp=20.0,
            real_climate_state=HVACMode.COOL.value,
            real_climate_temp=25.0,  # wildly divergent setpoint
        )
        assert entity._preset_mode == PRESET_SLEEP
