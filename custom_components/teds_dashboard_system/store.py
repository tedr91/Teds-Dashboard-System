"""Persistent manager for Ted's Cards alarms and recent timers."""

from __future__ import annotations

import functools
import uuid
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .light_fade import LightFadeEngine
from .playback import PlaybackEngine

from .const import (
    DEVICE_PRESENCE_TTL,
    EVENT_ALARM_RINGING,
    EVENT_ASSIST_RESPONSE,
    EVENT_NAVIGATE,
    EVENT_NOTIFICATION,
    EVENT_SETTINGS,
    EVENT_TIMER_FINISHED,
    EVENT_VISION_EVENT,
    ASSIST_HISTORY_MAX,
    NOTIFICATIONS_MAX,
    RECENT_ANNOUNCEMENTS_MAX,
    RECENT_TIMERS_MAX,
    SETTINGS_DEFAULTS,
    SETTINGS_FIRE_DEBOUNCE_S,
    SETTINGS_KEYS,
    STORAGE_KEY,
    STORAGE_VERSION,
    VISION_EVENTS_MAX,
)


class TedsManager:
    """Owns alarms + active/recent timers, persists them, and fires them."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.alarms: list[dict] = []
        self.recent: list[dict] = []  # last N timer presets (h/m/s + name)
        self.active: dict[str, dict] = {}  # id -> {name, ends, cancel}
        self.notifications: list[dict] = []  # newest-first notification list
        # Analyzed camera Vision Analysis events, newest-first (capped VISION_EVENTS_MAX).
        self.vision_events: list[dict] = []
        # last N announcements (message + targets) for quick re-send, newest-first.
        self.recent_announcements: list[dict] = []
        # Latest Assist-Response answer per target key ("device:<id>" / "area:<id>" /
        # "house") so a reloaded / late-joining screen can restore its current content.
        self.assist_responses: dict[str, dict] = {}
        # Rolling conversation history per target key (oldest-first) for scroll-back on
        # the Assist-Response view. Bounded to the most recent ASSIST_HISTORY_MAX entries.
        self.assist_history: dict[str, list[dict]] = {}
        # Settings: global baseline + per-device overrides (only overridden keys stored).
        self.settings: dict = {"global": {}, "devices": {}}
        # Devices that have registered themselves (device_id -> {area, name, last_seen}).
        self.device_registry: dict[str, dict] = {}
        # Pending debounced EVENT_SETTINGS fire (see `_fire_settings`), if one is scheduled.
        self._settings_fire_unsub = None
        # HA device_ids we've already nudged once about a missing area.
        self.area_nudged_devices: set[str] = set()
        # Server-side dependency detection results (req_id -> ok/missing/unknown).
        self.requirements: dict[str, str] = {}
        # Optional Frigate camera-source adoption. `answered` stops re-prompting;
        # `adopted` means Frigate is the chosen source; `notified` guards the
        # one-time startup notification. `frigate` caches the latest detection.
        self.frigate_answered: bool = False
        self.frigate_adopted: bool = False
        self.frigate_prompt_notified: bool = False
        self.frigate: dict = {"installed": False, "cameras": [], "capability": "absent"}
        # entity_id -> {instance_id, camera_name} for every Frigate camera; the card
        # uses it to drive its MSE live player. Derived (not persisted).
        self.frigate_cameras: dict[str, dict] = {}
        # Frigate MQTT review -> notification bridge (created in __init__.py setup).
        self.frigate_bridge = None
        # This integration's version (from the manifest), for status displays.
        self.version: str | None = None
        # media-source URI of the dedicated "Ted Dash System" wallpaper folder.
        self.media_folder: str | None = None
        # Writable dir where stitched announcement clips are cached + served.
        self.announce_cache_dir: str | None = None
        self.playback = PlaybackEngine(self)
        # Runs per-alarm sunrise light ramps + safeguard fade-back (server-side).
        self.light_fade = LightFadeEngine(self)
        self._listeners: list = []
        self._update_cbs: set = set()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.alarms = data.get("alarms", [])
        self.recent = data.get("recent", [])
        self.notifications = data.get("notifications", [])
        self.vision_events = data.get("vision_events", [])
        self.recent_announcements = data.get("recent_announcements", [])
        self.assist_responses = dict(data.get("assist_responses") or {})
        self.assist_history = {
            k: list(v) for k, v in (data.get("assist_history") or {}).items()
        }
        stored_settings = data.get("settings") or {}
        self.settings = {
            "global": dict(stored_settings.get("global") or {}),
            "devices": {k: dict(v) for k, v in (stored_settings.get("devices") or {}).items()},
        }
        self.device_registry = {k: dict(v) for k, v in (data.get("devices") or {}).items()}
        self.area_nudged_devices = set(data.get("area_nudged_devices") or [])
        self.frigate_answered = bool(data.get("frigate_answered"))
        self.frigate_adopted = bool(data.get("frigate_adopted"))
        self.frigate_prompt_notified = bool(data.get("frigate_prompt_notified"))
        # Per-minute alarm check.
        self._listeners.append(async_track_time_change(self.hass, self._tick, second=0))

    async def _save(self) -> None:
        await self._store.async_save({
            "alarms": self.alarms,
            "recent": self.recent,
            "notifications": self.notifications,
            "vision_events": self.vision_events,
            "recent_announcements": self.recent_announcements,
            "assist_responses": self.assist_responses,
            "assist_history": self.assist_history,
            "settings": self.settings,
            "devices": self.device_registry,
            "area_nudged_devices": sorted(self.area_nudged_devices),
            "frigate_answered": self.frigate_answered,
            "frigate_adopted": self.frigate_adopted,
            "frigate_prompt_notified": self.frigate_prompt_notified,
        })

    def shutdown(self) -> None:
        for unsub in self._listeners:
            unsub()
        if self._settings_fire_unsub is not None:
            self._settings_fire_unsub()
            self._settings_fire_unsub = None
        for t in self.active.values():
            if t.get("cancel"):
                t["cancel"]()
        self.playback.shutdown()
        self.light_fade.shutdown()

    # ── alarms ──────────────────────────────────────────────
    async def add_alarm(self, label, time, days, description="", enabled=True, location=None,
                        light_entity=None, light_fade_minutes=None, light_target_pct=None,
                        presence_entity=None):
        # An empty `days` list is a valid one-shot alarm (rings once at the next
        # matching time, then disables itself). Only default to every-day when no
        # days field was supplied at all (None).
        self.alarms.append({
            "id": uuid.uuid4().hex,
            "label": label,
            "description": description,
            "time": time,
            "days": list(days) if days is not None else [0, 1, 2, 3, 4, 5, 6],
            "enabled": enabled,
            "location": location,
            # Optional wake-up light: ramp `light_entity` up to `light_target_pct`
            # over `light_fade_minutes`, finishing when the alarm rings.
            "light_entity": light_entity,
            "light_fade_minutes": light_fade_minutes,
            "light_target_pct": light_target_pct,
            # Optional presence gate: skip the light + ring when this entity isn't present.
            "presence_entity": presence_entity,
        })
        await self._save()
        self._notify()

    async def update_alarm(self, alarm_id, **changes):
        for a in self.alarms:
            if a["id"] == alarm_id:
                for k, v in changes.items():
                    # `location`/`light_entity`/`presence_entity` may be cleared to None;
                    # other fields are only overwritten when a value is actually provided.
                    if k in ("location", "light_entity", "presence_entity") or v is not None:
                        a[k] = v
                # A disabled alarm (or one with the light removed) drops any active fade.
                if not a.get("enabled") or not a.get("light_entity"):
                    self.light_fade.cancel(alarm_id)
                break
        await self._save()
        self._notify()

    async def remove_alarm(self, alarm_id):
        self.light_fade.cancel(alarm_id)
        self.alarms = [a for a in self.alarms if a["id"] != alarm_id]
        await self._save()
        self._notify()

    @callback
    def _tick(self, now: datetime) -> None:
        local = dt_util.as_local(now)
        hhmm = local.strftime("%H:%M")
        # Start any wake-up light ramp that must COMPLETE at an alarm's time: it begins
        # `light_fade_minutes` early, so trigger when now + fade lands on the ring time.
        for a in self.alarms:
            if not a.get("enabled") or not a.get("light_entity"):
                continue
            if not self._present(a.get("presence_entity")):
                continue
            fade_min = int(a.get("light_fade_minutes") or 0)
            if fade_min <= 0:
                continue
            ring_at = local + timedelta(minutes=fade_min)
            if ring_at.strftime("%H:%M") != a.get("time"):
                continue
            days = a.get("days") or []
            if days and ring_at.weekday() not in days:
                continue
            self.light_fade.start_wake_fade(a)
        rang = False
        for a in self.alarms:
            if not a.get("enabled") or a.get("time") != hhmm:
                continue
            days = a.get("days") or []
            # No days = one-shot: ring at the next matching time on any weekday,
            # then disable. Otherwise only ring on the selected weekdays.
            one_shot = not days
            if not one_shot and local.weekday() not in days:
                continue
            # A presence sensor gates the whole alarm: nobody in the room = don't fire.
            if not self._present(a.get("presence_entity")):
                continue
            loc = a.get("location")
            self.hass.bus.async_fire(EVENT_ALARM_RINGING, {
                "id": a["id"],
                "label": a["label"],
                "location": loc,
                "area_name": self._area_name(loc),
            })
            self._add_notification(
                title="Alarm",
                message=a["label"],
                severity="warning",
                icon="mdi:alarm",
                area=loc,
                timeout=120,
                source="alarm",
                snooze={"kind": "alarm", "name": a["label"], "area": loc},
            )
            if one_shot:
                a["enabled"] = False
            rang = True
        if rang:
            self.hass.async_create_task(self._save())
            self._notify()

    def _area_name(self, location):
        """Resolve an area_id to its friendly name (None when unknown/unset)."""
        if not location:
            return None
        area = ar.async_get(self.hass).async_get_area(location)
        return area.name if area else None

    def _present(self, entity_id) -> bool:
        """True when the optional presence entity says someone's there.

        Fails OPEN: no sensor, or an unavailable/unknown one, never suppresses an alarm.
        """
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return True
        return state.state in ("on", "home")

    async def async_mark_area_nudged(self, device_id: str) -> bool:
        """Record a one-time 'assign this device a room' nudge. True the first time."""
        if not device_id or device_id in self.area_nudged_devices:
            return False
        self.area_nudged_devices.add(device_id)
        await self._save()
        return True


    # ── timers ──────────────────────────────────────────────
    async def start_timer(self, name, hours=0, minutes=0, seconds=0, location=None):
        secs = hours * 3600 + minutes * 60 + seconds
        tid = uuid.uuid4().hex
        ends = dt_util.utcnow() + timedelta(seconds=secs)
        cancel = async_call_later(self.hass, secs, functools.partial(self._on_elapsed, tid))
        self.active[tid] = {
            "id": tid, "name": name, "ends": ends.isoformat(),
            "duration": secs, "remaining": secs, "paused": False, "cancel": cancel,
            "location": location,
        }
        self.recent = [{"name": name, "h": hours, "m": minutes, "s": seconds, "location": location}] + [
            r for r in self.recent
            if not (r["h"] == hours and r["m"] == minutes and r["s"] == seconds
                    and r["name"] == name and r.get("location") == location)
        ][: RECENT_TIMERS_MAX - 1]
        await self._save()
        self._notify()

    def pause_timer(self, tid):
        t = self.active.get(tid)
        if not t or t.get("paused"):
            return
        if t.get("cancel"):
            t["cancel"]()
            t["cancel"] = None
        ends = dt_util.parse_datetime(t["ends"])
        remaining = (ends - dt_util.utcnow()).total_seconds() if ends else t.get("remaining", 0)
        t["remaining"] = max(0, int(round(remaining)))
        t["paused"] = True
        self._notify()

    def resume_timer(self, tid):
        t = self.active.get(tid)
        if not t or not t.get("paused"):
            return
        secs = max(0, int(t.get("remaining", 0)))
        t["ends"] = (dt_util.utcnow() + timedelta(seconds=secs)).isoformat()
        t["paused"] = False
        t["cancel"] = async_call_later(self.hass, secs, functools.partial(self._on_elapsed, tid))
        self._notify()

    def update_timer(self, tid, name=None, hours=None, minutes=None, seconds=None, location=None, _set_location=False):
        t = self.active.get(tid)
        if not t:
            return
        if name is not None:
            t["name"] = name
        if _set_location:
            t["location"] = location
        if hours is not None or minutes is not None or seconds is not None:
            secs = (hours or 0) * 3600 + (minutes or 0) * 60 + (seconds or 0)
            t["duration"] = secs
            t["remaining"] = secs
            if t.get("cancel"):
                t["cancel"]()
                t["cancel"] = None
            t["ends"] = (dt_util.utcnow() + timedelta(seconds=secs)).isoformat()
            if not t.get("paused"):
                t["cancel"] = async_call_later(self.hass, secs, functools.partial(self._on_elapsed, tid))
        self._notify()

    def cancel_timer(self, tid):
        t = self.active.pop(tid, None)
        if t and t.get("cancel"):
            t["cancel"]()
        self._notify()

    async def remove_recent(self, name, hours=0, minutes=0, seconds=0, location=None):
        """Drop a preset from the Recent timers list."""
        self.recent = [
            r for r in self.recent
            if not (r["name"] == name and r["h"] == hours and r["m"] == minutes
                    and r["s"] == seconds and r.get("location") == location)
        ]
        await self._save()
        self._notify()

    @callback
    def _on_elapsed(self, tid, _now=None):
        """Timer duration elapsed — runs in the event loop (via HassJob callback)."""
        self._finish(tid)

    @callback
    def _finish(self, tid):
        t = self.active.pop(tid, None)
        if t:
            loc = t.get("location")
            self.hass.bus.async_fire(EVENT_TIMER_FINISHED, {
                "id": tid,
                "name": t["name"],
                "duration": t.get("duration", 0),
                "location": loc,
                "area_name": self._area_name(loc),
            })
            self._add_notification(
                title="Timer complete",
                message=f"{t['name']} ({self._fmt_duration(t.get('duration', 0))} timer)",
                severity="info",
                icon="mdi:timer-check-outline",
                area=loc,
                timeout=60,
                source="timer",
                snooze={"kind": "timer", "name": t["name"], "area": loc},
            )
            self.hass.async_create_task(self._save())
        self._notify()

    @staticmethod
    def _fmt_duration(sec) -> str:
        """Seconds → "1 hr, 30 min" using only the relevant parts."""
        sec = int(sec or 0)
        h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
        parts = []
        if h:
            parts.append(f"{h} hr")
        if m:
            parts.append(f"{m} min")
        if s:
            parts.append(f"{s} sec")
        return ", ".join(parts) or "0 sec"

    # ── notifications ───────────────────────────────────────
    def _add_notification(self, *, title, message, severity="info", icon=None,
                          area=None, actions=None, notif_id=None, timeout=None,
                          persistence="normal", source="service", snooze=None,
                          announce_targets=None, play_sound=True, data=None):
        """Create a notification, fire the event, play sound, and refresh sensors.

        `persistence` controls its lifetime:
          - "transient": shown as a toast (+ sound) but never stored in the list.
          - "normal":    stored; auto-removed when the user reads/dismisses it.
          - "sticky":    stored; marked read on interaction, kept until cleared.

        `announce_targets` ({areas, devices}) scopes an announcement toast to the
        selected devices/areas on the client side. `play_sound=False` skips the
        generic alert sound (announcements drive their own TTS + chime instead).
        """
        nid = notif_id or uuid.uuid4().hex
        item = {
            "id": nid,
            "title": title,
            "message": message,
            "severity": severity,
            "icon": icon,
            "area": area,
            "area_name": self._area_name(area),
            "created": dt_util.utcnow().isoformat(),
            "read": False,
            "persistence": persistence,
            "timeout": timeout,
            # Client-resolved snooze: the device renders/acts using its own effective
            # settings (enable + minutes) — {"kind": "timer"|"alarm", "name", "area"}.
            "snooze": snooze,
            "actions": actions or [],
            "source": source,
            "announce_targets": announce_targets,
            # Free-form context for custom click handlers (e.g. a vision clip reference).
            "data": data,
        }
        # Transient notifications are never persisted: just deliver the toast + sound.
        if persistence != "transient":
            # Upsert by id (newest first), then cap.
            self.notifications = [n for n in self.notifications if n["id"] != nid]
            self.notifications.insert(0, item)
            del self.notifications[NOTIFICATIONS_MAX:]
        self.hass.bus.async_fire(EVENT_NOTIFICATION, item)
        # Single spot that drives sound for every notification (mapped by source +
        # severity). Alarm/timer alerts use their own sounds; others use the
        # per-severity notification sound.
        if play_sound:
            self.playback.on_notification(item)
        return item

    async def notify(self, title, message, severity="info", icon=None, area=None,
                     actions=None, notif_id=None, timeout=None, persistence="normal",
                     source="service", data=None, play_sound=True):
        self._add_notification(
            title=title, message=message, severity=severity, icon=icon, area=area,
            actions=actions, notif_id=notif_id, timeout=timeout, persistence=persistence,
            source=source, data=data, play_sound=play_sound,
        )
        await self._save()
        self._notify()

    async def dismiss_notification(self, notif_id):
        vision_ids = {
            (n.get("data") or {}).get("vision_event_id")
            for n in self.notifications if n["id"] == notif_id
        }
        self.notifications = [n for n in self.notifications if n["id"] != notif_id]
        self._mark_vision_events_reviewed(vision_ids)
        self._fire_dismissed(notif_id)
        await self._save()
        self._notify()

    async def mark_read(self, notif_id=None, area=None):
        """Handle a read/dismiss interaction.

        Sticky notifications are flagged read and kept; normal ones auto-clear
        (removed) on interaction. In both cases subscribers are told to close the
        matching toast, so acting on one device clears it everywhere.
        """
        affected = []
        vision_ids: set = set()
        remaining = []
        for n in self.notifications:
            match = (notif_id is None or n["id"] == notif_id) and (
                area is None or n.get("area") == area
            )
            if not match:
                remaining.append(n)
                continue
            affected.append(n["id"])
            vision_ids.add((n.get("data") or {}).get("vision_event_id"))
            if n.get("persistence") == "sticky":
                n["read"] = True
                remaining.append(n)
            # normal → dropped (auto-clear on interaction)
        self.notifications = remaining
        self._mark_vision_events_reviewed(vision_ids)
        for nid in affected:
            self._fire_dismissed(nid)
        await self._save()
        self._notify()

    async def clear_notifications(self, area=None):
        if area is None:
            cleared = list(self.notifications)
            self.notifications = []
        else:
            cleared = [n for n in self.notifications if n.get("area") == area]
            self.notifications = [n for n in self.notifications if n.get("area") != area]
        self._mark_vision_events_reviewed(
            {(n.get("data") or {}).get("vision_event_id") for n in cleared}
        )
        for n in cleared:
            self._fire_dismissed(n["id"])
        await self._save()
        self._notify()

    def _fire_dismissed(self, notif_id):
        """Signal subscribers that a notification was dismissed/read, so their
        toasts close on every device (not just the one that acted)."""
        self.playback.stop(notif_id)
        self.hass.bus.async_fire(EVENT_NOTIFICATION, {"id": notif_id, "dismissed": True})

    def _strip_vision_notifs(self, ids: set | None) -> list:
        """Drop notifications a vision toast created for the given event ids (None = every
        vision-linked notification). Sticky ones are kept but marked read. Returns the
        dismissed ids; the caller batches the save/notify + _fire_dismissed."""
        affected = []
        remaining = []
        for n in self.notifications:
            vid = (n.get("data") or {}).get("vision_event_id")
            if vid and (ids is None or vid in ids):
                affected.append(n["id"])
                if n.get("persistence") == "sticky":
                    n["read"] = True
                    remaining.append(n)
            else:
                remaining.append(n)
        self.notifications = remaining
        return affected

    def _mark_vision_events_reviewed(self, vision_ids: set) -> None:
        """Flag the given vision events reviewed in place + broadcast, so reading/dismissing
        a vision toast also marks its timeline entry reviewed (globally, every device)."""
        for e in self.vision_events:
            if e.get("id") in vision_ids and not e.get("reviewed"):
                e["reviewed"] = True
                self.hass.bus.async_fire(EVENT_VISION_EVENT, {"event": self._vision_public(e)})

    async def dismiss_vision_notifications(self, vision_event_id: str) -> None:
        """Read/dismiss the notification(s) a vision event's toast created (matched by
        data.vision_event_id) — same as the user acting on the toast, on every device, so
        marking the event reviewed also clears the notification center on the wallpanel."""
        affected = self._strip_vision_notifs({vision_event_id})
        if not affected:
            return
        for nid in affected:
            self._fire_dismissed(nid)
        await self._save()
        self._notify()

    # ── vision analysis events ──────────────────────────────
    async def add_vision_event(self, event: dict) -> list[dict]:
        """Store a new analyzed camera event (newest-first), cap the list, fire the
        live event, and return any events dropped past the cap (so the vision engine
        can delete their snapshot/clip files)."""
        self.vision_events.insert(0, event)
        dropped = self.vision_events[VISION_EVENTS_MAX:]
        del self.vision_events[VISION_EVENTS_MAX:]
        self.hass.bus.async_fire(EVENT_VISION_EVENT, {"event": self._vision_public(event)})
        await self._save()
        self._notify()
        return dropped

    async def update_vision_notifications(
        self, vision_event_id: str, *, message: str | None = None,
        clip_url: str | None = None, thumbnail_url: str | None = None,
    ) -> None:
        """Fold a vision event's final summary + media into the notifications its toast
        action created (matched by data.vision_event_id), WITHOUT re-alerting — refreshes
        the stored list/sensor only, so no second toast pops and no sound replays."""
        changed = False
        for n in self.notifications:
            if (n.get("data") or {}).get("vision_event_id") != vision_event_id:
                continue
            if message:
                n["message"] = message
            data = dict(n.get("data") or {})
            if clip_url is not None:
                data["clip_url"] = clip_url
            if thumbnail_url is not None:
                data["thumbnail_url"] = thumbnail_url
            n["data"] = data
            changed = True
        if changed:
            await self._save()
            self._notify()

    async def update_vision_event(self, event_id: str, **changes) -> dict | None:
        """Patch an event in place (e.g. mark reviewed) and broadcast the change."""
        for e in self.vision_events:
            if e.get("id") == event_id:
                e.update(changes)
                self.hass.bus.async_fire(
                    EVENT_VISION_EVENT, {"event": self._vision_public(e)}
                )
                await self._save()
                self._notify()
                return e
        return None

    async def remove_vision_event(self, event_id: str) -> dict | None:
        """Remove one event and the notifications its toast created; return it so the engine
        can clean up its files."""
        removed = None
        kept = []
        for e in self.vision_events:
            if e.get("id") == event_id and removed is None:
                removed = e
            else:
                kept.append(e)
        if removed is None:
            return None
        self.vision_events = kept
        dismissed = self._strip_vision_notifs({event_id})
        self.hass.bus.async_fire(EVENT_VISION_EVENT, {"id": event_id, "deleted": True})
        for nid in dismissed:
            self._fire_dismissed(nid)
        await self._save()
        self._notify()
        return removed

    async def clear_vision_events(self) -> list[dict]:
        """Remove all events and the notifications their toasts created; return them so the
        engine can clean up their files."""
        removed = self.vision_events
        self.vision_events = []
        dismissed = self._strip_vision_notifs(None)
        self.hass.bus.async_fire(EVENT_VISION_EVENT, {"cleared": True})
        for nid in dismissed:
            self._fire_dismissed(nid)
        await self._save()
        self._notify()
        return removed

    @staticmethod
    def _vision_public(event: dict) -> dict:
        """A copy of a vision event without internal file bookkeeping."""
        return {k: v for k, v in event.items() if not k.startswith("_")}

    def vision_events_public(self) -> list[dict]:
        """All events as frontend-safe copies (newest-first, no internal keys)."""
        return [self._vision_public(e) for e in self.vision_events]

    # ── announcements ───────────────────────────────────────
    async def announce(self, message, title="Announcement", icon=None, areas=None,
                       devices=None, persistent=False, timeout=None, volume=None,
                       source_device=None):
        """Broadcast a spoken announcement to the targeted areas/devices.

        Fires an "announcement"-source notification (a prominent, centered toast on
        the targeted screens) and speaks `message` on their players. "Until dismissed"
        (persistent) announcements stay + loop an alert chime after the spoken clip
        until dismissed; "Play once" announcements auto-dismiss after `timeout` seconds.

        `source_device` (the id of the device that sent it) is carried on the toast so
        recipients can Reply straight back to the sender.
        """
        message = (message or "").strip()
        if not message:
            return None
        areas = [a for a in (areas or []) if a]
        devices = [d for d in (devices or []) if d]
        nid = uuid.uuid4().hex
        # A primary area (first selected, else None = house-wide) keeps area_name/
        # notification-center filtering meaningful; announce_targets scopes the toast.
        primary_area = areas[0] if areas else None
        targets = {"areas": areas, "devices": devices}
        if source_device:
            targets["source_device"] = source_device
            targets["source_device_name"] = (
                self.device_registry.get(source_device, {}).get("name")
            )
        # 1) Prepare the audio first: resolve the target speaker(s) and pre-generate +
        # measure both spoken clips (this warms HA's TTS cache), so nothing is shown or
        # played until it's fully ready and exactly timed.
        prep = await self.playback.prepare_announcement(
            message, title=title, areas=areas, devices=devices, volume=volume
        )
        # 2) Now show the on-screen message (in sync with the audio starting).
        self._add_notification(
            title=title,
            message=message,
            severity="info",
            icon=icon or "mdi:bullhorn",
            area=primary_area,
            actions=None,
            notif_id=nid,
            # The timeout caps BOTH the on-screen message and the repeating alert sound
            # (0 = stay until manually dismissed). Same for both modes.
            timeout=timeout,
            persistence="normal" if persistent else "transient",
            source="announcement",
            announce_targets=targets,
            play_sound=False,
        )
        # 3) Play the prepared sequence (chime → preface → title → pause → message →
        # chime; "until dismissed" loops that closing chime until dismissed).
        # No-op when there's no speaker to target.
        self.playback.start_prepared(
            prep, nid, persistent=persistent, timeout=timeout,
        )
        # Auto-dismiss after the timeout: closes the message on every screen AND stops
        # the repeating alert sound (0/None = stay until the user dismisses it).
        if timeout and int(timeout) > 0:
            async_call_later(self.hass, float(timeout), self._announce_timeout_cb(nid))
        self._record_recent_announcement(
            message, title, icon, areas, devices, persistent, timeout, source_device,
        )
        await self._save()
        self._notify()
        return nid

    @callback
    def _announce_timeout_cb(self, notif_id):
        """A one-shot callback that auto-dismisses an announcement (box + sound)."""
        @callback
        def _cb(_now=None):
            self.hass.async_create_task(self.dismiss_notification(notif_id))

        return _cb

    def _record_recent_announcement(self, message, title, icon, areas, devices,
                                    persistent, timeout, source_device=None):
        """Add/refresh a preset in the Recent announcements list (per-device dedupe + cap)."""
        entry = {
            "id": uuid.uuid4().hex,
            "message": message,
            "title": title,
            "icon": icon,
            "areas": areas,
            "devices": devices,
            "persistent": persistent,
            "timeout": timeout,
            "source_device": source_device,
            "source_device_name": (
                self.device_registry.get(source_device, {}).get("name") if source_device else None
            ),
            "last_sent": dt_util.utcnow().isoformat(),
        }

        def _same(r):
            return (r.get("message") == message
                    and r.get("areas") == areas
                    and r.get("devices") == devices
                    and bool(r.get("persistent")) == bool(persistent)
                    and r.get("source_device") == source_device)

        merged = [entry] + [r for r in self.recent_announcements if not _same(r)]
        # Recent is PER-DEVICE: cap each sending device's history independently so a
        # busy device can't evict another device's recent announcements.
        counts: dict = {}
        kept = []
        for r in merged:
            key = r.get("source_device")
            counts[key] = counts.get(key, 0) + 1
            if counts[key] <= RECENT_ANNOUNCEMENTS_MAX:
                kept.append(r)
        self.recent_announcements = kept

    async def remove_recent_announcement(self, rid):
        """Drop an entry from the Recent announcements list."""
        self.recent_announcements = [
            r for r in self.recent_announcements if r.get("id") != rid
        ]
        await self._save()
        self._notify()

    # ── assist responses ────────────────────────────────────
    async def assist_response(self, message, title=None, image=None, areas=None,
                              devices=None, navigate=True, question=None):
        """Push a text answer to the targeted Assist-Response screens.

        Mirrors View Assist's "Info" view: an automation / voice pipeline calls this
        with the answer text; the targeted devices display the title + message (+ an
        optional background image) and — unless `navigate` is False — switch to the
        Assist-Response view. The latest answer is stored per target so a reloaded
        screen can restore it. No auto-revert: the content stays until replaced.
        """
        message = (message or "").strip()
        if not message:
            return None
        areas = [a for a in (areas or []) if a]
        devices = [d for d in (devices or []) if d]
        item = {
            "id": uuid.uuid4().hex,
            "title": (title or "").strip() or None,
            "question": (question or "").strip() or None,
            "message": message,
            "image": image or None,
            "areas": areas,
            "devices": devices,
            "ts": dt_util.utcnow().isoformat(),
        }
        # Store latest per target key (house-wide when no targets) for reload/late-join,
        # and append to that key's bounded conversation history for scroll-back.
        keys = [f"device:{d}" for d in devices] + [f"area:{a}" for a in areas] or ["house"]
        for key in keys:
            self.assist_responses[key] = item
            hist = self.assist_history.setdefault(key, [])
            hist.append(item)
            if len(hist) > ASSIST_HISTORY_MAX:
                del hist[: len(hist) - ASSIST_HISTORY_MAX]
        # Live push — each subscribed card filters by its own device/area.
        self.hass.bus.async_fire(EVENT_ASSIST_RESPONSE, item)
        # Reuse the existing navigation signal to switch explicit targets to the view.
        # House-wide (no targets) is content-only (never force every screen to jump).
        if navigate:
            for d in devices:
                self.hass.bus.async_fire(EVENT_NAVIGATE, {
                    "dashboard": "assist_response_dashboard", "area": None, "device_id": d,
                })
            for a in areas:
                self.hass.bus.async_fire(EVENT_NAVIGATE, {
                    "dashboard": "assist_response_dashboard", "area": a, "device_id": None,
                })
        await self._save()
        self._notify()
        return item["id"]

    # ── settings ────────────────────────────────────────────
    def effective_settings(self, device_id=None) -> dict:
        """Merge defaults ⊕ global ⊕ this device's overrides."""
        merged = dict(SETTINGS_DEFAULTS)
        merged.update(self.settings.get("global") or {})
        if device_id:
            merged.update(self.settings.get("devices", {}).get(device_id) or {})
        return merged

    def settings_payload(self) -> dict:
        """The full settings snapshot pushed to subscribers / exposed on the sensor."""
        global_settings = dict(self.settings.get("global") or {})
        if self.frigate_cameras:
            global_settings["frigate_cameras"] = {
                k: dict(v) for k, v in self.frigate_cameras.items()
            }
        return {
            "defaults": dict(SETTINGS_DEFAULTS),
            "global": global_settings,
            "devices": {k: dict(v) for k, v in (self.settings.get("devices") or {}).items()},
            "registry": {k: dict(v) for k, v in self.device_registry.items()},
        }

    def _fire_settings(self) -> None:
        """Fire EVENT_SETTINGS with the full snapshot, debounced: this payload goes to
        every connected client (every wall panel's navbar), so bursts of calls in quick
        succession (e.g. several `set_settings()`/`register_device()` calls, like a
        client reporting viewport changes) coalesce into one broadcast instead of one
        per call. In-process reads of `self.settings`/`settings_payload()` are unaffected
        since those read the already-updated in-memory state, not the event."""
        if self._settings_fire_unsub is not None:
            return

        def _fire(_now=None) -> None:
            self._settings_fire_unsub = None
            self.hass.bus.async_fire(EVENT_SETTINGS, self.settings_payload())

        self._settings_fire_unsub = async_call_later(self.hass, SETTINGS_FIRE_DEBOUNCE_S, _fire)

    async def set_settings(self, values: dict, scope="global", device_id=None) -> None:
        """Set one or more setting keys at the given scope. `None` value clears a key."""
        clean = {k: v for k, v in (values or {}).items() if k in SETTINGS_KEYS}
        if scope == "device":
            if not device_id:
                return
            target = self.settings["devices"].setdefault(device_id, {})
        else:
            target = self.settings["global"]
        for key, value in clean.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = value
        # Drop an emptied per-device override bucket so it fully inherits again.
        if scope == "device" and not self.settings["devices"].get(device_id):
            self.settings["devices"].pop(device_id, None)
        await self._save()
        self._fire_settings()
        self._notify()

    async def set_night_theme_snapshot(self, user_id: str, mode: str | None) -> None:
        """Save or clear one HA user's pre-night theme mode."""
        raw = self.settings["global"].get("night_dark_prev")
        snapshots = dict(raw) if isinstance(raw, dict) else {}
        if mode is None:
            snapshots.pop(user_id, None)
        else:
            snapshots[user_id] = mode
        if snapshots:
            self.settings["global"]["night_dark_prev"] = snapshots
        else:
            self.settings["global"].pop("night_dark_prev", None)
        await self._save()
        self._fire_settings()
        self._notify()

    async def clear_settings(self, keys=None, scope="global", device_id=None) -> None:
        """Clear specific keys (or all) at a scope so they inherit again."""
        if scope == "device":
            bucket = self.settings["devices"].get(device_id)
            if not bucket:
                return
            if keys is None:
                self.settings["devices"].pop(device_id, None)
            else:
                for key in keys:
                    bucket.pop(key, None)
                if not bucket:
                    self.settings["devices"].pop(device_id, None)
        else:
            if keys is None:
                self.settings["global"] = {}
            else:
                for key in keys:
                    self.settings["global"].pop(key, None)
        await self._save()
        self._fire_settings()
        self._notify()

    async def register_device(
        self, device_id, area=None, name=None, media_player=None,
        client_width=None, client_height=None,
        client_orientation=None, client_form_factor=None,
    ) -> None:
        """Record/refresh a device so server-side playback can target its area."""
        if not device_id:
            return
        entry = self.device_registry.setdefault(device_id, {})
        if area is not None:
            entry["area"] = area
        if name is not None:
            entry["name"] = name
        if media_player is not None:
            # The device's own media player (browser_mod / View Assist), used as the
            # final fallback when no per-device or global media_player is set.
            entry["media_player"] = media_player or None
        # Frontend-reported client characteristics (viewport / orientation).
        if client_width is not None:
            entry["client_width"] = client_width
        if client_height is not None:
            entry["client_height"] = client_height
        if client_orientation is not None:
            entry["client_orientation"] = client_orientation
        if client_form_factor is not None:
            entry["client_form_factor"] = client_form_factor
        entry["last_seen"] = dt_util.utcnow().isoformat()
        await self._save()
        self._fire_settings()
        self._notify()

    def _present_devices(self):
        """Registered devices seen within the presence TTL (device_id, entry)."""
        now = dt_util.utcnow()
        for did, entry in self.device_registry.items():
            seen = dt_util.parse_datetime(entry.get("last_seen") or "")
            if seen and (now - seen).total_seconds() <= DEVICE_PRESENCE_TTL:
                yield did, entry

    # ── notify sensors ──────────────────────────────────────
    def register(self, cb):
        self._update_cbs.add(cb)
        return lambda: self._update_cbs.discard(cb)

    def _notify(self):
        for cb in list(self._update_cbs):
            cb()

    async def refresh_requirements(self) -> None:
        """Re-run server-side dependency detection and update the sensor."""
        from .requirements import compute_requirements
        from .frigate import (
            detect_frigate,
            frigate_camera_meta,
            frigate_capability,
            frigate_url,
        )

        self.requirements = await compute_requirements(self.hass)
        det = detect_frigate(self.hass)
        self.frigate_cameras = frigate_camera_meta(self.hass) if det["installed"] else {}
        # Auto opt-in: Frigate is present with cameras but this install has no camera
        # list of its own yet — adopt it silently (no prompt) so cameras work out of
        # the box. When a list already exists, leave it and prompt instead.
        if (
            det["installed"] and det["cameras"] and not self.frigate_answered
            and not (self.settings.get("global") or {}).get("cameras_list")
        ):
            await self._adopt_frigate_cameras(det["cameras"])
        self.frigate = {
            "installed": det["installed"],
            "cameras": det["cameras"],
            "url": frigate_url(self.hass),
            "capability": frigate_capability(
                installed=det["installed"], cameras=det["cameras"],
                adopted=self.frigate_adopted, answered=self.frigate_answered,
            ),
        }
        if self.frigate_bridge is not None:
            await self.frigate_bridge.async_update()
        self._notify()

    async def _adopt_frigate_cameras(self, cameras: list[str] | None = None) -> None:
        """One-shot: replace the global camera list with Frigate's cameras."""
        if cameras is None:
            from .frigate import detect_frigate
            cameras = detect_frigate(self.hass)["cameras"]
        self.settings["global"]["cameras_list"] = list(cameras)
        self.frigate_adopted = True
        self.frigate_answered = True
        await self._save()
        self._fire_settings()
        self._notify()

    async def adopt_frigate_cameras(self) -> None:
        """User accepted the offer: adopt Frigate cameras, then refresh the sensor."""
        await self._adopt_frigate_cameras()
        await self.refresh_requirements()

    async def dismiss_frigate_prompt(self) -> None:
        """User declined the offer: stop prompting on every surface (and after restart)."""
        if self.frigate_answered:
            return
        self.frigate_answered = True
        await self._save()
        await self.refresh_requirements()

    async def maybe_notify_frigate(self) -> None:
        """Fire a one-time notification offering Frigate camera adoption."""
        if self.frigate_prompt_notified or self.frigate_answered:
            return
        if (self.frigate or {}).get("capability") != "available":
            return
        self.frigate_prompt_notified = True
        await self.notify(
            "Use Frigate cameras?",
            "Frigate is installed and exposing cameras. Ted's Dashboard can switch to "
            "them as your camera source \u2014 this does a one-time clear of your current "
            "camera list and replaces it with your Frigate cameras.",
            severity="info",
            icon="mdi:cctv",
            notif_id="frigate-adopt",
            persistence="sticky",
            actions=[
                {"label": "Use Frigate cameras", "action": "call-service",
                 "service": "teds_dashboard_system.adopt_frigate_cameras"},
                {"label": "No thanks", "action": "call-service",
                 "service": "teds_dashboard_system.dismiss_frigate_prompt"},
            ],
        )
