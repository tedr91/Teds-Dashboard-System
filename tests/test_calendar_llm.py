"""Tests for the device-scoped TDS calendar LLM tool."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace


class _Schema:
    def __init__(self, schema):
        self.schema = schema

    def __call__(self, data):
        return {"range": data.get("range", "month")}


def _install_stubs() -> None:
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Schema = _Schema
    voluptuous.Optional = lambda key, default=None: key
    voluptuous.In = lambda values: values
    sys.modules["voluptuous"] = voluptuous

    calendar = types.ModuleType("homeassistant.components.calendar")
    calendar.DOMAIN = "calendar"
    calendar.SERVICE_GET_EVENTS = "get_events"

    llm_component = types.ModuleType("homeassistant.components.llm")

    class LLMTools:
        def __init__(self, *, tools):
            self.tools = tools

    llm_component.LLMTools = LLMTools
    components = types.ModuleType("homeassistant.components")
    components.calendar = calendar
    components.llm = llm_component

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda function: function

    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.async_get = lambda _hass: None
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.device_registry = device_registry

    llm_helpers = types.ModuleType("homeassistant.helpers.llm")
    llm_helpers.LLM_API_ASSIST = "assist"
    llm_helpers.LLMContext = object
    llm_helpers.Tool = object
    llm_helpers.ToolInput = object

    dt_util = types.ModuleType("homeassistant.util.dt")
    dt_util.now = lambda: datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    util = types.ModuleType("homeassistant.util")
    util.dt = dt_util
    json_util = types.ModuleType("homeassistant.util.json")
    json_util.JsonObjectType = dict

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.components = components
    homeassistant.core = core
    homeassistant.helpers = helpers
    homeassistant.util = util
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.components": components,
            "homeassistant.components.calendar": calendar,
            "homeassistant.components.llm": llm_component,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.device_registry": device_registry,
            "homeassistant.helpers.llm": llm_helpers,
            "homeassistant.util": util,
            "homeassistant.util.dt": dt_util,
            "homeassistant.util.json": json_util,
        }
    )

    package = types.ModuleType("custom_components.teds_dashboard_system")
    package.__path__ = []
    sys.modules[package.__name__] = package
    const = types.ModuleType("custom_components.teds_dashboard_system.const")
    const.DOMAIN = "teds_dashboard_system"
    sys.modules[const.__name__] = const


def _load(name: str, filename: str):
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "teds_dashboard_system"
        / filename
    )
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_llm_module():
    _install_stubs()
    _load(
        "custom_components.teds_dashboard_system.calendar_scope",
        "calendar_scope.py",
    )
    return _load("custom_components.teds_dashboard_system.llm", "llm.py")


llm_module = _load_llm_module()


class _States:
    def get(self, entity_id):
        return SimpleNamespace(entity_id=entity_id)


class _Services:
    def __init__(self) -> None:
        self.calls = []

    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, data, kwargs))
        return {
            "calendar.work": {
                "events": [
                    {
                        "start": "2026-08-22T10:00:00+00:00",
                        "end": "2026-08-22T11:00:00+00:00",
                        "summary": "Workshop",
                    }
                ]
            }
        }


class _FailingServices:
    async def async_call(self, *args, **kwargs):
        raise RuntimeError("calendar unavailable")


def test_tool_uses_originating_devices_calendar_subset(monkeypatch) -> None:
    manager = SimpleNamespace(
        settings={
            "global": {
                "calendars_list": ["calendar.family", "calendar.work"],
                "calendar_options": {},
            },
            "devices": {"bm:office_panel": {"calendars_list": ["calendar.work"]}},
        }
    )
    hass = SimpleNamespace(
        data={"teds_dashboard_system": {"entry": manager}},
        states=_States(),
        services=_Services(),
    )
    device = SimpleNamespace(identifiers={("browser_mod", "office_panel")})
    registry = SimpleNamespace(async_get=lambda _device_id: device)
    monkeypatch.setattr(llm_module.dr, "async_get", lambda _hass: registry)
    context = SimpleNamespace(device_id="ha-office-device", context="context")

    tools = llm_module.async_get_tools(hass, context, "assist")

    assert tools is not None
    assert len(tools.tools) == 1
    tool = tools.tools[0]
    assert tool.name == "TedsCalendarGetEvents"
    assert tool._calendars == ["calendar.work"]

    result = asyncio.run(
        tool.async_call(
            hass,
            SimpleNamespace(tool_args={"range": "week"}),
            context,
        )
    )

    assert result["success"] is True
    assert result["result"][0]["calendar"] == "calendar.work"
    domain, service, data, kwargs = hass.services.calls[0]
    assert (domain, service) == ("calendar", "get_events")
    assert data["entity_id"] == ["calendar.work"]
    assert kwargs["context"] == "context"
    assert kwargs["return_response"] is True


def test_tool_is_not_offered_without_selected_calendars(monkeypatch) -> None:
    manager = SimpleNamespace(
        settings={"global": {"calendars_list": []}, "devices": {}}
    )
    hass = SimpleNamespace(
        data={"teds_dashboard_system": {"entry": manager}},
        states=_States(),
    )
    monkeypatch.setattr(
        llm_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _device_id: None),
    )

    assert (
        llm_module.async_get_tools(
            hass,
            SimpleNamespace(device_id=None),
            "assist",
        )
        is None
    )


def test_tool_returns_structured_failure_when_calendar_service_fails() -> None:
    tool = llm_module.TedsCalendarGetEventsTool(["calendar.work"])
    hass = SimpleNamespace(services=_FailingServices())

    result = asyncio.run(
        tool.async_call(
            hass,
            SimpleNamespace(tool_args={}),
            SimpleNamespace(context="context"),
        )
    )

    assert result == {
        "success": False,
        "error": "The selected calendars are unavailable",
    }
