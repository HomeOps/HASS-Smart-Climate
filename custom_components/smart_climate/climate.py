"""Smart Climate - EcoBee-like thermostat platform for Home Assistant.

Wraps a physical climate device and adds:
- Comfort presets (Home, Sleep, Away) with configurable temperature ranges
- Automatic heat/cool switching based on inside temperature vs setpoint range
- Outside temperature awareness for smarter mode decisions
- Manual mode for direct control
"""
from __future__ import annotations

import collections
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
    COMMAND_GRACE_SECONDS,
    COOL_RESTART_OFFSET,
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
    OUT_OF_BAND_ALERT_MINUTES,
    SENSOR_STALE_MINUTES,
    SHORT_CYCLE_THRESHOLD_PER_H,
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

        # Timestamp of the last `_unit_command` change.  Used by the
        # desync detector to allow the real device a grace period to
        # transition into the commanded state.  Don't update this on
        # every sync — only when the command itself changes.
        self._unit_command_at: datetime.datetime | None = None

        # Problem-detection state (surfaced via `problems` attribute).
        # Updated by sensor / sync callbacks; checked at attribute read.
        self._out_of_band_since: datetime.datetime | None = None
        self._cool_start_times: collections.deque[datetime.datetime] = (
            collections.deque(maxlen=20)
        )

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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Surface diagnostic + persisted state alongside HA's standard ones.

        - ``problems`` is a list of detected issues (empty when healthy).
        - ``auto_mode_committed`` is the sticky direction (`heat`/`cool`)
          the wrapper has chosen in AUTO mode.  Persisted via RestoreEntity
          so it survives HA restarts; the wrapper restores it in
          `async_added_to_hass` to skip the initial pick on potentially
          glitchy post-restart sensor data.
        - ``last_unit_command`` is the wrapper's last command to the
          real device (`heat`/`cool`/`off`).  Persisted for the same
          reason — keeps COOL hysteresis state continuous across
          restarts.
        """
        return {
            "problems": self._detect_problems(),
            "auto_mode_committed": (
                self._auto_mode.value if self._auto_mode else None
            ),
            "last_unit_command": (
                self._unit_command.value if self._unit_command else None
            ),
        }

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

            # Restore the AUTO state machine: the sticky committed
            # direction and the last unit command.  Without this, every
            # HA restart re-runs the initial pick — and on 2026-04-26
            # that picked HEAT against a glitchy startup sensor reading
            # (Z-Wave aggregator briefly reported 21.66 °C while
            # sensors were re-initialising), committing the wrapper to
            # 30 minutes of wrong-direction heating.  Persisting these
            # across restarts keeps the state machine stable.
            committed = attrs.get("auto_mode_committed")
            if committed:
                try:
                    self._auto_mode = HVACMode(committed)
                except ValueError:
                    self._auto_mode = None
            last_cmd = attrs.get("last_unit_command")
            if last_cmd:
                try:
                    self._unit_command = HVACMode(last_cmd)
                except ValueError:
                    self._unit_command = None

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

        # Track sustained out-of-band excursions for problem detection.
        # Only meaningful in AUTO mode — manual HEAT/COOL/OFF is on the
        # user's terms.
        if self._hvac_mode == HVACMode.AUTO:
            low, high = self._active_range()
            if new_temp < low or new_temp > high:
                if self._out_of_band_since is None:
                    self._out_of_band_since = self._now()
            else:
                self._out_of_band_since = None
        else:
            self._out_of_band_since = None

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

    def _detect_problems(self) -> list[str]:
        """Return a list of detected issues, empty when healthy.

        Checks (in order):
        - inside-sensor unavailable / unknown / missing
        - inside-sensor stale (no update for SENSOR_STALE_MINUTES)
        - real climate device unavailable
        - sustained out-of-band temperature in AUTO
        - COOL short-cycling (more than SHORT_CYCLE_THRESHOLD_PER_H starts/h)

        Each problem is a short string code with optional context, e.g.
        ``out_of_band:42min``, ``short_cycle:8/h``, ``sensor_stale:18min``.
        """
        problems: list[str] = []
        now = self._now()

        # Inside sensor health.
        inside_state = self.hass.states.get(self._inside_sensor_id)
        if inside_state is None:
            problems.append("inside_sensor_missing")
        elif inside_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
            problems.append(f"inside_sensor_{inside_state.state}")
        else:
            try:
                last = inside_state.last_updated
                if last.tzinfo is None:
                    last = last.replace(tzinfo=datetime.timezone.utc)
                stale_min = (now - last).total_seconds() / 60.0
                if stale_min > SENSOR_STALE_MINUTES:
                    problems.append(f"sensor_stale:{int(stale_min)}min")
            except (AttributeError, TypeError):
                pass

        # Real climate device health.
        real_state = self.hass.states.get(self._real_climate_id)
        if real_state is None:
            problems.append("real_climate_missing")
        elif real_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
            problems.append("real_climate_unavailable")

        # Sustained out-of-band in AUTO.  AUTO that can't keep the room
        # in band over half-hour windows means the unit is undersized,
        # blocked, or fighting an unmet load — the user should know.
        if (
            self._hvac_mode == HVACMode.AUTO
            and self._out_of_band_since is not None
        ):
            duration = (now - self._out_of_band_since).total_seconds() / 60.0
            if duration > OUT_OF_BAND_ALERT_MINUTES:
                problems.append(f"out_of_band:{int(duration)}min")

        # Short cycling.  Count COOL starts in the trailing hour; if it's
        # over the threshold the wrapper is flicking the compressor too
        # hard, likely from sensor jitter near the band edge.
        one_hour_ago = now - datetime.timedelta(hours=1)
        recent = sum(1 for t in self._cool_start_times if t > one_hour_ago)
        if recent > SHORT_CYCLE_THRESHOLD_PER_H:
            problems.append(f"short_cycle:{recent}/h")

        # Command desync.  We track the wrapper's last commanded state
        # (`_unit_command`) and the timestamp of the last *change*.  If
        # more than COMMAND_GRACE_SECONDS have passed since the change
        # AND the real device's state still doesn't match what the
        # wrapper believes it commanded, surface that — silently
        # diverging state is the failure mode that produced the
        # 2026-04-26 ghost-HEAT incident.  We compare against
        # `_unit_command.value` (e.g. "cool") which is what the real
        # device's state should read.  The check skips when the real
        # device is unavailable (already covered by another problem
        # code) or when no command has been sent yet.
        if (
            self._unit_command is not None
            and self._unit_command_at is not None
            and real_state is not None
            and real_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, "")
        ):
            elapsed = (now - self._unit_command_at).total_seconds()
            if elapsed > COMMAND_GRACE_SECONDS:
                expected = self._unit_command.value
                actual = real_state.state
                if expected != actual:
                    problems.append(f"command_desync:want={expected}_got={actual}")

        return problems

    def _desired_real_mode(self) -> HVACMode:
        """Determine which mode (HEAT/COOL/OFF) the real device should be in.

        For non-AUTO modes the user's choice is passed through.

        In AUTO mode the wrapper maintains a sticky **committed direction**
        (HEAT or COOL) using v2.0.0's FLIP_DWELL/FLIP_MARGIN logic — same
        as before.  The **unit command** sent to the real device is then
        derived asymmetrically by committed direction:

        **Committed HEAT** — return HEAT unconditionally (v2.0.0 contract
        unchanged).  The Midea unit modulates its compressor down to true
        idle in HEAT when the room is at setpoint, so continuous HEAT at
        min modulation costs less than OFF/ON cycling.

        **Committed COOL** — narrow, surgical change vs. v2.0.0, with
        hysteresis above the band midpoint:
        - currently OFF & `current > mid + COOL_RESTART_OFFSET`  →  COOL  (start)
        - currently COOL & `current > mid`                       →  COOL  (keep cooling)
        - currently COOL & `current ≤ mid`                       →  OFF   (stop at mid)
        - currently OFF & `current ≤ mid + COOL_RESTART_OFFSET`  →  OFF
        - `current > high`                                       →  COOL  (above-band, always)
        - `current < low`                                        →  COOL  (v2.0.0 wrong-side;
                                                                   FLIP_DWELL flips to HEAT)

        The hysteresis (start at `mid + COOL_RESTART_OFFSET`, stop at
        `mid`) prevents short-cycling at the band edge.  With the default
        home preset (21-23, mid=22, offset=0.75) cooling kicks in at
        22.75 and pulls down to 22 — leaving the upper 0.25 °C of the
        comfort band as headroom rather than the active operating zone.
        Tightens control vs. waiting for the high edge, at the cost of
        more frequent but still-meaningful compressor pulls.

        Why only COOL needs this: on this Midea unit the COOL mode holds
        a minimum-frequency floor and keeps pushing 12-14 °C supply air
        into rooms already in band — diagnosed empirically 2026-04-25.
        HEAT does not have this defect (refrigerant migrates outdoors
        when stopped, no slug-on-restart risk), and COOL outside the band
        still has real demand to chase.  The wrong-side COOL case (below
        low) is rare and the existing 30-min dwell flip handles it.
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

        # Initial mode pick — inside-vs-midpoint, full stop.  Outside is
        # NOT consulted: empirically the outside sensor in this
        # deployment doesn't correlate with the building's thermodynamics
        # (solar gain, occupancy, internal sources dominate), and using
        # it caused the live HEAT-at-23 bug on 2026-04-26 where cool
        # outside drove a HEAT pick while the room sat at the band's
        # high edge.  FLIP_DWELL (30 min sustained excursion past
        # mid + FLIP_MARGIN) corrects any wrong pick.
        if self._auto_mode is None:
            self._auto_mode = HVACMode.HEAT if inside < mid else HVACMode.COOL
            self._pending_flip_since = None
            # fall through to the band-aware unit-command logic below

        # Fast-flip on band-edge violation: when the committed direction
        # is the *opposite* of what's needed AND the room is past the
        # band edge, flip immediately — skip FLIP_DWELL.
        #
        # The dwell timer (30 min sustained excursion past mid ± FLIP_MARGIN)
        # is sized for sensor jitter / natural drift around the midpoint.
        # That's the wrong filter for the case where committed=HEAT but
        # inside is already > high (or committed=COOL and inside < low):
        # there's no jitter explanation for that, only a wrong pick or
        # an external swing the dwell would take 30 min to correct.
        # During those 30 min the wrapper actively heats a hot room (or
        # cools a cold one), making the excursion worse.  Live bug
        # 2026-04-26 (PR #62 fixed the *initial* pick that caused it;
        # this is the belt-and-suspenders catch for any future scenario
        # that lands committed-direction at odds with the band).
        if (
            (self._auto_mode == HVACMode.HEAT and inside > high)
            or (self._auto_mode == HVACMode.COOL and inside < low)
        ):
            self._auto_mode = (
                HVACMode.COOL if self._auto_mode == HVACMode.HEAT else HVACMode.HEAT
            )
            self._pending_flip_since = None

        # Sticky direction-flip evaluation (unchanged from v2.0.0).  Wrong-
        # side / right-side bands have a deadzone in between (mid ± FLIP_MARGIN)
        # where the timer keeps running but is not reset, so sensor jitter
        # near the margin doesn't repeatedly cancel an in-progress flip.
        # This handles the in-band drift case the fast-flip above doesn't.
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

        # Unit command — asymmetric between committed directions.
        # HEAT: always HEAT (v2.0.0 contract unchanged; the unit modulates
        # to true idle in HEAT when no demand).
        # COOL: hysteresis above midpoint inside the band, v2.0.0 elsewhere.
        #   above high                              → COOL (above-band; v2.0.0)
        #   below low                               → COOL (wrong-side; v2.0.0)
        #   was COOL, current > mid                 → COOL (keep cooling to mid)
        #   was COOL, current ≤ mid                 → OFF (full pull achieved)
        #   was OFF, current > mid + RESTART_OFFSET → COOL (start, lead high edge)
        #   was OFF, current ≤ mid + RESTART_OFFSET → OFF
        # The restart offset (default 0.75 °C above mid) leads the high
        # edge so the compressor has time to ramp before current would
        # otherwise overshoot the band.  Stopping at midpoint gives each
        # start a meaningful pull, amortising start-up cost.
        if self._auto_mode == HVACMode.COOL:
            if inside > high or inside < low:
                return HVACMode.COOL
            # Inside the band: hysteresis keyed on the previous command.
            if self._unit_command == HVACMode.COOL:
                return HVACMode.COOL if inside > mid else HVACMode.OFF
            # Was OFF (or first sync) — restart well shy of the high edge.
            if inside > mid + COOL_RESTART_OFFSET:
                return HVACMode.COOL
            return HVACMode.OFF
        return HVACMode.HEAT

    # ------------------------------------------------------------------
    # Real-climate synchronisation
    # ------------------------------------------------------------------

    async def _async_sync_real_climate(self) -> None:
        """Push the desired mode and setpoint to the real climate device."""
        if self._updating_from_control:
            return

        real_mode = self._desired_real_mode()
        # Record COOL transitions for short-cycle detection.  A "start"
        # is OFF → COOL (or None → COOL on first sync).
        if (
            real_mode == HVACMode.COOL
            and self._unit_command != HVACMode.COOL
        ):
            self._cool_start_times.append(self._now())
        # Track command changes for desync detection (only when the
        # command actually changes — re-issuing the same command
        # shouldn't reset the grace window).
        if real_mode != self._unit_command:
            self._unit_command_at = self._now()
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

        # In AUTO mode, the setpoint sent to the real device is biased
        # toward the band edge that matches the committed direction —
        # NOT the band midpoint.  This leverages the unit's own ±0.5 °C
        # internal hysteresis to keep the room sitting near the edge,
        # which saves energy and matches occupant comfort better:
        #
        #   HEAT setpoint = low      → unit holds room at [low, low+0.5]
        #   COOL setpoint = high - 1 → unit holds room at [high-1.5, high-1]
        #
        # For default home preset (21-23):
        #   HEAT setpoint=21 → room ~21..21.5
        #   COOL setpoint=22 → room ~21.5..22
        #
        # The mid-targeting that v2.0.0 used was actively wasteful in
        # sunny PNW climates: HEAT mode at mid=22 would heat a 21.4 °C
        # room to 22 even though 21.4 is well within the comfort band.
        # Diagnosed empirically from 48 h of v3.1.x data showing two
        # 64-min daily HEAT pulses initiated at inside ~21.45 °C.
        low: float | None = None
        high: float | None = None
        if self._hvac_mode == HVACMode.AUTO:
            low, high = self._active_range()
            if real_mode == HVACMode.HEAT:
                target_temp = float(low)
            elif real_mode == HVACMode.COOL:
                target_temp = float(high) - 1.0
            else:
                # OFF — value is irrelevant (set_hvac_mode=off path); use
                # mid as a neutral fallback.
                target_temp = (low + high) / 2.0
        else:
            target_temp = self._target_temperature

        # Real thermostats only accept whole-integer setpoints, and their
        # advertised ``target_temp_step`` cannot be trusted to reflect that.
        # Always round to an integer so the device stores exactly what we
        # send — otherwise it silently rounds (e.g. 22.5 → 22) and every
        # inside-sensor update drives another set_temperature call trying to
        # "correct" the mismatch, producing target flutter.
        # Round directionally (up for HEAT, down for COOL).  With band-edge
        # setpoints the rounding usually lands on the integer band edge
        # itself, but for non-integer bands we still want to stay inside the
        # comfort band — fall back to the opposite-direction integer bound
        # when the directional rounding lands outside it.
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
