"""Update platform for Ted's Dashboard System.

Surfaces a Home Assistant ``update`` entity that reports the installed vs. latest
Ted's Dashboard content version (from ``versions.json``) and installs updates by
downloading the latest content from the dashboard repo.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DASHBOARD_TITLE, DOMAIN
from .updater import DashboardUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ted's Dashboard update entity."""
    manager = hass.data[DOMAIN][entry.entry_id]
    coordinator: DashboardUpdateCoordinator | None = getattr(manager, "updater", None)
    if coordinator is None:
        return
    async_add_entities([TedsDashboardUpdate(coordinator, entry)])


class TedsDashboardUpdate(
    CoordinatorEntity[DashboardUpdateCoordinator], UpdateEntity
):
    """Reports (and installs) updates to the Ted's Dashboard content."""

    _attr_has_entity_name = True
    _attr_name = "Dashboard"
    _attr_title = DASHBOARD_TITLE
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(
        self, coordinator: DashboardUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialise the update entity for this config entry."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_dashboard_update"

    @property
    def installed_version(self) -> str | None:
        """Installed dashboard content version."""
        return self.coordinator.installed_version

    @property
    def latest_version(self) -> str | None:
        """Latest available dashboard content version."""
        return self.coordinator.latest_version

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Download and install the latest Ted's Dashboard content."""
        await self.coordinator.async_install_now()
