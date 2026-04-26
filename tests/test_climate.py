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
    FLIP_DWELL,
    FLIP_MARGIN,
    MIN_TEMP_DIFF,
)


def _fake_clock(start: datetime.datetime | None = None):
    """Return (now_callable, advance_callable) over a mutable fake clock.

    Use as ``entity._now = now`` to drive the AUTO flip-dwell timer in
    deterministic test steps, advancing time with ``advance(seconds)``.
    """
    state = {"t": start or datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)}

    def now() -> datetime.datetime:
        return state["t"]

    def advance(seconds: float) -> None:
        state["t"] = state["t"] + datetime.timedelta(seconds=seconds)

    return now, advance

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
# Unit tests – _desired_real_mode (sticky AUTO mode)
# ---------------------------------------------------------------------------

class TestDesiredRealMode:
    """Pass-through and initial-pick behaviour of _desired_real_mode."""

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

    def test_auto_no_inside_sensor_no_prior_choice_returns_auto(self):
        """Without a sensor reading and no committed mode yet, return AUTO
        so the real device is left untouched until we actually know."""
        entity = self._entity(inside=None)
        entity._current_temperature = None
        assert entity._auto_mode is None
        assert entity._desired_real_mode() == HVACMode.AUTO

    def test_auto_no_inside_sensor_keeps_committed_mode(self):
        """If a mode was already chosen, a transient sensor outage must not
        drop the commitment back to bare AUTO."""
        entity = self._entity(inside=None)
        entity._current_temperature = None
        entity._auto_mode = HVACMode.COOL
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_initial_pick_uses_outside_when_cold(self):
        """Outside sensor decides the first commitment: cold → HEAT.

        v3: in-band current returns OFF as the unit command, but the
        commitment side-effect on _auto_mode still fires.
        """
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MIN - 5)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.HEAT

    def test_initial_pick_uses_outside_when_warm(self):
        """Outside sensor decides the first commitment: warm → COOL."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid, outside=DEFAULT_HOME_MAX + 5)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL

    def test_initial_pick_falls_back_to_inside_when_no_outside(self):
        """No outside sensor: inside-vs-midpoint chooses initial mode."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        cold = self._entity(inside=mid - 1.0)
        cold._desired_real_mode()
        assert cold._auto_mode == HVACMode.HEAT
        warm = self._entity(inside=mid + 1.0)
        warm._desired_real_mode()
        assert warm._auto_mode == HVACMode.COOL

    def test_initial_pick_at_midpoint_breaks_to_cool(self):
        """Tie at exactly the midpoint with no outside sensor → COOL."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL

    def test_auto_below_low_picks_heat(self):
        """Way below the band — initial pick is HEAT and unit runs HEAT."""
        entity = self._entity(inside=DEFAULT_HOME_MIN - 1)
        assert entity._desired_real_mode() == HVACMode.HEAT
        assert entity._auto_mode == HVACMode.HEAT

    def test_auto_above_high_picks_cool(self):
        """Way above the band — initial pick is COOL and unit runs COOL."""
        entity = self._entity(inside=DEFAULT_HOME_MAX + 1)
        assert entity._desired_real_mode() == HVACMode.COOL
        assert entity._auto_mode == HVACMode.COOL

    def test_auto_cool_returns_off_when_inside_band(self):
        """v3 contract — narrow surgical change: only AUTO + COOL committed
        + in-band returns OFF.  Everywhere else AUTO behaves exactly as
        v2.0.0 (HEAT runs continuously and modulates; COOL outside band
        does work; wrong-side cases pass committed direction through and
        let FLIP_DWELL flip the direction).
        """
        low, high = DEFAULT_HOME_MIN, DEFAULT_HOME_MAX

        # Committed COOL + in band → OFF (the only deviation from v2.0.0)
        for inside in [low, low + 0.5, (low + high) / 2, high - 0.5, high]:
            entity = self._entity(inside=inside)
            entity._auto_mode = HVACMode.COOL
            assert entity._desired_real_mode() == HVACMode.OFF, (
                f"COOL committed, current={inside} in [{low},{high}]: "
                f"expected OFF, got {entity._desired_real_mode()}"
            )

        # Committed HEAT + in band → HEAT (v2.0.0 unchanged; unit modulates)
        for inside in [low, low + 0.5, (low + high) / 2, high - 0.5, high]:
            entity = self._entity(inside=inside)
            entity._auto_mode = HVACMode.HEAT
            assert entity._desired_real_mode() == HVACMode.HEAT, (
                f"HEAT committed, current={inside} in [{low},{high}]: "
                f"expected HEAT, got {entity._desired_real_mode()}"
            )

        # Outside the band, both directions do work (v2.0.0 unchanged)
        cool_committed_above = self._entity(inside=high + 1)
        cool_committed_above._auto_mode = HVACMode.COOL
        assert cool_committed_above._desired_real_mode() == HVACMode.COOL

        heat_committed_below = self._entity(inside=low - 1)
        heat_committed_below._auto_mode = HVACMode.HEAT
        assert heat_committed_below._desired_real_mode() == HVACMode.HEAT

        # Wrong-side excursions pass committed direction through (v2.0.0).
        # The FLIP_DWELL timer flips _auto_mode after sustained excursion.
        cool_committed_below = self._entity(inside=low - 1)
        cool_committed_below._auto_mode = HVACMode.COOL
        assert cool_committed_below._desired_real_mode() == HVACMode.COOL

        heat_committed_above = self._entity(inside=high + 1)
        heat_committed_above._auto_mode = HVACMode.HEAT
        assert heat_committed_above._desired_real_mode() == HVACMode.HEAT


class TestAutoCoolOffInBand:
    """Deliberate-OFF in AUTO is **COOL-only**: the wrapper provides the
    idle the Midea unit fails to provide in COOL mode.

    Diagnosed empirically 2026-04-25/26: COOL holds a min-frequency floor
    and pushes 12-14 °C supply air into rooms already in band.  HEAT does
    *not* have this defect (the unit modulates the compressor down to true
    idle), so the v2.0.0 "never OFF in AUTO" contract is preserved for
    HEAT and only narrowed for COOL.
    """

    def _entity(self, inside, low=21.0, high=23.0, committed=HVACMode.COOL):
        """Build a SmartClimateEntity in AUTO mode with custom band."""
        hass = _make_hass_mock(inside_temp=inside)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = inside
        # Override Home preset's range for this test
        entity._preset_ranges[PRESET_HOME] = (low, high)
        entity._auto_mode = committed
        return entity

    def test_overnight_cool_in_band_traversal_stays_off(self):
        """Reproduces the 2026-04-25 overnight observation.

        With COOL committed, the room sat in [21, 23] for the entire
        night (10 h, 1812 whole_home_temperature samples).  v2.0.0 kept
        the compressor at min-freq floor and pushed cold air into rooms
        already in band.  v3 (COOL-only): OFF the entire time.
        """
        overnight_currents = [
            21.5, 21.7, 22.0, 22.6, 22.7, 22.5,
            21.4, 21.5, 21.3, 22.6, 22.5, 21.4, 21.3,
            22.6, 22.5, 21.4, 22.5, 21.5, 21.7,
        ]
        for current in overnight_currents:
            entity = self._entity(
                inside=current, low=21.0, high=23.0, committed=HVACMode.COOL,
            )
            assert entity._desired_real_mode() == HVACMode.OFF, (
                f"COOL OVERNIGHT-BUG REGRESSION: current={current} "
                f"(in [21, 23]) should be OFF, got "
                f"{entity._desired_real_mode()}.  The Midea COOL min-"
                f"frequency floor wastes energy in band."
            )

    def test_cool_in_band_yields_off(self):
        """Point-tests across the band edges and midpoint, COOL committed."""
        for inside in (21.0, 21.5, 22.0, 22.5, 23.0):
            entity = self._entity(inside=inside, committed=HVACMode.COOL)
            assert entity._desired_real_mode() == HVACMode.OFF

    def test_cool_above_high_runs_cool(self):
        """COOL committed, above band → do work (v2.0.0 unchanged)."""
        entity = self._entity(inside=23.5, committed=HVACMode.COOL)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_cool_below_low_runs_cool_v2_compat(self):
        """COOL committed, below band → COOL passes through (v2.0.0).

        We deliberately do NOT short-circuit to OFF in the wrong-side
        case.  The user's instruction was: OFF only when tending to cool
        inside the band.  Wrong-side COOL is rare and the FLIP_DWELL
        timer flips committed direction to HEAT after 30 min.
        """
        entity = self._entity(inside=20.5, committed=HVACMode.COOL)
        assert entity._desired_real_mode() == HVACMode.COOL


class TestAutoCoolHysteresis:
    """In-band hysteresis for AUTO + COOL.

    v3.0.0 used a flat threshold at the band edge — the wrapper short-
    cycled because crossing into the band by 0.1 °C immediately
    commanded OFF after only ~2 min of useful cooling.  Deployed
    2026-04-26 and observed live: a 2-minute COOL pulse at 23.0 °C.

    Fix: keep cooling until current drops to the midpoint, then OFF;
    don't restart until current rises back above high.  Each
    compressor start now does ~½-band of useful work before stopping,
    amortising the start-up cost over a meaningful pull.
    """

    def _entity(self, inside, low=21.0, high=23.0,
                last_cmd=None, committed=HVACMode.COOL):
        hass = _make_hass_mock(inside_temp=inside)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = inside
        entity._preset_ranges[PRESET_HOME] = (low, high)
        entity._auto_mode = committed
        entity._unit_command = last_cmd
        return entity

    def test_keeps_cooling_above_midpoint(self):
        """COOL state, current still above mid → keep cooling.

        Reproduces the live bug: at 23.0 °C with COOL just sent, v3.0.0
        flipped to OFF immediately.  Hysteresis says keep going to mid.
        """
        for inside in (22.99, 22.5, 22.1):
            entity = self._entity(inside=inside, last_cmd=HVACMode.COOL)
            assert entity._desired_real_mode() == HVACMode.COOL, (
                f"COOL state at {inside} (>mid=22): expected COOL, "
                f"got {entity._desired_real_mode()}"
            )

    def test_stops_at_midpoint(self):
        """COOL state, current ≤ mid → OFF (compressor stops)."""
        for inside in (22.0, 21.9, 21.5, 21.0):
            entity = self._entity(inside=inside, last_cmd=HVACMode.COOL)
            assert entity._desired_real_mode() == HVACMode.OFF, (
                f"COOL state at {inside} (≤mid=22): expected OFF, "
                f"got {entity._desired_real_mode()}"
            )

    def test_off_state_does_not_restart_in_band(self):
        """OFF state, current still ≤ high → stay OFF (no restart).

        This is the other half of the hysteresis: once OFF, the wrapper
        does not restart COOL until current rises above the high edge.
        """
        for inside in (21.0, 22.0, 22.9, 23.0):
            entity = self._entity(inside=inside, last_cmd=HVACMode.OFF)
            assert entity._desired_real_mode() == HVACMode.OFF, (
                f"OFF state at {inside} (≤high=23): expected OFF, "
                f"got {entity._desired_real_mode()}"
            )

    def test_off_state_restarts_above_high(self):
        """OFF state, current > high → start COOL."""
        for inside in (23.01, 23.5, 24.0):
            entity = self._entity(inside=inside, last_cmd=HVACMode.OFF)
            assert entity._desired_real_mode() == HVACMode.COOL

    def test_first_sync_with_no_prior_command_treats_as_off_state(self):
        """`_unit_command is None` (first sync) behaves like OFF state:
        in band → stay OFF; above high → COOL."""
        e_in_band = self._entity(inside=22.5, last_cmd=None)
        assert e_in_band._desired_real_mode() == HVACMode.OFF
        e_above = self._entity(inside=24.0, last_cmd=None)
        assert e_above._desired_real_mode() == HVACMode.COOL

    def test_full_pull_cycle_minimum_two_starts_per_full_cycle(self):
        """End-to-end: simulate temp 24 → 22 → 24 → 22 with the wrapper
        tracking _unit_command.  Verify exactly one COOL start per
        full cycle (not two from short-cycling at 23)."""
        # Cycle 1: 24 → start COOL
        e = self._entity(inside=24.0, last_cmd=HVACMode.OFF)
        assert e._desired_real_mode() == HVACMode.COOL
        e._unit_command = HVACMode.COOL  # simulate sync committed COOL

        # Drift down through band — must stay COOL until mid
        for t in (23.5, 23.0, 22.5, 22.1):
            e._current_temperature = t
            assert e._desired_real_mode() == HVACMode.COOL, (
                f"COOL state at {t} should stay COOL"
            )

        # Hit mid → OFF
        e._current_temperature = 22.0
        assert e._desired_real_mode() == HVACMode.OFF
        e._unit_command = HVACMode.OFF

        # Drift around in band — must stay OFF
        for t in (21.5, 22.0, 22.5, 23.0):
            e._current_temperature = t
            assert e._desired_real_mode() == HVACMode.OFF, (
                f"OFF state at {t} (in band) should stay OFF"
            )

        # Climb above high → COOL again (one start, not many)
        e._current_temperature = 23.1
        assert e._desired_real_mode() == HVACMode.COOL


class TestAutoHeatNeverOff:
    """HEAT in AUTO retains v2.0.0's "never command OFF" contract.

    The Midea unit modulates HEAT down to a true compressor idle when
    the room is at setpoint, so commanding OFF (and absorbing the
    compressor start-up cost on the next call for heat) costs more than
    just letting it sit.
    """

    def _entity(self, inside, low=21.0, high=23.0):
        hass = _make_hass_mock(inside_temp=inside)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = inside
        entity._preset_ranges[PRESET_HOME] = (low, high)
        entity._auto_mode = HVACMode.HEAT
        return entity

    def test_heat_in_band_stays_heat(self):
        """HEAT committed + in band → HEAT (let unit idle internally)."""
        for inside in (21.0, 21.5, 22.0, 22.5, 23.0):
            entity = self._entity(inside=inside)
            assert entity._desired_real_mode() == HVACMode.HEAT

    def test_heat_below_low_stays_heat(self):
        """HEAT committed + cold room → HEAT (real demand)."""
        entity = self._entity(inside=20.5)
        assert entity._desired_real_mode() == HVACMode.HEAT

    def test_heat_above_high_stays_heat_v2_compat(self):
        """HEAT committed + warm room → HEAT (wrong-side; FLIP_DWELL
        eventually commits direction to COOL).  Pass-through matches
        v2.0.0; no early OFF short-circuit."""
        entity = self._entity(inside=23.5)
        assert entity._desired_real_mode() == HVACMode.HEAT


class TestHvacActionInAutoOff:
    """hvac_action surfaces IDLE in deliberate-OFF (AUTO + COOL + in-band).

    The real Midea unit, when commanded OFF, reports hvac_action='off'.
    The wrapper hides that and shows IDLE instead so the user sees
    "AUTO is resting between calls for work" rather than the alarming
    "thermostat is OFF" — which usually signals a manual user override.
    Outside the deliberate-OFF state (HEAT in AUTO, COOL outside band,
    user OFF), hvac_action mirrors the real device's reported action.
    """

    def _entity(self, inside, low=21.0, high=23.0, committed=HVACMode.COOL):
        hass = _make_hass_mock(inside_temp=inside)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = inside
        entity._preset_ranges[PRESET_HOME] = (low, high)
        entity._auto_mode = committed
        return entity

    @pytest.mark.asyncio
    async def test_idle_when_auto_cool_in_band(self):
        """AUTO + COOL + in-band → wrapper sends OFF → hvac_action == IDLE."""
        entity = self._entity(inside=22.0, committed=HVACMode.COOL)
        # Real device will be commanded OFF and report 'off' back to us.
        entity._hvac_action = HVACAction.OFF
        await entity._async_sync_real_climate()
        assert entity._unit_command == HVACMode.OFF
        assert entity.hvac_action == HVACAction.IDLE

    @pytest.mark.asyncio
    async def test_no_idle_when_auto_heat_in_band(self):
        """AUTO + HEAT + in-band → unit runs HEAT (modulating) → mirror.

        IDLE is COOL-only.  HEAT in AUTO never commands the real device
        OFF, so the unit's mirrored action (HEATING or its own internal
        idle) is what the user should see.
        """
        entity = self._entity(inside=22.0, committed=HVACMode.HEAT)
        entity._hvac_action = HVACAction.HEATING
        await entity._async_sync_real_climate()
        assert entity._unit_command == HVACMode.HEAT
        assert entity.hvac_action == HVACAction.HEATING

    @pytest.mark.asyncio
    async def test_mirrors_cooling_when_above_high(self):
        """AUTO + above-high + COOL committed → unit runs COOL → mirror."""
        entity = self._entity(inside=23.5, committed=HVACMode.COOL)
        entity._hvac_action = HVACAction.COOLING  # mirrored from real
        await entity._async_sync_real_climate()
        assert entity._unit_command == HVACMode.COOL
        assert entity.hvac_action == HVACAction.COOLING

    @pytest.mark.asyncio
    async def test_mirrors_heating_when_below_low(self):
        """AUTO + below-low + HEAT committed → unit runs HEAT → mirror."""
        entity = self._entity(inside=20.5, committed=HVACMode.HEAT)
        entity._hvac_action = HVACAction.HEATING
        await entity._async_sync_real_climate()
        assert entity._unit_command == HVACMode.HEAT
        assert entity.hvac_action == HVACAction.HEATING

    def test_user_off_mode_shows_off_not_idle(self):
        """User-commanded OFF must read OFF, not IDLE — IDLE is reserved
        for AUTO's deliberate in-band rest."""
        entity = self._entity(inside=22.0)
        entity._hvac_mode = HVACMode.OFF  # user turned it off
        entity._unit_command = HVACMode.OFF
        entity._hvac_action = HVACAction.OFF
        assert entity.hvac_action == HVACAction.OFF

    def test_initial_state_no_unit_command_passes_through(self):
        """Before the first sync runs, _unit_command is None — fall
        through to whatever the real device last reported."""
        entity = self._entity(inside=22.0)
        entity._unit_command = None
        entity._hvac_action = HVACAction.COOLING
        assert entity.hvac_action == HVACAction.COOLING


class TestStickyAutoMode:
    """Sticky **committed direction** behaviour: a committed HEAT/COOL
    choice survives jitter and only flips after FLIP_DWELL seconds
    continuously past the midpoint by FLIP_MARGIN against the committed
    mode (the room is asking for the opposite mode, not just sitting near
    the boundary).

    These tests assert ``_auto_mode`` (the committed direction) — not
    ``_desired_real_mode()``, which under v3 returns OFF anywhere inside
    the comfort band regardless of what direction is committed.  The
    sticky-direction logic and the unit-command logic are independent
    layers; this class covers the former.
    """

    def _entity(self, inside: float, committed: HVACMode):
        hass = _make_hass_mock(inside_temp=inside)
        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = inside
        entity._auto_mode = committed
        entity._pending_flip_since = None
        now, _ = _fake_clock()
        entity._now = now
        return entity

    def _set(self, entity, inside):
        entity._current_temperature = inside

    def test_cool_stays_cool_above_midpoint(self):
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid + 0.5, committed=HVACMode.COOL)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL

    def test_heat_stays_heat_below_midpoint(self):
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid - 0.5, committed=HVACMode.HEAT)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.HEAT

    def test_cool_survives_jitter_above_low_edge(self):
        """COOL committed; inside jittering at the low band edge does not
        flip mode (this is the wrong-side dead-zone before FLIP_MARGIN)."""
        low, high = DEFAULT_HOME_MIN, DEFAULT_HOME_MAX
        mid = (low + high) / 2
        # mid - FLIP_MARGIN is the wrong-side threshold.  Sit just inside
        # it — i.e. still on the right side / dead-zone boundary.
        entity = self._entity(inside=mid - FLIP_MARGIN + 0.01, committed=HVACMode.COOL)
        for _ in range(5):
            entity._desired_real_mode()
            assert entity._auto_mode == HVACMode.COOL

    def test_cool_does_not_flip_briefly_past_margin(self):
        """COOL committed; a few sensor ticks past the wrong-side margin
        do not commit the flip — only sustained dwell does."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid - FLIP_MARGIN - 0.1, committed=HVACMode.COOL)
        now, advance = _fake_clock()
        entity._now = now
        # First tick: starts the dwell timer.
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL
        assert entity._pending_flip_since is not None
        # Just under the dwell threshold: still COOL.
        advance(FLIP_DWELL - 1)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL

    def test_cool_flips_to_heat_after_dwell(self):
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid - FLIP_MARGIN - 0.1, committed=HVACMode.COOL)
        now, advance = _fake_clock()
        entity._now = now
        entity._desired_real_mode()  # arms timer
        assert entity._auto_mode == HVACMode.COOL
        advance(FLIP_DWELL)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.HEAT
        assert entity._pending_flip_since is None

    def test_heat_flips_to_cool_after_dwell(self):
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid + FLIP_MARGIN + 0.1, committed=HVACMode.HEAT)
        now, advance = _fake_clock()
        entity._now = now
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.HEAT
        advance(FLIP_DWELL)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL

    def test_dwell_resets_on_full_crossback_to_correct_side(self):
        """COOL committed, inside drops below the wrong-side margin then
        bounces all the way back past the midpoint: dwell timer must reset
        so a later excursion needs a fresh full FLIP_DWELL window."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid - FLIP_MARGIN - 0.1, committed=HVACMode.COOL)
        now, advance = _fake_clock()
        entity._now = now
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL
        assert entity._pending_flip_since is not None

        # Cross back fully to the correct (above-midpoint) side.
        advance(60)
        self._set(entity, mid + 0.5)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL
        assert entity._pending_flip_since is None

        # Now drop below margin again — timer must restart from now,
        # a near-full-dwell wait must NOT flip.
        advance(60)
        self._set(entity, mid - FLIP_MARGIN - 0.1)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL
        advance(FLIP_DWELL - 1)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL

    def test_dwell_keeps_running_in_deadzone(self):
        """If inside drops past margin (arms timer) then drifts up into the
        dead-zone (still on wrong half but not past margin), the timer
        must keep running — only a full crossback past midpoint resets."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid - FLIP_MARGIN - 0.1, committed=HVACMode.COOL)
        now, advance = _fake_clock()
        entity._now = now
        entity._desired_real_mode()  # arms timer
        assert entity._auto_mode == HVACMode.COOL
        armed_at = entity._pending_flip_since

        # Drift up into the dead-zone (between mid - FLIP_MARGIN and mid):
        # neither wrong_side nor right_side, so timer state is preserved.
        advance(60)
        self._set(entity, mid - FLIP_MARGIN + 0.1)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL
        assert entity._pending_flip_since == armed_at

        # Drop back past margin and let the original dwell complete.
        advance(60)
        self._set(entity, mid - FLIP_MARGIN - 0.1)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL
        # Total elapsed since arm = 60 + 60 + remaining; advance just enough
        # to cross the dwell threshold.
        advance(FLIP_DWELL - 120)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.HEAT


class TestLeavingAutoClearsCommitment:
    """Switching to a non-AUTO mode discards the AUTO commitment so the
    next AUTO entry re-picks from current conditions."""

    def _entity(self):
        hass = _make_hass_mock()
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._auto_mode = HVACMode.COOL
        entity._pending_flip_since = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
        entity.async_write_ha_state = MagicMock()
        return entity

    @pytest.mark.asyncio
    async def test_switch_to_off_clears_commitment(self):
        entity = self._entity()
        await entity.async_set_hvac_mode(HVACMode.OFF)
        assert entity._auto_mode is None
        assert entity._pending_flip_since is None

    @pytest.mark.asyncio
    async def test_switch_to_heat_clears_commitment(self):
        entity = self._entity()
        await entity.async_set_hvac_mode(HVACMode.HEAT)
        assert entity._auto_mode is None
        assert entity._pending_flip_since is None

    @pytest.mark.asyncio
    async def test_switch_to_cool_clears_commitment(self):
        entity = self._entity()
        await entity.async_set_hvac_mode(HVACMode.COOL)
        assert entity._auto_mode is None
        assert entity._pending_flip_since is None

    @pytest.mark.asyncio
    async def test_switch_to_auto_does_not_clear_existing_commitment(self):
        """Setting AUTO while already in AUTO must not blow away an
        in-flight dwell timer."""
        entity = self._entity()
        # Already in AUTO with a dwell timer running.
        await entity.async_set_hvac_mode(HVACMode.AUTO)
        # _async_sync_real_climate may re-evaluate but the commitment
        # itself is preserved by async_set_hvac_mode.
        assert entity._auto_mode == HVACMode.COOL


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
    """Tests verifying _async_sync_real_climate sends the right setpoint.

    In AUTO the real device's setpoint is the comfort-band midpoint (rounded
    directionally to a whole integer).  HEAT rounds up, COOL rounds down,
    with a fall-back to the opposite-direction integer bound when the band
    is too narrow for the directional rounding to land inside it.
    """

    @pytest.mark.asyncio
    async def test_sync_heat_targets_midpoint_rounded_up(self):
        """HEAT in AUTO: target = ceil(midpoint), within the band."""
        low, high = 21.0, 24.0  # midpoint 22.5 → ceil = 23
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=None,
            inside_temp=low - 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = low
        entity._target_temp_high = high
        entity._current_temperature = low - 1
        entity._auto_mode = HVACMode.HEAT
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_called_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        assert call_args[0][1] == "set_temperature"
        sent = call_args[0][2]["temperature"]
        assert sent == 23
        assert sent == int(sent)
        assert call_args[0][2]["hvac_mode"] == HVACMode.HEAT.value

    @pytest.mark.asyncio
    async def test_sync_cool_targets_midpoint_rounded_down(self):
        """COOL in AUTO: target = floor(midpoint), within the band."""
        low, high = 21.0, 24.0  # midpoint 22.5 → floor = 22
        hass = _make_hass_mock(
            real_climate_state=HVACMode.COOL.value,
            real_climate_temp=None,
            inside_temp=high + 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = low
        entity._target_temp_high = high
        entity._current_temperature = high + 1
        entity._auto_mode = HVACMode.COOL
        await entity._async_sync_real_climate()
        call_args = hass.services.async_call.call_args
        sent = call_args[0][2]["temperature"]
        assert sent == 22
        assert sent == int(sent)

    @pytest.mark.asyncio
    async def test_sync_21_23_band_heat_target_is_22(self):
        """21-23 band: midpoint is 22 (integer), HEAT and COOL both send 22."""
        low, high = 21.0, 23.0
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=None,
            inside_temp=low - 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = low
        entity._target_temp_high = high
        entity._current_temperature = low - 1
        entity._auto_mode = HVACMode.HEAT
        await entity._async_sync_real_climate()
        assert hass.services.async_call.call_args[0][2]["temperature"] == 22

    @pytest.mark.asyncio
    async def test_sync_21_23_band_cool_target_is_22(self):
        low, high = 21.0, 23.0
        hass = _make_hass_mock(
            real_climate_state=HVACMode.COOL.value,
            real_climate_temp=None,
            inside_temp=high + 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = low
        entity._target_temp_high = high
        entity._current_temperature = high + 1
        entity._auto_mode = HVACMode.COOL
        await entity._async_sync_real_climate()
        assert hass.services.async_call.call_args[0][2]["temperature"] == 22

    @pytest.mark.asyncio
    async def test_sync_no_resend_when_device_holds_target(self):
        """When the device's reported setpoint matches what we'd send, no
        service call fires — guards against a per-sensor-update resend
        loop (the integer-rounding fix from #46/#47)."""
        low, high = 21.0, 23.0  # midpoint 22 (integer)
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=22,  # exactly what we'd send
            inside_temp=low - 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = low
        entity._target_temp_high = high
        entity._current_temperature = low - 1
        entity._auto_mode = HVACMode.HEAT
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_cool_sends_off_in_band(self):
        """v3 contract — narrow surgical change vs. v2.0.0.

        AUTO + COOL committed + in band: the wrapper commands the real
        device OFF so the Midea unit actually idles instead of holding
        its COOL minimum-frequency floor.
        """
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        hass = _make_hass_mock(
            real_climate_state=HVACMode.COOL.value,  # currently cooling
            real_climate_temp=22,
            inside_temp=mid,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = mid
        entity._auto_mode = HVACMode.COOL  # already committed to COOL
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_called_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][1] == "set_hvac_mode"
        assert call_args[0][2]["hvac_mode"] == HVACMode.OFF.value

    @pytest.mark.asyncio
    async def test_sync_heat_never_sends_off_in_band(self):
        """v2.0.0 contract preserved for HEAT: never OFF in AUTO+HEAT.

        The Midea unit modulates HEAT down to a true compressor idle when
        in band; commanding OFF would trade that for a start-up cost on
        the next call for heat.
        """
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=22,
            inside_temp=mid,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = mid
        entity._auto_mode = HVACMode.HEAT
        await entity._async_sync_real_climate()
        for call in hass.services.async_call.call_args_list:
            args = call[0]
            sent_mode = args[2].get("hvac_mode")
            assert sent_mode != HVACMode.OFF.value, (
                f"HEAT in AUTO must never send OFF; got {args}"
            )

    @pytest.mark.asyncio
    async def test_sync_off_when_user_commanded_off(self):
        """When smart climate's own hvac_mode is OFF, the real device is
        commanded OFF (path covers the race between sensor-callback
        dispatch and a user-driven mode switch to OFF)."""
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=22,
            inside_temp=22,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.OFF
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = 22.0
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_called_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][1] == "set_hvac_mode"
        assert call_args[0][2]["hvac_mode"] == HVACMode.OFF.value

    @pytest.mark.asyncio
    async def test_sync_no_off_command_when_already_off(self):
        hass = _make_hass_mock(
            real_climate_state=HVACMode.OFF.value,
            real_climate_temp=None,
            inside_temp=22.0,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.OFF
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = 22.0
        await entity._async_sync_real_climate()
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_narrow_band_heat_falls_back_to_floor_high(self):
        """Band 21-21.5 (midpoint 21.25): HEAT ceil = 22 is above high.
        Fall back to floor(high) = 21 to keep the target inside the band
        and an integer."""
        low, high = 21.0, 21.5
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=None,
            inside_temp=low - 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = low
        entity._target_temp_high = high
        entity._current_temperature = low - 1
        entity._auto_mode = HVACMode.HEAT
        await entity._async_sync_real_climate()
        sent = hass.services.async_call.call_args[0][2]["temperature"]
        assert sent == 21
        assert sent == int(sent)

    @pytest.mark.asyncio
    async def test_sync_narrow_band_cool_falls_back_to_ceil_low(self):
        """Band 20.5-21 (midpoint 20.75): COOL floor = 20 is below low.
        Fall back to ceil(low) = 21."""
        low, high = 20.5, 21.0
        hass = _make_hass_mock(
            real_climate_state=HVACMode.COOL.value,
            real_climate_temp=None,
            inside_temp=high + 1,
        )
        entity = _make_entity(hass)
        entity._hvac_mode = HVACMode.AUTO
        entity._preset_mode = PRESET_NONE
        entity._target_temp_low = low
        entity._target_temp_high = high
        entity._current_temperature = high + 1
        entity._auto_mode = HVACMode.COOL
        await entity._async_sync_real_climate()
        sent = hass.services.async_call.call_args[0][2]["temperature"]
        assert sent == 21
        assert sent == int(sent)


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
