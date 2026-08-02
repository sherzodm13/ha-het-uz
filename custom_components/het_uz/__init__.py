"""HET Uzbekistan integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HetApiClient
from .const import PLATFORMS
from .coordinator import HetDataUpdateCoordinator

HetConfigEntry = ConfigEntry[HetDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: HetConfigEntry) -> bool:
    """Set up HET from a config entry."""
    client = HetApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = HetDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HetConfigEntry) -> bool:
    """Unload HET."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
