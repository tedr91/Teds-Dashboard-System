"""Calendar entity exposing Vision Analysis events (for Assist "what happened")."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback
) -> None:
    add([TedsVisionCalendar(hass.data[DOMAIN][entry.entry_id])])


class TedsVisionCalendar(CalendarEntity):
    """Read-only calendar backed by the manager's stored vision events."""

    _attr_should_poll = False
    _attr_name = "Teds Vision Timeline"
    _attr_unique_id = "teds_vision_timeline"
    _attr_icon = "mdi:timeline-text"

    def __init__(self, manager) -> None:
        self._m = manager

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._m.register(self.async_write_ha_state))

    @property
    def event(self) -> CalendarEvent | None:
        """Most-recent event (all vision events are in the past, so state is 'off')."""
        for e in self._m.vision_events:
            ce = self._to_calendar_event(e)
            if ce is not None:
                return ce
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        out: list[CalendarEvent] = []
        for e in self._m.vision_events:
            ce = self._to_calendar_event(e)
            if ce is not None and ce.end > start_date and ce.start < end_date:
                out.append(ce)
        return out

    @staticmethod
    def _to_calendar_event(e: dict) -> CalendarEvent | None:
        start = dt_util.parse_datetime(e.get("ts_start") or "")
        if start is None:
            return None
        end = dt_util.parse_datetime(e.get("ts_end") or "") or start
        if end <= start:
            end = start + timedelta(seconds=1)
        title = e.get("short_summary") or f"{e.get('camera_name', 'Camera')} event"
        return CalendarEvent(
            start=start,
            end=end,
            summary=f"{e.get('camera_name', 'Camera')}: {title}",
            description=e.get("long_summary") or "",
        )
