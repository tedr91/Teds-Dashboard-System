"""Sensors exposing alarms and timers for the cards to read."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    add([TedsAlarmsSensor(manager), TedsTimersSensor(manager), TedsNotificationsSensor(manager), TedsAnnouncementsSensor(manager), TedsAssistResponsesSensor(manager), TedsSettingsSensor(manager), TedsRequirementsSensor(manager)])


class _Base(SensorEntity):
    _attr_should_poll = False

    def __init__(self, manager) -> None:
        self._m = manager

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._m.register(self.async_write_ha_state))


class TedsAlarmsSensor(_Base):
    _attr_name = "Teds Alarms"
    _attr_unique_id = "teds_alarms"
    _attr_icon = "mdi:alarm"

    @property
    def native_value(self):
        return len([a for a in self._m.alarms if a.get("enabled")])

    @property
    def extra_state_attributes(self):
        # Return a fresh list of copies each read: the manager mutates its own
        # list in place, and returning that live reference makes HA compare the
        # new state against an already-mutated "old" state, so attribute-only
        # changes (e.g. editing an alarm) can fail to fire a state update.
        return {"alarms": [dict(a) for a in self._m.alarms]}


class TedsTimersSensor(_Base):
    _attr_name = "Teds Timers"
    _attr_unique_id = "teds_timers"
    _attr_icon = "mdi:timer"

    @property
    def native_value(self):
        return len(self._m.active)

    @property
    def extra_state_attributes(self):
        return {
            "active": [
                {
                    "id": t["id"], "name": t["name"], "ends": t["ends"],
                    "duration": t.get("duration", 0), "remaining": t.get("remaining", 0),
                    "paused": t.get("paused", False), "location": t.get("location"),
                }
                for t in self._m.active.values()
            ],
            "recent": [dict(r) for r in self._m.recent],
        }


class TedsNotificationsSensor(_Base):
    _attr_name = "Teds Notifications"
    _attr_unique_id = "teds_notifications"
    _attr_icon = "mdi:bell"

    @property
    def native_value(self):
        return len([n for n in self._m.notifications if not n.get("read")])

    @property
    def extra_state_attributes(self):
        return {
            "notifications": [dict(n) for n in self._m.notifications],
            "unread": len([n for n in self._m.notifications if not n.get("read")]),
            "total": len(self._m.notifications),
        }


class TedsAnnouncementsSensor(_Base):
    _attr_name = "Teds Announcements"
    _attr_unique_id = "teds_announcements"
    _attr_icon = "mdi:bullhorn"

    @property
    def native_value(self):
        return len(self._m.recent_announcements)

    @property
    def extra_state_attributes(self):
        return {"recent": [dict(r) for r in self._m.recent_announcements]}


class TedsAssistResponsesSensor(_Base):
    """Latest Assist-Response answer per target ("device:<id>"/"area:<id>"/"house").
    Cards read their own target's entry to restore content after a reload."""

    _attr_name = "Teds Assist Responses"
    _attr_unique_id = "teds_assist_responses"
    _attr_icon = "mdi:message-reply-text"

    @property
    def native_value(self):
        return len(self._m.assist_responses)

    @property
    def extra_state_attributes(self):
        return {"responses": {k: dict(v) for k, v in self._m.assist_responses.items()}}


class TedsSettingsSensor(_Base):
    _attr_name = "Teds Settings"
    _attr_unique_id = "teds_settings"
    _attr_icon = "mdi:cog"

    @property
    def native_value(self):
        # Number of per-device override buckets (a stable, cheap summary value).
        return len(self._m.settings.get("devices", {}))

    @property
    def extra_state_attributes(self):
        return self._m.settings_payload()


class TedsRequirementsSensor(_Base):
    """Server-side dependency detection. State = number of missing requirements;
    each requirement is also exposed as an attribute (ok/missing/unknown) so
    dashboards can gate a MessageBox with a `state` + `attribute` condition."""

    _attr_name = "Teds Requirements"
    _attr_unique_id = "teds_requirements"
    _attr_icon = "mdi:clipboard-check"

    @property
    def native_value(self):
        reqs = self._m.requirements or {}
        return len([1 for v in reqs.values() if v == "missing"])

    @property
    def extra_state_attributes(self):
        reqs = self._m.requirements or {}
        missing = [k for k, v in reqs.items() if v == "missing"]
        return {**reqs, "missing": missing, "ok": not missing, "version": self._m.version}

