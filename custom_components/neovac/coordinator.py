"""DataUpdateCoordinator for the NeoVac MyEnergy integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import NeoVacApiClient, NeoVacAuthError, NeoVacConnectionError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_USAGE_UNIT_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    RESOLUTION_HOUR,
    SUPPORTED_CATEGORIES,
)

_LOGGER = logging.getLogger(__name__)


class NeoVacCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch data from NeoVac MyEnergy API.

    The coordinator fetches consumption data for all available categories
    for a single usage unit. The data is structured as:

    {
        "usage_unit": { ... usage unit metadata ... },
        "categories": {
            "Electricity": { ... consumption data ... },
            "Water": { ... consumption data ... },
            ...
        },
        "available_categories": ["Electricity", "Water", ...],
    }
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: NeoVacApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.unit_id: str = entry.data[CONF_USAGE_UNIT_ID]
        self._available_categories: list[str] | None = None

        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.unit_id}",
            update_interval=timedelta(minutes=scan_interval),
            config_entry=entry,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the NeoVac API.

        Fetches consumption data for each available category using
        hourly resolution for the last 24 hours.
        """
        try:
            return await self._fetch_all_data()
        except NeoVacAuthError as err:
            raise ConfigEntryAuthFailed(
                f"Authentication failed: {err}"
            ) from err
        except NeoVacConnectionError as err:
            raise UpdateFailed(
                f"Connection error: {err}"
            ) from err
        except Exception as err:
            raise UpdateFailed(
                f"Error fetching data: {err}"
            ) from err

    async def _fetch_all_data(self) -> dict[str, Any]:
        """Fetch all data from the API."""
        result: dict[str, Any] = {
            "usage_unit": None,
            "categories": {},
            "available_categories": [],
        }

        # Get usage unit info (cached after first call)
        try:
            unit_data = await self.client.get_usage_unit(self.unit_id)
            result["usage_unit"] = unit_data
        except Exception as err:
            _LOGGER.debug("Could not fetch usage unit info: %s", err)

        # Discover available categories on first run
        if self._available_categories is None:
            self._available_categories = (
                await self.client.get_available_categories(self.unit_id)
            )
            _LOGGER.info(
                "Available categories for unit %s: %s",
                self.unit_id,
                self._available_categories,
            )

        result["available_categories"] = self._available_categories

        # Time window: last 24 hours with hourly resolution
        now = datetime.now()
        end_date = now.strftime("%Y-%m-%d %H:%M")
        start_date = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

        # Fetch consumption for each available category
        for category in self._available_categories:
            if category not in SUPPORTED_CATEGORIES:
                continue
            try:
                data = await self.client.get_consumption(
                    self.unit_id,
                    category,
                    resolution=RESOLUTION_HOUR,
                    start_date=start_date,
                    end_date=end_date,
                )
                if data is not None:
                    result["categories"][category] = data
                    _LOGGER.debug(
                        "Got consumption data for %s: %s",
                        category,
                        type(data).__name__,
                    )
            except Exception as err:
                _LOGGER.warning(
                    "Failed to fetch consumption for %s: %s",
                    category,
                    err,
                )

        return result

    async def refresh_categories(self) -> None:
        """Force re-discovery of available categories."""
        self._available_categories = None
        await self.async_request_refresh()
