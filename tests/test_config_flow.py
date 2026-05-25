"""Tests for the Smart Climate config / options flow."""
from __future__ import annotations

import pytest
import voluptuous as vol
from unittest.mock import MagicMock

from custom_components.smart_climate.config_flow import (
    SmartClimateConfigFlow,
    SmartClimateOptionsFlow,
)
from custom_components.smart_climate.const import (
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
    DEFAULT_HOME_MAX,
    DEFAULT_HOME_MIN,
)


REAL_CLIMATE_ID = "climate.real_ac"
INSIDE_SENSOR_ID = "sensor.whole_home_temperature"
SLEEP_SENSOR_ID = "sensor.upstairs_avg_temp"
AWAY_SENSOR_ID = "sensor.whole_home_temperature"
OUTSIDE_SENSOR_ID = "sensor.outside_temp"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _schema_keys(schema: vol.Schema) -> list[str]:
    """Return the field names (as strings) from a voluptuous schema."""
    return [str(k) for k in schema.schema.keys()]


def _schema_defaults(schema: vol.Schema) -> dict[str, object]:
    """Return {field name: default value} for fields that have a default."""
    out: dict[str, object] = {}
    for key in schema.schema.keys():
        # vol.Optional / vol.Required wrap the key string; default callable
        # exists on Marker.  Calling default() with no default raises.
        if hasattr(key, "default") and key.default is not vol.UNDEFINED:
            try:
                out[str(key)] = key.default()
            except (TypeError, vol.Invalid):
                pass
    return out


def _schema_suggested(schema: vol.Schema) -> dict[str, object]:
    """Return {field: suggested_value} from each marker's description."""
    out: dict[str, object] = {}
    for key in schema.schema.keys():
        desc = getattr(key, "description", None)
        if isinstance(desc, dict) and "suggested_value" in desc:
            out[str(key)] = desc["suggested_value"]
    return out


def _make_entry(data: dict, options: dict | None = None) -> MagicMock:
    """Build a mock ConfigEntry."""
    entry = MagicMock()
    entry.data = data
    entry.options = options or {}
    return entry


async def _build_options_schema(entry: MagicMock) -> vol.Schema:
    """Invoke async_step_init's form path and return the data_schema."""
    flow = SmartClimateOptionsFlow(entry)
    captured: dict = {}

    def _capture(*, step_id, data_schema, **_):
        captured["schema"] = data_schema
        return {"step_id": step_id}

    flow.async_show_form = _capture  # type: ignore[assignment]
    await flow.async_step_init(None)
    return captured["schema"]


async def _build_user_schema() -> vol.Schema:
    """Invoke async_step_user's form path and return the data_schema."""
    flow = SmartClimateConfigFlow()
    flow.hass = MagicMock()
    captured: dict = {}

    def _capture(*, step_id, data_schema, **_):
        captured["schema"] = data_schema
        return {"step_id": step_id}

    flow.async_show_form = _capture  # type: ignore[assignment]
    await flow.async_step_user(None)
    return captured["schema"]


# ---------------------------------------------------------------------------
# Initial config flow — sensor fields present
# ---------------------------------------------------------------------------

class TestUserStepSchema:
    """Initial setup (config flow) must expose all per-preset sensors."""

    @pytest.mark.asyncio
    async def test_user_step_includes_all_inside_sensors(self):
        schema = await _build_user_schema()
        keys = _schema_keys(schema)
        assert CONF_INSIDE_SENSOR in keys
        assert CONF_INSIDE_SENSOR_SLEEP in keys
        assert CONF_INSIDE_SENSOR_AWAY in keys
        assert CONF_OUTSIDE_SENSOR in keys


# ---------------------------------------------------------------------------
# Options flow — the missing-UI bug
# ---------------------------------------------------------------------------

class TestOptionsFlowSensors:
    """Existing installs must reach the per-preset sensors via Configure."""

    def _base_entry(self, options: dict | None = None) -> MagicMock:
        return _make_entry(
            data={
                CONF_REAL_CLIMATE: REAL_CLIMATE_ID,
                CONF_INSIDE_SENSOR: INSIDE_SENSOR_ID,
            },
            options=options,
        )

    @pytest.mark.asyncio
    async def test_options_flow_exposes_inside_sensor(self):
        schema = await _build_options_schema(self._base_entry())
        assert CONF_INSIDE_SENSOR in _schema_keys(schema)

    @pytest.mark.asyncio
    async def test_options_flow_exposes_sleep_sensor(self):
        """Bug: opening Configure on an existing v6.0.0 install showed no
        Sleep sensor field.  Options flow must list every per-preset sensor
        so users without sleep/away set up via initial flow can add them."""
        schema = await _build_options_schema(self._base_entry())
        assert CONF_INSIDE_SENSOR_SLEEP in _schema_keys(schema)

    @pytest.mark.asyncio
    async def test_options_flow_exposes_away_sensor(self):
        schema = await _build_options_schema(self._base_entry())
        assert CONF_INSIDE_SENSOR_AWAY in _schema_keys(schema)

    @pytest.mark.asyncio
    async def test_options_flow_exposes_outside_sensor(self):
        schema = await _build_options_schema(self._base_entry())
        assert CONF_OUTSIDE_SENSOR in _schema_keys(schema)

    @pytest.mark.asyncio
    async def test_options_flow_keeps_preset_temp_fields(self):
        """The fix must not regress the existing preset-range editing."""
        schema = await _build_options_schema(self._base_entry())
        keys = _schema_keys(schema)
        for expected in (
            CONF_HOME_MIN, CONF_HOME_MAX,
            CONF_SLEEP_MIN, CONF_SLEEP_MAX,
            CONF_AWAY_MIN, CONF_AWAY_MAX,
        ):
            assert expected in keys, f"missing {expected}"

    # ------------------------------------------------------------------
    # Defaults / suggested values come from data ∪ options
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_inside_sensor_default_from_entry_data(self):
        """Home sensor (Required) shows the original initial-setup value."""
        schema = await _build_options_schema(self._base_entry())
        defaults = _schema_defaults(schema)
        assert defaults.get(CONF_INSIDE_SENSOR) == INSIDE_SENSOR_ID

    @pytest.mark.asyncio
    async def test_options_override_data_for_inside_sensor_default(self):
        """If options already changed the Home sensor, that value wins."""
        new_home = "sensor.downstairs_avg_temp"
        entry = self._base_entry(options={CONF_INSIDE_SENSOR: new_home})
        schema = await _build_options_schema(entry)
        defaults = _schema_defaults(schema)
        assert defaults.get(CONF_INSIDE_SENSOR) == new_home

    @pytest.mark.asyncio
    async def test_sleep_suggested_when_already_configured(self):
        """When the Sleep sensor is set, the form pre-fills it."""
        entry = self._base_entry(options={CONF_INSIDE_SENSOR_SLEEP: SLEEP_SENSOR_ID})
        schema = await _build_options_schema(entry)
        suggested = _schema_suggested(schema)
        assert suggested.get(CONF_INSIDE_SENSOR_SLEEP) == SLEEP_SENSOR_ID

    @pytest.mark.asyncio
    async def test_sleep_has_no_default_when_unset(self):
        """No previous value → no default, so the field renders empty.

        Setting default=None for an EntitySelector would render an invalid
        pre-selection, so the absence of a default is the correct state.
        """
        schema = await _build_options_schema(self._base_entry())
        defaults = _schema_defaults(schema)
        suggested = _schema_suggested(schema)
        assert CONF_INSIDE_SENSOR_SLEEP not in defaults
        assert CONF_INSIDE_SENSOR_SLEEP not in suggested

    @pytest.mark.asyncio
    async def test_preset_temp_defaults_preserved(self):
        """Preset min/max defaults still come from options, falling back
        to const defaults — the original options-flow behavior."""
        entry = self._base_entry(options={CONF_HOME_MIN: 20.5, CONF_HOME_MAX: 24.5})
        schema = await _build_options_schema(entry)
        defaults = _schema_defaults(schema)
        assert defaults.get(CONF_HOME_MIN) == 20.5
        assert defaults.get(CONF_HOME_MAX) == 24.5

    @pytest.mark.asyncio
    async def test_preset_temp_falls_back_to_const_defaults(self):
        schema = await _build_options_schema(self._base_entry())
        defaults = _schema_defaults(schema)
        assert defaults.get(CONF_HOME_MIN) == DEFAULT_HOME_MIN
        assert defaults.get(CONF_HOME_MAX) == DEFAULT_HOME_MAX

    @pytest.mark.asyncio
    async def test_field_order_sensors_before_temperatures(self):
        """Sensors must appear before preset temperatures so they aren't
        buried below a wall of numeric inputs."""
        schema = await _build_options_schema(self._base_entry())
        keys = _schema_keys(schema)
        sensor_idx = keys.index(CONF_INSIDE_SENSOR)
        temp_idx = keys.index(CONF_HOME_MIN)
        assert sensor_idx < temp_idx
