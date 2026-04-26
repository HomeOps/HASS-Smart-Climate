# HASS-Smart-Climate

A HACS-compatible custom component for Home Assistant that turns any climate device into an **EcoBee-like smart thermostat** with comfort presets and automatic heat/cool switching.

## Features

- **Comfort Presets** – Home, Sleep, Away, and Manual modes with independently configurable temperature ranges (low/high setpoints).
- **Auto Heat/Cool Switching** – Sticky direction commitment with two-tier flip logic (immediate fast-flip on band-edge violations + 30-min dwell-flip for sustained excursions).
- **In-band COOL hysteresis with deliberate-OFF** – Stops the inverter min-frequency floor from pumping unwanted cold air; COOL pulses run from `mid + 0.75 °C` down to `mid`, OFF in between.  See [`docs/state-machine.md`](docs/state-machine.md).
- **Problem detection** – `problems` attribute lists detected issues (sensor unavailable / stale, sustained out-of-band, short-cycling) for dashboards and notifications.
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
   | **Outside Temperature Sensor** | ❌ | Optional sensor — tracked for display and future use (free-cooling detection, forecast preconditioning).  No longer consulted for the AUTO direction pick (see [v3.0.2 changelog](CHANGELOG.md)). |

### Configure preset temperatures

After setup, click **Configure** on the integration card (or go to **Options**) to adjust the comfort temperature ranges for each preset:

| Preset | Default Range |
|--------|--------------|
| **Home** | 21 °C – 24 °C |
| **Sleep** | 19 °C – 22 °C |
| **Away** | 18 °C – 26 °C |

## How It Works

### AUTO mode

The thermostat maintains a sticky **direction commitment** (HEAT or COOL) that
flips on demand mismatch, and derives a per-tick **unit command** (HEAT, COOL,
or OFF) from that direction plus the current inside temperature.

- **Initial pick** when entering AUTO: `inside < midpoint → HEAT, otherwise COOL`.
- **HEAT committed** runs HEAT continuously; the unit modulates to a true
  compressor idle when no demand.
- **COOL committed** uses **hysteresis around the band**: starts cooling when
  current rises above `mid + COOL_RESTART_OFFSET` (default 22.75 °C for the
  21-23 home preset), stops at `mid` (22 °C).  The 0.25 °C between the
  restart threshold and the high edge is response-lag headroom — by the time
  current would otherwise hit the high edge, COOL flow is already pulling
  the room down.  `hvac_action` reads `idle` while in deliberate-OFF.
- **Direction flips** on demand mismatch happen via two mechanisms: an
  immediate **fast-flip** when inside is past the wrong band edge, and a
  30-min **dwell-flip** for sustained excursions in band.

📐 **See [`docs/state-machine.md`](docs/state-machine.md) for the full state
diagrams, decision flowchart, and design rationale.**

### Problem detection

The wrapper exposes a `problems` extra-state-attribute — a list of detected
issues (empty list `[]` when healthy).  Conditions checked: inside-sensor
unavailable / stale, real-device unavailable, sustained out-of-band in AUTO,
COOL short-cycling.  Use in templates / dashboards:

```jinja
{% if state_attr('climate.smart_climate', 'problems') %}
  ⚠ {{ state_attr('climate.smart_climate', 'problems') | join('; ') }}
{% endif %}
```

### Manual mode (PRESET_NONE)

- The preset is cleared whenever the user directly adjusts setpoints.
- All temperature changes are forwarded directly to the real device.

### External changes

If the real device's setpoint is changed externally (e.g., via a physical
remote or another automation) by more than 0.5 °C, the smart thermostat
automatically switches to **Manual** mode to avoid conflicting with the
external change.

## Inspiration

This integration is inspired by the [ESPHome-Midea-XYE `smart_climate` component](https://github.com/HomeOps/ESPHome-Midea-XYE), which implements the same EcoBee-like logic as an ESPHome external component for ESP8266/ESP32 devices.

## License

See [LICENSE](LICENSE) for details.
