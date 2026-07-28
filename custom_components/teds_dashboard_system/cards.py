"""Serve + auto-load the bundled Ted's Cards frontend bundle.

Registers the integration's bundled ``ted-cards.js`` as a static path and, unless
a standalone HACS "Ted's Cards" install is detected, auto-loads it on every
dashboard via ``frontend.add_extra_js_url`` — so a user who installs only Ted's
Dashboard System still gets the cards (Scenario 1) without a manual Lovelace
resource, while an existing Ted's Cards install (Scenario 2) is left to win.
"""

from __future__ import annotations

import logging
import os

from homeassistant.core import HomeAssistant

from .const import CARDS_JS_NAME, CARDS_URL, DOMAIN, FRONTEND_DIR

_LOGGER = logging.getLogger(__name__)

_FRONTEND_URL = f"/{DOMAIN}/{FRONTEND_DIR}"
_VERSION_FILE = "version.txt"
_STATIC_FLAG = f"{DOMAIN}_cards_static_registered"


def _frontend_dir() -> str:
    return os.path.join(os.path.dirname(__file__), FRONTEND_DIR)


def _bundled_version(fallback: str | None) -> str:
    """Read the bundled cards version (for cache-busting), else the fallback."""
    try:
        with open(os.path.join(_frontend_dir(), _VERSION_FILE), encoding="utf-8") as fh:
            if version := fh.read().strip():
                return version
    except OSError:
        pass
    return fallback or "0"


def _teds_cards_already_provided(hass: HomeAssistant) -> bool:
    """True when a standalone Ted's Cards is already installed (HACS www/community)."""
    community = hass.config.path("www", "community")
    try:
        for name in os.listdir(community):
            if os.path.isfile(os.path.join(community, name, CARDS_JS_NAME)):
                return True
    except OSError:
        pass
    return False


async def _cards_resource_registered(hass: HomeAssistant) -> bool:
    """True when a standalone Ted's Cards is registered as a Lovelace resource.

    Covers a manual install added as a resource at a path outside ``www/community``;
    our own served bundle (``CARDS_URL``) is excluded.
    """
    from .requirements import _resource_urls

    urls = await _resource_urls(hass)
    if not urls:
        return False
    ours = CARDS_URL.lower()
    needle = CARDS_JS_NAME.lower()
    return any(needle in u and ours not in u for u in urls)


async def async_setup_cards(
    hass: HomeAssistant, fallback_version: str | None
) -> str | None:
    """Serve the bundled cards; auto-load them unless already provided.

    Returns the ``extra_js_url`` that was added (so unload can remove exactly it),
    or ``None`` if nothing was bundled or auto-load was deferred.
    """
    directory = _frontend_dir()
    bundle = os.path.join(directory, CARDS_JS_NAME)
    if not await hass.async_add_executor_job(os.path.isfile, bundle):
        _LOGGER.debug("No bundled %s found; not serving cards", CARDS_JS_NAME)
        return None

    if not hass.data.get(_STATIC_FLAG):
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(_FRONTEND_URL, directory, True)]
            )
            hass.data[_STATIC_FLAG] = True
        except Exception:  # noqa: BLE001 - fall back for older HA cores
            try:
                hass.http.register_static_path(_FRONTEND_URL, directory, True)
                hass.data[_STATIC_FLAG] = True
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Could not register the Ted's Cards static path")
                return None

    provided = await hass.async_add_executor_job(_teds_cards_already_provided, hass)
    if not provided:
        provided = await _cards_resource_registered(hass)
    if provided:
        _LOGGER.info(
            "A standalone Ted's Cards install was detected; deferring to it "
            "(not auto-loading the bundled copy)."
        )
        return None

    from homeassistant.components.frontend import add_extra_js_url

    version = await hass.async_add_executor_job(_bundled_version, fallback_version)
    url = f"{CARDS_URL}?v={version}"
    add_extra_js_url(hass, url)
    _LOGGER.info("Auto-loading bundled Ted's Cards at %s", url)
    return url


def async_unload_cards(hass: HomeAssistant, url: str | None) -> None:
    """Remove the auto-loaded cards module URL (if one was added)."""
    if not url:
        return
    try:
        from homeassistant.components.frontend import remove_extra_js_url

        remove_extra_js_url(hass, url)
    except Exception:  # noqa: BLE001
        pass
