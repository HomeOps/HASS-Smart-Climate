# HASS-Smart-Climate Development Guide

## Project Overview

**HASS-Smart-Climate** is a HACS-compatible Home Assistant custom component that turns any climate device into an EcoBee-like smart thermostat with comfort presets and automatic heat/cool switching.

- **Language**: Python 3.11+
- **Framework**: Home Assistant custom component
- **Owner**: @ocalvo (Oscar Calvo)
- **Current Version**: 0.0.8 (managed by release-please)
- **Releases**: Published to GitHub Releases automatically via release-please workflow

### Key Features

- **Comfort Presets**: Home, Sleep, Away modes with configurable temperature ranges (low/high setpoints)
- **Auto Heat/Cool Switching**: In AUTO mode, automatically switches between heating and cooling based on inside temperature vs comfort band
- **Outside Temperature Awareness**: Optional outdoor sensor for smarter decisions when inside temperature is within range
- **State Restoration**: Thermostat state survives Home Assistant restarts
- **Configuration**: Via Home Assistant Integrations UI with preset temperature options flow

## Development

### Setup

No special setup required beyond having pytest installed. All dependencies are standard Home Assistant components.

```bash
# Install test dependencies
pip install -r requirements_test.txt
```

### Testing

The project uses pytest with asyncio support (auto mode).

```bash
# Run all tests
pytest

# Run tests with verbose output
pytest -v

# Run a specific test file
pytest tests/test_climate.py

# Run tests matching a pattern
pytest -k "test_name_pattern"
```

All tests are in `tests/` and must be kept in sync with implementation changes.

## Code Structure

### Main Component Files

- **`custom_components/smart_climate/climate.py`**: Core thermostat logic
  - `SmartClimateEntity` class – main entity wrapping a real climate device
  - Auto heat/cool switching logic
  - Preset and state management
  
- **`custom_components/smart_climate/__init__.py`**: Component setup
  - Entry point for Home Assistant
  
- **`custom_components/smart_climate/config_flow.py`**: Configuration UI
  - User setup flow
  - Options flow for preset temperature adjustment
  
- **`custom_components/smart_climate/const.py`**: Constants and defaults
  - Preset temperature ranges
  - Temperature limits and deadbands
  - Configuration keys

- **`custom_components/smart_climate/strings.json`**: Localization strings
  - UI labels and descriptions
  - Error messages

### Tests

- **`tests/test_climate.py`**: Main test suite
  - Unit tests for all climate logic
  - Auto mode switching tests
  - Preset temperature tests
  - State restoration tests

### Configuration

- **`manifest.json`**: Component metadata (domain, version, requirements)
- **`release-please-config.json`**: Release versioning configuration
- **`.release-please-manifest.json`**: Version tracking for release-please
- **`CHANGELOG.md`**: Auto-generated changelog

## Key Patterns

### Temperature Management

- Temperatures are stored as floats (°C)
- `TEMP_STEP`: 0.5 °C granularity for setpoint adjustments
- `MIN_TEMP`/`MAX_TEMP`: Absolute bounds (10–35 °C)
- `MIN_TEMP_DIFF`: 0.5 °C minimum difference between low and high setpoints
- `FLIP_MARGIN` / `FLIP_DWELL`: how far past the comfort-band midpoint
  (in °C) and for how long (in seconds, default 30 min) the inside
  temperature must persist *against* the committed AUTO mode before
  HEAT↔COOL flips.

### AUTO Mode Logic — sticky-mode for modulating real devices

**v5.0.0 retraction:** the deliberate-OFF design (v3.0.x) and in-band
COOL hysteresis (v3.0.1, v4.0.x) were removed.  Empirical 2026-05-02
data showed manual constant-cool with `setpoint=high-1` used **~50×
less power** than smart_climate's deliberate-OFF cycling on a warm
afternoon (1.17 kW avg vs. 23 W avg).  The unit's own ±0.5 °C
hysteresis around the asymmetric setpoint already prevents the
original min-frequency-floor symptom that motivated deliberate-OFF;
adding wrapper cycling on top just adds compressor wear.  Below
sections describe the historical design — the *current* behavior is
simply "wrapper commits direction; unit handles its own hysteresis".

The wrapper picks HEAT or COOL **once** per AUTO entry and holds it.  The
real device's setpoint is **asymmetric** by direction:

- **HEAT** sends `setpoint = low` (e.g. 21 for the [21, 23] preset).
- **COOL** sends `setpoint = high - 1` (e.g. 22 for the [21, 23] preset).

The Midea unit's actual hysteresis is **asymmetric**: it holds the room
**at or slightly above setpoint** (~setpoint to setpoint + 0.3-0.5) in
*both* HEAT and COOL.  Initial design assumed symmetric ±0.5 around
setpoint; live observation 2026-04-29 corrected this — with COOL
setpoint=23 the unit held the room at 23.2-23.3, *above* the band high
edge.  v4.0.0's `setpoint=high` formula was therefore unsafe.

Corrected formulas:

- HEAT setpoint=low → unit holds room at `[low, low + 0.5]` ≈ `[21, 21.5]`.
- COOL setpoint=high - 1 → unit holds room at `[high - 1, high - 0.5]` ≈ `[22, 22.5]`.

The asymmetric `+0` for HEAT vs `-1` for COOL is dictated by the unit's
positive-only overshoot: HEAT `setpoint=low` puts the room at low+ε
(above floor, OK), but COOL `setpoint=high` would put the room at
high+ε (above ceiling, NOT OK).  Subtracting 1 °C from COOL leaves
room for the overshoot to land in band.

This gives an intermediate band around `[mid - 0.5, mid]` (e.g. 21.5 to
22 for [21, 23]) where neither mode actively pumps — the unit handles
the deadband via its own setpoint logic.  Narrower than the v4.0.0
"symmetric intermediate band" target (which assumed symmetric unit
hysteresis), but actually achievable given the unit's real behavior.

v2.0.0 / v3.0.x targeted the midpoint (22) for both directions; on this
unit that produced active heating of comfortable rooms (the v3.1.x
ghost-HEAT pattern, where HEAT setpoint=22 + current=21.4 caused 64-min
HEAT pulses to push the room from 21.4 to ~22).  Band-edge setpoints
fix this by leveraging the unit's own setpoint logic as defense in
depth: even if the wrapper commits HEAT incorrectly, the unit won't
actively pump heat unless the room is genuinely below the floor.

The wrapper's existing in-band COOL hysteresis (restart at `mid + 0.75`,
stop at `mid`) is still useful as a safety net during transition periods
where outside cooling is faster than the unit's own dynamics could
anticipate (see the cold-front transition walkthrough in design
discussion).

1. **Initial pick** (when no committed mode yet, e.g. AUTO entered fresh
   or after HA restart):
   - **inside < midpoint → HEAT, otherwise COOL** (always; outside is
     ignored).  Outside sensor remains a valid config option for
     display / future use, but does not influence the pick.  Empirical
     reason: in this deployment outside doesn't correlate with the
     building's thermodynamics, and using it caused the live HEAT-at-23
     bug on 2026-04-26 (cool outside drove HEAT pick while the room
     sat at the band's high edge).
2. **Sticky hold**: the committed mode is kept regardless of where inside
   sits.  The Midea inverter holds the midpoint via its own modulation.
3. **Flip rule**: HEAT↔COOL only flips when inside has been continuously
   past the midpoint by `FLIP_MARGIN` *against* the committed mode for
   `FLIP_DWELL` seconds.  The dwell timer is reset only when inside
   crosses fully back to the correct half of the band; the dead-zone in
   between (e.g. for COOL: `mid - FLIP_MARGIN < inside < mid`) keeps the
   timer running so jitter near the threshold doesn't repeatedly cancel
   an in-progress flip.
4. **Leaving AUTO** (to OFF, HEAT, or COOL) clears the commitment and
   the dwell timer; re-entering AUTO re-picks fresh.

### Preset System

Four presets supported (Home Assistant standard):
- `PRESET_HOME`: Default daytime (21–24 °C default)
- `PRESET_SLEEP`: Nighttime (19–22 °C default)
- `PRESET_AWAY`: Unoccupied (18–26 °C default)
- `PRESET_NONE`: Manual mode (no preset enforced)

When preset changes, the entity updates the real device's setpoint per the asymmetric rule (HEAT → low of new preset, COOL → high - 1 of new preset).

### State Restoration

The entity extends `RestoreEntity` to restore:
- Last active preset
- Last low/high setpoints
- Last HVAC mode

This survives Home Assistant restarts without requiring persistent state files.

## Versioning & Releases

**Release management is automated via `release-please`** (GitHub Actions):

1. Commits are analyzed for conventional commit format (`feat:`, `fix:`, `docs:`, etc.)
2. A PR is automatically created to bump the version and update CHANGELOG.md
3. When the PR is merged, release-please creates a GitHub Release
4. HACS automatically picks up the release via the version in `manifest.json`

### Conventional Commits

Use these prefixes in commit messages:
- `feat:` – New feature (bumps minor version)
- `fix:` – Bug fix (bumps patch version)
- `docs:` – Documentation
- `refactor:` – Code refactoring (no version bump)
- `test:` – Test changes (no version bump)
- `perf:` – Performance improvements (bumps patch version)

Example: `feat: add support for temperature rounding` or `fix: correct heat/cool switching logic`

## Common Tasks

### Adding a New Feature

1. Create a branch: `git checkout -b feature/your-feature-name`
2. Implement the feature in the appropriate file (usually `climate.py`)
3. Update `tests/test_climate.py` with comprehensive tests
4. Update `strings.json` if UI text changes
5. Commit with conventional format: `git commit -m "feat: your feature description"`
6. Open a PR – release-please will handle versioning

### Fixing a Bug

1. Write a test that reproduces the bug
2. Fix the implementation
3. Verify the test now passes
4. Commit with: `git commit -m "fix: description of fix"`

### Updating Preset Defaults

1. Edit constants in `const.py` (e.g., `DEFAULT_HOME_MIN`, `DEFAULT_HOME_MAX`)
2. Update tests in `test_climate.py` to verify the new defaults
3. Commit: `git commit -m "feat: update default preset temperatures"`

## Useful Commands

```bash
# Check what would be released
# (look at the release-please PR to see the next version number)

# Format check (if black is installed)
black --check custom_components/ tests/

# Run type checking (if mypy is installed)
mypy custom_components/

# Clean up test artifacts
rm -rf .pytest_cache __pycache__ .coverage
```

## Notes for Claude Code Users

- Tests are comprehensive — always run them before reporting a task complete
- Avoid breaking preset or state restoration behavior (very sensitive areas)
- Temperature calculations must maintain numeric precision (use TEMP_STEP consistently)
- The real climate device's setpoint in AUTO is asymmetric: `low` for HEAT, `high - 1` for COOL (corrected from v4.0.0's `high` after live observation showed unit overshoot is positive-only, not symmetric). Critical invariant.
- Master/slave (since #43): smart climate is the sole writer of the real device's
  hvac_mode and setpoint; real-device state events are only mirrored as
  `hvac_action` for UI and never feed back into preset/setpoint state
- All preset temperature ranges must respect MIN_TEMP_DIFF constraint

## Future Work — Per-preset inside sensor

> **Status:** design idea, not implemented. Captured here so a future
> session has the constraints in front of it before starting.

### Concept

Today the integration has one indoor sensor (`CONF_INSIDE_SENSOR`)
used for *every* preset. Real homes don't behave that way:

- At **Sleep**, only the bedrooms matter — the unoccupied living room
  drifting an extra degree shouldn't trigger a mode flip.
- At **Home (day)**, common areas are what we want to keep comfortable;
  bedrooms can swing.
- At **Away**, a coarse whole-home average is fine.

The fix: **each preset gets its own indoor sensor** that the user
points at.

### Proposed mechanism (deliberately minimal)

The integration **does not** compute, expose, or aggregate temperatures
itself. Building per-zone aggregates is the user's job using built-in
**HA helpers** — Min/Max/Mean helper, Group helper, Template sensor —
configured in the HA UI:

```
sensor.upstairs_avg_temp   = mean(bedroom1, bedroom2, bedroom3)
sensor.downstairs_avg_temp = mean(living, dining, kitchen, den)
sensor.whole_home_temperature = (existing) — fallback
```

The integration's only addition is config:

| Preset | Sensor field |
|--------|--------------|
| Home   | `CONF_INSIDE_SENSOR_HOME`  → `sensor.downstairs_avg_temp` |
| Sleep  | `CONF_INSIDE_SENSOR_SLEEP` → `sensor.upstairs_avg_temp` |
| Away   | `CONF_INSIDE_SENSOR_AWAY`  → `sensor.whole_home_temperature` |

When the preset changes, smart_climate switches which sensor it reads
for `current_temperature` and re-subscribes its
`async_track_state_change_event` to the new entity.

### Implications for downstream (Midea Follow Me)

`ESPHome-Midea-XYE` currently pushes a fixed sensor
(`sensor.whole_home_temperature`) into the unit via Follow Me. After
this feature lands, the right thing for it to push is whatever
`climate.smart_climate.current_temperature` resolves to — i.e. it
should follow the smart-climate output rather than a static sensor.
Either:

- **(simpler)** Re-point the ESPHome `homeassistant.sensor` reference
  at `climate.smart_climate` `current_temperature` attribute. Single
  source of truth, no preset awareness on the ESPHome side.
- **(alternative)** Have smart_climate fire a service call /
  notification when the active sensor changes, and ESPHome re-binds.
  More code, no real benefit if option 1 works.

Option 1 is the contract: **smart_climate's `current_temperature` is
the canonical "what the system should track right now"**. Everyone
downstream reads it.

### Notes & constraints

- **No new platform code.** Keep the integration to `climate.py`. No
  derived `sensor.py`, no template-engine work. The user owns the
  helpers.
- **Backwards compat.** Keep `CONF_INSIDE_SENSOR` as the default for
  any preset whose per-preset sensor is unset; existing installs keep
  working with no migration.
- **Sensor unavailability.** When the active preset's sensor returns
  `unavailable` / `unknown`, fall back to `CONF_INSIDE_SENSOR`. AUTO
  must never stall on a missing sensor.
- **No circular dependency.** Because the integration neither
  publishes nor aggregates a sensor, the loop that worried me in the
  earlier draft of this note disappears: data flows preset → sensor
  selection → climate → setpoint → real device, period.
- **Preset switch transient.** Re-subscription is synchronous; the new
  sensor's `last_state` is read immediately so the AUTO algorithm
  doesn't run on a stale value during the swap.

## v3.0.0 — Deliberate OFF in AUTO+COOL (asymmetric, narrow scope)

> **Status:** implemented in v3.0.0 (PR #58).  Refines the v2.0.0
> "never command OFF in AUTO" contract, but only for the case where
> empirical evidence showed it actively wastes energy.

### What we observed

With smart_climate in AUTO, `current = 21.5 °C`, `target = 25 °C`
(home preset, low/high = 21/23 → midpoint 22), **COOL committed**:

- Midea unit kept compressor at minimum frequency continuously.
- `compressor_flags = 0x80 ACTIVE`, `compressor_freq` non-zero,
  `t2b` (indoor coil outlet) pinned at 16.5–17.5 °C indefinitely.
- Vents pushed ~14 °C air into rooms that were already 3 °C below
  target.  Reproduced identically with both XYE and the factory
  wired thermostat — **Midea unit behavior**, not wrapper bug.

This is the **COOL minimum-frequency floor**: Midea inverters in
COOL mode at low load don't idle the compressor.  Likely cause is
the unit's internal dehumidification logic — many inverter ACs hold
a minimum compressor speed in COOL to maintain dehumidification
capacity even when temperature setpoint is met.

### Why HEAT does NOT need this fix

Tested 2026-04-26 with HEAT in AUTO + room in band: **the unit
properly idled its compressor**.

The asymmetry is most likely **refrigerant-cycle direction**, not
dehumidification.  An early hypothesis blamed COOL min-freq on
internal humidity logic — the unit has a separate selectable Dry
mode, COOL-only, which seemed to fit.  But Dry mode is *off* and
COOL still floors, so dehumidification can't be the active driver.

Better theory: in COOL the indoor coil is the cold sink, so when
the compressor stops, refrigerant migrates to it.  On the next
restart that liquid refrigerant can return to the compressor as a
slug → bearing wear or shutdown.  Holding a minimum frequency
prevents migration.  HEAT reverses the cycle (indoor coil is the
hot side), so refrigerant migrates outdoors when stopped — benign,
no slug risk on restart.  Compressor protection logic only needs
to forbid full idle in COOL.

The Dry-mode-COOL-only pairing is then explained simply by
thermodynamics: dehumidification requires a cold coil to condense
moisture, and only COOL gives you that.  It's not evidence about
*how* COOL is controlled.

So v2.0.0's "never command OFF" rule is *correct* for HEAT
(start-up cost > min-freq idle cost, no protection penalty) and
*wrong* for COOL on this unit (min-freq pumps unwanted cold air).
v3 makes the correction asymmetric to match.

### Risk assessment

Damage risk from cycling COOL on/off is **low**:

- We use the manufacturer's `set_hvac_mode` service.  The unit's
  firmware runs its own shutdown sequence (pump-down, valve
  positioning) and startup sequence (soft-start ramp from low
  frequency).  We're not yanking power.
- Modern inverter units have built-in anti-short-cycle protection.
  If the wrapper commands COOL within ~3-5 min of an OFF, the unit
  queues the request internally.
- Inverter compressors are far more cycle-tolerant than single-
  stage; soft-start makes each cycle cheap in wear terms.
- The OFF command we send is identical to what happens when a user
  presses OFF on the wired thermostat — a normal manufacturer-
  designed transition.

Cumulative wear from extra cycling is real but minor (≈30 s of
equivalent steady-state runtime per start).  The 2 °C band width
in the Home preset should comfortably keep cycles under 2/hour in
practice.

Watch in live deployment:

1. **Cycle frequency.** > 6 starts/hour sustained → add
   `OFF_HOLD_UP` (Future Work #2 below).
2. **Compressor restart frequency curve.** Healthy restart starts
   low and ramps over ~30-60 s.  If `compressor_freq` jumps
   straight to high, soft-start isn't engaging; revisit.
3. **Protection trip codes** in HA logs after the first weeks.

### Implemented behavior (v3.0.0 + v3.0.x hysteresis fixes)

Sticky AUTO commitment from v2.0.0 is unchanged.  The unit command
derived from the committed direction:

| `_auto_mode` | `current` | Last command | Unit command |
|--------------|-----------|--------------|--------------|
| HEAT         | (any)                          | (any)        | HEAT (v2.0.0 unchanged) |
| COOL         | `> high`                       | (any)        | COOL (v2.0.0) |
| COOL         | `< low`                        | (any)        | COOL (v2.0.0; FLIP_DWELL → HEAT) |
| COOL         | in band, `> mid`               | COOL         | COOL (keep cooling to mid) |
| COOL         | in band, `≤ mid`               | COOL         | OFF |
| COOL         | in band, `> mid + RESTART_OFF` | OFF / None   | COOL (lead the high edge) |
| COOL         | in band, `≤ mid + RESTART_OFF` | OFF / None   | OFF |

**In-band COOL hysteresis with offset restart**.  Two sequential
fixes after the v3.0.0 deploy on 2026-04-26:

1. **Stop at midpoint, not band edge.**  v3.0.0 returned OFF the
   moment current re-entered `[low, high]` from above.  Live result:
   2-min COOL pulses with no useful pull.  Fix: keep cooling until
   `current ≤ mid` so each start does ~½-band of cooling.

2. **Restart leads the high edge.**  Restarting at the high edge
   (e.g. `> 23` for the [21, 23] home preset) is too late — by the
   time the wrapper sees current cross 23, the unit's ramp + air-
   circulation lag has already let the room overshoot.  Fix: restart
   at `mid + COOL_RESTART_OFFSET` (default 0.75 °C above mid =
   22.75 for home preset).  COOL flow reaches the sensor before
   current would otherwise have hit high.

State is keyed on `_unit_command` (the wrapper's last sent command):
- Was OFF → stay OFF until current rises above `mid + RESTART_OFFSET`
- Was COOL → stay COOL until current drops to `mid`
- First sync (`None`) treated as OFF state (don't start uninvited)

The 0.25 °C between restart threshold and high edge is intentional
headroom for the unit's response lag, *not* unused band.  Tightens
control vs. starting at the edge, at the cost of more frequent (but
still meaningful) compressor pulls.

**Requires a sub-degree (decimal) inside-temperature sensor.**  The
hysteresis depends on resolving values like 22.7 vs. 22.8 to land
inside the 0.25 °C lead-headroom band between the restart threshold
and the high edge.  A whole-degree sensor would jump 22 → 23 and
skip the threshold entirely, defeating the lead and reverting to
start-at-high (the original short-cycling bug).  This deployment
uses Aeotec Multisensor 7 sensors (0.1 °C resolution) — fine.  For
coarser sensors, raise `COOL_RESTART_OFFSET` to at least
`(sensor_resolution + 0.5 °C)` so the lead headroom is wider than
one sensor step.

### Two-tier flip logic (v3.1.0)

Direction commitment changes on two timelines, by severity:

1. **Fast-flip — band-edge violation** (immediate).  When the
   committed direction is the *opposite* of demand AND inside is
   strictly past the band edge (`HEAT committed & inside > high`,
   or `COOL committed & inside < low`), flip on the very next
   sensor tick.  No dwell, no jitter filter — there's no legitimate
   jitter explanation for inside being past the wrong band edge.

2. **Dwell-flip — sustained margin excursion** (30 min).  When
   inside crosses `mid ± FLIP_MARGIN` against the committed
   direction but stays inside the band, the existing 30-min
   `FLIP_DWELL` logic filters jitter and only commits the flip
   after sustained pressure.

Live regression 2026-04-26: with HEAT committed (from a stale
state surviving an integration reload that pre-dated the v3.0.2
initial-pick fix), inside at 23.3 °C took 30 minutes to flip to
COOL — during which the wrapper actively heated the hot room.
The fast-flip catches this on the first sensor tick.

### Problem detection (`problems` attribute)

The wrapper exposes a `problems` extra-state-attribute — a list
of detected issues, empty when healthy.  Use in dashboards /
templates: e.g.

```yaml
- alert: |
    {% if state_attr('climate.smart_climate', 'problems') | length > 0 %}
    Smart Climate: {{ state_attr('climate.smart_climate', 'problems') | join(', ') }}
    {% endif %}
```

Conditions checked:

| Code | Meaning | Threshold |
|---|---|---|
| `inside_sensor_unavailable` / `_unknown` / `_missing` | Inside-temp sensor is unreachable | immediate |
| `sensor_stale:Nmin` | Inside sensor hasn't updated in N minutes | `SENSOR_STALE_MINUTES` (15) |
| `real_climate_unavailable` / `_missing` | Wrapped climate device is unreachable | immediate |
| `out_of_band:Nmin` | AUTO can't keep room in band | `OUT_OF_BAND_ALERT_MINUTES` (30) |
| `short_cycle:N/h` | COOL starts/hour too high | `SHORT_CYCLE_THRESHOLD_PER_H` (6) |
| `command_desync:want=X_got=Y` | Wrapper commanded `X` but real device is in `Y` past `COMMAND_GRACE_SECONDS` (60) | 60 s |

`out_of_band` only fires in AUTO (manual modes are on the user's
terms).  `short_cycle` count is a rolling 1-hour window of `OFF→COOL`
transitions tracked in `_async_sync_real_climate`.

**`command_desync`** catches silent failure to land a command — e.g.
a `set_hvac_mode` call dropped during a real-device unavailability,
or the real device deciding for itself.  Only the *change* of
`_unit_command` resets the grace window; re-issuing the same command
does not.

### State-machine persistence

The wrapper persists two pieces of state-machine state across HA
restarts via `extra_state_attributes` (which `RestoreEntity`
serialises automatically):

- `auto_mode_committed` — the sticky direction (`heat`/`cool`).
- `last_unit_command` — the wrapper's last commanded mode
  (`heat`/`cool`/`off`).

`async_added_to_hass` reads both back and rehydrates `_auto_mode`
and `_unit_command`.  Without persistence, every HA restart re-ran
the **initial pick** — and on 2026-04-26 the Z-Wave aggregator
behind `sensor.whole_home_temperature` briefly reported 21.66 °C
during sensor re-initialisation, committing the wrapper to HEAT for
30 minutes (until `FLIP_DWELL`) of wrong-direction heating against
a room that was actually at 22.7 °C.  Persisted state means we keep
the previously-correct commitment instead of guessing again.

`_pending_flip_since`, `_out_of_band_since`, and `_cool_start_times`
are *not* persisted — they re-arm naturally from sensor data and
the worst-case latency on each is acceptable (`FLIP_DWELL` 30 min,
`OUT_OF_BAND_ALERT_MINUTES` 30 min, short-cycle 1 h window).

`hvac_action` returns `IDLE` (not `OFF`) on the wrapper whenever
the unit_command is OFF — distinguishes "AUTO resting" from
"user turned it off entirely".  Implemented via a `_unit_command`
attribute set by `_async_sync_real_climate` and read by the
`hvac_action` property.

No anti-flap guards yet.  We deliberately shipped the simplest
possible thing first (just a band check, no `OFF_HOLD_DOWN`,
`OFF_HOLD_UP`, or `OFF_DEADBAND`).  If the live deployment shows
short-cycling at the band edges, those guards are next — see
"Future Work" below.

### Integration with the per-preset inside-sensor work

Both share one substrate: smart_climate's `current_temperature`
(whatever sensor the active preset points at) drives all decisions.
The per-preset sensor work changes *which* sensor; the deliberate-OFF
work changes *what to do* with the value.  Compose cleanly.

## Future Work — Deliberate-OFF refinements (not yet implemented)

The v3.0.0 implementation is intentionally minimal.  These refinements
are scoped but not built:

### 1. Mode-flip transition OFF

When AUTO commits a HEAT↔COOL flip (FLIP_DWELL met), emit
`old_mode → off → wait OFF_SETTLE → new_mode`.  Lets the compressor
spin down, the reversing valve stabilise, and refrigerant pressures
equalise before the new mode kicks in.  At most ~1–2 mode flips/day
in normal use, so the cost of an extra startup is negligible.
Default `OFF_SETTLE = 60 s`.

### 2. COOL-side anti-flap guards

If the live data shows the COOL unit short-cycling at the band
edges (sub-10-minute on/off cycles), add:

- `OFF_HOLD_DOWN` (default 60 s): require a sustained in-band
  reading before triggering OFF.  Filters sensor jitter.
- `OFF_HOLD_UP` (default 300 s): minimum OFF duration before
  re-arming COOL.  Prevents re-starting the compressor the moment
  current grazes the high edge.

### 3. OFF on wrong-side COOL excursion (CANDIDATE)

v3.0.0 deliberately keeps v2.0.0 behavior in the wrong-side COOL
case: COOL committed + current < low → send COOL (and let
`FLIP_DWELL` flip the committed direction to HEAT after 30 min).
Per user direction at PR time: *"OFF only when tending to cool
inside the band"*.

The downside surfaces on cool spring/fall nights after a warm day:
AUTO is still committed COOL from the afternoon, the room drifts
below low overnight, and the wrapper sends COOL into a cold room
for up to 30 minutes before the dwell-flip kicks in.  Empirically
small (the unit modulates and the room is already cold so the
gradient is small), but visible in logs.

If this turns out to matter in the live deployment, the surgical
fix is:

```python
if self._auto_mode == HVACMode.COOL:
    if low <= inside <= high:
        return HVACMode.OFF
    if inside < low:           # NEW: don't fight the room
        return HVACMode.OFF
    return HVACMode.COOL
```

Decision deferred until we have overnight data showing the
behavior is actually a problem.

### 4. Wide-deadband HEAT (DEFERRED — empirically not needed)

Originally proposed as a mirror of the COOL trigger.  Live testing
2026-04-26 showed HEAT properly idles, so this is not implemented
and not on the roadmap unless a different unit shows the symmetric
defect.

### Constants reserved for future use

```python
# const.py additions, when refinements are needed
OFF_SETTLE     = 60     # seconds, mode-flip OFF duration
OFF_HOLD_DOWN  = 60     # seconds in band before OFF (anti-jitter)
OFF_HOLD_UP    = 300    # minimum OFF duration before re-arming COOL
```

## Future Work — Fan time percentage (Ecobee-style)

> **Status:** scoped, not designed in detail.

Per-preset `home_fan_pct` / `sleep_fan_pct` / `away_fan_pct`
(default 0). When HVAC is OFF or idle, run the unit's fan at low
speed for *N* minutes per hour to circulate air, distribute
temperature, and lightly dehumidify. Useful particularly when
deliberate-OFF (above) is keeping the compressor stopped for long
stretches — the room would benefit from passive air mixing during
those periods.

Implementation note: Midea XYE supports FAN_ONLY mode. The fan-time
loop alternates the real device between OFF (during the fan-off
fraction) and FAN_ONLY (during the fan-on fraction).

## Deferred (v4+) — Whole-home ancillary controls

The user has additional whole-home equipment that the smart climate
*could* coordinate with eventually. Capturing now so they're not
forgotten; **out of scope for the next release**:

- **Outside-air-intake damper switch.** A motorised damper on a
  fresh-air intake. When smart_climate is running cooling overnight
  at low outdoor temperatures and the fresh-air damper is open,
  fresh cold air rushes in and amplifies the over-cooling problem.
  Coupling: when in deliberate-OFF, optionally close the damper too;
  when running fan-only with `*_fan_pct`, open the damper for free
  passive cooling on cool nights.
- **CO₂ sensors.** Indoor CO₂ trending up = need fresh air →
  override fan-time-percent and damper logic to prioritise air
  exchange. Below threshold, normal logic applies.

These are **out of scope until the basics are right**. Don't
implement them in the same release as deliberate-OFF or fan-time —
keep each refinement tractable and testable.

## References

- [Home Assistant Climate API](https://developers.home-assistant.io/docs/core/entity/climate/)
- [HACS Documentation](https://hacs.xyz/)
- [Release Please Configuration](https://github.com/googleapis/release-please/blob/main/docs/config.md)
