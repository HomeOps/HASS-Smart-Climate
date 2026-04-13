"""Smart Climate - EcoBee-like thermostat platform for Home Assistant.

Wraps a physical climate device and adds:
- Comfort presets (Home, Sleep, Away) with configurable temperature ranges
- Automatic heat/cool switching based on inside temperature vs setpoint range
- Outside temperature awareness for smarter mode decisions
- Manual mode for direct control
"""
from __future__ import annotations

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
    INSIDE_DEADBAND,
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
    and automatically switches the underlying climate device between HEAT and
    COOL as the inside temperature drifts outside the comfort band.  An
    optional outside temperature sensor further refines the decision when the
    inside temperature is already within range.

    Four presets are supported:
      - Home  – default daytime comfort range
      - Sleep – cooler nighttime comfort range
      - Away  – wider range for unoccupied periods
      - None  – manual/direct control (no preset enforced)
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

        # Guard flags – prevent feedback loops between control calls and state
        # changes arriving from the real climate / sensors.
        self._updating_from_control: bool = False
        self._updating_from_real: bool = False

        # Tracks the last HEAT/COOL mode sent to the real device; used by
        # _desired_real_mode to apply hysteresis and avoid rapid cycling.
        self._last_real_mode: HVACMode | None = None

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
        """Return the current running HVAC action (mirrored from real device)."""
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

        # Subscribe to state changes on all tracked entities.  This is done
        # *after* the initial sync so that stale state-change events from the
        # real climate device (whose setpoint may not yet match the restored
        # preset) do not trigger false "external change" detection and reset
        # the preset to NONE.
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

        # Seed _last_real_mode so hysteresis works correctly from the first update
        try:
            current_mode = HVACMode(state.state)
            if current_mode in (HVACMode.HEAT, HVACMode.COOL):
                self._last_real_mode = current_mode
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
        """Handle state change of the wrapped climate device."""
        if self._updating_from_control:
            return

        self._updating_from_real = True
        needs_write = False

        state = self.hass.states.get(self._real_climate_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._updating_from_real = False
            return

        # Mirror the HVAC action (heating / cooling / idle / off …)
        action_str = state.attributes.get("hvac_action")
        if action_str:
            try:
                new_action = HVACAction(action_str)
                if self._hvac_action != new_action:
                    self._hvac_action = new_action
                    needs_write = True
            except ValueError:
                pass

        # Detect when the real device's temperature setpoint was changed
        # externally (e.g., via a physical remote) and exit preset mode.
        if (
            self._hvac_mode == HVACMode.AUTO
            and self._preset_mode != PRESET_NONE
        ):
            real_target = state.attributes.get("temperature")
            if real_target is not None:
                expected = self._expected_real_target()
                if abs(float(real_target) - expected) > 0.5:
                    _LOGGER.debug(
                        "Real climate setpoint changed externally "
                        "(expected %.1f, got %.1f) – switching to manual",
                        expected,
                        float(real_target),
                    )
                    self._preset_mode = PRESET_NONE
                    needs_write = True

        if needs_write:
            self.async_write_ha_state()

        self._updating_from_real = False

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

    def _desired_real_mode(self) -> HVACMode:
        """Determine which mode (HEAT/COOL/OFF) the real device should be in.

        Decision logic mirrors the ESPHome smart_climate component:
        1. If inside temp is below the low setpoint → heat
        2. If inside temp is at or above high - INSIDE_DEADBAND → cool.
           This engages cooling *before* the temperature exits the comfort
           band, giving the system time to react and preventing overshoot
           (analogous to the cool target using high - 1).
        3. If inside temp is within range, apply a hysteresis deadband around
           the midpoint to prevent rapid mode cycling:
             - If last mode was HEAT, stay in HEAT until inside > mid + INSIDE_DEADBAND
             - If last mode was COOL, stay in COOL until inside < mid - INSIDE_DEADBAND
        4. When no prior mode (or temp has crossed the deadband boundary), use
           outside temperature as a tiebreaker; fall back to relative position.
        """
        if self._hvac_mode == HVACMode.OFF:
            return HVACMode.OFF
        if self._hvac_mode in (HVACMode.HEAT, HVACMode.COOL):
            return self._hvac_mode

        # AUTO mode
        if self._current_temperature is None or math.isnan(self._current_temperature):
            # No sensor reading – keep real device unchanged
            return HVACMode.AUTO

        inside = self._current_temperature
        low, high = self._active_range()
        mid = (low + high) / 2.0

        if inside < low:
            return HVACMode.HEAT
        if inside >= high - INSIDE_DEADBAND:
            return HVACMode.COOL

        # Inside the comfort band – apply hysteresis around the midpoint to
        # prevent rapid HEAT ↔ COOL cycling when temperature hovers near centre.
        if self._last_real_mode == HVACMode.HEAT and inside <= mid + INSIDE_DEADBAND:
            return HVACMode.HEAT
        if self._last_real_mode == HVACMode.COOL and inside >= mid - INSIDE_DEADBAND:
            return HVACMode.COOL

        # No prior mode, or temperature has clearly crossed the deadband boundary.
        # Use outside temperature as a tiebreaker when available.
        if (
            self._outside_temperature is not None
            and not math.isnan(self._outside_temperature)
        ):
            return HVACMode.HEAT if self._outside_temperature < mid else HVACMode.COOL

        # Fallback: position within the band
        return HVACMode.HEAT if inside < mid else HVACMode.COOL

    # ------------------------------------------------------------------
    # Real-climate synchronisation
    # ------------------------------------------------------------------

    def _expected_real_target(self) -> float:
        """Return the temperature the real device should currently be set to.

        In AUTO mode the target depends on whether we are heating or cooling:
        HEAT targets the *low* setpoint and COOL targets one below the *high*
        setpoint (high - 1).  Using high - 1 prevents real devices that only
        accept integer setpoints from overshooting to high + 0.5 due to their
        own internal hysteresis, which would push the effective upper limit one
        degree above the configured band.  The result is capped at low so it
        never falls below the heating target for very narrow bands.
        """
        if self._hvac_mode != HVACMode.AUTO:
            return self._target_temperature
        low, high = self._active_range()
        if self._last_real_mode == HVACMode.HEAT:
            return low
        if self._last_real_mode == HVACMode.COOL:
            return max(low, high - 1)
        return self._preset_midpoint()

    async def _async_sync_real_climate(self) -> None:
        """Push the desired mode and setpoint to the real climate device."""
        if self._updating_from_real or self._updating_from_control:
            return

        real_mode = self._desired_real_mode()
        # Track the decided HEAT/COOL mode so subsequent calls can apply
        # hysteresis and avoid rapid cycling near the midpoint.
        if real_mode in (HVACMode.HEAT, HVACMode.COOL):
            self._last_real_mode = real_mode

        # In AUTO mode, target the *low* setpoint when heating and the *high*
        # setpoint when cooling.  This prevents the real device from actively
        # heating/cooling into the comfort band's interior and eliminates the
        # rapid temperature oscillation that occurs when both modes chase the
        # same midpoint target.
        if self._hvac_mode == HVACMode.AUTO:
            low, high = self._active_range()
            if real_mode == HVACMode.HEAT:
                target_temp = low
            elif real_mode == HVACMode.COOL:
                target_temp = max(low, high - 1)
            else:
                target_temp = self._preset_midpoint()
        else:
            target_temp = self._target_temperature

        real_state = self.hass.states.get(self._real_climate_id)
        if real_state is None:
            return

        current_mode = real_state.state
        current_temp = real_state.attributes.get("temperature")

        mode_changed = current_mode != real_mode.value
        temp_changed = current_temp is None or abs(float(current_temp) - target_temp) > 0.1

        if not (mode_changed or temp_changed):
            return

        service_data: dict[str, Any] = {
            "entity_id": self._real_climate_id,
            "temperature": target_temp,
        }
        if real_mode == HVACMode.OFF:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self._real_climate_id, "hvac_mode": HVACMode.OFF.value},
                blocking=False,
            )
        else:
            service_data["hvac_mode"] = real_mode.value
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
