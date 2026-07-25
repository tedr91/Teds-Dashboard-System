"""WebSocket API for Ted's Cards Backend.

Non-admin HA users (e.g. kiosk / Wallpanel users) are not allowed to
`subscribe_events` for custom event types, so cards cannot listen to
`teds_cards_backend_notification` directly. This command lets any authenticated
user subscribe to notifications via a dedicated, non-admin command instead.
"""

from __future__ import annotations

import os

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback

from .bing_photos import (
    clear_bing_cache,
    favorite_bing_photo,
    fetch_and_cache_bing,
    import_photo,
    list_favorites,
    remove_bing_photo,
)
from .const import DOMAIN, EVENT_ASSIST_RESPONSE, EVENT_NAVIGATE, EVENT_NOTIFICATION, EVENT_SETTINGS

_REGISTERED = f"{DOMAIN}_ws_registered"

# Bundled wallpaper folders -> the category returned to the frontend.
_BACKGROUND_DIRS = {"general": "general", "light": "light-mode", "dark": "dark-mode"}
_BACKGROUND_EXTS = (".webp", ".jpg", ".jpeg", ".png", ".gif", ".avif")
_BACKGROUND_URL = "/teds_cards_backend/backgrounds"

# Bundled alert sounds live here and are served at this URL prefix.
_SOUNDS_URL = "/teds_cards_backend/sounds"
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
    websocket_api.async_register_command(hass, handle_register_device)
    websocket_api.async_register_command(hass, handle_list_backgrounds)
    websocket_api.async_register_command(hass, handle_list_sounds)
    websocket_api.async_register_command(hass, handle_list_bing_photos)
    websocket_api.async_register_command(hass, handle_clear_bing_photos_cache)
    websocket_api.async_register_command(hass, handle_favorite_bing_photo)
    websocket_api.async_register_command(hass, handle_remove_bing_photo)
    websocket_api.async_register_command(hass, handle_favorite_photo)
    websocket_api.async_register_command(hass, handle_store_background_photo)
    websocket_api.async_register_command(hass, handle_list_favorites)
    websocket_api.async_register_command(hass, handle_media_folder)
    hass.data[_REGISTERED] = True


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
    """Delete a single cached Bing image from the cache."""
    ok = await remove_bing_photo(hass, msg["filename"])
    connection.send_result(msg["id"], {"success": ok})


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

