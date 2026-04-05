# HASS-Smart-Climate

A HACS-compatible custom component for Home Assistant that turns any climate device into an **EcoBee-like smart thermostat** with comfort presets and automatic heat/cool switching.

## Features

- **Comfort Presets** – Home, Sleep, Away, and Manual modes with independently configurable temperature ranges (low/high setpoints).
- **Auto Heat/Cool Switching** – In AUTO mode the thermostat automatically switches the real device between heating and cooling as the inside temperature drifts outside the comfort band.
- **Outside Temperature Awareness** – When an optional outdoor sensor is configured, it is used as an additional signal for smarter mode decisions when the inside temperature is already within range.
- **State Restoration** – Thermostat state (mode, preset, setpoints) survives Home Assistant restarts.
- **HACS Compatible** – Install and update via the Home Assistant Community Store.
- **UI Configuration** – Set up via the Home Assistant Integrations UI; adjust preset temperatures via the Options flow.

## Installation

### Via HACS (recommended)

1. Open **HACS → Integrations**.
2. Click the three-dot menu (⋮) → **Custom repositories**.
3. Add `https://github.com/HomeOps/HASS-Smart-Climate` with category **Integration**.
4. Find **Smart Climate** in the HACS store and click **Download**.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/smart_climate/` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

### Add the integration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Smart Climate**.
3. Fill in the form:
   | Field | Required | Description |
   |-------|----------|-------------|
   | **Thermostat Name** | ✅ | Friendly name for the new thermostat entity |
   | **Climate Device** | ✅ | The real/dumb climate entity to control (e.g. `climate.heatpump`) |
   | **Inside Temperature Sensor** | ✅ | Sensor entity for the indoor temperature |
   | **Outside Temperature Sensor** | ❌ | Optional sensor for smarter auto mode decisions |

### Configure preset temperatures

After setup, click **Configure** on the integration card (or go to **Options**) to adjust the comfort temperature ranges for each preset:

| Preset | Default Range |
|--------|--------------|
| **Home** | 21 °C – 24 °C |
| **Sleep** | 19 °C – 22 °C |
| **Away** | 18 °C – 26 °C |

## How It Works

### AUTO mode (EcoBee-like behaviour)

When the thermostat is in AUTO mode and a comfort preset is active:

1. If inside temperature **< low setpoint** → switch real device to **HEAT**
2. If inside temperature **> high setpoint** → switch real device to **COOL**
3. If inside temperature is **within range**:
   - With an outside sensor: warm outside → **COOL**; cold outside → **HEAT**
   - Without an outside sensor: above midpoint → **COOL**; below midpoint → **HEAT**

The real device's setpoint is always kept at the **midpoint** of the active preset range.

### Manual mode (PRESET_NONE)

- The preset is cleared whenever the user directly adjusts setpoints.
- All temperature changes are forwarded directly to the real device.

### External changes

If the real device's setpoint is changed externally (e.g., via a physical remote or another automation) by more than 0.5 °C, the smart thermostat automatically switches to **Manual** mode to avoid conflicting with the external change.

## Inspiration

This integration is inspired by the [ESPHome-Midea-XYE `smart_climate` component](https://github.com/HomeOps/ESPHome-Midea-XYE), which implements the same EcoBee-like logic as an ESPHome external component for ESP8266/ESP32 devices.

## License

See [LICENSE](LICENSE) for details.
