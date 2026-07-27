"""Update platform for Ted's Dashboard System.

Surfaces a Home Assistant ``update`` entity that reports the installed vs. latest
Ted's Dashboard content release and, when the installer is wired in (later
phases), performs the install.

NOT YET WIRED: this platform is intentionally absent from ``PLATFORMS`` in
``__init__.py`` until the installer (P2–P4) exists and the per-entry
``hass.data`` layout gains an ``"updater"`` coordinator. Until then this module
is inert scaffolding.
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
    coordinator: DashboardUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["updater"]
    async_add_entities([TedsDashboardUpdate(coordinator, entry)])


class TedsDashboardUpdate(
    CoordinatorEntity[DashboardUpdateCoordinator], UpdateEntity
):
    """Reports (and installs) updates to the Ted's Dashboard content."""

    _attr_has_entity_name = True
    _attr_name = "Dashboard"
    _attr_title = DASHBOARD_TITLE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(
        self, coordinator: DashboardUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialise the update entity for this config entry."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_dashboard_update"

    @property
    def installed_version(self) -> str | None:
        """Release tag currently installed."""
        return self.coordinator.installed_tag

    @property
    def latest_version(self) -> str | None:
        """Latest release tag available."""
        return self.coordinator.latest_tag

    async def async_release_notes(self) -> str | None:
        """Return the latest release notes."""
        return self.coordinator.release_notes

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install the latest Ted's Dashboard content.

        The installer (added in P2–P4) performs the download → compose → register
        work and calls ``coordinator.async_record_install(...)``. Until it is
        wired, this simply refreshes availability.
        """
        await self.coordinator.async_request_refresh()
