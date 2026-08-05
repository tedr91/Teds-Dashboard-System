"""Detection of the optional Frigate integration for camera adoption.

Frigate is never required — TDS works fine without it. When it's present and
exposing cameras, TDS offers (or, for a fresh install, auto-applies) using those
cameras as the dashboard's camera source, and taps its native detection, event
thumbnails/clips, controls, and health entities for a tighter experience.
"""

from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

FRIGATE_DOMAIN = "frigate"

_LOGGER = logging.getLogger(__name__)


def frigate_url(hass: HomeAssistant) -> str | None:
    """The base URL of the first configured Frigate instance (for its web UI)."""
    for entry in hass.config_entries.async_entries(FRIGATE_DOMAIN):
        url = (entry.data or {}).get("url")
        if url:
            return str(url).rstrip("/")
    return None


def frigate_topic_prefix(hass: HomeAssistant) -> str:
    """Frigate's MQTT ``topic_prefix`` (default ``frigate``), read from its config."""
    for value in (hass.data.get(FRIGATE_DOMAIN) or {}).values():
        if isinstance(value, dict):
            prefix = ((value.get("config") or {}).get("mqtt") or {}).get("topic_prefix")
            if prefix:
                return str(prefix)
    return "frigate"


def is_frigate_camera(hass: HomeAssistant, camera_id: str) -> bool:
    """True when a camera entity is provided by the Frigate platform."""
    ent = er.async_get(hass).async_get(camera_id)
    return bool(ent and ent.platform == FRIGATE_DOMAIN)


def _friendly(name: str) -> str:
    return name.replace("_", " ").title()


def _frigate_camera_entity(hass: HomeAssistant, cam_name: str) -> str | None:
    """Resolve a Frigate MQTT camera name to its HA camera entity id."""
    for ent in er.async_get(hass).entities.values():
        if (
            ent.platform == FRIGATE_DOMAIN
            and ent.domain == "camera"
            and ent.unique_id.endswith(f":camera:{cam_name}")
        ):
            return ent.entity_id
    return None


def frigate_camera_entity(hass: HomeAssistant, cam_name: str) -> str | None:
    """Public alias of :func:`_frigate_camera_entity` for other modules."""
    return _frigate_camera_entity(hass, cam_name)


def detect_frigate(hass: HomeAssistant) -> dict:
    """Return Frigate presence and the camera entity ids it exposes.

    ``installed`` is True when the Frigate integration is loaded; ``cameras`` is the
    sorted list of enabled camera-domain entities provided by the Frigate platform.
    """
    installed = FRIGATE_DOMAIN in hass.config.components
    cameras: list[str] = []
    if installed:
        ent_reg = er.async_get(hass)
        cameras = sorted(
            e.entity_id
            for e in ent_reg.entities.values()
            if e.domain == "camera" and e.platform == FRIGATE_DOMAIN and not e.disabled
        )
    return {"installed": installed, "cameras": cameras}


def frigate_capability(*, installed: bool, cameras: list[str], adopted: bool, answered: bool) -> str:
    """Capability state used to gate the adoption prompts.

    - ``absent``:    Frigate isn't installed, or exposes no cameras.
    - ``adopted``:   Frigate is the chosen camera source (accepted or auto-opted-in).
    - ``dismissed``: the user declined the offer (kept for symmetry; not prompted).
    - ``available``: Frigate is present with cameras and hasn't been answered — prompt.
    """
    if not installed or not cameras:
        return "absent"
    if adopted:
        return "adopted"
    if answered:
        return "dismissed"
    return "available"


class FrigateEventBridge:
    """Turns Frigate review *alerts* into Ted's notifications with a real thumbnail
    and clip (via Frigate's public notification API), when Frigate is adopted and the
    MQTT integration is available. Enabled/disabled live from settings + adoption.
    """

    def __init__(self, hass: HomeAssistant, manager) -> None:
        self.hass = hass
        self.manager = manager
        self._unsub = None

    @property
    def active(self) -> bool:
        return self._unsub is not None

    async def async_update(self) -> None:
        """(Re)evaluate whether to be subscribed based on adoption + setting + MQTT."""
        want = (
            getattr(self.manager, "frigate_adopted", False)
            and bool((self.manager.effective_settings() or {}).get("frigate_notifications", True))
            and "mqtt" in self.hass.config.components
        )
        if want and self._unsub is None:
            await self._subscribe()
        elif not want and self._unsub is not None:
            self._unsub()
            self._unsub = None

    def shutdown(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _subscribe(self) -> None:
        from homeassistant.components import mqtt  # noqa: PLC0415

        topic = f"{frigate_topic_prefix(self.hass)}/reviews"
        try:
            self._unsub = await mqtt.async_subscribe(self.hass, topic, self._on_review, encoding=None)
        except Exception:  # noqa: BLE001 - MQTT not ready / not configured; try again later
            _LOGGER.debug("Frigate reviews subscription unavailable", exc_info=True)
            self._unsub = None

    @callback
    def _on_review(self, msg) -> None:
        try:
            data = json.loads(msg.payload)
        except (ValueError, TypeError):
            return
        # Notify once per review, when it ENDS as an alert (clip is available by then).
        if data.get("type") != "end":
            return
        after = data.get("after") or {}
        if str(after.get("severity") or "").lower() != "alert":
            return
        payload = after.get("data") or {}
        detections = payload.get("detections") or []
        event_id = detections[0] if detections else after.get("id")
        self.hass.async_create_task(
            self._notify(
                camera=str(after.get("camera") or ""),
                objects=[str(o) for o in (payload.get("objects") or [])],
                event_id=str(event_id) if event_id else None,
            )
        )

    async def _notify(self, camera: str, objects: list[str], event_id: str | None) -> None:
        cam_name = _friendly(camera) if camera else "Camera"
        label = ", ".join(_friendly(o) for o in objects) or "Activity"
        area_id = self._camera_area(camera)
        thumb = f"/api/frigate/notifications/{event_id}/thumbnail.jpg" if event_id else None
        clip = f"/api/frigate/notifications/{event_id}/clip.mp4" if event_id else None
        await self.manager.notify(
            f"{cam_name}: {label}",
            f"Frigate detected {label.lower()} on {cam_name}.",
            severity="warning",
            icon="mdi:cctv",
            area=area_id,
            notif_id=f"frigate-review-{event_id}" if event_id else None,
            source="frigate",
            data={"thumbnail_url": thumb, "clip_url": clip, "camera_name": cam_name},
        )

    def _camera_area(self, camera: str) -> str | None:
        cam_entity = _frigate_camera_entity(self.hass, camera)
        if not cam_entity:
            return None
        ent = er.async_get(self.hass).async_get(cam_entity)
        if ent is None:
            return None
        area_id = ent.area_id
        if area_id is None and ent.device_id:
            device = dr.async_get(self.hass).async_get(ent.device_id)
            area_id = device.area_id if device else None
        # Validate the area still exists (defensive; area registry is cheap).
        if area_id and ar.async_get(self.hass).async_get_area(area_id) is None:
            return None
        return area_id
