"""Detection of the optional Frigate integration for camera adoption.

Frigate is never required — TDS works fine without it. When it's present and
exposing cameras, TDS offers (or, for a fresh install, auto-applies) using those
cameras as the dashboard's camera source.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

FRIGATE_DOMAIN = "frigate"


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
