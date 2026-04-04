"""Sensor entities for the NeoVac MyEnergy integration.

Provides sensors compatible with the Home Assistant Energy Dashboard:
- Electricity consumption (kWh)
- Water consumption (L) - total, warm, and cold
- Heating consumption (kWh)
- Cooling consumption (kWh)

The NeoVac API returns per-interval consumption values (not cumulative).
We sum the values in the current invoice period to create a monotonically
increasing total that resets at the start of each invoice period, which is
what SensorStateClass.TOTAL_INCREASING expects.
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


# Sensor descriptions for each energy category.
# Water values come from the API in Liters; the invoice period sum is in m³.
# We report the invoice period sum (m³) for the energy dashboard.
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
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=1,
    ),
    NeoVacSensorEntityDescription(
        key="warm_water",
        translation_key="warm_water",
        category=CATEGORY_WARM_WATER,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=1,
    ),
    NeoVacSensorEntityDescription(
        key="cold_water",
        translation_key="cold_water",
        category=CATEGORY_COLD_WATER,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=1,
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


def _extract_period_total(
    category_data: dict[str, Any] | None,
) -> float | None:
    """Extract the invoice period total from API response data.

    The NeoVac API response contains:
    - invoicePeriods[].sum: cumulative total for the entire invoice period
    - currentPeriodValues[]: individual interval readings

    We use the invoice period sum as the sensor value because it's a
    cumulative total that increases over the billing period and resets
    when a new period starts -- exactly what TOTAL_INCREASING expects.
    """
    if category_data is None or not isinstance(category_data, dict):
        return None

    # Use the invoice period sum as the cumulative total
    invoice_periods = category_data.get("invoicePeriods")
    if isinstance(invoice_periods, list) and invoice_periods:
        # Use the most recent (last) period
        period = invoice_periods[-1]
        total = period.get("sum")
        if total is not None and isinstance(total, (int, float)):
            # Convert water from m³ to Liters if the sum is in CubicMeter
            # but the measurement unit is Liter
            measurement_unit = category_data.get("measurementUnit", "")
            sum_unit = period.get("sumUnit", "")
            if measurement_unit == "Liter" and sum_unit == "CubicMeter":
                return float(total) * 1000.0
            return float(total)

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
        unit_name = entry.data.get(CONF_USAGE_UNIT_NAME, str(unit_id))

        self._attr_unique_id = f"{unit_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(unit_id))},
            name=f"NeoVac {unit_name}",
            manufacturer="NeoVac",
            model="MyEnergy",
        )

    @property
    def native_value(self) -> float | None:
        """Return the current sensor value.

        Returns the invoice period cumulative total.
        """
        if not self.coordinator.data:
            return None

        categories = self.coordinator.data.get("categories", {})
        category_data = categories.get(self.entity_description.category)

        if category_data is None:
            return None

        value = _extract_period_total(category_data)

        if value is not None:
            _LOGGER.debug(
                "Sensor %s (%s) value: %s",
                self.entity_description.key,
                self.entity_description.category,
                value,
            )

        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        if not self.coordinator.data:
            return None

        categories = self.coordinator.data.get("categories", {})
        category_data = categories.get(self.entity_description.category)

        if not isinstance(category_data, dict):
            return None

        attrs: dict[str, Any] = {}

        # Add measurement unit info
        measurement_unit = category_data.get("measurementUnit")
        if measurement_unit:
            attrs["api_measurement_unit"] = measurement_unit

        # Add available resolutions
        resolutions = category_data.get("resolutions")
        if resolutions:
            attrs["available_resolutions"] = resolutions

        # Add the latest interval value (most recent reading)
        current_values = category_data.get("currentPeriodValues")
        if isinstance(current_values, list) and current_values:
            # Find last non-interpolated value, or just the last value
            latest = current_values[-1]
            for point in reversed(current_values):
                if not point.get("isInterpolated", False):
                    latest = point
                    break
            attrs["latest_reading"] = latest.get("value")
            attrs["latest_reading_date"] = latest.get("date")
            attrs["latest_reading_interpolated"] = latest.get(
                "isInterpolated", False
            )

        # Add invoice period info
        invoice_periods = category_data.get("invoicePeriods")
        if isinstance(invoice_periods, list) and invoice_periods:
            period = invoice_periods[-1]
            attrs["invoice_period_start"] = period.get("startDate")
            attrs["invoice_period_end"] = period.get("endDate")

        return attrs if attrs else None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        if not self.coordinator.data:
            return False
        categories = self.coordinator.data.get("categories", {})
        return self.entity_description.category in categories
