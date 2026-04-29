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
    COOL_RESTART_OFFSET,
    DEFAULT_HOME_MIN,
    DEFAULT_HOME_MAX,
    DEFAULT_SLEEP_MIN,
    DEFAULT_SLEEP_MAX,
    DEFAULT_AWAY_MIN,
    DEFAULT_AWAY_MAX,
    FLIP_DWELL,
    FLIP_MARGIN,
    MIN_TEMP_DIFF,
    OUT_OF_BAND_ALERT_MINUTES,
    SENSOR_STALE_MINUTES,
    SHORT_CYCLE_THRESHOLD_PER_H,
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

    def test_initial_pick_ignores_outside_sensor(self):
        """Outside is NOT consulted for the initial pick.

        Empirically the outside sensor in this deployment doesn't
        correlate with the building's thermodynamics (solar gain,
        occupancy, internal sources dominate).  Using outside caused
        the 2026-04-26 HEAT-at-23 bug where cool outside drove a HEAT
        pick while the room sat at the band's high edge.  Whatever
        outside reads, only inside-vs-mid decides.
        """
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        # Inside above mid, outside very cold → still COOL (inside wins).
        e_hot_inside = self._entity(inside=mid + 0.1, outside=mid - 20)
        e_hot_inside._desired_real_mode()
        assert e_hot_inside._auto_mode == HVACMode.COOL

        # Inside below mid, outside very warm → still HEAT.
        e_cold_inside = self._entity(inside=mid - 0.1, outside=mid + 20)
        e_cold_inside._desired_real_mode()
        assert e_cold_inside._auto_mode == HVACMode.HEAT

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

    def test_initial_pick_inside_demand_overrides_cold_outside(self):
        """Regression 2026-04-26: cool outside + hot inside must commit COOL.

        Live bug: PNW cool spring night (~13 °C outside) with room at
        the high edge of the band (23 °C, inside the [21, 23] preset)
        committed AUTO to HEAT because outside was below mid.  HEAT then
        ran unconditionally for FLIP_DWELL (30 min) before flipping to
        COOL — actively heating a room that wanted cooling.

        Fix: inside-temp demand past `mid ± FLIP_MARGIN` wins over
        outside.  Outside is a tiebreaker only for the indeterminate
        near-midpoint case.
        """
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        # Inside well past mid+FLIP_MARGIN, outside cold.
        entity = self._entity(inside=mid + FLIP_MARGIN, outside=mid - 10)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL, (
            "inside ≥ mid + FLIP_MARGIN must commit COOL despite cold "
            "outside — otherwise the wrapper heats a room that wants cooling"
        )

    def test_initial_pick_inside_demand_overrides_warm_outside(self):
        """Mirror: cold inside + warm outside must commit HEAT."""
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        entity = self._entity(inside=mid - FLIP_MARGIN, outside=mid + 10)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.HEAT

    def test_outside_sensor_config_still_accepted_but_ignored(self):
        """Outside-sensor config remains valid (no config-flow break).

        Users with CONF_OUTSIDE_SENSOR set in existing installs keep
        working; the wrapper accepts the config and reads the sensor,
        but the value is no longer consulted by the initial-pick
        logic.  Future features (free-cooling detection, forecast-
        driven preconditioning) may re-introduce its use.
        """
        mid = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2
        # Same inside, two wildly different outside readings: identical pick.
        e1 = self._entity(inside=mid - 0.1, outside=mid - 20)
        e1._desired_real_mode()
        e2 = self._entity(inside=mid - 0.1, outside=mid + 20)
        e2._desired_real_mode()
        assert e1._auto_mode == e2._auto_mode == HVACMode.HEAT
        # Outside is still tracked for display / future use.
        assert e1._outside_temperature == mid - 20
        assert e2._outside_temperature == mid + 20

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
        mid = (low + high) / 2
        # Below the COOL restart threshold (mid + COOL_RESTART_OFFSET): OFF.
        # Above it but in band: hysteresis would START cooling — see
        # TestAutoCoolHysteresis for that side.
        for inside in [low, low + 0.5, mid]:
            entity = self._entity(inside=inside)
            entity._auto_mode = HVACMode.COOL
            assert entity._desired_real_mode() == HVACMode.OFF, (
                f"COOL committed, current={inside} below restart "
                f"threshold {mid + COOL_RESTART_OFFSET}: expected OFF, "
                f"got {entity._desired_real_mode()}"
            )

        # Committed HEAT + in band → HEAT (v2.0.0 unchanged; unit modulates)
        for inside in [low, low + 0.5, (low + high) / 2, high - 0.5, high]:
            entity = self._entity(inside=inside)
            entity._auto_mode = HVACMode.HEAT
            assert entity._desired_real_mode() == HVACMode.HEAT, (
                f"HEAT committed, current={inside} in [{low},{high}]: "
                f"expected HEAT, got {entity._desired_real_mode()}"
            )

        # Outside the band on the *right* side (committed direction
        # matches demand): unit command runs the committed direction.
        cool_committed_above = self._entity(inside=high + 1)
        cool_committed_above._auto_mode = HVACMode.COOL
        assert cool_committed_above._desired_real_mode() == HVACMode.COOL

        heat_committed_below = self._entity(inside=low - 1)
        heat_committed_below._auto_mode = HVACMode.HEAT
        assert heat_committed_below._desired_real_mode() == HVACMode.HEAT

        # Wrong-side band-edge excursions: fast-flip to the correct
        # direction and run *that* direction's unit command.  Skips the
        # 30-min FLIP_DWELL the v2.0.0 / v3.0.0 wrong-side passthrough
        # would have waited on.  See TestFastFlipOnBandViolation.
        cool_committed_below = self._entity(inside=low - 1)
        cool_committed_below._auto_mode = HVACMode.COOL
        assert cool_committed_below._desired_real_mode() == HVACMode.HEAT
        assert cool_committed_below._auto_mode == HVACMode.HEAT

        heat_committed_above = self._entity(inside=high + 1)
        heat_committed_above._auto_mode = HVACMode.HEAT
        assert heat_committed_above._desired_real_mode() == HVACMode.COOL
        assert heat_committed_above._auto_mode == HVACMode.COOL


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

    def test_cool_in_band_below_restart_threshold_yields_off(self):
        """COOL committed, current ≤ mid + COOL_RESTART_OFFSET, OFF state.

        Hysteresis-aware: in this regime the wrapper holds OFF until the
        restart threshold (default 0.75 °C above mid) is exceeded.  The
        upper sliver between threshold and high is the "start cooling"
        zone (covered by TestAutoCoolHysteresis) and not OFF here.
        """
        # Band [21, 23], mid=22, restart threshold=22.75
        for inside in (21.0, 21.5, 22.0, 22.5, 22.75):
            entity = self._entity(inside=inside, committed=HVACMode.COOL)
            assert entity._desired_real_mode() == HVACMode.OFF, (
                f"OFF state at {inside} (≤22.75): expected OFF, "
                f"got {entity._desired_real_mode()}"
            )

    def test_cool_above_high_runs_cool(self):
        """COOL committed, above band → do work (v2.0.0 unchanged)."""
        entity = self._entity(inside=23.5, committed=HVACMode.COOL)
        assert entity._desired_real_mode() == HVACMode.COOL

    def test_cool_below_low_fast_flips_to_heat(self):
        """COOL committed + inside < low: fast-flip to HEAT immediately
        (no 30-min dwell wait).  Replaces the v2.0.0 / v3.0.0 wrong-
        side COOL passthrough — see TestFastFlipOnBandViolation."""
        entity = self._entity(inside=20.5, committed=HVACMode.COOL)
        assert entity._desired_real_mode() == HVACMode.HEAT
        assert entity._auto_mode == HVACMode.HEAT


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

    def test_restart_leads_high_edge_not_at_it(self):
        """Live-deployment regression 2026-04-26.

        User report: "by the time it reaches 23 is too late, we will be
        outside band".  v3.0.1 with restart-at-high (≥23) would let the
        compressor's ramp + air-circulation lag push the room *over*
        the high edge before COOL flow reaches the sensor.

        Fix: restart at mid + COOL_RESTART_OFFSET (default 22.75 for
        the [21, 23] home preset).  By the time the room would have
        otherwise reached 23, COOL is already active and pulling down.
        """
        # 22.5 — well below threshold, OFF state, must stay OFF
        e = self._entity(inside=22.5, last_cmd=HVACMode.OFF)
        assert e._desired_real_mode() == HVACMode.OFF
        # 22.75 — exactly at threshold, OFF state, still OFF
        e._current_temperature = 22.75
        assert e._desired_real_mode() == HVACMode.OFF
        # 22.8 — past threshold, OFF state, START COOL (lead the high edge)
        e._current_temperature = 22.8
        assert e._desired_real_mode() == HVACMode.COOL, (
            "must restart BEFORE high edge, not at/after it"
        )

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

    def test_off_state_does_not_restart_below_offset(self):
        """OFF state, current ≤ mid + offset → stay OFF.

        The wrapper holds OFF until current rises *above* mid + offset
        (default 22.75 for the [21, 23] band).  The 0.25 °C between the
        restart threshold and the high edge is intentional headroom —
        if the wrapper waited until the high edge to restart, the unit's
        ramp + air-circulation lag would let the room overshoot the band.
        """
        for inside in (21.0, 22.0, 22.5, 22.75):
            entity = self._entity(inside=inside, last_cmd=HVACMode.OFF)
            assert entity._desired_real_mode() == HVACMode.OFF, (
                f"OFF state at {inside} (≤22.75): expected OFF, "
                f"got {entity._desired_real_mode()}"
            )

    def test_off_state_restarts_above_offset_lead_high(self):
        """OFF state, current > mid + offset → start COOL.

        Restart fires before the room reaches the high edge so the
        compressor has time to ramp.  By the time current would have
        hit high, COOL air is already flowing.
        """
        for inside in (22.76, 22.9, 23.0, 23.5, 24.0):
            entity = self._entity(inside=inside, last_cmd=HVACMode.OFF)
            assert entity._desired_real_mode() == HVACMode.COOL, (
                f"OFF state at {inside} (>22.75): expected COOL, "
                f"got {entity._desired_real_mode()}"
            )

    def test_first_sync_with_no_prior_command_treats_as_off_state(self):
        """`_unit_command is None` (first sync) behaves like OFF state:
        ≤ mid+offset → stay OFF; > mid+offset → COOL."""
        e_below = self._entity(inside=22.5, last_cmd=None)
        assert e_below._desired_real_mode() == HVACMode.OFF
        e_above_threshold = self._entity(inside=22.8, last_cmd=None)
        assert e_above_threshold._desired_real_mode() == HVACMode.COOL

    def test_full_pull_cycle_one_start_per_cycle(self):
        """End-to-end: simulate temp drift 22 → 22.75 → 22.8 → 22 → 22.8
        with the wrapper tracking _unit_command.  Verify exactly one
        COOL start per full cycle (not multiple flickers near the
        restart threshold)."""
        # Start at midpoint, OFF state
        e = self._entity(inside=22.0, last_cmd=HVACMode.OFF)
        assert e._desired_real_mode() == HVACMode.OFF

        # Drift up through OFF zone — must stay OFF
        for t in (22.25, 22.5, 22.75):
            e._current_temperature = t
            assert e._desired_real_mode() == HVACMode.OFF, (
                f"OFF state at {t} (≤22.75): expected OFF"
            )

        # Cross the restart threshold → start COOL
        e._current_temperature = 22.8
        assert e._desired_real_mode() == HVACMode.COOL
        e._unit_command = HVACMode.COOL

        # Drift down through the band — keep COOL all the way to mid
        for t in (22.7, 22.5, 22.25, 22.1):
            e._current_temperature = t
            assert e._desired_real_mode() == HVACMode.COOL, (
                f"COOL state at {t} (>mid=22) should stay COOL"
            )

        # Hit mid → OFF
        e._current_temperature = 22.0
        assert e._desired_real_mode() == HVACMode.OFF
        e._unit_command = HVACMode.OFF

        # Drift up again to 22.75 — must stay OFF (not flicker)
        for t in (22.25, 22.5, 22.75):
            e._current_temperature = t
            assert e._desired_real_mode() == HVACMode.OFF

        # Cross threshold again — second cycle starts cleanly
        e._current_temperature = 22.8
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

    def test_heat_above_high_fast_flips_to_cool(self):
        """HEAT committed + inside > high: fast-flip to COOL immediately
        (no 30-min dwell wait).  Replaces the v2.0.0 / v3.0.0 wrong-
        side HEAT passthrough — see TestFastFlipOnBandViolation."""
        entity = self._entity(inside=23.5)
        assert entity._desired_real_mode() == HVACMode.COOL
        assert entity._auto_mode == HVACMode.COOL


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


class TestFastFlipOnBandViolation:
    """Fast-flip catches the case the dwell logic is too slow for:
    committed direction is the *opposite* of demand AND inside is past
    the band edge.  No legitimate jitter explanation, so flip
    immediately on the first such tick — skip FLIP_DWELL entirely.

    Live regression 2026-04-26: with HEAT committed (from a bad initial
    pick or stale state) and inside at 23.3 (above high=23), the dwell
    logic took 30 minutes to flip — during which the wrapper actively
    heated a hot room, pushing it further over the high edge.  The
    initial-pick fix in PR #62 prevents the bad pick; this fast-flip
    catches *any* future scenario that lands committed-direction at
    odds with the band (manual override, sudden outside event, etc.).
    """

    def _entity(self, inside, committed, low=21.0, high=23.0):
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
        entity._pending_flip_since = None
        now, _ = _fake_clock()
        entity._now = now
        return entity

    def test_heat_committed_inside_above_high_flips_immediately(self):
        """HEAT committed + inside > high → flip to COOL on first tick.

        Reproduces the live 2026-04-26 bug at 23.3 °C (above high=23).
        Pre-fix: dwell timer waited 30 min before flipping.
        Post-fix: flip on the first sensor tick, no waiting.
        """
        entity = self._entity(inside=23.3, committed=HVACMode.HEAT)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL, (
            "Fast-flip must fire on first tick when committed=HEAT and "
            "inside is above high — no dwell tolerable"
        )
        assert entity._pending_flip_since is None

    def test_cool_committed_inside_below_low_flips_immediately(self):
        """Mirror: COOL committed + inside < low → flip to HEAT immediately."""
        entity = self._entity(inside=20.5, committed=HVACMode.COOL)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.HEAT
        assert entity._pending_flip_since is None

    def test_no_fast_flip_at_exact_band_edge(self):
        """Inside exactly at high (or low) is not a violation — fast-flip
        only fires for strictly past-the-edge.  At the edge, the dwell
        logic still applies as the milder corrective."""
        e_high = self._entity(inside=23.0, committed=HVACMode.HEAT)
        e_high._desired_real_mode()
        assert e_high._auto_mode == HVACMode.HEAT, (
            "Fast-flip must NOT fire at exactly the high edge; only > high"
        )

        e_low = self._entity(inside=21.0, committed=HVACMode.COOL)
        e_low._desired_real_mode()
        assert e_low._auto_mode == HVACMode.COOL

    def test_no_fast_flip_in_band(self):
        """In-band excursion past FLIP_MARGIN but not past band edge:
        let the dwell handle it (the case it was designed for)."""
        # HEAT committed, inside in [mid+FLIP_MARGIN, high) — this is the
        # dwell's territory, not fast-flip's.
        entity = self._entity(inside=22.6, committed=HVACMode.HEAT)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.HEAT, (
            "In-band past FLIP_MARGIN but not past high: dwell, not fast-flip"
        )

    def test_no_fast_flip_when_committed_direction_matches_demand(self):
        """COOL committed + inside > high: do not flip (this is the
        right direction; the unit command logic returns COOL)."""
        entity = self._entity(inside=23.5, committed=HVACMode.COOL)
        entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL

        # HEAT committed + inside < low: do not flip (correct direction).
        entity2 = self._entity(inside=20.5, committed=HVACMode.HEAT)
        entity2._desired_real_mode()
        assert entity2._auto_mode == HVACMode.HEAT

    def test_fast_flip_then_unit_command_runs_correct_direction(self):
        """End-to-end: HEAT committed + inside=23.3 → fast-flip to COOL
        → unit command returns COOL (since inside > high under COOL)."""
        entity = self._entity(inside=23.3, committed=HVACMode.HEAT)
        result = entity._desired_real_mode()
        assert entity._auto_mode == HVACMode.COOL
        assert result == HVACMode.COOL, (
            "Same tick that flips must produce the correct unit command — "
            "no half-tick limbo where the wrong direction still drives"
        )


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
    async def test_sync_heat_targets_low_edge(self):
        """HEAT in AUTO: setpoint = low edge of band (NOT midpoint).

        v3.3.0 asymmetric setpoint design.  The unit's own ±0.5 °C
        hysteresis around setpoint=low keeps the room near low (e.g.
        21..21.5 for low=21), avoiding active heating of comfortable
        rooms — the v3.1.x ghost-HEAT pattern.
        """
        low, high = 21.0, 24.0  # HEAT setpoint should be low = 21
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
        assert sent == 21, f"HEAT setpoint should be low (21), got {sent}"
        assert sent == int(sent)
        assert call_args[0][2]["hvac_mode"] == HVACMode.HEAT.value

    @pytest.mark.asyncio
    async def test_sync_cool_targets_high_minus_one(self):
        """COOL in AUTO: setpoint = high - 1 (NOT midpoint).

        Unit cools toward high-1 with its own ±0.5 hysteresis, keeping
        the room in the upper half of the band (e.g. 22..23 for
        high=24).  Less aggressive than mid-targeting; saves energy
        for wider bands.
        """
        low, high = 21.0, 24.0  # COOL setpoint should be high - 1 = 23
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
        assert sent == 23, f"COOL setpoint should be high-1 (23), got {sent}"
        assert sent == int(sent)

    @pytest.mark.asyncio
    async def test_sync_21_23_band_heat_target_is_low(self):
        """21-23 band: HEAT setpoint = low = 21."""
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
        assert hass.services.async_call.call_args[0][2]["temperature"] == 21

    @pytest.mark.asyncio
    async def test_sync_21_23_band_cool_target_is_22(self):
        """21-23 band: COOL setpoint = high-1 = 22 (coincidentally same
        as old midpoint-based behavior for this narrow band)."""
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
        low, high = 21.0, 23.0  # HEAT setpoint = low = 21
        hass = _make_hass_mock(
            real_climate_state=HVACMode.HEAT.value,
            real_climate_temp=21,  # exactly what we'd send (HEAT setpoint = low)
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
# Unit tests – problem detection (`problems` attribute)
# ---------------------------------------------------------------------------

class TestProblemDetection:
    """The `problems` extra_state_attribute lists detected issues —
    empty when healthy.  Used by dashboards / templates to surface
    actionable notifications instead of silent failure modes."""

    def _entity(self, *, hvac_mode=HVACMode.AUTO, inside=22.0,
                low=21.0, high=23.0, real_state=HVACMode.OFF.value,
                inside_state_value=None, inside_last_updated=None):
        # inside_state_value overrides the sensor's reported state string;
        # when None the helper uses str(inside).
        if inside_state_value is None:
            inside_state_value = str(inside)
        hass = MagicMock()
        hass.config.units.temperature_unit = "°C"

        sensor_state = MagicMock()
        sensor_state.state = inside_state_value
        sensor_state.last_updated = (
            inside_last_updated
            or datetime.datetime(2026, 4, 26, 22, 0, tzinfo=datetime.timezone.utc)
        )

        real = MagicMock()
        real.state = real_state
        real.attributes = {"hvac_action": None, "temperature": None}

        def _get(eid):
            if eid == REAL_CLIMATE_ID: return real
            if eid == INSIDE_SENSOR_ID: return sensor_state
            return None
        hass.states.get = _get
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock()

        config = {
            CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
            CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
        }
        entity = _make_entity(hass, config)
        entity._hvac_mode = hvac_mode
        entity._preset_mode = PRESET_HOME
        entity._current_temperature = inside
        entity._preset_ranges[PRESET_HOME] = (low, high)
        # Pin _now to the sensor's last_updated so "fresh" is the default.
        fixed_now = sensor_state.last_updated
        entity._now = lambda: fixed_now
        return entity, sensor_state, real

    def test_healthy_returns_empty_list(self):
        entity, _, _ = self._entity()
        assert entity._detect_problems() == []
        # The attribute dict includes problems plus persisted state machine.
        attrs = entity.extra_state_attributes
        assert attrs["problems"] == []
        assert "auto_mode_committed" in attrs
        assert "last_unit_command" in attrs

    def test_inside_sensor_unavailable(self):
        entity, sensor, _ = self._entity(inside_state_value="unavailable")
        problems = entity._detect_problems()
        assert any(p.startswith("inside_sensor_") for p in problems), problems

    def test_inside_sensor_stale(self):
        # Sensor last updated 30 min before "now".
        now = datetime.datetime(2026, 4, 26, 22, 0,
                                tzinfo=datetime.timezone.utc)
        old = now - datetime.timedelta(minutes=SENSOR_STALE_MINUTES + 5)
        entity, _, _ = self._entity(inside_last_updated=old)
        entity._now = lambda: now
        problems = entity._detect_problems()
        assert any(p.startswith("sensor_stale:") for p in problems), problems

    def test_real_climate_unavailable(self):
        entity, _, real = self._entity()
        real.state = "unavailable"
        problems = entity._detect_problems()
        assert "real_climate_unavailable" in problems

    def test_out_of_band_under_threshold_not_reported(self):
        """Brief out-of-band excursion below threshold: no alert yet."""
        entity, _, _ = self._entity(inside=24.0)  # above high=23
        # Pretend we crossed out 5 min ago.
        now = datetime.datetime(2026, 4, 26, 22, 0,
                                tzinfo=datetime.timezone.utc)
        entity._now = lambda: now
        entity._out_of_band_since = now - datetime.timedelta(minutes=5)
        assert all(not p.startswith("out_of_band") for p in entity._detect_problems())

    def test_out_of_band_sustained_reports(self):
        """Sustained out-of-band past threshold: alert."""
        entity, _, _ = self._entity(inside=24.0)
        now = datetime.datetime(2026, 4, 26, 22, 0,
                                tzinfo=datetime.timezone.utc)
        entity._now = lambda: now
        entity._out_of_band_since = (
            now - datetime.timedelta(minutes=OUT_OF_BAND_ALERT_MINUTES + 5)
        )
        problems = entity._detect_problems()
        assert any(p.startswith("out_of_band:") for p in problems), problems

    def test_out_of_band_only_in_auto(self):
        """Manual HEAT/COOL/OFF doesn't fire out-of-band alerts — the
        user is on their own terms there."""
        entity, _, _ = self._entity(hvac_mode=HVACMode.HEAT, inside=24.0)
        # Even with stale out_of_band_since, manual mode shouldn't alert.
        entity._out_of_band_since = (
            entity._now() - datetime.timedelta(minutes=60)
        )
        # Force the AUTO gate by switching: in manual mode, the wrapper
        # also wouldn't track _out_of_band_since via _on_inside_sensor_update.
        # We rely on the gate inside _detect_problems.
        assert all(not p.startswith("out_of_band") for p in entity._detect_problems())

    def test_short_cycle_under_threshold_not_reported(self):
        entity, _, _ = self._entity()
        now = entity._now()
        # SHORT_CYCLE_THRESHOLD_PER_H starts in the last hour: not over.
        for i in range(SHORT_CYCLE_THRESHOLD_PER_H):
            entity._cool_start_times.append(
                now - datetime.timedelta(minutes=i * 5)
            )
        assert all(not p.startswith("short_cycle") for p in entity._detect_problems())

    def test_short_cycle_over_threshold_reports(self):
        entity, _, _ = self._entity()
        now = entity._now()
        # SHORT_CYCLE_THRESHOLD_PER_H + 2 starts in the last hour: over.
        for i in range(SHORT_CYCLE_THRESHOLD_PER_H + 2):
            entity._cool_start_times.append(
                now - datetime.timedelta(minutes=i * 3)
            )
        problems = entity._detect_problems()
        assert any(p.startswith("short_cycle:") for p in problems), problems

    def test_short_cycle_window_is_trailing_hour(self):
        """Old starts (>1h ago) are excluded from the rolling count."""
        entity, _, _ = self._entity()
        now = entity._now()
        # 5 starts > 1h ago — should not count.
        for i in range(5):
            entity._cool_start_times.append(
                now - datetime.timedelta(hours=2, minutes=i)
            )
        # 2 starts in the last hour — well under threshold.
        for i in range(2):
            entity._cool_start_times.append(
                now - datetime.timedelta(minutes=i * 5)
            )
        assert all(not p.startswith("short_cycle") for p in entity._detect_problems())

    def test_multiple_problems_compose(self):
        """Independent issues stack; we don't short-circuit at the first."""
        entity, _, real = self._entity(inside_state_value="unavailable")
        real.state = "unavailable"
        problems = entity._detect_problems()
        assert any(p.startswith("inside_sensor_") for p in problems)
        assert "real_climate_unavailable" in problems

    def test_command_desync_within_grace_not_reported(self):
        """Command just sent — real device hasn't transitioned yet but
        we're inside the grace window.  No alert."""
        entity, _, real = self._entity()
        real.state = "off"  # real device says off
        entity._unit_command = HVACMode.COOL  # wrapper just commanded cool
        # 30 seconds ago — within COMMAND_GRACE_SECONDS (60)
        entity._unit_command_at = entity._now() - datetime.timedelta(seconds=30)
        assert all(not p.startswith("command_desync") for p in entity._detect_problems())

    def test_command_desync_past_grace_reports(self):
        """Real device hasn't matched the command after grace window —
        flag as desync.  Reproduces the 2026-04-26 ghost-HEAT pattern
        where the wrapper believed it commanded COOL but real device
        sat in HEAT."""
        entity, _, real = self._entity()
        real.state = "heat"
        entity._unit_command = HVACMode.COOL
        # Two minutes ago — well past 60s grace
        entity._unit_command_at = entity._now() - datetime.timedelta(minutes=2)
        problems = entity._detect_problems()
        assert any(
            p == "command_desync:want=cool_got=heat" for p in problems
        ), problems

    def test_command_desync_real_unavailable_not_reported(self):
        """Real device unavailable: don't flag desync (already covered
        by `real_climate_unavailable` and not actionable as desync)."""
        entity, _, real = self._entity()
        real.state = "unavailable"
        entity._unit_command = HVACMode.COOL
        entity._unit_command_at = entity._now() - datetime.timedelta(minutes=2)
        assert all(not p.startswith("command_desync") for p in entity._detect_problems())

    def test_command_desync_no_command_yet_not_reported(self):
        """Wrapper has never sent a command (`_unit_command_at is None`)
        — no desync to report."""
        entity, _, real = self._entity()
        real.state = "heat"
        entity._unit_command = None
        entity._unit_command_at = None
        assert all(not p.startswith("command_desync") for p in entity._detect_problems())

    def test_command_desync_match_not_reported(self):
        """Real device matches command: no desync."""
        entity, _, real = self._entity()
        real.state = "cool"
        entity._unit_command = HVACMode.COOL
        entity._unit_command_at = entity._now() - datetime.timedelta(minutes=5)
        assert all(not p.startswith("command_desync") for p in entity._detect_problems())


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
        auto_mode_committed: str | None = None,
        last_unit_command: str | None = None,
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
        if auto_mode_committed is not None:
            attrs["auto_mode_committed"] = auto_mode_committed
        if last_unit_command is not None:
            attrs["last_unit_command"] = last_unit_command
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

    @pytest.mark.asyncio
    async def test_auto_mode_committed_restored(self):
        """`_auto_mode` is restored from last_state.attributes — without
        this, every HA restart re-runs the initial pick on potentially
        glitchy post-restart sensor data (the 2026-04-26 ghost-HEAT bug).
        """
        last_state = self._make_last_state(
            hvac_mode=HVACMode.AUTO.value,
            auto_mode_committed=HVACMode.COOL.value,
        )
        entity, _ = await self._setup_entity(last_state, inside_temp=22.5)
        assert entity._auto_mode == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_last_unit_command_restored(self):
        """COOL hysteresis state survives restarts: if we were mid-pull
        (last command COOL), restoration keeps that so the hysteresis
        doesn't restart from the OFF state."""
        last_state = self._make_last_state(
            hvac_mode=HVACMode.AUTO.value,
            auto_mode_committed=HVACMode.COOL.value,
            last_unit_command=HVACMode.COOL.value,
        )
        entity, _ = await self._setup_entity(last_state, inside_temp=22.3)
        assert entity._unit_command == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_no_initial_pick_when_committed_restored(self):
        """When `_auto_mode` is restored, the wrapper must not run the
        initial-pick logic against current sensor data — the whole
        point of persistence is to skip that step.

        Set up a scenario that would be a *bad* initial pick (inside
        21.5, mid 22.5 → would pick HEAT) but with restored COOL.  The
        wrapper should keep COOL.
        """
        last_state = self._make_last_state(
            hvac_mode=HVACMode.AUTO.value,
            preset_mode=PRESET_HOME,
            target_temp_low=DEFAULT_HOME_MIN,
            target_temp_high=DEFAULT_HOME_MAX,
            auto_mode_committed=HVACMode.COOL.value,
        )
        # inside well below mid — would have picked HEAT if uninitialised.
        entity, _ = await self._setup_entity(
            last_state, inside_temp=DEFAULT_HOME_MIN + 0.5
        )
        assert entity._auto_mode == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_invalid_committed_value_falls_back_to_none(self):
        """A garbage `auto_mode_committed` (e.g. an old version's value
        format) must not crash — fall back to None and the wrapper will
        do an initial pick on the next sensor tick."""
        last_state = self._make_last_state(
            hvac_mode=HVACMode.AUTO.value,
            auto_mode_committed="garbage",
        )
        entity, _ = await self._setup_entity(last_state, inside_temp=22.5)
        assert entity._auto_mode in (None, HVACMode.HEAT, HVACMode.COOL)
        # Specifically: it MUST NOT be the literal "garbage" string.
        assert entity._auto_mode != "garbage"

    @pytest.mark.asyncio
    async def test_extra_attrs_round_trip(self):
        """Round-trip: save → restore → save should preserve values.
        Pins the contract that `extra_state_attributes` keys match what
        `async_added_to_hass` reads."""
        last_state = self._make_last_state(
            hvac_mode=HVACMode.AUTO.value,
            auto_mode_committed=HVACMode.COOL.value,
            last_unit_command=HVACMode.OFF.value,
        )
        entity, _ = await self._setup_entity(last_state)
        attrs = entity.extra_state_attributes
        assert attrs["auto_mode_committed"] == HVACMode.COOL.value
        assert attrs["last_unit_command"] == HVACMode.OFF.value
