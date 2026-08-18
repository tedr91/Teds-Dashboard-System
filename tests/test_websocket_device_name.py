"""Tests for the device-name WebSocket command."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _identity_decorator(_schema=None):
    return lambda function: function


def _install_stubs() -> None:
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Required = lambda key: key
    voluptuous.Optional = lambda key, default=None: key
    voluptuous.Any = lambda *values: values
    sys.modules["voluptuous"] = voluptuous

    websocket_api = types.ModuleType("homeassistant.components.websocket_api")
    websocket_api.websocket_command = _identity_decorator
    websocket_api.async_response = lambda function: function
    websocket_api.require_admin = lambda function: function
    websocket_api.ActiveConnection = object
    components = types.ModuleType("homeassistant.components")
    components.websocket_api = websocket_api
    core = types.ModuleType("homeassistant.core")
    core.Event = object
    core.HomeAssistant = object
    core.callback = lambda function: function
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = Exception
    helpers = types.ModuleType("homeassistant.helpers")
    for name in ("area_registry", "device_registry"):
        module = types.ModuleType(f"homeassistant.helpers.{name}")
        setattr(helpers, name, module)
        sys.modules[f"homeassistant.helpers.{name}"] = module
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.components = components
    homeassistant.core = core
    homeassistant.helpers = helpers
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.components": components,
            "homeassistant.components.websocket_api": websocket_api,
            "homeassistant.core": core,
            "homeassistant.exceptions": exceptions,
            "homeassistant.helpers": helpers,
        }
    )

    package = types.ModuleType("custom_components.teds_dashboard_system")
    package.__path__ = []
    sys.modules["custom_components.teds_dashboard_system"] = package
    imports = {
        "bing_photos": (
            "clear_bing_cache", "favorite_bing_photo", "fetch_and_cache_bing",
            "import_photo", "list_favorites", "remove_bing_photo",
        ),
        "frigate": ("async_mark_frigate_reviewed",),
        "vision": (
            "ai_task_entities", "discover_camera_detectors", "frigate_native_camera",
            "preferred_ai_task_entity",
        ),
    }
    for module_name, attributes in imports.items():
        module = types.ModuleType(f"custom_components.teds_dashboard_system.{module_name}")
        for attribute in attributes:
            setattr(module, attribute, MagicMock())
        sys.modules[module.__name__] = module
    const = types.ModuleType("custom_components.teds_dashboard_system.const")
    for name in (
        "DASHBOARD_USER_DIR", "DASHBOARDS_DIR", "EVENT_ASSIST_RESPONSE",
        "EVENT_BING_REMOVED", "EVENT_DASHBOARD_UPDATED", "EVENT_NAVIGATE",
        "EVENT_NOTIFICATION", "EVENT_SETTINGS", "EVENT_VISION_EVENT",
    ):
        setattr(const, name, name.lower())
    const.DOMAIN = "teds_dashboard_system"
    sys.modules[const.__name__] = const


def _load_handler():
    _install_stubs()
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "teds_dashboard_system"
        / "websocket.py"
    )
    name = "custom_components.teds_dashboard_system.websocket"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    for module_name in tuple(sys.modules):
        if module_name == "homeassistant" or module_name.startswith("homeassistant."):
            sys.modules.pop(module_name, None)
    return module.handle_set_device_name


handle_set_device_name = _load_handler()


class _Connection:
    def __init__(self, *, admin: bool) -> None:
        self.user = SimpleNamespace(is_admin=admin)
        self.send_error = MagicMock()
        self.send_result = MagicMock()


def _run_handler(*, admin: bool, allowed: bool, device, name):
    hass = SimpleNamespace(data={})
    connection = _Connection(admin=admin)
    registry = MagicMock()
    registry.async_get.return_value = device
    manager = MagicMock()
    manager.effective_settings.return_value = {
        "allow_device_area_self_assign": allowed
    }
    hass.data["teds_dashboard_system"] = {"entry": manager}
    message = {"id": 1, "device_id": "device-1", "name": name}

    with patch(
        "custom_components.teds_dashboard_system.websocket.dr.async_get",
        return_value=registry,
        create=True,
    ):
        asyncio.run(handle_set_device_name(hass, connection, message))

    return connection, registry


def test_admin_can_rename_any_device() -> None:
    device = SimpleNamespace(identifiers=frozenset({("light", "one")}))
    connection, registry = _run_handler(
        admin=True, allowed=False, device=device, name="  Hall panel  "
    )

    registry.async_update_device.assert_called_once_with(
        "device-1", name_by_user="Hall panel"
    )
    connection.send_result.assert_called_once_with(1, {"success": True})


def test_non_admin_can_rename_and_clear_panel_device() -> None:
    device = SimpleNamespace(identifiers=frozenset({("browser_mod", "panel")}))
    connection, registry = _run_handler(
        admin=False, allowed=True, device=device, name="   "
    )

    registry.async_update_device.assert_called_once_with("device-1", name_by_user=None)
    connection.send_result.assert_called_once_with(1, {"success": True})


def test_non_admin_is_rejected_when_self_assignment_is_disabled() -> None:
    device = SimpleNamespace(identifiers=frozenset({("mobile_app", "phone")}))
    connection, registry = _run_handler(
        admin=False, allowed=False, device=device, name="Phone"
    )

    connection.send_error.assert_called_once_with(
        1, "unauthorized", "Self-assignment is disabled"
    )
    registry.async_update_device.assert_not_called()


def test_non_admin_cannot_rename_other_device_classes() -> None:
    device = SimpleNamespace(identifiers=frozenset({("light", "one")}))
    connection, registry = _run_handler(
        admin=False, allowed=True, device=device, name="Light"
    )

    connection.send_error.assert_called_once_with(
        1, "unauthorized", "Device cannot be self-named"
    )
    registry.async_update_device.assert_not_called()


def test_unknown_device_and_long_name_are_rejected() -> None:
    connection, registry = _run_handler(
        admin=True, allowed=True, device=None, name="Missing"
    )
    connection.send_error.assert_called_once_with(1, "not_found", "Device not found")
    registry.async_update_device.assert_not_called()

    device = SimpleNamespace(identifiers=frozenset())
    connection, registry = _run_handler(
        admin=True, allowed=True, device=device, name="x" * 256
    )
    connection.send_error.assert_called_once_with(
        1, "invalid_format", "Device name is too long"
    )
    registry.async_update_device.assert_not_called()