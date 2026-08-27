"""Tests for the slimmed EVENT_SETTINGS payload and explicit device pruning.

The full settings snapshot used to ride on the event bus, where it exceeded the
recorder's 32 KB event-data limit and logged a warning on every settings change. It is
now stashed on the manager and pushed to subscribers by the WebSocket forwarder, so the
client wire format is unchanged while the bus event stays tiny.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

COMPONENT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "teds_dashboard_system"
)


def _identity_decorator(_schema=None):
    return lambda function: function


def _clear_homeassistant() -> None:
    for module_name in tuple(sys.modules):
        if module_name == "homeassistant" or module_name.startswith("homeassistant."):
            sys.modules.pop(module_name, None)


def _take_component() -> dict:
    """Detach component modules another test module registered, returning them."""
    saved = {}
    for module_name in tuple(sys.modules):
        if module_name.startswith("custom_components.teds_dashboard_system"):
            saved[module_name] = sys.modules.pop(module_name)
    return saved


def _restore_component(saved: dict) -> None:
    """Drop our temporary copies and put the other module's registrations back.

    Leaving our copies in place would break ``mock.patch`` targets in test modules
    that loaded the same component module against their own stubs.
    """
    for module_name in tuple(sys.modules):
        if module_name.startswith("custom_components.teds_dashboard_system"):
            sys.modules.pop(module_name, None)
    sys.modules.update(saved)


def _exec(module_name: str):
    path = COMPONENT / f"{module_name}.py"
    name = f"custom_components.teds_dashboard_system.{module_name}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_store():
    """Load store.py against stubbed HA, with the real sibling modules."""
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda function: function
    storage = types.ModuleType("homeassistant.helpers.storage")

    class _Store:
        def __init__(self, *args, **kwargs) -> None:
            self.saved: dict | None = None

        async def async_load(self):
            return {}

        async def async_save(self, data):
            self.saved = data

    storage.Store = _Store
    event = types.ModuleType("homeassistant.helpers.event")
    event.async_call_later = lambda *args, **kwargs: (lambda: None)
    event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
    network = types.ModuleType("homeassistant.helpers.network")
    network.NoURLAvailableError = Exception
    network.get_url = lambda *args, **kwargs: "http://localhost:8123"
    area_registry = types.ModuleType("homeassistant.helpers.area_registry")
    dt_util = types.ModuleType("homeassistant.util.dt")
    dt_util.utcnow = lambda: datetime.now(timezone.utc)

    def _parse_datetime(value):
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    dt_util.parse_datetime = _parse_datetime
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.area_registry = area_registry
    helpers.event = event
    helpers.network = network
    helpers.storage = storage
    util = types.ModuleType("homeassistant.util")
    util.dt = dt_util
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.core = core
    homeassistant.helpers = helpers
    homeassistant.util = util
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.area_registry": area_registry,
            "homeassistant.helpers.event": event,
            "homeassistant.helpers.network": network,
            "homeassistant.helpers.storage": storage,
            "homeassistant.util": util,
            "homeassistant.util.dt": dt_util,
        }
    )

    package = types.ModuleType("custom_components.teds_dashboard_system")
    package.__path__ = [str(COMPONENT)]
    saved = _take_component()
    sys.modules["custom_components.teds_dashboard_system"] = package
    module = _exec("store")
    _restore_component(saved)
    _clear_homeassistant()
    return module


def _load_websocket():
    """Load websocket.py against stubbed HA (mirrors tests/test_websocket_device_name)."""
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
    websocket_api.event_message = lambda msg_id, data: {"id": msg_id, "event": data}
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
    saved = _take_component()
    sys.modules["custom_components.teds_dashboard_system"] = package
    imports = {
        "bing_photos": (
            "clear_bing_cache", "favorite_bing_photo", "fetch_and_cache_bing",
            "import_photo", "list_favorites", "remove_bing_photo",
        ),
        "calendar_scope": ("tds_device_id",),
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

    module = _exec("websocket")
    _restore_component(saved)
    _clear_homeassistant()
    return module


store_module = _load_store()
websocket_module = _load_websocket()


class _Bus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event_type, data):
        self.fired.append((event_type, data))


def _manager():
    """A TedsManager with the debounce timer captured instead of scheduled."""
    scheduled: list = []
    bus = _Bus()
    hass = SimpleNamespace(bus=bus, states=SimpleNamespace(get=lambda _e: None))
    manager = store_module.TedsManager.__new__(store_module.TedsManager)
    manager.hass = hass
    manager.settings = {"global": {"theme": "dark"}, "devices": {}}
    manager.device_registry = {}
    manager.frigate_cameras = {}
    manager._settings_fire_unsub = None
    manager.last_settings_payload = None
    manager.settings_revision = 0
    manager._store = SimpleNamespace()
    manager._update_cbs = set()

    async def _save():
        manager.saved = True

    manager._save = _save
    original = store_module.async_call_later
    store_module.async_call_later = lambda _hass, _delay, cb: scheduled.append(cb) or (lambda: None)
    return manager, bus, scheduled, original


def _fire(manager, scheduled):
    """Run the debounced broadcast that `_fire_settings` scheduled."""
    manager._fire_settings()
    assert scheduled, "no broadcast was scheduled"
    scheduled.pop()()


def test_settings_event_carries_only_a_revision_not_the_snapshot() -> None:
    manager, bus, scheduled, original = _manager()
    try:
        # A registry big enough that the old full-snapshot event blew the 32 KB limit.
        manager.device_registry = {
            f"bm:browser_mod_{i:08x}_{i:08x}": {
                "name": f"Panel {i}", "area": "office", "last_seen": "2026-08-26T12:00:00+00:00",
            }
            for i in range(400)
        }
        _fire(manager, scheduled)

        assert len(bus.fired) == 1
        event_type, data = bus.fired[0]
        assert event_type == store_module.EVENT_SETTINGS
        assert data == {"revision": 1}
        # Comfortably inside the recorder's 32768-byte event-data limit.
        assert len(json.dumps(data)) < 100
        # ...while the snapshot it replaced is far past it.
        assert len(json.dumps(manager.last_settings_payload)) > 32768
    finally:
        store_module.async_call_later = original


def test_stashed_snapshot_is_the_full_payload_and_revision_advances() -> None:
    manager, bus, scheduled, original = _manager()
    try:
        manager.device_registry = {"bm:surfacepro": {"name": "Surface Pro"}}
        _fire(manager, scheduled)

        payload = manager.last_settings_payload
        assert set(payload) == {"defaults", "global", "devices", "registry"}
        assert payload["registry"] == {"bm:surfacepro": {"name": "Surface Pro"}}
        assert payload["global"]["theme"] == "dark"
        assert payload == manager.settings_payload()

        manager.settings["global"]["theme"] = "light"
        _fire(manager, scheduled)

        assert bus.fired[-1][1] == {"revision": 2}
        assert manager.last_settings_payload["global"]["theme"] == "light"
    finally:
        store_module.async_call_later = original


def test_subscribers_still_receive_the_full_snapshot() -> None:
    """The client wire format must be unchanged despite the slim bus event."""
    manager, _bus, scheduled, original = _manager()
    try:
        manager.device_registry = {"bm:surfacepro": {"name": "Surface Pro"}}
        _fire(manager, scheduled)
    finally:
        store_module.async_call_later = original

    sent: list = []
    listeners: list = []
    connection = SimpleNamespace(
        subscriptions={}, send_message=sent.append, send_result=lambda *a: None
    )
    hass = SimpleNamespace(
        bus=SimpleNamespace(
            async_listen=lambda _event, cb: listeners.append(cb) or (lambda: None)
        ),
        data={"teds_dashboard_system": {"entry": manager}},
    )

    websocket_module.handle_subscribe_settings(hass, connection, {"id": 7})
    sent.clear()
    # Deliver the slim bus event; the forwarder must expand it to the full snapshot.
    listeners[0](SimpleNamespace(data={"revision": 1}))

    assert sent == [{"id": 7, "event": manager.last_settings_payload}]
    assert sent[0]["event"]["registry"] == {"bm:surfacepro": {"name": "Surface Pro"}}


def test_prune_removes_only_the_named_devices() -> None:
    manager, _bus, _scheduled, original = _manager()
    try:
        manager.device_registry = {
            "bm:orphan": {"name": None},
            "bm:asleep": {"name": "iPhone Air"},
        }
        manager.settings["devices"] = {
            "bm:orphan": {"theme": "dark"},
            "bm:asleep": {"theme": "light"},
        }

        result = asyncio.run(manager.prune_devices(["bm:orphan", "bm:never-registered"]))

        assert result["removed"] == ["bm:orphan"]
        assert result["unknown"] == ["bm:never-registered"]
        # Both structures are cleaned for the pruned id...
        assert "bm:orphan" not in manager.device_registry
        assert "bm:orphan" not in manager.settings["devices"]
        # ...and the un-named device is untouched, settings and all.
        assert manager.device_registry["bm:asleep"] == {"name": "iPhone Air"}
        assert manager.settings["devices"]["bm:asleep"] == {"theme": "light"}
    finally:
        store_module.async_call_later = original


def test_prune_with_nothing_to_do_does_not_broadcast() -> None:
    manager, bus, scheduled, original = _manager()
    try:
        manager.device_registry = {"bm:asleep": {"name": "iPhone Air"}}

        result = asyncio.run(manager.prune_devices(["bm:ghost", "", None]))

        assert result == {"removed": [], "unknown": ["bm:ghost"]}
        assert manager.device_registry == {"bm:asleep": {"name": "iPhone Air"}}
        assert scheduled == []
        assert bus.fired == []
    finally:
        store_module.async_call_later = original


def test_prune_candidates_reports_without_removing() -> None:
    manager, _bus, _scheduled, original = _manager()
    try:
        manager.device_registry = {
            "bm:browser_mod_5e2bea59_8274057d": {"name": None, "last_seen": None},
            "bm:surfacepro": {"name": "Surface Pro", "last_seen": None},
        }

        report = manager.prune_candidates({"bm:surfacepro"})

        assert report["summary"]["total"] == 2
        assert report["summary"]["likely_orphans"] == 1
        assert report["candidates"][0]["device_id"] == "bm:browser_mod_5e2bea59_8274057d"
        # Reporting must never mutate the registry.
        assert len(manager.device_registry) == 2
    finally:
        store_module.async_call_later = original
