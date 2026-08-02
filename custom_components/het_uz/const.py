"""Constants for the HET Uzbekistan integration."""

from typing import Final

DOMAIN: Final = "het_uz"

CONF_SCAN_INTERVAL: Final = "scan_interval"
DEFAULT_SCAN_INTERVAL: Final = 15
MIN_SCAN_INTERVAL: Final = 1
MAX_SCAN_INTERVAL: Final = 1440

API_BASE_URL: Final = "https://cabinet-api.het.uz"
LOGIN_PATH: Final = "/household-consumer/v1/mobile-cabinet/user-login"
STATE_PATH: Final = "/household-consumer/v1/mobile-cabinet/consumer-state"
REQUEST_TIMEOUT: Final = 30

PLATFORMS: Final = ["sensor"]
