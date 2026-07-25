"""Custom Assist intents for Ted's Dashboard System.

Registers voice/conversation intents for managing alarms and notifications,
backed by the existing :class:`TedsManager` services. Requests are scoped to the
voice satellite's area (or a spoken area) so the same phrase works from any
device — and, in the future, from a Ted's Dashboard device acting as a
satellite.

The matching *sentences* live in ``sentences/en.yaml`` and are installed into
``<config>/custom_sentences/en/`` at setup (that's the only folder the default
conversation agent auto-loads).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    intent,
)
import voluptuous as vol

from .const import (
    DOMAIN,
    EVENT_NAVIGATE,
    INTENT_ADD_ALARM,
    INTENT_CLEAR_NOTIFICATIONS,
    INTENT_DISABLE_ALARM,
    INTENT_ENABLE_ALARM,
    INTENT_LIST_ALARMS,
    INTENT_MARK_NOTIFICATIONS_READ,
    INTENT_NAVIGATE,
    INTENT_READ_NOTIFICATIONS,
    INTENT_REMOVE_ALARM,
    INTENT_WEATHER,
)

_REGISTERED = f"{DOMAIN}_intents_registered"

# Ted's alarms store weekdays as Python weekday ints (Monday = 0).
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_TOKEN_TO_INT = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]
_WEEKDAYS = [0, 1, 2, 3, 4]
_WEEKENDS = [5, 6]


@callback
def async_register_intents(hass: HomeAssistant) -> None:
    """Register Ted's custom Assist intent handlers once."""
    if hass.data.get(_REGISTERED):
        return
    intent.async_register(hass, AddAlarmIntent())
    intent.async_register(hass, ListAlarmsIntent())
    intent.async_register(hass, SetAlarmEnabledIntent(INTENT_ENABLE_ALARM, True))
    intent.async_register(hass, SetAlarmEnabledIntent(INTENT_DISABLE_ALARM, False))
    intent.async_register(hass, RemoveAlarmIntent())
    intent.async_register(hass, ReadNotificationsIntent())
    intent.async_register(hass, ClearNotificationsIntent())
    intent.async_register(hass, MarkNotificationsReadIntent())
    intent.async_register(hass, NavigateIntent())
    intent.async_register(hass, WeatherIntent())
    hass.data[_REGISTERED] = True


# ── shared helpers ──────────────────────────────────────────


def _manager(hass: HomeAssistant):
    """Return the single TedsManager (first config entry), or None."""
    return next(iter((hass.data.get(DOMAIN) or {}).values()), None)


def _slot(intent_obj: intent.Intent, name: str):
    """Return a recognized slot's value, or None when absent/empty."""
    entry = intent_obj.slots.get(name)
    if not entry:
        return None
    value = entry.get("value")
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _area_id_by_name(hass: HomeAssistant, name: str) -> str | None:
    """Resolve a spoken area name to its area_id (case-insensitive)."""
    reg = ar.async_get(hass)
    area = reg.async_get_area_by_name(name)
    if area:
        return area.id
    wanted = name.strip().casefold()
    for area in reg.async_list_areas():
        if area.name.casefold() == wanted:
            return area.id
        for alias in area.aliases:
            if alias.casefold() == wanted:
                return area.id
    return None


def _resolve_area(hass: HomeAssistant, intent_obj: intent.Intent) -> str | None:
    """Resolve the target area_id for a request.

    Priority: spoken area → the satellite's preferred area → the calling
    device's area → None (house-wide).
    """
    spoken = _slot(intent_obj, "area")
    if spoken:
        if area_id := _area_id_by_name(hass, str(spoken)):
            return area_id
    if preferred := _slot(intent_obj, "preferred_area_id"):
        return str(preferred)
    device_id = intent_obj.device_id
    if device_id and (device := dr.async_get(hass).async_get(device_id)):
        if device.area_id:
            return device.area_id
    return None


def _to_24h(hour: int, minute: int, meridiem: str | None) -> str:
    """Build an ``HH:MM`` string from a spoken hour/minute/(am|pm)."""
    h = int(hour) % 24
    m = int(minute) % 60
    if meridiem == "pm" and h < 12:
        h += 12
    elif meridiem == "am" and h == 12:
        h = 0
    return f"{h:02d}:{m:02d}"


def _spoken_time(hhmm: str) -> str:
    """Format ``HH:MM`` (24h) as a friendly 12-hour string, e.g. ``7:05 AM``."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return hhmm
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {period}"


def _days_from_set(dayset: str | None) -> list[int]:
    """Map a spoken/typed day set to Ted's weekday-int list (Monday = 0)."""
    if not dayset:
        return list(_EVERY_DAY)
    token = str(dayset).strip().casefold()
    if token in ("daily", "every day", "everyday", "all"):
        return list(_EVERY_DAY)
    if token in ("weekdays", "weekday"):
        return list(_WEEKDAYS)
    if token in ("weekends", "weekend"):
        return list(_WEEKENDS)
    if token in _DAY_TOKEN_TO_INT:
        return [_DAY_TOKEN_TO_INT[token]]
    return list(_EVERY_DAY)


def _spoken_days(days: list[int] | None) -> str:
    """Describe a weekday-int list for speech."""
    normalized = sorted(set(days or _EVERY_DAY))
    if normalized == _EVERY_DAY:
        return "every day"
    if normalized == _WEEKDAYS:
        return "on weekdays"
    if normalized == _WEEKENDS:
        return "on weekends"
    names = [_WEEKDAY_NAMES[d] for d in normalized if 0 <= d <= 6]
    return "on " + ", ".join(names) if names else "every day"


def _alarm_time_slots(intent_obj: intent.Intent) -> str | None:
    """Return an ``HH:MM`` from hour/minute/meridiem slots, if an hour was given."""
    hour = _slot(intent_obj, "hour")
    if hour is None:
        return None
    minute = _slot(intent_obj, "minute") or 0
    meridiem = _slot(intent_obj, "meridiem")
    return _to_24h(hour, minute, meridiem)


def _match_alarms(mgr, intent_obj: intent.Intent) -> list[dict]:
    """Find alarms matching a spoken label (substring) or time."""
    name = _slot(intent_obj, "name")
    if name:
        wanted = str(name).casefold()
        return [a for a in mgr.alarms if wanted in (a.get("label") or "").casefold()]
    hhmm = _alarm_time_slots(intent_obj)
    if hhmm:
        return [a for a in mgr.alarms if a.get("time") == hhmm]
    return []


def _speech(intent_obj: intent.Intent, text: str) -> intent.IntentResponse:
    response = intent_obj.create_response()
    response.async_set_speech(text)
    return response


# Slot-schema fragments. Presence of a `slot_schema` is what lets LLM
# conversation agents (OpenAI, Gemini, etc.) call these as tools with typed
# parameters — the default agent fills the same slots from spoken sentences.
# `preferred_area_id` is stripped from the LLM tool and auto-filled from the
# calling device's area (see IntentTool), giving free per-room scoping.
_TIME_SLOTS = {
    vol.Optional("hour", description="Hour of day in 24-hour format (0-23)"): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=23)
    ),
    vol.Optional("minute", description="Minute (0-59)"): vol.All(
        vol.Coerce(int), vol.Range(min=0, max=59)
    ),
    vol.Optional("meridiem", description="am or pm; omit if hour is 24-hour"): vol.In(
        ["am", "pm"]
    ),
}
_AREA_SLOTS = {
    vol.Optional("area", description="Area/room name to scope to"): cv.string,
    vol.Optional("preferred_area_id"): cv.string,
}

# Voice "view" tokens → the dashboard-path setting key the frontend resolves.
_VIEW_TO_DASHBOARD = {
    "cameras": "cameras_dashboard",
    "climate": "climate_dashboard",
    "weather": "weather_dashboard",
    "music": "music_dashboard",
    "calendar": "calendar_dashboard",
    "home": "home_dashboard",
}


def _fire_navigate(
    hass: HomeAssistant,
    dashboard_key: str,
    area: str | None,
    device_id: str | None,
) -> bool:
    """Fire a navigation signal targeting an area and/or a device.

    Returns False (no signal fired) when neither an area nor a device is known,
    since there'd be no way for a frontend to know the screen is meant for it.
    """
    if not area and not device_id:
        return False
    hass.bus.async_fire(
        EVENT_NAVIGATE,
        {"dashboard": dashboard_key, "area": area, "device_id": device_id},
    )
    return True


# ── alarm intents ───────────────────────────────────────────


class AddAlarmIntent(intent.IntentHandler):
    """Create an alarm from a spoken time (and optional day set / name)."""

    intent_type = INTENT_ADD_ALARM
    description = "Add or schedule an alarm at a given time in Ted's Cards"
    slot_schema = {
        **_TIME_SLOTS,
        vol.Optional(
            "dayset",
            description="Repeat days: 'every day', 'weekdays', 'weekends', or a weekday name",
        ): cv.string,
        vol.Optional("name", description="Optional label for the alarm"): cv.string,
        vol.Optional(
            "scope",
            description="Set to 'all' for a whole-home alarm instead of scoping it to this room",
        ): vol.In(["all"]),
        **_AREA_SLOTS,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")

        hour = _slot(intent_obj, "hour")
        if hour is None:
            return _speech(intent_obj, "What time should I set the alarm for?")

        hhmm = _to_24h(hour, _slot(intent_obj, "minute") or 0, _slot(intent_obj, "meridiem"))
        days = _days_from_set(_slot(intent_obj, "dayset"))
        whole_home = _slot(intent_obj, "scope") == "all"
        if whole_home:
            area_id = None
        else:
            area_id = _resolve_area(hass, intent_obj)
            if area_id is None:
                # No room could be determined (e.g. the browser Assist dialog
                # sends no device/area). Ask rather than silently going house-wide.
                return _speech(
                    intent_obj,
                    "Which room is this alarm for? Say the room name, "
                    "or say 'whole home' for a house-wide alarm.",
                )
        label = _slot(intent_obj, "name") or f"{_spoken_time(hhmm)} alarm"

        await mgr.add_alarm(str(label), hhmm, days, location=area_id)
        if whole_home:
            where = " for the whole home"
        elif area_id and (area := ar.async_get(hass).async_get_area(area_id)):
            where = f" in {area.name}"
        else:
            where = ""
        return _speech(
            intent_obj,
            f"Alarm set for {_spoken_time(hhmm)} {_spoken_days(days)}{where}.",
        )


class ListAlarmsIntent(intent.IntentHandler):
    """Read back the alarms for the current area (plus house-wide)."""

    intent_type = INTENT_LIST_ALARMS
    description = "List the alarms in Ted's Cards"
    slot_schema = {**_AREA_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")

        area_id = _resolve_area(hass, intent_obj)
        alarms = [
            a for a in mgr.alarms
            if a.get("location") in (None, area_id)
        ]
        if not alarms:
            return _speech(intent_obj, "You have no alarms set.")

        alarms.sort(key=lambda a: (not a.get("enabled"), a.get("time") or ""))
        parts = []
        for a in alarms:
            state = "" if a.get("enabled") else " (disabled)"
            parts.append(
                f"{a.get('label') or 'Alarm'} at {_spoken_time(a.get('time') or '')}"
                f" {_spoken_days(a.get('days'))}{state}"
            )
        count = len(alarms)
        noun = "alarm" if count == 1 else "alarms"
        return _speech(intent_obj, f"You have {count} {noun}: " + "; ".join(parts) + ".")


class SetAlarmEnabledIntent(intent.IntentHandler):
    """Enable or disable a matched alarm."""

    # `slot_schema` is a read-only property on the base class, so it must be
    # overridden as a class attribute (not set on the instance in __init__).
    slot_schema = {
        vol.Optional("name", description="Label of the alarm to match"): cv.string,
        **_TIME_SLOTS,
    }

    def __init__(self, intent_type: str, enabled: bool) -> None:
        self.intent_type = intent_type
        self._enabled = enabled
        self.description = (
            "Enable an alarm in Ted's Cards" if enabled
            else "Disable an alarm in Ted's Cards"
        )

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")

        word = "enable" if self._enabled else "disable"
        matches = _match_alarms(mgr, intent_obj)
        if not matches:
            return _speech(intent_obj, f"I couldn't find an alarm to {word}.")
        if len(matches) > 1:
            return _speech(
                intent_obj,
                f"You have {len(matches)} matching alarms — please be more specific.",
            )

        alarm = matches[0]
        await mgr.update_alarm(alarm["id"], enabled=self._enabled)
        state = "enabled" if self._enabled else "disabled"
        return _speech(
            intent_obj,
            f"{alarm.get('label') or 'Alarm'} at {_spoken_time(alarm.get('time') or '')} {state}.",
        )


class RemoveAlarmIntent(intent.IntentHandler):
    """Delete a matched alarm."""

    intent_type = INTENT_REMOVE_ALARM
    description = "Remove an alarm from Ted's Cards"
    slot_schema = {
        vol.Optional("name", description="Label of the alarm to match"): cv.string,
        **_TIME_SLOTS,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")

        matches = _match_alarms(mgr, intent_obj)
        if not matches:
            return _speech(intent_obj, "I couldn't find an alarm to remove.")
        if len(matches) > 1:
            return _speech(
                intent_obj,
                f"You have {len(matches)} matching alarms — please be more specific.",
            )

        alarm = matches[0]
        await mgr.remove_alarm(alarm["id"])
        return _speech(
            intent_obj,
            f"Removed the {_spoken_time(alarm.get('time') or '')} alarm.",
        )


# ── notification intents ────────────────────────────────────


def _notifications_for_area(mgr, area_id: str | None) -> list[dict]:
    """Notifications relevant to an area: house-wide plus that area (all if none)."""
    if area_id is None:
        return list(mgr.notifications)
    return [n for n in mgr.notifications if n.get("area") in (None, area_id)]


class ReadNotificationsIntent(intent.IntentHandler):
    """Read out the current notifications for this area."""

    intent_type = INTENT_READ_NOTIFICATIONS
    description = "Read Ted's Cards notifications"
    slot_schema = {**_AREA_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")

        area_id = _resolve_area(hass, intent_obj)
        items = _notifications_for_area(mgr, area_id)
        if not items:
            return _speech(intent_obj, "You have no notifications.")

        parts = []
        for n in items:
            title = (n.get("title") or "").strip()
            message = (n.get("message") or "").strip()
            if title and message:
                parts.append(f"{title}: {message}")
            else:
                parts.append(title or message)
        count = len(items)
        noun = "notification" if count == 1 else "notifications"
        return _speech(intent_obj, f"You have {count} {noun}. " + ". ".join(parts) + ".")


class ClearNotificationsIntent(intent.IntentHandler):
    """Clear notifications for this area (or everywhere)."""

    intent_type = INTENT_CLEAR_NOTIFICATIONS
    description = "Clear Ted's Cards notifications"
    slot_schema = {
        **_AREA_SLOTS,
        vol.Optional(
            "scope", description="Set to 'all' to clear notifications everywhere"
        ): vol.In(["all"]),
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")

        # "clear all notifications everywhere" forces a house-wide clear.
        force_all = _slot(intent_obj, "scope") == "all"
        area_id = None if force_all else _resolve_area(hass, intent_obj)
        await mgr.clear_notifications(area_id)
        where = "" if area_id is None else " here"
        return _speech(intent_obj, f"Cleared your notifications{where}.")


class MarkNotificationsReadIntent(intent.IntentHandler):
    """Mark notifications for this area (or everywhere) as read."""

    intent_type = INTENT_MARK_NOTIFICATIONS_READ
    description = "Mark Ted's Cards notifications as read"
    slot_schema = {
        **_AREA_SLOTS,
        vol.Optional(
            "scope", description="Set to 'all' to mark every area's notifications read"
        ): vol.In(["all"]),
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")

        force_all = _slot(intent_obj, "scope") == "all"
        area_id = None if force_all else _resolve_area(hass, intent_obj)
        await mgr.mark_read(None, area_id)
        return _speech(intent_obj, "Marked your notifications as read.")


# ── navigation intents ──────────────────────────────────────


def _weather_entity(hass: HomeAssistant) -> str | None:
    """The configured weather entity (global setting) or the first weather.* entity."""
    mgr = _manager(hass)
    if mgr is not None:
        configured = (mgr.effective_settings() or {}).get("weather_entity")
        if configured:
            return str(configured)
    for state in hass.states.async_all("weather"):
        return state.entity_id
    return None


class NavigateIntent(intent.IntentHandler):
    """Navigate the caller's dashboard screen(s) to a Ted's Cards view."""

    intent_type = INTENT_NAVIGATE
    description = (
        "Show a Ted's Cards dashboard view (cameras, climate, weather, music, calendar, home)"
    )
    slot_schema = {
        vol.Required("view"): vol.In(sorted(_VIEW_TO_DASHBOARD)),
        **_AREA_SLOTS,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        view = _slot(intent_obj, "view")
        dashboard_key = _VIEW_TO_DASHBOARD.get(str(view))
        if not dashboard_key:
            return _speech(intent_obj, "I don't know that view.")
        area_id = _resolve_area(hass, intent_obj)
        if not _fire_navigate(hass, dashboard_key, area_id, intent_obj.device_id):
            return _speech(
                intent_obj,
                "I'm not sure which screen to show — try asking from a room device.",
            )
        return _speech(intent_obj, f"Showing {view}.")


class WeatherIntent(intent.IntentHandler):
    """Answer the current weather and nudge the screen to the Weather view."""

    intent_type = INTENT_WEATHER
    description = "Report the current weather and show the Weather view"
    slot_schema = {**_AREA_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        # Nudge the screen to the Weather view when a target can be resolved.
        _fire_navigate(
            hass, "weather_dashboard", _resolve_area(hass, intent_obj), intent_obj.device_id
        )

        entity_id = _weather_entity(hass)
        if not entity_id:
            return _speech(intent_obj, "I couldn't find a weather entity.")
        state = hass.states.get(entity_id)
        if state is None:
            return _speech(intent_obj, "Weather is unavailable right now.")
        condition = (state.state or "unknown").replace("_", " ").replace("-", " ")
        temp = state.attributes.get("temperature")
        unit = state.attributes.get("temperature_unit") or ""
        if temp is not None:
            return _speech(intent_obj, f"It's currently {condition}, {temp}{unit}.")
        return _speech(intent_obj, f"It's currently {condition}.")
