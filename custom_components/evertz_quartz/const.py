"""Constants for the Evertz Quartz integration."""

DOMAIN = "evertz_quartz"

# Config entry keys (set at setup, stored in entry.data)
CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "router_name"       # optional friendly name, falls back to IP
CONF_MAX_SOURCES = "max_sources"
CONF_MAX_DESTINATIONS = "max_destinations"
CONF_LEVELS = "levels"

# Options keys (editable post-setup via Configure, stored in entry.options)
CONF_VERBOSE_LOGGING = "verbose_logging"
CONF_RECONNECT_DELAY = "reconnect_delay"
CONF_CONNECT_TIMEOUT = "connect_timeout"

# Defaults
DEFAULT_PORT = 3737
DEFAULT_MAX_SOURCES = 32
DEFAULT_MAX_DESTINATIONS = 32
DEFAULT_LEVELS = "V"
DEFAULT_VERBOSE_LOGGING = False
DEFAULT_RECONNECT_DELAY = 5    # seconds
DEFAULT_CONNECT_TIMEOUT = 10   # seconds

# Quartz protocol constants
QUARTZ_ACK = ".A"
QUARTZ_POLL_INTERVAL = 30      # seconds between periodic state re-polls
