"""Constants for the Evertz Quartz integration."""

DOMAIN = "evertz_quartz"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_MAX_SOURCES = "max_sources"
CONF_MAX_DESTINATIONS = "max_destinations"
CONF_LEVELS = "levels"

DEFAULT_PORT = 3737
DEFAULT_MAX_SOURCES = 32
DEFAULT_MAX_DESTINATIONS = 32
DEFAULT_LEVELS = "V"

# Quartz protocol constants
QUARTZ_ACK = ".A"
QUARTZ_RECONNECT_DELAY = 5  # seconds
QUARTZ_CONNECT_TIMEOUT = 10  # seconds
QUARTZ_POLL_INTERVAL = 30  # seconds for periodic state refresh
