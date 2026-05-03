"""Constants for the Smart Climate integration."""

DOMAIN = "smart_climate"

# Configuration keys
CONF_REAL_CLIMATE = "real_climate"
CONF_INSIDE_SENSOR = "inside_sensor"
CONF_OUTSIDE_SENSOR = "outside_sensor"

# Preset temperature range configuration keys
CONF_HOME_MIN = "home_min"
CONF_HOME_MAX = "home_max"
CONF_SLEEP_MIN = "sleep_min"
CONF_SLEEP_MAX = "sleep_max"
CONF_AWAY_MIN = "away_min"
CONF_AWAY_MAX = "away_max"

# Default preset temperatures (°C)
DEFAULT_HOME_MIN = 21.0
DEFAULT_HOME_MAX = 24.0
DEFAULT_SLEEP_MIN = 19.0
DEFAULT_SLEEP_MAX = 22.0
DEFAULT_AWAY_MIN = 18.0
DEFAULT_AWAY_MAX = 26.0

# Temperature constraints
MIN_TEMP = 10.0
MAX_TEMP = 35.0
TEMP_STEP = 0.5

# Minimum allowed difference between low and high setpoints
MIN_TEMP_DIFF = 0.5

# AUTO mode picks HEAT or COOL once and holds it; the real device's setpoint
# is the comfort-band midpoint and the (modulating) device is left to settle
# on it.  HEAT↔COOL flips only when the inside temperature has been
# continuously past the midpoint by FLIP_MARGIN for FLIP_DWELL seconds —
# i.e. the room is asking for the opposite mode, not just jittering across
# the boundary.  This is sized for inverter heat pumps where the cost of
# restarting the compressor far exceeds the energy of holding low
# modulation, and where short OFF cycles defeat the unit's own steady-state
# operation.
FLIP_MARGIN = 0.5
FLIP_DWELL = 1800  # 30 min

# NOTE: COOL_RESTART_OFFSET (used in v3.0.x – v4.0.x for in-band COOL
# hysteresis driven by the wrapper) was removed in v5.0.0.  Empirical
# 2026-05-02 data showed the wrapper's deliberate-OFF cycling used
# ~50× more power than just letting the unit handle its own hysteresis
# via the asymmetric setpoints (HEAT=low, COOL=high-1).  The
# Midea unit's own ±0.5 °C internal hysteresis around setpoint keeps
# the room near the band edge that matches the committed direction;
# the wrapper just commits the direction.

# Problem-detection thresholds.  Surfaced as the wrapper's `problems`
# attribute (a list of detected issues, empty when healthy).  Sized
# generously so transient blips don't fire false alarms; tune down if
# false-negatives matter more than false-alarms in a given deployment.
OUT_OF_BAND_ALERT_MINUTES   = 30   # sustained outside [low, high] in AUTO
SHORT_CYCLE_THRESHOLD_PER_H = 6    # COOL starts/hour above this is "too much"
SENSOR_STALE_MINUTES        = 15   # no inside-temp update for this long

# Command-desync detection: how long we wait for the real device to
# settle into the wrapper's last commanded state before flagging the
# divergence as a problem.  60 s is generous — `set_hvac_mode` is
# fire-and-forget (`blocking=False`), and the real device's
# soft-start ramp + state-event propagation takes a few seconds even
# in the happy path.
COMMAND_GRACE_SECONDS       = 60
