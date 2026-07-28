"""Dashboard override-layer services (the user overlay).

Lets the frontend Settings card manage the user's dashboard customizations
without ever touching the managed content:

* ``dashboard_customize_view`` — fork a shipped view (copy it into
  ``ted-dashboard-user/overrides/``; the fork wins and stops receiving upstream
  updates; the installed version at fork time is recorded for drift tracking).
* ``dashboard_revert_view`` — drop the fork and return to the shipped view.
* ``dashboard_add_custom_view`` — scaffold a new user view.
* ``dashboard_remove_custom_view`` — delete a user view.

Every change recomposes the generated main file so the include list updates.
"""

from __future__ import annotations

import logging
import os
import shutil

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from . import dashboard
from .const import (
    DASHBOARD_MANAGED_DIR,
    DASHBOARD_USER_DIR,
    DASHBOARDS_DIR,
    DOMAIN,
)
from .updater import async_load_installer_state

_LOGGER = logging.getLogger(__name__)

SERVICE_CUSTOMIZE_VIEW = "dashboard_customize_view"
SERVICE_REVERT_VIEW = "dashboard_revert_view"
SERVICE_ADD_CUSTOM_VIEW = "dashboard_add_custom_view"
SERVICE_REMOVE_CUSTOM_VIEW = "dashboard_remove_custom_view"
SERVICE_SET_LAYOUT = "dashboard_set_layout"

_MANAGED_VIEW_SUBDIRS = ("views", "views-home")

_CUSTOM_VIEW_TEMPLATE = (
    "# Custom view — yours; never modified by Ted's Dashboard System updates.\n"
    "title: {title}\n"
    "path: {path}\n"
    "icon: {icon}\n"
    "cards:\n"
    "  - type: custom:ted-messagebox-card\n"
    "    severity: info\n"
    "    title: {title}\n"
    "    message: Your new custom view — add cards here.\n"
)


def _safe_name(name: str) -> str:
    """Return a traversal-safe ``*.yaml`` basename."""
    base = os.path.basename(str(name).strip())
    if not base.endswith((".yaml", ".yml")):
        base = f"{base}.yaml"
    return base


def _dashboards_dir(hass: HomeAssistant) -> str:
    return os.path.join(hass.config.config_dir, DASHBOARDS_DIR)


def _user_dir(hass: HomeAssistant, *parts: str) -> str:
    return os.path.join(_dashboards_dir(hass), DASHBOARD_USER_DIR, *parts)


def _managed_view_path(hass: HomeAssistant, name: str) -> str | None:
    for sub in _MANAGED_VIEW_SUBDIRS:
        path = os.path.join(_dashboards_dir(hass), DASHBOARD_MANAGED_DIR, sub, name)
        if os.path.isfile(path):
            return path
    return None


def async_register_services(hass: HomeAssistant) -> None:
    """Register the override-layer services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_CUSTOMIZE_VIEW):
        return

    async def customize_view(call: ServiceCall) -> None:
        name = _safe_name(call.data["view"])
        src = await hass.async_add_executor_job(_managed_view_path, hass, name)
        if src is None:
            _LOGGER.warning("Cannot customize unknown view %s", name)
            return
        dest = _user_dir(hass, "overrides", name)
        await hass.async_add_executor_job(_copy_file, src, dest)
        store, state = await async_load_installer_state(hass)
        stem = os.path.splitext(name)[0]
        installed_view_ver = (
            state.get("versions", {}).get("dashboard", {}).get("views", {}).get(stem)
        )
        state.setdefault("forks", {})[name] = installed_view_ver
        await store.async_save(state)
        await dashboard.async_recompose(hass)

    async def revert_view(call: ServiceCall) -> None:
        name = _safe_name(call.data["view"])
        await hass.async_add_executor_job(_remove_file, _user_dir(hass, "overrides", name))
        store, state = await async_load_installer_state(hass)
        state.setdefault("forks", {}).pop(name, None)
        await store.async_save(state)
        await dashboard.async_recompose(hass)

    async def add_custom_view(call: ServiceCall) -> None:
        name = _safe_name(call.data["name"])
        stem = os.path.splitext(name)[0]
        title = call.data.get("title") or stem
        icon = call.data.get("icon") or "mdi:view-dashboard-variant"
        text = _CUSTOM_VIEW_TEMPLATE.format(title=title, path=stem, icon=icon)
        await hass.async_add_executor_job(
            _write_if_absent, _user_dir(hass, "views", name), text
        )
        await dashboard.async_recompose(hass)

    async def remove_custom_view(call: ServiceCall) -> None:
        name = _safe_name(call.data["name"])
        await hass.async_add_executor_job(_remove_file, _user_dir(hass, "views", name))
        await dashboard.async_recompose(hass)

    async def set_layout(call: ServiceCall) -> None:
        """Persist the user's view order + hidden list (basenames) and recompose."""
        store, state = await async_load_installer_state(hass)
        layout = state.setdefault("layout", {})
        if "hidden" in call.data:
            layout["hidden"] = [os.path.basename(str(x)) for x in call.data["hidden"]]
        if "order" in call.data:
            layout["order"] = [os.path.basename(str(x)) for x in call.data["order"]]
        await store.async_save(state)
        await dashboard.async_recompose(hass)

    view_schema = vol.Schema({vol.Required("view"): cv.string})
    add_schema = vol.Schema(
        {
            vol.Required("name"): cv.string,
            vol.Optional("title"): cv.string,
            vol.Optional("icon"): cv.string,
        }
    )
    name_schema = vol.Schema({vol.Required("name"): cv.string})
    layout_schema = vol.Schema(
        {vol.Optional("hidden"): [cv.string], vol.Optional("order"): [cv.string]}
    )

    hass.services.async_register(DOMAIN, SERVICE_CUSTOMIZE_VIEW, customize_view, schema=view_schema)
    hass.services.async_register(DOMAIN, SERVICE_REVERT_VIEW, revert_view, schema=view_schema)
    hass.services.async_register(DOMAIN, SERVICE_ADD_CUSTOM_VIEW, add_custom_view, schema=add_schema)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_CUSTOM_VIEW, remove_custom_view, schema=name_schema)
    hass.services.async_register(DOMAIN, SERVICE_SET_LAYOUT, set_layout, schema=layout_schema)


# -- blocking filesystem helpers (run in the executor) ---------------------


def _copy_file(src: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _write_if_absent(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        return
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
