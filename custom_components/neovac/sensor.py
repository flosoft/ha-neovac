"""Sensor entities for the NeoVac MyEnergy integration.

Provides sensors compatible with the Home Assistant Energy Dashboard:
- Electricity consumption (kWh)
- Water consumption (m³)
- Warm water consumption (m³)
- Cold water consumption (m³)
- Heating consumption (kWh)
- Cooling consumption (kWh)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CATEGORY_COLD_WATER,
    CATEGORY_COOLING,
    CATEGORY_ELECTRICITY,
    CATEGORY_HEATING,
    CATEGORY_WARM_WATER,
    CATEGORY_WATER,
    CONF_USAGE_UNIT_ID,
    CONF_USAGE_UNIT_NAME,
    DOMAIN,
)
from .coordinator import NeoVacCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NeoVacSensorEntityDescription(SensorEntityDescription):
    """Describes a NeoVac sensor entity."""

    category: str
    value_key: str = "value"


# Sensor descriptions for each energy category
SENSOR_DESCRIPTIONS: tuple[NeoVacSensorEntityDescription, ...] = (
    NeoVacSensorEntityDescription(
        key="electricity",
        translation_key="electricity",
        category=CATEGORY_ELECTRICITY,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    NeoVacSensorEntityDescription(
        key="water",
        translation_key="water",
        category=CATEGORY_WATER,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
    ),
    NeoVacSensorEntityDescription(
        key="warm_water",
        translation_key="warm_water",
        category=CATEGORY_WARM_WATER,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
    ),
    NeoVacSensorEntityDescription(
        key="cold_water",
        translation_key="cold_water",
        category=CATEGORY_COLD_WATER,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
    ),
    NeoVacSensorEntityDescription(
        key="heating",
        translation_key="heating",
        category=CATEGORY_HEATING,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    NeoVacSensorEntityDescription(
        key="cooling",
        translation_key="cooling",
        category=CATEGORY_COOLING,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NeoVac sensors from a config entry."""
    coordinator: NeoVacCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Wait for initial data
    await coordinator.async_config_entry_first_refresh()

    available_categories = coordinator.data.get("available_categories", [])
    _LOGGER.debug("Setting up sensors for categories: %s", available_categories)

    entities: list[NeoVacSensor] = []
    for description in SENSOR_DESCRIPTIONS:
        if description.category in available_categories:
            entities.append(
                NeoVacSensor(
                    coordinator=coordinator,
                    entry=entry,
                    description=description,
                )
            )
            _LOGGER.debug(
                "Added sensor: %s (%s)", description.key, description.category
            )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Set up %d NeoVac sensors", len(entities))
    else:
        _LOGGER.warning(
            "No sensors created - no supported categories available. "
            "Available categories from API: %s",
            available_categories,
        )


def _extract_latest_value(
    category_data: dict[str, Any] | list | None,
    value_key: str = "value",
) -> float | None:
    """Extract the latest consumption value from API response data.

    The NeoVac API returns consumption data in various formats. This
    function tries to extract the most recent total/cumulative value.

    Common response structures:
    - List of data points with timestamps and values
    - Dict with a 'data' list of data points
    - Dict with a direct 'value' or 'total' field
    """
    if category_data is None:
        return None

    # If it's a dict, look for common patterns
    if isinstance(category_data, dict):
        # Direct value field
        for key in ("total", "value", "currentValue", "cumulativeValue"):
            if key in category_data:
                val = category_data[key]
                if isinstance(val, (int, float)):
                    return float(val)

        # Data points list inside the dict
        data_points = category_data.get("data") or category_data.get(
            "dataPoints"
        ) or category_data.get("values") or category_data.get("items")

        if isinstance(data_points, list) and data_points:
            return _get_latest_from_points(data_points, value_key)

        # Try to find any list value that looks like data points
        for val in category_data.values():
            if isinstance(val, list) and val:
                result = _get_latest_from_points(val, value_key)
                if result is not None:
                    return result

    # If it's a list directly
    if isinstance(category_data, list) and category_data:
        return _get_latest_from_points(category_data, value_key)

    return None


def _get_latest_from_points(
    points: list, value_key: str = "value"
) -> float | None:
    """Get the latest value from a list of data points.

    Data points are typically dicts with a timestamp and value field.
    We want the last non-null value, which should be the most recent
    cumulative reading.
    """
    # Iterate from the end to find the latest non-null value
    for point in reversed(points):
        if isinstance(point, dict):
            # Try common value field names
            for key in (value_key, "value", "total", "y", "consumption"):
                if key in point:
                    val = point[key]
                    if val is not None and isinstance(val, (int, float)):
                        return float(val)
        elif isinstance(point, (int, float)):
            return float(point)

    return None


class NeoVacSensor(CoordinatorEntity[NeoVacCoordinator], SensorEntity):
    """Representation of a NeoVac energy/water sensor."""

    entity_description: NeoVacSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NeoVacCoordinator,
        entry: ConfigEntry,
        description: NeoVacSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description

        unit_id = entry.data[CONF_USAGE_UNIT_ID]
        unit_name = entry.data.get(CONF_USAGE_UNIT_NAME, unit_id)

        self._attr_unique_id = f"{unit_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unit_id)},
            name=f"NeoVac {unit_name}",
            manufacturer="NeoVac",
            model="MyEnergy",
        )

    @property
    def native_value(self) -> float | None:
        """Return the current sensor value."""
        if not self.coordinator.data:
            return None

        categories = self.coordinator.data.get("categories", {})
        category_data = categories.get(self.entity_description.category)

        if category_data is None:
            return None

        value = _extract_latest_value(
            category_data, self.entity_description.value_key
        )

        if value is not None:
            _LOGGER.debug(
                "Sensor %s (%s) value: %s",
                self.entity_description.key,
                self.entity_description.category,
                value,
            )

        return value

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        if not self.coordinator.data:
            return False
        categories = self.coordinator.data.get("categories", {})
        return self.entity_description.category in categories
