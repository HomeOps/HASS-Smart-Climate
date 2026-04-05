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
