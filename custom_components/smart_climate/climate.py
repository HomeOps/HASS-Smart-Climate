"""Smart Climate - EcoBee-like thermostat platform for Home Assistant.

Wraps a physical climate device and adds:
- Comfort presets (Home, Sleep, Away) with configurable temperature ranges
- Automatic heat/cool switching based on inside temperature vs setpoint range
- Outside temperature awareness for smarter mode decisions
- Manual mode for direct control
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Any

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    PRESET_AWAY,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_AWAY_MAX,
    CONF_AWAY_MIN,
    CONF_HOME_MAX,
    CONF_HOME_MIN,
    CONF_INSIDE_SENSOR,
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
    FLIP_DWELL,
    FLIP_MARGIN,
    MAX_TEMP,
    MIN_TEMP,
    MIN_TEMP_DIFF,
    TEMP_STEP,
)

_LOGGER = logging.getLogger(__name__)

# Presets offered to the user (HA standard preset constants)
SUPPORTED_PRESETS = [PRESET_HOME, PRESET_SLEEP, PRESET_AWAY, PRESET_NONE]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Smart Climate from a config entry."""
    config = {**entry.data, **entry.options}
    entity = SmartClimateEntity(hass, entry.entry_id, entry.title, config)
    async_add_entities([entity])


class SmartClimateEntity(ClimateEntity, RestoreEntity):
    """EcoBee-like smart thermostat that wraps a real climate entity.

    In AUTO mode the entity manages a *temperature range* (low/high setpoints)
    and picks HEAT or COOL once, holding that mode while the (modulating)
    real device settles on the comfort-band midpoint.  HEAT↔COOL flips only
    after the inside temperature has been continuously past the midpoint by
    FLIP_MARGIN for FLIP_DWELL seconds — i.e. the room is genuinely asking
    for the opposite mode, not just jittering across the boundary.  The real
    device is never commanded OFF in AUTO; on inverter heat pumps a steady
    setpoint at low modulation costs less than the start-up of an OFF→ON
    cycle, and short OFF cycles defeat the unit's own steady-state operation.

    Four presets are supported:
      - Home  – default daytime comfort range
      - Sleep – cooler nighttime comfort range
      - Away  – wider range for unoccupied periods
      - None  – manual/direct control (no preset enforced)

    Master/slave relationship with the real climate entity:
    This entity is the sole writer of the wrapped device's hvac_mode and
    temperature.  State-change events from the real device are used only to
    mirror its hvac_action for UI display – they never feed back into the
    authoritative state owned here (hvac_mode, preset_mode, setpoints).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Suppress backwards-compat warning for turn_on / turn_off
    _enable_turn_on_off_backwards_compat = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        name: str,
        config: dict,
    ) -> None:
        """Initialize the entity."""
        self.hass = hass
        self._entry_id = entry_id
        self._attr_name = name
        self._attr_unique_id = entry_id

        self._real_climate_id: str = config[CONF_REAL_CLIMATE]
        self._inside_sensor_id: str = config[CONF_INSIDE_SENSOR]
        self._outside_sensor_id: str | None = config.get(CONF_OUTSIDE_SENSOR)

        # Preset temperature ranges keyed by HA preset constant
        self._preset_ranges: dict[str, tuple[float, float]] = {
            PRESET_HOME: (
                config.get(CONF_HOME_MIN, DEFAULT_HOME_MIN),
                config.get(CONF_HOME_MAX, DEFAULT_HOME_MAX),
            ),
            PRESET_SLEEP: (
                config.get(CONF_SLEEP_MIN, DEFAULT_SLEEP_MIN),
                config.get(CONF_SLEEP_MAX, DEFAULT_SLEEP_MAX),
            ),
            PRESET_AWAY: (
                config.get(CONF_AWAY_MIN, DEFAULT_AWAY_MIN),
                config.get(CONF_AWAY_MAX, DEFAULT_AWAY_MAX),
            ),
        }

        # Internal state
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._preset_mode: str = PRESET_HOME
        self._target_temp_low: float = DEFAULT_HOME_MIN
        self._target_temp_high: float = DEFAULT_HOME_MAX
        self._target_temperature: float = (DEFAULT_HOME_MIN + DEFAULT_HOME_MAX) / 2.0
        self._current_temperature: float | None = None
        self._outside_temperature: float | None = None
        self._hvac_action: HVACAction | None = None

        # Guard flag – skip sensor-triggered syncs while a control call is in
        # flight so the two paths do not issue overlapping service calls.
        self._updating_from_control: bool = False

        # Sticky HEAT/COOL choice for AUTO mode.  None means no AUTO decision
        # has been made yet (initial entry, or after leaving AUTO).  Once
        # set, _desired_real_mode returns this value until the flip rule
        # commits the opposite mode.
        self._auto_mode: HVACMode | None = None

        # Timestamp at which the inside temperature first crossed FLIP_MARGIN
        # past the midpoint *against* the current _auto_mode.  Cleared when
        # the temperature returns to the correct half of the band; flips
        # _auto_mode once it has been set continuously for FLIP_DWELL.
        self._pending_flip_since: datetime.datetime | None = None

        # Last unit command (HEAT/COOL/OFF) the wrapper computed for the
        # real device.  Used by the hvac_action property to surface IDLE
        # while in AUTO + deliberate-OFF (inside the comfort band) so the
        # frontend distinguishes "AUTO resting" from "user turned it off".
        self._unit_command: HVACMode | None = None

    # ------------------------------------------------------------------
    # ClimateEntity properties
    # ------------------------------------------------------------------

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement used by the platform."""
        return self.hass.config.units.temperature_unit

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available HVAC modes."""
        return [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT, HVACMode.COOL]

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC operation mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running HVAC action.

        In AUTO mode, when the wrapper has commanded the real device OFF
        (deliberate-OFF inside the comfort band), surface IDLE so the
        frontend distinguishes "AUTO resting between calls for work" from
        "user turned the thermostat off entirely".  Otherwise mirror the
        real device's reported action.
        """
        if (
            self._hvac_mode == HVACMode.AUTO
            and self._unit_command == HVACMode.OFF
        ):
            return HVACAction.IDLE
        return self._hvac_action

    @property
    def preset_modes(self) -> list[str]:
        """Return the list of available preset modes."""
        return SUPPORTED_PRESETS

    @property
    def preset_mode(self) -> str:
        """Return current preset mode."""
        return self._preset_mode

    @property
    def current_temperature(self) -> float | None:
        """Return current temperature from the inside sensor."""
        return self._current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return single-point target temperature.

        In HEAT / COOL modes this is the direct user-configured setpoint.
        In AUTO mode the midpoint of the active comfort range is returned so
        that the ``temperature`` state attribute is never ``null`` – it gives
        users a meaningful reference value while ``target_temp_low`` /
        ``target_temp_high`` still describe the full range.
        """
        if self._hvac_mode == HVACMode.AUTO:
            return (self._target_temp_low + self._target_temp_high) / 2.0
        return self._target_temperature

    @property
    def target_temperature_low(self) -> float | None:
        """Return low end of target temperature range (AUTO mode only)."""
        if self._hvac_mode == HVACMode.AUTO:
            return self._target_temp_low
        return None

    @property
    def target_temperature_high(self) -> float | None:
        """Return high end of target temperature range (AUTO mode only)."""
        if self._hvac_mode == HVACMode.AUTO:
            return self._target_temp_high
        return None

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return bitmask of supported features."""
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            | ClimateEntityFeature.PRESET_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )

    @property
    def min_temp(self) -> float:
        """Return the minimum settable temperature."""
        return MIN_TEMP

    @property
    def max_temp(self) -> float:
        """Return the maximum settable temperature."""
        return MAX_TEMP

    @property
    def target_temperature_step(self) -> float:
        """Return the supported step size for target temperature."""
        return TEMP_STEP

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Run when entity is added; restore state and subscribe to changes."""
        await super().async_added_to_hass()

        # Restore previous state
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        ):
            try:
                self._hvac_mode = HVACMode(last_state.state)
            except ValueError:
                self._hvac_mode = HVACMode.OFF

            attrs = last_state.attributes
            restored_preset = attrs.get("preset_mode", PRESET_HOME)
            self._preset_mode = (
                restored_preset if restored_preset in SUPPORTED_PRESETS else PRESET_HOME
            )
            if ATTR_TARGET_TEMP_LOW in attrs:
                self._target_temp_low = float(attrs[ATTR_TARGET_TEMP_LOW])
            if ATTR_TARGET_TEMP_HIGH in attrs:
                self._target_temp_high = float(attrs[ATTR_TARGET_TEMP_HIGH])
            if ATTR_TEMPERATURE in attrs and attrs[ATTR_TEMPERATURE] is not None:
                self._target_temperature = float(attrs[ATTR_TEMPERATURE])

        # Populate initial state from current entity states
        self._sync_from_real_climate()
        self._sync_from_sensors()

        # Push the restored state to the real climate device so that the
        # correct heat/cool commands are sent after a HA restart.
        if self._hvac_mode != HVACMode.OFF:
            await self._async_sync_real_climate()

        # Subscribe to state changes on all tracked entities.
        entities_to_track = [self._real_climate_id, self._inside_sensor_id]
        if self._outside_sensor_id:
            entities_to_track.append(self._outside_sensor_id)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                entities_to_track,
                self._async_entity_state_changed,
            )
        )

    @callback
    def _async_entity_state_changed(self, event: Any) -> None:
        """Dispatch state-change events to the appropriate handler."""
        entity_id: str = event.data.get("entity_id", "")
        if entity_id == self._real_climate_id:
            self._on_real_climate_update()
        elif entity_id == self._inside_sensor_id:
            self._on_inside_sensor_update()
        elif entity_id == self._outside_sensor_id:
            self._on_outside_sensor_update()

    # ------------------------------------------------------------------
    # Initial state sync helpers
    # ------------------------------------------------------------------

    def _sync_from_real_climate(self) -> None:
        """Pull current state from the real climate entity at startup."""
        state = self.hass.states.get(self._real_climate_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        action = state.attributes.get("hvac_action")
        if action:
            try:
                self._hvac_action = HVACAction(action)
            except ValueError:
                pass

    def _sync_from_sensors(self) -> None:
        """Pull current temperatures from the sensor entities at startup."""
        inside_state = self.hass.states.get(self._inside_sensor_id)
        if inside_state and inside_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            None,
        ):
            try:
                self._current_temperature = float(inside_state.state)
            except (ValueError, TypeError):
                pass

        if self._outside_sensor_id:
            outside_state = self.hass.states.get(self._outside_sensor_id)
            if outside_state and outside_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
            ):
                try:
                    self._outside_temperature = float(outside_state.state)
                except (ValueError, TypeError):
                    pass

    # ------------------------------------------------------------------
    # State-change callbacks
    # ------------------------------------------------------------------

    @callback
    def _on_real_climate_update(self) -> None:
        """Mirror the real device's hvac_action for UI display.

        Smart climate is the authoritative source of truth for mode, preset,
        and setpoints – see the master/slave note at the top of this class.
        Real-device state-change events are used only to surface the current
        action (heating / cooling / idle / off) in the frontend.
        """
        state = self.hass.states.get(self._real_climate_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        action_str = state.attributes.get("hvac_action")
        if not action_str:
            return

        try:
            new_action = HVACAction(action_str)
        except ValueError:
            return

        if self._hvac_action != new_action:
            self._hvac_action = new_action
            self.async_write_ha_state()

    @callback
    def _on_inside_sensor_update(self) -> None:
        """Handle inside temperature sensor state changes."""
        state = self.hass.states.get(self._inside_sensor_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        try:
            new_temp = float(state.state)
        except (ValueError, TypeError):
            return
        if math.isnan(new_temp):
            return

        old_temp = self._current_temperature
        self._current_temperature = new_temp

        # In AUTO preset mode, re-evaluate the real device's mode
        if (
            self._hvac_mode == HVACMode.AUTO
            and self._preset_mode != PRESET_NONE
            and not self._updating_from_control
        ):
            self.hass.async_create_task(self._async_sync_real_climate())

        if old_temp != new_temp:
            self.async_write_ha_state()

    @callback
    def _on_outside_sensor_update(self) -> None:
        """Handle outside temperature sensor state changes."""
        state = self.hass.states.get(self._outside_sensor_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        try:
            new_temp = float(state.state)
        except (ValueError, TypeError):
            return
        if math.isnan(new_temp):
            return

        self._outside_temperature = new_temp

        # In AUTO preset mode, outside temperature changes may refine the mode
        if (
            self._hvac_mode == HVACMode.AUTO
            and self._preset_mode != PRESET_NONE
            and not self._updating_from_control
        ):
            self.hass.async_create_task(self._async_sync_real_climate())

    # ------------------------------------------------------------------
    # Temperature / preset helpers
    # ------------------------------------------------------------------

    def _preset_midpoint(self) -> float:
        """Return the midpoint of the current comfort range."""
        low, high = self._active_range()
        return (low + high) / 2.0

    def _active_range(self) -> tuple[float, float]:
        """Return the (low, high) temperature range for the active preset."""
        if self._preset_mode in self._preset_ranges:
            return self._preset_ranges[self._preset_mode]
        # PRESET_NONE / manual – use whatever was last set
        return (self._target_temp_low, self._target_temp_high)

    def _now(self) -> datetime.datetime:
        """Return the current UTC time.  Indirection point for tests."""
        return datetime.datetime.now(datetime.timezone.utc)

    def _desired_real_mode(self) -> HVACMode:
        """Determine which mode (HEAT/COOL/OFF) the real device should be in.

        For non-AUTO modes the user's choice is passed through.

        In AUTO mode the wrapper maintains a sticky **committed direction**
        (HEAT or COOL) using v2.0.0's FLIP_DWELL/FLIP_MARGIN logic — same
        as before:
        - First entry: pick HEAT or COOL based on outside sensor (or
          inside-vs-midpoint with no outside sensor).  Tie-breaks to COOL.
        - Subsequent ticks: keep the committed direction; only flip after
          inside has been past midpoint by FLIP_MARGIN against the committed
          direction for FLIP_DWELL seconds (with the dead-zone hysteresis
          that doesn't reset the timer in the buffer area).

        The **unit command** sent to the real device is then derived from
        current vs the comfort band, *regardless* of committed direction:

        - `current ∈ [low, high]`  →  **OFF** (no work needed)
        - committed COOL & `current > high`  →  COOL  (room hot, do work)
        - committed HEAT & `current < low`   →  HEAT  (room cold, do work)
        - wrong-side excursion (committed direction opposite to demand)
          →  OFF, FLIP_DWELL timer counts toward direction flip

        This refines v2.0.0's "never OFF in AUTO" rule.  v2.0.0 assumed the
        underlying device would idle the compressor at min modulation when
        in band; on Midea inverter heat pumps that's false — they have a
        minimum-frequency floor and hold the compressor running.  v3 lets
        the wrapper provide the idle by commanding OFF directly.
        """
        if self._hvac_mode == HVACMode.OFF:
            return HVACMode.OFF
        if self._hvac_mode in (HVACMode.HEAT, HVACMode.COOL):
            return self._hvac_mode

        # AUTO mode – need an inside reading to make any decision.
        if self._current_temperature is None or math.isnan(self._current_temperature):
            return self._auto_mode or HVACMode.AUTO

        inside = self._current_temperature
        low, high = self._active_range()
        mid = (low + high) / 2.0

        # Initial mode pick.
        if self._auto_mode is None:
            ref: float | None = self._outside_temperature
            if ref is None or math.isnan(ref):
                ref = inside
            self._auto_mode = HVACMode.HEAT if ref < mid else HVACMode.COOL
            self._pending_flip_since = None
            # fall through to the band-aware unit-command logic below

        # Sticky direction-flip evaluation (unchanged from v2.0.0).  Wrong-
        # side / right-side bands have a deadzone in between (mid ± FLIP_MARGIN)
        # where the timer keeps running but is not reset, so sensor jitter
        # near the margin doesn't repeatedly cancel an in-progress flip.
        if self._auto_mode == HVACMode.COOL:
            wrong_side = inside <= mid - FLIP_MARGIN
            right_side = inside >= mid
        else:  # HEAT
            wrong_side = inside >= mid + FLIP_MARGIN
            right_side = inside <= mid

        now = self._now()
        if wrong_side:
            if self._pending_flip_since is None:
                self._pending_flip_since = now
            elif (now - self._pending_flip_since).total_seconds() >= FLIP_DWELL:
                self._auto_mode = (
                    HVACMode.HEAT if self._auto_mode == HVACMode.COOL else HVACMode.COOL
                )
                self._pending_flip_since = None
        elif right_side:
            self._pending_flip_since = None

        # Unit command from current vs band.  OFF inside the band; the
        # committed direction's active mode only outside the band on the
        # corresponding side.  Wrong-side excursions return OFF (don't fight
        # the room while the dwell timer counts toward a flip).
        if self._auto_mode == HVACMode.COOL:
            if inside > high:
                return HVACMode.COOL
            return HVACMode.OFF
        else:  # HEAT committed
            if inside < low:
                return HVACMode.HEAT
            return HVACMode.OFF

    # ------------------------------------------------------------------
    # Real-climate synchronisation
    # ------------------------------------------------------------------

    async def _async_sync_real_climate(self) -> None:
        """Push the desired mode and setpoint to the real climate device."""
        if self._updating_from_control:
            return

        real_mode = self._desired_real_mode()
        # Record the wrapper's intent so hvac_action can surface IDLE for
        # deliberate-OFF (AUTO + OFF inside the comfort band) without
        # re-running _desired_real_mode (which has timer side-effects).
        self._unit_command = real_mode

        real_state = self.hass.states.get(self._real_climate_id)
        if real_state is None:
            return

        current_mode = real_state.state

        # OFF reaches here in two cases now: (a) the user commanded smart
        # climate OFF, and (b) AUTO is in deliberate-OFF — the wrapper
        # provides the idle state Midea inverters can't (their HEAT/COOL
        # modes hold a min-frequency floor instead of truly idling).
        # Forward the OFF; only send the command if the real device isn't
        # already off.
        if real_mode == HVACMode.OFF:
            if current_mode != HVACMode.OFF.value:
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": self._real_climate_id, "hvac_mode": HVACMode.OFF.value},
                    blocking=False,
                )
            return

        # In AUTO mode, the modulating real device is asked to settle on the
        # comfort-band midpoint; mode selection (HEAT vs COOL) is what
        # determines whether the room is being warmed or cooled towards it.
        low: float | None = None
        high: float | None = None
        if self._hvac_mode == HVACMode.AUTO:
            low, high = self._active_range()
            target_temp = (low + high) / 2.0
        else:
            target_temp = self._target_temperature

        # Real thermostats only accept whole-integer setpoints, and their
        # advertised ``target_temp_step`` cannot be trusted to reflect that.
        # Always round to an integer so the device stores exactly what we
        # send — otherwise it silently rounds (e.g. 22.5 → 22) and every
        # inside-sensor update drives another set_temperature call trying to
        # "correct" the mismatch, producing target flutter.
        # Round directionally (up for HEAT, down for COOL) and stay inside
        # the comfort band, falling back to the opposite-direction integer
        # bound when the band is too narrow for the directional rounding to
        # land inside it (e.g. mid 21.25 in a 21–21.5 band: ceil=22 is
        # above high → use floor(high)=21 instead).
        if real_mode == HVACMode.HEAT:
            target_temp = float(math.ceil(target_temp))
            if high is not None and target_temp > high:
                target_temp = float(math.floor(high))
        else:  # COOL
            target_temp = float(math.floor(target_temp))
            if low is not None and target_temp < low:
                target_temp = float(math.ceil(low))

        current_temp = real_state.attributes.get("temperature")

        # Tolerance must cover the full integer step so the next sensor
        # update doesn't trigger a spurious resend when the device is
        # already holding the exact value we sent.
        mode_changed = current_mode != real_mode.value
        temp_changed = current_temp is None or abs(float(current_temp) - target_temp) >= 0.5

        if not (mode_changed or temp_changed):
            return

        service_data: dict[str, Any] = {
            "entity_id": self._real_climate_id,
            "temperature": target_temp,
            "hvac_mode": real_mode.value,
        }
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            service_data,
            blocking=False,
        )

    # ------------------------------------------------------------------
    # ClimateEntity control methods
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode."""
        self._updating_from_control = True
        old_mode = self._hvac_mode
        self._hvac_mode = hvac_mode

        # Leaving AUTO clears the sticky AUTO state so the next AUTO entry
        # picks a fresh mode from current temperatures rather than reusing
        # a stale commitment.
        if hvac_mode != HVACMode.AUTO:
            self._auto_mode = None
            self._pending_flip_since = None

        if hvac_mode == HVACMode.OFF:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self._real_climate_id, "hvac_mode": HVACMode.OFF.value},
                blocking=False,
            )
        elif hvac_mode == HVACMode.AUTO:
            # Entering AUTO mode – reload the active preset's range
            if self._preset_mode != PRESET_NONE:
                low, high = self._active_range()
                self._target_temp_low = low
                self._target_temp_high = high
            await self._async_sync_real_climate()
        else:
            # HEAT or COOL: use single target temperature
            if old_mode == HVACMode.AUTO:
                # Carry the preset midpoint over as the single-point target
                self._target_temperature = self._preset_midpoint()
            await self._async_sync_real_climate()

        self.async_write_ha_state()
        self._updating_from_control = False

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the active preset, updating the temperature range accordingly."""
        self._updating_from_control = True
        self._preset_mode = preset_mode

        if preset_mode != PRESET_NONE:
            low, high = self._active_range()
            self._target_temp_low = low
            self._target_temp_high = high
            if self._hvac_mode == HVACMode.AUTO:
                await self._async_sync_real_climate()

        self.async_write_ha_state()
        self._updating_from_control = False

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature.

        In AUTO mode this accepts ``target_temp_low`` / ``target_temp_high``
        to update the comfort band.  In HEAT / COOL mode it accepts a single
        ``temperature`` value.  Either way the preset is cleared to NONE
        (manual mode) because the user is directly editing setpoints.
        """
        self._updating_from_control = True

        # Any manual temperature edit exits preset mode
        self._preset_mode = PRESET_NONE

        if ATTR_TARGET_TEMP_LOW in kwargs or ATTR_TARGET_TEMP_HIGH in kwargs:
            if ATTR_TARGET_TEMP_LOW in kwargs:
                new_low = float(kwargs[ATTR_TARGET_TEMP_LOW])
                self._target_temp_low = new_low
            if ATTR_TARGET_TEMP_HIGH in kwargs:
                new_high = float(kwargs[ATTR_TARGET_TEMP_HIGH])
                self._target_temp_high = new_high

            # Enforce minimum separation between low and high
            if self._target_temp_high - self._target_temp_low < MIN_TEMP_DIFF:
                if ATTR_TARGET_TEMP_HIGH in kwargs:
                    self._target_temp_low = self._target_temp_high - MIN_TEMP_DIFF
                else:
                    self._target_temp_high = self._target_temp_low + MIN_TEMP_DIFF

            if self._hvac_mode == HVACMode.AUTO:
                await self._async_sync_real_climate()

        elif ATTR_TEMPERATURE in kwargs:
            temp = float(kwargs[ATTR_TEMPERATURE])
            self._target_temperature = temp
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self._real_climate_id, "temperature": temp},
                blocking=False,
            )

        self.async_write_ha_state()
        self._updating_from_control = False

    async def async_turn_on(self) -> None:
        """Turn on by entering AUTO mode."""
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        """Turn off."""
        await self.async_set_hvac_mode(HVACMode.OFF)
