"""Update coordinator for HET Uzbekistan."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HetApiAuthError, HetApiClient, HetApiConnectionError, HetState
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class HetDataUpdateCoordinator(DataUpdateCoordinator[HetState]):
    """Poll all three values with one API request."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: HetApiClient
    ) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=interval),
            always_update=False,
        )
        self.client = client

    async def _async_update_data(self) -> HetState:
        try:
            return await self.client.async_get_state()
        except HetApiAuthError as err:
            raise ConfigEntryAuthFailed("HET authentication failed") from err
        except HetApiConnectionError as err:
            raise UpdateFailed(f"Error communicating with HET: {err}") from err
