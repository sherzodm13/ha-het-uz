"""Sensors for HET Uzbekistan."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HetConfigEntry
from .api import HetState
from .const import DOMAIN
from .coordinator import HetDataUpdateCoordinator

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HetSensorDescription(SensorEntityDescription):
    """Describe an HET sensor."""

    value_fn: Callable[[HetState], Decimal | None]


SENSORS: tuple[HetSensorDescription, ...] = (
    HetSensorDescription(
        key="balance",
        translation_key="balance",
        icon="mdi:wallet",
        native_unit_of_measurement="UZS",
        suggested_display_precision=2,
        value_fn=lambda data: data.balance,
    ),
    HetSensorDescription(
        key="current_month_kwh",
        translation_key="current_month_kwh",
        icon="mdi:lightning-bolt",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda data: data.current_month_kwh,
    ),
    HetSensorDescription(
        key="current_month_amount",
        translation_key="current_month_amount",
        icon="mdi:cash",
        native_unit_of_measurement="UZS",
        suggested_display_precision=2,
        value_fn=lambda data: data.current_month_amount,
    ),
)


async def async_setup_entry(hass, entry: HetConfigEntry, async_add_entities) -> None:
    """Create the three HET sensors."""
    async_add_entities(
        HetSensor(entry.runtime_data, entry, description) for description in SENSORS
    )


class HetSensor(CoordinatorEntity[HetDataUpdateCoordinator], SensorEntity):
    """A value from the HET household cabinet."""

    entity_description: HetSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HetDataUpdateCoordinator,
        entry: HetConfigEntry,
        description: HetSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name="HET Uzbekistan",
            manufacturer="Hududiy Elektr Tarmoqlari",
            configuration_url="https://cabinet.het.uz/household/home",
        )

    @property
    def native_value(self) -> Decimal | None:
        """Return the latest API value."""
        return self.entity_description.value_fn(self.coordinator.data)
