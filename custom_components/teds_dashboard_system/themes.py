"""Install the bundled Ted's Themes into ``<config>/themes/`` (coexistence-safe).

Writes each bundled theme yaml into the Home Assistant themes directory, but
never overwrites a theme file the integration did not create — so a standalone
HACS "Ted's Themes" install (or the user's own themes) always wins. The set of
files we created is tracked in the installer store (``versions[ASSET_THEMES]``)
so updates and uninstall can target only our own files. After any change,
``frontend.reload_themes`` hot-applies the new themes.
"""

from __future__ import annotations

import logging
import os
import shutil

from homeassistant.core import HomeAssistant

from .const import ASSET_THEMES, THEMES_DIR
from .updater import async_load_installer_state

_LOGGER = logging.getLogger(__name__)

_VERSION_FILE = "version.txt"


def _bundled_themes_dir() -> str:
    return os.path.join(os.path.dirname(__file__), THEMES_DIR)


def _bundled_themes_version() -> str:
    try:
        path = os.path.join(_bundled_themes_dir(), _VERSION_FILE)
        with open(path, encoding="utf-8") as fh:
            if version := fh.read().strip():
                return version
    except OSError:
        pass
    return "0"


def _write_themes(
    src_dir: str, dest_dir: str, recorded: dict[str, str], version: str
) -> tuple[bool, dict[str, str]]:
    """Copy bundled theme files into ``dest_dir`` without clobbering foreign files.

    Returns ``(changed, updated_record)``. Runs in the executor (blocking I/O).
    """
    os.makedirs(dest_dir, exist_ok=True)
    changed = False
    src_names = {n for n in os.listdir(src_dir) if n.endswith((".yaml", ".yml"))}
    for name in sorted(src_names):
        dest = os.path.join(dest_dir, name)
        exists = os.path.isfile(dest)
        ours = name in recorded
        if exists and not ours:
            _LOGGER.info(
                "Theme %s already present in themes/; leaving it (managed "
                "elsewhere, e.g. HACS or user).",
                name,
            )
            continue
        if exists and ours and recorded.get(name) == version:
            continue  # already up to date
        shutil.copyfile(os.path.join(src_dir, name), dest)
        recorded[name] = version
        changed = True
    # Remove themes we previously installed that are no longer bundled (renamed or
    # dropped) so a rename doesn't leave a stale theme on the user's system.
    for name in [n for n in recorded if n not in src_names]:
        dest = os.path.join(dest_dir, name)
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except OSError:
            _LOGGER.debug("Could not remove orphaned theme %s", name)
        recorded.pop(name, None)
        changed = True
    return changed, recorded


async def _reload_themes(hass: HomeAssistant) -> None:
    try:
        await hass.services.async_call("frontend", "reload_themes", {}, blocking=True)
    except Exception:  # noqa: BLE001 - best-effort hot reload
        _LOGGER.debug("frontend.reload_themes unavailable; themes apply on next restart")


async def async_install_bundled_themes(hass: HomeAssistant) -> None:
    """Install/update the bundled Ted's Themes into ``<config>/themes/``."""
    src_dir = _bundled_themes_dir()
    if not await hass.async_add_executor_job(os.path.isdir, src_dir):
        return
    store, state = await async_load_installer_state(hass)
    versions: dict = state.setdefault("versions", {})
    recorded = dict(versions.get(ASSET_THEMES, {}))
    version = await hass.async_add_executor_job(_bundled_themes_version)
    dest_dir = hass.config.path("themes")
    changed, new_record = await hass.async_add_executor_job(
        _write_themes, src_dir, dest_dir, recorded, version
    )
    if changed:
        versions[ASSET_THEMES] = new_record
        await store.async_save(state)
        await _reload_themes(hass)
        _LOGGER.info("Installed/updated %d Ted's Themes file(s)", len(new_record))
