"""Update coordinator + persisted installer state for Ted's Dashboard System.

The coordinator polls the public Ted's Dashboard content repo for its latest
release (every ``UPDATE_POLL_INTERVAL_HOURS``) and, together with a small
``Store``, tracks which release tag and per-asset versions are currently
installed. The actual download/compose/register work lives in the installer
(added in later phases); this module only handles *state* and *availability*.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    INSTALLER_STORAGE_KEY,
    INSTALLER_STORAGE_VERSION,
    UPDATE_POLL_INTERVAL_HOURS,
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


class DashboardUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the Ted's Dashboard repo for the latest release; track installed state."""

    def __init__(
        self,
        hass: HomeAssistant,
        github: GitHubClient,
        store: Store,
        data: dict[str, Any],
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
        self._installed_tag: str | None = data.get("installed_tag")
        self._versions: dict[str, Any] = data.get("versions", {})

    @property
    def installed_tag(self) -> str | None:
        """Release tag currently installed on this system (None before first install)."""
        return self._installed_tag

    @property
    def latest_tag(self) -> str | None:
        """Latest release tag available in the repo (falls back to installed)."""
        return (self.data or {}).get("tag") or self._installed_tag

    @property
    def release_notes(self) -> str | None:
        """Body of the latest release, if any."""
        return (self.data or {}).get("body") or None

    @property
    def versions(self) -> dict[str, Any]:
        """Per-asset versions recorded at the last install (see ``versions.json``)."""
        return self._versions

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the latest release metadata (tag + notes)."""
        try:
            latest = await self.github.async_latest_release()
        except GitHubError as err:
            raise UpdateFailed(str(err)) from err
        if latest is None:
            return {"tag": self._installed_tag, "body": ""}
        tag, body = latest
        return {"tag": tag, "body": body}

    async def async_record_install(
        self, tag: str | None, versions: dict[str, Any]
    ) -> None:
        """Persist the installed release tag + per-asset versions after an install."""
        self._installed_tag = tag
        self._versions = versions
        await self._store.async_save({"installed_tag": tag, "versions": versions})
        self.async_update_listeners()
