"""Ted's Dashboard System integration."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import timedelta
from functools import partial

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import Unauthorized
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.loader import async_get_integration

from .bing_photos import cache_has_images as bing_cache_has_images, fetch_and_cache_bing
from .cards import async_setup_cards, async_unload_cards
from .climate import apply_climate, resolve_climate_entity
from .dashboard import (
    async_install_dashboard,
    async_remove_dashboard_files,
    async_unregister_dashboard,
)
from .overrides import async_register_services as async_register_override_services
from .const import (
    CONF_DASHBOARD_BRANCH,
    CONF_DASHBOARD_REPO,
    CLIENT_RELOAD_ON_STARTUP_DELAY,
    DEFAULT_DASHBOARD_BRANCH,
    DEFAULT_DASHBOARD_REPO,
    DOMAIN,
    EVENT_NAVIGATE,
    MEDIA_FOLDER_NAME,
)
from .intents import async_register_intents
from .store import TedsManager
from .themes import async_install_bundled_themes
from .vision import VisionEngine
from .websocket import async_register as async_register_ws

PLATFORMS = [Platform.CALENDAR, Platform.SENSOR, Platform.UPDATE]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = TedsManager(hass)
    await manager.async_load()
    try:
        integration = await async_get_integration(hass, DOMAIN)
        manager.version = str(integration.version) if integration.version else None
    except Exception:  # noqa: BLE001 - version is best-effort, never block setup
        manager.version = None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    async_register_ws(hass)
    async_register_intents(hass)
    await _install_sentences(hass)
    await _register_sound_path(hass)
    await _register_background_path(hass)
    manager.announce_cache_dir = await _register_announce_cache_path(hass)
    manager.media_folder = await _ensure_media_folder(hass)
    await _register_media_paths(hass)
    manager.vision = VisionEngine(hass, manager)
    manager.vision.cache_dir = await _register_vision_cache_path(hass)
    manager.cards_js_url = await async_setup_cards(hass, manager.version)
    try:
        await async_install_bundled_themes(hass)
    except Exception:  # noqa: BLE001 - theme install must never block setup
        _LOGGER.exception("Ted's Themes install failed")
    try:
        await async_install_dashboard(hass)
    except Exception:  # noqa: BLE001 - dashboard install must never block setup
        _LOGGER.exception("Ted's Dashboard install failed")
    await _async_setup_updater(hass, entry, manager)
    async_register_override_services(hass)

    async def add_alarm(call: ServiceCall):
        await manager.add_alarm(
            call.data["label"], call.data["time"], call.data.get("days"),
            call.data.get("description", ""), call.data.get("enabled", True),
            call.data.get("location"),
        )

    async def update_alarm(call: ServiceCall):
        await manager.update_alarm(call.data["id"], **{k: call.data[k] for k in ("label", "time", "days", "description", "enabled", "location") if k in call.data})

    async def remove_alarm(call: ServiceCall):
        await manager.remove_alarm(call.data["id"])

    async def start_timer(call: ServiceCall):
        await manager.start_timer(call.data["name"], call.data.get("hours", 0), call.data.get("minutes", 0), call.data.get("seconds", 0), call.data.get("location"))

    async def cancel_timer(call: ServiceCall):
        manager.cancel_timer(call.data["id"])

    async def remove_recent(call: ServiceCall):
        await manager.remove_recent(
            call.data["name"], call.data.get("hours", 0), call.data.get("minutes", 0),
            call.data.get("seconds", 0), call.data.get("location"),
        )

    async def notify(call: ServiceCall):
        await manager.notify(
            call.data["title"], call.data["message"],
            severity=call.data.get("severity", "info"), icon=call.data.get("icon"),
            area=call.data.get("area"), actions=call.data.get("actions"),
            notif_id=call.data.get("id"), timeout=call.data.get("timeout"),
            persistence=call.data.get("persistence", "normal"),
        )

    async def dismiss_notification(call: ServiceCall):
        await manager.dismiss_notification(call.data["id"])

    async def mark_read(call: ServiceCall):
        await manager.mark_read(call.data.get("id"), call.data.get("area"))

    async def clear_notifications(call: ServiceCall):
        await manager.clear_notifications(call.data.get("area"))

    async def announce(call: ServiceCall):
        await manager.announce(
            call.data["message"], title=call.data.get("title", "Announcement"),
            icon=call.data.get("icon"), areas=call.data.get("areas"),
            devices=call.data.get("devices"), persistent=call.data.get("persistent", False),
            timeout=call.data.get("timeout"), volume=call.data.get("volume"),
            source_device=call.data.get("source_device"),
        )

    async def remove_announcement(call: ServiceCall):
        await manager.remove_recent_announcement(call.data["id"])

    async def assist_response(call: ServiceCall):
        await manager.assist_response(
            call.data["message"], title=call.data.get("title"),
            image=call.data.get("image"), areas=call.data.get("areas"),
            devices=call.data.get("devices"), navigate=call.data.get("navigate", True),
            question=call.data.get("question"),
        )

    async def _require_admin_for_global(call: ServiceCall) -> None:
        """Only admins may write Global settings; device-scope writes are open."""
        if call.data.get("scope", "global") != "global":
            return
        user_id = call.context.user_id
        if user_id is None:
            return  # internal/trusted call (no user context)
        user = await hass.auth.async_get_user(user_id)
        if not (user and user.is_admin):
            raise Unauthorized(context=call.context)

    async def set_setting(call: ServiceCall):
        await _require_admin_for_global(call)
        await manager.set_settings(
            {call.data["key"]: call.data.get("value")},
            scope=call.data.get("scope", "global"),
            device_id=call.data.get("device_id"),
        )

    async def clear_setting(call: ServiceCall):
        await _require_admin_for_global(call)
        key = call.data.get("key")
        await manager.clear_settings(
            keys=[key] if key else None,
            scope=call.data.get("scope", "global"),
            device_id=call.data.get("device_id"),
        )

    async def register_device(call: ServiceCall):
        await manager.register_device(
            call.data["device_id"], call.data.get("area"), call.data.get("name"),
            call.data.get("media_player"),
            client_width=call.data.get("client_width"),
            client_height=call.data.get("client_height"),
            client_orientation=call.data.get("client_orientation"),
            client_form_factor=call.data.get("client_form_factor"),
        )

    async def pause_timer(call: ServiceCall):
        manager.pause_timer(call.data["id"])

    async def resume_timer(call: ServiceCall):
        manager.resume_timer(call.data["id"])

    async def update_timer(call: ServiceCall):
        manager.update_timer(
            call.data["id"], name=call.data.get("name"),
            hours=call.data.get("hours"), minutes=call.data.get("minutes"), seconds=call.data.get("seconds"),
            location=call.data.get("location"), _set_location=("location" in call.data),
        )

    async def analyze_camera(call: ServiceCall):
        await manager.vision.analyze(
            call.data["camera_entity"], call.data.get("event_type", "manual")
        )

    async def delete_vision_event(call: ServiceCall):
        removed = await manager.remove_vision_event(call.data["id"])
        if removed:
            await manager.vision.cleanup_event(removed)

    async def clear_vision_events(call: ServiceCall):
        removed = await manager.clear_vision_events()
        if removed:
            await manager.vision.cleanup_events(removed)

    async def apply_climate_service(call: ServiceCall):
        """Run the smart climate logic (shared by voice intents + prompt buttons)."""
        entity_id = call.data.get("entity_id")
        if not entity_id:
            entity_id = resolve_climate_entity(
                hass, manager, call.data.get("zone"), call.data.get("area")
            )
        if not entity_id:
            return
        await apply_climate(
            hass, manager, entity_id=entity_id,
            kind=call.data.get("kind", "absolute"),
            temperature=call.data.get("temperature"),
            amount=call.data.get("amount"),
            direction=call.data.get("direction"),
            hvac_mode=call.data.get("hvac_mode"),
            preset=call.data.get("preset"),
            force_on=call.data.get("force_on", False),
        )

    hass.services.async_register(DOMAIN, "add_alarm", add_alarm, schema=vol.Schema({
        vol.Required("label"): cv.string, vol.Required("time"): cv.string,
        vol.Optional("days"): [int], vol.Optional("description"): cv.string, vol.Optional("enabled"): cv.boolean,
        vol.Optional("location"): vol.Any(None, cv.string)}))
    hass.services.async_register(DOMAIN, "update_alarm", update_alarm, schema=vol.Schema({vol.Required("id"): cv.string}, extra=vol.ALLOW_EXTRA))
    hass.services.async_register(DOMAIN, "remove_alarm", remove_alarm, schema=vol.Schema({vol.Required("id"): cv.string}))
    hass.services.async_register(DOMAIN, "start_timer", start_timer, schema=vol.Schema({
        vol.Required("name"): cv.string, vol.Optional("hours"): int, vol.Optional("minutes"): int, vol.Optional("seconds"): int,
        vol.Optional("location"): vol.Any(None, cv.string)}))
    hass.services.async_register(DOMAIN, "cancel_timer", cancel_timer, schema=vol.Schema({vol.Required("id"): cv.string}))
    hass.services.async_register(DOMAIN, "remove_recent", remove_recent, schema=vol.Schema({
        vol.Required("name"): cv.string, vol.Optional("hours"): int, vol.Optional("minutes"): int, vol.Optional("seconds"): int,
        vol.Optional("location"): vol.Any(None, cv.string)}))
    hass.services.async_register(DOMAIN, "pause_timer", pause_timer, schema=vol.Schema({vol.Required("id"): cv.string}))
    hass.services.async_register(DOMAIN, "resume_timer", resume_timer, schema=vol.Schema({vol.Required("id"): cv.string}))
    hass.services.async_register(DOMAIN, "update_timer", update_timer, schema=vol.Schema({
        vol.Required("id"): cv.string, vol.Optional("name"): cv.string,
        vol.Optional("hours"): int, vol.Optional("minutes"): int, vol.Optional("seconds"): int,
        vol.Optional("location"): vol.Any(None, cv.string)}))
    hass.services.async_register(DOMAIN, "notify", notify, schema=vol.Schema({
        vol.Required("title"): cv.string, vol.Required("message"): cv.string,
        vol.Optional("severity"): cv.string, vol.Optional("icon"): cv.string,
        vol.Optional("area"): vol.Any(None, cv.string), vol.Optional("actions"): list,
        vol.Optional("id"): cv.string, vol.Optional("timeout"): vol.Any(None, int),
        vol.Optional("persistence"): vol.In(("transient", "normal", "sticky"))}))
    hass.services.async_register(DOMAIN, "dismiss_notification", dismiss_notification, schema=vol.Schema({vol.Required("id"): cv.string}))
    hass.services.async_register(DOMAIN, "mark_read", mark_read, schema=vol.Schema({
        vol.Optional("id"): cv.string, vol.Optional("area"): vol.Any(None, cv.string)}))
    hass.services.async_register(DOMAIN, "clear_notifications", clear_notifications, schema=vol.Schema({
        vol.Optional("area"): vol.Any(None, cv.string)}))
    hass.services.async_register(DOMAIN, "announce", announce, schema=vol.Schema({
        vol.Required("message"): cv.string, vol.Optional("title"): cv.string,
        vol.Optional("icon"): vol.Any(None, cv.string),
        vol.Optional("areas"): [cv.string], vol.Optional("devices"): [cv.string],
        vol.Optional("persistent"): cv.boolean,
        vol.Optional("timeout"): vol.Any(None, int), vol.Optional("volume"): vol.Any(None, int),
        vol.Optional("source_device"): vol.Any(None, cv.string)}))
    hass.services.async_register(DOMAIN, "remove_announcement", remove_announcement, schema=vol.Schema({
        vol.Required("id"): cv.string}))
    hass.services.async_register(DOMAIN, "assist_response", assist_response, schema=vol.Schema({
        vol.Required("message"): cv.string, vol.Optional("title"): vol.Any(None, cv.string),
        vol.Optional("image"): vol.Any(None, cv.string),
        vol.Optional("question"): vol.Any(None, cv.string),
        vol.Optional("areas"): [cv.string], vol.Optional("devices"): [cv.string],
        vol.Optional("navigate"): cv.boolean}))
    hass.services.async_register(DOMAIN, "set_setting", set_setting, schema=vol.Schema({
        vol.Required("key"): cv.string, vol.Optional("value"): vol.Any(None, bool, int, float, cv.string, list, dict),
        vol.Optional("scope"): vol.In(["global", "device"]), vol.Optional("device_id"): cv.string}))
    hass.services.async_register(DOMAIN, "clear_setting", clear_setting, schema=vol.Schema({
        vol.Optional("key"): cv.string, vol.Optional("scope"): vol.In(["global", "device"]),
        vol.Optional("device_id"): cv.string}))
    hass.services.async_register(DOMAIN, "register_device", register_device, schema=vol.Schema({
        vol.Required("device_id"): cv.string, vol.Optional("area"): vol.Any(None, cv.string),
        vol.Optional("name"): vol.Any(None, cv.string),
        vol.Optional("media_player"): vol.Any(None, cv.string),
        vol.Optional("client_width"): vol.Any(None, int),
        vol.Optional("client_height"): vol.Any(None, int),
        vol.Optional("client_orientation"): vol.Any(None, cv.string),
        vol.Optional("client_form_factor"): vol.Any(None, cv.string)}))

    async def check_requirements(call: ServiceCall):
        await manager.refresh_requirements()

    hass.services.async_register(DOMAIN, "check_requirements", check_requirements, schema=vol.Schema({}))

    hass.services.async_register(DOMAIN, "analyze_camera", analyze_camera, schema=vol.Schema({
        vol.Required("camera_entity"): cv.entity_id, vol.Optional("event_type"): cv.string}))
    hass.services.async_register(DOMAIN, "delete_vision_event", delete_vision_event, schema=vol.Schema({
        vol.Required("id"): cv.string}))
    hass.services.async_register(DOMAIN, "clear_vision_events", clear_vision_events, schema=vol.Schema({}))

    hass.services.async_register(DOMAIN, "apply_climate", apply_climate_service, schema=vol.Schema({
        vol.Optional("entity_id"): cv.entity_id, vol.Optional("zone"): cv.string,
        vol.Optional("area"): vol.Any(None, cv.string),
        vol.Optional("kind"): vol.In(["absolute", "relative", "mode", "preset"]),
        vol.Optional("temperature"): vol.Coerce(float), vol.Optional("amount"): vol.Coerce(float),
        vol.Optional("direction"): vol.In(["warmer", "cooler"]),
        vol.Optional("hvac_mode"): cv.string, vol.Optional("preset"): cv.string,
        vol.Optional("force_on"): cv.boolean}))

    # Detect optional dependencies server-side. Run once HA has fully started (so
    # all integrations + Lovelace resources are loaded), then re-check when the
    # dashboards change and periodically.
    async def _refresh_reqs(*_):
        await manager.refresh_requirements()
        _check_unassigned_devices(hass)

    entry.async_on_unload(async_at_started(hass, _refresh_reqs))
    entry.async_on_unload(hass.bus.async_listen("lovelace_updated", _refresh_reqs))
    entry.async_on_unload(async_track_time_interval(hass, _refresh_reqs, timedelta(minutes=10)))

    # Recover dashboard clients after an HA restart: they often hang on a loading
    # spinner while HA was down. A short while after HA fully starts, refresh every
    # browser_mod browser once (mirrors a manual page refresh). Fires ONLY on a real
    # HA startup — not on an integration reload while HA is already running.
    @callback
    def _schedule_client_reload(_event: Event) -> None:
        async def _reload_clients(_now) -> None:
            if hass.services.has_service("browser_mod", "refresh"):
                await hass.services.async_call("browser_mod", "refresh", {}, blocking=False)

        entry.async_on_unload(
            async_call_later(hass, CLIENT_RELOAD_ON_STARTUP_DELAY, _reload_clients)
        )

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _schedule_client_reload)
    )

    # Keep the Bing "Photo of the Day" cache fresh once a day, but only when it's
    # already in use (non-empty) — never download for users who don't use it.
    async def _refresh_bing(*_):
        if await hass.async_add_executor_job(bing_cache_has_images):
            await fetch_and_cache_bing(hass)

    entry.async_on_unload(
        async_track_time_change(hass, _refresh_bing, hour=0, minute=10, second=0)
    )

    _setup_action_nudge(hass, entry, manager)

    manager.vision.async_setup(entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


# Services that should nudge a device's screen to a Ted's Cards view when fired
# by voice (or anything else) — mapped to the dashboard-path setting key.
_NUDGE_SERVICES: dict[tuple[str, str], str] = {
    ("climate", "set_temperature"): "climate_dashboard",
    ("climate", "set_hvac_mode"): "climate_dashboard",
    ("climate", "set_preset_mode"): "climate_dashboard",
    ("media_player", "play_media"): "music_dashboard",
    ("media_player", "media_play"): "music_dashboard",
}


def _service_entity_ids(data: dict) -> list[str]:
    """Extract entity ids from a service call's data (str or list)."""
    ent = (data or {}).get("entity_id")
    if not ent:
        return []
    return [ent] if isinstance(ent, str) else [e for e in ent if isinstance(e, str)]


def _entity_area(hass: HomeAssistant, entity_id: str) -> str | None:
    """Resolve an entity's area (its own, else its device's)."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device and device.area_id:
            return device.area_id
    return None


def _is_music_player(hass: HomeAssistant, entity_id: str) -> bool:
    """True when the media_player is a Music Assistant player (the Music view's domain)."""
    entry = er.async_get(hass).async_get(entity_id)
    return bool(entry and entry.platform == "music_assistant")


def _setup_action_nudge(hass: HomeAssistant, entry: ConfigEntry, manager) -> None:
    """When a climate/music action fires, nudge that area's screens to the matching view.

    Gated by the global `nav_follow_actions` setting. Music nudges are limited to
    Music Assistant players so TTS/announcements on other players don't trigger them.
    Only fires when the affected entity's area is known (so a device can target it).
    """

    @callback
    def _on_call_service(event: Event) -> None:
        if manager.effective_settings().get("nav_follow_actions", True) is False:
            return
        key = _NUDGE_SERVICES.get((event.data.get("domain"), event.data.get("service")))
        if not key:
            return
        music = key == "music_dashboard"
        for entity_id in _service_entity_ids(event.data.get("service_data") or {}):
            if music and not _is_music_player(hass, entity_id):
                continue
            area = _entity_area(hass, entity_id)
            if area:
                hass.bus.async_fire(
                    EVENT_NAVIGATE,
                    {"dashboard": key, "area": area, "device_id": None},
                )
                return  # one nudge per call is enough

    entry.async_on_unload(hass.bus.async_listen(EVENT_CALL_SERVICE, _on_call_service))


@callback
def _check_unassigned_devices(hass: HomeAssistant) -> None:
    """Raise/clear a repair issue when Companion-app devices have no area.

    Voice commands from an area-less device aren't room-aware, so surface it for admins.
    """
    dev_reg = dr.async_get(hass)
    unassigned = [
        d
        for d in dev_reg.devices.values()
        if d.area_id is None
        and not d.disabled_by
        and any(ident[0] == "mobile_app" for ident in d.identifiers)
    ]
    if unassigned:
        names = ", ".join(sorted((d.name_by_user or d.name or d.id) for d in unassigned))
        ir.async_create_issue(
            hass,
            DOMAIN,
            "unassigned_mobile_app_devices",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unassigned_mobile_app_devices",
            translation_placeholders={"count": str(len(unassigned)), "devices": names},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, "unassigned_mobile_app_devices")


async def _install_sentences(hass: HomeAssistant) -> None:
    """Install bundled Assist sentences into <config>/custom_sentences/<lang>/.

    The default conversation agent only auto-loads sentences from that folder,
    so a bundled integration must copy them there. Best-effort: never blocks
    setup. Reloads the conversation agent when the file changed so voice works
    without a restart.
    """
    src = os.path.join(os.path.dirname(__file__), "sentences", "en.yaml")
    dest_dir = hass.config.path("custom_sentences", "en")
    dest = os.path.join(dest_dir, f"{DOMAIN}.yaml")

    def _copy_if_changed() -> bool:
        try:
            if not os.path.exists(src):
                return False
            os.makedirs(dest_dir, exist_ok=True)
            if os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(src):
                return False
            shutil.copyfile(src, dest)
            return True
        except OSError:
            return False

    changed = await hass.async_add_executor_job(_copy_if_changed)
    if changed and hass.services.has_service("conversation", "reload"):
        try:
            await hass.services.async_call("conversation", "reload", {}, blocking=False)
        except Exception:  # noqa: BLE001 - reload is best-effort
            pass


async def _register_sound_path(hass: HomeAssistant) -> None:
    """Serve bundled alert sounds at /teds_dashboard_system/sounds/* (once)."""
    flag = f"{DOMAIN}_sounds_registered"
    if hass.data.get(flag):
        return
    sounds_dir = os.path.join(os.path.dirname(__file__), "sounds")
    url = "/teds_dashboard_system/sounds"
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, sounds_dir, False)]
        )
        hass.data[flag] = True
    except Exception:  # noqa: BLE001 - fall back for older HA cores
        try:
            hass.http.register_static_path(url, sounds_dir, False)
            hass.data[flag] = True
        except Exception:  # noqa: BLE001
            pass


async def _register_announce_cache_path(hass: HomeAssistant) -> str | None:
    """Serve stitched announcement clips at /teds_dashboard_system/announce_cache/* and
    return the writable cache directory (under the config dir, so it survives updates)."""
    cache_dir = hass.config.path(f"{DOMAIN}_cache")
    try:
        await hass.async_add_executor_job(lambda: os.makedirs(cache_dir, exist_ok=True))
    except Exception:  # noqa: BLE001 - if we can't make the dir, combining is skipped
        return None
    flag = f"{DOMAIN}_announce_cache_registered"
    if not hass.data.get(flag):
        url = "/teds_dashboard_system/announce_cache"
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(url, cache_dir, False)]
            )
            hass.data[flag] = True
        except Exception:  # noqa: BLE001 - fall back for older HA cores
            try:
                hass.http.register_static_path(url, cache_dir, False)
                hass.data[flag] = True
            except Exception:  # noqa: BLE001
                return None
    return cache_dir


async def _register_vision_cache_path(hass: HomeAssistant) -> str | None:
    """Serve Vision Analysis thumbnails/clips at /teds_dashboard_system/vision_cache/*
    and return the writable cache directory (under the config dir, survives updates)."""
    cache_dir = hass.config.path(f"{DOMAIN}_vision_cache")
    try:
        await hass.async_add_executor_job(lambda: os.makedirs(cache_dir, exist_ok=True))
    except Exception:  # noqa: BLE001 - if we can't make the dir, capture just skips media
        return None
    flag = f"{DOMAIN}_vision_cache_registered"
    if not hass.data.get(flag):
        url = "/teds_dashboard_system/vision_cache"
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(url, cache_dir, False)]
            )
            hass.data[flag] = True
        except Exception:  # noqa: BLE001 - fall back for older HA cores
            try:
                hass.http.register_static_path(url, cache_dir, False)
                hass.data[flag] = True
            except Exception:  # noqa: BLE001
                return None
    return cache_dir


async def _register_background_path(hass: HomeAssistant) -> None:
    """Serve bundled wallpaper images at /teds_dashboard_system/backgrounds/* (once)."""
    flag = f"{DOMAIN}_backgrounds_registered"
    if hass.data.get(flag):
        return
    backgrounds_dir = os.path.join(os.path.dirname(__file__), "backgrounds")
    url = "/teds_dashboard_system/backgrounds"
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, backgrounds_dir, False)]
        )
        hass.data[flag] = True
    except Exception:  # noqa: BLE001 - fall back for older HA cores
        try:
            hass.http.register_static_path(url, backgrounds_dir, False)
            hass.data[flag] = True
        except Exception:  # noqa: BLE001
            pass


async def _register_media_paths(hass: HomeAssistant) -> None:
    """Make Photos/Bing favorites, imported wallpapers, and the Bing "removed"
    blocklist survive integration updates:

    - Favorites  → <media>/Ted Dash System/Favorites   (served /teds_dashboard_system/media_favorites)
    - Wallpapers → <media>/Ted Dash System/Wallpapers  (served /teds_dashboard_system/media_wallpapers)
    - Bing blocklist → <config>/teds_dashboard_system_data/  (config-root, survives updates)

    Best-effort: the media paths no-op when no media dir is configured (favorites/
    wallpapers then fall back to the integration folder)."""
    from .bing_photos import (
        MEDIA_FAVORITES_URL,
        MEDIA_WALLPAPERS_URL,
        media_favorites_dir,
        media_wallpapers_dir,
        set_data_dir,
    )

    # Persistent data dir (config-root) for the Bing removed-blocklist.
    data_dir = hass.config.path(f"{DOMAIN}_data")
    try:
        await hass.async_add_executor_job(lambda: os.makedirs(data_dir, exist_ok=True))
        set_data_dir(data_dir)
    except OSError:
        pass

    # Serve the media-folder albums (favorites + imported wallpapers) read-only.
    flag = f"{DOMAIN}_media_paths_registered"
    if hass.data.get(flag):
        return
    targets = [
        (MEDIA_FAVORITES_URL, media_favorites_dir(hass)),
        (MEDIA_WALLPAPERS_URL, media_wallpapers_dir(hass)),
    ]
    registered = False
    for url, fs in targets:
        if not fs:
            continue
        try:
            await hass.async_add_executor_job(lambda p=fs: os.makedirs(p, exist_ok=True))
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths([StaticPathConfig(url, fs, False)])
            registered = True
        except Exception:  # noqa: BLE001 - fall back for older HA cores
            try:
                hass.http.register_static_path(url, fs, False)
                registered = True
            except Exception:  # noqa: BLE001
                pass
    if registered:
        hass.data[flag] = True


async def _ensure_media_folder(hass: HomeAssistant) -> str | None:
    """Create a dedicated "Ted Dash System" folder under HA's local "My media"
    source and return its media-source URI, so Background wallpaper uploads land
    there and the media pickers can open into it.

    Uses the first configured `media_dirs` entry (the default local source).
    Best-effort: returns None if no media dir is configured or creation fails.
    """
    media_dirs = getattr(hass.config, "media_dirs", None) or {}
    source_dir_id = next(iter(media_dirs), None)
    if source_dir_id is None:
        return None
    folder = os.path.join(media_dirs[source_dir_id], MEDIA_FOLDER_NAME)
    try:
        await hass.async_add_executor_job(lambda: os.makedirs(folder, exist_ok=True))
    except OSError:
        return None
    return f"media-source://media_source/{source_dir_id}/{MEDIA_FOLDER_NAME}"


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        manager = hass.data[DOMAIN].pop(entry.entry_id)
        async_unload_cards(hass, getattr(manager, "cards_js_url", None))
        async_unregister_dashboard(hass)
        manager.shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Full cleanup on uninstall. Runtime registrations (panel, cards URL, static
    path) are already removed by async_unload_entry; here we remove the on-disk
    artifacts: the generated + managed dashboard files (the user overlay
    ``ted-dashboard-user/`` is kept) and the installer state store. The settings /
    alarms store is left intact so a reinstall keeps the user's data."""
    try:
        await async_remove_dashboard_files(hass)
    except Exception:  # noqa: BLE001 - best-effort cleanup, never raise on uninstall
        _LOGGER.exception("Failed to remove Ted's Dashboard files on uninstall")
    try:
        from .updater import async_load_installer_state

        store, _ = await async_load_installer_state(hass)
        await store.async_remove()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed to remove the installer state on uninstall")


async def _async_setup_updater(
    hass: HomeAssistant, entry: ConfigEntry, manager: TedsManager
) -> None:
    """Create the update coordinator and start a background first refresh."""
    from .github import GitHubClient
    from .updater import DashboardUpdateCoordinator, async_load_installer_state

    repo = entry.options.get(CONF_DASHBOARD_REPO, DEFAULT_DASHBOARD_REPO)
    branch = entry.options.get(CONF_DASHBOARD_BRANCH, DEFAULT_DASHBOARD_BRANCH)
    store, state = await async_load_installer_state(hass)
    coordinator = DashboardUpdateCoordinator(
        hass, GitHubClient(hass, repo, branch), store, state
    )
    manager.updater = coordinator
    entry.async_on_unload(
        coordinator.async_add_listener(
            partial(_maybe_autoupdate, hass, manager, coordinator)
        )
    )
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN}_update_check"
    )


@callback
def _maybe_autoupdate(hass: HomeAssistant, manager: TedsManager, coordinator) -> None:
    """Auto-install a newer dashboard when the 'dashboard_auto_update' setting allows."""
    if not coordinator.last_update_success or not coordinator.update_available:
        return
    if manager.effective_settings().get("dashboard_auto_update", True) is not True:
        return
    hass.async_create_task(coordinator.async_install_now())
