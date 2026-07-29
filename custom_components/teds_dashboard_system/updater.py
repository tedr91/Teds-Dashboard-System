"""Update coordinator + persisted installer state for Ted's Dashboard System.

The coordinator polls the public Ted's Dashboard content repo's ``versions.json``
(every ``UPDATE_POLL_INTERVAL_HOURS``) and, together with a small ``Store``,
tracks the installed dashboard version so an ``update`` entity can report — and
apply — updates.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    EVENT_DASHBOARD_UPDATED,
    INSTALLER_STORAGE_KEY,
    INSTALLER_STORAGE_VERSION,
    UPDATE_POLL_INTERVAL_HOURS,
    VERSIONS_FILE,
)
from .github import GitHubClient, GitHubError

_LOGGER = logging.getLogger(__name__)


async def async_load_installer_state(hass: HomeAssistant) -> tuple[Store, dict[str, Any]]:
    """Load persisted installer state, returning ``(store, data)``.

    ``data`` has the shape ``{"installed_tag": str | None, "versions": dict}``.
    """
    store: Store = Store(hass, INSTALLER_STORAGE_VERSION, INSTALLER_STORAGE_KEY)
    data = await store.async_load() or {}
    return store, data


def _version_tuple(value: Any) -> tuple[int, ...]:
    parts: list[int] = []
    for part in str(value or "0").split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


class DashboardUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the Ted's Dashboard repo's versions.json; report & apply updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        github: GitHubClient,
        store: Store,
        state: dict[str, Any],
    ) -> None:
        """Initialise with the GitHub client and previously-persisted state."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_updater",
            update_interval=timedelta(hours=UPDATE_POLL_INTERVAL_HOURS),
        )
        self.github = github
        self._store = store
        self._state = state

    @property
    def _installed_versions(self) -> dict[str, Any]:
        return self._state.get("versions", {}).get("dashboard", {})

    @property
    def remote_versions(self) -> dict[str, Any]:
        """The most-recently-fetched remote versions.json (empty until polled)."""
        return self.data or {}

    @property
    def installed_version(self) -> str | None:
        """Installed dashboard content version."""
        value = self._installed_versions.get("dashboard")
        return str(value) if value is not None else None

    @property
    def latest_version(self) -> str | None:
        """Latest available dashboard content version (falls back to installed)."""
        value = self.remote_versions.get("dashboard")
        return str(value) if value is not None else self.installed_version

    @property
    def update_available(self) -> bool:
        """True when the remote dashboard version is newer than installed."""
        return _version_tuple(self.latest_version) > _version_tuple(
            self.installed_version
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the remote versions.json."""
        try:
            text = await self.github.async_get_text(VERSIONS_FILE)
        except GitHubError as err:
            raise UpdateFailed(str(err)) from err
        try:
            data = json.loads(text)
        except ValueError as err:
            raise UpdateFailed(f"Invalid versions.json: {err}") from err
        return data if isinstance(data, dict) else {}

    async def async_install_now(self) -> None:
        """Download the latest dashboard content and (re)install it."""
        from . import dashboard  # lazy import to avoid a cycle

        remote = self.remote_versions or await self._async_update_data()
        installed = await dashboard.async_download_and_install(
            self.hass, self.github, remote
        )
        if installed is not None:
            self._state.setdefault("versions", {})["dashboard"] = installed
            await self._store.async_save(self._state)
            self.async_update_listeners()
            # Let clients (Ted's Cards) auto-refresh once now the files have changed.
            self.hass.bus.async_fire(
                EVENT_DASHBOARD_UPDATED, {"version": self.installed_version}
            )
