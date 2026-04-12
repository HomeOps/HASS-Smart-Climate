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
- `TEMP_STEP`: 0.1 °C granularity for setpoint adjustments
- `MIN_TEMP`/`MAX_TEMP`: Absolute bounds (16–32 °C)
- `INSIDE_DEADBAND`: 0.5 °C hysteresis to avoid switching oscillation
- `MIN_TEMP_DIFF`: 0.5 °C minimum difference between low and high setpoints

### AUTO Mode Logic

1. If `inside_temp < low_setpoint` → switch real device to **HEAT**
2. If `inside_temp > high_setpoint` → switch real device to **COOL**
3. If `inside_temp` is within range:
   - **With outside sensor**: use outside temperature to decide (warm → cool, cold → heat)
   - **Without outside sensor**: use midpoint (above → cool, below → heat)

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
- The real climate device's setpoint is always the preset midpoint (critical invariant)
- External setpoint changes > INSIDE_DEADBAND force Manual mode (prevents conflicts)
- All preset temperature ranges must respect MIN_TEMP_DIFF constraint

## References

- [Home Assistant Climate API](https://developers.home-assistant.io/docs/core/entity/climate/)
- [HACS Documentation](https://hacs.xyz/)
- [Release Please Configuration](https://github.com/googleapis/release-please/blob/main/docs/config.md)
