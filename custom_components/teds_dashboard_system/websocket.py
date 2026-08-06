"""WebSocket API for Ted's Dashboard System.

Non-admin HA users (e.g. kiosk / Wallpanel users) are not allowed to
`subscribe_events` for custom event types, so cards cannot listen to
`teds_dashboard_system_notification` directly. This command lets any authenticated
user subscribe to notifications via a dedicated, non-admin command instead.
"""

from __future__ import annotations

import asyncio
import logging
import os

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr

from .bing_photos import (
    clear_bing_cache,
    favorite_bing_photo,
    fetch_and_cache_bing,
    import_photo,
    list_favorites,
    remove_bing_photo,
)
from .const import (
    DASHBOARD_USER_DIR,
    DASHBOARDS_DIR,
    DOMAIN,
    EVENT_ASSIST_RESPONSE,
    EVENT_BING_REMOVED,
    EVENT_DASHBOARD_UPDATED,
    EVENT_NAVIGATE,
    EVENT_NOTIFICATION,
    EVENT_SETTINGS,
    EVENT_VISION_EVENT,
)
from .frigate import async_mark_frigate_reviewed
from .vision import (
    ai_task_entities,
    discover_camera_detectors,
    frigate_native_camera,
    preferred_ai_task_entity,
)

_REGISTERED = f"{DOMAIN}_ws_registered"

# Bundled wallpaper folders -> the category returned to the frontend.
_BACKGROUND_DIRS = {"general": "general", "light": "light-mode", "dark": "dark-mode"}
_BACKGROUND_EXTS = (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif")
_BACKGROUND_URL = "/teds_dashboard_system/backgrounds"

# Bundled alert sounds live here and are served at this URL prefix.
_SOUNDS_URL = "/teds_dashboard_system/sounds"
# Filename prefix -> category label shown in the sound picker.
_SOUND_CATEGORIES = (("alarm", "Alarm"), ("timer", "Timer"), ("notification", "Notification"))


def _manager(hass: HomeAssistant):
    """The single TedsManager for the integration (first config entry)."""
    return next(iter((hass.data.get(DOMAIN) or {}).values()), None)


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the WebSocket commands once."""
    if hass.data.get(_REGISTERED):
        return
    websocket_api.async_register_command(hass, handle_subscribe_notifications)
    websocket_api.async_register_command(hass, handle_subscribe_settings)
    websocket_api.async_register_command(hass, handle_subscribe_navigate)
    websocket_api.async_register_command(hass, handle_subscribe_assist_responses)
    websocket_api.async_register_command(hass, handle_subscribe_dashboard_updated)
    websocket_api.async_register_command(hass, handle_register_device)
    websocket_api.async_register_command(hass, handle_list_backgrounds)
    websocket_api.async_register_command(hass, handle_list_sounds)
    websocket_api.async_register_command(hass, handle_list_bing_photos)
    websocket_api.async_register_command(hass, handle_clear_bing_photos_cache)
    websocket_api.async_register_command(hass, handle_favorite_bing_photo)
    websocket_api.async_register_command(hass, handle_remove_bing_photo)
    websocket_api.async_register_command(hass, handle_subscribe_bing_removed)
    websocket_api.async_register_command(hass, handle_favorite_photo)
    websocket_api.async_register_command(hass, handle_store_background_photo)
    websocket_api.async_register_command(hass, handle_list_favorites)
    websocket_api.async_register_command(hass, handle_media_folder)
    websocket_api.async_register_command(hass, handle_list_dashboard_views)
    websocket_api.async_register_command(hass, handle_create_ma_player)
    websocket_api.async_register_command(hass, handle_set_device_area)
    websocket_api.async_register_command(hass, handle_subscribe_vision_events)
    websocket_api.async_register_command(hass, handle_list_vision_events)
    websocket_api.async_register_command(hass, handle_list_camera_detectors)
    websocket_api.async_register_command(hass, handle_list_ai_task_entities)
    websocket_api.async_register_command(hass, handle_mark_vision_reviewed)
    websocket_api.async_register_command(hass, handle_delete_vision_event)
    websocket_api.async_register_command(hass, handle_clear_vision_events)
    hass.data[_REGISTERED] = True


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_device_area",
        vol.Required("device_id"): str,
        vol.Required("area_id"): vol.Any(None, str),
    }
)
@websocket_api.async_response
async def handle_set_device_area(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Assign an HA device to an area on behalf of the frontend.

    Admins may set any device. Non-admin (kiosk/wall-panel) users may only set a
    device that currently has NO area, and only when the `allow_device_area_self_assign`
    setting is on — so a panel can fix its own missing room but can't reassign
    already-configured devices.
    """
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(msg["device_id"])
    if device is None:
        connection.send_error(msg["id"], "not_found", "Device not found")
        return
    is_admin = bool(connection.user and connection.user.is_admin)
    if not is_admin:
        mgr = _manager(hass)
        allowed = bool(
            mgr and mgr.effective_settings().get("allow_device_area_self_assign", True)
        )
        if not allowed:
            connection.send_error(msg["id"], "unauthorized", "Self-assignment is disabled")
            return
        if device.area_id:
            connection.send_error(msg["id"], "unauthorized", "Device already has an area")
            return
    area_id = msg["area_id"]
    if area_id and ar.async_get(hass).async_get_area(area_id) is None:
        connection.send_error(msg["id"], "not_found", "Area not found")
        return
    dev_reg.async_update_device(msg["device_id"], area_id=area_id)
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_notifications"}
)
@callback
def handle_subscribe_notifications(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Forward backend notification events to the subscribing connection."""

    @callback
    def forward(event: Event) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event.data))

    connection.subscriptions[msg["id"]] = hass.bus.async_listen(
        EVENT_NOTIFICATION, forward
    )
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_settings"}
)
@callback
def handle_subscribe_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Push the current settings snapshot, then forward settings updates."""

    @callback
    def forward(event: Event) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event.data))

    connection.subscriptions[msg["id"]] = hass.bus.async_listen(EVENT_SETTINGS, forward)
    connection.send_result(msg["id"])
    mgr = _manager(hass)
    if mgr:
        connection.send_message(
            websocket_api.event_message(msg["id"], mgr.settings_payload())
        )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_navigate"}
)
@callback
def handle_subscribe_navigate(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Forward backend navigation signals to the subscribing connection.

    Each event is ``{dashboard, area, device_id}``; the frontend decides whether
    it targets this device (by area or device id) and navigates accordingly.
    """

    @callback
    def forward(event: Event) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event.data))

    connection.subscriptions[msg["id"]] = hass.bus.async_listen(EVENT_NAVIGATE, forward)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_assist_responses"}
)
@callback
def handle_subscribe_assist_responses(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Forward Assist-Response pushes to the subscribing connection.

    Each event is the answer item ``{id, title, message, image, areas, devices, ts}``;
    the frontend card decides whether it targets this device (by area or device id).
    """

    @callback
    def forward(event: Event) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event.data))

    connection.subscriptions[msg["id"]] = hass.bus.async_listen(
        EVENT_ASSIST_RESPONSE, forward
    )
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_dashboard_updated"}
)
@callback
def handle_subscribe_dashboard_updated(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Forward dashboard-update signals so non-admin panels can auto-refresh.

    Non-admin (kiosk/Wallpanel) users can't ``subscribe_events`` for custom event
    types, so they subscribe through this command instead of listening to
    ``EVENT_DASHBOARD_UPDATED`` directly.
    """

    @callback
    def forward(event: Event) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event.data))

    connection.subscriptions[msg["id"]] = hass.bus.async_listen(
        EVENT_DASHBOARD_UPDATED, forward
    )
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/register_device",
        vol.Required("device_id"): str,
        vol.Optional("area"): vol.Any(None, str),
        vol.Optional("name"): vol.Any(None, str),
        vol.Optional("media_player"): vol.Any(None, str),
        vol.Optional("client_width"): vol.Any(None, int),
        vol.Optional("client_height"): vol.Any(None, int),
        vol.Optional("client_orientation"): vol.Any(None, str),
        vol.Optional("client_form_factor"): vol.Any(None, str),
    }
)
@websocket_api.async_response
async def handle_register_device(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Let a (non-admin) device register its id + area for settings targeting."""
    mgr = _manager(hass)
    if mgr:
        await mgr.register_device(
            msg["device_id"], msg.get("area"), msg.get("name"), msg.get("media_player"),
            client_width=msg.get("client_width"),
            client_height=msg.get("client_height"),
            client_orientation=msg.get("client_orientation"),
            client_form_factor=msg.get("client_form_factor"),
        )
    connection.send_result(msg["id"])


def _scan_backgrounds() -> dict:
    """Enumerate bundled wallpaper images grouped by category (blocking I/O)."""
    base = os.path.join(os.path.dirname(__file__), "backgrounds")
    out: dict[str, list[str]] = {}
    for category, folder in _BACKGROUND_DIRS.items():
        path = os.path.join(base, folder)
        try:
            names = sorted(os.listdir(path))
        except OSError:
            names = []
        out[category] = [
            f"{_BACKGROUND_URL}/{folder}/{name}"
            for name in names
            if name.lower().endswith(_BACKGROUND_EXTS)
        ]
    return out


def _scan_sounds() -> list[dict]:
    """Enumerate bundled alert sounds with a friendly name + category (blocking I/O).

    Each entry is {file, url, name, category}. The category comes from the filename
    prefix (alarm/timer/notification), and the name is a title-cased version of the
    stem (e.g. "notification-alt1" -> "Notification Alt1").
    """
    base = os.path.join(os.path.dirname(__file__), "sounds")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        if not name.lower().endswith(".mp3"):
            continue
        stem = name[:-4]
        low = stem.lower()
        category = next((label for pfx, label in _SOUND_CATEGORIES if low.startswith(pfx)), "Other")
        friendly = stem.replace("-", " ").replace("_", " ").strip().title()
        out.append({
            "file": name,
            "url": f"{_SOUNDS_URL}/{name}",
            "name": friendly,
            "category": category,
        })
    return out


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_backgrounds"}
)
@websocket_api.async_response
async def handle_list_backgrounds(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return the bundled wallpaper image URLs grouped by category."""
    result = await hass.async_add_executor_job(_scan_backgrounds)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_sounds"}
)
@websocket_api.async_response
async def handle_list_sounds(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return the bundled alert sounds (url, friendly name, category)."""
    result = await hass.async_add_executor_job(_scan_sounds)
    connection.send_result(msg["id"], {"sounds": result})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_bing_photos"}
)
@websocket_api.async_response
async def handle_list_bing_photos(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Ensure the Bing "Photo of the Day" cache is fresh and return its photos.

    Returns ``{photos: [{url, title, copyright, startdate}, ...]}`` newest-first.
    """
    photos = await fetch_and_cache_bing(hass)
    connection.send_result(msg["id"], {"photos": photos})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/clear_bing_photos_cache"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def handle_clear_bing_photos_cache(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Delete all cached Bing images (admin only — the cache is HA-wide)."""
    await clear_bing_cache(hass)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/favorite_bing_photo",
        vol.Required("filename"): str,
    }
)
@websocket_api.async_response
async def handle_favorite_bing_photo(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Copy a cached Bing image into the favorites folder."""
    ok = await favorite_bing_photo(hass, msg["filename"])
    connection.send_result(msg["id"], {"success": ok})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/remove_bing_photo",
        vol.Required("filename"): str,
    }
)
@websocket_api.async_response
async def handle_remove_bing_photo(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Delete a single cached Bing image from the cache and tell every device to
    drop it from its live slideshow."""
    ok = await remove_bing_photo(hass, msg["filename"])
    if ok:
        hass.bus.async_fire(EVENT_BING_REMOVED, {"filename": msg["filename"]})
    connection.send_result(msg["id"], {"success": ok})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_bing_removed"}
)
@callback
def handle_subscribe_bing_removed(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Forward Bing photo-removed signals so every device drops the image live.

    Each event is ``{filename}``; the background engine removes that image from its
    in-memory slideshow (advancing if it was the current slide). Non-admin (kiosk /
    Wallpanel) users can't ``subscribe_events`` for custom event types, so they
    subscribe through this command instead.
    """

    @callback
    def forward(event: Event) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event.data))

    connection.subscriptions[msg["id"]] = hass.bus.async_listen(
        EVENT_BING_REMOVED, forward
    )
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/favorite_photo",
        vol.Required("ref"): str,
    }
)
@websocket_api.async_response
async def handle_favorite_photo(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Import an arbitrary image into the favorites folder (deduped)."""
    url = await import_photo(hass, msg["ref"], "favorites")
    connection.send_result(msg["id"], {"success": url is not None, "url": url})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/store_background_photo",
        vol.Required("ref"): str,
    }
)
@websocket_api.async_response
async def handle_store_background_photo(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Import an arbitrary image into the stored-background folder (deduped)."""
    url = await import_photo(hass, msg["ref"], "stored")
    connection.send_result(msg["id"], {"success": url is not None, "url": url})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_favorites"}
)
@websocket_api.async_response
async def handle_list_favorites(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return the served URLs of every favorited photo."""
    photos = await list_favorites(hass)
    connection.send_result(msg["id"], {"photos": photos})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/media_folder"}
)
@callback
def handle_media_folder(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return the media-source URI of the dedicated wallpaper folder (or null)."""
    mgr = _manager(hass)
    connection.send_result(
        msg["id"], {"media_content_id": mgr.media_folder if mgr else None}
    )


def _scan_user_overlay(dashboards_dir: str) -> tuple[list[str], set[str]]:
    """Return (custom view files, override filenames) from the ted-dashboard-user overlay."""
    base = os.path.join(dashboards_dir, DASHBOARD_USER_DIR)
    views: list[str] = []
    overrides: set[str] = set()
    try:
        views = sorted(
            f
            for f in os.listdir(os.path.join(base, "views"))
            if f.endswith((".yaml", ".yml"))
        )
    except OSError:
        pass
    try:
        overrides = {
            f
            for f in os.listdir(os.path.join(base, "overrides"))
            if f.endswith((".yaml", ".yml"))
        }
    except OSError:
        pass
    return views, overrides


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_dashboard_views"}
)
@websocket_api.async_response
async def handle_list_dashboard_views(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Installed managed views + custom views with fork/drift status.

    Powers the Settings → Dashboard management UI: each managed view reports its
    installed version, the latest upstream version, whether it's been forked into the
    user overrides, and whether the upstream has drifted past the forked version.
    """
    from .dashboard import _version_gt, effective_view_order
    from .updater import async_load_installer_state

    _, state = await async_load_installer_state(hass)
    installed = state.get("versions", {}).get("dashboard", {}) or {}
    order = (installed.get("layout", {}) or {}).get("order", []) or []
    inst_views = installed.get("views", {}) or {}
    forks = state.get("forks", {}) or {}
    ordered, hidden_set = effective_view_order(order, state.get("layout") or {})

    manager = _manager(hass)
    remote = getattr(getattr(manager, "updater", None), "remote_versions", None) or {}
    remote_views = remote.get("views", {}) or {}
    remote_dash = remote.get("dashboard")
    inst_dash = installed.get("dashboard")

    dashboards_dir = hass.config.path(DASHBOARDS_DIR)
    custom_files, override_files = await hass.async_add_executor_job(
        _scan_user_overlay, dashboards_dir
    )

    views: list[dict] = []
    for rel in ordered:
        file = os.path.basename(rel)
        stem = file[:-5] if file.endswith(".yaml") else file
        forked = file in override_files
        fork_ver = forks.get(file)
        latest = remote_views.get(stem)
        drift = bool(forked and latest and fork_ver and _version_gt(latest, fork_ver))
        views.append(
            {
                "name": stem,
                "file": file,
                "version": inst_views.get(stem),
                "latest": latest,
                "forked": forked,
                "fork_version": fork_ver,
                "drift": drift,
                "hidden": file in hidden_set,
            }
        )

    customs = [
        {"file": f, "name": f[:-5] if f.endswith(".yaml") else f} for f in custom_files
    ]

    connection.send_result(
        msg["id"],
        {
            "dashboard_version": inst_dash,
            "latest_version": remote_dash,
            "update_available": bool(
                remote_dash and inst_dash and _version_gt(remote_dash, inst_dash)
            ),
            "views": views,
            "custom_views": customs,
        },
    )


# Serializes Music Assistant player creation so concurrent auto-expose calls from several
# devices don't race the MA provider/player config saves.
_CREATE_MA_LOCK = asyncio.Lock()
_LOGGER = logging.getLogger(__name__)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/create_ma_player",
        vol.Required("entity_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def handle_create_ma_player(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Auto-create/configure a Music Assistant player for a device's media_player.

    Drives the Music Assistant server API (via the shared music_assistant client) to add
    the entity to the Home Assistant MediaPlayers provider and configure the resulting
    player. Admin-only, since it changes Music Assistant's configuration.

    Serialized with a module-level lock so several devices auto-exposing at once don't
    race Music Assistant's provider/player config saves. A `GUIDE_HASS_PLUGIN`-prefixed
    error (external MA needs a URL + token) is reported as the distinct `needs_hass_setup`
    code so the UI can show a guided step rather than a hard failure.
    """
    from .music_assistant import GUIDE_HASS_PLUGIN, GUIDE_NEEDS_TOKEN, async_create_music_player

    user = connection.user
    try:
        async with _CREATE_MA_LOCK:
            result = await async_create_music_player(
                hass,
                msg["entity_id"],
                getattr(user, "id", None),
                getattr(user, "name", None),
            )
    except HomeAssistantError as err:
        message = str(err)
        if message.startswith(GUIDE_NEEDS_TOKEN):
            _LOGGER.info("create_ma_player needs an admin token for %s", msg["entity_id"])
            connection.send_error(
                msg["id"], "needs_admin_token", message[len(GUIDE_NEEDS_TOKEN) :]
            )
        elif message.startswith(GUIDE_HASS_PLUGIN):
            _LOGGER.info("create_ma_player needs guided setup for %s: %s", msg["entity_id"], message)
            connection.send_error(
                msg["id"], "needs_hass_setup", message[len(GUIDE_HASS_PLUGIN) :]
            )
        else:
            _LOGGER.warning("create_ma_player failed for %s: %s", msg["entity_id"], message)
            connection.send_error(msg["id"], "create_failed", message)
        return
    except Exception as err:  # noqa: BLE001 - surface any unexpected failure to the UI
        _LOGGER.exception("create_ma_player crashed for %s", msg["entity_id"])
        connection.send_error(msg["id"], "unknown_error", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe_vision_events"}
)
@callback
def handle_subscribe_vision_events(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Push the current vision events, then forward new/updated/removed ones."""

    @callback
    def forward(event: Event) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event.data))

    connection.subscriptions[msg["id"]] = hass.bus.async_listen(EVENT_VISION_EVENT, forward)
    connection.send_result(msg["id"])
    mgr = _manager(hass)
    if mgr:
        connection.send_message(
            websocket_api.event_message(msg["id"], {"events": mgr.vision_events_public()})
        )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_vision_events"}
)
@callback
def handle_list_vision_events(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return all stored Vision Analysis events (newest-first)."""
    mgr = _manager(hass)
    events = mgr.vision_events_public() if mgr else []
    connection.send_result(msg["id"], {"events": events})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/list_camera_detectors",
        vol.Required("camera_entity"): str,
    }
)
@callback
def handle_list_camera_detectors(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return the detection event types discoverable for a camera (for the opt-in UI), plus
    whether Frigate's native alert detection will drive it."""
    cam = msg["camera_entity"]
    mgr = _manager(hass)
    settings = mgr.effective_settings() if mgr else {}
    connection.send_result(
        msg["id"],
        {
            "detectors": discover_camera_detectors(hass, cam),
            "frigate_native": frigate_native_camera(hass, settings, cam),
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/list_ai_task_entities"}
)
@callback
def handle_list_ai_task_entities(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """List ai_task entities + attachment support + the preferred one (for onboarding)."""
    connection.send_result(
        msg["id"],
        {
            "entities": ai_task_entities(hass),
            "preferred": preferred_ai_task_entity(hass),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/mark_vision_reviewed",
        vol.Required("event_id"): str,
        vol.Optional("reviewed", default=True): bool,
    }
)
@websocket_api.async_response
async def handle_mark_vision_reviewed(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Mark a vision event reviewed / unreviewed. Reviewing a Frigate-native event also
    marks the underlying Frigate review as reviewed in Frigate."""
    mgr = _manager(hass)
    event = await mgr.update_vision_event(msg["event_id"], reviewed=msg["reviewed"]) if mgr else None
    if event and msg["reviewed"]:
        if event.get("frigate_review_id"):
            await async_mark_frigate_reviewed(hass, [event["frigate_review_id"]])
        # Clear the toast this event created so the notification center clears everywhere.
        await mgr.dismiss_vision_notifications(msg["event_id"])
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/delete_vision_event",
        vol.Required("event_id"): str,
    }
)
@websocket_api.async_response
async def handle_delete_vision_event(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Delete one vision event and its snapshot/clip files."""
    mgr = _manager(hass)
    if mgr:
        removed = await mgr.remove_vision_event(msg["event_id"])
        if removed and getattr(mgr, "vision", None):
            await mgr.vision.cleanup_event(removed)
    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/clear_vision_events"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def handle_clear_vision_events(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Delete all vision events and their files (admin — the store is HA-wide)."""
    mgr = _manager(hass)
    if mgr:
        removed = await mgr.clear_vision_events()
        if removed and getattr(mgr, "vision", None):
            await mgr.vision.cleanup_events(removed)
    connection.send_result(msg["id"], {"success": True})

