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

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    intent,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .climate import apply_climate, resolve_climate_entity
from .const import (
    DOMAIN,
    EVENT_NAVIGATE,
    INTENT_ADD_ALARM,
    INTENT_ADJUST_THERMOSTAT,
    INTENT_ANNOUNCE,
    INTENT_CLEAR_NOTIFICATIONS,
    INTENT_DISABLE_ALARM,
    INTENT_ENABLE_ALARM,
    INTENT_LIST_ALARMS,
    INTENT_MARK_NOTIFICATIONS_READ,
    INTENT_NAVIGATE,
    INTENT_NEXT_ALARM,
    INTENT_NEXT_EVENT,
    INTENT_PLAY_MUSIC,
    INTENT_READ_NOTIFICATIONS,
    INTENT_REMOVE_ALARM,
    INTENT_SET_THERMOSTAT,
    INTENT_START_TIMER,
    INTENT_CANCEL_TIMER,
    INTENT_PAUSE_TIMER,
    INTENT_RESUME_TIMER,
    INTENT_ADD_TIME,
    INTENT_REMOVE_TIME,
    INTENT_TIMER_STATUS,
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
    intent.async_register(hass, PlayMusicIntent())
    intent.async_register(hass, AnnounceIntent())
    intent.async_register(hass, NextAlarmIntent())
    intent.async_register(hass, TimerStatusIntent())
    intent.async_register(hass, NextCalendarEventIntent())
    intent.async_register(hass, SetThermostatIntent())
    intent.async_register(hass, AdjustThermostatIntent())
    intent.async_register(hass, StartTimerIntent())
    intent.async_register(hass, CancelTimerIntent())
    intent.async_register(hass, PauseTimerIntent())
    intent.async_register(hass, ResumeTimerIntent())
    intent.async_register(hass, AddTimeIntent())
    intent.async_register(hass, RemoveTimeIntent())
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
    "photos": "photos_dashboard",
    "alarms": "alarms_dashboard",
    "timers": "timers_dashboard",
    "notifications": "notifications_dashboard",
    "settings": "settings_dashboard",
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
            return await _answer(hass, intent_obj, "You have no alarms set.", title="Alarms")

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
        text = f"You have {count} {noun}: " + "; ".join(parts) + "."
        return await _answer(hass, intent_obj, text, title="Alarms")


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
            return await _answer(hass, intent_obj, "You have no notifications.", title="Notifications")

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
        text = f"You have {count} {noun}. " + ". ".join(parts) + "."
        return await _answer(hass, intent_obj, text, title="Notifications")


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


# ── shared helpers for the new intents ────────────────────


async def _answer(
    hass: HomeAssistant,
    intent_obj: intent.Intent,
    text: str,
    *,
    title: str | None = None,
    image: str | None = None,
    navigate: bool = True,
) -> intent.IntentResponse:
    """Speak `text` AND mirror it onto the caller's Assist-Response screen.

    Targets the caller's area (the Assist-Response card matches by area); when no
    area can be resolved we just speak. `navigate` switches that area's screen to
    the Assist-Response view.
    """
    mgr = _manager(hass)
    area_id = _resolve_area(hass, intent_obj)
    if mgr is not None and area_id:
        await mgr.assist_response(
            text, title=title, image=image, areas=[area_id], devices=[], navigate=navigate
        )
    else:
        await _maybe_area_nudge(hass, intent_obj)
    return _speech(intent_obj, text)


async def _maybe_area_nudge(hass: HomeAssistant, intent_obj: intent.Intent) -> None:
    """Once per device, nudge an un-scoped caller to open device name/room setup.

    Fires when the request came from a real device that has no area (and none was
    spoken/injected), so voice features can't be room-aware yet. House-wide (we
    can't target the area we don't have); self-clears once the room is assigned.
    """
    mgr = _manager(hass)
    if mgr is None:
        return
    device_id = intent_obj.device_id
    if not device_id or _slot(intent_obj, "area") or _slot(intent_obj, "preferred_area_id"):
        return
    device = dr.async_get(hass).async_get(device_id)
    if device is None or device.area_id:
        return
    if not await mgr.async_mark_area_nudged(device_id):
        return
    eff = mgr.effective_settings() or {}
    root = str(eff.get("dashboard_root") or "ted-dashboard")
    home = str(eff.get("home_dashboard") or "[root]/home-welcome").replace("[root]", root)
    if not home.startswith("/"):
        home = "/" + home
    await mgr.notify(
        "Assign this screen to a room",
        "Voice commands here aren't room-aware yet because this device has no area. "
        "Open the welcome screen to set it, or ask an admin to assign it in Settings.",
        severity="info",
        icon="mdi:map-marker-question",
        actions=[
            {"label": "Set the room", "action": "navigate", "navigation_path": home, "variant": "primary"}
        ],
    )


def _resolve_music_player(hass: HomeAssistant, area_id: str | None) -> str | None:
    """Find a Music Assistant media_player for the area (else any MA player)."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    ma_players: list[tuple[str, object]] = []
    for state in hass.states.async_all("media_player"):
        entry = ent_reg.async_get(state.entity_id)
        if entry and entry.platform == "music_assistant":
            ma_players.append((state.entity_id, entry))
    if not ma_players:
        return None
    if area_id:
        for entity_id, entry in ma_players:
            area = entry.area_id
            if area is None and entry.device_id:
                device = dev_reg.async_get(entry.device_id)
                area = device.area_id if device else None
            if area == area_id:
                return entity_id
    return ma_players[0][0]


def _norm_mode(spoken: str | None) -> str | None:
    """Normalize a spoken HVAC mode to a canonical value (e.g. 'heat and cool')."""
    if not spoken:
        return None
    token = str(spoken).strip().casefold()
    token = token.replace(" and ", "_").replace(" ", "_").replace("-", "_")
    return token or None


def _next_alarm_dt(alarm: dict, now: datetime) -> datetime | None:
    """Next future firing datetime for an alarm (None if it never fires)."""
    raw = alarm.get("time") or ""
    try:
        hh, mm = (int(x) for x in raw.split(":"))
    except (ValueError, AttributeError):
        return None
    days = alarm.get("days") or []
    for offset in range(0, 8):
        day = now + timedelta(days=offset)
        if days and day.weekday() not in days:
            continue
        candidate = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            continue
        return candidate
    return None


def _next_alarm(hass: HomeAssistant, mgr, area_id: str | None):
    """Return (alarm, when) for the soonest enabled alarm in scope, or None."""
    now = dt_util.now()
    best = None
    for alarm in mgr.alarms:
        if not alarm.get("enabled"):
            continue
        if alarm.get("location") not in (None, area_id):
            continue
        when = _next_alarm_dt(alarm, now)
        if when is None:
            continue
        if best is None or when < best[1]:
            best = (alarm, when)
    return best


def _remaining_secs(timer: dict) -> int:
    """Seconds left on an active/mirrored timer."""
    if timer.get("paused"):
        return int(timer.get("remaining", 0) or 0)
    ends = dt_util.parse_datetime(timer.get("ends") or "")
    if ends is None:
        return int(timer.get("remaining", 0) or 0)
    return max(0, int((ends - dt_util.utcnow()).total_seconds()))


def _calendar_entities(hass: HomeAssistant, mgr) -> list[str]:
    """The configured calendars (calendars_list) else every calendar.* entity."""
    chosen = (mgr.effective_settings() or {}).get("calendars_list") or []
    if chosen:
        return [e for e in chosen if hass.states.get(e)]
    return [s.entity_id for s in hass.states.async_all("calendar")]


def _parse_event_start(raw) -> tuple[datetime | None, bool]:
    """Parse a calendar event 'start' into (datetime, all_day)."""
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date")
    if not isinstance(raw, str) or not raw:
        return None, False
    if "T" in raw:
        parsed = dt_util.parse_datetime(raw)
        if parsed is not None and parsed.tzinfo is None:
            parsed = dt_util.as_local(parsed)
        return parsed, False
    day = dt_util.parse_date(raw)
    if day is None:
        return None, False
    return dt_util.start_of_local_day(datetime(day.year, day.month, day.day)), True


def _describe_when(when: datetime, now: datetime, include_time: bool = True) -> str:
    """Human phrase for an upcoming datetime relative to now (both local-aware)."""
    day_diff = (when.date() - now.date()).days
    if day_diff == 0:
        base = "today"
    elif day_diff == 1:
        base = "tomorrow"
    elif 2 <= day_diff <= 6:
        base = f"on {when.strftime('%A')}"
    else:
        base = f"on {when.strftime('%A, %B ')}{when.day}"
    if include_time:
        return f"{base} at {_spoken_time(when.strftime('%H:%M'))}"
    return base


# ── music intent ──────────────────────────────────


class PlayMusicIntent(intent.IntentHandler):
    """Play music (optionally a spoken search) on the area's Music Assistant player."""

    intent_type = INTENT_PLAY_MUSIC
    description = "Play music in Ted's Cards, optionally a song, artist, album, or playlist"
    slot_schema = {
        vol.Optional(
            "query", description="What to play: a song, artist, album, or playlist"
        ): cv.string,
        **_AREA_SLOTS,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        await _maybe_area_nudge(hass, intent_obj)
        area_id = _resolve_area(hass, intent_obj)
        player = _resolve_music_player(hass, area_id)
        if not player:
            return _speech(intent_obj, "I couldn't find a Music Assistant player to play on.")
        query = _slot(intent_obj, "query")
        if query:
            if not hass.services.has_service("music_assistant", "play_media"):
                return _speech(
                    intent_obj, "Music Assistant isn't set up, so I can't search for that."
                )
            await hass.services.async_call(
                "music_assistant", "play_media",
                {"entity_id": player, "media_id": str(query), "enqueue": "replace"},
                blocking=True,
            )
        else:
            await hass.services.async_call(
                "media_player", "media_play", {"entity_id": player}, blocking=True
            )
        _fire_navigate(hass, "music_dashboard", area_id, intent_obj.device_id)
        return _speech(intent_obj, f"Playing {query}." if query else "Playing music.")


# ── announce intent ────────────────────────────────


class AnnounceIntent(intent.IntentHandler):
    """Broadcast a spoken announcement to Ted's Dashboard devices."""

    intent_type = INTENT_ANNOUNCE
    description = "Broadcast a spoken announcement to Ted's Dashboard devices"
    slot_schema = {
        vol.Required("message", description="The announcement to speak"): cv.string,
        vol.Optional(
            "scope", description="Set to 'all' to announce house-wide"
        ): vol.In(["all"]),
        **_AREA_SLOTS,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        message = _slot(intent_obj, "message")
        if not message:
            return _speech(intent_obj, "What would you like me to announce?")
        await _maybe_area_nudge(hass, intent_obj)
        force_all = _slot(intent_obj, "scope") == "all"
        area_id = None if force_all else _resolve_area(hass, intent_obj)
        areas = [area_id] if area_id else []
        await mgr.announce(str(message), areas=areas)
        if area_id and (area := ar.async_get(hass).async_get_area(area_id)):
            return _speech(intent_obj, f"Announcing in {area.name}.")
        return _speech(intent_obj, "Announcing.")


# ── status query intents ─────────────────────────────


class NextAlarmIntent(intent.IntentHandler):
    """Say when the next alarm is scheduled (and show it on the Assist-Response view)."""

    intent_type = INTENT_NEXT_ALARM
    description = "Say when the next alarm is scheduled in Ted's Cards"
    slot_schema = {**_AREA_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        area_id = _resolve_area(hass, intent_obj)
        found = _next_alarm(hass, mgr, area_id)
        if not found:
            return await _answer(
                hass, intent_obj, "You have no upcoming alarms.", title="Next alarm"
            )
        alarm, when = found
        label = alarm.get("label") or "Alarm"
        text = f"Your next alarm, {label}, is {_describe_when(when, dt_util.now())}."
        return await _answer(hass, intent_obj, text, title="Next alarm")


class TimerStatusIntent(intent.IntentHandler):
    """Report the active timers for this area (plus house-wide)."""

    intent_type = INTENT_TIMER_STATUS
    description = "Report the active timers in Ted's Cards"
    slot_schema = {**_AREA_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        # Non-Ted's devices keep native timers: report those instead.
        if not _is_tds_voice_device(hass, mgr, intent_obj.device_id):
            return _speech(intent_obj, _native_timer_status_speech(hass, intent_obj))
        area_id = _resolve_area(hass, intent_obj)
        timers = [t for t in mgr.active.values() if t.get("location") in (None, area_id)]
        if not timers:
            return await _answer(hass, intent_obj, "You have no active timers.", title="Timers")
        parts = []
        for timer in sorted(timers, key=_remaining_secs):
            left = mgr._fmt_duration(_remaining_secs(timer))
            state = " (paused)" if timer.get("paused") else ""
            parts.append(f"{timer.get('name') or 'Timer'} with {left} left{state}")
        count = len(timers)
        noun = "timer" if count == 1 else "timers"
        text = f"You have {count} {noun}: " + "; ".join(parts) + "."
        return await _answer(hass, intent_obj, text, title="Timers")


class NextCalendarEventIntent(intent.IntentHandler):
    """Say the next upcoming calendar event across the configured calendars."""

    intent_type = INTENT_NEXT_EVENT
    description = "Say the next calendar event or appointment in Ted's Cards"
    slot_schema = {**_AREA_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        calendars = _calendar_entities(hass, mgr)
        if not calendars:
            return _speech(intent_obj, "You don't have any calendars set up.")
        now = dt_util.now()
        try:
            response = await hass.services.async_call(
                "calendar", "get_events",
                {
                    "entity_id": calendars,
                    "start_date_time": now.isoformat(),
                    "end_date_time": (now + timedelta(days=30)).isoformat(),
                },
                blocking=True, return_response=True,
            )
        except Exception:  # noqa: BLE001 - calendar backends can raise; degrade gracefully
            return _speech(intent_obj, "I couldn't read your calendars right now.")
        best = None
        for data in (response or {}).values():
            for event in ((data or {}).get("events") or []):
                start, all_day = _parse_event_start(event.get("start"))
                if start is None or start < now:
                    continue
                if best is None or start < best[0]:
                    best = (start, all_day, event)
        if best is None:
            return await _answer(
                hass, intent_obj, "You have no upcoming events.", title="Next appointment"
            )
        start, all_day, event = best
        summary = (event.get("summary") or "an event").strip()
        when = _describe_when(start, now, include_time=not all_day)
        text = f"Your next appointment is {summary}, {when}."
        return await _answer(hass, intent_obj, text, title="Next appointment")


# ── thermostat intents ──────────────────────────────


class SetThermostatIntent(intent.IntentHandler):
    """Set a thermostat's temperature, HVAC mode, or preset (smart logic in climate.py)."""

    intent_type = INTENT_SET_THERMOSTAT
    description = "Set a thermostat's temperature, mode, or preset in Ted's Cards"
    slot_schema = {
        vol.Optional(
            "temperature", description="Target temperature in degrees"
        ): vol.Coerce(float),
        vol.Optional(
            "hvac_mode", description="heat, cool, auto, heat_cool, or off"
        ): cv.string,
        vol.Optional(
            "preset", description="A preset such as eco, away, home, sleep, or boost"
        ): cv.string,
        vol.Optional("zone", description="Which thermostat, zone, or room"): cv.string,
        **_AREA_SLOTS,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        await _maybe_area_nudge(hass, intent_obj)
        entity_id = resolve_climate_entity(
            hass, mgr, _slot(intent_obj, "zone"), _resolve_area(hass, intent_obj)
        )
        if not entity_id:
            return _speech(intent_obj, "I couldn't find that thermostat.")
        mode = _norm_mode(_slot(intent_obj, "hvac_mode"))
        preset = _slot(intent_obj, "preset")
        temperature = _slot(intent_obj, "temperature")
        if mode:
            speech = await apply_climate(hass, mgr, entity_id=entity_id, kind="mode", hvac_mode=mode)
        elif preset:
            speech = await apply_climate(
                hass, mgr, entity_id=entity_id, kind="preset", preset=str(preset)
            )
        elif temperature is not None:
            speech = await apply_climate(
                hass, mgr, entity_id=entity_id, kind="absolute", temperature=float(temperature)
            )
        else:
            return _speech(intent_obj, "What would you like to set the thermostat to?")
        return _speech(intent_obj, speech)


class AdjustThermostatIntent(intent.IntentHandler):
    """Make a thermostat warmer or cooler by an optional amount (smart logic in climate.py)."""

    intent_type = INTENT_ADJUST_THERMOSTAT
    description = "Make a thermostat warmer or cooler in Ted's Cards"
    slot_schema = {
        vol.Required("direction", description="warmer or cooler"): vol.In(["warmer", "cooler"]),
        vol.Optional(
            "amount", description="How many degrees to change by"
        ): vol.Coerce(float),
        vol.Optional("zone", description="Which thermostat, zone, or room"): cv.string,
        **_AREA_SLOTS,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        await _maybe_area_nudge(hass, intent_obj)
        entity_id = resolve_climate_entity(
            hass, mgr, _slot(intent_obj, "zone"), _resolve_area(hass, intent_obj)
        )
        if not entity_id:
            return _speech(intent_obj, "I couldn't find that thermostat.")
        amount = _slot(intent_obj, "amount")
        speech = await apply_climate(
            hass, mgr, entity_id=entity_id, kind="relative",
            direction=str(_slot(intent_obj, "direction") or "warmer"),
            amount=float(amount) if amount is not None else None,
        )
        return _speech(intent_obj, speech)


# ── timers (Ted's-owned voice timers, per device) ───────────
# Custom sentences deterministically beat the built-in Hass*Timer intents (the
# default agent prefers custom-sentence matches). On a Ted's Dashboard panel we
# create a Ted's timer (live countdown on the Timers view + Ted's alert); on any
# other device we hand the same utterance back to the built-in intent so native
# timers (with their on-device ring/notification) keep working there.

_TIMER_DURATION_SLOTS = {
    vol.Optional("hours"): vol.Coerce(int),
    vol.Optional("minutes"): vol.Coerce(int),
    vol.Optional("seconds"): vol.Coerce(int),
}
_TIMER_FIND_SLOTS = {
    vol.Optional("name"): cv.string,
    vol.Optional("start_hours"): vol.Coerce(int),
    vol.Optional("start_minutes"): vol.Coerce(int),
    vol.Optional("start_seconds"): vol.Coerce(int),
    vol.Optional("area"): cv.string,
    vol.Optional("preferred_area_id"): cv.string,
}


def _is_tds_voice_device(hass: HomeAssistant, mgr, device_id: str | None) -> bool:
    """True when the calling device is a Ted's Dashboard panel.

    A panel is identified by a ``mobile_app`` (Companion app) OR ``browser_mod``
    (dashboard webview) device whose area contains a registered Ted's Dashboard
    screen. The browser_mod case covers the in-dashboard voice satellite, where the
    request is attributed to the panel's own dashboard device.
    """
    if not device_id or mgr is None:
        return False
    device = dr.async_get(hass).async_get(device_id)
    if device is None or not device.area_id:
        return False
    if not any(ident[0] in ("mobile_app", "browser_mod") for ident in device.identifiers):
        return False
    tds_areas = {e.get("area") for e in mgr.device_registry.values() if e.get("area")}
    return device.area_id in tds_areas


async def _redispatch_native(
    intent_obj: intent.Intent, native_type: str
) -> intent.IntentResponse:
    """Hand the utterance to a built-in HA timer intent (native behavior)."""
    return await intent.async_handle(
        intent_obj.hass,
        DOMAIN,
        native_type,
        intent_obj.slots,
        intent_obj.text,
        intent_obj.context,
        intent_obj.language,
        device_id=intent_obj.device_id,
    )


def _duration_slots(intent_obj: intent.Intent) -> tuple[int, int, int]:
    return (
        int(_slot(intent_obj, "hours") or 0),
        int(_slot(intent_obj, "minutes") or 0),
        int(_slot(intent_obj, "seconds") or 0),
    )


def _fmt_secs(secs) -> str:
    """Spoken duration, e.g. "1 hour 30 minutes"."""
    secs = max(0, int(secs or 0))
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s or not parts:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return " ".join(parts)


def _native_timer_status_speech(hass: HomeAssistant, intent_obj: intent.Intent) -> str:
    """Summarize the built-in HA timers for this device (non-Ted's devices)."""
    try:
        from homeassistant.components.intent.const import TIMER_DATA
    except ImportError:  # pragma: no cover - defensive
        return "You have no active timers."
    manager = hass.data.get(TIMER_DATA)
    device_id = intent_obj.device_id
    timers = [
        t
        for t in (getattr(manager, "timers", {}) or {}).values()
        if (not device_id) or t.device_id == device_id
    ]
    if not timers:
        return "You have no active timers."
    parts = []
    for t in sorted(timers, key=lambda x: x.seconds_left):
        state = "" if t.is_active else " (paused)"
        parts.append(f"{t.name or 'Timer'} with {_fmt_secs(t.seconds_left)} left{state}")
    count = len(timers)
    noun = "timer" if count == 1 else "timers"
    return f"You have {count} {noun}: " + "; ".join(parts) + "."


def _find_ted_timers(mgr, intent_obj: intent.Intent, area_id, paused=None) -> list[dict]:
    """Ted's active timers in scope, optionally filtered by name/start-time/paused."""
    timers = [t for t in mgr.active.values() if t.get("location") in (None, area_id)]
    if paused is not None:
        timers = [t for t in timers if bool(t.get("paused")) == paused]
    name = _slot(intent_obj, "name")
    if name:
        wanted = str(name).casefold()
        timers = [t for t in timers if wanted in (t.get("name") or "").casefold()]
    sh = _slot(intent_obj, "start_hours")
    sm = _slot(intent_obj, "start_minutes")
    ss = _slot(intent_obj, "start_seconds")
    if sh is not None or sm is not None or ss is not None:
        total = int(sh or 0) * 3600 + int(sm or 0) * 60 + int(ss or 0)
        timers = [t for t in timers if t.get("duration") == total]
    return timers


def _pick_ted_timer(mgr, intent_obj: intent.Intent, area_id, paused=None):
    """Return (timer, error_speech). error_speech set on 0 or >1 matches."""
    matches = _find_ted_timers(mgr, intent_obj, area_id, paused=paused)
    if not matches:
        return None, "I couldn't find that timer."
    if len(matches) > 1:
        return None, f"You have {len(matches)} timers running — please be more specific."
    return matches[0], None


class StartTimerIntent(intent.IntentHandler):
    """Start a countdown timer (Ted's on a panel, native elsewhere)."""

    intent_type = INTENT_START_TIMER
    description = "Start a countdown timer in Ted's Cards"
    slot_schema = {**_TIMER_DURATION_SLOTS, vol.Optional("name"): cv.string, **_AREA_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        h, m, s = _duration_slots(intent_obj)
        if not _is_tds_voice_device(hass, mgr, intent_obj.device_id):
            response = await _redispatch_native(intent_obj, "HassStartTimer")
            if not response.speech:
                response.async_set_speech(f"Timer set for {_fmt_secs(h * 3600 + m * 60 + s)}.")
            return response
        if h == 0 and m == 0 and s == 0:
            return _speech(intent_obj, "How long should the timer run?")
        name = _slot(intent_obj, "name") or f"{_fmt_secs(h * 3600 + m * 60 + s)} timer"
        area_id = _resolve_area(hass, intent_obj)
        await mgr.start_timer(str(name), h, m, s, location=area_id)
        _fire_navigate(hass, "timers_dashboard", area_id, intent_obj.device_id)
        return _speech(intent_obj, f"Timer set for {_fmt_secs(h * 3600 + m * 60 + s)}.")


class CancelTimerIntent(intent.IntentHandler):
    """Cancel a running timer."""

    intent_type = INTENT_CANCEL_TIMER
    description = "Cancel a running timer in Ted's Cards"
    slot_schema = {**_TIMER_FIND_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        if not _is_tds_voice_device(hass, mgr, intent_obj.device_id):
            response = await _redispatch_native(intent_obj, "HassCancelTimer")
            if not response.speech:
                response.async_set_speech("Timer cancelled.")
            return response
        timer, err = _pick_ted_timer(mgr, intent_obj, _resolve_area(hass, intent_obj))
        if err:
            return _speech(intent_obj, err)
        mgr.cancel_timer(timer["id"])
        return _speech(intent_obj, "Timer cancelled.")


class PauseTimerIntent(intent.IntentHandler):
    """Pause a running timer."""

    intent_type = INTENT_PAUSE_TIMER
    description = "Pause a running timer in Ted's Cards"
    slot_schema = {**_TIMER_FIND_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        if not _is_tds_voice_device(hass, mgr, intent_obj.device_id):
            response = await _redispatch_native(intent_obj, "HassPauseTimer")
            if not response.speech:
                response.async_set_speech("Timer paused.")
            return response
        timer, err = _pick_ted_timer(mgr, intent_obj, _resolve_area(hass, intent_obj), paused=False)
        if err:
            return _speech(intent_obj, err)
        mgr.pause_timer(timer["id"])
        return _speech(intent_obj, "Timer paused.")


class ResumeTimerIntent(intent.IntentHandler):
    """Resume a paused timer."""

    intent_type = INTENT_RESUME_TIMER
    description = "Resume a paused timer in Ted's Cards"
    slot_schema = {**_TIMER_FIND_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        if not _is_tds_voice_device(hass, mgr, intent_obj.device_id):
            response = await _redispatch_native(intent_obj, "HassUnpauseTimer")
            if not response.speech:
                response.async_set_speech("Timer resumed.")
            return response
        timer, err = _pick_ted_timer(mgr, intent_obj, _resolve_area(hass, intent_obj), paused=True)
        if err:
            return _speech(intent_obj, err)
        mgr.resume_timer(timer["id"])
        return _speech(intent_obj, "Timer resumed.")


class AddTimeIntent(intent.IntentHandler):
    """Add time to a running timer."""

    intent_type = INTENT_ADD_TIME
    description = "Add time to a running timer in Ted's Cards"
    slot_schema = {**_TIMER_DURATION_SLOTS, **_TIMER_FIND_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        h, m, s = _duration_slots(intent_obj)
        delta = h * 3600 + m * 60 + s
        if not _is_tds_voice_device(hass, mgr, intent_obj.device_id):
            response = await _redispatch_native(intent_obj, "HassIncreaseTimer")
            if not response.speech:
                response.async_set_speech(f"Added {_fmt_secs(delta)} to the timer.")
            return response
        timer, err = _pick_ted_timer(mgr, intent_obj, _resolve_area(hass, intent_obj))
        if err:
            return _speech(intent_obj, err)
        new_total = _remaining_secs(timer) + delta
        mgr.update_timer(timer["id"], seconds=new_total)
        return _speech(intent_obj, f"Added {_fmt_secs(delta)} to the timer.")


class RemoveTimeIntent(intent.IntentHandler):
    """Remove time from a running timer."""

    intent_type = INTENT_REMOVE_TIME
    description = "Remove time from a running timer in Ted's Cards"
    slot_schema = {**_TIMER_DURATION_SLOTS, **_TIMER_FIND_SLOTS}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        mgr = _manager(hass)
        if mgr is None:
            return _speech(intent_obj, "Ted's Cards is not set up yet.")
        h, m, s = _duration_slots(intent_obj)
        delta = h * 3600 + m * 60 + s
        if not _is_tds_voice_device(hass, mgr, intent_obj.device_id):
            response = await _redispatch_native(intent_obj, "HassDecreaseTimer")
            if not response.speech:
                response.async_set_speech(f"Removed {_fmt_secs(delta)} from the timer.")
            return response
        timer, err = _pick_ted_timer(mgr, intent_obj, _resolve_area(hass, intent_obj))
        if err:
            return _speech(intent_obj, err)
        new_total = _remaining_secs(timer) - delta
        if new_total <= 0:
            mgr.cancel_timer(timer["id"])
            return _speech(intent_obj, "That leaves no time, so I cancelled the timer.")
        mgr.update_timer(timer["id"], seconds=new_total)
        return _speech(intent_obj, f"Removed {_fmt_secs(delta)} from the timer.")
