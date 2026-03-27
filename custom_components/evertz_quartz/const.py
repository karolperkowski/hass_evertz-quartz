"""Constants for the Evertz Quartz integration."""

DOMAIN = "evertz_quartz"

# Config entry keys (stored in entry.data)
CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "router_name"            # optional friendly name, falls back to IP
CONF_MAX_SOURCES = "max_sources"
CONF_MAX_DESTINATIONS = "max_destinations"
CONF_LEVELS = "levels"
CONF_CSV_LOADED = "csv_loaded"       # True when names/port maps came from a CSV upload

# Options keys (stored in entry.options, editable via Configure)
CONF_RECONNECT_DELAY = "reconnect_delay"
CONF_CONNECT_TIMEOUT = "connect_timeout"

# Defaults
DEFAULT_PORT = 3737
DEFAULT_MAX_SOURCES = 32
DEFAULT_MAX_DESTINATIONS = 32
DEFAULT_LEVELS = "V"
DEFAULT_RECONNECT_DELAY = 5    # seconds
DEFAULT_CONNECT_TIMEOUT = 10   # seconds

# Quartz protocol constants
QUARTZ_ACK = ".A"

# Profile mismatch tracking (stored in entry.data, cleared on reload)
CONF_PROFILE_MISMATCH = "profile_mismatch_orders"  # list of out-of-range Orders seen
