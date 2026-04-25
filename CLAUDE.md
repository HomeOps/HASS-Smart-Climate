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

The wrapper picks HEAT or COOL **once** per AUTO entry and holds it; the
real device's setpoint is the comfort-band midpoint (rounded directionally
to a whole integer — up for HEAT, down for COOL — and clamped into the
band).  The (modulating) real device is left to settle on the midpoint at
low compressor modulation rather than being repeatedly start/stop cycled.
Real device is **never** commanded OFF in AUTO.

1. **Initial pick** (when no committed mode yet, e.g. AUTO entered fresh
   or after HA restart):
   - With outside sensor: outside < midpoint → HEAT, otherwise COOL.
   - Without outside sensor: inside < midpoint → HEAT, otherwise COOL.
   - Tie at midpoint with no outside sensor → COOL.
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

When preset changes, the entity updates the real device's setpoint to the preset's midpoint.

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
- The real climate device's setpoint in AUTO is the preset midpoint (critical invariant)
- Master/slave (since #43): smart climate is the sole writer of the real device's
  hvac_mode and setpoint; real-device state events are only mirrored as
  `hvac_action` for UI and never feed back into preset/setpoint state
- All preset temperature ranges must respect MIN_TEMP_DIFF constraint

## References

- [Home Assistant Climate API](https://developers.home-assistant.io/docs/core/entity/climate/)
- [HACS Documentation](https://hacs.xyz/)
- [Release Please Configuration](https://github.com/googleapis/release-please/blob/main/docs/config.md)
