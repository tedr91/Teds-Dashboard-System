"""LLM tools for Ted's Dashboard System."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast, override

import voluptuous as vol
from homeassistant.components import calendar
from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.llm import LLM_API_ASSIST, LLMContext, Tool, ToolInput
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from .calendar_scope import expanded_calendars, selected_calendars, tds_device_id
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _manager(hass: HomeAssistant):
    """Return the first configured TDS manager."""
    return next(iter((hass.data.get(DOMAIN) or {}).values()), None)


def _calendar_entities(
    hass: HomeAssistant,
    manager,
    ha_device_id: str | None,
) -> list[str]:
    """Resolve the calendars displayed by the originating TDS device."""
    global_settings = manager.settings.get("global") or {}
    device_key = None
    if ha_device_id and (device := dr.async_get(hass).async_get(ha_device_id)):
        device_key = tds_device_id(device.identifiers)
    selected = selected_calendars(
        global_settings.get("calendars_list"),
        manager.settings.get("devices") or {},
        device_key,
    )
    expanded = expanded_calendars(selected, global_settings.get("calendar_options"))
    return [entity_id for entity_id in expanded if hass.states.get(entity_id)]


class TedsCalendarGetEventsTool(Tool):
    """Query calendars selected for the requesting TDS dashboard device."""

    name = "TedsCalendarGetEvents"
    description = (
        "Get upcoming events from the calendars selected for this Ted's Dashboard "
        "device. Use this whenever the user asks about their calendar, schedule, "
        "appointments, meetings, or next event."
    )
    parameters = vol.Schema(
        {
            vol.Optional("range", default="month"): vol.In(
                ["today", "week", "month"]
            )
        }
    )

    def __init__(self, calendars: list[str]) -> None:
        """Initialize the tool with this device's selected calendars."""
        self._calendars = calendars

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: ToolInput,
        llm_context: LLMContext,
    ) -> JsonObjectType:
        """Return upcoming events from every selected calendar."""
        data = self.parameters(tool_input.tool_args)
        now = dt_util.now()
        days = {"today": 1, "week": 7, "month": 30}[data["range"]]
        try:
            response = await hass.services.async_call(
                calendar.DOMAIN,
                calendar.SERVICE_GET_EVENTS,
                {
                    "entity_id": self._calendars,
                    "start_date_time": now.isoformat(),
                    "end_date_time": (now + timedelta(days=days)).isoformat(),
                },
                context=llm_context.context,
                blocking=True,
                return_response=True,
            )
        except Exception:  # Calendar backends can fail independently.
            _LOGGER.exception("TDS calendar LLM tool could not read calendar events")
            return {"success": False, "error": "The selected calendars are unavailable"}
        events: list[dict[str, Any]] = []
        for entity_id, result in cast(
            dict[str, dict[str, Any]], response or {}
        ).items():
            for event in result.get("events") or []:
                events.append({"calendar": entity_id, **event})
        events.sort(key=lambda event: str(event.get("start") or ""))
        return {"success": True, "result": events}


@callback
def async_get_tools(
    hass: HomeAssistant,
    llm_context: LLMContext,
    api_id: str,
) -> LLMTools | None:
    """Return the device-scoped calendar tool for the Assist API."""
    if api_id != LLM_API_ASSIST:
        return None
    manager = _manager(hass)
    if manager is None:
        return None
    calendars = _calendar_entities(hass, manager, llm_context.device_id)
    if not calendars:
        return None
    return LLMTools(tools=[TedsCalendarGetEventsTool(calendars)])